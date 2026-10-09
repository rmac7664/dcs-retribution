"""Real-world callsigns for aircraft DCS gives no type-specific list (the F-14)."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml
from dcs import Mission
from dcs.countries import USA
from dcs.mapping import Point
from dcs.planes import F_14B, FA_18C_hornet
from dcs.terrain import Caucasus

from game.callsigns import (
    EXTRA_CALLNAMES,
    apply_custom_callsign,
    create_group_callsign_from_unit,
    dcs_callnames,
    extra_callnames,
)
from game.missiongenerator.aircraft.flightgroupspawner import FlightGroupSpawner
from game.missiongenerator.missiondata import MissionData


def test_f14_gets_squadron_callsigns_for_us_flights_only() -> None:
    assert "Wichita" in extra_callnames("F-14B", "USA")
    assert "Dog" in extra_callnames("F-14A-135-GR", "Combined Joint Task Forces Blue")
    assert extra_callnames("F-14A-135-GR", "Iran") == []
    assert extra_callnames("FA-18C_hornet", "USA") == []


def test_dcs_callnames_include_type_specific_names() -> None:
    usa = USA()
    assert "Enfield" in dcs_callnames(usa, F_14B)
    assert "Wichita" not in dcs_callnames(usa, F_14B)
    assert "Ragin" in dcs_callnames(usa, FA_18C_hornet)


def make_group() -> Any:
    mission = Mission(Caucasus())
    return mission.flight_group_inflight(
        mission.country(USA.name),
        "test",
        F_14B,
        Point(0, 0, mission.terrain),
        altitude=5000,
        group_size=2,
    )


def test_custom_callsign_names_the_group() -> None:
    group = make_group()
    apply_custom_callsign(group, "Dog", 3)
    assert [u.callsign_as_str() for u in group.units] == ["Dog31", "Dog32"]
    assert create_group_callsign_from_unit(group.units[0]) == "Dog 3"
    apply_custom_callsign(group, "Fast Eagle", 1)
    assert create_group_callsign_from_unit(group.units[0]) == "Fast Eagle 1"
    # DCS identifies callsigns by number: keep a valid generic one.
    assert group.units[0].callsign_dict[1] == 1


def spawner(flight_callsign: Any, squadron_callsign: Any, data: MissionData) -> Any:
    spawner: Any = object.__new__(FlightGroupSpawner)
    spawner.country = USA()
    spawner.mission_data = data
    spawner._callsigns = None
    spawner.flight = SimpleNamespace(
        callsign=flight_callsign,
        squadron=SimpleNamespace(callsign=squadron_callsign),
        unit_type=SimpleNamespace(dcs_unit_type=F_14B),
    )
    return spawner


def test_squadron_callsign_is_numbered_across_the_mission() -> None:
    data = MissionData()
    first = spawner(None, "Dog", data)
    second = spawner(None, "Dog", data)
    assert first._resolve_callsigns() == ((None, None), ("Dog", 1))
    assert second._resolve_callsigns() == ((None, None), ("Dog", 2))
    # A flight's own callsign wins; DCS names go through pydcs as before.
    own = spawner(SimpleNamespace(name="Enfield", nr=4), "Dog", data)
    assert own._resolve_callsigns() == (("Enfield", 4), None)
    picked = spawner(SimpleNamespace(name="Rage", nr=2), None, data)
    assert picked._resolve_callsigns() == ((None, None), ("Rage", 2))
    # No callsign anywhere: DCS picks one.
    assert spawner(None, None, data)._resolve_callsigns() == ((None, None), None)


def test_every_f14_squadron_callsign_is_offered() -> None:
    offered = set(EXTRA_CALLNAMES["F-14"])
    files = list(Path("resources/squadrons").glob("F-14*/*.yaml"))
    assert files
    for path in files:
        data = yaml.safe_load(path.read_text(encoding="utf8"))
        assert data.get("callsign") in offered, path
