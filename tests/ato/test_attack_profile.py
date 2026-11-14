from types import SimpleNamespace
from typing import Any

import pytest

from game.ato import attackprofile
from game.ato.attackprofile import (
    AttackProfile,
    Terrain,
    attack_settings,
    briefing_lines,
    flown_profile,
    loft_solution,
    low_height,
    profile_of,
)
from game.ato.flighttype import FlightType
from game.utils import feet, meters


def flight(
    task: FlightType = FlightType.STRIKE,
    profile: Any = AttackProfile.SLED,
    players: int = 0,
    helo: bool = False,
    dcs_id: str = "F-15ESE",
) -> Any:
    return SimpleNamespace(
        flight_type=task,
        attack_profile=profile,
        client_count=players,
        is_helo=helo,
        unit_type=SimpleNamespace(dcs_unit_type=SimpleNamespace(id=dcs_id)),
    )


def test_only_jets_on_point_bombing_tasks_can_fly_low() -> None:
    assert attackprofile.supported(flight())
    assert attackprofile.supported(flight(FlightType.BAI))
    assert attackprofile.supported(flight(FlightType.OCA_RUNWAY))
    assert attackprofile.supported(flight(FlightType.OCA_AIRCRAFT))
    assert not attackprofile.supported(flight(FlightType.ARMED_RECON))
    assert not attackprofile.supported(flight(FlightType.CAS))
    assert not attackprofile.supported(flight(helo=True))
    assert not attackprofile.supported(flight(dcs_id="B-52H"))
    assert profile_of(flight(FlightType.CAS)) is AttackProfile.NORMAL


def test_old_saves_and_odd_values_read_as_normal() -> None:
    old = flight()
    del old.attack_profile
    assert profile_of(old) is AttackProfile.NORMAL
    assert profile_of(flight(profile="Loft")) is AttackProfile.NORMAL


def test_ai_flights_fly_the_sled_instead_of_a_loft() -> None:
    assert flown_profile(flight(profile=AttackProfile.LOFT)) is AttackProfile.SLED
    player = flight(profile=AttackProfile.LOFT, players=1)
    assert flown_profile(player) is AttackProfile.LOFT
    assert flown_profile(flight(profile=AttackProfile.POPUP)) is AttackProfile.POPUP


def test_rough_or_unknown_ground_is_flown_higher() -> None:
    flat = Terrain(meters(60))
    hills = Terrain(meters(400))
    unknown = Terrain(None)
    mountains = Terrain(meters(1200))
    assert low_height(flat) == feet(500)
    assert low_height(hills) == feet(1000)
    assert low_height(unknown) == feet(1000)
    assert mountains.mountainous and not hills.mountainous
    assert not unknown.mountainous and unknown.rough


def test_terrain_is_sampled_along_the_legs(monkeypatch: pytest.MonkeyPatch) -> None:
    heights = iter([100.0, 140.0, 520.0, 90.0] * 50)
    monkeypatch.setattr(attackprofile, "ground_m", lambda point: next(heights))
    start = SimpleNamespace(
        distance_to_point=lambda other: 3000.0,
        heading_between_point=lambda other: 90.0,
        point_from_heading=lambda heading, distance: object(),
    )
    terrain = attackprofile.terrain_along([(start, object())])  # type: ignore[list-item]
    assert terrain.relief == meters(420)  # three samples: 100, 140 and 520 m
    monkeypatch.setattr(attackprofile, "ground_m", lambda point: None)
    assert not attackprofile.terrain_along([(start, object())]).known  # type: ignore[list-item]


def test_release_altitudes_are_measured_from_the_target_ground(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attackprofile, "ground_m", lambda point: 400.0)
    target: Any = object()
    guided = ["weapons.bombs.GBU_12"]
    sled = attack_settings(AttackProfile.SLED, feet(500), target, guided)
    assert sled is not None and sled.attack_type is None
    assert sled.altitude_m == pytest.approx(400 + feet(500).meters)

    # Slick bombs release higher, clear of their own fragments.
    slick = attack_settings(
        AttackProfile.SLED, feet(500), target, ["weapons.bombs.Mk_82"]
    )
    assert slick is not None
    assert slick.altitude_m == pytest.approx(400 + feet(1500).meters)
    retarded = attack_settings(
        AttackProfile.SLED, feet(500), target, ["weapons.bombs.MK_82SNAKEYE"]
    )
    assert retarded is not None
    assert retarded.altitude_m == pytest.approx(400 + feet(500).meters)

    popup = attack_settings(AttackProfile.POPUP, feet(500), target, guided)
    assert popup is not None and popup.attack_type == "Dive"
    assert popup.altitude_m == pytest.approx(400 + feet(3000).meters)

    assert attack_settings(AttackProfile.NORMAL, feet(500), target, guided) is None


def test_unknown_target_ground_leaves_the_altitude_to_the_ai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(attackprofile, "ground_m", lambda point: None)
    sled = attack_settings(AttackProfile.SLED, feet(1000), object(), [])  # type: ignore[arg-type]
    assert sled is not None and sled.altitude_m is None


def test_the_loft_estimate_is_in_the_usual_range() -> None:
    loft = loft_solution(feet(500))
    assert 2.0 < loft.release.nautical_miles < 3.5
    assert loft.pull_up.meters > loft.release.meters
    assert loft.angle_deg == 30


def test_the_kneeboard_describes_the_profile() -> None:
    layout = SimpleNamespace(
        attack_profile="Loft (player flights)", low_level_height_ft=500
    )
    lines = briefing_lines(layout, [6])
    assert lines[0].startswith("LOFT: 500 ft AGL")
    assert "Pull up (STPT 6)" in lines[1]
    assert "(STPTs 6, 11)" in briefing_lines(layout, [6, 11])[1]
    assert briefing_lines(SimpleNamespace()) == []
    popup = SimpleNamespace(attack_profile="Pop-up dive", low_level_height_ft=1000)
    assert "roll in and dive" in briefing_lines(popup)[1]
