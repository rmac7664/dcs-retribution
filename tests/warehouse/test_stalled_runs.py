"""Supply runs nothing can carry: other routes, then called off."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import game.transfers as transfers_module
from game.settings import Settings
from game.theater.transitnetwork import TransitConnection
from game.transfers import PendingTransfers
from game.warehouse.state import BaseStock, WarehouseState
from game.warehouse.supply import (
    STALL_LIMIT,
    PurchaseReport,
    SupplyLoad,
    cancel_stalled_runs,
    report_lines,
)

AIM_120C = "weapons.missiles.AIM_120C"


def world() -> Any:
    settings = Settings()
    settings.logistics_enabled = True
    game: Any = SimpleNamespace(settings=settings, warehouse_logistics=WarehouseState())

    def base(name: str) -> Any:
        cp = SimpleNamespace(
            id=uuid.uuid4(),
            name=name,
            captured=SimpleNamespace(is_neutral=False, is_blue=False),
            coalition=SimpleNamespace(game=game),
        )
        game.warehouse_logistics.stocks[cp.id] = BaseStock(0.0)
        return cp

    return game, base("Ushuaia Helo Port"), base("Ushuaia")


def run(origin: Any, destination: Any, stalled: int) -> Any:
    return SimpleNamespace(
        origin=origin,
        destination=destination,
        position=origin,
        units={"truck": 2},
        size=2,
        supplies=SupplyLoad({AIM_120C: 10}, tons=1, carriers=2),
        stalled_turns=stalled,
        transport=None,
    )


def test_runs_stuck_too_long_are_called_off_and_unloaded() -> None:
    game, depot, base = world()
    stuck, waiting = run(depot, base, STALL_LIMIT), run(depot, base, 1)
    coalition: Any = SimpleNamespace(
        transfers=SimpleNamespace(pending_transfers=[stuck, waiting])
    )

    cancelled = cancel_stalled_runs(coalition)

    assert cancelled == ["Ushuaia Helo Port to Ushuaia"]
    assert coalition.transfers.pending_transfers == [waiting]
    assert game.warehouse_logistics.stocks[depot.id].munitions == {AIM_120C: 10}
    report = PurchaseReport(cancelled_runs=cancelled)
    assert any("called off" in line for line in report_lines(report, game.settings))


class FakeNetwork:
    def __init__(self, link: TransitConnection) -> None:
        self.link = link

    def shortest_path_between(self, a: Any, b: Any) -> list[Any]:
        return [b]

    def link_type(self, a: Any, b: Any) -> TransitConnection:
        return self.link


@pytest.mark.parametrize(
    "link,expected",
    [
        (TransitConnection.Road, "convoy"),
        (TransitConnection.Shipping, "ship"),
        (TransitConnection.Airlift, None),
    ],
)
def test_runs_meant_for_the_air_take_the_road_if_no_aircraft_can(
    monkeypatch: pytest.MonkeyPatch, link: TransitConnection, expected: Any
) -> None:
    game, depot, base = world()
    order = run(depot, base, 0)
    order.request_airflift = True
    monkeypatch.setattr(
        transfers_module,
        "AirliftPlanner",
        lambda *_a: SimpleNamespace(create_package_for_airlift=lambda _now: None),
    )
    monkeypatch.setattr("game.warehouse.supply.load_into_trucks", lambda *_a: None)
    pending = PendingTransfers.__new__(PendingTransfers)
    pending.game = game
    sent: list[str] = []
    pending.convoys = SimpleNamespace(add=lambda t, _s: sent.append("convoy"))  # type: ignore[assignment]
    pending.cargo_ships = SimpleNamespace(add=lambda t, _s: sent.append("ship"))  # type: ignore[assignment]
    pending.network_for = lambda _cp: FakeNetwork(link)  # type: ignore[method-assign,assignment,return-value]

    pending.arrange_transport(order, None)  # type: ignore[arg-type]

    assert sent == ([expected] if expected else [])
