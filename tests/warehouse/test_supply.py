from types import SimpleNamespace
from typing import Any

import pytest

from game.settings import Settings
from game.warehouse.supply import (
    MunitionMasses,
    MunitionPrices,
    SupplyLoad,
    cargo_tons,
    carriers_per_aircraft,
    fit_pallets,
)

AIM_120C = "weapons.missiles.AIM_120C"
MK_82 = "weapons.bombs.Mk_82"


def aircraft(dcs_id: str, helicopter: bool = False) -> Any:
    return SimpleNamespace(
        dcs_unit_type=SimpleNamespace(id=dcs_id, helicopter=helicopter)
    )


def test_load_scales_and_splits_by_carrier() -> None:
    load = SupplyLoad({AIM_120C: 12, MK_82: 7}, fuel_kg=6000, tons=12.0, carriers=4)
    half = load.scaled(0.5)
    assert half.munitions == {AIM_120C: 6, MK_82: 3}
    assert half.fuel_kg == pytest.approx(3000)
    part = load.split_off(1)
    assert part.carriers == 1 and part.munitions == {AIM_120C: 3, MK_82: 1}
    assert load.carriers == 3 and load.munitions == {AIM_120C: 9, MK_82: 6}
    assert load.tons == pytest.approx(9.0)


def test_lost_carriers_lose_their_share() -> None:
    load = SupplyLoad({AIM_120C: 40}, tons=8, carriers=4)
    assert load.scaled(3 / 4).munitions == {AIM_120C: 30}
    assert load.scaled(0).munitions == {}


def test_prices_follow_the_table_and_multiplier() -> None:
    settings = Settings()
    assert MunitionPrices.price(AIM_120C, settings) == pytest.approx(1.0)
    assert MunitionPrices.price(MK_82, settings) == pytest.approx(0.008)
    assert MunitionPrices.price("weapons.missiles.SOMETHING_NEW", settings) == (
        pytest.approx(0.3)
    )
    settings.logistics_munition_price_percent = 250
    assert MunitionPrices.price(AIM_120C, settings) == pytest.approx(2.5)


def test_masses_come_from_launcher_data() -> None:
    assert 140 < MunitionMasses.mass_kg(AIM_120C) < 180
    assert MunitionMasses.tons({AIM_120C: 10}, fuel_kg=1000) == pytest.approx(
        MunitionMasses.mass_kg(AIM_120C) * 10 / 1000 + 1
    )


def test_cargo_capacity_by_type() -> None:
    assert cargo_tons(aircraft("C-17A")) > cargo_tons(aircraft("C-130J-30")) > 10
    assert cargo_tons(aircraft("IL-76MD")) == 47
    assert cargo_tons(aircraft("SomeModHelo", helicopter=True)) == 3.0


def test_airlift_capacity_counts_pallets_by_weight() -> None:
    supply = SimpleNamespace(supplies=SupplyLoad({AIM_120C: 60}, tons=40, carriers=4))
    ground = SimpleNamespace(supplies=None)
    # 10 t pallets, whole pallets only: a C-17 (77 t) takes 7, a C-130 (19 t) one.
    assert carriers_per_aircraft(supply, aircraft("C-17A")) == 7  # type: ignore[arg-type]
    assert carriers_per_aircraft(supply, aircraft("C-130J-30")) == 1  # type: ignore[arg-type]
    assert carriers_per_aircraft(supply, aircraft("Mi-8MT", True)) == 1  # type: ignore[arg-type]
    # Ground-unit transfers keep the old rule.
    assert carriers_per_aircraft(ground, aircraft("C-130")) == 2  # type: ignore[arg-type]
    assert carriers_per_aircraft(ground, aircraft("Mi-8MT", True)) == 1  # type: ignore[arg-type]


def test_small_helicopters_get_smaller_pallets() -> None:
    truck = "truck"
    transfer = SimpleNamespace(
        units={truck: 4},
        supplies=SupplyLoad({AIM_120C: 40}, fuel_kg=0, tons=40, carriers=4),
    )
    transfer.size = 4
    mi8 = aircraft("Mi-8MT", True)

    fit_pallets(transfer, mi8)  # type: ignore[arg-type]

    # 40 t no longer goes up as four 10 t truckloads: ten 4 t pallets instead.
    assert transfer.units == {truck: 10}
    assert transfer.supplies.carriers == 10
    assert transfer.supplies.tons_per_carrier == pytest.approx(4)
    assert transfer.supplies.munitions == {AIM_120C: 40}
    assert carriers_per_aircraft(transfer, mi8) == 1  # type: ignore[arg-type]


