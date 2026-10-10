"""Every base's supply situation on one page (the Logistics overview window)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from game.theater.controlpoint import Airfield, NavalControlPoint, OffMapSpawn

from .state import KG_PER_TON, short_name

if TYPE_CHECKING:
    from game import Game
    from game.theater import ControlPoint

#: A base holding less than this share of an item is short of it.
SHORT_SHARE = 0.5
#: An enemy airfield this close puts a base at risk.
RISK_RANGE_NM = 50
METERS_PER_NM = 1852.0


@dataclass
class BaseRow:
    name: str
    #: "Main base", "Depot", "Carrier", "Rear area" or "Base".
    role: str
    munitions_percent: int
    fuel_percent: int
    fuel_tons: float
    #: Items held below SHORT_SHARE of what the base should have, worst first.
    short_of: list[str] = field(default_factory=list)
    #: What is on its way, e.g. "2 by convoy, 1 by air".
    inbound: str = ""
    #: Why the base or its supply is in danger.
    risks: list[str] = field(default_factory=list)
    priority: bool = False
    cp: Any = None

    @property
    def level(self) -> int:
        return min(self.munitions_percent, self.fuel_percent)


@dataclass
class Overview:
    rows: list[BaseRow]
    #: Lines summing up last turn's logistics (spending, deliveries, losses).
    summary: list[str]


def _role(game: Game, cp: ControlPoint) -> str:
    from .supply import SupplyPlanner, main_base

    if main_base(game, cp.captured) is cp:
        return "Main base"
    if isinstance(cp, OffMapSpawn):
        return "Rear area"
    if isinstance(cp, NavalControlPoint):
        return "Carrier" if cp.is_carrier else "LHA"
    if game.settings.logistics_supply_lines and SupplyPlanner.is_depot(cp):
        return "Depot"
    return "Base"


def _how(transfer: Any) -> str:
    kind = type(getattr(transfer, "transport", None)).__name__
    return {
        "Convoy": "convoy",
        "CargoShip": "cargo ship",
        "Airlift": "air",
    }.get(kind, "waiting")


def _inbound(game: Game, cp: ControlPoint) -> str:
    from collections import Counter

    from .supply import iter_supply_transfers

    ways: Counter[str] = Counter()
    for transfer in iter_supply_transfers(cp.coalition):
        if transfer.destination is cp:
            ways[_how(transfer)] += 1
    ships = sum(1 for _ in game.warehouse_logistics.ships_bound_for(cp))
    if ships:
        ways["replenishment ship"] += ships
    return ", ".join(
        f"{n} by {how}" if how != "waiting" else f"{n} waiting for transport"
        for how, n in ways.items()
    )


def _risks(game: Game, cp: ControlPoint) -> list[str]:
    risks = []
    if isinstance(cp, (Airfield,)) and cp.has_active_frontline:
        risks.append("front line")
    if not isinstance(cp, OffMapSpawn):
        limit = RISK_RANGE_NM * METERS_PER_NM
        near = [
            other
            for other in game.theater.controlpoints
            if isinstance(other, Airfield)
            and other.captured == cp.captured.opponent
            and other.position.distance_to_point(cp.position) <= limit
        ]
        if near:
            risks.append(f"enemy airfield within {RISK_RANGE_NM} nm")
    route = _route_risk(game, cp)
    if route:
        risks.append(route)
    from .supply import carrier_reachable_by_sea

    if isinstance(cp, NavalControlPoint) and not carrier_reachable_by_sea(game, cp):
        risks.append("no sea access: supplied by air only")
    sunk = getattr(game.warehouse_logistics, "last_ships_sunk", {}) or {}
    if cp.name in sunk.get(cp.captured.name, []):
        risks.append("supply ship sunk last mission")
    from .supply import iter_supply_transfers, supply_of

    for transfer in iter_supply_transfers(cp.coalition):
        load = supply_of(transfer)
        if (
            transfer.destination is cp
            and load is not None
            and transfer.size < load.carriers
        ):
            risks.append("supply run hit en route")
            break
    return risks


def _route_risk(game: Game, cp: ControlPoint) -> Optional[str]:
    """How supply reaches a land base from its side's main base, if that's a worry.

    "no supply route": nothing can reach it (its roads run through enemy bases and
    it has no sea lane or airlift link). "supplied by air only": its last leg is an
    airlift, so everything it gets, fuel included, depends on transport aircraft.
    """
    from game.theater.transitnetwork import TransitConnection

    from .supply import main_base

    if not game.settings.logistics_supply_lines or isinstance(
        cp, (NavalControlPoint, OffMapSpawn)
    ):
        return None
    hub = main_base(game, cp.captured)
    if hub is None or hub is cp:
        return None
    try:
        network = cp.coalition.transit_network
        if not network.has_path_between(hub, cp):
            return "no supply route"
        path = network.shortest_path_between(hub, cp)
        before = path[-2] if len(path) > 1 else hub
        if network.link_type(before, cp) == TransitConnection.Airlift:
            return "supplied by air only"
    except Exception:
        return None
    return None


def base_row(game: Game, cp: ControlPoint) -> Optional[BaseRow]:
    from .priority import is_priority

    state = game.warehouse_logistics
    stock = state.stocks.get(cp.id)
    if stock is None:
        return None
    authorized = state.authorized_munitions(game, cp)
    wanted = sum(authorized.values())
    held = sum(min(stock.munitions.get(n, 0), w) for n, w in authorized.items())
    capacity = state.fuel_capacity_kg(cp, game.settings)
    shortages = sorted(
        (
            (stock.munitions.get(name, 0) / want, name)
            for name, want in authorized.items()
            if want > 0 and stock.munitions.get(name, 0) < want * SHORT_SHARE
        ),
    )
    return BaseRow(
        name=cp.name,
        role=_role(game, cp),
        munitions_percent=100 if wanted <= 0 else round(100 * held / wanted),
        fuel_percent=(
            100
            if capacity <= 0
            else round(100 * min(1.0, stock.jet_fuel_kg / capacity))
        ),
        fuel_tons=stock.jet_fuel_kg / KG_PER_TON,
        short_of=[short_name(name) for _, name in shortages],
        inbound=_inbound(game, cp),
        risks=_risks(game, cp),
        priority=is_priority(game, cp),
        cp=cp,
    )


def overview(game: Game, player: Any) -> Overview:
    """The supply situation at every base `player` manages, worst first."""
    from .report import summary_lines

    state = game.warehouse_logistics
    rows = []
    for cp in state.managed_points(game):
        if cp.captured != player:
            continue
        row = base_row(game, cp)
        if row is not None:
            rows.append(row)
    rows.sort(key=lambda r: (r.role == "Rear area", r.level, r.name))
    return Overview(rows, summary_lines(game, player))
