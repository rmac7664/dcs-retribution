"""Puts carrier replenishment ships in the mission.

Each SupplyShip the logistics system has at sea (game/warehouse/state.py) becomes a
single ship that starts about an hour and a half's sailing from its carrier and heads
for it. The warehouse script (dcs_retribution_warehouses.lua) keeps steering it at the
carrier and, when it comes alongside, unloads its stores into the carrier's warehouse.
If it is sunk on the way its cargo is lost.
"""

from __future__ import annotations

import itertools
import logging
from typing import Callable, Optional, TYPE_CHECKING

import dcs.ships
from dcs import Mission
from dcs.mapping import Point
from dcs.unitgroup import ShipGroup

from game.dcs.shipunittype import ShipUnitType
from game.theater import NavalControlPoint
from game.utils import Distance, knots, nautical_miles
from .missiondata import CarrierInfo, MissionData, ReplenishmentShipInfo

if TYPE_CHECKING:
    from game import Game
    from game.unitmap import UnitMap
    from game.warehouse.state import SupplyShip

#: How far out the ship starts: an hour and a half at its cruising speed.
SAIL_SPEED = knots(12)
START_DISTANCE = nautical_miles(18)

#: DCS ship types for the role, most fitting first. The faction's cargo ship is the
#: fallback.
SHIP_PREFERENCE = (dcs.ships.Ship_Tilde_Supply, dcs.ships.ELNYA, dcs.ships.HandyWind)

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
        # Meet the carrier where its launch-and-recovery leg ends.
        rendezvous = carrier_group.points[-1].position
        threat = self.nearest_enemy_base(cp)
        start = start_point(
            rendezvous, threat, START_DISTANCE, self.game.theater.is_in_sea
        )
        if start is None:
            logging.info(
                "Supply: no open water to start %s's replenishment ship from",
                cp.name,
            )
            return

        coalition = cp.coalition
        country = self.mission.country(coalition.faction.country.name)
        name = f"{cp.name} replenishment {next(self.count)}"
        group = self.mission.ship_group(
            country,
            name,
            self.ship_type(coalition.faction.cargo_ship).dcs_unit_type,
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

    def nearest_enemy_base(self, cp: NavalControlPoint) -> Optional[Point]:
        enemy = [
            other.position
            for other in self.game.theater.controlpoints
            if other.captured == cp.captured.opponent
        ]
        if not enemy:
            return None
        return min(enemy, key=lambda p: p.distance_to_point(cp.position))

    @staticmethod
    def ship_type(fallback: ShipUnitType) -> ShipUnitType:
        for dcs_type in SHIP_PREFERENCE:
            for unit_type in ShipUnitType.for_dcs_type(dcs_type):
                return unit_type
        return fallback
