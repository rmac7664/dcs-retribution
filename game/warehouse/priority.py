"""Priority bases: resupplied first.

The player marks bases as priority (e.g. before an offensive) from the Logistics
overview or a base's Logistics tab. A priority base's shortfalls are bought first when
money is short, and it is first in line for spare stock at its depot. The AI treats
its front-line bases as priority.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from game import Game
    from game.theater import ControlPoint


def is_priority(game: Optional[Game], cp: ControlPoint) -> bool:
    captured = getattr(cp, "captured", None)
    if captured is None:
        return False
    if getattr(captured, "is_blue", False):
        state = getattr(game, "warehouse_logistics", None)
        chosen = getattr(state, "priority_bases", None) or set()
        return cp.id in chosen
    return bool(getattr(cp, "has_active_frontline", False))


def set_priority(game: Game, cp: ControlPoint, priority: bool) -> None:
    state = game.warehouse_logistics
    if getattr(state, "priority_bases", None) is None:
        state.priority_bases = set()
    if priority:
        state.priority_bases.add(cp.id)
    else:
        state.priority_bases.discard(cp.id)
