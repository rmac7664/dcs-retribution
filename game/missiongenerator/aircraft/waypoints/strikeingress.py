import copy
from collections.abc import Sequence
from typing import Any, Union

from dcs import Point
from dcs.planes import B_17G, B_52H, Tu_22M3, B_1B, F_15ESE, C_130J_30
from dcs.point import MovingPoint
from dcs.task import Bombing, Expend, OptFormation, WeaponType, CarpetBombing

from game.theater.theatergroundobject import MotorpoolGroundObject
from game.utils import mach, meters
from .pydcswaypointbuilder import PydcsWaypointBuilder


class StrikeIngressBuilder(PydcsWaypointBuilder):
    _special_wpts_injected: bool = False

    def add_tasks(self, waypoint: MovingPoint) -> None:
        if self.group.units[0].unit_type is C_130J_30 and any(
            unit.pylons.get(3, {}).get("CLSID") == "{GBU-43}"
            for unit in self.group.units
        ):
            self.add_moab_task(waypoint)
            self.register_special_ingress_points()
            return

        bomber = self.group.units[0].unit_type in [B_17G, Tu_22M3]
        bomber_guided = self.group.units[0].unit_type in [B_1B, B_52H]
        if bomber_guided or not bomber:
            waypoint.tasks.append(OptFormation.finger_four_open())
            self.add_strike_tasks(waypoint, WeaponType.ASM)
            waypoint.tasks.append(OptFormation.trail_open())
            self.add_strike_tasks(waypoint, WeaponType.GuidedBombs)

        waypoint.tasks.append(OptFormation.ww2_bomber_element_close())
        self.add_bombing_tasks(waypoint)
        waypoint.tasks.append(OptFormation.finger_four_open())
        if not self.is_follow_on:
            self.register_special_ingress_points()

    @property
    def is_follow_on(self) -> bool:
        """True for the ingress of an additional target set, after the first."""
        layout = self.flight.flight_plan.layout
        check = getattr(layout, "is_follow_on", None)
        return bool(check is not None and check(self.waypoint))

    @property
    def target_sets(self) -> int:
        """How many target sets this flight attacks, the first one included."""
        layout = self.flight.flight_plan.layout
        sets = getattr(layout, "follow_on_sets", None)
        return 1 if sets is None else 1 + len(sets())

    @property
    def sets_remaining(self) -> int:
        """How many target sets are still to be attacked, this one included.

        1 for an ordinary flight and for the last set of a multi-target one, so the
        last target is not attacked with weapons held back for nothing.
        """
        layout = self.flight.flight_plan.layout
        sets = getattr(layout, "follow_on_sets", None)
        if sets is None:
            return 1
        ingresses = [ingress for ingress, _ in sets()]
        waypoint = getattr(self, "waypoint", None)
        for index, ingress in enumerate(ingresses):
            if waypoint is ingress:
                return len(ingresses) - index
        return 1 + len(ingresses)

    def share_of_payload(self) -> Expend:
        """The most one target set may use, so that later sets get their share."""
        sets = self.sets_remaining
        if sets <= 1:
            return Expend.All
        if sets == 2:
            return Expend.Half
        return Expend.Quarter

    def add_moab_task(self, waypoint: MovingPoint) -> None:
        """Use one normal bombing pass without the generic jet-speed override."""
        targets = self.waypoint.targets
        if isinstance(self.package.target, MotorpoolGroundObject):
            targets = self.package.target.strike_targets
        if not targets:
            return

        center: Point = copy.copy(targets[0].position)
        for target in targets[1:]:
            center += target.position
        center /= len(targets)
        waypoint.tasks.append(
            Bombing(
                center,
                weapon_type=WeaponType.Auto,
                expend=Expend.All,
                altitude=waypoint.alt,
            )
        )

    def add_bombing_tasks(self, waypoint: MovingPoint) -> None:
        targets = self.waypoint.targets
        if not self.is_follow_on and isinstance(
            self.package.target, MotorpoolGroundObject
        ):
            # The motorpool is a live cache reconciled from the current reserve;
            # re-derive the units at ingress so the tasks match what actually
            # rendered. Mission spec: one bomb task per parked unit (an empty
            # reserve adds nothing), not a single centroid task.
            targets = self.package.target.strike_targets
            if targets:
                self.add_bombing_task_per_target(waypoint, targets, WeaponType.Bombs)
            return
        if not targets:
            return

        center: Point = copy.copy(targets[0].position)
        for target in targets[1:]:
            center += target.position
        center /= len(targets)
        avg_spacing = 0.0
        for t in targets:
            avg_spacing += center.distance_to_point(t.position)
        avg_spacing /= len(targets)
        bombing: Union[CarpetBombing, Bombing]
        if self.group.task == "Ground Attack":
            bombing = CarpetBombing(
                center,
                weapon_type=WeaponType.Bombs,
                expend=self.share_of_payload(),
                carpet_length=avg_spacing,
                altitude=waypoint.alt,
            )
        else:
            bombing = Bombing(
                center,
                weapon_type=WeaponType.Bombs,
                expend=self.share_of_payload(),
                group_attack=True,
                altitude=waypoint.alt,
            )
        waypoint.tasks.append(bombing)

    def add_strike_tasks(
        self, waypoint: MovingPoint, weapon_type: WeaponType = WeaponType.Auto
    ) -> None:
        if not self.waypoint.targets:
            # Nothing to strike (e.g. an objective whose units are all already
            # destroyed). Skip -- as add_bombing_tasks does -- so we neither add
            # an empty attack task nor divide by zero on the units/targets ratio.
            return
        self.add_bombing_task_per_target(waypoint, self.waypoint.targets, weapon_type)

        # Register special waypoints
        if not self._special_wpts_injected and not self.is_follow_on:
            self.register_special_strike_points(self.waypoint.targets)
            if self.flight.unit_type.dcs_unit_type == F_15ESE:
                self.register_special_strike_points(self.flight.custom_targets, 2)
            self._special_wpts_injected = True

    def add_bombing_task_per_target(
        self,
        waypoint: MovingPoint,
        targets: Sequence[Any],
        weapon_type: WeaponType,
    ) -> None:
        """Add one Bombing task per target, sizing the expenditure per task.

        The expend per target is scaled by the aircraft-to-target ratio so a
        package does not dump its whole payload on the first task: with at
        least one aircraft per target each aircraft expends everything per
        target, and progressively less as targets outnumber aircraft.
        """
        bomber = self.group.units[0].unit_type in [B_1B, B_52H]
        # A flight with further target sets to fly keeps a share back for them.
        sets = self.sets_remaining
        ratio = len(self.group.units) / (len(targets) * sets)
        for target in targets:
            bombing = Bombing(target.position, weapon_type=weapon_type)
            if len(targets) == 1:
                bombing.params["groupAttack"] = True
            # If there is only one target, drop all ordnance in one pass with group attack.
            if len(targets) == 1 and sets == 1:
                bombing.params["expend"] = Expend.All.value
            elif ratio >= 1:
                # #TGTs > 1 & #AC >= #TGTs => each AC drops entire payload per TGT
                bombing.params["expend"] = Expend.All.value
            elif 1 > ratio >= 0.5:
                # #TGTs > 1 & 2 * #AC >= #TGTs => each AC drops half payload per TGT
                bombing.params["expend"] = Expend.Half.value
            elif 0.5 > ratio >= 0.25:
                # #TGTs > 1 & 4 * #AC >= #TGTs => each AC drops quarter payload per TGT
                bombing.params["expend"] = Expend.Quarter.value
            elif 0.25 > ratio >= (1.0 / 6) and bomber:
                # #TGTs > 1 & 4 * #AC < #TGTs & bomber => each AC drops 4 bombs per TGT
                bombing.params["expend"] = Expend.Four.value
            elif bomber:
                # #TGTs > 1 & 6 * #AC < #TGTs & bomber => each AC drops 2 bombs per TGT
                bombing.params["expend"] = Expend.Two.value
            elif sets > 1:
                # More target sets to fly => one weapon per target, so that the
                # first set does not take everything.
                bombing.params["expend"] = Expend.One.value
            # else => Auto QTY

            waypoint.tasks.append(bombing)

            waypoint.speed = mach(0.85, meters(waypoint.alt)).meters_per_second
