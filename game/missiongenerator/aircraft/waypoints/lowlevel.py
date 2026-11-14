"""Low-level attack profiles in the AI attack tasks.

Shared by the strike and BAI ingress builders. See game.ato.attackprofile for the
profiles and how they keep the AI off the ground.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, TYPE_CHECKING

from dcs import Point

from game.ato import attackprofile
from game.ato.attackprofile import AttackProfile, AttackSettings, attack_settings
from game.utils import feet

if TYPE_CHECKING:
    from game.ato.flight import Flight


class LowLevelAttack:
    """Release altitudes and attack types for a flight's low-level profile."""

    flight: Flight

    @property
    def low_level(self) -> bool:
        """Whether the flight's route was built for a low-level profile."""
        flight = getattr(self, "flight", None)
        layout = getattr(getattr(flight, "flight_plan", None), "layout", None)
        return getattr(layout, "attack_profile", None) is not None

    def low_level_attack(self, target: Point) -> Optional[AttackSettings]:
        """Release altitude and attack type for a low-level profile, or None."""
        layout = self.flight.flight_plan.layout
        profile_name = getattr(layout, "attack_profile", None)
        height_ft = getattr(layout, "low_level_height_ft", None)
        if profile_name is None or height_ft is None:
            return None
        if attackprofile.route_only(self.flight):
            return None
        try:
            profile = AttackProfile(profile_name)
        except ValueError:
            return None
        return attack_settings(profile, feet(height_ft), target, self._munitions())

    def _munitions(self) -> list[str]:
        if not hasattr(self, "_munitions_cache"):
            from game import weaponeering

            try:
                self._munitions_cache = list(
                    weaponeering.carried(
                        [member.loadout for member in self.flight.iter_members()],
                        weaponeering.catalog_for(self.flight.coalition.game),
                    )
                )
            except Exception:
                logging.exception("Could not list the munitions of %s", self.flight)
                self._munitions_cache = []
        return self._munitions_cache

    @staticmethod
    def apply_low_level(task: Any, settings: Optional[AttackSettings]) -> None:
        """Sets a low-level release altitude and attack type on an attack task."""
        if settings is None:
            return
        if settings.altitude_m is not None:
            task.params["altitudeEnabled"] = True
            task.params["altitude"] = round(settings.altitude_m)
        else:
            # The waypoint altitude is a height above the ground here, not an
            # altitude above sea level: never hand it to the AI as one.
            task.params["altitudeEnabled"] = False
            task.params["altitude"] = 0
        # A carpet bombing run stays a level carpet: only the altitude changes.
        # Runway bombing has no attack type at all: it is always a level pass.
        if (
            settings.attack_type is not None
            and "attackType" not in task.params
            and "runwayId" not in task.params
        ):
            task.params["attackType"] = settings.attack_type
