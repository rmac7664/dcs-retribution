from collections.abc import Iterator

from game.commander.tasks.primitive.supplydepotstrike import PlanSupplyDepotStrike
from game.commander.theaterstate import TheaterState
from game.htn import CompoundTask, Method


class CutSupplyLines(CompoundTask[TheaterState]):
    """Strikes the enemy's supply depots, the most valuable stock first.

    Only with limited supplies and supply lines: depots are where a side buys new
    munitions, so losing their depot buildings starves the bases they serve.
    """

    def each_valid_method(self, state: TheaterState) -> Iterator[Method[TheaterState]]:
        for depot in state.enemy_supply_depots:
            yield [PlanSupplyDepotStrike(depot)]
