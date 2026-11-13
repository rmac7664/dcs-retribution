"""Additional strike targets: package data, route waypoints, timing and AI tasks."""

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from dcs import Point
from dcs.task import Expend
from dcs.terrain.caucasus import Caucasus

from game.ato import multitarget
from game.ato.flightplans.formationattack import FormationAttackLayout
from game.ato.flightplans.strike import StrikeFlightPlan
from game.ato.flighttype import FlightType
from game.ato.flightwaypoint import FlightWaypoint
from game.ato.flightwaypointtype import FlightWaypointType
from game.ato.package import Package
from game.missiongenerator.aircraft.waypoints.strikeingress import (
    StrikeIngressBuilder,
)
from game.theater.theatergroundobject import TheaterGroundObject

TERRAIN = Caucasus()
T0 = datetime(2026, 4, 24, 12, 0)


def waypoint(name: str, kind: FlightWaypointType, x: float = 0) -> FlightWaypoint:
    return FlightWaypoint(name, kind, Point(x, 0, TERRAIN))


def ingress(name: str, x: float = 0) -> FlightWaypoint:
    return waypoint(name, FlightWaypointType.INGRESS_STRIKE, x)


def point(name: str, x: float = 0) -> FlightWaypoint:
    return waypoint(name, FlightWaypointType.TARGET_POINT, x)


def layout(follow_on: list[FlightWaypoint]) -> FormationAttackLayout:
    nav = waypoint("x", FlightWaypointType.NAV)
    return FormationAttackLayout(
        departure=nav,
        arrival=nav,
        divert=None,
        bullseye=nav,
        nav_to=[],
        nav_from=[],
        custom_waypoints=[],
        hold=None,
        join=waypoint("JOIN", FlightWaypointType.JOIN),
        split=waypoint("SPLIT", FlightWaypointType.SPLIT, 90),
        refuel=None,
        ingress=ingress("INGRESS"),
        targets=[point("first")],
        follow_on=follow_on,
    )


def test_follow_on_waypoints_fly_between_the_targets_and_the_split() -> None:
    second = [ingress("i2", 10), point("a", 20), point("b", 20)]
    third = [ingress("i3", 30), point("c", 40)]
    plan = layout(second + third)

    names = [w.name for w in plan.iter_waypoints()]
    assert names[names.index("first") + 1 : names.index("SPLIT")] == [
        "i2",
        "a",
        "b",
        "i3",
        "c",
    ]
    assert [(i.name, [t.name for t in ts]) for i, ts in plan.follow_on_sets()] == [
        ("i2", ["a", "b"]),
        ("i3", ["c"]),
    ]


def test_removing_follow_on_waypoints() -> None:
    i2, a, b, i3, c = ingress("i2"), point("a"), point("b"), ingress("i3"), point("c")
    plan = layout([i2, a, b, i3, c])

    assert not plan.remove_follow_on(plan.targets[0])
    # One of two target points goes on its own.
    assert plan.remove_follow_on(a)
    assert plan.follow_on == [i2, b, i3, c]
    # The last target point of a set takes the set with it.
    assert plan.remove_follow_on(c)
    assert plan.follow_on == [i2, b]
    # So does the ingress, which carries the attack tasks.
    assert plan.remove_follow_on(i2)
    assert plan.follow_on == []


def test_routes_saved_before_follow_on_targets_load() -> None:
    plan = layout([])
    state = dict(plan.__dict__)
    del state["follow_on"]
    restored = FormationAttackLayout.__new__(FormationAttackLayout)
    restored.__setstate__(state)
    assert restored.follow_on == []
    assert not restored.is_follow_on(restored.ingress)


class _Plan(StrikeFlightPlan):
    """A strike plan that flies one metre per second and is over target at T0."""

    def __init__(self, plan_layout: FormationAttackLayout) -> None:
        self.layout = plan_layout

    @property
    def tot(self) -> datetime:
        return T0

    @property
    def target_area_waypoint(self) -> FlightWaypoint:
        return point("area", 0)

    def total_time_between_waypoints(
        self, a: FlightWaypoint, b: FlightWaypoint
    ) -> timedelta:
        return timedelta(seconds=abs(b.position.x - a.position.x))


