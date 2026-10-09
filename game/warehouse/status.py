"""A base's supply situation in a few words, for the campaign map."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from .state import KG_PER_TON

if TYPE_CHECKING:
    from game import Game
    from game.theater import ControlPoint


@dataclass
class SupplyStatus:
    #: Share of the base's authorized munitions it holds (0-100).
    munitions_percent: int
    #: Share of its fuel capacity it holds (0-100).
    fuel_percent: int
    #: Whether new munitions are bought here (supply lines).
    depot: bool
    #: Supply runs on their way to the base.
    inbound_runs: int
    #: The side's main supply base (see supply.main_base).
    main_base: bool = False
    #: Tooltip lines.
    lines: list[str] = field(default_factory=list)

    @property
    def level(self) -> int:
        """The lower of the two, which is what limits flying."""
        return min(self.munitions_percent, self.fuel_percent)


def supply_status(game: Game, cp: ControlPoint) -> Optional[SupplyStatus]:
    """The supply situation at one of the player's managed bases, else None.

    Enemy bases report nothing: their stock levels are not something the player
    would know.
    """
    from .supply import SupplyPlanner, iter_supply_transfers, main_base, supply_of

    settings = game.settings
    state = game.warehouse_logistics
    if not cp.captured.is_blue or not state.is_managed(cp, settings):
        return None
    stock = state.stocks.get(cp.id)
    if stock is None:
        return None
    authorized = state.authorized_munitions(game, cp)
    wanted = sum(authorized.values())
    held = sum(min(stock.munitions.get(n, 0), want) for n, want in authorized.items())
    munitions = 100 if wanted <= 0 else round(100 * held / wanted)
    capacity = state.fuel_capacity_kg(cp, settings)
    fuel = 100 if capacity <= 0 else round(100 * min(1.0, stock.jet_fuel_kg / capacity))
    depot = settings.logistics_supply_lines and SupplyPlanner.is_depot(cp)

    runs = [t for t in iter_supply_transfers(cp.coalition) if t.destination.id == cp.id]
    ships = list(state.ships_bound_for(cp))
    lines = [
        f"Munitions {munitions}% of authorized",
        f"Fuel {fuel}% ({stock.jet_fuel_kg / KG_PER_TON:,.0f} t)",
    ]
    is_main = main_base(game, cp.captured) is cp
    if is_main:
        lines.insert(0, "Main supply base")
    elif depot:
        lines.append("Supply depot")
    from .priority import is_priority

    if is_priority(game, cp):
        lines.append("Priority base: resupplied first")
    for transfer in runs:
        load = supply_of(transfer)
        what = load.describe() if load is not None else "supplies"
        lines.append(f"Inbound from {transfer.origin.name}: {what}")
    if ships:
        lines.append(f"{len(ships)} replenishment ship(s) at sea")
    return SupplyStatus(
        munitions,
        fuel,
        depot,
        len(runs) + len(ships),
        main_base=is_main,
        lines=lines,
    )
