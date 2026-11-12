from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional

from game.ato.flighttype import FlightType
from game.ato.timeline import (
    FlightBar,
    PackageRow,
    build_rows,
    category_for,
    dcs_window,
    main_push,
    snap,
)
from game.settings.settings import FastForwardStopCondition

T0 = datetime(2026, 4, 24, 12, 0)


class FakePlan:
    def __init__(self, start: datetime, broken: bool = False) -> None:
        self.start = start
        self.broken = broken

    def startup_time(self) -> datetime:
        return self.start

    def takeoff_time(self) -> datetime:
        return self.start + timedelta(minutes=15)

    @property
    def landing_time(self) -> datetime:
        if self.broken:
            raise NotImplementedError
        return self.start + timedelta(hours=2)

    @property
    def mission_begin_on_station_time(self) -> Optional[datetime]:
        return None

    @property
    def mission_departure_time(self) -> datetime:
        return self.start + timedelta(hours=1)

    @property
    def tot(self) -> datetime:
        return self.start + timedelta(minutes=50)


def flight(start: datetime, players: int = 0, broken: bool = False) -> Any:
    return SimpleNamespace(
        flight_plan=FakePlan(start, broken),
        client_count=players,
        count=2,
        unit_type="F-16CM",
        flight_type=FlightType.STRIKE,
        custom_name=None,
    )


def package(task: Optional[FlightType], tot: datetime, *flights: Any) -> Any:
    return SimpleNamespace(
        primary_task=task,
        time_over_target=tot,
        flights=list(flights),
        target=SimpleNamespace(name="TURKEY"),
        package_description=task.value if task else "No mission",
    )


def game(blue: list[Any], red: list[Any], condition: Any) -> Any:
    return SimpleNamespace(
        blue=SimpleNamespace(ato=SimpleNamespace(packages=blue)),
        red=SimpleNamespace(ato=SimpleNamespace(packages=red)),
        conditions=SimpleNamespace(start_time=T0),
        settings=SimpleNamespace(
            fast_forward_stop_condition=condition,
            desired_player_mission_duration=timedelta(hours=1),
        ),
    )


def test_categories() -> None:
    assert category_for(FlightType.BARCAP) == "Defensive counter-air"
    assert category_for(FlightType.DEAD) == "SEAD / DEAD"
    assert category_for(FlightType.ANTISHIP) == "Interdiction"
    assert category_for(None) == "Other"


def test_rows_are_grouped_and_opfor_is_optional() -> None:
    strike = package(FlightType.STRIKE, T0 + timedelta(hours=2), flight(T0))
    cap = package(FlightType.BARCAP, T0 + timedelta(hours=1), flight(T0))
    red = package(FlightType.STRIKE, T0, flight(T0))
    g = game([strike, cap], [red], FastForwardStopCondition.DISABLED)
    assert [r.package for r in build_rows(g, False)] == [cap, strike]
    rows = build_rows(g, True)
    assert [r.package for r in rows] == [cap, strike, red]
    assert [r.editable for r in rows] == [True, True, False]


def test_missing_times_leave_gaps() -> None:
    row = build_rows(
        game(
            [package(FlightType.STRIKE, T0, flight(T0, broken=True))],
            [],
            FastForwardStopCondition.DISABLED,
        ),
        False,
    )[0]
    (bar,) = row.flights
    assert bar.landing is None
    assert bar.end == T0 + timedelta(hours=1)
    assert bar.name == "2× F-16CM Strike"


def test_dcs_window_follows_fast_forward_to_player_startup() -> None:
    start = T0 + timedelta(minutes=40)
    pkg = package(FlightType.STRIKE, T0 + timedelta(hours=2), flight(start, 2))
    for condition, expected in [
        (FastForwardStopCondition.DISABLED, T0),
        (FastForwardStopCondition.PLAYER_STARTUP, start),
        (FastForwardStopCondition.PLAYER_TAKEOFF, start + timedelta(minutes=15)),
    ]:
        g = game([pkg], [], condition)
        assert dcs_window(g, build_rows(g, False)) == (
            expected,
            expected + timedelta(hours=1),
        )


def test_main_push_is_the_first_player_package() -> None:
    late = package(FlightType.STRIKE, T0 + timedelta(hours=3), flight(T0, 1))
    early = package(FlightType.DEAD, T0 + timedelta(hours=2), flight(T0, 2))
    ai = package(FlightType.STRIKE, T0 + timedelta(hours=1), flight(T0))
    rows = build_rows(game([late, early, ai], [], None), False)
    assert main_push(rows) == T0 + timedelta(hours=2)
    assert main_push([r for r in rows if not r.player]) is None


def test_drag_preview_and_snap() -> None:
    bar = FlightBar(None, "x", False, T0, T0, None, None, None, T0)  # type: ignore[arg-type]
    moved = bar.shifted(timedelta(minutes=10))
    assert moved.startup == T0 + timedelta(minutes=10)
    assert moved.landing is None
    assert snap(timedelta(minutes=4.6)) == timedelta(minutes=5)
    assert snap(timedelta(minutes=-1.2)) == timedelta(minutes=-1)
    assert isinstance(PackageRow, type)
