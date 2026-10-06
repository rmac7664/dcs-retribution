"""Regressions for the logistics fixes on fix/logistics-bugs."""

import uuid
from types import SimpleNamespace
from typing import Any

from game.settings import Settings
from game.theater.controlpoint import ControlPoint
from game.transfers import MultiGroupTransport
from game.warehouse.plan import MissionWarehousePlan
from game.warehouse.state import BaseStock, WarehouseState
from game.warehouse.supply import SupplyLoad, return_to

AIM_120C = "weapons.missiles.AIM_120C"


def fob() -> Any:
    # Not an Airfield, carrier or off-map spawn, so logistics doesn't manage it.
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="FOB Alpha",
        captured=SimpleNamespace(is_neutral=False, is_blue=True),
    )


def game_with_logistics() -> Any:
    settings = Settings()
    settings.logistics_enabled = True
    game = SimpleNamespace(settings=settings)
    game.warehouse_logistics = WarehouseState()
    return game


def test_cargo_arriving_somewhere_new_does_not_come_with_a_full_warehouse() -> None:
    base, game = fob(), game_with_logistics()
    state = game.warehouse_logistics

    state.receive(game, base, {AIM_120C: 6}, fuel_kg=2000)

    assert state.stocks[base.id].munitions == {AIM_120C: 6}
    assert state.stocks[base.id].jet_fuel_kg == 2000


def test_unmanaged_bases_start_empty() -> None:
    base, game = fob(), game_with_logistics()

    stock = game.warehouse_logistics.ensure_stock(game, base)

    assert stock.munitions == {} and stock.jet_fuel_kg == 0


def test_supply_trucks_are_not_reinforcements() -> None:
    truck, tank = "truck", "tank"
    base: Any = SimpleNamespace(
        ground_unit_orders=SimpleNamespace(units={}),
        base=SimpleNamespace(armor={}),
    )
    supply_run = SimpleNamespace(
        destination=base, units={truck: 3}, supplies=SupplyLoad({AIM_120C: 1})
    )
    reinforcements = SimpleNamespace(destination=base, units={tank: 4}, supplies=None)

    allocated = ControlPoint.allocated_ground_units(
        base, [supply_run, reinforcements]  # type: ignore[arg-type]
    )

    assert dict(allocated.transferring) == {tank: 4}


def test_returned_and_delivered_cargo_add_up_to_the_whole_load() -> None:
    depot, game = fob(), game_with_logistics()
    depot.coalition = SimpleNamespace(game=game)
    game.warehouse_logistics.stocks[depot.id] = BaseStock(0.0)
    load = SupplyLoad({AIM_120C: 7}, tons=2, carriers=2)
    transfer = SimpleNamespace(supplies=load)

    return_to(transfer, depot, 1)  # type: ignore[arg-type]

    returned = game.warehouse_logistics.stocks[depot.id].munitions[AIM_120C]
    # The rest stays on the order for delivery: nothing vanishes to rounding.
    assert returned + load.munitions[AIM_120C] == 7
    assert load.carriers == 1


def test_airlifts_to_bases_the_mission_cannot_see_are_not_tracked() -> None:
    airfield, forward = fob(), fob()
    plan = MissionWarehousePlan.__new__(MissionWarehousePlan)
    plan.bases = {airfield.id: SimpleNamespace()}  # type: ignore[dict-item]
    plan.cargo_units = {}
    plan.units = {}

    def airlifter(destination: Any) -> Any:
        transfer = SimpleNamespace(
            supplies=SupplyLoad(), destination=destination, transport=None
        )
        return SimpleNamespace(cargo=transfer, departure=SimpleNamespace(id=None))

    plan.register_unit(airlifter(airfield), SimpleNamespace(name="Herc 1"), None)  # type: ignore[arg-type]
    plan.register_unit(airlifter(forward), SimpleNamespace(name="Hip 1"), None)  # type: ignore[arg-type]

    # Only the landing at the airfield can be confirmed by the mission script; the
    # helicopter to the FOB counts as delivered unless it is shot down.
    assert plan.cargo_units == {"Herc 1": str(airfield.id)}


