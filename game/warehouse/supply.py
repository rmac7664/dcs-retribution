"""Supply lines: munitions (and optionally fuel) bought at depots and shipped forward.

Each turn, for each side:

1. Production. New munitions are bought at depots: bases with a live ammo depot,
   factory, fuel depot or warehouse; off-map spawns (the rear area); carriers and LHAs
   (replenished at sea). Purchases are capped per turn and, if munition costs are
   enabled, paid from the side's budget, most-needed items first.
2. Distribution. Every other base is served by its nearest depot. Whatever a base is
   short of, and its depot has spare, is loaded into a supply order: a regular
   Retribution transfer order whose "units" are cargo trucks (or, in the air, pallets)
   carrying a SupplyLoad. From there the existing transfer machinery takes over: road
   links become convoys, sea links cargo ships, anything else an airlift by the side's
   transport squadrons (C-130, C-17, Il-76, Mi-8...). Destroyed trucks or shot-down
   transports lose their share of the cargo; an airlifter that doesn't land at the
   destination brings its share back.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Iterable, Optional, TYPE_CHECKING

import yaml

from game.theater.controlpoint import (
    Airfield,
    ControlPoint,
    Fob,
    NavalControlPoint,
    OffMapSpawn,
)
from .munitions import MunitionCatalog, metered_prefixes

if TYPE_CHECKING:
    from game import Game
    from game.coalition import Coalition
    from game.dcs.aircrafttype import AircraftType
    from game.dcs.groundunittype import GroundUnitType
    from game.settings import Settings
    from game.transfers import TransferOrder
    from .state import WarehouseState

from .state import SupplyShip

PRICES = Path("resources/warehouse/munition_prices.yaml")
DEPOT_CATEGORIES = {"ammo", "factory", "fuel", "ware"}

#: Approximate payload in tons by DCS aircraft type. Anything not listed falls back to
#: a generic helicopter or plane figure.
CARGO_TONS: dict[str, float] = {
    "C-17A": 77,
    "C5_Galaxy": 120,
    "IL-76MD": 47,
    "A400M_Atlas": 37,
    "C-130": 19,
    "C-130J-30": 19,
    "Hercules": 19,
    "An-26B": 5.5,
    "An-30M": 5,
    "C-47": 3,
    "C2A_Greyhound": 4.5,
    "V22_Osprey": 9,
    "Yak-40": 2.7,
    "vwv_l-1049": 8,
    "Mi-26": 20,
    "CH-53E": 13,
    "CH-47D": 10,
    "CH-47Fbl1": 10,
    "vwv_ch46d": 3,
    "vwv_ch46d_late": 3,
    "Mi-8MT": 4,
    "UH-60A": 4,
    "UH-60L": 4,
    "UH-60L_DAP": 2,
    "SH-60B": 2,
    "HKP15B": 1.5,
    "UH-1H": 1.5,
}

#: Typical mass (kg) of one munition when DCS data doesn't say.
DEFAULT_MASS_KG = {
    "weapons.missiles.": 250.0,
    "weapons.torpedoes.": 400.0,
    "weapons.bombs.": 300.0,
    "weapons.nurs.": 10.0,
}

#: Preferred cargo vehicles, in order, when a faction lists several logistics units.
TRUCK_PREFERENCE = ("M818", "Ural-4320", "Ural-375", "KAMAZ", "MAN", "Opel", "GAZ")


def cargo_tons(aircraft: AircraftType) -> float:
    known = CARGO_TONS.get(aircraft.dcs_unit_type.id)
    if known is not None:
        return known
    return 3.0 if aircraft.dcs_unit_type.helicopter else 15.0


@dataclass
class SupplyLoad:
    """Cargo carried by a supply order, spread evenly over its carriers."""

    munitions: dict[str, int] = field(default_factory=dict)
    fuel_kg: float = 0.0
    #: Weight of the whole load when it was dispatched.
    tons: float = 0.0
    #: Trucks/pallets the load was spread over when it was dispatched.
    carriers: int = 1

    @property
    def tons_per_carrier(self) -> float:
        return self.tons / max(1, self.carriers)

    def scaled(self, fraction: float) -> SupplyLoad:
        fraction = max(0.0, min(1.0, fraction))
        return SupplyLoad(
            munitions={
                k: int(math.floor(v * fraction + 1e-9))
                for k, v in self.munitions.items()
                if int(math.floor(v * fraction + 1e-9))
            },
            fuel_kg=self.fuel_kg * fraction,
            tons=self.tons * fraction,
            carriers=max(1, round(self.carriers * fraction)),
        )

    def split_off(self, carriers: int) -> SupplyLoad:
        """Moves `carriers` worth of cargo into a new load (for splitting airlifts)."""
        fraction = carriers / max(1, self.carriers)
        part = self.scaled(fraction)
        part.carriers = carriers
        for k, v in part.munitions.items():
            self.munitions[k] -= v
            if not self.munitions[k]:
                del self.munitions[k]
        self.fuel_kg -= part.fuel_kg
        self.tons -= part.tons
        self.carriers = max(1, self.carriers - carriers)
        return part

    def describe(self) -> str:
        parts = []
        if self.munitions:
            total = sum(self.munitions.values())
            parts.append(f"{total} munitions")
        if self.fuel_kg >= 1:
            parts.append(f"{self.fuel_kg / 1000:.0f} t fuel")
        return f"{' and '.join(parts) or 'supplies'} ({self.tons:.1f} t)"


class MunitionPrices:
    """Unit prices for warehouse items, from resources/warehouse/munition_prices.yaml."""

    _rules: ClassVar[Optional[list[tuple[re.Pattern[str], float]]]] = None
    _defaults: ClassVar[dict[str, float]] = {}

    @classmethod
    def _load(cls) -> list[tuple[re.Pattern[str], float]]:
        if cls._rules is None:
            try:
                with PRICES.open(encoding="utf-8") as prices_file:
                    data = yaml.safe_load(prices_file) or {}
            except OSError as ex:
                logging.error(f"Could not load {PRICES}: {ex}")
                data = {}
            cls._rules = [
                (re.compile(rule["pattern"], re.IGNORECASE), float(rule["price"]))
                for rule in data.get("rules", [])
            ]
            cls._defaults = {
                str(k): float(v) for k, v in data.get("defaults", {}).items()
            }
        return cls._rules

    @classmethod
    def price(cls, resource: str, settings: Settings) -> float:
        from .sam import is_sam, sam_price

        if is_sam(resource):
            return sam_price(resource) * settings.logistics_munition_price_percent / 100
        rules = cls._load()
        short = resource.split(".", 2)[-1]
        base = None
        for pattern, price in rules:
            if pattern.search(short):
                base = price
                break
        if base is None:
            base = next(
                (v for k, v in cls._defaults.items() if resource.startswith(k)), 0.1
            )
        return base * settings.logistics_munition_price_percent / 100


class MunitionMasses:
    """Mass of one munition, from single-munition launchers in pydcs' weapon data."""

    _masses: ClassVar[Optional[dict[str, float]]] = None

    @classmethod
    def mass_kg(cls, resource: str) -> float:
        from .sam import is_sam, sam_mass_kg

        if is_sam(resource):
            return sam_mass_kg(resource)
        if cls._masses is None:
            from dcs.weapons_data import weapon_ids

            masses: dict[str, float] = {}
            for clsid, munitions in MunitionCatalog.datamine().items():
                if len(munitions) != 1:
                    continue
                name, count = next(iter(munitions.items()))
                data: Any = weapon_ids.get(clsid)
                weight = data.get("weight") if data else None
                if not weight or count != 1:
                    continue
                masses[name] = min(masses.get(name, float(weight)), float(weight))
            cls._masses = masses
        known = cls._masses.get(resource)
        if known:
            return known
        return next(
            (v for k, v in DEFAULT_MASS_KG.items() if resource.startswith(k)), 250.0
        )

    @classmethod
    def tons(cls, munitions: dict[str, int], fuel_kg: float = 0.0) -> float:
        kg = sum(cls.mass_kg(k) * v for k, v in munitions.items()) + fuel_kg
        return kg / 1000


