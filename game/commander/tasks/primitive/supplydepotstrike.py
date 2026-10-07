from __future__ import annotations

from dataclasses import dataclass

from game.commander.tasks.primitive.strike import PlanStrike
from game.commander.theaterstate import TheaterState


@dataclass
class PlanSupplyDepotStrike(PlanStrike):
    """A strike on an enemy supply depot building, to cut its supply lines."""

    def preconditions_met(self, state: TheaterState) -> bool:
        if state.supply_strikes_left <= 0:
            return False
        if self.target not in state.enemy_supply_depots:
            return False
        return super().preconditions_met(state)

    def apply_effects(self, state: TheaterState) -> None:
        state.enemy_supply_depots.remove(self.target)  # type: ignore[arg-type]
        state.supply_strikes_left -= 1
        super().apply_effects(state)
