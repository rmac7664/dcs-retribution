"""The AI cuts the enemy's supply lines and covers its own supply arrivals."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import game.commander.objectivefinder as finder_module
from game.commander.objectivefinder import ObjectiveFinder
from game.commander.tasks.compound.cutsupplylines import CutSupplyLines
from game.commander.tasks.compound.nextaction import PlanNextAction
from game.commander.tasks.compound.interdictreinforcements import (
    InterdictReinforcements,
)
from game.commander.tasks.primitive.strike import PlanStrike
from game.commander.tasks.primitive.supplydepotstrike import PlanSupplyDepotStrike
from game.settings import Settings
from game.theater.player import Player
from game.warehouse.state import BaseStock, WarehouseState

AIM_120C = "weapons.missiles.AIM_120C"
MK_82 = "weapons.bombs.Mk_82"


class FakeBuilding:
    def __init__(self, name: str, category: str, dead: bool = False) -> None:
        self.name = name
        self.category = category
        self.is_dead = dead


def base(name: str, *buildings: FakeBuilding, depot: bool = True) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        captured=Player.RED,
        ground_objects=list(buildings),
        is_depot=depot,
        is_friendly=lambda player: player == Player.RED,
    )


def finder_for(*bases: Any, supply_lines: bool = True) -> tuple[ObjectiveFinder, Any]:
    settings = Settings()
    settings.logistics_enabled = True
    settings.logistics_supply_lines = supply_lines
    state = WarehouseState()
    game: Any = SimpleNamespace(
        settings=settings,
        warehouse_logistics=state,
        theater=SimpleNamespace(controlpoints=list(bases)),
    )
    return ObjectiveFinder(game, Player.BLUE), state


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    import game.warehouse.supply as supply_module

    monkeypatch.setattr(finder_module, "BuildingGroundObject", FakeBuilding)
    monkeypatch.setattr(
        supply_module.SupplyPlanner, "is_depot", staticmethod(lambda cp: cp.is_depot)
    )


def test_depots_holding_the_most_are_struck_first() -> None:
    small = base("Maykop", FakeBuilding("Maykop ammo", "ammo"))
    rich = base(
        "Krymsk",
        FakeBuilding("Krymsk fuel", "fuel"),
        FakeBuilding("Krymsk factory", "factory"),
        FakeBuilding("Krymsk SAM", "aa"),  # not part of the depot
        FakeBuilding("Krymsk ware", "ware", dead=True),
    )
    forward = base("Anapa", FakeBuilding("Anapa ammo", "ammo"), depot=False)
    finder, state = finder_for(small, rich, forward)
    state.stocks[small.id] = BaseStock(1000.0, {MK_82: 10})
    state.stocks[rich.id] = BaseStock(1000.0, {AIM_120C: 50})

    names = [t.name for t in finder.supply_depot_targets()]

    assert names == ["Krymsk factory", "Krymsk fuel", "Maykop ammo"]


def test_no_depot_strikes_without_supply_lines() -> None:
    finder, _ = finder_for(
        base("Maykop", FakeBuilding("Maykop ammo", "ammo")), supply_lines=False
    )
    assert list(finder.supply_depot_targets()) == []


def test_supply_depot_strikes_are_capped_per_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(PlanStrike, "preconditions_met", lambda self, state: True)
    monkeypatch.setattr(PlanStrike, "apply_effects", lambda self, state: None)
    depots: list[Any] = [FakeBuilding(f"d{i}", "ammo") for i in range(3)]
    state: Any = SimpleNamespace(
        enemy_supply_depots=list(depots), supply_strikes_left=2
    )

    planned = []
    for depot in depots:
        task = PlanSupplyDepotStrike(depot)
        if task.preconditions_met(state):
            task.apply_effects(state)
            planned.append(depot)

    assert planned == depots[:2]
    assert state.supply_strikes_left == 0


def test_cutting_supply_lines_comes_after_interdiction() -> None:
    methods = [m[0] for m in PlanNextAction(False).each_valid_method(None)]  # type: ignore[arg-type]
    kinds = [type(m) for m in methods]
    assert kinds.index(CutSupplyLines) == kinds.index(InterdictReinforcements) + 1
    state: Any = SimpleNamespace(enemy_supply_depots=["a", "b"])
    tasks: list[Any] = [m[0] for m in CutSupplyLines().each_valid_method(state)]
    assert [t.target for t in tasks] == [
        "a",
        "b",
    ]


def test_threatened_supply_destinations_get_cover() -> None:
    class Base:
        def __init__(self, name: str, exposed: bool) -> None:
            self.name = name
            self.exposed = exposed

    exposed, safe = Base("Senaki", True), Base("Vaziani", False)
    runs = [
        SimpleNamespace(destination=exposed, supplies="cargo"),
        SimpleNamespace(destination=exposed, supplies="cargo"),
        SimpleNamespace(destination=safe, supplies="cargo"),
        SimpleNamespace(destination=exposed, supplies=None),  # tanks, not supply
    ]
    settings = Settings()
    settings.logistics_enabled = True
    game: Any = SimpleNamespace(
        settings=settings,
        threat_zone_for=lambda _player: SimpleNamespace(
            threatened_by_aircraft=lambda cp: cp.exposed
        ),
        coalition_for=lambda _player: SimpleNamespace(
            transfers=SimpleNamespace(pending_transfers=runs)
        ),
    )
    finder = ObjectiveFinder(game, Player.BLUE)
    assert list(finder.threatened_supply_destinations()) == [exposed]
