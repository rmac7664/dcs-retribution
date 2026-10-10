def test_removing_replaced_flight_keeps_new_transport() -> None:
    """Changing a transport flight's squadron must not strand its cargo."""
    from types import SimpleNamespace
    from typing import Any

    from game.ato.package import Package

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
