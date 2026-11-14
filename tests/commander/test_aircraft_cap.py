"""The per-side AI aircraft limit cuts the lowest-priority work first."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Optional

from game.ato.flighttype import FlightType
from game.commander import aircraftcap

T0 = datetime(2026, 11, 13, 11, 0)


class FakeAto:
    def __init__(self, packages: list[Any]) -> None:
        self.packages = packages

    def remove_package(self, package: Any) -> None:
        self.packages.remove(package)


def flight(task: FlightType, count: int = 2, players: int = 0) -> Any:
    return SimpleNamespace(flight_type=task, count=count, client_count=players)


def package(
    task: FlightType,
    *flights: Any,
    minutes: int = 0,
    target: Optional[Any] = None,
) -> Any:
    flights = flights or (flight(task),)
    pkg = SimpleNamespace(
        primary_task=task,
        flights=list(flights),
        time_over_target=T0 + timedelta(minutes=minutes),
        target=target or SimpleNamespace(name=task.value),
        package_description=task.value,
    )
    pkg.remove_flight = lambda f: pkg.flights.remove(f)
    return pkg


def coalition(packages: list[Any], limit: Optional[int], blue: bool = False) -> Any:
    settings = SimpleNamespace(
        perf_limit_ai_aircraft=limit is not None,
        perf_max_ai_aircraft_blue=limit or 0,
        perf_max_ai_aircraft_red=limit or 0,
    )
    return SimpleNamespace(
        ato=FakeAto(packages),
        player=SimpleNamespace(is_blue=blue),
        game=SimpleNamespace(settings=settings),
    )


def tasks(side: Any) -> list[str]:
    return sorted(p.primary_task.value for p in side.ato.packages)


def test_nothing_is_cut_when_the_limit_is_off_or_not_reached() -> None:
    side = coalition([package(FlightType.STRIKE)], None)
    assert aircraftcap.enforce(side) == []
    side = coalition([package(FlightType.STRIKE)], 2)
    assert aircraftcap.enforce(side) == []


def test_sweeps_go_before_strikes_and_barcaps() -> None:
    base = SimpleNamespace(name="Hahn")
    side = coalition(
        [
            package(FlightType.BARCAP, target=base),
            package(FlightType.STRIKE),
            package(FlightType.SWEEP),
        ],
        4,
    )
    cuts = aircraftcap.enforce(side)
    assert [c.description for c in cuts] == ["Fighter sweep Fighter sweep"]
    assert tasks(side) == ["BARCAP", "Strike"]


def test_escorts_go_before_the_packages_they_escort() -> None:
    escort = flight(FlightType.ESCORT)
    strike = package(FlightType.STRIKE, flight(FlightType.STRIKE), escort)
    side = coalition([strike], 2)
    aircraftcap.enforce(side)
    assert strike.flights and escort not in strike.flights
    assert tasks(side) == ["Strike"]


def test_a_second_barcap_rotation_goes_before_strikes_and_the_first_stays() -> None:
    base = SimpleNamespace(name="Frankfurt")
    first = package(FlightType.BARCAP, target=base, minutes=10)
    second = package(FlightType.BARCAP, target=base, minutes=70)
    side = coalition([second, first, package(FlightType.STRIKE)], 4)
    aircraftcap.enforce(side)
    assert first in side.ato.packages and second not in side.ato.packages


def test_player_and_support_packages_are_never_cut() -> None:
    player = package(
        FlightType.STRIKE, flight(FlightType.STRIKE, count=4, players=1), minutes=90
    )
    awacs = package(FlightType.AEWC)
    tanker = package(FlightType.REFUELING)
    side = coalition([player, awacs, tanker, package(FlightType.CAS)], 1)
    aircraftcap.enforce(side)
    # Still over the limit, but only with work that is never cut.
    assert tasks(side) == ["AEW&C", "Refueling", "Strike"]
    # The player's three AI wingmen count towards the limit.
    assert aircraftcap.ai_aircraft_in(side.ato.packages) == 7


def test_within_a_step_the_latest_package_goes_first() -> None:
    early = package(FlightType.STRIKE, minutes=5)
    late = package(FlightType.STRIKE, minutes=60)
    side = coalition([early, late], 2)
    aircraftcap.enforce(side)
    assert side.ato.packages == [early]


def test_room_left_for_the_opfor_ai() -> None:
    side = coalition([package(FlightType.STRIKE)], 5)
    assert aircraftcap.room_left(side) == 3
    assert aircraftcap.room_left(coalition([], None)) is None
