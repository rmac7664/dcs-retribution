"""Per-mission logistics plan: fits loadouts to stock and fills DCS warehouses."""

from __future__ import annotations

import hashlib
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING
from uuid import UUID

from dcs.action import DoScript
from dcs.translation import String
from dcs.triggers import TriggerStart

from game.data.weapons import Pylon, Weapon
from game.theater.controlpoint import Airfield, ControlPoint, NavalControlPoint
from .munitions import MunitionSource, ammo_only, metered_prefixes
from .state import KG_PER_TON, WarehouseState, short_name

if TYPE_CHECKING:
    from dcs import Mission
    from dcs.flyingunit import FlyingUnit

    from game import Game
    from game.ato import Flight
    from game.ato.flightmember import FlightMember
    from game.ato.loadouts import Loadout
    from game.missiongenerator.missiondata import MissionData

#: Stock written for warehouse items that aren't limited, so DCS never strips them.
UNLIMITED_AMOUNT = 10000

#: Name of the Lua global the mission script reads (dcs_retribution_warehouses.lua).
LUA_DATA_GLOBAL = "dcsRetributionWarehouses"


@dataclass
class BasePlan:
    cp: ControlPoint
    fuel_kg: float
    munitions: dict[str, int]
    #: Stock left after the loadouts generated so far took what they need.
    available: Counter[str] = field(default_factory=Counter)
    #: DCS airbase name the mission script looks the warehouse up by.
    dcs_name: Optional[str] = None
    #: True if DCS should not limit munitions here this mission (see `calibrating`).
    calibrate: bool = False
    fuel_demand_kg: float = 0.0
    #: Munition (short name) -> pylons that had to fall back or be emptied.
    shortages: Counter[str] = field(default_factory=Counter)
    downgraded: int = 0