def test_follow_on_times_run_on_from_the_first_target() -> None:
    i2, a, b = ingress("i2", 10), point("a", 20), point("b", 20)
    i3, c = ingress("i3", 30), point("c", 40)
    plan = _Plan(layout([i2, a, b, i3, c]))
    over_target = timedelta(seconds=45)

    assert plan.tot_for_waypoint(i2) == T0 + over_target + timedelta(seconds=10)
    second = T0 + over_target + timedelta(seconds=20)
    assert plan.tot_for_waypoint(a) == plan.tot_for_waypoint(b) == second
    assert plan.tot_for_waypoint(i3) == second + 2 * over_target + timedelta(seconds=10)
    third = second + 2 * over_target + timedelta(seconds=20)
    assert plan.tot_for_waypoint(c) == third
    # The package holds together until it leaves the last target.
    assert plan.split_time == third + over_target + timedelta(seconds=50)


class _Target(TheaterGroundObject):
    """A ground object with just what target selection reads."""

    def __init__(self, name: str, x: float, friendly: bool = False, alive: int = 1):
        self.name = name
        self.position = Point(x, 0, TERRAIN)
        self._friendly = friendly
        self._alive = alive

    def is_friendly(self, to_player: Any) -> bool:
        return self._friendly

    @property
    def is_dead(self) -> bool:
        return self._alive == 0

    @property
    def strike_targets(self) -> list[Any]:
        return [object()] * self._alive

    def mission_types(self, for_player: Any) -> Any:
        yield FlightType.STRIKE

    @property
    def symbol_set_and_entity(self) -> Any:
        raise NotImplementedError


def _flight(task: FlightType, replanned: list[Any]) -> Any:
    flight = SimpleNamespace(flight_type=task, coalition=SimpleNamespace(player="blue"))
    flight.recreate_flight_plan = lambda: replanned.append(flight)
    return flight


def _package(target: Any, flights: list[Any]) -> Any:
    return SimpleNamespace(target=target, flights=flights, additional_targets=[])


def test_candidates_are_live_enemy_targets_nearest_the_last_one_first() -> None:
    main = _Target("main", 0)
    near, far = _Target("near", 10), _Target("far", 50)
    package = _package(main, [_flight(FlightType.STRIKE, [])])
    game: Any = SimpleNamespace(
        theater=SimpleNamespace(
            ground_objects=[
                far,
                main,
                _Target("ours", 5, friendly=True),
                _Target("rubble", 5, alive=0),
                near,
            ]
        )
    )

    assert multitarget.candidates(package, game) == [near, far]
    package.additional_targets = [far]
    assert multitarget.candidates(package, game) == [near]


def test_setting_targets_replans_the_whole_package_once_each() -> None:
    main, second = _Target("main", 0), _Target("second", 10)
    replanned: list[Any] = []
    strike = _flight(FlightType.STRIKE, replanned)
    escort = _flight(FlightType.ESCORT, replanned)
    package = _package(main, [strike, escort])

    assert multitarget.supported(package)
    multitarget.set_additional_targets(package, [second, main, second])

    assert package.additional_targets == [second]
    assert replanned == [strike, escort]
    assert not multitarget.supported(_package(main, [escort]))


def test_dead_additional_targets_are_not_flown() -> None:
    package = _package(_Target("main", 0), [_flight(FlightType.STRIKE, [])])
    alive, dead = _Target("alive", 10), _Target("dead", 20, alive=0)
    package.additional_targets = [dead, alive]
    assert multitarget.live_additional_targets(package) == [alive]


def test_packages_saved_before_additional_targets_load() -> None:
    package = Package.__new__(Package)
    package.__setstate__({"target": object(), "flights": []})
    assert package.additional_targets == []


def _strike_builder(follow_on_sets: int) -> StrikeIngressBuilder:
    builder = StrikeIngressBuilder.__new__(StrikeIngressBuilder)
    sets = [(ingress(f"i{n}"), [point(f"t{n}")]) for n in range(follow_on_sets)]
    builder.flight = SimpleNamespace(  # type: ignore[assignment]
        flight_plan=SimpleNamespace(
            layout=SimpleNamespace(
                follow_on_sets=lambda: sets,
                is_follow_on=lambda w: any(w is i for i, _ in sets),
            )
        )
    )
    return builder


def test_each_target_set_is_left_its_share_of_the_weapons() -> None:
    assert _strike_builder(0).share_of_payload() is Expend.All
    assert _strike_builder(1).share_of_payload() is Expend.Half
    assert _strike_builder(2).share_of_payload() is Expend.Quarter
    assert _strike_builder(2).target_sets == 3
