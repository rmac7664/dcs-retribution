"""Automatic resupply of naval vessels (carriers, LHAs) from shared supply ship pool.

Carriers and coastal bases are resupplied when fuel or munitions drop below 40%.
Supply ships spawn from nearest depot and take 30 minutes to dock/unload.
Multiple destinations can be served in one supply run.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING

from dcs.mapping import Point
from game.dcs.groundunittype import GroundUnitType
from game.theater import ControlPoint, NavalControlPoint, OffMapSpawn
from game.theater.player import Player

if TYPE_CHECKING:
    from game import Game
    from game.coalition import Coalition


CARRIER_RESUPPLY_THRESHOLD = 0.40  # 40% fuel or munitions triggers resupply
CARGO_SHIP_TRAVEL_TIME_HOURS = 1.5  # Spawn this far away from origin
CARGO_SHIP_DOCKING_TIME_MINUTES = 30  # Time to dock and unload at each stop
ONE_TURN_HOURS = 24  # One game turn is 24 hours
SHIP_SPEED_KNOTS = 15  # Approximate speed for cargo ship (15 knots)

logger = logging.getLogger(__name__)


@dataclass
class CarrierResupplyNeed:
    """One carrier or coastal base that needs resupply."""

    base: ControlPoint
    fuel_deficit_kg: float  # How much fuel to bring (0 if full)
    munitions_needed: dict[str, int]  # Resource name -> quantity needed
    urgency: float  # 0.0 (full) to 1.0 (empty), based on shortfall


def distance_nm(p1: Point, p2: Point) -> float:
    """Calculate distance between two points in nautical miles."""
    # Use DCS built-in distance calculation
    return p1.distance_to_point(p2) / 1852.0  # 1 nautical mile = 1852 meters


def get_travel_distance_nm(
    origin: ControlPoint, destination: ControlPoint
) -> float:
    """Calculate approximate travel distance in nautical miles via shipping lane."""
    try:
        route: Sequence[Point] = origin.shipping_lanes.get(destination, ())
        if not route:
            return 0.0

        total_distance_nm = 0.0
        for i in range(len(route) - 1):
            total_distance_nm += distance_nm(route[i], route[i + 1])
        return total_distance_nm
    except Exception:
        return 0.0


class CarrierResupplyPlanner:
    """Plan and execute automatic resupply of carriers and coastal naval bases."""

    def __init__(self, game: Game, coalition: Coalition) -> None:
        self.game = game
        self.coalition = coalition
        self.settings = game.settings

    def find_resupply_needs(self) -> list[CarrierResupplyNeed]:
        """Identify carriers and coastal bases below 40% fuel or munitions.

        Returns list of bases needing resupply, ordered by urgency (most critical first).
        """
        needs: list[CarrierResupplyNeed] = []

        for cp in self.game.theater.control_points:
            if cp.captured != self.coalition.player:
                continue
            if not isinstance(cp, NavalControlPoint):
                continue
            if not (cp.is_carrier or cp.is_lha):
                continue

            # For now, we don't have direct access to stock levels in the real codebase
            # This is a placeholder - in a real implementation, you'd check actual fuel/munitions
            # from the supply system or similar storage mechanism

            # TODO: Integrate with actual supply tracking system once available

        # Sort by urgency (most critical first)
        needs.sort(key=lambda n: -n.urgency)
        return needs

    def find_nearest_depot(
        self, destination: ControlPoint
    ) -> Optional[ControlPoint]:
        """Find nearest suitable depot to source supplies from."""
        depots = [
            cp
            for cp in self.game.theater.control_points
            if cp.captured == self.coalition.player
            and (isinstance(cp, OffMapSpawn) or cp.has_runway or cp.has_helipads)
        ]

        if not depots:
            return None

        network = self.coalition.transit_network
        best: Optional[tuple[float, ControlPoint]] = None

        for depot in depots:
            if isinstance(depot, NavalControlPoint):
                # Don't source from the same carrier
                if depot == destination:
                    continue
                # Non-flagship carriers supply themselves; they don't resupply others
                continue

            try:
                if not network.has_path_between(depot, destination):
                    continue
                _, cost = network.shortest_path_with_cost(depot, destination)
            except Exception:
                continue

            if best is None or cost < best[0]:
                best = (cost, depot)

        return best[1] if best else None

    @staticmethod
    def _has_warehouse(cp: ControlPoint) -> bool:
        """Check if control point has warehouse or ammo/fuel depot."""
        DEPOT_CATEGORIES = {"ammo", "factory", "fuel", "ware"}
        return any(
            tgo.category in DEPOT_CATEGORIES and not tgo.is_dead
            for tgo in cp.connected_objectives
        )

    def plan_carrier_resupply(self) -> None:
        """Execute automatic carrier resupply at turn end.

        For each carrier/coastal base below 40%, generate supply orders from
        nearest depot, dispatch via shared supply ship pool.
        """
        needs = self.find_resupply_needs()
        if not needs:
            return

        logger.info(
            "Carrier resupply check: %d bases need resupply for %s",
            len(needs),
            self.coalition.player.name if self.coalition.player else "Unknown",
        )

        # TODO: Integrate with actual transfer/cargo system once available
        # This is placeholder until supply tracking and transfer system integration is complete
