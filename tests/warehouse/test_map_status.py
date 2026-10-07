"""Supply levels and supply runs on the campaign map."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from game.server.supplyroutes.models import TransportFinder
from game.settings import Settings
from game.warehouse.state import BaseStock, SupplyShip, WarehouseState
from game.warehouse.status import supply_status
from game.warehouse.supply import SupplyLoad

AIM_120C = "weapons.missiles.AIM_120C"


def setup(blue: bool = True) -> Any:
    settings = Settings()
    settings.logistics_enabled = True
    state = WarehouseState()
    depot = SimpleNamespace(id=uuid.uuid4(), name="Vaziani")
    run = SimpleNamespace(
        origin=depot, supplies=SupplyLoad({AIM_120C: 6}, tons=1.0, carriers=1)
    )
    cp = SimpleNamespace(
        id=uuid.uuid4(),
        name="Senaki",
        captured=SimpleNamespace(is_blue=blue),
        coalition=SimpleNamespace(transfers=SimpleNamespace(pending_transfers=[run])),
    )
    run.destination = cp
    state.stocks[cp.id] = BaseStock(500_000.0, {AIM_120C: 5})
    state.supply_ships.append(SupplyShip(cp.id, cp.name, "BLUE"))
    state.is_managed = lambda _cp, _s: True  # type: ignore[method-assign,assignment]
    state.authorized_munitions = lambda _g, _cp: {AIM_120C: 20}  # type: ignore[method-assign,assignment]
    state.fuel_capacity_kg = lambda _cp, _s: 1_000_000.0  # type: ignore[method-assign,assignment]
    game = SimpleNamespace(settings=settings, warehouse_logistics=state)
    return game, cp


def test_a_bases_supply_situation(monkeypatch: pytest.MonkeyPatch) -> None:
    from game.warehouse.supply import SupplyPlanner

    monkeypatch.setattr(SupplyPlanner, "is_depot", staticmethod(lambda _cp: False))
    game, cp = setup()

    status = supply_status(game, cp)

    assert status is not None
    assert (status.munitions_percent, status.fuel_percent, status.level) == (25, 50, 25)
    assert status.inbound_runs == 2  # a supply run and a replenishment ship
    assert status.lines == [
        "Munitions 25% of authorized",
        "Fuel 50% (500 t)",
        "Inbound from Vaziani: 6 munitions (1.0 t)",
        "1 replenishment ship(s) at sea",
    ]


def test_enemy_stock_is_not_shown() -> None:
    game, cp = setup(blue=False)
    assert supply_status(game, cp) is None


def test_supply_convoys_say_what_they_carry() -> None:
    supply = SimpleNamespace(
        supplies=SupplyLoad({AIM_120C: 6}, tons=1.0, carriers=1), size=1
    )
    tanks = SimpleNamespace(supplies=None, size=3)
    convoy: Any = SimpleNamespace(
        transfers=[supply, tanks], size=4, origin="Vaziani", destination="Senaki"
    )
    finder = TransportFinder.__new__(TransportFinder)
    finder.find_transports = lambda _sea: [convoy]  # type: ignore[method-assign,assignment]
    assert finder.describe_active_transports(False) == [
        "Supply run of 6 munitions (1.0 t) from Vaziani to Senaki",
        "3 units transferring from Vaziani to Senaki",
    ]
