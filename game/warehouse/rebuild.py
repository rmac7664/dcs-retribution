"""Rebuilding destroyed supply depot buildings.

Ammo depots, fuel depots, factories and warehouses that are destroyed can be
rebuilt: it costs the same as a runway repair and takes the same number of turns.
The player starts a rebuild from the base's Logistics tab; the AI rebuilds its own,
main supply base first. A base that changes hands loses any rebuild in progress.
"""

from __future__ import annotations

import logging
from typing import Any, Iterator, Optional, TYPE_CHECKING

from game.config import RUNWAY_REPAIR_COST

if TYPE_CHECKING:
    from game import Game
    from game.theater import ControlPoint

#: Turns a rebuild takes: the same as a runway repair (RunwayStatus.begin_repair).
REBUILD_TURNS = 4
#: What a rebuild costs ($M): the same as a runway repair.
REBUILD_COST = RUNWAY_REPAIR_COST
#: Rebuilds the AI starts per turn.
AI_REBUILDS_PER_TURN = 1


def _rebuilds(game: Game) -> dict[Any, list[Any]]:
    """Ground object ID -> [turns left, side that started it]."""
    state = game.warehouse_logistics
    rebuilds = getattr(state, "rebuilds", None)
    if rebuilds is None:
        rebuilds = {}
        state.rebuilds = rebuilds
    return rebuilds


def depot_buildings(cp: ControlPoint) -> list[Any]:
    from .supply import DEPOT_CATEGORIES

    return [
        tgo
        for tgo in cp.connected_objectives
        if getattr(tgo, "category", None) in DEPOT_CATEGORIES
    ]


def turns_left(game: Game, tgo: Any) -> Optional[int]:
    entry = _rebuilds(game).get(tgo.id)
    return entry[0] if entry else None


def can_rebuild(game: Game, tgo: Any) -> bool:
    return tgo.is_dead and turns_left(game, tgo) is None


def begin_rebuild(game: Game, tgo: Any) -> None:
    """Starts rebuilding a destroyed depot building. Raises ValueError if it can't."""
    cp = tgo.control_point
    coalition = cp.coalition
    if not can_rebuild(game, tgo):
        raise ValueError(f"{tgo.name} doesn't need rebuilding")
    if coalition.budget < REBUILD_COST:
        raise ValueError(
            f"Rebuilding costs ${REBUILD_COST}M; you have ${coalition.budget:,.0f}M"
        )
    coalition.adjust_budget(-REBUILD_COST)
    _rebuilds(game)[tgo.id] = [REBUILD_TURNS, cp.captured.name]
    logging.info(
        "Supply: %s began rebuilding %s at %s", cp.captured.name, tgo.name, cp.name
    )


def _ground_object(game: Game, tgo_id: Any) -> Optional[Any]:
    for cp in game.theater.controlpoints:
        for tgo in cp.connected_objectives:
            if tgo.id == tgo_id:
                return tgo
    return None


def _revive(tgo: Any) -> None:
    from game.sim import GameUpdateEvents

    events = GameUpdateEvents()
    for unit in tgo.units:
        if not unit.alive:
            unit.revive(events)


def process_rebuilds(game: Game) -> list[tuple[str, str, str]]:
    """Advances rebuilds a turn; returns (side, base, building) for those finished."""
    rebuilds = _rebuilds(game)
    finished = []
    for tgo_id, (turns, side) in list(rebuilds.items()):
        tgo = _ground_object(game, tgo_id)
        if tgo is None or tgo.control_point.captured.name != side:
            del rebuilds[tgo_id]  # destroyed for good, or the base changed hands
            continue
        if turns > 1:
            rebuilds[tgo_id] = [turns - 1, side]
            continue
        del rebuilds[tgo_id]
        _revive(tgo)
        dead_fuel = getattr(game.warehouse_logistics, "dead_fuel_depots", None)
        if dead_fuel is not None:
            dead_fuel.discard(tgo.id)
        finished.append((side, tgo.control_point.name, tgo.name))
        logging.info("Supply: %s rebuilt at %s", tgo.name, tgo.control_point.name)
    return finished


def _ai_candidates(game: Game, player: Any) -> Iterator[Any]:
    from .supply import main_base

    hub = main_base(game, player)
    bases = [cp for cp in game.theater.controlpoints if cp.captured == player]
    bases.sort(key=lambda cp: cp is not hub)
    for cp in bases:
        for tgo in depot_buildings(cp):
            if can_rebuild(game, tgo):
                yield tgo


def ai_rebuild(game: Game, coalition: Any) -> list[str]:
    """The AI rebuilds destroyed depot buildings, main base first, if it can afford."""
    started: list[str] = []
    for tgo in _ai_candidates(game, coalition.player):
        if len(started) >= AI_REBUILDS_PER_TURN:
            break
        # Keep money for everything else: only rebuild with twice the cost in hand.
        if coalition.budget < REBUILD_COST * 2:
            break
        begin_rebuild(game, tgo)
        started.append(f"{tgo.name} at {tgo.control_point.name}")
    return started
