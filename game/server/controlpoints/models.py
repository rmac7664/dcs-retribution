from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from game.health import HealthStatus
from game.persistentdamage import health_report

from game.server.leaflet import LeafletPoint

if TYPE_CHECKING:
    from game import Game
    from game.theater import ControlPoint


class SupplyStatusJs(BaseModel):
    munitions_percent: int
    fuel_percent: int
    level: int
    depot: bool
    inbound_runs: int
    lines: list[str]
    main_base: bool = False

    model_config = ConfigDict(title="SupplyStatus")


class ControlPointJs(BaseModel):
    health: HealthStatus | None = None
    id: UUID
    name: str
    blue: bool
    position: LeafletPoint
    mobile: bool
    destination: LeafletPoint | None
    sidc: str
    units: list[str]
    threat_ranges: list[float]
    detection_ranges: list[float]
    # Comms/nav summary for the hover tooltip. None when not applicable
    # (e.g. enemy control point, or no TACAN allocated yet).
    tacan: str | None
    atc_frequency: str | None
    # Carrier/LHA control points only: True when the whole ship group is sunk, so the
    # map's "destroyed (non-repairable)" layer can hide it like a destroyed naval TGO.
    # Always False for airfields/FOBs (a base is not a "destroyed" object).
    dead: bool = False
    supply: SupplyStatusJs | None = None
    #: The side's main supply base (enemy ones only once found).
    main_base: bool = False

    class Config:
        title = "ControlPoint"

    @staticmethod
    def for_control_point(control_point: ControlPoint) -> ControlPointJs:
        destination = None
        if control_point.target_position is not None:
            destination = control_point.target_position.latlng()
        if control_point.captured.is_blue:
            blue = True
        else:
            blue = False
        main_base = ControlPointJs.main_base_for(control_point)
        sidc = control_point.sidc()
        if main_base:
            # Drawn as a headquarters: the NATO HQ staff under the base's symbol.
            from game.sidc import HeadquartersTaskForceDummy

            sidc.headquarters_task_force_dummy = HeadquartersTaskForceDummy.HEADQUARTERS

        # Carrier/LHA control points carry their ship groups (the carrier and
        # its escorts) as an is_control_point ground object that is
        # intentionally not emitted as a standalone TGO. Surface the surviving
        # units and their air-defense ranges on the control point itself so the
        # map can show the escort detail and threat rings the same way it does
        # for ordinary naval groups.
        units: list[str] = []
        threat_ranges: list[float] = []
        detection_ranges: list[float] = []
        dead = False
        for tgo in control_point.ground_objects:
            if not tgo.is_control_point:
                continue
            # A carrier/LHA is "destroyed" once its whole ship group (carrier + escorts)
            # is sunk; naval losses are permanent, so this is a non-repairable death.
            #
            # Fleets only. A FOB carries an is_control_point objective too — its own
            # structures — and reading that one as destroyed hid every neutral FARP
            # behind the "destroyed (non-repairable)" layer, which is off by default.
            # Those FARPs are exactly the ones you are meant to spot and capture.
            if control_point.is_fleet:
                dead = tgo.is_dead
            # Show every unit (display_name already tags dead ones with
            # " [DEAD]"), matching how ordinary naval groups list their losses.
            units.extend(unit.display_name for unit in tgo.units)
            for group in tgo.groups:
                threat = group.max_threat_range().meters
                if threat:
                    threat_ranges.append(threat)
                detection = group.max_detection_range().meters
                if detection:
                    detection_ranges.append(detection)

        tacan, atc_frequency = _comms_summary(control_point)

        return ControlPointJs(
            health=(
                HealthStatus(
                    **health_report(
                        (
                            unit
                            for tgo in control_point.ground_objects
                            if tgo.is_control_point
                            for unit in tgo.units
                        ),
                        control_point.coalition.game.settings,
                    )
                )
                if control_point.is_fleet
                else None
            ),
            id=control_point.id,
            name=control_point.name,
            blue=blue,
            position=control_point.position.latlng(),
            mobile=control_point.moveable and control_point.captured.is_blue,
            destination=destination,
            sidc=str(sidc),
            units=units,
            threat_ranges=threat_ranges,
            detection_ranges=detection_ranges,
            tacan=tacan,
            atc_frequency=atc_frequency,
            dead=dead,
            supply=ControlPointJs.supply_for(control_point),
            main_base=main_base,
        )

    @staticmethod
    def main_base_for(control_point: ControlPoint) -> bool:
        from game.warehouse.supply import main_base_known

        try:
            game = control_point.coalition.game
            return game.settings.logistics_enabled and main_base_known(
                game, control_point
            )
        except Exception:
            logging.exception("Could not check the main base at %s", control_point)
            return False

    @staticmethod
    def supply_for(control_point: ControlPoint) -> SupplyStatusJs | None:
        from game.warehouse.status import supply_status

        try:
            game = control_point.coalition.game
            if not game.settings.logistics_enabled:
                return None
            status = supply_status(game, control_point)
        except Exception:
            # The map must never fail to draw because of a logistics problem.
            logging.exception("Could not summarize supply at %s", control_point)
            return None
        if status is None:
            return None
        return SupplyStatusJs(
            munitions_percent=status.munitions_percent,
            fuel_percent=status.fuel_percent,
            level=status.level,
            depot=status.depot,
            inbound_runs=status.inbound_runs,
            lines=status.lines,
            main_base=status.main_base,
        )

    @staticmethod
    def all_in_game(game: Game) -> list[ControlPointJs]:
        return [
            ControlPointJs.for_control_point(cp) for cp in game.theater.controlpoints
        ]


def _comms_summary(cp: ControlPoint) -> tuple[str | None, str | None]:
    """Return (tacan, atc_frequency) strings for the tooltip, or None if N/A.

    Only resolved for friendly (blue) airfields, since the tooltip shows our
    own bases' comms — enemy details are not exposed in planning.
    """
    from game.atcdata import AtcData
    from game.radio.TacanContainer import TacanContainer
    from game.theater.controlpoint import Airfield

    if not cp.captured.is_blue:
        return None, None

    tacan: str | None = None
    if isinstance(cp, TacanContainer) and cp.tacan is not None:
        if cp.tcn_name:
            tacan = f"{cp.tacan} ({cp.tcn_name})"
        else:
            tacan = str(cp.tacan)

    atc: str | None = None
    if isinstance(cp, Airfield):
        atc_radio = AtcData.from_pydcs(cp.airport)
        if atc_radio is not None and atc_radio.uhf is not None:
            atc = str(atc_radio.uhf)

    return tacan, atc
