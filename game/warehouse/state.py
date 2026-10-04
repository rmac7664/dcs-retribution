"""Campaign-persistent stock of fuel and munitions at each base."""

from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, TYPE_CHECKING
from uuid import UUID

from game.theater.controlpoint import Airfield, ControlPoint, NavalControlPoint
from .munitions import (
    MunitionCatalog,
    ammo_only,
    is_ammo,
    learn_from_observations,
    metered_prefixes,
)

if TYPE_CHECKING:
    from game import Game
    from game.dcs.aircrafttype import AircraftType
    from game.settings import Settings
    from .plan import MissionWarehousePlan

KG_PER_TON = 1000.0


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
        if isinstance(cp, Airfield):
            return True
        return isinstance(cp, NavalControlPoint) and (cp.is_carrier or cp.is_lha)

    def managed_points(self, game: Game) -> Iterator[ControlPoint]:
        for cp in game.theater.controlpoints:
            if self.is_managed(cp, game.settings):
                yield cp

    # Authorized levels -------------------------------------------------------------

    @staticmethod
    def fuel_capacity_kg(cp: ControlPoint, settings: Settings) -> float:
        if isinstance(cp, NavalControlPoint):
            return settings.logistics_ship_fuel_tons * KG_PER_TON
        return settings.logistics_airfield_fuel_tons * KG_PER_TON

    def authorized_munitions(self, game: Game, cp: ControlPoint) -> dict[str, int]:
        """Munitions a base is stocked for: N sorties of every load its squadrons use.

        For each aircraft type, the per-aircraft need for a munition is the most any of
        that type's loadouts carries (after date restrictions), so every standard and
        user-defined loadout can be flown from a full base.
        """
        prefixes = metered_prefixes(game.settings)
        sorties = game.settings.logistics_munitions_sorties
        per_type: dict[AircraftType, int] = Counter()
        for squadron in cp.squadrons:
            per_type[squadron.aircraft] += squadron.owned_aircraft
        authorized: Counter[str] = Counter()
        for aircraft, count in per_type.items():
            for name, per_aircraft in self._max_loads(game, cp, aircraft).items():
                if name.startswith(prefixes):
                    authorized[name] += per_aircraft * count * sorties
        return dict(authorized)

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
        stock = self.stocks.get(cp.id)
        if stock is None:
            stock = BaseStock(
                jet_fuel_kg=self.fuel_capacity_kg(cp, game.settings),
                munitions=self.authorized_munitions(game, cp),
            )
            self.stocks[cp.id] = stock
        return stock

    def resupply(self, game: Game) -> None:
        """Turn-end resupply toward each base's capacity and authorized levels."""
        settings = game.settings
        fuel_rate = settings.logistics_fuel_resupply_percent / 100
        munitions_rate = settings.logistics_munitions_resupply_percent / 100
        for cp in self.managed_points(game):
            if cp.id not in self.stocks:
                self.ensure_stock(game, cp)
                continue
            stock = self.stocks[cp.id]
            capacity = self.fuel_capacity_kg(cp, settings)
            if stock.jet_fuel_kg < capacity:
                stock.jet_fuel_kg = min(
                    capacity, stock.jet_fuel_kg + capacity * fuel_rate
                )
            for name, authorized in self.authorized_munitions(game, cp).items():
                have = stock.munitions.get(name, 0)
                if have < authorized:
                    delivery = max(1, math.ceil(authorized * munitions_rate))
                    if munitions_rate > 0:
                        stock.munitions[name] = min(authorized, have + delivery)

    # Mission results ----------------------------------------------------------------

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
                summary.munitions_used[cp.name] = used
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