def test_pallets_lost_on_the_way_are_not_repacked_into_cargo() -> None:
    transfer = SimpleNamespace(
        units={"truck": 2},
        supplies=SupplyLoad({AIM_120C: 40}, tons=40, carriers=4),
    )
    transfer.size = 2

    fit_pallets(transfer, aircraft("Mi-8MT", True))  # type: ignore[arg-type]

    # Two of four trucks were lost: only the surviving 20 t is repacked.
    assert transfer.supplies.munitions == {AIM_120C: 20}
    assert transfer.units == {"truck": 5}


def test_big_transports_keep_their_pallets() -> None:
    load = SupplyLoad({AIM_120C: 40}, tons=40, carriers=4)
    transfer = SimpleNamespace(units={"truck": 4}, supplies=load)
    transfer.size = 4

    fit_pallets(transfer, aircraft("C-17A"))  # type: ignore[arg-type]

    assert transfer.supplies is load and transfer.units == {"truck": 4}


def test_airlifter_that_did_not_land_brings_its_cargo_back() -> None:
    import uuid

    from game.warehouse.state import BaseStock, WarehouseState

    settings = Settings()
    settings.logistics_enabled = True
    origin = SimpleNamespace(id=uuid.uuid4(), name="Ramat David")
    destination = SimpleNamespace(id=uuid.uuid4(), name="Akrotiri")
    game = SimpleNamespace(settings=settings)
    origin.coalition = SimpleNamespace(game=game)
    destination.coalition = origin.coalition
    truck = "pallet"
    transfer = SimpleNamespace(
        units={truck: 4},
        supplies=SupplyLoad({AIM_120C: 40}, tons=8, carriers=4),
        position=origin,
        destination=destination,
    )
    transfer.size = 4

    def kill_unit(unit_type: str) -> None:
        transfer.units[unit_type] -= 1
        transfer.size -= 1

    transfer.kill_unit = kill_unit
    state = WarehouseState()
    game.warehouse_logistics = state
    state.stocks[origin.id] = BaseStock(0.0, {})
    state.pending_plan = SimpleNamespace(cargo_units={"Herc 1": "x", "Herc 2": "x"})  # type: ignore[assignment]
    airlifts = {
        "Herc 1": SimpleNamespace(cargo=(truck, truck), transfer=transfer),
        "Herc 2": SimpleNamespace(cargo=(truck, truck), transfer=transfer),
    }
    debriefing = SimpleNamespace(
        state_data=SimpleNamespace(mission_ended=True, killed_aircraft=[]),
        unit_map=SimpleNamespace(airlift_unit=airlifts.get),
    )
    state.apply_airlift_deliveries(
        game, debriefing, {"delivered": {"Herc 1": True}}  # type: ignore[arg-type]
    )
    # Herc 2 never landed at Akrotiri: its two pallets (20 missiles) stay at the depot.
    assert transfer.size == 2
    assert state.stocks[origin.id].munitions == {AIM_120C: 20}


class FakeCoalition:
    def __init__(self, budget: float) -> None:
        self.budget = budget
        self.player = SimpleNamespace(is_blue=True)

    def adjust_budget(self, amount: float) -> None:
        self.budget += amount


def order_world(budget: float = 10.0) -> Any:
    import uuid

    from game.warehouse.state import BaseStock, WarehouseState

    coalition = FakeCoalition(budget)
    depot = SimpleNamespace(
        id=uuid.uuid4(), name="Incirlik", coalition=coalition, captured=coalition.player
    )
    settings = Settings()
    settings.logistics_enabled = True
    state = WarehouseState()
    state.stocks[depot.id] = BaseStock(0.0, {AIM_120C: 2})
    game = SimpleNamespace(
        settings=settings,
        warehouse_logistics=state,
        theater=SimpleNamespace(
            controlpoints=[depot],
            find_control_point_by_id=lambda cp_id: {depot.id: depot}[cp_id],
        ),
    )
    return game, coalition, depot


