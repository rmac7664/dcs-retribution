"""Pull-up and pop-up points sit after the ingress of every target set."""

from types import SimpleNamespace
from typing import Any

from game.ato.flightplans.formationattack import FormationAttackLayout
from game.ato.flightwaypointtype import FlightWaypointType


def waypoint(name: str, kind: FlightWaypointType = FlightWaypointType.NAV) -> Any:
    return SimpleNamespace(name=name, waypoint_type=kind)


def layout() -> Any:
    names = {}
    for name in ("departure", "join", "ingress", "split", "arrival", "bullseye"):
        names[name] = waypoint(name)
    first_target = waypoint("t1", FlightWaypointType.TARGET_POINT)
    second_ingress = waypoint("ingress2", FlightWaypointType.INGRESS_STRIKE)
    second_target = waypoint("t2", FlightWaypointType.TARGET_POINT)
    result = FormationAttackLayout(
        departure=names["departure"],
        hold=None,
        nav_to=[],
        join=names["join"],
        ingress=names["ingress"],
        initial=None,
        targets=[first_target],
        split=names["split"],
        refuel=None,
        nav_from=[],
        arrival=names["arrival"],
        divert=None,
        bullseye=names["bullseye"],
        custom_waypoints=[],
        follow_on=[second_ingress, second_target],
    )
    result.pop_points = [
        (result.ingress, waypoint("PULL-UP")),
        (second_ingress, waypoint("PULL-UP 2")),
    ]
    return result


def test_each_target_set_has_its_point_after_its_ingress() -> None:
    names = [w.name for w in layout().iter_waypoints()]
    assert names[names.index("ingress") + 1] == "PULL-UP"
    assert names[names.index("ingress2") + 1] == "PULL-UP 2"
    assert names.index("PULL-UP 2") < names.index("t2")


def test_a_pop_point_can_be_deleted_on_its_own() -> None:
    plan = layout()
    _, second = plan.pop_points[1]
    assert plan.can_delete_waypoint(second)
    assert plan.delete_waypoint(second)
    assert "PULL-UP 2" not in [w.name for w in plan.iter_waypoints()]
    assert [ingress.name for ingress, _ in plan.follow_on_sets()] == ["ingress2"]


def test_old_layouts_have_none() -> None:
    plan = layout()
    del plan.__dict__["pop_points"]
    plan.__setstate__(dict(plan.__dict__))
    assert plan.pop_points == []
