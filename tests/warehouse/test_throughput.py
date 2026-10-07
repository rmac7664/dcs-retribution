"""Cargo handling limits: a base takes in only so much supply per turn."""

import uuid
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

import game.warehouse.supply as supply_module
from game.settings import Settings
from game.warehouse.state import BaseStock, WarehouseState
from game.warehouse.supply import PurchaseReport, SupplyPlanner, report_lines

MK_82 = "weapons.bombs.Mk_82"  # 241 kg each


class FakeAirfield:
    def __init__(self, name: str) -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.captured = SimpleNamespace(name="BLUE", is_neutral=False, is_blue=True)


def fob(name: str) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        captured=SimpleNamespace(name="BLUE", is_neutral=False, is_blue=True),
    )


@pytest.fixture(autouse=True)
def fake_airfield(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supply_module, "Airfield", FakeAirfield)


def plan(dest: Any, in_transit_tons: float = 0.0, limit: int = 60) -> Any:
    depot: Any = FakeAirfield("Vaziani")
    settings = Settings()
    settings.logistics_enabled = True
    settings.logistics_fuel_by_supply_lines = False
    settings.logistics_base_throughput_tons = limit
    state = WarehouseState()
    state.stocks[depot.id] = BaseStock(0.0, {MK_82: 1000})
    state.stocks[dest.id] = BaseStock(0.0, {})
    transfers = SimpleNamespace(pending_transfers=[])
    planner = SupplyPlanner.__new__(SupplyPlanner)
    planner.game = SimpleNamespace(settings=settings)  # type: ignore[assignment]
    planner.settings = settings
    planner.state = state
    planner.coalition = SimpleNamespace(transfers=transfers)  # type: ignore[assignment]
    planner.bases = [depot, dest]
    planner.served_by = {depot.id: depot, dest.id: depot}
    planner.authorized = {depot.id: {}, dest.id: {MK_82: 500}}
    planner.inbound = {cp.id: Counter() for cp in planner.bases}
    planner.inbound_fuel = {cp.id: 0.0 for cp in planner.bases}
    planner.inbound_tons = {depot.id: 0.0, dest.id: in_transit_tons}
    planner.cargo_truck = lambda: "truck"  # type: ignore[method-assign,assignment,return-value]
    report = PurchaseReport()
    planner.distribute(report)
    return SimpleNamespace(
        report=report, runs=transfers.pending_transfers, settings=settings
    )


def tons(run: Any) -> float:
    return run.supplies.tons


def test_an_airfield_takes_a_full_convoy() -> None:
    (run,) = plan(FakeAirfield("Senaki")).runs
    assert 55 < tons(run) <= 60


def test_a_fob_handles_a_third() -> None:
    result = plan(fob("FOB Alpha"))
    (run,) = result.runs
    assert 15 < tons(run) <= 20
    assert "At their cargo handling limit: FOB Alpha." in report_lines(
        result.report, result.settings
    )


def test_cargo_on_its_way_counts_against_the_limit() -> None:
    (run,) = plan(FakeAirfield("Senaki"), in_transit_tons=50).runs
    assert tons(run) <= 10
    result = plan(FakeAirfield("Senaki"), in_transit_tons=60)
    assert result.runs == []
    assert result.report.throughput_limited == ["Senaki"]


def test_zero_means_no_limit() -> None:
    (run,) = plan(fob("FOB Alpha"), in_transit_tons=500, limit=0).runs
    assert tons(run) > 55
