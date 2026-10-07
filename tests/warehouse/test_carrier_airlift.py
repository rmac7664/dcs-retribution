"""Carrier onboard delivery: urgent top-ups flown to carriers from land depots."""

import uuid
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest
from dcs.mapping import Point
from dcs.terrain import Caucasus

import game.warehouse.supply as supply_module
from game.settings import Settings
from game.utils import nautical_miles
from game.warehouse.state import BaseStock, WarehouseState
from game.warehouse.supply import PurchaseReport, SupplyPlanner, report_lines

AIM_120C = "weapons.missiles.AIM_120C"
AIM_9X = "weapons.missiles.AIM_9X"
TERRAIN = Caucasus()


def nm_east(nm: float) -> Point:
    return Point(0, nautical_miles(nm).meters, TERRAIN)


class FakeCarrier:
    def __init__(self, nm: float) -> None:
        self.id = uuid.uuid4()
        self.name = "CVN-73"
        self.position = nm_east(nm)
        self.captured = SimpleNamespace(name="BLUE", is_neutral=False, is_blue=True)

    def can_operate(self, aircraft: Any) -> bool:
        return aircraft.carrier_capable


def land(name: str, nm: float) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        position=nm_east(nm),
        captured=SimpleNamespace(name="BLUE", is_neutral=False, is_blue=True),
        can_operate=lambda _aircraft: True,
    )


def aircraft(dcs_id: str, carrier_capable: bool, helicopter: bool) -> Any:
    return SimpleNamespace(
        carrier_capable=carrier_capable,
        capable_of=lambda _task: True,
        dcs_unit_type=SimpleNamespace(id=dcs_id, helicopter=helicopter),
    )


C2 = aircraft("C2A_Greyhound", True, False)
CH47 = aircraft("CH-47Fbl1", True, True)
C130 = aircraft("C-130J-30", False, False)


@pytest.fixture(autouse=True)
def fake_naval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supply_module, "NavalControlPoint", FakeCarrier)


def setup(squadrons: list[tuple[Any, Any]], carrier_nm: float = 80) -> Any:
    carrier = FakeCarrier(carrier_nm)
    near, far = land("Batumi", 0), land("Kobuleti", -150)
    settings = Settings()
    settings.logistics_enabled = True
    state = WarehouseState()
    state.stocks[carrier.id] = BaseStock(0.0, {AIM_120C: 2})
    state.stocks[near.id] = BaseStock(0.0, {AIM_120C: 30, AIM_9X: 4})
    state.stocks[far.id] = BaseStock(0.0, {AIM_120C: 30, AIM_9X: 30})
    transfers = SimpleNamespace(pending_transfers=[])
    coalition = SimpleNamespace(
        player=SimpleNamespace(name="BLUE"),
        transfers=transfers,
        air_wing=SimpleNamespace(
            iter_squadrons=lambda: [
                SimpleNamespace(
                    aircraft=a, location=home, can_auto_assign=lambda _task: True
                )
                for a, home in squadrons
            ]
        ),
        faction=SimpleNamespace(logistics_units=[]),
    )
    planner = SupplyPlanner.__new__(SupplyPlanner)
    planner.game = SimpleNamespace(settings=settings)  # type: ignore[assignment]
    planner.settings = settings
    planner.state = state
    planner.coalition = coalition  # type: ignore[assignment]
    planner.bases = [carrier, near, far]  # type: ignore[list-item]
    planner.depots = [carrier, near, far]  # type: ignore[list-item]
    planner.authorized = {
        carrier.id: {AIM_120C: 12, AIM_9X: 8},
        near.id: {AIM_120C: 20},
        far.id: {},
    }
    planner.inbound = {cp.id: Counter() for cp in planner.bases}
    planner.inbound_fuel = {cp.id: 0.0 for cp in planner.bases}
    planner.cargo_truck = lambda: "truck"  # type: ignore[method-assign,assignment,return-value]
    report = PurchaseReport()
    planner.airlift_to_carriers(report)
    return SimpleNamespace(
        planner=planner,
        report=report,
        state=state,
        carrier=carrier,
        near=near,
        far=far,
        transfers=transfers.pending_transfers,
    )


def test_spare_stock_ashore_is_flown_to_the_carrier() -> None:
    r = setup([(C2, land("Home", 0))])

    (transfer,) = r.transfers
    assert transfer.origin is r.near and transfer.destination is r.carrier
    assert transfer.request_airflift
    # Batumi keeps its own 20 AMRAAMs: 10 spare go out, and all 4 of its AIM-9X.
    assert transfer.supplies.munitions == {AIM_120C: 10, AIM_9X: 4}
    assert r.state.stocks[r.near.id].munitions == {AIM_120C: 20, AIM_9X: 0}
    # Counted as inbound, so the ship isn't loaded with the same missiles.
    assert r.planner.inbound[r.carrier.id] == Counter({AIM_120C: 10, AIM_9X: 4})
    assert r.report.cod_runs == 1
    assert "1 onboard delivery run(s) sent to carriers." in report_lines(
        r.report, r.planner.settings
    )


def test_no_carrier_capable_transport_means_no_delivery() -> None:
    r = setup([(C130, land("Home", 0))])
    assert r.transfers == []


def test_helicopters_only_reach_depots_within_range() -> None:
    # The carrier is 130 nm out: a CH-47 can't make that leg.
    r = setup([(CH47, land("Home", 0))], carrier_nm=130)
    assert r.transfers == []
    r = setup([(CH47, land("Home", 0))], carrier_nm=60)
    assert len(r.transfers) == 1


def test_one_run_at_a_time() -> None:
    r = setup([(C2, land("Home", 0))])
    r.planner.airlift_to_carriers(r.report)
    assert len(r.transfers) == 1
