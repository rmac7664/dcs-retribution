"""Spaces out supply transports leaving the same base.

Each supply run gets its own airlift package scheduled "as soon as possible", so
several transports at one base all start up, taxi, take off and (on the same
route) land back within seconds of each other. Some transports can't fly as a
multi-ship flight (the C-2 mod allows one aircraft per group), so a big load
becomes several identical single-ship flights. Delaying each one a little keeps
them from arriving in a clump.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from game.ato.flighttype import FlightType

if TYPE_CHECKING:
    from game.coalition import Coalition

#: Gap between AI supply transports taking off from the same base.
TRANSPORT_SPACING = timedelta(minutes=3)


def _is_ai_transport(flight: Any) -> bool:
    return (
        flight.flight_type is FlightType.TRANSPORT
        and getattr(flight, "cargo", None) is not None
        and not flight.client_count
    )


def space_out_transports(
    coalition: Coalition, spacing: timedelta = TRANSPORT_SPACING
) -> int:
    """Delays AI supply transports so ones from the same base take off `spacing`
    apart. Returns how many flights were delayed.

    Uses each flight's TOT offset, which survives the package being rescheduled.
    Player flights are left alone.
    """
    by_base: dict[Any, list[Any]] = defaultdict(list)
    for package in coalition.ato.packages:
        for flight in package.flights:
            if _is_ai_transport(flight):
                by_base[flight.departure].append(flight)

    delayed = 0
    for flights in by_base.values():
        if len(flights) < 2:
            continue
        flights.sort(key=lambda f: f.flight_plan.takeoff_time())
        previous = None
        for flight in flights:
            takeoff = flight.flight_plan.takeoff_time()
            if previous is not None and takeoff < previous + spacing:
                flight.flight_plan.tot_offset += previous + spacing - takeoff
                takeoff = flight.flight_plan.takeoff_time()
                delayed += 1
            previous = takeoff
    return delayed
