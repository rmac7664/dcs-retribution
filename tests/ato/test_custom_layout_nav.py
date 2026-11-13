from dcs.mapping import Point
from dcs.terrain import Caucasus

from game.ato.flightplans.custom import CustomLayout
from game.ato.flightwaypoint import FlightWaypoint
from game.ato.flightplans.waypointbuilder import WaypointBuilder
from game.utils import feet


def nav(x: float) -> FlightWaypoint:
    return WaypointBuilder.nav(Point(x, 0, Caucasus()), feet(10000))


def test_nav_points_insert_after_the_selected_waypoint() -> None:
    departure, a, b = nav(0), nav(100), nav(300)
    layout = CustomLayout(departure, [a, b])
    assert layout.add_waypoint(departure, a)
    assert layout.custom_waypoints[0].position.x == 50
    assert layout.add_waypoint(a, b)
    assert layout.custom_waypoints[2].position.x == 200
    assert layout.add_waypoint(b, None)
    assert len(layout.custom_waypoints) == 5
    assert not layout.add_waypoint(nav(999), None)


def test_custom_points_can_be_deleted() -> None:
    departure, a = nav(0), nav(100)
    layout = CustomLayout(departure, [a])
    assert layout.can_delete_waypoint(a)
    assert not layout.can_delete_waypoint(departure)
    assert layout.delete_waypoint(a)
    assert layout.custom_waypoints == []


def test_custom_plan_times_its_first_target_not_take_off() -> None:
    from types import SimpleNamespace

    from game.ato.flightplans.custom import CustomFlightPlan
    from game.ato.flightwaypointtype import FlightWaypointType

    departure, ingress, target = nav(0), nav(100), nav(200)
    target.waypoint_type = FlightWaypointType.TARGET_POINT
    layout = CustomLayout(departure, [ingress, target])
    plan = CustomFlightPlan.__new__(CustomFlightPlan)
    plan.layout = layout
    plan.flight = SimpleNamespace()  # type: ignore[assignment]
    assert plan.tot_waypoint is target