def test_player_orders_are_paid_when_placed_and_refunded_when_cut() -> None:
    from game.warehouse.supply import ordered_at, set_order

    game, coalition, depot = order_world(budget=10.0)
    assert set_order(game, depot, AIM_120C, 4) == pytest.approx(4.0)
    assert coalition.budget == pytest.approx(6.0)
    assert ordered_at(game.warehouse_logistics, depot) == {AIM_120C: 4}

    # A price change mid-turn doesn't change what a cancellation refunds.
    game.settings.logistics_munition_price_percent = 300
    assert set_order(game, depot, AIM_120C, 1) == pytest.approx(-3.0)
    assert coalition.budget == pytest.approx(9.0)

    with pytest.raises(ValueError):
        set_order(game, depot, AIM_120C, 10)  # 9 more at $3M each
    assert coalition.budget == pytest.approx(9.0)

    set_order(game, depot, AIM_120C, 0)
    assert coalition.budget == pytest.approx(10.0)
    assert game.warehouse_logistics.orders == {}


def test_orders_are_free_when_munitions_cost_nothing() -> None:
    from game.warehouse.supply import set_order

    game, coalition, depot = order_world(budget=0.0)
    game.settings.logistics_munitions_cost = False
    set_order(game, depot, AIM_120C, 50)
    assert coalition.budget == 0.0


def test_orders_arrive_at_turn_end_or_are_refunded_if_the_depot_fell() -> None:
    from game.warehouse.supply import PurchaseReport, SupplyPlanner, set_order

    game, coalition, depot = order_world(budget=10.0)
    set_order(game, depot, AIM_120C, 3)
    set_order(game, depot, MK_82, 100)
    planner = SupplyPlanner.__new__(SupplyPlanner)
    planner.game = game
    planner.coalition = coalition
    planner.state = game.warehouse_logistics
    report = PurchaseReport()
    planner.deliver_orders(report)
    stock = game.warehouse_logistics.stocks[depot.id].munitions
    assert stock == {AIM_120C: 5, MK_82: 100}
    assert report.delivered == {AIM_120C: 3, MK_82: 100}
    assert game.warehouse_logistics.orders == {}

    set_order(game, depot, AIM_120C, 2)
    budget = coalition.budget
    depot.captured = SimpleNamespace(is_blue=False)  # captured before turn end
    report = PurchaseReport()
    planner.deliver_orders(report)
    assert report.refunded == pytest.approx(2.0)
    assert coalition.budget == pytest.approx(budget + 2.0)
    assert stock[AIM_120C] == 5


def test_manual_purchasing_is_player_only() -> None:
    from game.warehouse.supply import manual_purchasing

    settings = Settings()
    assert settings.logistics_manual_munition_purchases is False
    settings.logistics_manual_munition_purchases = True
    game: Any = SimpleNamespace(settings=settings)
    assert manual_purchasing(game, FakeCoalition(0))  # type: ignore[arg-type]
    red: Any = SimpleNamespace(player=SimpleNamespace(is_blue=False))
    assert not manual_purchasing(game, red)


def test_ships_restock_interceptors_before_strike_missiles() -> None:
    from collections import Counter

    from game.warehouse.supply import SupplyPlanner

    settings = Settings()
    settings.logistics_munitions_resupply_percent = 25
    carrier: Any = SimpleNamespace(id="cv", name="CVN-74")
    planner = SupplyPlanner.__new__(SupplyPlanner)
    planner.settings = settings
    planner.depots = [carrier]
    planner.bases = [carrier]
    planner.served_by = {"cv": carrier}
    authorized: Any = {"cv": {"sam.missiles.SM_2": 100, "sam.missiles.BGM_109B": 100}}
    planner.authorized = authorized
    planner.inbound = {"cv": Counter()}
    planner.state = SimpleNamespace(  # type: ignore[assignment]
        stocks={"cv": SimpleNamespace(munitions={})}, orders={}
    )
    order = [s.resource for s in planner.suggestions()]
    assert order == ["sam.missiles.SM_2", "sam.missiles.BGM_109B"]
