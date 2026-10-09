"""Switching ship and convoy cargo to air delivery during the turn."""

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional

import pytest
from dcs.mapping import Point
from dcs.terrain import Caucasus

import game.transfers as transfers_module
import game.warehouse.airswitch as airswitch
import game.warehouse.supply as supply_module
from game.settings import Settings
from game.theater.player import Player
from game.transfers import TransferOrder
from game.utils import nautical_miles
from game.warehouse.state import BaseStock, SupplyShip, WarehouseState
from game.warehouse.supply import MunitionMasses, SupplyLoad

AIM_120C = "weapons.missiles.AIM_120C"
AIM_9X = "weapons.missiles.AIM_9X"
MK_82 = "weapons.bombs.Mk_82"
TERRAIN = Caucasus()


def nm_east(nm: float) -> Point:
    return Point(0, nautical_miles(nm).meters, TERRAIN)


class FakeBase:
    def __init__(self, name: str, nm: float, player: Player = Player.BLUE) -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.position = nm_east(nm)
        self.captured = player
        self.carrier = False

    def can_operate(self, aircraft: Any) -> bool:
        return aircraft.carrier_capable or not self.carrier

    def __str__(self) -> str:
        return self.name


class FakeCarrier(FakeBase):
    def __init__(self, name: str, nm: float) -> None:
        super().__init__(name, nm)
        self.carrier = True


class FakeConvoy:
    pass


def aircraft(name: str, carrier_capable: bool, helicopter: bool) -> Any:
    return SimpleNamespace(
        name=name,
        carrier_capable=carrier_capable,
        capable_of=lambda _task: True,
        dcs_unit_type=SimpleNamespace(id=name, helicopter=helicopter),
        __str__=lambda: name,
    )


C2 = aircraft("C2A_Greyhound", True, False)
CH47 = aircraft("CH-47Fbl1", True, True)
C130 = aircraft("C-130J-30", False, False)


class FakeSquadron:
    def __init__(self, aircraft: Any, location: Any, free: int) -> None:
        self.aircraft = aircraft
        self.location = location
        self.untasked_aircraft = free
        self.has_available_pilots = True

    @property
    def untasked_crewed_aircraft(self) -> int:
        return self.untasked_aircraft

    def __str__(self) -> str:
        return f"{self.aircraft.name} squadron"


class FakePlanner:
    """Stands in for AirliftPlanner: one flight takes the whole run."""

    HELO_MAX_RANGE = transfers_module.AirliftPlanner.HELO_MAX_RANGE

    def __init__(self, _game: Any, transfer: Any, next_stop: Any) -> None:
        self.transfer = transfer
        self.package = SimpleNamespace(flights=[], set_tot_asap=lambda _now: None)

    def create_airlift_flight(self, squadron: FakeSquadron) -> int:
        needed = self.transfer.size
        take = min(needed, squadron.untasked_aircraft)
        squadron.untasked_aircraft -= take
        if take < needed:
            return take  # not enough: no transport
        flight = SimpleNamespace(cargo=self.transfer, count=take)
        self.transfer.transport = SimpleNamespace(flight=flight)
        self.package.flights.append(flight)
        return take


