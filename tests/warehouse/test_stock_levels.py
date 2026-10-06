"""Stock targets from flown loadouts, rear-only depots, starting supply."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from game.ato.flighttype import FlightType
from game.settings import Settings
from game.theater.player import Player
from game.warehouse.state import WarehouseState
from game.warehouse.supply import SupplyPlanner

AIM_9 = "weapons.missiles.AIM_9"
AGM_65 = "weapons.missiles.AGM_65D"
MK_82 = "weapons.bombs.Mk_82"


def game_with(**settings: Any) -> Any:
    s = Settings()
    s.logistics_enabled = True
    for k, v in settings.items():
        setattr(s, k, v)
    return SimpleNamespace(settings=s, date=None)


def squadron(count: int, *tasks: FlightType) -> Any:
    return SimpleNamespace(
        aircraft="A-10C",
        owned_aircraft=count,
        primary_task=tasks[0],
        auto_assignable_mission_types=set(tasks),
    )


def test_bases_are_stocked_for_the_missions_their_squadrons_fly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = WarehouseState()
    seen: list[set[FlightType]] = []

    def task_loads(_game: Any, _cp: Any, _aircraft: Any, tasks: Any) -> dict[str, int]:
        seen.append(set(tasks))
        return {AGM_65: 4, AIM_9: 2}

    monkeypatch.setattr(state, "_task_loads", task_loads)
    monkeypatch.setattr(state, "_max_loads", lambda *_: pytest.fail("not needed"))
    base = SimpleNamespace(squadrons=[squadron(11, FlightType.CAS, FlightType.BAI)])

    authorized = state.authorized_munitions(game_with(), base)  # type: ignore[arg-type]

    # 4 sorties x 11 aircraft x the most each mission's loadout carries.
    assert authorized == {AGM_65: 176, AIM_9: 88}
    assert seen == [{FlightType.CAS, FlightType.BAI}]


def test_squadrons_without_armed_mission_loadouts_fall_back_to_every_loadout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = WarehouseState()
    monkeypatch.setattr(state, "_task_loads", lambda *_: {})
    monkeypatch.setattr(state, "_max_loads", lambda *_: {MK_82: 6})
    base = SimpleNamespace(squadrons=[squadron(2, FlightType.STRIKE)])

    assert state.authorized_munitions(game_with(), base) == {MK_82: 48}  # type: ignore[arg-type]


def land_base(
    owner: Player, started: Player, front: bool, depot_alive: bool = True
) -> Any:
    building = SimpleNamespace(category="ammo", is_dead=not depot_alive)
    return SimpleNamespace(
        captured=owner,
        starting_coalition=started,
        has_frontline=front,
        connected_objectives=[building],
    )


def test_only_rear_bases_held_since_the_start_buy_munitions() -> None:
    blue, red = Player.BLUE, Player.RED
    assert SupplyPlanner.is_depot(land_base(blue, blue, front=False))
    # On a front line: supplied by supply run.
    assert not SupplyPlanner.is_depot(land_base(blue, blue, front=True))
    # Captured from the enemy, even with its ammo depot standing.
    assert not SupplyPlanner.is_depot(land_base(blue, red, front=False))
    # No live depot buildings.
    assert not SupplyPlanner.is_depot(land_base(blue, blue, False, depot_alive=False))


def test_starting_supply_scales_each_sides_first_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = game_with(
        logistics_player_starting_supply=0.5, logistics_enemy_starting_supply=0.25
    )
    state = WarehouseState()
    monkeypatch.setattr(WarehouseState, "is_managed", staticmethod(lambda *_: True))
    monkeypatch.setattr(
        WarehouseState, "fuel_capacity_kg", staticmethod(lambda *_: 1000.0)
    )
    monkeypatch.setattr(state, "authorized_munitions", lambda *_: {AIM_9: 40, MK_82: 2})

    def base(owner: Player) -> Any:
        return SimpleNamespace(id=uuid.uuid4(), captured=owner)

    blue = state.ensure_stock(game, base(Player.BLUE))
    red = state.ensure_stock(game, base(Player.RED))
    assert blue.jet_fuel_kg == 500 and blue.munitions == {AIM_9: 20, MK_82: 1}
    assert red.jet_fuel_kg == 250 and red.munitions == {AIM_9: 10}
