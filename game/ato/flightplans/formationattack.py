from __future__ import annotations

from abc import ABC
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional
from typing import TYPE_CHECKING, TypeVar

from dcs import Point

from game.flightplan import HoldZoneGeometry
from game.theater import MissionTarget, TheaterGroundObject, TheaterUnit
from game.theater import MissionTarget
from game.theater.theatergroundobject import MotorpoolGroundObject
from game.utils import meters, nautical_miles, Speed, feet
from . import alignpoint
from .. import multitarget, prerefuel
from .refuelneed import needs_refuelling
from .flightplan import FlightPlan
from .formation import FormationFlightPlan, FormationLayout
from .ibuilder import IBuilder
from .planningerror import PlanningError
from .waypointbuilder import StrikeTarget, WaypointBuilder
from .. import FlightType
from ..flightwaypoint import FlightWaypoint
from ..flightwaypointtype import FlightWaypointType

if TYPE_CHECKING:
    from ..flight import Flight


#: How far before a follow-on target its ingress is placed, when the leg from the
#: previous target is long enough.
FOLLOW_ON_RUN_IN = nautical_miles(15)


class FormationAttackFlightPlan(FormationFlightPlan, ABC):
    @property
    def package_speed_waypoints(self) -> set[FlightWaypoint]:
        """The waypoints the package flies together, at one speed.

        The run-in from the join to the ingress belongs here: it was the one leg of the
        route every flight priced on its own, so nothing held the package together
        across it and an escort with a faster airframe arrived at the ingress ahead of
        the striker it was escorting.
        """
        # The descent and climb points of a low-level attack sit on the run-in and
        # the egress, so they are flown at the same speed as the legs they split.
        extra = {
            waypoint
            for waypoint in (
                getattr(self.layout, "lineup", None),
                getattr(self.layout, "climb", None),
            )
            if waypoint is not None
        } | {point for _, point in getattr(self.layout, "pop_points", [])}
        return (
            {
                self.layout.join,
                self.layout.ingress,
                self.layout.split,
            }
            | set(self.layout.targets)
            | set(self._follow_on)
            | extra
        )

    @property
    def combat_speed_waypoints(self) -> set[FlightWaypoint]:
        """Package speed is not combat burn: the run-in is still cruise.

        `fuel_rate_to_between_points` charges the combat rate for a leg that ends on
        one of these, so leaving the ingress out keeps the fuel model where it was.
        """
        return self.package_speed_waypoints - {self.layout.ingress}

    def can_delete_waypoint(self, waypoint: FlightWaypoint) -> bool:
        """One of several target points can go; the last one cannot.

        A strike spreads itself over one waypoint per target, and dropping one of them
        is the player saying he does not want that building. Dropping the only one
        leaves an attack with nothing to attack, which is the degrade-to-custom path.
        """
        if self._is_one_of_several_targets(waypoint):
            return True
        if self._is_follow_on(waypoint):
            return True
        return super().can_delete_waypoint(waypoint)

    def delete_waypoint(self, waypoint: FlightWaypoint) -> bool:
        if self._is_one_of_several_targets(waypoint):
            self.layout.targets = [
                target for target in self.layout.targets if target is not waypoint
            ]
            return True
        if self._is_follow_on(waypoint) and self.layout.remove_follow_on(waypoint):
            return True
        return super().delete_waypoint(waypoint)

    @property
    def _follow_on(self) -> list[FlightWaypoint]:
        # Layouts without follow-on targets include ones saved before they existed.
        return getattr(self.layout, "follow_on", [])

    def _is_follow_on(self, waypoint: FlightWaypoint) -> bool:
        # By identity, for the same reason as the target points below.
        return any(waypoint is other for other in self._follow_on)

    def _is_one_of_several_targets(self, waypoint: FlightWaypoint) -> bool:
        # By identity: FlightWaypoint compares by value, and two target points on
        # identical buildings are equal without being the same waypoint.
        targets = self.layout.targets
        return len(targets) > 1 and any(waypoint is target for target in targets)

    def speed_between_waypoints(self, a: FlightWaypoint, b: FlightWaypoint) -> Speed:
        # FlightWaypoint is only comparable by identity, so adding
        # target_area_waypoint to package_speed_waypoints is useless.
        if b.waypoint_type == FlightWaypointType.TARGET_GROUP_LOC:
            # The package only has a formation speed while it contains a
            # formation flight. While the package is being edited (e.g. a flight
            # is mid-deletion) it can transiently have none, so fall back to
            # this flight's own formation speed rather than crashing.
            speed = self.package.formation_speed(self.flight.is_helo)
            if speed is not None:
                return speed
            return self.best_flight_formation_speed
        return super().speed_between_waypoints(a, b)

    @property
    def tot_waypoint(self) -> FlightWaypoint:
        return self.layout.targets[0]

    @property
    def target_area_waypoint(self) -> FlightWaypoint:
        return FlightWaypoint(
            "TARGET AREA",
            FlightWaypointType.TARGET_GROUP_LOC,
            self.package.target.position,
            feet(0),
            "RADIO",
        )

    @property
    def travel_time_to_target(self) -> timedelta:
        """The estimated time between the first waypoint and the target."""
        destination = self.tot_waypoint
        total = timedelta()
        for previous_waypoint, waypoint in self.edges():
            if waypoint == self.tot_waypoint:
                # For anything strike-like the TOT waypoint is the *flight's*
                # mission target, but to synchronize with the rest of the
                # package we need to use the travel time to the same position as
                # the others.
                total += self.travel_time_between_waypoints(
                    previous_waypoint, self.target_area_waypoint
                )
                break
            total += self.travel_time_between_waypoints(previous_waypoint, waypoint)
            if previous_waypoint is self.layout.pre_push_refuel:
                total += prerefuel.tanking_time(self.flight)
        else:
            raise PlanningError(
                f"Did not find destination waypoint {destination} in "
                f"waypoints for {self.flight}"
            )
        return total

    @property
    def join_time(self) -> datetime:
        travel_time = self.total_time_between_waypoints(
            self.layout.join, self.layout.ingress
        )
        return self.ingress_time - travel_time

    def total_time_between_waypoints(
        self, a: FlightWaypoint, b: FlightWaypoint
    ) -> timedelta:
        duration = super().total_time_between_waypoints(a, b)
        if a is self.layout.pre_push_refuel:
            duration += prerefuel.tanking_time(self.flight)
        return duration

    @property
    def push_time(self) -> datetime:
        if self.layout.pre_push_refuel is None or self.layout.hold is None:
            return super().push_time
        points = [
            self.layout.hold,
            self.layout.pre_push_refuel,
            *self.layout.nav_to,
            self.layout.join,
        ]
        duration = sum(
            (
                self.total_time_between_waypoints(a, b)
                for a, b in zip(points, points[1:])
            ),
            timedelta(),
        )
        # The hold's dwell is planned before its departure, not after it.
        return self.join_time - duration + self.hold_duration

    @property
    def split_time(self) -> datetime:
        travel_time_ingress = self.total_time_between_waypoints(
            self.layout.ingress, self.target_area_waypoint
        )
        travel_time_egress = self.total_time_between_waypoints(
            self.target_area_waypoint, self.layout.split
        )
        timedelta_at_target = self._time_over(self.layout.targets)
        if self._follow_on:
            return self._follow_on_schedule()[1]
        return (
            self.ingress_time
            + travel_time_ingress
            + timedelta_at_target
            + travel_time_egress
        )

    @staticmethod
    def _time_over(targets: list[FlightWaypoint]) -> timedelta:
        return timedelta(minutes=0.75 * len(targets))

    def _follow_on_schedule(self) -> tuple[dict[int, datetime], datetime]:
        """Arrival times of the follow-on waypoints, and the split time after them.

        Times are keyed by waypoint identity. Each further target set is flown after
        the one before it: the leg to its ingress, the leg to the set, and the same
        time over each target as the first set is given.
        """
        times: dict[int, datetime] = {}
        clock = self.tot + self._time_over(self.layout.targets)
        previous = self.target_area_waypoint
        for ingress, targets in self.layout.follow_on_sets():
            clock += self.total_time_between_waypoints(previous, ingress)
            times[id(ingress)] = clock
            previous = ingress
            if targets:
                clock += self.total_time_between_waypoints(previous, targets[0])
                for target in targets:
                    times[id(target)] = clock
                clock += self._time_over(targets)
                previous = targets[0]
        clock += self.total_time_between_waypoints(previous, self.layout.split)
        return times, clock

    @property
    def ingress_time(self) -> datetime:
        tot = self.tot
        travel_time = self.total_time_between_waypoints(
            self.layout.ingress, self.target_area_waypoint
        )
        return tot - travel_time

    @property
    def initial_time(self) -> datetime:
        tot = self.tot
        travel_time = self.travel_time_between_waypoints(
            self.layout.initial, self.target_area_waypoint
        )
        return tot - travel_time

    def tot_for_waypoint(self, waypoint: FlightWaypoint) -> datetime | None:
        if waypoint == self.layout.ingress:
            return self.ingress_time
        elif waypoint == self.layout.initial:
            return self.initial_time
        elif self._is_follow_on(waypoint):
            return self._follow_on_schedule()[0].get(id(waypoint))
        elif waypoint in self.layout.targets:
            return self.tot
        elif waypoint is self.layout.pre_push_refuel and self.layout.hold is not None:
            return self.push_time + self.travel_time_between_waypoints(
                self.layout.hold, waypoint
            )
        return super().tot_for_waypoint(waypoint)


