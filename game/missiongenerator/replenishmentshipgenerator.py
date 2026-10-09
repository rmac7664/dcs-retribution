"""Puts carrier replenishment ships in the mission.

Each SupplyShip the logistics system has at sea (game/warehouse/state.py) becomes a
single ship that starts far out ("Replenishment ships start", 70 nm by default),
coming from the direction of its side's main supply base, and sails in at SAIL_SPEED
to where the carrier will be when it gets there. On final approach the warehouse script
(dcs_retribution_warehouses.lua) steers it at the carrier and, when it comes alongside,
unloads its stores into the carrier's warehouse. If the mission ends first, its cargo
arrives at the end of the turn; if it is sunk on the way its cargo is lost.
"""

from __future__ import annotations

import itertools
import logging
from typing import Any, Callable, Optional, TYPE_CHECKING

import dcs.ships
from dcs import Mission
from dcs.mapping import Point
from dcs.point import MovingPoint
from dcs.unitgroup import ShipGroup

from game.dcs.shipunittype import ShipUnitType
from game.theater import NavalControlPoint
from game.utils import Distance, knots, nautical_miles
from .missiondata import CarrierInfo, MissionData, ReplenishmentShipInfo

if TYPE_CHECKING:
    from game import Game
    from game.unitmap import UnitMap
    from game.warehouse.state import SupplyShip

#: A fleet oiler's transit speed.
SAIL_SPEED = knots(18)


def start_distance(settings: Any) -> Distance:
    return nautical_miles(getattr(settings, "logistics_replenishment_start_nm", 70))


def sailing_seconds(distance: Distance) -> float:
    """Time to sail `distance` at SAIL_SPEED."""
    return distance.meters / SAIL_SPEED.meters_per_second


#: DCS ship types for the role, most fitting first; the faction's cargo ship is the
#: fallback. Red uses the Soviet Project 160 fleet oiler, so the sides look different.
SHIP_PREFERENCE = {
    "BLUE": (dcs.ships.Ship_Tilde_Supply, dcs.ships.HandyWind),
    "RED": (dcs.ships.ELNYA, dcs.ships.Dry_cargo_ship_2),
}


def position_after(points: list[MovingPoint], seconds: float) -> Point:
    """Where a group following `points` will be after `seconds`.

    Each leg is sailed at the speed set on the waypoint it heads for. A group that
    has run out of route (or is told to stop) stays at its last point.
    """
    here = points[0].position
    remaining = seconds
    for waypoint in points[1:]:
        leg = here.distance_to_point(waypoint.position)
        speed = waypoint.speed  # m/s
        if leg <= 0:
            continue
        if speed <= 0:
            return here
        if remaining * speed < leg:
            return here.point_from_heading(
                here.heading_between_point(waypoint.position), remaining * speed
            )
        remaining -= leg / speed
        here = waypoint.position
    return here


#: Bearings tried (relative to "directly away from the enemy") to find open water.
BEARING_OFFSETS = (0, 30, -30, 60, -60, 90, -90, 120, -120, 150, -150, 180)


def start_point(
    rendezvous: Point,
    threat: Optional[Point],
    distance: Distance,
    is_in_sea: Callable[[Point], bool],
) -> Optional[Point]:
    """Where a replenishment ship starts, or None if there's no open water.

    The ship comes from the side away from the enemy, swinging round in 30° steps
    until both its start point and the half-way point are at sea. If nothing fits,
    it tries again from closer in.
    """
    away = 0.0 if threat is None else threat.heading_between_point(rendezvous)
    for scale in (1.0, 0.6, 0.3):
        for offset in BEARING_OFFSETS:
            start = rendezvous.point_from_heading(
                (away + offset) % 360, distance.meters * scale
            )
            if is_in_sea(start) and is_in_sea(start.midpoint(rendezvous)):
                return start
    return None


def origin_bearing_point(
    game: Game, cp: NavalControlPoint, rendezvous: Point
) -> Optional[Point]:
    """A point opposite the side's main supply base, as seen from `rendezvous`.

    start_point() places the ship away from the point it is given, so passing this
    makes the ship come from the main base's direction.
    """
    from game.warehouse.supply import main_base

    base = main_base(game, cp.captured)
    if base is None or base.position.distance_to_point(rendezvous) < 1:
        return None
    toward = rendezvous.heading_between_point(base.position)
    return rendezvous.point_from_heading((toward + 180) % 360, 1000)


