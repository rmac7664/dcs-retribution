"""A carrier's replenishment ship carries its whole shortfall, fuel included."""

import uuid
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

import game.warehouse.supply as supply_module
from game.settings import Settings
from game.warehouse.state import BaseStock, WarehouseState
from game.warehouse.supply import PurchaseReport, SupplyPlanner

MK_82 = "weapons.bombs.Mk_82"  # 241 kg each
CAPACITY_KG = {"Ramstein": 1_500_000.0, "Independence": 2_500_000.0}


class FakeAirfield:
    def __init__(self, name: str) -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.captured = SimpleNamespace(name="BLUE", is_neutral=False, is_blue=True)


class FakeCarrier(FakeAirfield):
    pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supply_module, "Airfield", FakeAirfield)
    monkeypatch.setattr(supply_module, "NavalControlPoint", FakeCarrier)
    monkeypatch.setattr(
        WarehouseState,
        "fuel_capacity_kg",
        lambda self, cp, settings: CAPACITY_KG[cp.name],
    )


def plan(air_only: bool = False) -> Any:
    depot: Any = FakeAirfield("Ramstein")
    carrier: Any = FakeCarrier("Independence")
    settings = Settings()
    settings.logistics_enabled = True
    settings.logistics_fuel_by_supply_lines = True
    state = WarehouseState()
    # The CW Germany turn 7 numbers: carrier at 619 t of 2,500 t fuel and short
    # of munitions; Ramstein holds 2,960 t against a 1,500 t capacity.
    state.stocks[depot.id] = BaseStock(2_960_000.0, {MK_82: 2000})
    state.stocks[carrier.id] = BaseStock(618_900.0, {})
    transfers = SimpleNamespace(pending_transfers=[])
    planner = SupplyPlanner.__new__(SupplyPlanner)
    planner.game = SimpleNamespace(settings=settings)  # type: ignore[assignment]
    planner.settings = settings
    planner.state = state
    planner.coalition = SimpleNamespace(  # type: ignore[assignment]
        transfers=transfers, player=SimpleNamespace(name="BLUE")
    )
    planner.bases = [depot, carrier]
    planner.served_by = {depot.id: depot, carrier.id: depot}
    planner.authorized = {depot.id: {}, carrier.id: {MK_82: 600}}
    planner.inbound = {cp.id: Counter() for cp in planner.bases}
    planner.inbound_fuel = {cp.id: 0.0 for cp in planner.bases}
    planner.inbound_tons = {cp.id: 0.0 for cp in planner.bases}
    planner.ship_cargo = {}
    planner.air_only = {carrier.id} if air_only else set()
    planner.cargo_truck = lambda: "truck"  # type: ignore[method-assign,assignment,return-value]
    planner.distribute(PurchaseReport())
    return SimpleNamespace(
        ship=planner.ship_cargo.get(carrier.id),
        runs=transfers.pending_transfers,
        depot=state.stocks[depot.id],
    )


def test_ship_carries_munitions_and_fuel() -> None:
    ship = plan().ship
    assert ship is not None
    # All 600 bombs (145 t), well past one 60 t convoy...
    assert ship.munitions == {MK_82: 600}
    # ...and Ramstein's spare fuel: 2,960 t - 1,500 t kept = 1,460 t.
    assert ship.fuel_kg == pytest.approx(1_460_000.0)


def test_main_base_keeps_its_own_fuel() -> None:
    result = plan()
    assert result.depot.jet_fuel_kg == pytest.approx(1_500_000.0)


def test_air_only_carrier_still_gets_one_aircraft_run() -> None:
    result = plan(air_only=True)
    assert result.ship is None
    (run,) = result.runs
    assert run.supplies.tons <= 60.5
