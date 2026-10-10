"""Player supply airlifts carried as CTLD crates."""

import uuid
from types import SimpleNamespace
from typing import Any

from dcs.mapping import Point
from dcs.terrain import Caucasus

from game.settings import Settings
from game.warehouse.plan import MissionWarehousePlan
from game.warehouse.state import BaseStock, WarehouseState
from game.warehouse.supply import SupplyLoad, uses_ctld_crates

AIM_120C = "weapons.missiles.AIM_120C"


def settings(cargo: str = "ctld", ctld: bool = True) -> Settings:
    s = Settings()
    s.logistics_enabled = True
    s.logistics_player_supply_cargo = cargo
    s.plugins[s.plugin_settings_key("ctld")] = ctld
    return s


def flight(players: int = 1, helo: bool = True) -> Any:
    return SimpleNamespace(client_count=players, is_helo=helo)


def test_only_player_helicopters_carry_crates_and_only_when_chosen() -> None:
    assert uses_ctld_crates(settings(), flight())
    assert not uses_ctld_crates(settings(cargo="landing"), flight())
    assert not uses_ctld_crates(settings(ctld=False), flight())
    assert not uses_ctld_crates(settings(), flight(players=0))
    assert not uses_ctld_crates(settings(), flight(helo=False))


def test_no_crates_for_a_carrier_delivery() -> None:
    from game.theater.controlpoint import Carrier

    carrier = Carrier.__new__(Carrier)
    to_carrier = flight()
    to_carrier.cargo = SimpleNamespace(
        transport=SimpleNamespace(destination=carrier), destination=carrier
    )
    assert not uses_ctld_crates(settings(), to_carrier)


def bare_plan() -> MissionWarehousePlan:
    plan = MissionWarehousePlan.__new__(MissionWarehousePlan)
    plan.prefixes = ("weapons.missiles.",)
    plan.bases = {}
    plan.units = {}
    plan.learn_keys = set()
    plan.loadout_keys = {}
    plan.cargo_units = {"Hip 1": "x", "Hip 2": "x", "Herc 1": "y"}
    plan.crate_runs = {}
    plan.state = WarehouseState()
    return plan


def test_a_crate_run_no_longer_delivers_on_landing() -> None:
    plan = bare_plan()
    senaki: Any = SimpleNamespace(
        id=uuid.uuid4(), name="Senaki", position=Point(100.04, 200, Caucasus())
    )

    plan.register_crate_run(
        ["Hip 1", "Hip 2"],
        senaki,
        crates=3,
        weight_kg=1500,
        spawn_zone="Hip 1crate_spawn",
        side="blue",
        unit_type="M818",
        description="Supply pallet for Senaki",
    )

    assert plan.cargo_units == {"Herc 1": "y"}
    crates = plan.lua_data()["crates"]
    assert crates["1500"]["key"] == "Hip 1"
    assert crates["1500"]["dest"] == str(senaki.id)
    assert (crates["1500"]["x"], crates["1500"]["z"]) == (100.0, 200)
    restored = MissionWarehousePlan.__new__(MissionWarehousePlan)
    restored.__setstate__(plan.__getstate__())
    assert restored.crate_runs == plan.crate_runs


def crate_debrief(reported: dict[str, int]) -> Any:
    settings_ = Settings()
    settings_.logistics_enabled = True
    game: Any = SimpleNamespace(settings=settings_)
    state = WarehouseState()
    game.warehouse_logistics = state

    def base(name: str) -> Any:
        cp = SimpleNamespace(
            id=uuid.uuid4(),
            name=name,
            captured=SimpleNamespace(is_neutral=False, is_blue=True),
        )
        cp.coalition = SimpleNamespace(game=game)
        state.stocks[cp.id] = BaseStock(0.0)
        return cp

    depot, senaki = base("Kutaisi"), base("Senaki")
    game.theater = SimpleNamespace(
        find_control_point_by_id=lambda cp_id: {depot.id: depot, senaki.id: senaki}[
            cp_id
        ]
    )
    transfer: Any = SimpleNamespace(
        units={"truck": 3},
        supplies=SupplyLoad({AIM_120C: 30}, fuel_kg=3000, tons=3, carriers=3),
        position=depot,
        destination=senaki,
    )
    transfer.size = 3

    def kill_unit(unit_type: str) -> None:
        transfer.units[unit_type] -= 1
        transfer.size -= 1

    transfer.kill_unit = kill_unit
    plan = bare_plan()
    plan.cargo_units = {}
    plan.crate_runs = {
        "Hip 1": {"dest": str(senaki.id), "dest_name": "Senaki", "crates": 3}
    }
    state.pending_plan = plan
    debriefing: Any = SimpleNamespace(
        state_data=SimpleNamespace(mission_ended=True, killed_aircraft=[]),
        unit_map=SimpleNamespace(
            airlift_unit={"Hip 1": SimpleNamespace(transfer=transfer)}.get
        ),
    )
    state.apply_airlift_deliveries(game, debriefing, {"crates_delivered": reported})
    return SimpleNamespace(state=state, depot=depot, senaki=senaki, transfer=transfer)


def test_crates_set_down_are_delivered_and_the_rest_go_back() -> None:
    result = crate_debrief({"Hip 1": 2})
    stocks = result.state.stocks
    assert stocks[result.senaki.id].munitions == {AIM_120C: 20}
    assert stocks[result.senaki.id].jet_fuel_kg == 2000
    assert stocks[result.depot.id].munitions == {AIM_120C: 10}
    assert stocks[result.depot.id].jet_fuel_kg == 1000
    assert result.transfer.size == 0


def test_more_crates_than_were_carried_are_not_credited() -> None:
    result = crate_debrief({"Hip 1": 7})
    assert result.state.stocks[result.senaki.id].munitions == {AIM_120C: 30}
    assert result.state.stocks[result.depot.id].munitions == {}


def test_crates_never_flown_go_back_to_the_depot() -> None:
    result = crate_debrief({})
    assert result.state.stocks[result.senaki.id].munitions == {}
    assert result.state.stocks[result.depot.id].munitions == {AIM_120C: 30}
