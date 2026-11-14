"""A per-side limit on AI aircraft in the mission, for server performance.

When the limit is on, each side's planned air tasking is cut back after planning
until the side has no more AI aircraft than its limit. The lowest-priority work
goes first, a whole step at a time:

1. fighter sweeps and TARCAPs;
2. fighter escorts inside other packages;
3. second and later BARCAP rotations over a base;
4. attacks on airfields, ships, and armed reconnaissance;
5. strikes and BAI;
6. attacks on air defences (SEAD and DEAD);
7. close air support and air assaults;
8. the first BARCAP over each base.

Within a step the latest package goes first. Packages with player slots, AEW&C,
tankers, transports and ferry flights are never cut, so a side can stay above its
limit if those alone exceed it. AI wingmen in player flights count towards the
limit.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Iterable, Optional, TYPE_CHECKING

from game.ato.flighttype import FlightType

if TYPE_CHECKING:
    from game.ato.flight import Flight
    from game.ato.package import Package
    from game.coalition import Coalition
    from game.settings import Settings
    from game.theater.player import Player


#: Flights that are never cut: support and logistics the rest depends on.
PROTECTED_TASKS = frozenset(
    {
        FlightType.AEWC,
        FlightType.REFUELING,
        FlightType.RECOVERY,
        FlightType.TRANSPORT,
        FlightType.FERRY,
    }
)

SWEEPS = frozenset({FlightType.SWEEP, FlightType.SEAD_SWEEP, FlightType.TARCAP})
AIRFIELDS_AND_SHIPS = frozenset(
    {
        FlightType.OCA_AIRCRAFT,
        FlightType.OCA_RUNWAY,
        FlightType.ANTISHIP,
        FlightType.ARMED_RECON,
    }
)
STRIKES = frozenset({FlightType.STRIKE, FlightType.BAI})
AIR_DEFENCES = frozenset({FlightType.SEAD, FlightType.DEAD})
GROUND_SUPPORT = frozenset(
    {
        FlightType.CAS,
        FlightType.AIR_ASSAULT,
        FlightType.AFAC_AREA,
        FlightType.AFAC_FRONTLINE,
    }
)

# Steps, in the order they are cut. Step 2 (escort flights) is handled apart, as
# it trims flights out of packages rather than dropping packages.
STEP_SWEEPS = 1
STEP_ESCORTS = 2
STEP_EXTRA_BARCAP = 3
STEP_AIRFIELDS_AND_SHIPS = 4
STEP_STRIKES = 5
STEP_AIR_DEFENCES = 6
STEP_GROUND_SUPPORT = 7
STEP_FIRST_BARCAP = 8


def limit_for(settings: Settings, player: Player) -> Optional[int]:
    """The side's AI aircraft limit, or None when the limit is off."""
    if not getattr(settings, "perf_limit_ai_aircraft", False):
        return None
    if player.is_blue:
        return int(getattr(settings, "perf_max_ai_aircraft_blue", 35))
    return int(getattr(settings, "perf_max_ai_aircraft_red", 35))


def ai_aircraft_in_flight(flight: Flight) -> int:
    return max(0, flight.count - flight.client_count)


def ai_aircraft_in(packages: Iterable[Package]) -> int:
    """AI aircraft across these packages. Player-flown seats don't count."""
    return sum(ai_aircraft_in_flight(f) for p in packages for f in p.flights)


def has_players(package: Package) -> bool:
    return any(f.client_count > 0 for f in package.flights)


def is_protected(package: Package) -> bool:
    """Never cut: it has players, or it is support or logistics."""
    if has_players(package):
        return True
    return any(f.flight_type in PROTECTED_TASKS for f in package.flights)


