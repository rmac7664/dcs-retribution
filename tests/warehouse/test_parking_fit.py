"""Transport planes need a parking spot they fit at each stop."""

from types import SimpleNamespace
from typing import Any

import pytest

import game.warehouse.supply as supply


class FakeAirfield:
    def __init__(self, name: str, slots: list[Any], version: int = 2) -> None:
        self.name = name
        self.airport = SimpleNamespace(parking_slots=slots, slot_version=version)


def slot(width: float, length: float = 60.0, airplanes: bool = True) -> Any:
    return SimpleNamespace(
        width=width, length=length, height=20.0, airplanes=airplanes, large=False
    )


def plane(width: float, helicopter: bool = False) -> Any:
    return SimpleNamespace(
        dcs_unit_type=SimpleNamespace(
            id=f"plane{width}",
            helicopter=helicopter,
            width=width,
            length=20.0,
            height=5.0,
        )
    )


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supply, "Airfield", FakeAirfield)
    monkeypatch.setattr(supply, "_PARKING_FIT", {})


def test_fighter_base_has_no_spot_for_a_c2() -> None:
    # Spangdahlem: lots of fighter spots, none wider than 24 m. A C-2 is 24.6 m.
    spangdahlem: Any = FakeAirfield("Spangdahlem", [slot(24.0)] * 105)
    ramstein: Any = FakeAirfield("Ramstein", [slot(24.0), slot(61.0)])
    c2 = plane(24.6)
    assert not supply.fits_parking(spangdahlem, c2)
    assert supply.fits_parking(ramstein, c2)


def test_helicopter_only_spots_dont_count_for_planes() -> None:
    helipads: Any = FakeAirfield("Pads", [slot(40.0, airplanes=False)])
    assert not supply.fits_parking(helipads, plane(20.0))


def test_helicopters_ships_and_unknown_slots_always_fit() -> None:
    small: Any = FakeAirfield("Small", [slot(10.0)])
    assert supply.fits_parking(small, plane(18.0, helicopter=True))
    carrier: Any = SimpleNamespace(name="CVN-62")  # not an airfield
    assert supply.fits_parking(carrier, plane(24.6))
    old_style: Any = FakeAirfield("Old", [slot(10.0)], version=1)
    assert supply.fits_parking(old_style, plane(24.6))
    no_data: Any = FakeAirfield("Empty", [])
    assert supply.fits_parking(no_data, plane(24.6))