def supply_of(transfer: TransferOrder) -> Optional[SupplyLoad]:
    # Transfer orders saved before supply lines existed don't have the attribute.
    return getattr(transfer, "supplies", None)


def carriers_per_aircraft(transfer: TransferOrder, aircraft: AircraftType) -> int:
    """Trucks or pallets of a transfer one aircraft can lift.

    Supply loads are sized by weight: only whole pallets that fit count. Call
    `fit_pallets` first so a pallet is never heavier than the aircraft can carry.
    """
    load = supply_of(transfer)
    if load is None:
        return 1 if aircraft.dcs_unit_type.helicopter else 2
    return max(1, int(cargo_tons(aircraft) / max(0.1, load.tons_per_carrier) + 1e-6))


def uses_ctld_crates(settings: Settings, flight: Any) -> bool:
    """True if this supply airlift's cargo is carried as CTLD crates by a player."""
    return (
        settings.logistics_player_supply_cargo == "ctld"
        and bool(settings.plugins.get(settings.plugin_settings_key("ctld"), False))
        and flight.client_count > 0
        and flight.is_helo
    )


def fit_pallets(transfer: TransferOrder, aircraft: AircraftType) -> None:
    """Repacks a supply load into pallets no heavier than `aircraft` can lift.

    Supply runs are loaded into ~10 t trucks. A helicopter that lifts 4 t can't take
    a whole truckload, so the remaining cargo is split into smaller pallets.
    """
    load = supply_of(transfer)
    if load is None or len(transfer.units) != 1 or transfer.size <= 0:
        return
    capacity = cargo_tons(aircraft)
    if capacity <= 0 or load.tons_per_carrier <= capacity + 1e-6:
        return
    (unit_type,) = transfer.units
    # Trucks lost on earlier legs took their share of the cargo with them.
    remaining = load.scaled(transfer.size / max(1, load.carriers))
    pallets = max(1, math.ceil(remaining.tons / capacity - 1e-9))
    remaining.carriers = pallets
    transfer.supplies = remaining
    transfer.units[unit_type] = pallets


def load_into_trucks(transfer: TransferOrder, settings: Settings) -> None:
    """Repacks a supply load into full trucks before it goes by road.

    Undoes `fit_pallets` when cargo flown in small pallets continues by convoy, so
    the convoy isn't a long line of half-empty trucks.
    """
    load = supply_of(transfer)
    if load is None or len(transfer.units) != 1 or transfer.size <= 0:
        return
    remaining = load.scaled(transfer.size / max(1, load.carriers))
    trucks = min(
        settings.logistics_max_trucks_per_shipment,
        max(1, math.ceil(remaining.tons / max(0.1, settings.logistics_truck_tons))),
    )
    if trucks >= transfer.size:
        return
    (unit_type,) = transfer.units
    remaining.carriers = trucks
    transfer.supplies = remaining
    transfer.units[unit_type] = trucks


def deliver(transfer: TransferOrder, location: ControlPoint) -> None:
    """A supply order reached `location`: unload what's left of its cargo."""
    load = supply_of(transfer)
    if load is None:
        return
    remaining = transfer.size / max(1, load.carriers)
    if remaining <= 0:
        return
    game = location.coalition.game
    state: WarehouseState = game.warehouse_logistics
    cargo = load.scaled(remaining)
    state.receive(game, location, cargo.munitions, cargo.fuel_kg)
    lost = 1 - remaining
    from .report import record_delivery

    side = getattr(getattr(transfer, "player", None), "name", None)
    record_delivery(
        game,
        str(side or getattr(location.captured, "name", "")),
        location.name,
        cargo.tons,
        load.tons * max(0.0, lost),
    )
    logging.info(
        "Supply: %s delivered to %s%s",
        cargo.describe(),
        location.name,
        f" ({lost:.0%} lost en route)" if lost > 0.01 else "",
    )


def return_to(transfer: TransferOrder, location: ControlPoint, carriers: int) -> None:
    """`carriers` worth of a supply order's cargo goes back into `location`."""
    load = supply_of(transfer)
    if load is None or carriers <= 0:
        return
    game = location.coalition.game
    # Taken out of the load (not just scaled), so what is returned plus what is later
    # delivered adds up to the whole load: no munitions vanish to rounding.
    cargo = load.split_off(min(carriers, load.carriers))
    game.warehouse_logistics.receive(game, location, cargo.munitions, cargo.fuel_kg)


@dataclass
class PurchaseReport:
    spent: float = 0.0
    bought: Counter[str] = field(default_factory=Counter)
    unaffordable: Counter[str] = field(default_factory=Counter)
    shipments: int = 0
    #: Munitions the player ordered last turn that arrived at their depots.
    delivered: Counter[str] = field(default_factory=Counter)
    #: Money returned for orders at depots lost before they arrived.
    refunded: float = 0.0
    #: Munition types still short that the player could order (manual purchasing).
    still_short: int = 0
    #: Replenishment ships sent to carriers, unloaded, and lost with their carrier.
    ships_sent: int = 0
    #: Urgent top-ups flown out to carriers (onboard delivery).
    cod_runs: int = 0
    #: Supply runs called off because nothing could carry them.
    cancelled_runs: list[str] = field(default_factory=list)
    #: Bases that got less (or nothing) because of their cargo handling limit.
    throughput_limited: list[str] = field(default_factory=list)
    ships_arrived: int = 0
    ships_lost: list[str] = field(default_factory=list)
    #: Carriers no ship can reach, supplied by air this turn.
    air_only_carriers: list[str] = field(default_factory=list)


@dataclass
class MunitionOrder:
    """Munitions the player ordered at one depot this turn, already paid for."""

    count: int = 0
    paid: float = 0.0


@dataclass(frozen=True)
class Suggestion:
    """One line of the computer's shopping list."""

    urgency: float
    depot: ControlPoint
    resource: str
    quantity: int


def manual_purchasing(game: Game, coalition: Coalition) -> bool:
    """True if the player picks this side's munition purchases."""
    return (
        coalition.player.is_blue and game.settings.logistics_manual_munition_purchases
    )


def ordered_at(state: WarehouseState, depot: ControlPoint) -> dict[str, int]:
    return {
        name: order.count
        for name, order in state.orders.get(depot.id, {}).items()
        if order.count > 0
    }


def set_order(game: Game, depot: ControlPoint, resource: str, count: int) -> float:
    """Sets the player's order for a munition at a depot, paying or refunding the
    difference. Returns the change in money spent (negative for a refund).

    Raises ValueError if the side can't afford the increase.
    """
    state = game.warehouse_logistics
    coalition = depot.coalition
    orders = state.orders.setdefault(depot.id, {})
    order = orders.get(resource, MunitionOrder())
    count = max(0, count)
    if count == order.count:
        return 0.0
    if count > order.count:
        price = (
            MunitionPrices.price(resource, game.settings)
            if game.settings.logistics_munitions_cost
            else 0.0
        )
        cost = (count - order.count) * price
        if cost > coalition.budget + 1e-9:
            raise ValueError(
                f"Not enough money: {short(resource)} ×{count - order.count} costs "
                f"${cost:,.2f}M, you have ${coalition.budget:,.2f}M."
            )
        coalition.adjust_budget(-cost)
        order = MunitionOrder(count, order.paid + cost)
        change = cost
    else:
        # Refund at what was paid, so a price change mid-turn can't make money.
        refund = order.paid * (order.count - count) / order.count
        coalition.adjust_budget(refund)
        order = MunitionOrder(count, order.paid - refund)
        change = -refund
    if order.count:
        orders[resource] = order
    else:
        orders.pop(resource, None)
    if not orders:
        state.orders.pop(depot.id, None)
    return change


