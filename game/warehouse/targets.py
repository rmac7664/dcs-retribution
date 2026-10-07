"""Replenishment ships as targets the AI can plan anti-ship strikes against."""

from __future__ import annotations

from typing import Iterator, Optional, TYPE_CHECKING

from dcs.mapping import Point

from game.theater.controlpoint import NavalControlPoint
from game.theater.missiontarget import MissionTarget
from game.utils import nautical_miles

if TYPE_CHECKING:
    from game import Game
    from game.ato import FlightType
    from game.theater.player import Player
    from .state import SupplyShip

#: How far from its carrier a replenishment ship is expected when strikes are planned:
#: it starts 18 nm out and closes on the carrier during the mission.
EXPECTED_DISTANCE = nautical_miles(15)


class ReplenishmentShipTarget(MissionTarget):
    """An enemy replenishment ship heading for its carrier this mission."""

    def __init__(
        self, ship: SupplyShip, carrier: NavalControlPoint, position: Point
    ) -> None:
        super().__init__(f"Replenishment ship for {carrier.name}", position)
        self.ship = ship
        self.carrier = carrier

    @property
    def group_name(self) -> str:
        """The ship's group in the mission (see ReplenishmentShipGenerator)."""
        return ship_group_name(self.ship)

    def is_friendly(self, to_player: Player) -> bool:
        return self.carrier.captured == to_player

    def mission_types(self, for_player: Player) -> Iterator[FlightType]:
        from game.ato import FlightType

        if self.is_friendly(for_player):
            return
        yield FlightType.ANTISHIP
        yield from super().mission_types(for_player)


def ship_group_name(ship: SupplyShip) -> str:
    return getattr(ship, "name", "") or f"{ship.carrier_name} replenishment"


def expected_position(game: Game, carrier: NavalControlPoint) -> Point:
    """Where the ship should be: on the carrier's far side from the enemy."""
    from game.missiongenerator.replenishmentshipgenerator import start_point

    enemy = [
        cp.position
        for cp in game.theater.controlpoints
        if cp.captured == carrier.captured.opponent
    ]
    threat: Optional[Point] = (
        min(enemy, key=carrier.position.distance_to_point) if enemy else None
    )
    found = start_point(
        carrier.position, threat, EXPECTED_DISTANCE, game.theater.is_in_sea
    )
    return found if found is not None else carrier.position


def enemy_replenishment_ships(
    game: Game, player: Player
) -> Iterator[ReplenishmentShipTarget]:
    """The opponent's replenishment ships that will be in the next mission."""
    settings = game.settings
    if not settings.logistics_enabled or settings.perf_disable_replenishment_ships:
        return
    for ship in game.warehouse_logistics.supply_ships:
        if ship.side != player.opponent.name or ship.is_empty():
            continue
        try:
            carrier = game.theater.find_control_point_by_id(ship.carrier_id)
        except KeyError:
            continue
        if not isinstance(carrier, NavalControlPoint):
            continue
        if not carrier.runway_is_operational() or carrier.captured != player.opponent:
            continue
        yield ReplenishmentShipTarget(ship, carrier, expected_position(game, carrier))