class FakeTransfers:
    def __init__(self) -> None:
        self.pending_transfers: list[Any] = []
        self.cancelled: list[Any] = []

    def cancel_transport(self, transport: Any, transfer: Any) -> None:
        self.cancelled.append(transport)
        transfer.transport = None

    def arrange_transport(self, transfer: Any, _now: Any) -> None:
        assert not transfer.request_airflift
        transfer.transport = FakeConvoy()

    def _send_supply_route_event_stream_update(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(transfers_module, "AirliftPlanner", FakePlanner)
    monkeypatch.setattr(airswitch, "NavalControlPoint", FakeCarrier)
    monkeypatch.setattr(supply_module, "NavalControlPoint", FakeCarrier)
    monkeypatch.setattr(airswitch, "_announce_flights", lambda _flights: None)
    monkeypatch.setattr(
        airswitch, "is_by_ground", lambda t: isinstance(t.transport, FakeConvoy)
    )
    monkeypatch.setattr(
        supply_module.SupplyPlanner, "cargo_truck", lambda _self: "truck"
    )
    monkeypatch.setattr(
        supply_module.SupplyPlanner, "__init__", lambda _self, _g, _c: None
    )
    monkeypatch.setattr(
        supply_module.SupplyPlanner,
        "is_depot",
        staticmethod(lambda cp: not getattr(cp, "carrier", False)),
    )


def make_game(
    squadrons: list[FakeSquadron],
    bases: list[Any],
    stocks: dict[Any, dict[str, int]],
    authorized: dict[Any, dict[str, int]],
) -> Any:
    settings = Settings()
    settings.logistics_enabled = True
    state = WarehouseState()
    for cp, munitions in stocks.items():
        state.stocks[cp.id] = BaseStock(0.0, dict(munitions))
    state.authorized_munitions = lambda _game, cp: authorized.get(cp, {})  # type: ignore
    packages: list[Any] = []
    coalitions = {
        p: SimpleNamespace(
            player=p,
            transfers=FakeTransfers(),
            air_wing=SimpleNamespace(iter_squadrons=lambda: list(squadrons)),
        )
        for p in (Player.BLUE, Player.RED)
    }
    game: Any = SimpleNamespace(
        settings=settings,
        warehouse_logistics=state,
        turn=3,
        conditions=SimpleNamespace(start_time=datetime(2026, 1, 1)),
        theater=SimpleNamespace(
            controlpoints=bases,
            find_control_point_by_id=lambda cid: next(b for b in bases if b.id == cid),
        ),
        coalition_for=lambda p: coalitions[p],
        ato_for=lambda _p: SimpleNamespace(add_package=packages.append),
        packages=packages,
    )
    return game


def test_ship_cargo_flies_urgent_items_first_and_fuel_stays_on_board(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depot = FakeBase("Ben-Gurion", 0)
    carrier = FakeCarrier("CVN-74", 80)
    monkeypatch.setattr(airswitch, "main_base", lambda _g, _p: depot)
    # The carrier has plenty of Mk-82s but almost no AMRAAMs.
    authorized = {carrier: {AIM_120C: 40, MK_82: 40}}
    stocks = {carrier: {AIM_120C: 2, MK_82: 40}, depot: {}}
    c2 = FakeSquadron(C2, depot, free=1)  # 4.5 t: not enough for everything
    game = make_game([c2], [depot, carrier], stocks, authorized)
    ship = SupplyShip(
        carrier.id, carrier.name, "BLUE", {AIM_120C: 20, MK_82: 40}, 600_000
    )
    game.warehouse_logistics.supply_ships.append(ship)

    assert airswitch.switchable(game, ship)
    (option,) = airswitch.air_options(game, ship)
    assert option.problem is None and option.origin is depot
    assert not option.carries_all

    runs: Any = airswitch.send_by_air(game, ship, option)
    (run,) = runs
    flown = run.supplies.munitions
    assert flown[AIM_120C] == 20  # the urgent item goes first
    assert MunitionMasses.tons(flown) <= 4.5
    assert ship.fuel_kg == 600_000  # fuel stays on the ship
    assert ship.munitions[MK_82] + flown.get(MK_82, 0) == 40
    assert AIM_120C not in ship.munitions
    assert run.origin is depot and run.destination is carrier
    assert game.packages  # the transport flight is in the ATO

    # Undo: the cargo goes back on the ship and the flight is cancelled.
    assert airswitch.can_undo(game, run)
    airswitch.undo_send_by_air(game, run)
    assert ship.munitions == {AIM_120C: 20, MK_82: 40}
    transfers = game.coalition_for(Player.BLUE).transfers
    assert run not in transfers.pending_transfers
    assert transfers.cancelled


def test_a_ship_left_empty_doesnt_sail_until_undone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depot = FakeBase("Ben-Gurion", 0)
    carrier = FakeCarrier("CVN-74", 80)
    monkeypatch.setattr(airswitch, "main_base", lambda _g, _p: depot)
    game = make_game([FakeSquadron(C2, depot, 4)], [depot, carrier], {}, {})
    ship = SupplyShip(carrier.id, carrier.name, "BLUE", {AIM_9X: 4}, 0.0)
    state = game.warehouse_logistics
    state.supply_ships.append(ship)
    (run,) = airswitch.send_by_air(game, ship, airswitch.air_options(game, ship)[0])
    assert ship not in state.supply_ships
    airswitch.undo_send_by_air(game, run)
    assert ship in state.supply_ships and ship.munitions == {AIM_9X: 4}


def convoy_run(
    origin: Any, destination: Any, munitions: dict[str, int], fuel: float
) -> Any:
    tons = MunitionMasses.tons(munitions, fuel)
    trucks: Any = {"truck": 2}
    run: Any = TransferOrder(
        origin,
        destination,
        trucks,
        supplies=SupplyLoad(munitions=munitions, fuel_kg=fuel, tons=tons, carriers=2),
    )
    run.transport = FakeConvoy()
    return run


def test_convoy_munitions_fly_and_fuel_keeps_driving() -> None:
    depot = FakeBase("Tel Nof", 0)
    base = FakeBase("Ovda", 120)
    game = make_game([FakeSquadron(C130, depot, 2)], [depot, base], {}, {})
    transfers = game.coalition_for(Player.BLUE).transfers
    run = convoy_run(depot, base, {AIM_9X: 10}, 8_000.0)
    transfers.pending_transfers.append(run)

    air: Any
    (air,) = airswitch.send_by_air(game, run, airswitch.air_options(game, run)[0])
    assert air.supplies.munitions == {AIM_9X: 10}
    # The convoy still drives, with only the fuel.
    assert run in transfers.pending_transfers
    assert isinstance(run.transport, FakeConvoy)
    assert run.supplies.munitions == {} and run.supplies.fuel_kg == 8_000.0
    assert not run.request_airflift

    airswitch.undo_send_by_air(game, air)
    assert run.supplies.munitions == {AIM_9X: 10}
    assert air not in transfers.pending_transfers


def test_a_convoy_left_empty_is_called_off_until_undone() -> None:
    depot = FakeBase("Tel Nof", 0)
    base = FakeBase("Ovda", 120)
    game = make_game([FakeSquadron(C130, depot, 2)], [depot, base], {}, {})
    transfers = game.coalition_for(Player.BLUE).transfers
    run = convoy_run(depot, base, {AIM_9X: 10}, 0.0)
    transfers.pending_transfers.append(run)
    air: Any
    (air,) = airswitch.send_by_air(game, run, airswitch.air_options(game, run)[0])
    assert run not in transfers.pending_transfers
    airswitch.undo_send_by_air(game, air)
    (back,) = [t for t in transfers.pending_transfers]
    assert back.supplies.munitions == {AIM_9X: 10}
    assert isinstance(back.transport, FakeConvoy)


def test_options_explain_what_cant_fly_it(monkeypatch: pytest.MonkeyPatch) -> None:
    depot = FakeBase("Ben-Gurion", 0)
    carrier = FakeCarrier("CVN-74", 150)
    monkeypatch.setattr(airswitch, "main_base", lambda _g, _p: depot)
    squadrons = [
        FakeSquadron(C130, depot, 4),
        FakeSquadron(CH47, depot, 2),
        FakeSquadron(C2, depot, 0),
    ]
    game = make_game(squadrons, [depot, carrier], {}, {})
    ship = SupplyShip(carrier.id, carrier.name, "BLUE", {AIM_9X: 4}, 0.0)
    game.warehouse_logistics.supply_ships.append(ship)
    problems = {
        str(o.squadron.aircraft.dcs_unit_type.id): o.problem
        for o in airswitch.air_options(game, ship)
    }
    assert problems["C-130J-30"] == "can't land at CVN-74"
    assert "100 nm" in (problems["CH-47Fbl1"] or "")
    assert problems["C2A_Greyhound"] == "no aircraft free this turn"


def test_ai_flies_urgent_cargo_only(monkeypatch: pytest.MonkeyPatch) -> None:
    depot = FakeBase("Sharm", 0, Player.RED)
    base = FakeBase("Hurghada", 120, Player.RED)
    calm = FakeBase("El Minya", 140, Player.RED)
    authorized = {base: {AIM_9X: 40}, calm: {AIM_9X: 40}}
    stocks = {base: {AIM_9X: 2}, calm: {AIM_9X: 40}}
    game = make_game(
        [FakeSquadron(C130, depot, 4)], [depot, base, calm], stocks, authorized
    )
    transfers = game.coalition_for(Player.RED).transfers
    urgent = convoy_run(depot, base, {AIM_9X: 10}, 0.0)
    routine = convoy_run(depot, calm, {AIM_9X: 10}, 0.0)
    transfers.pending_transfers.extend([urgent, routine])
    red = game.coalition_for(Player.RED)
    assert airswitch.ai_send_by_air(game, red) == ["Hurghada"]
    assert routine in transfers.pending_transfers
    assert isinstance(routine.transport, FakeConvoy)


def test_ai_flies_cargo_on_a_route_that_was_hit() -> None:
    depot = FakeBase("Sharm", 0, Player.RED)
    base = FakeBase("Hurghada", 120, Player.RED)
    game = make_game([FakeSquadron(C130, depot, 4)], [depot, base], {}, {})
    transfers = game.coalition_for(Player.RED).transfers
    hit = convoy_run(depot, base, {AIM_9X: 10}, 0.0)
    hit.units["truck"] = 1  # lost a truck on the way
    transfers.pending_transfers.append(hit)
    assert airswitch.ai_send_by_air(game, game.coalition_for(Player.RED)) == [
        "Hurghada"
    ]
