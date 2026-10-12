"""Supply transports leaving the same base are spaced out."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from game.ato.flighttype import FlightType
from game.warehouse.spacing import space_out_transports

START = datetime(1988, 7, 14, 8, 0)


class FakePlan:
    def __init__(self, takeoff_minutes: float) -> None:
        self.base = START + timedelta(minutes=takeoff_minutes)
        self.tot_offset = timedelta()

    def takeoff_time(self) -> datetime:
        return self.base + self.tot_offset


def flight(
    base: str,
    minutes: float,
    task: FlightType = FlightType.TRANSPORT,
    cargo: bool = True,
    players: int = 0,
) -> Any:
    return SimpleNamespace(
        flight_type=task,
        cargo=object() if cargo else None,
        client_count=players,
        departure=base,
        flight_plan=FakePlan(minutes),
    )


def coalition(*flights: Any) -> Any:
    return SimpleNamespace(
        ato=SimpleNamespace(packages=[SimpleNamespace(flights=list(flights))])
    )


def takeoffs(*flights: Any) -> list[float]:
    return [
        (f.flight_plan.takeoff_time() - START).total_seconds() / 60 for f in flights
    ]


def test_same_base_transports_take_off_three_minutes_apart() -> None:
    c2s = [flight("Ramstein", 49) for _ in range(5)]
    assert space_out_transports(coalition(*c2s)) == 4
    assert takeoffs(*c2s) == [49, 52, 55, 58, 61]


def test_already_spaced_and_other_bases_are_left_alone() -> None:
    early = flight("Ramstein", 0)
    later = flight("Ramstein", 44)
    other_base = flight("Spangdahlem", 0)
    assert space_out_transports(coalition(early, later, other_base)) == 0
    assert takeoffs(early, later, other_base) == [0, 44, 0]


def test_close_takeoffs_are_pushed_back_in_order() -> None:
    a = flight("Ramstein", 10)
    b = flight("Ramstein", 11)
    c = flight("Ramstein", 15)
    space_out_transports(coalition(c, b, a))
    assert takeoffs(a, b, c) == [10, 13, 16]


def test_player_and_non_transport_flights_are_not_moved() -> None:
    ai = flight("Ramstein", 0)
    player = flight("Ramstein", 0, players=1)
    strike = flight("Ramstein", 0, task=FlightType.STRIKE, cargo=False)
    assert space_out_transports(coalition(ai, player, strike)) == 0
    assert takeoffs(ai, player, strike) == [0, 0, 0]
