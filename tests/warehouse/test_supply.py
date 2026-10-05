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
    # 10 t pallets: a C-17 takes 8, a C-130 two, a Mi-8 at least one.
    assert carriers_per_aircraft(supply, aircraft("C-17A")) == 8  # type: ignore[arg-type]
    assert carriers_per_aircraft(supply, aircraft("C-130J-30")) == 2  # type: ignore[arg-type]
    assert carriers_per_aircraft(supply, aircraft("Mi-8MT", True)) == 1  # type: ignore[arg-type]
    # Ground-unit transfers keep the old rule.
    assert carriers_per_aircraft(ground, aircraft("C-130")) == 2  # type: ignore[arg-type]
    assert carriers_per_aircraft(ground, aircraft("Mi-8MT", True)) == 1  # type: ignore[arg-type]


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
    state.pending_plan = SimpleNamespace(cargo_units={"Herc 1": "x", "Herc 2": "x"})
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
