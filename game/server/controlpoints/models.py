from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel, ConfigDict

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

    model_config = ConfigDict(title="SupplyStatus")


class ControlPointJs(BaseModel):
    id: UUID
    name: str
    blue: bool
    position: LeafletPoint
    mobile: bool
    destination: LeafletPoint | None
    sidc: str
    supply: SupplyStatusJs | None = None

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
        return ControlPointJs(
            id=control_point.id,
            name=control_point.name,
            blue=blue,
            position=control_point.position.latlng(),
            mobile=control_point.moveable and control_point.captured.is_blue,
            destination=destination,
            sidc=str(control_point.sidc()),
            supply=ControlPointJs.supply_for(control_point),
        )

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
        )

    @staticmethod
    def all_in_game(game: Game) -> list[ControlPointJs]:
        return [
            ControlPointJs.for_control_point(cp) for cp in game.theater.controlpoints
        ]