@dataclass
class FormationAttackLayout(FormationLayout):
    ingress: FlightWaypoint
    targets: list[FlightWaypoint]
    initial: Optional[FlightWaypoint] = None
    lineup: Optional[FlightWaypoint] = None
    pre_push_refuel: Optional[FlightWaypoint] = None

    #: Further target sets flown after the first one, in flight order. Each set is an
    #: ingress waypoint, which carries the attack tasks for the AI, followed by one
    #: target point per target.
    follow_on: list[FlightWaypoint] = field(default_factory=list, kw_only=True)

    #: The low-level attack profile the route was built for (an AttackProfile
    #: value), or None for a normal attack.
    attack_profile: Optional[str] = field(default=None, kw_only=True)

    #: Height above the ground of the low-level legs, for a low-level profile.
    low_level_height_ft: Optional[int] = field(default=None, kw_only=True)

    #: Where a low-level egress climbs back up, between the last target and the split.
    climb: Optional[FlightWaypoint] = field(default=None, kw_only=True)

    #: The pull-up or pop-up point of each target set, for player flights, paired
    #: with the ingress of the set it belongs to. Flown between the two.
    pop_points: list[tuple[FlightWaypoint, FlightWaypoint]] = field(
        default_factory=list, kw_only=True
    )

    def pop_point_after(self, ingress: FlightWaypoint) -> Optional[FlightWaypoint]:
        for owner, point in getattr(self, "pop_points", []):
            if owner is ingress:
                return point
        return None

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        # Saved before follow-on targets existed.
        self.__dict__.setdefault("follow_on", [])
        # Saved before attack profiles existed.
        self.__dict__.setdefault("attack_profile", None)
        self.__dict__.setdefault("low_level_height_ft", None)
        self.__dict__.setdefault("climb", None)
        self.__dict__.setdefault("pop_points", [])

    @property
    def follow_on_waypoints(self) -> list[FlightWaypoint]:
        return getattr(self, "follow_on", [])

    def is_follow_on(self, waypoint: FlightWaypoint) -> bool:
        # By identity: two waypoints on identical targets compare equal.
        return any(waypoint is other for other in self.follow_on_waypoints)

    def follow_on_sets(self) -> list[tuple[FlightWaypoint, list[FlightWaypoint]]]:
        """The follow-on waypoints as (ingress, target points) per target set."""
        sets: list[tuple[FlightWaypoint, list[FlightWaypoint]]] = []
        for waypoint in self.follow_on_waypoints:
            if waypoint.waypoint_type is FlightWaypointType.TARGET_POINT and sets:
                sets[-1][1].append(waypoint)
            else:
                sets.append((waypoint, []))
        return sets

    def remove_follow_on(self, waypoint: FlightWaypoint) -> bool:
        """Removes a follow-on waypoint. Says whether it was one.

        Removing an ingress removes its whole target set, because the attack tasks
        are on the ingress. Removing the last target point of a set does the same.
        """
        for ingress, targets in self.follow_on_sets():
            if waypoint is ingress or (len(targets) == 1 and waypoint is targets[0]):
                doomed = [ingress, *targets]
            elif any(waypoint is target for target in targets):
                doomed = [waypoint]
            else:
                continue
            self.follow_on = [
                w for w in self.follow_on_waypoints if not any(w is d for d in doomed)
            ]
            return True
        return False

    def _is_pop_point(self, waypoint: FlightWaypoint) -> bool:
        return any(waypoint is point for _, point in getattr(self, "pop_points", []))

    def delete_waypoint(self, waypoint: FlightWaypoint) -> bool:
        if waypoint is self.pre_push_refuel:
            self.pre_push_refuel = None
            return True
        if self._is_pop_point(waypoint):
            self.pop_points = [
                (owner, point)
                for owner, point in self.pop_points
                if point is not waypoint
            ]
            return True
        return super().delete_waypoint(waypoint)

    def can_delete_waypoint(self, waypoint: FlightWaypoint) -> bool:
        return (
            waypoint is self.pre_push_refuel
            or self._is_pop_point(waypoint)
            or super().can_delete_waypoint(waypoint)
        )

    def iter_waypoints(self) -> Iterator[FlightWaypoint]:
        yield self.departure
        if self.hold:
            yield self.hold
        if self.pre_push_refuel is not None:
            yield self.pre_push_refuel
        yield from self.nav_to
        yield self.join
        if self.lineup:
            yield self.lineup
        yield self.ingress
        pop = self.pop_point_after(self.ingress)
        if pop is not None:
            yield pop
        if self.initial is not None:
            yield self.initial
        yield from self.targets
        for waypoint in self.follow_on_waypoints:
            yield waypoint
            pop = self.pop_point_after(waypoint)
            if pop is not None:
                yield pop
        climb = getattr(self, "climb", None)
        if climb is not None:
            yield climb
        yield self.split
        if self.refuel is not None:
            yield self.refuel
        yield from self.nav_from
        yield self.arrival
        if self.divert is not None:
            yield self.divert
        yield self.bullseye
        yield from self.custom_waypoints


