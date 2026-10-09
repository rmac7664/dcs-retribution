"""Campaign-persistent stock of fuel and munitions at each base."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, TYPE_CHECKING
from uuid import UUID

from game.theater.controlpoint import (
    Airfield,
    ControlPoint,
    NavalControlPoint,
    OffMapSpawn,
)
from .munitions import (
    MunitionCatalog,
    ammo_only,
    is_ammo,
    learn_from_observations,
    metered_prefixes,
)

if TYPE_CHECKING:
    from game import Game
    from game.ato.flighttype import FlightType
    from game.dcs.aircrafttype import AircraftType
    from game.settings import Settings
    from .plan import MissionWarehousePlan
    from .supply import MunitionOrder, PurchaseReport
    from game.debriefing import Debriefing

KG_PER_TON = 1000.0

#: Turns a carrier supply ship spends at sea. Ships set out about an hour and a half's
#: sailing from the carrier, so they arrive during the next turn.
SUPPLY_SHIP_TRANSIT_TURNS = 1


@dataclass
class SupplyShip:
    """A replenishment ship sailing to a carrier or LHA with its stores.

    Carriers have no shipping lanes (they move), so these don't use the transfer
    system. What the ship carries counts as inbound, so nothing is ordered twice.
    """

    carrier_id: UUID
    carrier_name: str
    #: Name of the Player (side) that sent the ship.
    side: str
    munitions: dict[str, int] = field(default_factory=dict)
    fuel_kg: float = 0.0
    turns_left: int = SUPPLY_SHIP_TRANSIT_TURNS
    #: Its group name in the mission, so strikes planned against it can find it.
    name: str = ""

    def is_empty(self) -> bool:
        return not any(self.munitions.values()) and self.fuel_kg < 1


@dataclass
class BaseStock:
    jet_fuel_kg: float
    munitions: dict[str, int] = field(default_factory=dict)

    def fuel_tons(self) -> float:
        return self.jet_fuel_kg / KG_PER_TON


@dataclass
class MissionResultSummary:
    """What the last mission did to stock, for the debriefing and the log."""

    fuel_used_kg: dict[str, float] = field(default_factory=dict)
    munitions_used: dict[str, dict[str, int]] = field(default_factory=dict)
    learned_launchers: int = 0
    diagnostics: dict[str, Any] = field(default_factory=dict)


class WarehouseState:
    """Everything the logistics system persists in the save game."""

    def __init__(self) -> None:
        #: Stock at each managed control point, by control point ID.
        self.stocks: dict[UUID, BaseStock] = {}
        #: DCS resource name -> wsType, reported by DCS at the end of each mission.
        #: wsTypes change between DCS versions, so this is refreshed every mission.
        self.resource_map: dict[str, list[int]] = {}
        #: Launcher CLSID -> warehouse items, learned from what DCS reported.
        self.learned: dict[str, dict[str, int]] = {}
        #: Plan for the mission currently being flown (needed to learn from results).
        self.pending_plan: Optional[MissionWarehousePlan] = None
        self.last_result: Optional[MissionResultSummary] = None
        #: Last turn's purchases and shipments per side (Player name -> report).
        self.last_purchase: dict[str, PurchaseReport] = {}
        #: The player's munition orders this turn: depot ID -> resource -> order.
        #: Paid when placed, delivered at turn end.
        self.orders: dict[UUID, dict[str, MunitionOrder]] = {}
        #: Replenishment ships at sea, bound for carriers and LHAs.
        self.supply_ships: list[SupplyShip] = []
        #: Player-picked main supply base by side name (see supply.main_base).
        self.main_bases: dict[str, UUID] = {}
        #: Enemy main bases the player has found by striking or scouting them.
        self.revealed_main_bases: set[UUID] = set()
        #: Numbers replenishment ships, for unique mission group names.
        self.ship_serial = 0
        #: DCS SAM launcher type -> missiles it carries (learned; see sam.py).
        self.sam_loads: dict[str, dict[str, int]] = {}
        #: "cp id|SAM missile" already given starting stock (see sam.py).
        self.sam_seeded: set[str] = set()
        #: Not persisted: (aircraft, date, faction, learned count) -> max load.
        self._load_cache: dict[tuple[str, Any, str, int], dict[str, int]] = {}

    def __getstate__(self) -> dict[str, Any]:
        state = dict(self.__dict__)
        state.pop("_load_cache", None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        fresh = WarehouseState().__dict__
        fresh.update(state)
        self.__dict__.update(fresh)
        # Ships that sailed before they had names (older saves) get one now, so
        # strikes planned against them can find them in the mission.
        for ship in self.supply_ships:
            if not getattr(ship, "name", ""):
                self.ship_serial += 1
                ship.name = f"{ship.carrier_name} replenishment {self.ship_serial}"

    @property
    def catalog(self) -> MunitionCatalog:
        return MunitionCatalog(self.learned)

    # Which bases are managed ------------------------------------------------------

    @staticmethod
    def is_managed(cp: ControlPoint, settings: Settings) -> bool:
        if not settings.logistics_enabled:
            return False
        if cp.captured.is_neutral:
            return False
        if not (cp.captured.is_blue or settings.logistics_apply_to_opfor):
            return False
        if isinstance(cp, (Airfield, OffMapSpawn)):
            # Off-map spawns are the rear-area depot; they have no DCS warehouse.
            return True
        # A sunk carrier or LHA is no longer a base: nothing is stocked or shipped
        # for it (its squadrons were lost with it).
        return (
            isinstance(cp, NavalControlPoint)
            and (cp.is_carrier or cp.is_lha)
            and cp.runway_is_operational()
        )

    def managed_points(self, game: Game) -> Iterator[ControlPoint]:
        for cp in game.theater.controlpoints:
            if self.is_managed(cp, game.settings):
                yield cp

    # Authorized levels -------------------------------------------------------------

    @staticmethod
    def fuel_capacity_kg(cp: ControlPoint, settings: Settings) -> float:
        if isinstance(cp, NavalControlPoint):
            return settings.logistics_ship_fuel_tons * KG_PER_TON
        if isinstance(cp, OffMapSpawn):
            return settings.logistics_airfield_fuel_tons * 4 * KG_PER_TON
        return settings.logistics_airfield_fuel_tons * KG_PER_TON

    def authorized_munitions(self, game: Game, cp: ControlPoint) -> dict[str, int]:
        """Munitions a base is stocked for: N sorties of the loads its squadrons fly.

        Each squadron is stocked for the default loadouts of the missions it flies (its
        auto-assignable mission types): per aircraft, the most of each munition any of
        those loadouts carries. A squadron whose missions have no loadouts with
        munitions falls back to every loadout its aircraft has.
        """
        prefixes = metered_prefixes(game.settings)
        sorties = game.settings.logistics_munitions_sorties
        authorized: Counter[str] = Counter()
        for squadron in cp.squadrons:
            count = squadron.owned_aircraft
            if count <= 0:
                continue
            tasks = set(squadron.auto_assignable_mission_types) | {
                squadron.primary_task
            }
            loads = self._task_loads(game, cp, squadron.aircraft, tasks)
            if not any(name.startswith(prefixes) for name in loads):
                loads = self._max_loads(game, cp, squadron.aircraft)
            for name, per_aircraft in loads.items():
                if name.startswith(prefixes):
                    authorized[name] += per_aircraft * count * sorties
        from .sam import authorized_sam

        authorized.update(authorized_sam(game, cp))
        return dict(authorized)

    def _task_loads(
        self,
        game: Game,
        cp: ControlPoint,
        aircraft: AircraftType,
        tasks: set[FlightType],
    ) -> dict[str, int]:
        """Per aircraft, the most of each munition the tasks' default loadouts carry."""
        from game.ato.loadouts import Loadout

        faction = cp.coalition.faction
        task_key = ",".join(sorted(t.value for t in tasks))
        key = (
            f"{aircraft.variant_id}|{task_key}",
            game.date,
            faction.name,
            len(self.learned),
        )
        cached = self._load_cache.get(key)
        if cached is not None:
            return cached
        catalog = self.catalog
        best: Counter[str] = Counter()
        for task in tasks:
            loadout = Loadout.default_for_task_and_aircraft(
                task, aircraft.dcs_unit_type
            )
            if game.settings.restrict_weapons_by_date:
                loadout = loadout.degrade_for_date(aircraft, game.date, faction)
            weapons = [w for w in loadout.pylons.values() if w is not None]
            demand, _, _ = catalog.demand(weapons)
            for name, count in demand.items():
                best[name] = max(best[name], count)
        self._load_cache[key] = dict(best)
        return self._load_cache[key]

    def _max_loads(
        self, game: Game, cp: ControlPoint, aircraft: AircraftType
    ) -> dict[str, int]:
        from game.ato.loadouts import Loadout

        faction = cp.coalition.faction
        key = (aircraft.variant_id, game.date, faction.name, len(self.learned))
        cached = self._load_cache.get(key)
        if cached is not None:
            return cached
        catalog = self.catalog
        best: Counter[str] = Counter()
        for loadout in Loadout.iter_for_aircraft(aircraft):
            if game.settings.restrict_weapons_by_date:
                loadout = loadout.degrade_for_date(aircraft, game.date, faction)
            weapons = [w for w in loadout.pylons.values() if w is not None]
            demand, _, _ = catalog.demand(weapons)
            for name, count in demand.items():
                best[name] = max(best[name], count)
        self._load_cache[key] = dict(best)
        return self._load_cache[key]

    # Lifecycle --------------------------------------------------------------------

    def ensure_stock(self, game: Game, cp: ControlPoint) -> BaseStock:
        """The base's stock, created on first sight.

        Bases the logistics system manages (airfields, carriers, the rear area) start
        full, scaled by the player's or enemy's starting supply setting. Anything else seen for the first time, such as a FOB holding stock for
        shipping, starts empty and fills from production or deliveries.
        """
        stock = self.stocks.get(cp.id)
        if stock is None:
            if self.is_managed(cp, game.settings):
                settings = game.settings
                fill = (
                    settings.logistics_player_starting_supply
                    if cp.captured.is_blue
                    else settings.logistics_enemy_starting_supply
                )
                stock = BaseStock(
                    jet_fuel_kg=self.fuel_capacity_kg(cp, settings) * fill,
                    munitions={
                        name: int(count * fill)
                        for name, count in self.authorized_munitions(game, cp).items()
                        if int(count * fill) > 0
                    },
                )
            else:
                stock = BaseStock(jet_fuel_kg=0.0)
            self.stocks[cp.id] = stock
        return stock

    def receive(
        self,
        game: Game,
        cp: ControlPoint,
        munitions: dict[str, int],
        fuel_kg: float = 0.0,
    ) -> None:
        """Adds delivered or returned cargo to a base's stock.

        A base with no stock yet starts from empty, so cargo arriving somewhere new
        never comes with a free full warehouse.
        """
        stock = self.stocks.get(cp.id)
        if stock is None:
            stock = BaseStock(jet_fuel_kg=0.0)
            self.stocks[cp.id] = stock
        for name, count in munitions.items():
            stock.munitions[name] = stock.munitions.get(name, 0) + count
        stock.jet_fuel_kg += fuel_kg

    def resupply(self, game: Game) -> None:
        """Turn end: each side buys munitions at its depots and ships them forward."""
        from .supply import SupplyPlanner, report_lines, update_main_bases

        for line in update_main_bases(game):
            game.message("Logistics: main supply base", line)
        for coalition in (game.blue, game.red):
            if coalition.player.is_red and not game.settings.logistics_apply_to_opfor:
                continue
            # Ships that left last turn unload first, so they aren't counted as both
            # stock and inbound when this turn's needs are worked out.
            arrived, lost = self.arrive_supply_ships(game, coalition.player.name)
            report = SupplyPlanner(game, coalition).run()
            report.ships_arrived += arrived
            report.ships_lost.extend(lost)
            self.last_purchase[coalition.player.name] = report
            if coalition.player.is_blue:
                lines = report_lines(report, game.settings)
                if lines:
                    game.message("Logistics: supply", " ".join(lines))
            elif report.bought:
                logging.info(
                    "Supply: OPFOR bought %d munitions for %.1f",
                    sum(report.bought.values()),
                    report.spent,
                )

    def arrive_supply_ships(self, game: Game, side: str) -> tuple[int, list[str]]:
        """Advances one side's supply ships; those due unload at their carrier.

        Returns how many unloaded, and the names of carriers whose ship had nobody to
        unload to (the carrier was sunk or changed hands), so its cargo was lost.
        """
        arrived = 0
        lost: list[str] = []
        at_sea: list[SupplyShip] = []
        for ship in self.supply_ships:
            if ship.side != side:
                at_sea.append(ship)
                continue
            ship.turns_left -= 1
            if ship.turns_left > 0:
                at_sea.append(ship)
                continue
            try:
                carrier: Optional[ControlPoint] = game.theater.find_control_point_by_id(
                    ship.carrier_id
                )
            except KeyError:
                carrier = None
            if (
                carrier is None
                or carrier.captured.name != side
                or not carrier.runway_is_operational()
            ):
                logging.info(
                    "Supply: ship for %s lost its carrier; cargo lost",
                    ship.carrier_name,
                )
                lost.append(ship.carrier_name)
                continue
            self.receive(game, carrier, ship.munitions, ship.fuel_kg)
            arrived += 1
            logging.info("Supply: replenishment ship unloaded at %s", carrier.name)
        self.supply_ships = at_sea
        return arrived, lost

    def apply_replenishment(
        self,
        game: Game,
        debriefing: Debriefing,
        data: Optional[dict[str, Any]],
    ) -> None:
        """Replenishment ships that were in the mission: unloaded, sunk, or still out.

        A ship that came alongside unloaded straight into the carrier's DCS warehouse;
        the mission script reports it as replenished (without booking it to the
        ledger), and its cargo is added to the carrier's stock here, whether or not the
        mission ended cleanly. A sunk ship loses its cargo. Ships still sailing arrive
        at the end of the turn as usual.
        """
        reported = _as_dict(data.get("replenished")) if data else {}
        unloaded: list[str] = []
        for name in reported:
            ship = debriefing.unit_map.replenishment_ship(str(name))
            if ship is None or not self._remove_ship(ship):
                continue
            unloaded.append(ship.carrier_name)
            try:
                carrier = game.theater.find_control_point_by_id(ship.carrier_id)
            except KeyError:
                continue
            self.receive(game, carrier, ship.munitions, ship.fuel_kg)
        for ship in debriefing.replenishment_ships_lost():
            if self._remove_ship(ship):
                logging.info(
                    "Supply: replenishment ship for %s was sunk with its cargo",
                    ship.carrier_name,
                )
                if ship.side == "BLUE":
                    game.message(
                        "Logistics: replenishment ship sunk",
                        f"The replenishment ship sailing for {ship.carrier_name} was "
                        "sunk; its fuel and munitions were lost.",
                    )
        if unloaded:
            logging.info("Supply: replenishment ships unloaded at %s", unloaded)

    def _remove_ship(self, ship: SupplyShip) -> bool:
        """Takes `ship` (this exact one) off the list of ships at sea."""
        for i, at_sea in enumerate(self.supply_ships):
            if at_sea is ship:
                del self.supply_ships[i]
                return True
        return False

    def ships_bound_for(self, cp: ControlPoint) -> Iterator[SupplyShip]:
        for ship in self.supply_ships:
            if ship.carrier_id == cp.id:
                yield ship

    def on_capture(self, game: Game, cp: ControlPoint) -> None:
        """A base changed hands: its munitions are destroyed, half its fuel survives.

        Called before the base flips. A base the old owner never stocked (e.g. OPFOR
        with logistics off) is taken as having been full, so the captor finds half a
        fuel farm and no munitions rather than a fully stocked warehouse.
        """
        if not game.settings.logistics_enabled:
            return
        stock = self.stocks.get(cp.id)
        if stock is not None:
            fuel_kg = stock.jet_fuel_kg
        elif cp.captured.is_neutral:
            fuel_kg = 0.0
        else:
            fuel_kg = self.fuel_capacity_kg(cp, game.settings)
        self.stocks[cp.id] = BaseStock(jet_fuel_kg=fuel_kg / 2, munitions={})
        logging.info(
            "Supply: %s (held by %s) captured; munitions destroyed, %.1f t of fuel "
            "remain",
            cp.name,
            getattr(cp.captured, "name", "?"),
            fuel_kg / 2 / KG_PER_TON,
        )

    # Mission results ----------------------------------------------------------------

    def apply_airlift_deliveries(
        self, game: Game, debriefing: Debriefing, data: Optional[dict[str, Any]]
    ) -> None:
        """Supply airlifters that didn't land at their destination bring cargo back.

        Applied whenever the mission script reported, even if the mission didn't end
        cleanly: an airlifter that hadn't landed when the mission stopped didn't
        deliver. Without any report (the mission wasn't flown in DCS) transports
        are assumed to arrive. Shot-down transports are handled by the regular
        airlift loss processing.
        """
        from .supply import return_to, supply_of

        plan = self.pending_plan
        if plan is None or not data:
            return
        self._apply_crate_deliveries(game, debriefing, data)
        if not getattr(plan, "cargo_units", None):
            return
        delivered = _as_dict(data.get("delivered"))
        killed = set(debriefing.state_data.killed_aircraft)
        for name in plan.cargo_units:
            if name in delivered or name in killed:
                continue
            airlift = debriefing.unit_map.airlift_unit(name)
            if airlift is None:
                continue
            transfer = airlift.transfer
            if supply_of(transfer) is None:
                continue
            returned = 0
            for unit_type in airlift.cargo:
                try:
                    transfer.kill_unit(unit_type)
                    returned += 1
                except KeyError:
                    continue
            return_to(transfer, transfer.position, returned)
            logging.info(
                "Supply: %s did not land at %s; its cargo stays at %s",
                name,
                transfer.destination.name,
                transfer.position.name,
            )

    def _apply_crate_deliveries(
        self, game: Game, debriefing: Debriefing, data: dict[str, Any]
    ) -> None:
        """Player supply airlifts carried as CTLD crates: count what was set down.

        Each crate the mission script saw set down near the destination unloads there
        now; the rest (left at the pickup, still slung, or lost) go back to where the
        run started. The order is then empty, so nothing else delivers it again.
        """
        from .supply import return_to, supply_of

        plan = self.pending_plan
        runs = getattr(plan, "crate_runs", None) or {}
        reported = _as_dict(data.get("crates_delivered"))
        for key, run in runs.items():
            airlift = debriefing.unit_map.airlift_unit(key)
            if airlift is None:
                continue
            transfer = airlift.transfer
            load = supply_of(transfer)
            if load is None or transfer.size <= 0 or len(transfer.units) != 1:
                continue
            (truck,) = transfer.units
            crates = min(int(run.get("crates", 0)), transfer.size)
            delivered = max(0, min(int(reported.get(key, 0) or 0), crates))
            try:
                destination: Optional[ControlPoint] = (
                    game.theater.find_control_point_by_id(UUID(str(run["dest"])))
                )
            except (KeyError, ValueError):
                destination = None
            if delivered and destination is not None:
                cargo = load.split_off(delivered)
                for _ in range(delivered):
                    transfer.kill_unit(truck)
                self.receive(game, destination, cargo.munitions, cargo.fuel_kg)
            back = transfer.size
            for _ in range(back):
                transfer.kill_unit(truck)
            if back:
                return_to(transfer, transfer.position, back)
            logging.info(
                "Supply: %d of %d CTLD supply crates delivered to %s; %d returned to %s",
                delivered,
                crates,
                run.get("dest_name", "?"),
                back,
                transfer.position.name,
            )

    def apply_mission_results(
        self, game: Game, data: Optional[dict[str, Any]], mission_ended: bool
    ) -> None:
        plan = self.pending_plan
        self.pending_plan = None
        if not data:
            return
        summary = MissionResultSummary()

        resource_map = data.get("resource_map")
        if isinstance(resource_map, dict) and resource_map:
            self.resource_map = {
                str(k): [int(x) for x in v]
                for k, v in resource_map.items()
                if isinstance(v, list) and len(v) == 4
            }
            logging.info(
                "Warehouse logistics: cached %d DCS warehouse items",
                len(self.resource_map),
            )

        if plan is not None:
            observations = []
            for key, observed in _as_dict(data.get("learned")).items():
                clsids = plan.loadout_keys.get(str(key))
                if clsids is not None:
                    observations.append(
                        (
                            clsids,
                            {str(k): int(v) for k, v in _as_dict(observed).items()},
                        )
                    )
            learned = learn_from_observations(self.catalog, self.learned, observations)
            summary.learned_launchers = len(learned)
            if learned:
                logging.info(
                    "Warehouse logistics: learned %d launchers from DCS: %s",
                    len(learned),
                    ", ".join(learned),
                )

        summary.diagnostics = _as_dict(data.get("diag"))
        if data.get("stale"):
            logging.warning(
                "Warehouse logistics: DCS item IDs changed since they were cached "
                "(DCS update?). The mission script corrected the stock by name; the "
                "cache has been refreshed for the next mission."
            )

        if not mission_ended:
            logging.warning(
                "Warehouse logistics: the mission did not end cleanly, so fuel and "
                "munition use from it was not applied."
            )
            self.last_result = summary
            return

        from .sam import apply_results as apply_sam_results

        for cp_name, sam_used in apply_sam_results(game, data).items():
            summary.munitions_used.setdefault(cp_name, {}).update(sam_used)

        by_id = {str(cp.id): cp for cp in game.theater.controlpoints}
        prefixes = metered_prefixes(game.settings)
        for cp_id, delta in _as_dict(data.get("bases")).items():
            cp = by_id.get(str(cp_id))
            if cp is None or cp.id not in self.stocks:
                continue
            stock = self.stocks[cp.id]
            delta = _as_dict(delta)
            fuel_delta = float(delta.get("fuel", 0) or 0)
            stock.jet_fuel_kg = max(0.0, stock.jet_fuel_kg + fuel_delta)
            if fuel_delta < 0:
                summary.fuel_used_kg[cp.name] = -fuel_delta
            used: dict[str, int] = {}
            for name, change in _as_dict(delta.get("mun")).items():
                name = str(name)
                change = int(change)
                if not name.startswith(prefixes) or not is_ammo(name):
                    continue
                stock.munitions[name] = max(0, stock.munitions.get(name, 0) + change)
                if change < 0:
                    used[name] = -change
            if used:
                summary.munitions_used.setdefault(cp.name, {}).update(used)
        self.last_result = summary
        for cp_name, used in summary.munitions_used.items():
            logging.info("Warehouse logistics: %s expended %s", cp_name, used)


def _as_dict(value: Any) -> dict[Any, Any]:
    # json.lua encodes empty tables as arrays.
    return value if isinstance(value, dict) else {}


def stock_summary(stock: BaseStock, limit: int = 12) -> list[tuple[str, int]]:
    """Largest munition stocks, by short display name."""
    items = sorted(
        ((short_name(k), v) for k, v in ammo_only(stock.munitions).items()),
        key=lambda kv: (-kv[1], kv[0]),
    )
    return items[:limit]


def short_name(resource: str) -> str:
    return resource.split(".", 2)[-1]