def short(resource: str) -> str:
    return resource.split(".", 2)[-1]


def stock_value(state: WarehouseState, cp: ControlPoint, settings: Settings) -> float:
    """What a base's stock is worth, in $M (fuel at a nominal $1M per 1,000 t)."""
    stock = state.stocks.get(cp.id)
    if stock is None:
        return 0.0
    value = sum(
        MunitionPrices.price(name, settings) * count
        for name, count in stock.munitions.items()
        if count > 0
    )
    return value + stock.jet_fuel_kg / 1_000_000


#: A main base this close to an enemy airfield gets a warning (not a block).
MAIN_BASE_WARNING_NM = 50
METERS_PER_NM = 1852.0


def _base_tier(cp: ControlPoint) -> Optional[int]:
    """What kind of main base `cp` could be: 0 airfield, 1 carrier/LHA, 2 FOB.

    None if it can't be one at all (off-map spawns, a sunk ship, a closed runway).
    """
    if isinstance(cp, OffMapSpawn):
        return None
    if isinstance(cp, Airfield):
        return 0 if cp.runway_is_operational() else None
    if isinstance(cp, NavalControlPoint):
        return 1 if cp.runway_is_operational() else None
    if isinstance(cp, Fob):
        return 2
    return None


def enemy_positions(game: Game, player: Any) -> list[Any]:
    return [
        cp.position
        for cp in game.theater.controlpoints
        if cp.captured == player.opponent and not isinstance(cp, OffMapSpawn)
    ]


def distance_to_enemy(cp: ControlPoint, enemy: list[Any]) -> float:
    return min((cp.position.distance_to_point(e) for e in enemy), default=0.0)


def _depot_buildings(cp: ControlPoint) -> list[Any]:
    """The base's ammo depots, fuel depots, factories and warehouses, dead or alive."""
    return [
        tgo
        for tgo in getattr(cp, "connected_objectives", [])
        if getattr(tgo, "category", None) in DEPOT_CATEGORIES
    ]


def _has_live_depot(cp: ControlPoint) -> bool:
    """True if `cp` can be a depot main base: a carrier/LHA (supplied by ship) or a
    base with a depot building still standing."""
    if isinstance(cp, NavalControlPoint):
        return True
    return any(not tgo.is_dead for tgo in _depot_buildings(cp))


def _safer_half(
    game: Game, player: Any, pool: list[ControlPoint]
) -> list[ControlPoint]:
    best = min(_base_tier(cp) for cp in pool)  # type: ignore[type-var]
    pool = [cp for cp in pool if _base_tier(cp) == best]
    enemy = enemy_positions(game, player)
    pool.sort(key=lambda cp: (-distance_to_enemy(cp, enemy), cp.name))
    return pool[: math.ceil(len(pool) / 2)]


def _depot_options(game: Game, player: Any) -> list[ControlPoint]:
    """Main base options that are supply depots; empty if the side has none."""
    pool = [cp for cp in _main_base_pool(game, player) if _has_live_depot(cp)]
    return _safer_half(game, player, pool) if pool else []


def _main_base_pool(game: Game, player: Any) -> list[ControlPoint]:
    points = list(game.theater.controlpoints)
    friendly = [cp for cp in points if cp.captured == player]
    held = [
        cp for cp in friendly if getattr(cp, "starting_coalition", player) == player
    ]
    # A side that has lost every base it started with can still have a main base.
    return [cp for cp in held if _base_tier(cp) is not None] or [
        cp for cp in friendly if _base_tier(cp) is not None
    ]


def main_base_options(game: Game, player: Any) -> list[ControlPoint]:
    """The bases the side may pick as its main supply base, safest first.

    Rules: friendly and held since the campaign started, not off the map, and a supply
    depot (a depot building still standing, or a carrier/LHA). Among those: an
    airfield with a working runway, or if none its carrier/LHA, or if none a FOB; then
    the rear half, ranked by distance to the nearest enemy base.

    A side with no depot at all falls back to the same rules without the depot
    requirement. The first entry is the automatic choice: the base farthest from the
    enemy.
    """
    depots = _depot_options(game, player)
    if depots:
        return depots
    pool = _main_base_pool(game, player)
    return _safer_half(game, player, pool) if pool else []


def main_base_warnings(game: Game, cp: ControlPoint) -> list[str]:
    """Reasons `cp` is a risky main base. These warn; they don't block the pick."""
    warnings = []
    if cp.has_active_frontline:
        warnings.append("it is on a front line")
    limit = MAIN_BASE_WARNING_NM * METERS_PER_NM
    near = [
        other
        for other in game.theater.controlpoints
        if isinstance(other, Airfield)
        and other.captured == cp.captured.opponent
        and other.position.distance_to_point(cp.position) <= limit
    ]
    if near:
        closest = min(near, key=lambda o: o.position.distance_to_point(cp.position))
        miles = closest.position.distance_to_point(cp.position) / METERS_PER_NM
        warnings.append(f"it is {miles:.0f} nm from the enemy airfield {closest.name}")
    return warnings


def _lost_reason(game: Game, cp: ControlPoint, player: Any) -> Optional[str]:
    """Why a locked main base is no longer the main base, or None if it still is.

    Lost when captured, when the ship is sunk, or when its last depot building is
    destroyed (if the side has another depot to move to). A damaged runway doesn't
    count; it gets repaired.
    """
    if cp.captured != player or isinstance(cp, OffMapSpawn):
        return "captured"
    if isinstance(cp, NavalControlPoint):
        return None if cp.runway_is_operational() else "sunk"
    buildings = _depot_buildings(cp)
    if buildings and all(tgo.is_dead for tgo in buildings):
        if _depot_options(game, player):
            return "depots destroyed"
    return None


def _still_main_base(game: Game, cp: ControlPoint, player: Any) -> bool:
    return _lost_reason(game, cp, player) is None


def _stored_main_base(game: Game, player: Any) -> Optional[ControlPoint]:
    state = getattr(game, "warehouse_logistics", None)
    chosen = (getattr(state, "main_bases", None) or {}).get(player.name)
    if chosen is None:
        return None
    for cp in game.theater.controlpoints:
        if cp.id == chosen:
            return cp
    return None


def main_base(game: Game, player: Any) -> Optional[ControlPoint]:
    """The side's main supply base: where its supplies originate.

    The player picks it before the campaign begins (see main_base_options); the AI's
    is picked automatically. Once picked it stays until lost, then a new one is picked
    (update_main_bases). Stock from an off-map rear area arrives here by strategic
    airlift, it always buys munitions like a depot, and replenishment ships sail from
    its direction.
    """
    stored = _stored_main_base(game, player)
    if stored is not None and _still_main_base(game, stored, player):
        return stored
    options = main_base_options(game, player)
    return options[0] if options else None


def main_base_is_locked(game: Game, player: Any) -> bool:
    """True once the side's main base has been picked for good."""
    stored = _stored_main_base(game, player)
    return stored is not None and _still_main_base(game, stored, player)


def set_main_base(game: Game, player: Any, cp: ControlPoint) -> None:
    """Picks `cp` as the side's main supply base. It must be one of the options."""
    if cp not in main_base_options(game, player):
        raise ValueError(f"{cp.name} can't be a main supply base")
    game.warehouse_logistics.main_bases[player.name] = cp.id


