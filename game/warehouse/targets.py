"""Replenishment ships as targets the AI can plan anti-ship strikes against."""

from __future__ import annotations

import logging
from typing import Iterator, Optional, TYPE_CHECKING

from dcs.mapping import Point

from game.theater.controlpoint import NavalControlPoint
from game.theater.missiontarget import MissionTarget
from game.utils import nautical_miles

if TYPE_CHECKING:
    from game import Game
    from game.coalition import Coalition
    from game.ato import FlightType
    from game.theater.player import Player
    from game.theater.theatergroundobject import TheaterGroundObject
    from .state import SupplyShip

#: Where along its approach a replenishment ship is expected when strikes are planned,
#: as a share of its start distance: early in its transit (strikes are flown in the
#: first part of the mission), before it reaches the carrier.
EXPECTED_SHARE = 0.9


class ReplenishmentShipTarget(MissionTarget):
    """An enemy replenishment ship heading for its carrier this mission."""

    def __init__(
        self, ship: SupplyShip, carrier: NavalControlPoint, position: Point
    ) -> None:
        super().__init__(f"Replenishment ship for {carrier.name}", position)
        self.ship = ship
        self.carrier = carrier

    @property
    def coalition(self) -> Coalition:
        return self.carrier.coalition

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
    """Where the ship should be: partway in from the main supply base's side."""
    from game.missiongenerator.replenishmentshipgenerator import (
        origin_bearing_point,
        start_distance,
        start_point,
    )

    away: Optional[Point] = origin_bearing_point(game, carrier, carrier.position)
    if away is None:
        enemy = [
            cp.position
            for cp in game.theater.controlpoints
            if cp.captured == carrier.captured.opponent
        ]
        away = min(enemy, key=carrier.position.distance_to_point) if enemy else None
    distance = nautical_miles(
        start_distance(game.settings).nautical_miles * EXPECTED_SHARE
    )
    found = start_point(carrier.position, away, distance, game.theater.is_in_sea)
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
        position = expected_position(game, carrier)
        reason = worth_striking(game, ship, carrier, position)
        if reason is None:
            logging.info(
                "%s: not striking %s's replenishment ship: it sails under the "
                "group's air defences, whose magazines are well stocked",
                player.name,
                carrier.name,
            )
            continue
        logging.info(
            "%s: %s's replenishment ship is worth striking (%s)",
            player.name,
            carrier.name,
            reason,
        )
        yield ReplenishmentShipTarget(ship, carrier, position)


#: Below this share of their interceptors, a ship group's magazines are low enough
#: that its replenishment ship is worth the risk of striking.
LOW_MAGAZINE_SHARE = 0.25


def protecting_ships(
    game: Game, carrier: NavalControlPoint, position: Point
) -> list[TheaterGroundObject]:
    """Live warships of the carrier's side whose air defences cover `position`.

    The full threat range is used, whatever the auto-planner's aggressiveness: a
    replenishment ship sails alongside its group, so a strike on it is a strike
    into the group's missile envelope.
    """
    from game.theater.theatergroundobject import NavalGroundObject

    found: list[TheaterGroundObject] = []
    for cp in game.theater.controlpoints:
        if cp.captured != carrier.captured:
            continue
        for tgo in getattr(cp, "ground_objects", []):
            if not isinstance(tgo, NavalGroundObject) or tgo.is_dead:
                continue
            reach = tgo.max_threat_range().meters
            if reach > 0 and tgo.position.distance_to_point(position) <= reach:
                found.append(tgo)
    return found


def interceptor_share(game: Game, carrier: NavalControlPoint) -> Optional[float]:
    """How full the carrier group's interceptor magazines are (0-1), if tracked.

    None when SAM missiles aren't limited or the group's loads aren't learned yet.
    """
    from .sam import authorized_sam, enabled, seed_new_missiles
    from .supply import STRIKE_SAM_PREFIXES

    if not enabled(game):
        return None
    # Missiles just learned get their starting stock before anyone judges it.
    seed_new_missiles(game)
    wanted = {
        k: v
        for k, v in authorized_sam(game, carrier).items()
        if not k.startswith(STRIKE_SAM_PREFIXES)
    }
    total = sum(wanted.values())
    if total <= 0:
        return None
    stock = game.warehouse_logistics.stocks.get(carrier.id)
    held = stock.munitions if stock is not None else {}
    have = sum(min(max(0, held.get(k, 0)), v) for k, v in wanted.items())
    return have / total


def worth_striking(
    game: Game, ship: SupplyShip, carrier: NavalControlPoint, position: Point
) -> Optional[str]:
    """Why a replenishment ship is worth an anti-ship strike, or None if it isn't.

    It is if it sails outside its group's air defences (the escorts are sunk), or if
    the group's interceptor magazines are low, so the strike can get through and
    stopping the resupply hurts.
    """
    if not protecting_ships(game, carrier, position):
        return "no air defence covers it"
    share = interceptor_share(game, carrier)
    if share is not None and share < LOW_MAGAZINE_SHARE:
        return f"the group's interceptors are down to {share:.0%}"
    return None
