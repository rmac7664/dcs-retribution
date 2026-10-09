"""What each side actually fires, for spending its munitions budget well.

After each mission, every munition a side expended is added to a decaying average
(USAGE_DECAY per mission), so recent missions count most. Purchases then:

* buy items the side has been using before items it hasn't (supply.suggestions), and
* stock items it hasn't used for DORMANT_AFTER missions for a single sortie instead of
  the full "Munitions stocked for N sorties", so idle expensive munitions (cruise
  missiles a bomber never flies with, say) don't eat the budget.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from game import Game

#: Weight kept from older missions each time a new one is added.
USAGE_DECAY = 0.5
#: Missions of history needed before an unused item counts as dormant.
DORMANT_AFTER = 3


def record_usage(
    state: Any, game: Game, used_by_base: dict[str, dict[str, int]]
) -> None:
    """Adds a mission's expenditure (by base name) to each side's average."""
    theater = getattr(game, "theater", None)
    if theater is None or not hasattr(game, "blue"):
        return
    usage: dict[str, dict[str, float]] = getattr(state, "usage", None) or {}
    missions: dict[str, int] = getattr(state, "usage_missions", None) or {}
    sides: dict[str, dict[str, int]] = {}
    by_name = {cp.name: cp for cp in theater.controlpoints}
    for base, used in used_by_base.items():
        cp = by_name.get(base)
        if cp is None:
            continue
        side = sides.setdefault(cp.captured.name, {})
        for name, count in used.items():
            side[name] = side.get(name, 0) + count
    for player in (game.blue.player, game.red.player):
        side_usage = usage.setdefault(player.name, {})
        for name in list(side_usage):
            side_usage[name] *= USAGE_DECAY
            if side_usage[name] < 0.01:
                del side_usage[name]
        for name, count in sides.get(player.name, {}).items():
            side_usage[name] = side_usage.get(name, 0.0) + count
        missions[player.name] = missions.get(player.name, 0) + 1
    state.usage = usage
    state.usage_missions = missions


def usage_of(game: Any, side: str) -> dict[str, float]:
    state = getattr(game, "warehouse_logistics", None)
    return (getattr(state, "usage", None) or {}).get(side, {})


def is_dormant(game: Any, side: str, name: str) -> bool:
    """True if the side has flown DORMANT_AFTER missions without using `name`."""
    state = getattr(game, "warehouse_logistics", None)
    missions = (getattr(state, "usage_missions", None) or {}).get(side, 0)
    if missions < DORMANT_AFTER:
        return False
    if name.startswith("sam."):
        return False  # SAM sites keep their loads whether or not they fired
    return usage_of(game, side).get(name, 0.0) < 0.5
