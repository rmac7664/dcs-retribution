"""Fuel depots hold a base's fuel: destroying one burns part of it.

A base's fuel is spread over its fuel depots. When a strike destroys one, that
depot's share of the base's fuel is lost (BURN_SHARE of it, the rest having been
in tanks elsewhere on the base). Checked at the end of each turn, for both sides.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from game import Game

#: Share of a destroyed depot's fuel that is lost.
BURN_SHARE = 0.8
FUEL_CATEGORY = "fuel"


def fuel_depot_losses(game: Game) -> list[tuple[str, str, float]]:
    """Burns fuel at bases whose fuel depots were destroyed since last checked.

    Returns (side, base, tonnes burned) for each base that lost fuel.
    """
    state = game.warehouse_logistics
    known = getattr(state, "dead_fuel_depots", None)
    first_check = known is None
    if known is None:
        known = set()
    losses = []
    for cp in game.theater.controlpoints:
        depots = [
            tgo
            for tgo in cp.connected_objectives
            if getattr(tgo, "category", None) == FUEL_CATEGORY
        ]
        if not depots:
            continue
        newly_dead = [d for d in depots if d.is_dead and d.id not in known]
        for depot in newly_dead:
            known.add(depot.id)
        stock = state.stocks.get(cp.id)
        if first_check or not newly_dead or stock is None:
            continue
        burned = stock.jet_fuel_kg * BURN_SHARE * len(newly_dead) / len(depots)
        stock.jet_fuel_kg = max(0.0, stock.jet_fuel_kg - burned)
        if burned >= 1000:
            losses.append((cp.captured.name, cp.name, burned / 1000))
            logging.info(
                "Supply: fuel depot destroyed at %s; %.0f t of fuel burned",
                cp.name,
                burned / 1000,
            )
    state.dead_fuel_depots = known
    return losses
