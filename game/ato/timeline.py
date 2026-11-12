"""Time data for the ATO timeline window.

Turns the packages of one or both ATOs into rows of flight bars: start-up,
airborne and on-station periods, and the time over target. Pure data, so it can
be tested without the UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional, TYPE_CHECKING

from game.ato.flighttype import FlightType

if TYPE_CHECKING:
    from game import Game
    from game.ato.flight import Flight
    from game.ato.package import Package


#: Row groups in display order, and the tasks in each.
CATEGORIES: list[tuple[str, frozenset[FlightType]]] = [
    (
        "Defensive counter-air",
        frozenset({FlightType.BARCAP, FlightType.INTERCEPTION}),
    ),
    (
        "Offensive counter-air",
        frozenset(
            {
                FlightType.TARCAP,
                FlightType.SWEEP,
                FlightType.ESCORT,
                FlightType.OCA_AIRCRAFT,
                FlightType.OCA_RUNWAY,
            }
        ),
    ),
    (
        "Support (AEW&C, tankers)",
        frozenset({FlightType.AEWC, FlightType.REFUELING, FlightType.RECOVERY}),
    ),
    (
        "SEAD / DEAD",
        frozenset(
            {
                FlightType.SEAD,
                FlightType.DEAD,
                FlightType.SEAD_ESCORT,
                FlightType.SEAD_SWEEP,
            }
        ),
    ),
    ("Strike", frozenset({FlightType.STRIKE})),
    (
        "Interdiction",
        frozenset({FlightType.BAI, FlightType.ANTISHIP, FlightType.ARMED_RECON}),
    ),
    ("Close air support", frozenset({FlightType.CAS})),
]
OTHER_CATEGORY = "Other"


def category_for(task: Optional[FlightType]) -> str:
    for name, tasks in CATEGORIES:
        if task in tasks:
            return name
    return OTHER_CATEGORY


def category_order() -> list[str]:
    return [name for name, _ in CATEGORIES] + [OTHER_CATEGORY]


@dataclass
class FlightBar:
    """One flight's times. Any of them can be missing for an unusual plan."""

    flight: Flight
    name: str
    player: bool
    startup: Optional[datetime]
    takeoff: Optional[datetime]
    landing: Optional[datetime]
    on_station: Optional[datetime]
    off_station: Optional[datetime]
    tot: Optional[datetime]

    @property
    def start(self) -> Optional[datetime]:
        return self.startup or self.takeoff or self.on_station or self.tot

    @property
    def end(self) -> Optional[datetime]:
        return self.landing or self.off_station or self.tot

    def shifted(self, delta: timedelta) -> FlightBar:
        """The same bar moved in time, for previewing a TOT drag."""

        def move(t: Optional[datetime]) -> Optional[datetime]:
            return None if t is None else t + delta

        return FlightBar(
            self.flight,
            self.name,
            self.player,
            move(self.startup),
            move(self.takeoff),
            move(self.landing),
            move(self.on_station),
            move(self.off_station),
            move(self.tot),
        )


@dataclass
class PackageRow:
    package: Package
    blue: bool
    category: str
    label: str
    flights: list[FlightBar] = field(default_factory=list)

    @property
    def player(self) -> bool:
        return any(f.player for f in self.flights)

    @property
    def editable(self) -> bool:
        """Only the player's own packages can be moved or edited here."""
        return self.blue


def _safe(read: object) -> Optional[datetime]:
    try:
        value = read() if callable(read) else read
    except Exception:  # Plans without that time raise; leave the gap.
        return None
    return value if isinstance(value, datetime) else None


def flight_name(flight: Flight) -> str:
    """ "2× F/A-18C Strike", with the flight's custom name first if it has one."""
    name = f"{flight.count}× {flight.unit_type} {flight.flight_type.value}"
    return f"{flight.custom_name}: {name}" if flight.custom_name else name


def flight_bar(flight: Flight) -> FlightBar:
    plan = flight.flight_plan
    return FlightBar(
        flight=flight,
        name=flight_name(flight),
        player=flight.client_count > 0,
        startup=_safe(plan.startup_time),
        takeoff=_safe(plan.takeoff_time),
        landing=_safe(lambda: plan.landing_time),
        on_station=_safe(lambda: plan.mission_begin_on_station_time),
        off_station=_safe(lambda: plan.mission_departure_time),
        tot=_safe(lambda: plan.tot),
    )


def package_label(package: Package) -> str:
    try:
        target = package.target.name
    except Exception:
        target = ""
    description = package.package_description
    return f"{description} — {target}" if target else description


def package_row(package: Package, blue: bool) -> PackageRow:
    row = PackageRow(
        package=package,
        blue=blue,
        category=category_for(package.primary_task),
        label=package_label(package),
    )
    for flight in package.flights:
        try:
            row.flights.append(flight_bar(flight))
        except Exception:
            logging.exception("Timeline: no times for %s", flight)
    return row


def build_rows(game: Game, show_opfor: bool) -> list[PackageRow]:
    """Every package as a row, grouped by category, then by time over target."""
    rows = [package_row(p, True) for p in game.blue.ato.packages]
    if show_opfor:
        rows.extend(package_row(p, False) for p in game.red.ato.packages)
    order = {name: i for i, name in enumerate(category_order())}
    rows.sort(
        key=lambda r: (
            order.get(r.category, len(order)),
            not r.blue,
            r.package.time_over_target,
        )
    )
    return rows


def dcs_window(game: Game, rows: Iterable[PackageRow]) -> tuple[datetime, datetime]:
    """When the DCS mission starts and the planned mission length after it.

    With fast forward to a player event, the mission starts at the earliest
    player flight's start-up (or taxi/take-off) time, otherwise at turn start.
    """
    from game.settings.settings import FastForwardStopCondition

    settings = game.settings
    start = game.conditions.start_time
    condition = getattr(settings, "fast_forward_stop_condition", None)
    player_bars = [f for r in rows if r.blue for f in r.flights if f.player]
    times: list[datetime] = []
    if condition is FastForwardStopCondition.PLAYER_STARTUP:
        times = [f.startup for f in player_bars if f.startup is not None]
    elif condition in (
        FastForwardStopCondition.PLAYER_TAXI,
        FastForwardStopCondition.PLAYER_TAKEOFF,
    ):
        times = [f.takeoff for f in player_bars if f.takeoff is not None]
    if times:
        start = max(start, min(times))
    return start, start + settings.desired_player_mission_duration


def main_push(rows: Iterable[PackageRow]) -> Optional[datetime]:
    """The player's push: the earliest time over target of a package with players."""
    tots = [
        r.package.time_over_target
        for r in rows
        if r.blue and r.player and r.package.time_over_target > datetime.min
    ]
    return min(tots) if tots else None


def snap(delta: timedelta, step: timedelta = timedelta(minutes=1)) -> timedelta:
    """Rounds a drag to whole minutes."""
    steps = round(delta / step)
    return step * steps