class MissionWarehousePlan:
    """Built at the start of mission generation when logistics is enabled.

    Loadouts are generated flight by flight; each one is checked against what is still
    on the shelf at its departure base. Anything the base can't supply falls back to an
    older weapon from the same family (the same chain date restrictions use) or leaves
    the pylon empty.

    A base "calibrates" munitions when DCS can't be trusted to limit them correctly yet:
    on the first mission (no wsType cache) or when a planned loadout uses a launcher
    whose contents DCS hasn't confirmed (mostly mod weapons). DCS munitions stay
    unlimited at that base for one mission while the mission script still meters what
    is used and reports what each unverified launcher really carries.
    """

    def __init__(self, game: Game) -> None:
        self.game = game
        self.state: WarehouseState = game.warehouse_logistics
        self.catalog = self.state.catalog
        self.prefixes = metered_prefixes(game.settings)
        self.bases: dict[UUID, BasePlan] = {}
        self.loadout_keys: dict[str, list[str]] = {}
        self.learn_keys: set[str] = set()
        self.units: dict[str, dict[str, str]] = {}
        #: Supply airlift unit name -> destination control point id.
        self.cargo_units: dict[str, str] = {}
        #: Player supply airlifts carried as CTLD crates: lead unit name -> run.
        self.crate_runs: dict[str, dict[str, Any]] = {}
        no_wstypes = not self.state.resource_map
        for cp in self.state.managed_points(game):
            stock = self.state.ensure_stock(game, cp)
            self.bases[cp.id] = BasePlan(
                cp=cp,
                fuel_kg=stock.jet_fuel_kg,
                munitions=dict(stock.munitions),
                available=Counter(stock.munitions),
                calibrate=no_wstypes,
            )

    def __getstate__(self) -> dict[str, Any]:
        # Only what's needed to learn from the mission results survives a save.
        return {
            "loadout_keys": self.loadout_keys,
            "learn_keys": self.learn_keys,
            "cargo_units": self.cargo_units,
            "crate_runs": getattr(self, "crate_runs", {}),
        }

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        self.cargo_units = state.get("cargo_units", {})
        self.crate_runs = state.get("crate_runs", {})
        self.bases = {}
        self.units = {}

    @classmethod
    def create(cls, game: Game) -> Optional[MissionWarehousePlan]:
        if not game.settings.logistics_enabled:
            return None
        return cls(game)

    def manages(self, cp: ControlPoint) -> bool:
        return cp.id in self.bases

    # Loadouts -----------------------------------------------------------------------

    def _metered(self, munitions: dict[str, int]) -> dict[str, int]:
        return {
            k: v for k, v in ammo_only(munitions).items() if k.startswith(self.prefixes)
        }

    @staticmethod
    def _fits(base: BasePlan, need: dict[str, int]) -> bool:
        return all(base.available[k] >= v for k, v in need.items())

    @staticmethod
    def _take(base: BasePlan, need: dict[str, int]) -> None:
        for k, v in need.items():
            base.available[k] -= v

    def constrain(self, flight: Flight, loadout: Loadout) -> Loadout:
        """The loadout this flight member can actually get from its base."""
        from game.ato.loadouts import Loadout

        base = self.bases.get(flight.departure.id)
        if base is None:
            return loadout
        pylons: dict[int, Optional[Weapon]] = dict(loadout.pylons)
        settings = dict(loadout.pylon_settings)
        faction = flight.squadron.coalition.faction
        changed = False
        for number, weapon in sorted(loadout.pylons.items()):
            if weapon is None:
                continue
            result = self.catalog.lookup(weapon)
            if result is None or result[1] is MunitionSource.HEURISTIC:
                base.calibrate = True
            if result is None:
                continue
            need = self._metered(result[0])
            if self._fits(base, need):
                self._take(base, need)
                continue
            replacement = self._fallback(base, flight, number, weapon, faction)
            changed = True
            base.downgraded += 1
            for name in need:
                if base.available[name] < need[name]:
                    base.shortages[short_name(name)] += 1
            settings.pop(number, None)
            if replacement is None:
                del pylons[number]
            else:
                pylons[number] = replacement
        if not changed:
            return loadout
        return Loadout(
            loadout.name,
            pylons,
            loadout.date,
            loadout.is_custom,
            pylon_settings=settings,
        )

    def _fallback(
        self,
        base: BasePlan,
        flight: Flight,
        number: int,
        weapon: Weapon,
        faction: Any,
    ) -> Optional[Weapon]:
        pylon = Pylon.for_aircraft(flight.unit_type, number)
        for candidate in weapon.fallbacks:
            if candidate == weapon or not pylon.can_equip(candidate):
                continue
            if self.game.settings.restrict_weapons_by_date and not (
                candidate.available_on(self.game.date, faction)
            ):
                continue
            result = self.catalog.lookup(candidate)
            if result is None or result[1] is MunitionSource.HEURISTIC:
                continue
            need = self._metered(result[0])
            if self._fits(base, need):
                self._take(base, need)
                return candidate
        return None

    def register_unit(self, flight: Flight, unit: FlyingUnit, loadout: Loadout) -> None:
        transfer = getattr(flight, "cargo", None)
        if transfer is not None and getattr(transfer, "supplies", None) is not None:
            destination = transfer.destination
            if transfer.transport is not None:
                destination = transfer.transport.destination
            # The mission script only sees landings at bases with a warehouse plan
            # (airfields, carriers). A leg to a FOB or helipad can't be confirmed, so
            # it counts as delivered unless the transport is shot down.
            if destination.id in self.bases:
                self.cargo_units[str(unit.name)] = str(destination.id)
        base = self.bases.get(flight.departure.id)
        if base is None:
            return
        base.fuel_demand_kg += float(unit.fuel or 0)
        clsids = sorted(w.clsid for w in loadout.pylons.values() if w is not None)
        entry = {"home": str(base.cp.id)}
        if clsids:
            key = hashlib.sha1("|".join(clsids).encode("utf-8")).hexdigest()[:12]
            self.loadout_keys[key] = clsids
            entry["key"] = key
            untrusted = any(
                (r := self.catalog.lookup(w)) is None
                or r[1] is MunitionSource.HEURISTIC
                for w in loadout.pylons.values()
                if w is not None
            )
            if untrusted:
                self.learn_keys.add(key)
        self.units[str(unit.name)] = entry

    def register_crate_run(
        self,
        unit_names: list[str],
        destination: ControlPoint,
        crates: int,
        weight_kg: int,
        spawn_zone: str,
        side: str,
        unit_type: str,
        description: str,
    ) -> None:
        """A player supply airlift whose cargo is CTLD crates instead of a landing.

        Delivery is counted per crate set down near `destination`, so the landing of
        its aircraft no longer decides it.
        """
        for name in unit_names:
            self.cargo_units.pop(name, None)
        self.crate_runs[unit_names[0]] = {
            "dest": str(destination.id),
            "dest_name": destination.name,
            "x": round(destination.position.x, 1),
            "z": round(destination.position.y, 1),
            "crates": crates,
            "weight": weight_kg,
            "zone": spawn_zone,
            "side": side,
            "unit": unit_type,
            "desc": description,
        }

    # Mission file ---------------------------------------------------------------------

    def apply_to_mission(self, mission: Mission, mission_data: MissionData) -> None:
        """Writes stock into the DCS warehouses and the data the mission script needs."""
        weapons_template = [
            (name, ws)
            for name, ws in sorted(self.state.resource_map.items())
            if name.startswith("weapons.")
            and not name.startswith("weapons.shells.")
            and any(ws)
        ]
        carrier_names = {
            info.ship_group.units[0].id: info.unit_name
            for info in mission_data.carriers
            if info.ship_group.units
        }
        for base in list(self.bases.values()):
            cp = base.cp
            # The mission editor stores whole tons.
            fuel_tons = int(base.fuel_kg // KG_PER_TON)
            limit_munitions = not base.calibrate and bool(weapons_template)
            weapons = (
                [
                    {
                        "wsType": list(ws),
                        "initialAmount": (
                            int(base.munitions.get(name, 0))
                            if name.startswith(self.prefixes)
                            else UNLIMITED_AMOUNT
                        ),
                    }
                    for name, ws in weapons_template
                ]
                if limit_munitions
                else []
            )
            if isinstance(cp, Airfield):
                airport = mission.terrain.airport_by_id(cp.airport.id)
                if airport is None:
                    del self.bases[cp.id]
                    continue
                base.dcs_name = airport.name
                airport.unlimited_fuel = False
                airport.jet_init = fuel_tons
                airport.gasoline_init = fuel_tons
                if limit_munitions:
                    airport.unlimited_munitions = False
                    airport.weapons = weapons  # type: ignore[assignment]
            elif isinstance(cp, NavalControlPoint):
                carrier_id = getattr(cp, "carrier_id", None)
                if carrier_id is None:
                    del self.bases[cp.id]
                    continue
                warehouse = mission.warehouses.warehouses.get(carrier_id)
                name = carrier_names.get(carrier_id)
                if warehouse is None or name is None:
                    del self.bases[cp.id]
                    continue
                base.dcs_name = name
                warehouse["unlimitedFuel"] = False
                warehouse["jet_fuel"] = {"InitFuel": fuel_tons}
                warehouse["gasoline"] = {"InitFuel": fuel_tons}
                if limit_munitions:
                    warehouse["unlimitedMunitions"] = False
                    warehouse["weapons"] = weapons
            else:
                del self.bases[cp.id]

        # Bases dropped above have no warehouse in the mission, so the script can't
        # confirm landings there either: those airlifts count as delivered.
        known = {str(cp_id) for cp_id in self.bases}
        self.cargo_units = {
            name: dest for name, dest in self.cargo_units.items() if dest in known
        }

        trigger = TriggerStart(comment="Set DCS Retribution warehouse data")
        trigger.add_action(
            DoScript(
                String(f"{LUA_DATA_GLOBAL} = {to_lua(self.lua_data(mission_data))}")
            )
        )
        # Must run before the plugin scripts that read it.
        mission.triggerrules.triggers.insert(0, trigger)
        self.report()

    def lua_data(self, mission_data: Optional[MissionData] = None) -> dict[str, Any]:
        bases: dict[str, Any] = {}
        ws: dict[str, list[int]] = {}
        for base in self.bases.values():
            if base.dcs_name is None:
                continue
            metered = {
                k: int(v)
                for k, v in base.munitions.items()
                if k.startswith(self.prefixes)
            }
            bases[base.dcs_name] = {
                "id": str(base.cp.id),
                "fuel_kg": round(base.fuel_kg, 1),
                "calibrate": base.calibrate,
                "mun": metered,
            }
            if not base.calibrate:
                for name in metered:
                    if name in self.state.resource_map:
                        ws[name] = self.state.resource_map[name]
        return {
            "version": 1,
            "prefixes": list(self.prefixes),
            "bases": bases,
            "units": self.units,
            "learn": {key: True for key in sorted(self.learn_keys)},
            "cargo": self.cargo_units,
            # CTLD supply crates: crate weight (identifies the crate type) -> run.
            "crates": {
                str(run["weight"]): dict(run, key=key)
                for key, run in getattr(self, "crate_runs", {}).items()
            },
            "ws": ws,
            "wantResourceMap": True,
            # Replenishment ship unit name -> the carrier it sails to and its cargo.
            "replenishment": {
                info.unit_name: {
                    "carrier": info.carrier_unit_name,
                    "cp": info.carrier_cp_id,
                    "mun": dict(info.munitions),
                    "fuel": round(info.fuel_kg, 1),
                }
                for info in (mission_data.replenishment_ships if mission_data else [])
            },
        }

    def report(self) -> None:
        for base in self.bases.values():
            lines = []
            if base.shortages:
                worst = ", ".join(
                    f"{name} ({count})" for name, count in base.shortages.most_common(6)
                )
                lines.append(
                    f"{base.downgraded} pylon(s) fell back or were left empty for "
                    f"lack of: {worst}."
                )
            if base.fuel_demand_kg > base.fuel_kg:
                lines.append(
                    f"Planned flights need about {base.fuel_demand_kg / KG_PER_TON:.0f} t "
                    f"of fuel but only {base.fuel_kg / KG_PER_TON:.0f} t is stored."
                )
            if lines and base.cp.captured.is_blue:
                self.game.message(f"Logistics: {base.cp.name}", " ".join(lines))
            for line in lines:
                logging.info("Warehouse logistics: %s: %s", base.cp.name, line)
        from .supply import supply_of

        stranded = [
            t
            for t in self.game.blue.transfers.pending_transfers
            if supply_of(t) is not None and t.transport is None
        ]
        if stranded:
            self.game.message(
                "Logistics: supply runs waiting",
                f"{len(stranded)} supply run(s) have no transport this turn: "
                + "; ".join(str(t) for t in stranded[:4])
                + ". Airlifts need transport-capable squadrons (C-130, C-17, CH-47...) "
                "that can reach the depot.",
            )
        calibrating = [b.cp.name for b in self.bases.values() if b.calibrate]
        if calibrating:
            logging.info(
                "Warehouse logistics: munitions calibrate this mission (unlimited in "
                "DCS, still metered) at: %s",
                ", ".join(calibrating),
            )


def to_lua(value: Any) -> str:
    """Serializes plain Python data as a Lua table constructor."""
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, int) else f"{value:.6g}"
    if isinstance(value, str):
        escaped = (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    if isinstance(value, dict):
        items = ",".join(f"[{to_lua(str(k))}]={to_lua(v)}" for k, v in value.items())
        return "{" + items + "}"
    if isinstance(value, (list, tuple, set)):
        return "{" + ",".join(to_lua(v) for v in value) + "}"
    raise TypeError(f"Cannot serialize {type(value)} to Lua")
