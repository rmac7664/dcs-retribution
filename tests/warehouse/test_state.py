import pickle
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from game.settings import Settings
from game.warehouse.plan import MissionWarehousePlan, to_lua
from game.warehouse.state import BaseStock, WarehouseState

AIM_120C = "weapons.missiles.AIM_120C"
HYDRA = "weapons.nurs.HYDRA_70_M151"


def make_game(*cps: Any) -> Any:
    settings = Settings()
    settings.logistics_enabled = True
    return SimpleNamespace(
        settings=settings,
        theater=SimpleNamespace(controlpoints=list(cps)),
    )


def make_cp(name: str) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), name=name)


def test_mission_results_debit_and_credit_metered_stock() -> None:
    cp = make_cp("Batumi")
    game = make_game(cp)
    state = WarehouseState()
    state.stocks[cp.id] = BaseStock(1_000_000, {AIM_120C: 10, HYDRA: 50})
    state.apply_mission_results(
        game,
        {
            "bases": {
                str(cp.id): {
                    "fuel": -12_500.5,
                    # Rockets aren't limited by default, so they aren't booked.
                    "mun": {AIM_120C: -4, HYDRA: -19},
                }
            },
            "resource_map": {AIM_120C: [4, 4, 7, 106], "bogus": [1, 2]},
            "learned": [],
        },
        mission_ended=True,
    )
    stock = state.stocks[cp.id]
    assert stock.munitions == {AIM_120C: 6, HYDRA: 50}
    assert stock.jet_fuel_kg == pytest.approx(987_499.5)
    assert state.resource_map == {AIM_120C: [4, 4, 7, 106]}
    assert state.last_result is not None
    assert state.last_result.munitions_used == {"Batumi": {AIM_120C: 4}}


def test_stock_never_goes_negative() -> None:
    cp = make_cp("Batumi")
    state = WarehouseState()
    state.stocks[cp.id] = BaseStock(100.0, {AIM_120C: 1})
    state.apply_mission_results(
        make_game(cp),
        {"bases": {str(cp.id): {"fuel": -500, "mun": {AIM_120C: -3}}}},
        mission_ended=True,
    )
    assert state.stocks[cp.id].munitions[AIM_120C] == 0
    assert state.stocks[cp.id].jet_fuel_kg == 0


def test_unfinished_mission_does_not_change_stock() -> None:
    cp = make_cp("Batumi")
    state = WarehouseState()
    state.stocks[cp.id] = BaseStock(100.0, {AIM_120C: 5})
    state.apply_mission_results(
        make_game(cp),
        {
            "bases": {str(cp.id): {"fuel": -50, "mun": {AIM_120C: -3}}},
            "resource_map": {AIM_120C: [4, 4, 7, 106]},
        },
        mission_ended=False,
    )
    assert state.stocks[cp.id].munitions[AIM_120C] == 5
    # The wsType cache is still worth keeping.
    assert AIM_120C in state.resource_map


def test_state_survives_pickling_and_old_saves() -> None:
    state = WarehouseState()
    state.learned["X"] = {AIM_120C: 1}
    restored = pickle.loads(pickle.dumps(state))
    assert restored.learned == {"X": {AIM_120C: 1}}
    # A save written before a field existed still loads with the default.
    old = WarehouseState.__new__(WarehouseState)
    old.__setstate__({"stocks": {}})
    assert old.resource_map == {} and old.pending_plan is None


def test_plan_pickles_only_learning_data() -> None:
    plan = MissionWarehousePlan.__new__(MissionWarehousePlan)
    plan.loadout_keys = {"k": ["{A}"]}
    plan.learn_keys = {"k"}
    plan.cargo_units = {"Herc 1": "cp-id"}
    plan.bases = {uuid.uuid4(): SimpleNamespace()}  # type: ignore[dict-item]
    restored = pickle.loads(pickle.dumps(plan))
    assert restored.loadout_keys == {"k": ["{A}"]}
    assert restored.learn_keys == {"k"}
    assert restored.cargo_units == {"Herc 1": "cp-id"}
    assert restored.bases == {}


def test_to_lua() -> None:
    assert to_lua({"a": [1, 2.5, True], 'q"uote': None}) == (
        '{["a"]={1,2.5,true},["q\\"uote"]=nil}'
    )
    assert to_lua("line\nbreak\\") == '"line\\nbreak\\\\"'


def test_logistics_settings_default_off() -> None:
    settings = Settings()
    assert settings.logistics_enabled is False
    assert settings.logistics_meter_missiles and settings.logistics_meter_bombs
    assert not settings.logistics_meter_rockets
