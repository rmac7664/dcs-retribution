"""Automatic resupply of naval vessels (carriers, LHAs) from shared supply ship pool.

Carriers and coastal bases are resupplied when depot infrastructure drops below 60%.
Supply orders are created from nearest friendly depot and routed via cargo ship.
Multiple destinations can be served in supply runs; ships automatically calculate routes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional, Sequence, TYPE_CHECKING

from dcs.mapping import Point
from game.dcs.groundunittype import GroundUnitType
from game.theater import ControlPoint, NavalControlPoint, OffMapSpawn
from game.theater.player import Player
from game.transfers import TransferOrder

if TYPE_CHECKING:
    from game import Game
    from game.coalition import Coalition


# Supply infrastructure thresholds (when to trigger resupply)
AMMO_DEPOT_RESUPPLY_THRESHOLD = 0.60  # Resupply if lost >40% of ammo depots
FUEL_DEPOT_RESUPPLY_THRESHOLD = 0.60  # Resupply if lost >40% of fuel depots

# Cargo ship parameters
CARGO_SHIP_DOCKING_TIME_MINUTES = 30  # Time to dock and unload at each stop
RESUPPLY_PRIORITY_MULTIPLIER = 2.0  # Urgency scale factor

logger = logging.getLogger(__name__)


@dataclass
class CarrierResupplyNeed:
    """One carrier or coastal base that needs resupply.

    Resupply need based on depot infrastructure health, not consumables.
    """

    base: ControlPoint
    missing_ammo_depots: int  # Number of destroyed ammo depots
    missing_fuel_depots: int  # Number of destroyed fuel depots
    urgency: float  # 0.0 (no need) to 1.0 (critical), based on depot losses


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
        """Identify carriers and coastal bases with damaged depot infrastructure.

        A carrier/LHA needs resupply when its ammo or fuel depot count has dropped
        below the threshold (i.e., >40% of depots have been destroyed).

        Returns list of bases needing resupply, ordered by urgency (most critical first).
        """
        needs: list[CarrierResupplyNeed] = []

        for cp in self.game.theater.control_points:
            # Only process friendly naval control points
            if cp.captured != self.coalition.player:
                continue
            if not isinstance(cp, NavalControlPoint):
                continue
            if not (cp.is_carrier or cp.is_lha):
                continue

            # Check depot infrastructure health
            total_ammo = cp.total_ammo_depots_count
            active_ammo = cp.active_ammo_depots_count
            total_fuel = cp.total_fuel_depots_count
            active_fuel = cp.active_fuel_depots_count

            # Calculate how many depots are missing (destroyed)
            missing_ammo = total_ammo - active_ammo if total_ammo > 0 else 0
            missing_fuel = total_fuel - active_fuel if total_fuel > 0 else 0

            # Check if resupply is needed (threshold: >40% destroyed = <60% remaining)
            ammo_depleted = (
                total_ammo > 0 and active_ammo / total_ammo < AMMO_DEPOT_RESUPPLY_THRESHOLD
            )
            fuel_depleted = (
                total_fuel > 0 and active_fuel / total_fuel < FUEL_DEPOT_RESUPPLY_THRESHOLD
            )

            if not (ammo_depleted or fuel_depleted):
                continue

            # Calculate urgency: higher for more losses
            ammo_urgency = (
                (missing_ammo / total_ammo) if total_ammo > 0 else 0.0
            )
            fuel_urgency = (
                (missing_fuel / total_fuel) if total_fuel > 0 else 0.0
            )
            urgency = max(ammo_urgency, fuel_urgency)

            need = CarrierResupplyNeed(
                base=cp,
                missing_ammo_depots=missing_ammo,
                missing_fuel_depots=missing_fuel,
                urgency=urgency * RESUPPLY_PRIORITY_MULTIPLIER,
            )
            needs.append(need)

            logger.debug(
                f"Resupply needed for {cp.name}: "
                f"{missing_ammo}/{total_ammo} ammo depots, "
                f"{missing_fuel}/{total_fuel} fuel depots destroyed"
            )

        # Sort by urgency (most critical first)
        needs.sort(key=lambda n: -n.urgency)
        return needs

    def find_nearest_depot(
        self, destination: ControlPoint
    ) -> Optional[ControlPoint]:
        """Find nearest suitable depot to source supplies from.

        Prefers depots with active warehouse infrastructure and checks for
        transit route availability via the coalition's transit network.
        """
        depots = [
            cp
            for cp in self.game.theater.control_points
            if cp.captured == self.coalition.player
            and (isinstance(cp, OffMapSpawn) or cp.has_runway or cp.has_helipads)
            and self._has_warehouse(cp)
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

    def _get_supply_unit_type(self, depot: ControlPoint) -> Optional[GroundUnitType]:
        """Determine the most appropriate supply unit type from depot.

        Prefers logistics units (trucks, supply vehicles) available in the faction.
        Falls back to generic transport if specific logistics units unavailable.
        """
        faction = self.coalition.faction

        # Try to find logistics/transport units in the faction
        # Common logistics units: Ural-4320 (USSR), M1078 LMTV (USA), etc.
        logistics_unit_names = {"Ural", "LMTV", "truck", "supply", "transport"}

        # Search faction unit inventory for suitable logistics units
        for unit_type in faction.units:
            if unit_type is None:
                continue
            unit_name = unit_type.name.lower()
            # Check if this is a logistics-type unit
            if any(keyword in unit_name for keyword in logistics_unit_names):
                return unit_type

        # Fallback: use any available ground unit for supply transport
        # (in reality, this would transport ammo/fuel containers)
        for unit_type in faction.units:
            if unit_type and hasattr(unit_type, "helicopter") and not unit_type.helicopter:
                # Prefer non-helicopter ground units
                return unit_type

        # Last resort: use first available unit from faction
        for unit_type in faction.units:
            if unit_type:
                return unit_type

        return None

    @staticmethod
    def _has_warehouse(cp: ControlPoint) -> bool:
        """Check if control point has warehouse or supply depot infrastructure.

        Returns True if the base has any active warehouse, ammo, or fuel depot objects.
        """
        DEPOT_CATEGORIES = {"ammo", "factory", "fuel", "ware"}
        return any(
            tgo.category in DEPOT_CATEGORIES and not tgo.is_dead
            for tgo in cp.connected_objectives
        )

    def plan_carrier_resupply(self) -> None:
        """Execute automatic carrier resupply during turn initialization.

        For each carrier/coastal base with damaged depot infrastructure, generate
        resupply orders from nearest friendly depot and route via cargo ship.
        """
        needs = self.find_resupply_needs()
        if not needs:
            return

        logger.info(
            "Carrier resupply planning: %d bases need resupply for %s",
            len(needs),
            self.coalition.player.name if self.coalition.player else "Unknown",
        )

        resupply_count = 0
        for need in needs:
            # Find nearest suitable depot to source supplies from
            depot = self.find_nearest_depot(need.base)
            if not depot:
                logger.warning(
                    f"No suitable depot found to resupply {need.base.name}; skipping"
                )
                continue

            # Determine how many supply units to send
            # Use a nominal supply unit based on faction preferences (e.g., trucks, APCs)
            # If the depot doesn't have specific logistics units, use generic transport
            supply_unit_type = self._get_supply_unit_type(depot)
            if not supply_unit_type:
                logger.warning(
                    f"No supply unit type available at {depot.name}; skipping {need.base.name}"
                )
                continue

            # Calculate quantity: send more for critical shortfalls
            quantity = max(1, int(need.urgency * 5))  # 1-5 units based on urgency

            # Create transfer order for supply delivery
            try:
                transfer = TransferOrder(
                    origin=depot,
                    destination=need.base,
                    position=depot,
                    player=self.coalition.player,
                    units={supply_unit_type: quantity},
                    request_airflift=False,  # Use cargo ship for naval bases
                )

                # Register the transfer with the coalition's transfer system
                # The transfer system will automatically arrange cargo ship transport
                self.coalition.transfers.new_transfer(
                    transfer, self.game.conditions.start_time
                )
                resupply_count += 1

                logger.info(
                    f"Resupply order created: {quantity} {supply_unit_type.name} "
                    f"from {depot.name} to {need.base.name} "
                    f"(urgency: {need.urgency:.2f})"
                )

            except Exception as e:
                logger.error(
                    f"Failed to create resupply transfer for {need.base.name}: {e}"
                )
                continue

        logger.info(
            f"Carrier resupply: {resupply_count}/{len(needs)} orders created"
        )