FlightPlanT = TypeVar("FlightPlanT", bound=FlightPlan[FormationAttackLayout])
LayoutT = TypeVar("LayoutT", bound=FormationAttackLayout)


class FormationAttackBuilder(IBuilder[FlightPlanT, LayoutT], ABC):
    def _build(
        self,
        ingress_type: FlightWaypointType,
        targets: list[StrikeTarget] | None = None,
    ) -> FormationAttackLayout:
        assert self.package.waypoints is not None
        builder = WaypointBuilder(self.flight, targets)

        target_waypoints: list[FlightWaypoint] = []
        # Mission spec: STRIKE against a motorpool keeps one player-facing
        # waypoint per parked unit, while motorpool BAI and ARMED_RECON use one
        # target-area waypoint. Non-motorpool behavior remains per target.
        is_motorpool = isinstance(self.package.target, MotorpoolGroundObject)
        if targets and (
            not is_motorpool or self.flight.flight_type == FlightType.STRIKE
        ):
            for target in targets:
                target_waypoints.append(
                    self.target_waypoint(self.flight, builder, target)
                )
        elif self.flight.flight_type == FlightType.BAI and isinstance(
            self.package.target, MotorpoolGroundObject
        ):
            # Mission spec: motorpool BAI gets one waypoint for the motorpool
            # total, named the way every other BAI flight names its target
            # waypoint ("ATTACK ..."), not a strike-style area name.
            target_waypoints.append(
                builder.bai_group(
                    StrikeTarget(self.package.target.name, self.package.target)
                )
            )
        else:
            # Targetless missions and motorpool BAI/ARMED_RECON retain the
            # single target-area waypoint.
            target_waypoints.append(
                self.target_area_waypoint(
                    self.flight, self.flight.package.target, builder
                )
            )

        hold = None
        if not self.flight.is_helo:
            hold = builder.hold(self._hold_point())
        join_pos = self.package.waypoints.join
        if self.flight.is_helo:
            join_pos = self.package.waypoints.ingress
            join_pos = WaypointBuilder.perturb(join_pos, feet(500))
        join = builder.join(join_pos)
        split = builder.split(self._get_split())
        refuel = self._build_refuel(builder)
        pre_refuel = prerefuel.build_stop(self.flight, join.position) if hold else None

        ingress = builder.ingress(
            ingress_type, self.package.waypoints.ingress, self.package.target
        )

        initial = None
        if ingress_type == FlightWaypointType.INGRESS_SEAD:
            # The SEAD Search anchor is a stand-off loiter for HARM shooters; a decoy
            # stand-off run (release_at_ingress) releases at the ingress, so the anchor
            # only drags the flight past its release point -- drop it for those.
            if not self.flight.release_at_ingress:
                initial = builder.sead_search(self.package.target)
        elif ingress_type == FlightWaypointType.INGRESS_SEAD_SWEEP:
            initial = builder.sead_sweep(self.package.target)

        # A strike used to get a line-up waypoint ten miles before the ingress. The
        # ingress is already on the run-in, so it said nothing the ingress did not.
        lineup = None

        is_helo = self.flight.is_helo
        ingress_egress_altitude = builder.get_combat_altitude
        use_agl_ingress_egress = is_helo

        layout = FormationAttackLayout(
            departure=builder.takeoff(self.flight.departure),
            hold=hold,
            pre_push_refuel=pre_refuel,
            nav_to=builder.nav_path(
                (
                    pre_refuel.position
                    if pre_refuel
                    else (hold.position if hold else self.flight.departure.position)
                ),
                join.position if join else ingress.position,
                ingress_egress_altitude,
                use_agl_ingress_egress,
            ),
            join=join,
            lineup=lineup,
            ingress=ingress,
            initial=initial,
            targets=target_waypoints,
            follow_on=self._follow_on_waypoints(ingress_type),
            split=split,
            refuel=refuel,
            nav_from=builder.nav_path(
                refuel.position if refuel else split.position,
                self.flight.arrival.position,
                ingress_egress_altitude,
                use_agl_ingress_egress,
            ),
            arrival=builder.land(self.flight.arrival),
            divert=builder.divert(self.flight.divert),
            bullseye=builder.bullseye(),
            custom_waypoints=list(),
        )
        if ingress_type in (
            FlightWaypointType.INGRESS_STRIKE,
            FlightWaypointType.INGRESS_BAI,
        ):
            self._apply_attack_profile(layout, builder)
        return layout

    def _apply_attack_profile(
        self, layout: FormationAttackLayout, builder: WaypointBuilder
    ) -> None:
        """Brings the run-in, the attack and the egress down low, if the flight asked.

        The low legs are heights above the ground, which the AI follows over hills.
        The height is raised where the ground is rough or its height unknown, and an
        AI flight whose run-in crosses mountains keeps the normal profile.
        """
        from game.ato import attackprofile
        from game.ato.attackprofile import AttackProfile

        profile = attackprofile.flown_profile(self.flight)
        if not profile.low_level:
            return
        route = [layout.ingress, *layout.targets, *layout.follow_on_waypoints]
        legs = [(a.position, b.position) for a, b in zip(route, route[1:])]
        # The low stretches either side of the attack, as far as they go.
        legs.append(
            (
                layout.ingress.position,
                layout.ingress.position.point_from_heading(
                    layout.ingress.position.heading_between_point(layout.join.position),
                    min(
                        attackprofile.LOW_RUN_IN.meters,
                        layout.ingress.position.distance_to_point(layout.join.position),
                    ),
                ),
            )
        )
        legs.append((route[-1].position, layout.split.position))
        terrain = attackprofile.terrain_along(legs)
        if terrain.mountainous and self.flight.client_count == 0:
            return
        height = attackprofile.low_height(terrain)
        low = [layout.ingress]
        low += [
            waypoint
            for waypoint in layout.follow_on_waypoints
            if waypoint.waypoint_type is not FlightWaypointType.TARGET_POINT
        ]

        # Down from the join only for the last stretch of the run-in, and back up
        # soon after the last target: a low leg is slow on fuel and the AI starts
        # its descent as soon as it turns for a low waypoint.
        run_in = meters(layout.join.position.distance_to_point(layout.ingress.position))
        if run_in.meters > attackprofile.LOW_RUN_IN.meters + nautical_miles(5).meters:
            heading = layout.ingress.position.heading_between_point(
                layout.join.position
            )
            layout.lineup = FlightWaypoint(
                "DESCENT",
                FlightWaypointType.NAV,
                layout.ingress.position.point_from_heading(
                    heading, attackprofile.LOW_RUN_IN.meters
                ),
                layout.join.alt,
                layout.join.alt_type,
                description="Descend to low level",
                pretty_name="Descent",
            )
        last = (
            layout.follow_on_waypoints[-1]
            if layout.follow_on_waypoints
            else layout.targets[-1]
        )
        egress = meters(last.position.distance_to_point(layout.split.position))
        if egress.meters > attackprofile.LOW_EGRESS.meters + nautical_miles(5).meters:
            heading = last.position.heading_between_point(layout.split.position)
            layout.climb = FlightWaypoint(
                "CLIMB",
                FlightWaypointType.NAV,
                last.position.point_from_heading(
                    heading, attackprofile.LOW_EGRESS.meters
                ),
                height,
                "RADIO",
                description="Climb out from low level",
                pretty_name="Climb",
            )
        else:
            low.append(layout.split)

        for waypoint in low:
            waypoint.alt = height
            waypoint.alt_type = "RADIO"
        layout.attack_profile = profile.value
        layout.low_level_height_ft = round(height.feet)

        if self.flight.client_count == 0:
            return
        if profile is AttackProfile.LOFT:
            distance = attackprofile.loft_solution(height).pull_up
            name, description = "PULL-UP", "Pull up for the loft"
        elif profile is AttackProfile.POPUP:
            distance = attackprofile.POPUP_DISTANCE
            name, description = "POP-UP", "Pop up for the dive"
        else:
            return
        # One for every target set: the main one and each additional target.
        sets = [(layout.ingress, layout.targets), *layout.follow_on_sets()]
        for ingress, targets in sets:
            if not targets:
                continue
            target = targets[0].position
            to_target = target.distance_to_point(ingress.position)
            if to_target <= distance.meters:
                continue
            heading = target.heading_between_point(ingress.position)
            layout.pop_points.append(
                (
                    ingress,
                    FlightWaypoint(
                        name,
                        FlightWaypointType.NAV,
                        target.point_from_heading(heading, distance.meters),
                        height,
                        "RADIO",
                        description=description,
                        pretty_name=name,
                        only_for_player=True,
                    ),
                )
            )

    def _follow_on_waypoints(
        self, ingress_type: FlightWaypointType
    ) -> list[FlightWaypoint]:
        """Waypoints for the package's additional strike targets, in order.

        Only strike flights fly them. Each additional target gets its own ingress,
        on the line from the previous target, and one target point per live unit.
        A target with nothing left alive is skipped.
        """
        if self.flight.flight_type is not FlightType.STRIKE:
            return []
        if ingress_type is not FlightWaypointType.INGRESS_STRIKE:
            return []
        waypoints: list[FlightWaypoint] = []
        previous = self.package.target.position
        for location in multitarget.live_additional_targets(self.package):
            targets = self.strike_targets_for(location)
            builder = WaypointBuilder(self.flight, targets)
            leg = location.position.distance_to_point(previous)
            run_in = min(leg / 2, FOLLOW_ON_RUN_IN.meters)
            position = location.position.point_from_heading(
                location.position.heading_between_point(previous), run_in
            )
            waypoints.append(builder.ingress(ingress_type, position, location))
            waypoints.extend(
                self.target_waypoint(self.flight, builder, target) for target in targets
            )
            previous = location.position
        return waypoints

    def _target_waypoints(
        self, builder: WaypointBuilder, targets: list[StrikeTarget] | None
    ) -> list[FlightWaypoint]:
        # `targets` can be an *empty* list (not just None) -- e.g. a Strike/DEAD/
        # SEAD against an objective whose units are all already destroyed, since
        # strike_targets_for() only lists live units. Fall back to a single
        # target-area waypoint in that case so the layout always has at least one
        # target (tot_waypoint and the timing math index targets[0]).
        if targets:
            return [
                self.target_waypoint(self.flight, builder, target) for target in targets
            ]
        return [
            self.target_area_waypoint(self.flight, self.flight.package.target, builder)
        ]

    def _build_refuel(self, builder: WaypointBuilder) -> Optional[FlightWaypoint]:
        # It used to be enough that the flight was not a helicopter and the faction
        # owned a tanker: every strike, escort and sweep got a refuelling waypoint,
        # whether it could fly the plan twice over or not.
        if not self.package.waypoints:
            return None
        settings = self.flight.coalition.game.settings
        # The bands the rest of this layout is about to be built at, so the estimate
        # is charged at the altitudes the flight will really be flown at.
        if not needs_refuelling(
            self.flight,
            self.package,
            settings,
            builder.get_cruise_altitude,
            builder.get_combat_altitude,
        ):
            return None
        return builder.refuel(self.package.waypoints.refuel)

    @property
    def primary_flight_is_air_assault(self) -> bool:
        if self.flight is self.package.primary_flight:
            # Can't call self.package.primary_flight.flight_plan here
            # because the flight-plan wasn't created yet.
            # Calling the fligh_plan property would result in infinite recursion
            return self.flight.flight_type == FlightType.AIR_ASSAULT
        else:
            assert self.package.primary_flight is not None
            fp = self.package.primary_flight.flight_plan
            return fp.is_airassault

    @staticmethod
    def strike_targets_for(location: TheaterGroundObject) -> list[StrikeTarget]:
        """One StrikeTarget per individual unit of a ground objective.

        This is the same per-unit list the kneeboard target page renders (with
        coordinates). Mission types whose kneeboard lists targets with
        coordinates (Strike, DEAD, SEAD) pass this to ``_build`` so each listed
        target also gets its own TARGET_POINT waypoint in the aircraft, making it
        trivial to designate with TOO.
        """
        return FormationAttackBuilder._targets_from(location.strike_targets)

    @staticmethod
    def sead_targets_for(location: TheaterGroundObject) -> list[StrikeTarget]:
        """As above, but only the emitters: see `TheaterGroundObject.sead_targets`."""
        return FormationAttackBuilder._targets_from(location.sead_targets)

    @staticmethod
    def _targets_from(units: list[TheaterUnit]) -> list[StrikeTarget]:
        targets: list[StrikeTarget] = []
        for idx, unit in enumerate(units):
            # Vehicles read better as their type ("Patriot ln #3"); statics carry the
            # objective in their name ("Factory Zaragoza-2"), which beats the DCS type.
            name = unit.type.id if unit.unit_type is not None else unit.name
            targets.append(StrikeTarget(f"{name} #{idx}", unit))
        return targets

    @staticmethod
    def target_waypoint(
        flight: Flight, builder: WaypointBuilder, target: StrikeTarget
    ) -> FlightWaypoint:
        if flight.flight_type in {FlightType.ANTISHIP, FlightType.BAI}:
            return builder.bai_group(target)
        elif flight.flight_type == FlightType.DEAD:
            return builder.dead_point(target)
        elif flight.flight_type in {FlightType.SEAD, FlightType.SEAD_SWEEP}:
            return builder.sead_point(target)
        else:
            return builder.strike_point(target)

    @staticmethod
    def target_area_waypoint(
        flight: Flight, location: MissionTarget, builder: WaypointBuilder
    ) -> FlightWaypoint:
        if flight.flight_type == FlightType.DEAD:
            return builder.dead_area(location)
        elif flight.flight_type == FlightType.SEAD:
            return builder.sead_area(location)
        elif flight.flight_type == FlightType.OCA_AIRCRAFT:
            return builder.oca_strike_area(location)
        elif flight.flight_type == FlightType.ARMED_RECON:
            return builder.armed_recon_area(location)
        else:
            return builder.strike_area(location)

    def _hold_point(self) -> Point:
        assert self.package.waypoints is not None
        aligned = alignpoint.hold_point(self.flight, self.coalition.doctrine)
        if aligned is not None:
            return aligned
        origin = self.flight.departure.position
        target = self.package.target.position
        join = self.package.waypoints.join
        ip = self.package.waypoints.ingress
        return HoldZoneGeometry(
            target, origin, ip, join, self.coalition, self.theater
        ).find_best_hold_point()

    def _get_split(self) -> Point:
        assert self.package.waypoints is not None
        assert self.package.primary_flight is not None
        primary = self.package.primary_flight
        if primary.is_helo:
            return primary.arrival.position
        split = self.package.waypoints.split
        further = multitarget.live_additional_targets(self.package)
        if not further or primary.flight_type is not FlightType.STRIKE:
            return split
        # The package split is placed for the main target. With further targets the
        # package leaves from the last one instead, the same distance out on the
        # way home, so the route does not double back.
        last = further[-1].position
        home = primary.arrival.position
        reach = min(
            self.package.target.position.distance_to_point(split),
            last.distance_to_point(home) / 2,
        )
        return last.point_from_heading(last.heading_between_point(home), reach)
