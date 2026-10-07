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
    ships_arrived: int = 0
    ships_lost: list[str] = field(default_factory=list)


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


def airhead(game: Game, player: Any) -> Optional[ControlPoint]:
    """Where the side's off-map rear area delivers: its rear-most main airfield.

    Stock from the rear area arrives here by strategic airlift (it isn't flown in the
    mission and can't be intercepted) and goes forward by convoy or airlift. It is the
    side's airfield with a working runway, held since the campaign began, that is
    farthest from any enemy base. None if the side has no off-map rear area or no such
    airfield.
    """
    points = list(game.theater.controlpoints)
    if not any(isinstance(cp, OffMapSpawn) and cp.captured == player for cp in points):
        return None
    enemy = [cp.position for cp in points if cp.captured == player.opponent]
    candidates = [
        cp
        for cp in points
        if isinstance(cp, Airfield)
        and cp.captured == player
        and cp.starting_coalition == player
        and cp.runway_is_operational()
    ]
    if not candidates:
        return None

    def safety(cp: ControlPoint) -> tuple[float, str]:
        nearest = min((cp.position.distance_to_point(e) for e in enemy), default=0.0)
        return nearest, cp.name

    return max(candidates, key=safety)


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
        if game.settings.logistics_supply_lines:
            self.depots = [cp for cp in self.bases if self.is_depot(cp)]
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
        for transfer in coalition.transfers.pending_transfers:
            load = supply_of(transfer)
            if load is None or transfer.destination.id not in self.inbound:
                continue
            share = load.scaled(transfer.size / max(1, load.carriers))
            self.inbound[transfer.destination.id].update(share.munitions)
            self.inbound_fuel[transfer.destination.id] += share.fuel_kg
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
        if not isinstance(cp, NavalControlPoint):
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
        if isinstance(cp, (OffMapSpawn, NavalControlPoint)):
            return True
        if cp.captured.is_neutral:
            return False
        if airhead(cp.coalition.game, cp.captured) is cp:
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
        for cp in self.bases:
            if cp in self.depots:
                served[cp.id] = cp
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
        need: dict[Any, Counter[str]] = {d.id: Counter() for d in self.depots}
        for cp in self.bases:
            depot = self.served_by.get(cp.id)
            if depot is None:
                continue
            need[depot.id].update(self._shortfall(cp))
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

        # Most-needed first (relative to what the side is authorized to hold).
        orders: list[Suggestion] = []
        for depot in self.depots:
            for name, count in need[depot.id].items():
                if count <= 0:
                    continue
                share = count / max(1, total_need[name])
                quantity = min(count, max(1, math.floor(cap.get(name, 1) * share)))
                urgency = count / max(1, total_authorized[name])
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
        for suggestion in self.suggestions():
            depot, name, quantity = (
                suggestion.depot,
                suggestion.resource,
                suggestion.quantity,
            )
            if pay:
                price = MunitionPrices.price(name, self.settings)
                affordable = (
                    quantity if price <= 0 else int((budget - report.spent) // price)
                )
                bought = max(0, min(quantity, affordable))
                if bought < quantity:
                    report.unaffordable[name] += quantity - bought
                report.spent += bought * price
            else:
                bought = quantity
            if bought:
                self._stock_or_ship(depot, {name: bought})
                report.bought[name] += bought
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

    def distribute(self, report: PurchaseReport) -> None:
        from game.transfers import TransferOrder

        truck = self.cargo_truck()
        fuel_by_supply_lines = self.settings.logistics_fuel_by_supply_lines
        for cp in self.bases:
            depot = self.served_by.get(cp.id)
            if depot is None or depot is cp:
                continue
            depot_stock = self.state.stocks[depot.id]
            depot_auth = self.authorized[depot.id]
            load: dict[str, int] = {}
            for name, gap in self._shortfall(cp).items():
                spare = depot_stock.munitions.get(name, 0) - depot_auth.get(name, 0)
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
            load, fuel = self._fit_to_shipment(cp, load, fuel)
            tons = MunitionMasses.tons(load, fuel)
            if not load and fuel < 1:
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
            carriers = min(
                self.settings.logistics_max_trucks_per_shipment,
                max(1, math.ceil(tons / self.settings.logistics_truck_tons)),
            )
            transfer = TransferOrder(
                depot,
                cp,
                {truck: carriers},
                request_airflift=self.settings.logistics_prefer_airlift,
                supplies=SupplyLoad(
                    munitions=load, fuel_kg=fuel, tons=tons, carriers=carriers
                ),
            )
            self.coalition.transfers.pending_transfers.append(transfer)
            self.inbound[cp.id].update(load)
            self.inbound_fuel[cp.id] += fuel
            report.shipments += 1
            logging.info(
                "Supply: %s from %s to %s",
                transfer.supplies.describe() if transfer.supplies else "",
                depot.name,
                cp.name,
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
            limit_kg = (
                self.settings.logistics_max_trucks_per_shipment
                * self.settings.logistics_truck_tons
                * 1000
            )
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

    def airlift_to_carriers(self, report: PurchaseReport) -> None:
        """Urgent top-ups flown to carriers from land depots (C-2, helicopters).

        Before anything is bought onto a carrier's next replenishment ship, spare stock
        already ashore is flown out for what the carrier is short of: one aircraft load
        per carrier per turn, most urgent and most valuable items first. The rest is
        bought for the ship as usual.
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
                stock = self.state.stocks[depot.id].munitions
                own = self.authorized.get(depot.id, {})
                load = {
                    name: min(gap, stock.get(name, 0) - own.get(name, 0))
                    for name, gap in shortfall.items()
                }
                load = {k: v for k, v in load.items() if v > 0}
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
        self.produce_fuel()
        if (
            self.settings.logistics_supply_lines
            and self.settings.logistics_carrier_airlift
        ):
            self.airlift_to_carriers(report)
        self.produce(report)
        self.dispatch_ships(report)
        if self.settings.logistics_supply_lines:
            self.distribute(report)
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
    if report.cod_runs:
        lines.append(f"{report.cod_runs} onboard delivery run(s) sent to carriers.")
    if report.ships_sent:
        lines.append(f"{report.ships_sent} replenishment ship(s) sailed for carriers.")
    if report.ships_arrived:
        lines.append(f"{report.ships_arrived} replenishment ship(s) unloaded.")
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
