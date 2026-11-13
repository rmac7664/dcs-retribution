"""Additional strike targets for a package.

A package normally attacks one target. A strike package can optionally be given
further targets, which its strike flights attack in order after the first one
before they turn for home. Escorts stay with the strike flights throughout.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from game.theater.theatergroundobject import TheaterGroundObject

from .flighttype import FlightType

if TYPE_CHECKING:
    from game import Game
    from game.theater import MissionTarget

    from .flight import Flight
    from .package import Package


def strike_flights(package: Package) -> list[Flight]:
    return [f for f in package.flights if f.flight_type is FlightType.STRIKE]


def supported(package: Package) -> bool:
    """Whether the package can take additional targets at all."""
    return isinstance(package.target, TheaterGroundObject) and bool(
        strike_flights(package)
    )


def _can_strike(target: TheaterGroundObject, package: Package) -> bool:
    if not package.flights:
        return False
    player = package.flights[0].coalition.player
    if target.is_friendly(player) or target.is_dead:
        return False
    if not target.strike_targets:
        return False
    return FlightType.STRIKE in target.mission_types(player)


def candidates(package: Package, game: Game) -> list[TheaterGroundObject]:
    """Targets that can be added, nearest to the package's last target first."""
    taken = [package.target, *package.additional_targets]
    origin = taken[-1].position
    found = [
        target
        for target in game.theater.ground_objects
        if not any(target is other for other in taken) and _can_strike(target, package)
    ]
    found.sort(key=lambda target: target.position.distance_to_point(origin))
    return found


def live_additional_targets(package: Package) -> list[TheaterGroundObject]:
    """The additional targets that still have something to attack, in order."""
    return [
        target
        for target in getattr(package, "additional_targets", [])
        if isinstance(target, TheaterGroundObject) and target.strike_targets
    ]


def set_additional_targets(package: Package, targets: list[MissionTarget]) -> None:
    """Replaces the package's additional targets and replans its strike flights."""
    unique: list[MissionTarget] = []
    for target in targets:
        if target is package.target or any(target is other for other in unique):
            continue
        unique.append(target)
    package.additional_targets = unique
    # Every flight, not only the strikers: the escorts leave the package at a split
    # point that follows the last target.
    for flight in package.flights:
        flight.recreate_flight_plan()