def _step(package: Package, first_barcaps: set[int]) -> int:
    task = package.primary_task
    if task in SWEEPS:
        return STEP_SWEEPS
    if task is FlightType.BARCAP:
        return STEP_FIRST_BARCAP if id(package) in first_barcaps else STEP_EXTRA_BARCAP
    if task in STRIKES:
        return STEP_STRIKES
    if task in AIR_DEFENCES:
        return STEP_AIR_DEFENCES
    if task in GROUND_SUPPORT:
        return STEP_GROUND_SUPPORT
    # Airfields, ships, armed recon, and anything new or unusual.
    return STEP_AIRFIELDS_AND_SHIPS


def _first_barcaps(packages: Iterable[Package]) -> set[int]:
    """The earliest BARCAP over each base, by package identity."""
    by_target: dict[int, list[Package]] = defaultdict(list)
    for package in packages:
        if package.primary_task is FlightType.BARCAP:
            by_target[id(package.target)].append(package)
    return {
        id(min(group, key=lambda p: p.time_over_target)) for group in by_target.values()
    }


@dataclass(frozen=True)
class Cut:
    """One thing taken out of the plan, for the log and the OPFOR AI."""

    description: str
    aircraft: int


def describe(package: Package) -> str:
    try:
        target = package.target.name
    except Exception:
        target = "?"
    return f"{package.package_description} {target}"


def enforce(coalition: Coalition) -> list[Cut]:
    """Cuts the side's air tasking back to its limit. Returns what was cut."""
    limit = limit_for(coalition.game.settings, coalition.player)
    if limit is None:
        return []
    return cut_to(coalition, limit)


def cut_to(coalition: Coalition, limit: int) -> list[Cut]:
    ato = coalition.ato
    cuts: list[Cut] = []

    def over() -> int:
        return ai_aircraft_in(ato.packages) - limit

    if over() <= 0:
        return cuts
    first_barcaps = _first_barcaps(ato.packages)

    for step in range(STEP_SWEEPS, STEP_FIRST_BARCAP + 1):
        if over() <= 0:
            break
        if step == STEP_ESCORTS:
            cuts.extend(_cut_escorts(ato.packages, over))
            continue
        candidates = [
            p
            for p in ato.packages
            if not is_protected(p) and _step(p, first_barcaps) == step
        ]
        # Latest first: the end of the mission is what players see least of.
        candidates.sort(key=lambda p: p.time_over_target, reverse=True)
        for package in candidates:
            if over() <= 0:
                break
            aircraft = ai_aircraft_in([package])
            cuts.append(Cut(describe(package), aircraft))
            ato.remove_package(package)

    remaining = ai_aircraft_in(ato.packages)
    side = "BLUFOR" if coalition.player.is_blue else "OPFOR"
    if cuts:
        logging.info(
            "%s AI aircraft limit %d: cut %d aircraft (%s), %d remain",
            side,
            limit,
            sum(c.aircraft for c in cuts),
            "; ".join(c.description for c in cuts),
            remaining,
        )
    if remaining > limit:
        logging.warning(
            "%s still has %d AI aircraft after cutting to its limit of %d: the rest "
            "are support, logistics or flights with players, which are never cut.",
            side,
            remaining,
            limit,
        )
    return cuts


def _cut_escorts(packages: list[Package], over: Callable[[], int]) -> list[Cut]:
    """Takes fighter escorts out of AI packages, latest package first."""
    cuts: list[Cut] = []
    for package in sorted(packages, key=lambda p: p.time_over_target, reverse=True):
        if over() <= 0:
            break
        if is_protected(package):
            continue
        escorts = [f for f in package.flights if f.flight_type is FlightType.ESCORT]
        # A package that is only escorts is left to the package steps.
        if not escorts or len(escorts) == len(package.flights):
            continue
        for flight in escorts:
            if over() <= 0:
                break
            cuts.append(
                Cut(f"escort for {describe(package)}", ai_aircraft_in_flight(flight))
            )
            package.remove_flight(flight)
    return cuts


def room_left(coalition: Coalition) -> Optional[int]:
    """How many more AI aircraft the side may plan, or None without a limit."""
    limit = limit_for(coalition.game.settings, coalition.player)
    if limit is None:
        return None
    return max(0, limit - ai_aircraft_in(coalition.ato.packages))