def nearest_enemy_base(game: Game, cp: NavalControlPoint) -> Optional[Point]:
    enemy = [
        other.position
        for other in game.theater.controlpoints
        if other.captured == cp.captured.opponent
    ]
    if not enemy:
        return None
    return min(enemy, key=lambda p: p.distance_to_point(cp.position))


def ship_can_reach(game: Game, cp: NavalControlPoint) -> bool:
    """True if a replenishment ship can sail to `cp` through open water.

    A carrier or LHA tucked in close to a coast may have no open water to start a
    ship from (see start_point); such a ship can't be resupplied by sea, only by air.
    """
    rendezvous = cp.position
    away = origin_bearing_point(game, cp, rendezvous) or nearest_enemy_base(game, cp)
    return (
        start_point(
            rendezvous, away, start_distance(game.settings), game.theater.is_in_sea
        )
        is not None
    )


class ReplenishmentShipGenerator:
    def __init__(
        self,
        mission: Mission,
        game: Game,
        unit_map: UnitMap,
        mission_data: MissionData,
    ) -> None:
        self.mission = mission
        self.game = game
        self.unit_map = unit_map
        self.mission_data = mission_data
        self.count = itertools.count(1)

    def generate(self) -> None:
        settings = self.game.settings
        if not settings.logistics_enabled or settings.perf_disable_replenishment_ships:
            return
        carriers = {
            info.ship_group.units[0].id: info
            for info in self.mission_data.carriers
            if info.ship_group.units
        }
        for ship in self.game.warehouse_logistics.supply_ships:
            try:
                self.generate_ship(ship, carriers)
            except Exception:
                # A ship that can't be placed stays at sea off-map and still arrives
                # at the end of the turn; never let it break mission generation.
                logging.exception(
                    "Could not put the replenishment ship for %s in the mission",
                    ship.carrier_name,
                )

    def generate_ship(self, ship: SupplyShip, carriers: dict[int, CarrierInfo]) -> None:
        try:
            cp = self.game.theater.find_control_point_by_id(ship.carrier_id)
        except KeyError:
            return
        if not isinstance(cp, NavalControlPoint) or not cp.runway_is_operational():
            return
        info = carriers.get(getattr(cp, "carrier_id", None) or -1)
        if info is None:
            return
        carrier_group: ShipGroup = info.ship_group
        # Meet the carrier where it will be by the time the ship gets there.
        distance = start_distance(self.game.settings)
        rendezvous = position_after(
            list(carrier_group.points), sailing_seconds(distance)
        )
        start = start_point(
            rendezvous,
            self.away_from(cp, rendezvous),
            distance,
            self.game.theater.is_in_sea,
        )
        if start is None:
            logging.info(
                "Supply: no open water to start %s's replenishment ship from",
                cp.name,
            )
            return

        coalition = cp.coalition
        country = self.mission.country(coalition.faction.country.name)
        name = (
            getattr(ship, "name", "") or f"{cp.name} replenishment {next(self.count)}"
        )
        group = self.mission.ship_group(
            country,
            name,
            self.ship_type(ship.side, coalition.faction.cargo_ship).dcs_unit_type,
            position=start,
            group_size=1,
        )
        group.add_waypoint(rendezvous, speed=SAIL_SPEED.kph)
        self.unit_map.add_replenishment_ship(group, ship)
        self.mission_data.replenishment_ships.append(
            ReplenishmentShipInfo(
                group_name=str(group.name),
                unit_name=str(group.units[0].name),
                carrier_unit_name=info.unit_name,
                carrier_cp_id=str(cp.id),
                munitions=dict(ship.munitions),
                fuel_kg=ship.fuel_kg,
            )
        )
        logging.info("Supply: replenishment ship %s sails for %s", name, cp.name)

    def away_from(self, cp: NavalControlPoint, rendezvous: Point) -> Optional[Point]:
        """The point the ship sails away from: it comes from the main supply base."""
        return origin_bearing_point(self.game, cp, rendezvous) or (
            nearest_enemy_base(self.game, cp)
        )

    @staticmethod
    def ship_type(side: str, fallback: ShipUnitType) -> ShipUnitType:
        for dcs_type in SHIP_PREFERENCE.get(side, ()):
            for unit_type in ShipUnitType.for_dcs_type(dcs_type):
                return unit_type
        return fallback