def test_airlifters_that_had_not_landed_when_the_mission_stopped_bring_cargo_back() -> (
    None
):
    depot, game = fob(), game_with_logistics()
    depot.coalition = SimpleNamespace(game=game)
    state = game.warehouse_logistics
    state.stocks[depot.id] = BaseStock(0.0)
    transfer: Any = SimpleNamespace(
        units={"pallet": 1},
        supplies=SupplyLoad({AIM_120C: 10}, tons=2, carriers=1),
        position=depot,
        destination=SimpleNamespace(name="Akrotiri"),
    )
    transfer.size = 1

    def kill_unit(unit_type: str) -> None:
        transfer.units[unit_type] -= 1
        transfer.size -= 1

    transfer.kill_unit = kill_unit
    state.pending_plan = SimpleNamespace(cargo_units={"Herc 1": "x"})
    debriefing = SimpleNamespace(
        state_data=SimpleNamespace(mission_ended=False, killed_aircraft=[]),
        unit_map=SimpleNamespace(
            airlift_unit={
                "Herc 1": SimpleNamespace(cargo=("pallet",), transfer=transfer)
            }.get
        ),
    )

    state.apply_airlift_deliveries(game, debriefing, {"delivered": {}})

    assert transfer.size == 0
    assert state.stocks[depot.id].munitions == {AIM_120C: 10}


def test_losses_in_a_merged_convoy_are_spread_over_its_orders() -> None:
    truck = "truck"

    def order(trucks: int) -> Any:
        o = SimpleNamespace(units={truck: trucks})

        def kill_unit(unit_type: str) -> None:
            o.units[unit_type] -= 1

        o.kill_unit = kill_unit
        return o

    first, second = order(2), order(4)
    convoy = MultiGroupTransport.__new__(MultiGroupTransport)
    convoy.transfers = [first, second]

    for _ in range(3):
        convoy.kill_unit(truck)  # type: ignore[arg-type]

    assert first.units[truck] + second.units[truck] == 3
    assert first.units[truck] >= 1 and second.units[truck] >= 1


def test_pallets_go_back_into_full_trucks_for_the_road() -> None:
    from game.warehouse.supply import load_into_trucks

    settings = Settings()  # 10 t trucks, at most 6 per shipment
    transfer: Any = SimpleNamespace(
        units={"truck": 10},
        supplies=SupplyLoad({AIM_120C: 40}, tons=40, carriers=10),
    )
    transfer.size = 10

    load_into_trucks(transfer, settings)

    assert transfer.units == {"truck": 4}
    assert transfer.supplies.carriers == 4
    assert transfer.supplies.munitions == {AIM_120C: 40}


def test_an_empty_order_is_not_repacked() -> None:
    from game.warehouse.supply import fit_pallets

    transfer: Any = SimpleNamespace(
        units={"truck": 0}, supplies=SupplyLoad({AIM_120C: 4}, tons=40, carriers=4)
    )
    transfer.size = 0
    mi8: Any = SimpleNamespace(
        dcs_unit_type=SimpleNamespace(id="Mi-8MT", helicopter=True)
    )

    fit_pallets(transfer, mi8)

    assert transfer.units == {"truck": 0}


def test_airlifts_to_bases_dropped_from_the_mission_count_as_delivered() -> None:
    from dcs import Mission
    from dcs.terrain import Caucasus

    from game.missiongenerator.missiondata import MissionData

    kept, dropped = fob(), fob()
    plan = MissionWarehousePlan.__new__(MissionWarehousePlan)
    # A non-Airfield, non-naval base is dropped by apply_to_mission.
    dropped_base: Any = SimpleNamespace(
        cp=dropped, fuel_kg=0.0, calibrate=True, munitions={}
    )
    plan.bases = {dropped.id: dropped_base}
    plan.cargo_units = {"Hip 1": str(dropped.id), "Herc 1": str(kept.id)}
    plan.units = {}
    plan.learn_keys = set()
    plan.prefixes = ("weapons.missiles.",)
    plan.state = WarehouseState()
    plan.report = lambda: None  # type: ignore[method-assign]

    plan.apply_to_mission(Mission(Caucasus()), MissionData())

    assert plan.cargo_units == {}