def update_main_bases(game: Game) -> list[str]:
    """Locks in each side's main base, picking a new one if it was lost.

    Returns messages about the player's side for the turn report.
    """
    messages = []
    bases = game.warehouse_logistics.main_bases
    for coalition in (game.blue, game.red):
        player = coalition.player
        stored = _stored_main_base(game, player)
        reason = None if stored is None else _lost_reason(game, stored, player)
        if stored is not None and reason is None:
            continue
        options = main_base_options(game, player)
        if not options:
            bases.pop(player.name, None)
            continue
        bases[player.name] = options[0].id
        if stored is None:
            continue
        new = options[0].name
        logging.info(
            "Supply: %s main base %s %s; now %s", player.name, stored.name, reason, new
        )
        if player.is_blue:
            if reason == "depots destroyed":
                messages.append(
                    f"Every supply depot building at {stored.name} was destroyed, so "
                    f"{new} is now our main supply base."
                )
            else:
                messages.append(
                    f"{stored.name} was {reason}, so {new} is now our main supply "
                    "base."
                )
        elif reason == "captured" and stored.captured == game.blue.player:
            messages.append(f"We captured the enemy's main supply base, {stored.name}.")
        elif reason == "depots destroyed" and (
            stored.id in game.warehouse_logistics.revealed_main_bases
        ):
            messages.append(
                f"We destroyed the enemy's main supply base at {stored.name}; "
                "they have moved it elsewhere."
            )
    return messages


#: A player flight with a waypoint this close to the enemy main base scouts it.
SCOUT_RANGE_NM = 10


def reveal_enemy_main_base(game: Game) -> bool:
    """Marks the enemy's main base as found if this turn's flights struck or scouted it.

    Struck: a package targeted the base or something at it. Scouted: a flight's route
    passed within SCOUT_RANGE_NM of it. Called at the end of a turn that was flown.
    """
    enemy_base = main_base(game, game.red.player)
    state = game.warehouse_logistics
    if enemy_base is None or enemy_base.id in state.revealed_main_bases:
        return False
    limit = SCOUT_RANGE_NM * METERS_PER_NM
    for package in game.blue.ato.packages:
        target = package.target
        if target is enemy_base or getattr(target, "control_point", None) is enemy_base:
            found = True
        else:
            found = any(
                point.position.distance_to_point(enemy_base.position) <= limit
                for flight in package.flights
                for point in flight.points
            )
        if found:
            state.revealed_main_bases.add(enemy_base.id)
            game.message(
                "Logistics: enemy main supply base found",
                f"{enemy_base.name} is the enemy's main supply base.",
            )
            return True
    return False


def main_base_known(game: Game, cp: ControlPoint) -> bool:
    """True if `cp` is a main base the player should see marked on the map."""
    if main_base(game, cp.captured) is not cp:
        return False
    if cp.captured.is_blue:
        return True
    return cp.id in game.warehouse_logistics.revealed_main_bases


def carrier_reachable_by_sea(game: Any, cp: ControlPoint) -> bool:
    """True unless `cp` is a carrier/LHA no replenishment ship can sail to.

    Ships need open water to approach from (see replenishmentshipgenerator); a
    carrier or LHA close in to a coast may have none, and is then resupplied by air
    only.
    """
    if not isinstance(cp, NavalControlPoint):
        return True
    theater = getattr(game, "theater", None)
    if theater is None or not hasattr(theater, "is_in_sea"):
        return True
    from game.missiongenerator.replenishmentshipgenerator import ship_can_reach

    try:
        return ship_can_reach(game, cp)
    except Exception:
        logging.exception("Could not check sea access to %s", cp.name)
        return True


def airhead(game: Game, player: Any) -> Optional[ControlPoint]:
    """Where the side's off-map rear area delivers: its main supply base.

    Stock from the rear area arrives here by strategic airlift (it isn't flown in the
    mission and can't be intercepted) and goes forward by convoy or airlift. None if
    the side has no off-map rear area.
    """
    points = list(game.theater.controlpoints)
    if not any(isinstance(cp, OffMapSpawn) and cp.captured == player for cp in points):
        return None
    return main_base(game, player)


#: Ship-launched strike missiles: bought after a ship's interceptors.
STRIKE_SAM_PREFIXES = (
    "sam.missiles.BGM_109",
    "sam.missiles.AGM_84",
    "sam.missiles.RGM_84",
    "sam.missiles.TOMAHAWK",
)

#: Turns a supply run may wait with no transport able to carry it before it is
#: called off and its cargo unloaded where it waits.
STALL_LIMIT = 2


def cancel_stalled_runs(coalition: Coalition) -> list[str]:
    """Calls off supply runs nothing could carry for STALL_LIMIT turns.

    E.g. an airlift-only leg with no transport aircraft in range. The cargo goes
    back into stock where it waits, so it isn't locked away and doesn't count
    against the destination's cargo handling forever; the planner sends it again
    when it can.
    """
    transfers = coalition.transfers.pending_transfers
    cancelled = []
    for transfer in list(transfers):
        if supply_of(transfer) is None:
            continue
        if getattr(transfer, "stalled_turns", 0) < STALL_LIMIT:
            continue
        deliver(transfer, transfer.position)
        transfer.units.clear()
        transfers.remove(transfer)
        cancelled.append(f"{transfer.origin.name} to {transfer.destination.name}")
        logging.info(
            "Supply: run from %s to %s called off after %d turns without transport",
            transfer.origin.name,
            transfer.destination.name,
            transfer.stalled_turns,
        )
    return cancelled


#: A carrier holding less than this share of an item it should have is urgently
#: short of it, and a land depot will fly some of its own stock out (see
#: SupplyPlanner.airlift_to_carriers).
URGENT_SHARE = 0.5
#: The least share of its own needs a land depot keeps when it does.
DEPOT_RESERVE_SHARE = 0.5


