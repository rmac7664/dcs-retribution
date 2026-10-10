"""Changing a transport flight's squadron keeps its cargo moving."""

from types import SimpleNamespace
from typing import Any

from game.ato.package import Package
from game.transfers import PendingTransfers, TransferOrder


def test_removing_replaced_flight_keeps_new_transport() -> None:
    new_flight: Any = object()
    old_flight: Any = SimpleNamespace(id=1, return_pilots_and_aircraft=lambda: None)
    cargo: Any = SimpleNamespace(transport=SimpleNamespace(flight=new_flight))
    old_flight.cargo = cargo
    package: Any = SimpleNamespace(
        flights=[old_flight], _db=SimpleNamespace(remove=lambda _id: None)
    )
    Package.remove_flight(package, old_flight)
    assert cargo.transport is not None

    cargo.transport = SimpleNamespace(flight=old_flight)
    package.flights = [old_flight]
    Package.remove_flight(package, old_flight)
    assert cargo.transport is None


def test_split_part_stays_where_the_cargo_is() -> None:
    origin: Any = SimpleNamespace(name="A", captured="BLUE")
    middle: Any = SimpleNamespace(name="B", captured="BLUE")
    destination: Any = SimpleNamespace(name="C", captured="BLUE")
    unit: Any = "tank"
    transfer = TransferOrder(origin, destination, {unit: 4})
    transfer.position = middle
    pending = PendingTransfers.__new__(PendingTransfers)
    pending.pending_transfers = [transfer]
    part = pending.split_transfer(transfer, 1)
    assert part.position is middle
    assert part.size == 1 and transfer.size == 3
