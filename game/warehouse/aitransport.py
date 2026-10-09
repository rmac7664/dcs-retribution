"""The AI keeps a transport squadron, so its supply can go by air.

Airlifts, carrier onboard delivery and switching cargo to air all need transport
aircraft. An AI side with supply lines on and no transport squadron at all forms one
at its main supply base and asks for a pair of aircraft (bought by the normal
procurement, budget permitting).
"""

from __future__ import annotations

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from game import Game
    from game.coalition import Coalition
    from game.squadrons import Squadron

#: Aircraft the AI asks for when it forms a transport squadron.
TRANSPORT_AIRCRAFT = 2
#: Largest the squadron may grow.
SQUADRON_SIZE = 6


def ensure_transport_squadron(game: Game, coalition: Coalition) -> Optional[Squadron]:
    """Forms a transport squadron at the main base if the side has none."""
    from game.ato.flighttype import FlightType
    from game.campaignloader.squadrondefgenerator import SquadronDefGenerator
    from game.procurement import AircraftProcurementRequest
    from game.squadrons import Squadron

    from .supply import main_base

    settings = game.settings
    if not (settings.logistics_enabled and settings.logistics_supply_lines):
        return None
    if coalition.player.is_red and not settings.logistics_apply_to_opfor:
        return None
    if any(
        s.aircraft.capable_of(FlightType.TRANSPORT)
        and s.can_auto_assign(FlightType.TRANSPORT)
        for s in coalition.air_wing.iter_squadrons()
    ):
        return None
    base = main_base(game, coalition.player)
    if base is None:
        return None
    squadron_def = SquadronDefGenerator(coalition.faction).generate_for_task(
        FlightType.TRANSPORT, base
    )
    if squadron_def is None:
        return None
    squadron = Squadron.create_from(
        squadron_def,
        FlightType.TRANSPORT,
        SQUADRON_SIZE,
        base,
        coalition,
        game,
    )
    squadron.set_auto_assignable_mission_types({FlightType.TRANSPORT})
    # The air wing only has entries for aircraft it started with.
    coalition.air_wing.squadrons.setdefault(squadron.aircraft, []).append(squadron)
    coalition.add_procurement_request(
        AircraftProcurementRequest(base, FlightType.TRANSPORT, TRANSPORT_AIRCRAFT)
    )
    logging.info(
        "Supply: %s formed transport squadron %s (%s) at %s",
        coalition.player.name,
        squadron,
        squadron.aircraft,
        base.name,
    )
    return squadron
