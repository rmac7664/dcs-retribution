from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Type

from .flightplan import FlightPlan, Layout
from .ibuilder import IBuilder
from .waypointbuilder import WaypointBuilder
from .. import Flight
from ..flightwaypointtype import FlightWaypointType

if TYPE_CHECKING:
    from ..flightwaypoint import FlightWaypoint


@dataclass
class CustomLayout(Layout):
    def iter_waypoints(self) -> Iterator[FlightWaypoint]:
        yield self.departure
        yield from self.custom_waypoints

    def add_waypoint(
        self, wpt: FlightWaypoint, next_wpt: FlightWaypoint | None
    ) -> bool:
        """Inserts a NAV point halfway to the next waypoint (or after the last)."""
        from .standard import StandardLayout

        new_wpt = StandardLayout.get_midpoint(wpt, next_wpt)
        if wpt is self.departure:
            self.custom_waypoints.insert(0, new_wpt)
            return True
        if wpt in self.custom_waypoints:
            self.custom_waypoints.insert(self.custom_waypoints.index(wpt) + 1, new_wpt)
            return True
        return False

    def can_delete_waypoint(self, waypoint: FlightWaypoint) -> bool:
        return waypoint in self.custom_waypoints

    def delete_waypoint(self, waypoint: FlightWaypoint) -> bool:
        if waypoint in self.custom_waypoints:
            self.custom_waypoints.remove(waypoint)
            return True
        return False


class CustomFlightPlan(FlightPlan[CustomLayout]):
    @staticmethod
    def builder_type() -> Type[Builder]:
        return Builder

    @property
    def is_custom(self) -> bool:
        return True

    @property
    def tot_waypoint(self) -> FlightWaypoint:
        target_types = (
            FlightWaypointType.PATROL_TRACK,
            FlightWaypointType.TARGET_GROUP_LOC,
            FlightWaypointType.TARGET_POINT,
            FlightWaypointType.TARGET_SHIP,
        )
        for waypoint in self.waypoints:
            if waypoint in target_types:
                return waypoint
        return self.layout.departure

    def tot_for_waypoint(self, waypoint: FlightWaypoint) -> datetime | None:
        if waypoint == self.tot_waypoint:
            return self.tot
        return None

    def depart_time_for_waypoint(self, waypoint: FlightWaypoint) -> datetime | None:
        return None

    @property
    def mission_begin_on_station_time(self) -> datetime | None:
        return None

    @property
    def mission_departure_time(self) -> datetime:
        return self.package.time_over_target

    @property
    def landing_time(self) -> datetime:
        arrival = (
            self.layout.custom_waypoints[-1]
            if self.layout.custom_waypoints
            else self.layout.departure
        )
        return_time = self.total_time_between_waypoints(self.tot_waypoint, arrival)
        return self.mission_departure_time + return_time


class Builder(IBuilder[CustomFlightPlan, CustomLayout]):
    def __init__(
        self, flight: Flight, waypoints: list[FlightWaypoint] | None = None
    ) -> None:
        super().__init__(flight)
        if waypoints is None:
            waypoints = []
        self.waypoints = waypoints

    def layout(self) -> CustomLayout:
        builder = WaypointBuilder(self.flight)
        return CustomLayout(builder.takeoff(self.flight.departure), self.waypoints)

    def build(self, dump_debug_info: bool = False) -> CustomFlightPlan:
        return CustomFlightPlan(self.flight, self.layout())