class SupplyPlanner:
    """Turn-end production and distribution for one side."""

    def __init__(self, game: Game, coalition: Coalition) -> None:
        self.game = game
        self.coalition = coalition
        self.settings = game.settings
        self.state: WarehouseState = game.warehouse_logistics
        self.prefixes = metered_prefixes(game.settings)
        self.bases = [
            cp
            for cp in self.state.managed_points(game)
            if cp.captured == coalition.player
        ]
        self.airhead = airhead(game, coalition.player)
        self._land_rear_area_runs()
        self.cancelled_runs = cancel_stalled_runs(coalition)
        #: Carriers/LHAs no replenishment ship can reach: supplied by air only.
        self.air_only: set[Any] = {
            cp.id
            for cp in self.bases
            if isinstance(cp, NavalControlPoint)
            and not carrier_reachable_by_sea(game, cp)
        }
        if game.settings.logistics_supply_lines:
            self.depots = [
                cp
                for cp in self.bases
                if self.is_depot(cp) and cp.id not in self.air_only
            ]
            # Forward supply points: FOBs and other bases with a live ammo/fuel
            # depot or warehouse hold stock for shipping even without a DCS warehouse.
            for cp in game.theater.control_points_for(coalition.player):
                if cp not in self.bases and self.is_depot(cp):
                    self.bases.append(cp)
                    self.depots.append(cp)
        else:
            # Without supply lines every base is restocked where it stands.
            self.depots = list(self.bases)
        if self.bases and not self.depots:
            logging.warning(
                "Supply: %s has no depots (ammo/fuel depot, factory, warehouse, "
                "off-map spawn or carrier). Every base is treated as a depot.",
                coalition.faction.name,
            )
            self.depots = list(self.bases)
        self.authorized = {
            cp.id: self.state.authorized_munitions(game, cp) for cp in self.bases
        }
        for cp in self.bases:
            self.state.ensure_stock(game, cp)
        self.inbound: dict[Any, Counter[str]] = {cp.id: Counter() for cp in self.bases}
        self.inbound_fuel: dict[Any, float] = {cp.id: 0.0 for cp in self.bases}
        #: Supply still on its way to each base, in tons (for cargo handling limits).
        self.inbound_tons: dict[Any, float] = {cp.id: 0.0 for cp in self.bases}
        for transfer in coalition.transfers.pending_transfers:
            load = supply_of(transfer)
            if load is None or transfer.destination.id not in self.inbound:
                continue
            share = load.scaled(transfer.size / max(1, load.carriers))
            self.inbound[transfer.destination.id].update(share.munitions)
            self.inbound_fuel[transfer.destination.id] += share.fuel_kg
            self.inbound_tons[transfer.destination.id] += share.tons
        # Ships already at sea count too, so a carrier is never ordered for twice.
        for cp in self.bases:
            for ship in self.state.ships_bound_for(cp):
                self.inbound[cp.id].update(ship.munitions)
                self.inbound_fuel[cp.id] += ship.fuel_kg
        #: What this turn's replenishment ships will carry, by carrier ID. Carriers
        #: buy like any depot, but what they buy arrives by ship a turn later.
        self.ship_cargo: dict[Any, SupplyShip] = {}
        self.served_by = self._assign_depots()

    def _stock_or_ship(
        self, cp: ControlPoint, munitions: dict[str, int], fuel_kg: float = 0.0
    ) -> None:
        """New stores for `cp`: straight into stock, or onto a carrier's ship."""
        if not isinstance(cp, NavalControlPoint) or cp.id in getattr(
            self, "air_only", set()
        ):
            self.state.receive(self.game, cp, munitions, fuel_kg)
            return
        ship = self.ship_cargo.get(cp.id)
        if ship is None:
            ship = SupplyShip(cp.id, cp.name, self.coalition.player.name)
            self.ship_cargo[cp.id] = ship
        for name, count in munitions.items():
            ship.munitions[name] = ship.munitions.get(name, 0) + count
        ship.fuel_kg += fuel_kg
        self.inbound[cp.id].update(munitions)
        self.inbound_fuel[cp.id] += fuel_kg

    def dispatch_ships(self, report: PurchaseReport) -> None:
        for ship in self.ship_cargo.values():
            if ship.is_empty():
                continue
            self.state.ship_serial += 1
            ship.name = f"{ship.carrier_name} replenishment {self.state.ship_serial}"
            self.state.supply_ships.append(ship)
            report.ships_sent += 1
            logging.info(
                "Supply: replenishment ship sails for %s (%d munitions, %.1f t fuel)",
                ship.carrier_name,
                sum(ship.munitions.values()),
                ship.fuel_kg / 1000,
            )
        self.ship_cargo.clear()

    @staticmethod
    def is_depot(cp: ControlPoint) -> bool:
        """Where new munitions are bought: the rear area, and carriers (by ship).

        A land base is a depot only if it has a live ammo depot, factory, fuel depot
        or warehouse, has been held since the campaign began, and isn't on a front
        line. Front-line and captured bases are always supplied by supply run.
        """
        if cp.captured.is_neutral:
            return False
        game = cp.coalition.game
        hub = main_base(game, cp.captured)
        settings = getattr(game, "settings", None)
        if getattr(settings, "logistics_main_base_only", False) and hub:
            # Only the main base buys; everything else is supplied from it.
            return hub is cp
        if isinstance(cp, (OffMapSpawn, NavalControlPoint)):
            return True
        if hub is cp:
            return True
        if cp.captured != cp.starting_coalition or cp.has_frontline:
            return False
        return any(
            tgo.category in DEPOT_CATEGORIES and not tgo.is_dead
            for tgo in cp.connected_objectives
        )

    def _land_rear_area_runs(self) -> None:
        """Supply runs waiting in the off-map rear area are flown to the airhead.

        Strategic airlift: they continue from the airhead by convoy or airlift (or
        are unloaded there if it's their destination), instead of a transport being
        sent to the edge of the map to collect them.
        """
        if self.airhead is None:
            return
        transfers = self.coalition.transfers.pending_transfers
        for transfer in list(transfers):
            if supply_of(transfer) is None or transfer.transport is not None:
                continue
            if not isinstance(transfer.position, OffMapSpawn):
                continue
            if transfer.destination is self.airhead:
                deliver(transfer, self.airhead)
                transfer.units.clear()
                transfers.remove(transfer)
            else:
                transfer.position = self.airhead
            logging.info(
                "Supply: rear-area supply run for %s landed at the airhead %s",
                transfer.destination.name,
                self.airhead.name,
            )

    def _assign_depots(self) -> dict[Any, ControlPoint]:
        network = self.coalition.transit_network
        served: dict[Any, ControlPoint] = {}
        hub = self.main_base_only_hub()
        for cp in self.bases:
            if cp in self.depots:
                served[cp.id] = cp
                continue
            if cp.id in getattr(self, "air_only", set()):
                # Flown out from the main base, or the nearest land depot.
                land = [
                    d
                    for d in self.depots
                    if not isinstance(d, (NavalControlPoint, OffMapSpawn))
                ]
                land.sort(
                    key=lambda d: (
                        d is not main_base(self.game, self.coalition.player),
                        d.position.distance_to_point(cp.position),
                    )
                )
                if land:
                    served[cp.id] = land[0]
                continue
            if hub is not None and isinstance(cp, NavalControlPoint):
                # Its replenishment ship is loaded at the main base.
                served[cp.id] = hub
                continue
            best: Optional[tuple[float, ControlPoint]] = None
            for depot in self.depots:
                if isinstance(depot, NavalControlPoint):
                    # Ship's stores replenish the ship; they don't go ashore.
                    continue
                if isinstance(depot, OffMapSpawn) and self.airhead is not None:
                    # The rear area supplies the theater through its airhead.
                    continue
                try:
                    if not network.has_path_between(depot, cp):
                        continue
                    _, cost = network.shortest_path_with_cost(depot, cp)
                except Exception:
                    continue
                if best is None or cost < best[0]:
                    best = (cost, depot)
            if best is not None:
                served[cp.id] = best[1]
            else:
                # Cut off (e.g. runway closed, no road): it waits until a route opens.
                logging.info("Supply: %s has no route from a depot", cp.name)
        return served

    def main_base_only_hub(self) -> Optional[ControlPoint]:
        """The main base, if it is the side's only source of supply."""
        settings = getattr(self, "settings", None)
        if not getattr(settings, "logistics_main_base_only", False):
            return None
        game = getattr(self, "game", None)
        if game is None:
            return None
        return main_base(game, self.coalition.player)

    def _shortfall(self, cp: ControlPoint) -> Counter[str]:
        stock = self.state.stocks[cp.id].munitions
        short: Counter[str] = Counter()
        for name, want in self.authorized[cp.id].items():
            gap = want - stock.get(name, 0) - self.inbound[cp.id][name]
            if gap > 0:
                short[name] = gap
        return short

    # Production ---------------------------------------------------------------------

    def suggestions(self) -> list[Suggestion]:
        """What the side should buy this turn, most urgent first.

        Each depot needs what it and the bases it serves are short of, less spare stock
        it already holds and anything the player has already ordered there. Each
        munition is capped at the per-turn resupply share of the side's authorized
        total, split between depots by need.
        """
        rate = self.settings.logistics_munitions_resupply_percent / 100
        if rate <= 0:
            return []
        from .priority import is_priority

        need: dict[Any, Counter[str]] = {d.id: Counter() for d in self.depots}
        #: Items a priority base served by the depot is short of: bought first.
        urgent: dict[Any, set[str]] = {d.id: set() for d in self.depots}
        for cp in self.bases:
            depot = self.served_by.get(cp.id)
            if depot is None:
                continue
            shortfall = self._shortfall(cp)
            need[depot.id].update(shortfall)
            if is_priority(getattr(self, "game", None), cp):
                urgent[depot.id].update(shortfall)
        for depot in self.depots:
            # Spare stock already at the depot covers part of what it serves.
            stock = self.state.stocks[depot.id].munitions
            own = self.authorized[depot.id]
            for name in list(need[depot.id]):
                spare = max(0, stock.get(name, 0) - own.get(name, 0))
                need[depot.id][name] = max(0, need[depot.id][name] - spare)
            for name, count in ordered_at(self.state, depot).items():
                if name in need[depot.id]:
                    need[depot.id][name] = max(0, need[depot.id][name] - count)

        total_authorized: Counter[str] = Counter()
        for auth in self.authorized.values():
            total_authorized.update(auth)
        cap = {
            name: max(1, math.ceil(count * rate))
            for name, count in total_authorized.items()
        }
        total_need: Counter[str] = Counter()
        for depot_need in need.values():
            total_need.update(depot_need)

        from .usage import is_dormant, usage_of

        coalition = getattr(self, "coalition", None)
        side = coalition.player.name if coalition is not None else ""
        game = getattr(self, "game", None)
        used = usage_of(game, side)
        sorties = max(1, int(getattr(self.settings, "logistics_munitions_sorties", 1)))
        # Most-needed first (relative to what the side is authorized to hold).
        orders: list[Suggestion] = []
        for depot in self.depots:
            for name, count in need[depot.id].items():
                if count <= 0:
                    continue
                if is_dormant(game, side, name):
                    # Not fired for a while: stock one sortie's worth, not N.
                    count = max(1, math.ceil(count / sorties))
                share = count / max(1, total_need[name])
                quantity = min(count, max(1, math.floor(cap.get(name, 1) * share)))
                urgency = count / max(1, total_authorized[name])
                if name.startswith(STRIKE_SAM_PREFIXES):
                    # Ships restock interceptors before land-attack and anti-ship
                    # missiles.
                    urgency /= 2
                if used.get(name, 0.0) >= 0.5:
                    # What the side has actually been firing comes first.
                    urgency += 0.5
                if name in urgent[depot.id]:
                    # Priority bases' shortfalls come before everything else.
                    urgency += 1.0
                orders.append(Suggestion(urgency, depot, name, quantity))
        orders.sort(key=lambda o: (-o.urgency, short(o.resource)))
        return orders

    def deliver_orders(self, report: PurchaseReport) -> None:
        """The player's orders from this turn arrive at their depots."""
        if not self.coalition.player.is_blue:
            return
        for depot_id, items in list(self.state.orders.items()):
            try:
                depot = self.game.theater.find_control_point_by_id(depot_id)
            except KeyError:
                depot = None
            if depot is None or depot.captured != self.coalition.player:
                refund = sum(order.paid for order in items.values())
                self.coalition.adjust_budget(refund)
                report.refunded += refund
                continue
            arrived = {n: o.count for n, o in items.items() if o.count > 0}
            self._stock_or_ship(depot, arrived)
            report.delivered.update(arrived)
        self.state.orders.clear()

    def produce(self, report: PurchaseReport) -> None:
        self.deliver_orders(report)
        if manual_purchasing(self.game, self.coalition):
            report.still_short = len({s.resource for s in self.suggestions()})
            return

        pay = self.settings.logistics_munitions_cost
        budget = self.coalition.budget * (
            self.settings.logistics_munitions_budget_percent / 100
        )
        suggestions = self.suggestions()
        # Two passes: first up to half of each item, most urgent first, so one
        # expensive item can't use up the whole budget; then the rest.
        remaining = {id(s): s.quantity for s in suggestions}
        for share in (0.5, 1.0):
            for suggestion in suggestions:
                depot, name = suggestion.depot, suggestion.resource
                want = math.ceil(suggestion.quantity * share) - (
                    suggestion.quantity - remaining[id(suggestion)]
                )
                if want <= 0:
                    continue
                if pay:
                    price = MunitionPrices.price(name, self.settings)
                    affordable = (
                        want if price <= 0 else int((budget - report.spent) // price)
                    )
                    bought = max(0, min(want, affordable))
                    report.spent += bought * price
                else:
                    bought = want
                remaining[id(suggestion)] -= bought
                if bought:
                    self._stock_or_ship(depot, {name: bought})
                    report.bought[name] += bought
        for suggestion in suggestions:
            if remaining[id(suggestion)] > 0:
                report.unaffordable[suggestion.resource] += remaining[id(suggestion)]
        if pay and report.spent:
            self.coalition.adjust_budget(-report.spent)

    def produce_fuel(self) -> None:
        """Fuel arrives at depots (or every base, if fuel isn't shipped)."""
        rate = self.settings.logistics_fuel_resupply_percent / 100
        by_supply_lines = (
            self.settings.logistics_supply_lines
            and self.settings.logistics_fuel_by_supply_lines
        )
        for cp in self.bases:
            stock = self.state.stocks[cp.id]
            capacity = self.state.fuel_capacity_kg(cp, self.settings)
            if isinstance(cp, NavalControlPoint):
                if by_supply_lines and cp not in self.depots:
                    continue  # fuelled from the main base's stock (ship or air)
                # Ships are refuelled by their replenishment ship, not at sea.
                gap = capacity - stock.jet_fuel_kg - self.inbound_fuel[cp.id]
                if gap >= 1:
                    self._stock_or_ship(cp, {}, min(gap, capacity * rate))
                continue
            if by_supply_lines:
                if cp not in self.depots:
                    continue
                # A depot holds its own capacity plus as much again to ship out.
                capacity *= 2
            if stock.jet_fuel_kg < capacity:
                stock.jet_fuel_kg = min(capacity, stock.jet_fuel_kg + capacity * rate)

    # Distribution ---------------------------------------------------------------------

    def throughput_tons(self, cp: ControlPoint) -> Optional[float]:
        """Most supply `cp` can take in per turn (None: no limit)."""
        limit = self.settings.logistics_base_throughput_tons
        if limit <= 0 or isinstance(cp, (NavalControlPoint, OffMapSpawn)):
            return None
        if self.main_base_only_hub() is cp:
            return None  # all the side's supply passes through it
        if isinstance(cp, Airfield):
            return float(limit)
        return limit / 3

    def handling_room_kg(self, cp: ControlPoint) -> Optional[float]:
        """Cargo `cp` can still accept this turn, after what's already inbound."""
        limit = self.throughput_tons(cp)
        if limit is None:
            return None
        inbound = getattr(self, "inbound_tons", {}).get(cp.id, 0.0)
        return max(0.0, (limit - inbound) * 1000)

    def distribute(self, report: PurchaseReport) -> None:
        from game.transfers import TransferOrder

        from .priority import is_priority

        truck = self.cargo_truck()
        fuel_by_supply_lines = self.settings.logistics_fuel_by_supply_lines
        # Priority bases are first in line for their depot's spare stock.
        game = getattr(self, "game", None)
        hub = self.main_base_only_hub()
        ordered = sorted(self.bases, key=lambda b: not is_priority(game, b))
        for cp in ordered:
            depot = self.served_by.get(cp.id)
            if depot is None or depot is cp:
                continue
            depot_stock = self.state.stocks[depot.id]
            depot_auth = self.authorized[depot.id]
            # As the only source, the main base shares: it keeps half of its own
            # needs and sends the rest where it's short.
            keep = DEPOT_RESERVE_SHARE if depot is hub else 1.0
            load: dict[str, int] = {}
            for name, gap in self._shortfall(cp).items():
                spare = depot_stock.munitions.get(name, 0) - math.ceil(
                    depot_auth.get(name, 0) * keep
                )
                take = min(gap, spare)
                if take > 0:
                    load[name] = take
            fuel = 0.0
            if fuel_by_supply_lines:
                capacity = self.state.fuel_capacity_kg(cp, self.settings)
                gap_kg = (
                    capacity
                    - self.state.stocks[cp.id].jet_fuel_kg
                    - self.inbound_fuel.get(cp.id, 0.0)
                )
                spare_kg = depot_stock.jet_fuel_kg - self.state.fuel_capacity_kg(
                    depot, self.settings
                )
                fuel = max(0.0, min(gap_kg, spare_kg))
            wanted = MunitionMasses.tons(*self._fit_to_shipment(cp, load, fuel))
            room = self.handling_room_kg(cp)
            load, fuel = self._fit_to_shipment(cp, load, fuel, room)
            tons = MunitionMasses.tons(load, fuel)
            limited = room is not None and tons < wanted - 0.5
            if limited:
                report.throughput_limited.append(cp.name)
                logging.info(
                    "Supply: %s is at its cargo handling limit (%.0f of %.0f t)",
                    cp.name,
                    tons,
                    wanted,
                )
            if not load and fuel < 1:
                continue
            if cp.id in getattr(self, "air_only", set()):
                if truck is None:
                    continue
                for name, count in load.items():
                    depot_stock.munitions[name] -= count
                depot_stock.jet_fuel_kg -= fuel
                transfer = TransferOrder(
                    depot,
                    cp,
                    {truck: 1},
                    request_airflift=True,
                    supplies=SupplyLoad(
                        munitions=load, fuel_kg=fuel, tons=tons, carriers=1
                    ),
                )
                self.coalition.transfers.pending_transfers.append(transfer)
                self.inbound[cp.id].update(load)
                self.inbound_fuel[cp.id] += fuel
                report.shipments += 1
                report.air_only_carriers.append(cp.name)
                logging.info(
                    "Supply: %s flown from %s to %s (no sea access)",
                    transfer.supplies.describe() if transfer.supplies else "",
                    depot.name,
                    cp.name,
                )
                continue
            if isinstance(cp, NavalControlPoint):
                # Main base only: the carrier's replenishment ship is loaded here.
                for name, count in load.items():
                    depot_stock.munitions[name] -= count
                depot_stock.jet_fuel_kg -= fuel
                self._stock_or_ship(cp, load, fuel)
                report.shipments += 1
                continue
            if tons < self.settings.logistics_min_shipment_tons and not any(
                k.startswith("weapons.missiles.") for k in load
            ):
                continue
            if truck is None:
                logging.warning(
                    "Supply: %s has no logistics vehicles to carry cargo",
                    self.coalition.faction.name,
                )
                return
            for name, count in load.items():
                depot_stock.munitions[name] -= count
            depot_stock.jet_fuel_kg -= fuel
            # Fuel goes in its own convoy of tanker trucks (a separate target);
            # munitions in cargo trucks.
            tanker = self.fuel_truck() if load and fuel >= 1 else None
            if load and fuel >= 1 and tanker is not None:
                parts = [(load, 0.0, truck), ({}, fuel, tanker)]
            elif not load and fuel >= 1:
                parts = [({}, fuel, self.fuel_truck() or truck)]
            else:
                parts = [(load, fuel, truck)]
            for munitions, fuel_kg, vehicle in parts:
                part_tons = MunitionMasses.tons(munitions, fuel_kg)
                carriers = min(
                    self.settings.logistics_max_trucks_per_shipment,
                    max(1, math.ceil(part_tons / self.settings.logistics_truck_tons)),
                )
                transfer = TransferOrder(
                    depot,
                    cp,
                    {vehicle: carriers},
                    # Fuel isn't flown: hundreds of tonnes don't fit in transports.
                    request_airflift=(
                        self.settings.logistics_prefer_airlift and bool(munitions)
                    ),
                    supplies=SupplyLoad(
                        munitions=munitions,
                        fuel_kg=fuel_kg,
                        tons=part_tons,
                        carriers=carriers,
                    ),
                )
                self.coalition.transfers.pending_transfers.append(transfer)
                report.shipments += 1
                logging.info(
                    "Supply: %s from %s to %s",
                    transfer.supplies.describe() if transfer.supplies else "",
                    depot.name,
                    cp.name,
                )
            self.inbound[cp.id].update(load)
            self.inbound_fuel[cp.id] += fuel
            if hasattr(self, "inbound_tons"):
                self.inbound_tons[cp.id] = self.inbound_tons.get(cp.id, 0.0) + tons

    def fuel_truck(self) -> Optional[GroundUnitType]:
        """The side's fuel tanker truck: HEMTT for the west, ATZ-10 otherwise."""
        from game.dcs.groundunittype import GroundUnitType

        units = getattr(getattr(self.coalition, "faction", None), "logistics_units", ())
        for unit in units:
            if (
                "refueler" in unit.variant_id.lower()
                or "atz" in unit.variant_id.lower()
            ):
                return unit
        name = (
            "Refueler M978 HEMTT"
            if self.coalition.player.is_blue
            else "Refueler ATZ-10"
        )
        try:
            return GroundUnitType.named(name)
        except KeyError:
            return None

    def default_shipment_kg(self) -> float:
        """Heaviest single supply run: a full convoy."""
        return (
            self.settings.logistics_max_trucks_per_shipment
            * self.settings.logistics_truck_tons
            * 1000
        )

    def _fit_to_shipment(
        self,
        cp: ControlPoint,
        load: dict[str, int],
        fuel: float,
        limit_kg: Optional[float] = None,
    ) -> tuple[dict[str, int], float]:
        """Trims a load to one supply run's capacity; the rest waits a turn.

        Items the base is shortest of (relative to its authorized level) go first, then
        fuel fills whatever weight is left.
        """
        if limit_kg is None:
            limit_kg = self.default_shipment_kg()
        else:
            limit_kg = min(limit_kg, self.default_shipment_kg())
        authorized = self.authorized.get(cp.id, {})
        stock = self.state.stocks[cp.id].munitions

        def urgency(item: tuple[str, int]) -> float:
            name, _ = item
            want = max(1, authorized.get(name, 1))
            return (want - stock.get(name, 0)) / want

        fitted: dict[str, int] = {}
        used = 0.0

        def value(item: tuple[str, int]) -> float:
            return MunitionPrices.price(item[0], self.settings)

        # Most urgent first; among equally short items, the most valuable.
        ranked = sorted(
            load.items(), key=lambda kv: (-round(urgency(kv), 2), -value(kv))
        )
        for name, count in ranked:
            mass = MunitionMasses.mass_kg(name)
            room = int((limit_kg - used) // mass) if mass > 0 else count
            take = min(count, room)
            if take > 0:
                fitted[name] = take
                used += take * mass
        return fitted, max(0.0, min(fuel, limit_kg - used))

    # Carrier onboard delivery --------------------------------------------------------

    def carrier_transports(
        self, carrier: ControlPoint
    ) -> list[tuple[AircraftType, ControlPoint]]:
        """Transport squadrons (aircraft type, home) able to fly to `carrier`."""
        from game.ato.flighttype import FlightType

        found = []
        for squadron in self.coalition.air_wing.iter_squadrons():
            aircraft = squadron.aircraft
            if (
                squadron.can_auto_assign(FlightType.TRANSPORT)
                and aircraft.capable_of(FlightType.TRANSPORT)
                and carrier.can_operate(aircraft)
            ):
                found.append((aircraft, squadron.location))
        return found

    @staticmethod
    def can_fly_leg(
        aircraft: AircraftType,
        home: ControlPoint,
        depot: ControlPoint,
        to: ControlPoint,
    ) -> bool:
        """Same reach rule as the airlift planner: helicopters, 100 nm per leg."""
        from game.transfers import AirliftPlanner

        if not depot.can_operate(aircraft):
            return False
        if not aircraft.dcs_unit_type.helicopter:
            return True
        reach = AirliftPlanner.HELO_MAX_RANGE.meters
        a, b, c = home.position, depot.position, to.position
        return (
            a.distance_to_point(b) <= reach
            and b.distance_to_point(c) <= reach
            and c.distance_to_point(a) <= reach
        )

    def _onboard_load(
        self, carrier: ControlPoint, depot: ControlPoint, shortfall: Counter[str]
    ) -> dict[str, int]:
        """What `depot` can fly out to `carrier` for its shortfall.

        Spare stock (beyond the depot's own needs) can always go. For an urgent item,
        one the carrier holds less than half of what it should, the depot also gives
        up part of its own stock, keeping at least half of what it needs itself.
        """
        stock = self.state.stocks[depot.id].munitions
        own = self.authorized.get(depot.id, {})
        wanted = self.authorized.get(carrier.id, {})
        load: dict[str, int] = {}
        for name, gap in shortfall.items():
            want = wanted.get(name, 0)
            urgent = want - gap < want * URGENT_SHARE
            keep = own.get(name, 0)
            if urgent:
                keep = math.ceil(keep * DEPOT_RESERVE_SHARE)
            take = min(gap, stock.get(name, 0) - keep)
            if take > 0:
                load[name] = take
        return load

    def airlift_to_carriers(self, report: PurchaseReport) -> None:
        """Urgent top-ups flown to carriers from land depots (C-2, helicopters).

        Before anything is bought onto a carrier's next replenishment ship, stock
        ashore is flown out for what the carrier is short of: spare stock, and for
        items the carrier is badly short of, part of the depot's own (see
        _onboard_load). One aircraft load per carrier per turn, most urgent and most
        valuable items first. The rest is bought for the ship as usual.
        """
        from game.transfers import TransferOrder

        truck = self.cargo_truck()
        if truck is None:
            return
        land_depots = [
            d
            for d in self.depots
            if not isinstance(d, (NavalControlPoint, OffMapSpawn))
        ]
        for carrier in self.bases:
            if not isinstance(carrier, NavalControlPoint):
                continue
            if any(
                t.destination is carrier and t.request_airflift
                for t in iter_supply_transfers(self.coalition)
            ):
                continue  # one run at a time
            shortfall = self._shortfall(carrier)
            if not shortfall:
                continue
            transports = self.carrier_transports(carrier)
            best: Optional[tuple[float, ControlPoint, float, dict[str, int]]] = None
            for depot in land_depots:
                reachable = [
                    cargo_tons(aircraft)
                    for aircraft, home in transports
                    if self.can_fly_leg(aircraft, home, depot, carrier)
                ]
                if not reachable:
                    continue
                load = self._onboard_load(carrier, depot, shortfall)
                if not load:
                    continue
                distance = depot.position.distance_to_point(carrier.position)
                if best is None or distance < best[0]:
                    best = (distance, depot, max(reachable), load)
            if best is None:
                continue
            _, depot, tons_each, load = best
            load, _ = self._fit_to_shipment(carrier, load, 0.0, tons_each * 1000)
            if not load:
                continue
            depot_stock = self.state.stocks[depot.id]
            for name, count in load.items():
                depot_stock.munitions[name] -= count
            tons = MunitionMasses.tons(load)
            transfer = TransferOrder(
                depot,
                carrier,
                {truck: 1},
                request_airflift=True,
                supplies=SupplyLoad(munitions=load, tons=tons, carriers=1),
            )
            self.coalition.transfers.pending_transfers.append(transfer)
            self.inbound[carrier.id].update(load)
            report.cod_runs += 1
            logging.info(
                "Supply: onboard delivery of %s from %s to %s",
                transfer.supplies.describe() if transfer.supplies else "",
                depot.name,
                carrier.name,
            )

    def cargo_truck(self) -> Optional[GroundUnitType]:
        units = sorted(
            self.coalition.faction.logistics_units, key=lambda u: u.variant_id
        )
        for preferred in TRUCK_PREFERENCE:
            for unit in units:
                if preferred.lower() in unit.variant_id.lower():
                    return unit
        trucks = [u for u in units if "truck" in u.variant_id.lower()]
        if trucks:
            return trucks[0]
        if units:
            return units[0]
        from game.dcs.groundunittype import GroundUnitType

        fallback = (
            "Truck M818 6x6" if self.coalition.player.is_blue else ("Truck Ural-375")
        )
        try:
            return GroundUnitType.named(fallback)
        except KeyError:
            return None

    def run(self) -> PurchaseReport:
        report = PurchaseReport()
        report.cancelled_runs = list(getattr(self, "cancelled_runs", []))
        self.produce_fuel()
        if (
            self.settings.logistics_supply_lines
            and self.settings.logistics_carrier_airlift
        ):
            self.airlift_to_carriers(report)
        self.produce(report)
        if self.settings.logistics_supply_lines:
            # Before the ships sail: with main base only, they're loaded here.
            self.distribute(report)
        self.dispatch_ships(report)
        return report


def report_lines(report: PurchaseReport, settings: Settings) -> list[str]:
    lines = []
    if report.delivered:
        lines.append(
            f"{sum(report.delivered.values())} ordered munitions arrived at depots."
        )
    if report.refunded:
        lines.append(
            f"${report.refunded:,.1f}M refunded for orders at depots lost this turn."
        )
    if report.bought:
        bought = sum(report.bought.values())
        cost = (
            f" for ${report.spent:,.1f}M" if settings.logistics_munitions_cost else ""
        )
        lines.append(f"Bought {bought} munitions{cost}.")
    if report.unaffordable:
        top = ", ".join(
            f"{k.split('.', 2)[-1]} ×{v}" for k, v in report.unaffordable.most_common(5)
        )
        lines.append(f"Couldn't afford: {top}.")
    if report.shipments:
        lines.append(f"{report.shipments} supply shipment(s) dispatched.")
    if report.cancelled_runs:
        lines.append(
            f"{len(report.cancelled_runs)} supply run(s) called off: no transport "
            "could reach them. The cargo went back into stock."
        )
    if report.throughput_limited:
        names = ", ".join(sorted(set(report.throughput_limited)))
        lines.append(f"At their cargo handling limit: {names}.")
    if report.cod_runs:
        lines.append(f"{report.cod_runs} onboard delivery run(s) sent to carriers.")
    if report.ships_sent:
        lines.append(f"{report.ships_sent} replenishment ship(s) sailed for carriers.")
    if report.ships_arrived:
        lines.append(f"{report.ships_arrived} replenishment ship(s) unloaded.")
    if report.air_only_carriers:
        names = ", ".join(sorted(set(report.air_only_carriers)))
        lines.append(
            f"No open water for replenishment ships to reach {names}: supplies are "
            "flown out instead."
        )
    if report.ships_lost:
        names = ", ".join(sorted(set(report.ships_lost)))
        lines.append(f"Cargo lost: no carrier left to unload at ({names}).")
    if report.still_short:
        lines.append(
            f"{report.still_short} munition type(s) are running short. Order them "
            "with Order munitions in any base's Logistics tab."
        )
    return lines


def iter_supply_transfers(coalition: Coalition) -> Iterable[TransferOrder]:
    for transfer in coalition.transfers.pending_transfers:
        if supply_of(transfer) is not None:
            yield transfer
