"""Carrier replenishment ships and what a captured base keeps."""

import uuid
from collections import Counter
from types import SimpleNamespace
from typing import Any

import pytest

import game.warehouse.supply as supply_module
from game.settings import Settings
from game.warehouse.state import BaseStock, SupplyShip, WarehouseState
from game.warehouse.supply import PurchaseReport, SupplyPlanner

AIM_120C = "weapons.missiles.AIM_120C"


class FakeCarrier:
    """Stands in for NavalControlPoint (patched into the planner below)."""

    def __init__(self, name: str, side: str = "BLUE", afloat: bool = True) -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.captured = SimpleNamespace(name=side, is_neutral=False)
        self.afloat = afloat

    def runway_is_operational(self) -> bool:
        return self.afloat


def make_game(*points: Any) -> Any:
    settings = Settings()
    settings.logistics_enabled = True
    by_id = {cp.id: cp for cp in points}

    def find(cp_id: uuid.UUID) -> Any:
        return by_id[cp_id]

    game = SimpleNamespace(
        settings=settings,
        theater=SimpleNamespace(find_control_point_by_id=find),
    )
    game.warehouse_logistics = WarehouseState()
    return game


def planner_for(game: Any, *bases: Any) -> SupplyPlanner:
    planner = SupplyPlanner.__new__(SupplyPlanner)
    planner.game = game
    planner.state = game.warehouse_logistics
    planner.coalition = SimpleNamespace(player=SimpleNamespace(name="BLUE"))  # type: ignore[assignment]
    planner.inbound = {cp.id: Counter() for cp in bases}
    planner.inbound_fuel = {cp.id: 0.0 for cp in bases}
    planner.ship_cargo = {}
    return planner


@pytest.fixture(autouse=True)
def fake_naval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supply_module, "NavalControlPoint", FakeCarrier)


def test_carrier_purchases_sail_instead_of_appearing() -> None:
    carrier = FakeCarrier("CVN-73")
    game = make_game(carrier)
    state = game.warehouse_logistics
    state.stocks[carrier.id] = BaseStock(0.0, {})
    planner = planner_for(game, carrier)

    planner._stock_or_ship(carrier, {AIM_120C: 8}, fuel_kg=5000)  # type: ignore[arg-type]
    planner._stock_or_ship(carrier, {AIM_120C: 4})  # type: ignore[arg-type]
    report = PurchaseReport()
    planner.dispatch_ships(report)

    # Nothing on deck yet: one ship carries the whole turn's purchases.
    assert state.stocks[carrier.id].munitions == {}
    assert report.ships_sent == 1
    (ship,) = state.supply_ships
    assert ship.munitions == {AIM_120C: 12} and ship.fuel_kg == 5000
    # ...and it counts as inbound, so the same shortfall isn't bought twice.
    assert planner.inbound[carrier.id][AIM_120C] == 12

    arrived, lost = state.arrive_supply_ships(game, "BLUE")
    assert (arrived, lost) == (1, [])
    assert state.stocks[carrier.id].munitions == {AIM_120C: 12}
    assert state.stocks[carrier.id].jet_fuel_kg == 5000
    assert state.supply_ships == []


def test_ship_cargo_is_lost_if_the_carrier_sank() -> None:
    carrier = FakeCarrier("CVN-73", afloat=False)
    game = make_game(carrier)
    state = game.warehouse_logistics
    state.supply_ships.append(
        SupplyShip(carrier.id, carrier.name, "BLUE", {AIM_120C: 6})
    )

    arrived, lost = state.arrive_supply_ships(game, "BLUE")

    assert (arrived, lost) == (0, ["CVN-73"])
    assert carrier.id not in state.stocks


def test_each_side_only_moves_its_own_ships() -> None:
    blue, red = FakeCarrier("CVN-73"), FakeCarrier("Kuznetsov", side="RED")
    game = make_game(blue, red)
    state = game.warehouse_logistics
    state.supply_ships.append(SupplyShip(red.id, red.name, "RED", {AIM_120C: 1}))

    assert state.arrive_supply_ships(game, "BLUE") == (0, [])
    assert len(state.supply_ships) == 1


def test_land_bases_still_restock_in_place() -> None:
    airfield = SimpleNamespace(id=uuid.uuid4(), name="Incirlik")
    game = make_game(airfield)
    game.warehouse_logistics.stocks[airfield.id] = BaseStock(0.0, {})
    planner = planner_for(game, airfield)

    planner._stock_or_ship(airfield, {AIM_120C: 3})  # type: ignore[arg-type]

    assert game.warehouse_logistics.stocks[airfield.id].munitions == {AIM_120C: 3}
    assert planner.ship_cargo == {}


def airbase(side: str = "BLUE") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name="Incirlik",
        captured=SimpleNamespace(is_neutral=side == "NEUTRAL"),
    )


def test_capture_destroys_munitions_and_halves_fuel() -> None:
    base = airbase()
    game = make_game(base)
    state = game.warehouse_logistics
    state.stocks[base.id] = BaseStock(80_000.0, {AIM_120C: 40})

    state.on_capture(game, base)

    assert state.stocks[base.id].munitions == {}
    assert state.stocks[base.id].jet_fuel_kg == pytest.approx(40_000)


def test_captured_unstocked_base_is_not_a_free_full_warehouse() -> None:
    base = airbase()
    game = make_game(base)
    state = game.warehouse_logistics
    full = state.fuel_capacity_kg(base, game.settings)

    state.on_capture(game, base)

    assert state.stocks[base.id].munitions == {}
    assert state.stocks[base.id].jet_fuel_kg == pytest.approx(full / 2)


def test_capturing_a_neutral_base_finds_it_empty() -> None:
    base = airbase("NEUTRAL")
    game = make_game(base)

    game.warehouse_logistics.on_capture(game, base)

    assert game.warehouse_logistics.stocks[base.id].jet_fuel_kg == 0


def test_capture_does_nothing_with_logistics_off() -> None:
    base = airbase()
    game = make_game(base)
    game.settings.logistics_enabled = False

    game.warehouse_logistics.on_capture(game, base)

    assert game.warehouse_logistics.stocks == {}
