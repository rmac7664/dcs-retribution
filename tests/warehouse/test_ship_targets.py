"""The AI can plan anti-ship strikes on replenishment ships sailing to carriers."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from dcs.mapping import Point
from dcs.terrain import Caucasus

import game.warehouse.targets as targets_module
from game.ato import FlightType
from game.settings import Settings
from game.theater.player import Player
from game.utils import nautical_miles
from game.warehouse.state import SupplyShip, WarehouseState
from game.warehouse.supply import PurchaseReport, SupplyPlanner
from game.warehouse.targets import enemy_replenishment_ships, ship_group_name

TERRAIN = Caucasus()


class FakeCarrier:
    def __init__(self, name: str, side: Player, afloat: bool = True) -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.captured = side
        self.position = Point(0, 0, TERRAIN)
        self.afloat = afloat
        self.ground_objects: list[Any] = []

    def runway_is_operational(self) -> bool:
        return self.afloat


@pytest.fixture(autouse=True)
def fake_naval(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(targets_module, "NavalControlPoint", FakeCarrier)


def make_game(*carriers: FakeCarrier, disabled: bool = False) -> Any:
    settings = Settings()
    settings.logistics_enabled = True
    settings.perf_disable_replenishment_ships = disabled
    enemy_base = SimpleNamespace(
        position=Point(0, -nautical_miles(100).meters, TERRAIN), captured=Player.BLUE
    )
    by_id = {c.id: c for c in carriers}
    return SimpleNamespace(
        settings=settings,
        warehouse_logistics=WarehouseState(),
        theater=SimpleNamespace(
            controlpoints=[*carriers, enemy_base],
            find_control_point_by_id=lambda cp_id: by_id[cp_id],
            is_in_sea=lambda _p: True,
        ),
    )


def ship_for(carrier: FakeCarrier, name: str = "Kuznetsov replenishment 3") -> Any:
    ship = SupplyShip(carrier.id, carrier.name, carrier.captured.name, {"x": 4}, 0)
    ship.name = name
    return ship


def test_blue_sees_reds_ships_but_not_its_own() -> None:
    kuznetsov = FakeCarrier("Kuznetsov", Player.RED)
    lincoln = FakeCarrier("Lincoln", Player.BLUE)
    lincoln.position = Point(0, -nautical_miles(200).meters, TERRAIN)
    sunk = FakeCarrier("Sunk", Player.RED, afloat=False)
    game = make_game(kuznetsov, lincoln, sunk)
    red_ship = ship_for(kuznetsov)
    game.warehouse_logistics.supply_ships = [
        red_ship,
        ship_for(lincoln, "Lincoln replenishment 1"),
        ship_for(sunk, "Sunk replenishment 2"),
    ]

    (target,) = enemy_replenishment_ships(game, Player.BLUE)

    assert target.ship is red_ship and target.carrier is kuznetsov
    assert target.group_name == "Kuznetsov replenishment 3"
    assert not target.is_friendly(Player.BLUE) and target.is_friendly(Player.RED)
    assert FlightType.ANTISHIP in list(target.mission_types(Player.BLUE))
    assert list(target.mission_types(Player.RED)) == []
    # Expected early in its transit, 90% of the 70 nm start distance out, on the
    # carrier's far side from blue's base (which is to the west).
    assert target.position.y == pytest.approx(nautical_miles(63).meters, rel=1e-3)


def test_no_targets_when_ships_are_off_the_map() -> None:
    kuznetsov = FakeCarrier("Kuznetsov", Player.RED)
    game = make_game(kuznetsov, disabled=True)
    game.warehouse_logistics.supply_ships = [ship_for(kuznetsov)]
    assert list(enemy_replenishment_ships(game, Player.BLUE)) == []


def test_ships_get_unique_names_when_they_sail() -> None:
    carrier = FakeCarrier("Lincoln", Player.BLUE)
    planner = SupplyPlanner.__new__(SupplyPlanner)
    planner.state = WarehouseState()
    first = SupplyShip(carrier.id, carrier.name, "BLUE", {"x": 1})
    second = SupplyShip(carrier.id, carrier.name, "BLUE", {"x": 1})
    planner.ship_cargo = {1: first, 2: second}
    planner.dispatch_ships(PurchaseReport())
    assert [s.name for s in planner.state.supply_ships] == [
        "Lincoln replenishment 1",
        "Lincoln replenishment 2",
    ]
    # Ships at sea in an older save have no name yet.
    old = SupplyShip(carrier.id, carrier.name, "BLUE")
    del old.__dict__["name"]
    assert ship_group_name(old) == "Lincoln replenishment"


def test_anti_ship_flights_are_told_which_group_to_attack() -> None:
    from dcs import Mission
    from dcs.point import MovingPoint
    from dcs.task import AttackGroup

    from game.missiongenerator.aircraft.waypoints.antishipingress import (
        AntiShipIngressBuilder,
    )
    from game.warehouse.targets import ReplenishmentShipTarget

    import dcs.ships

    mission = Mission(TERRAIN)
    russia = mission.country("Russia")
    ship_group = mission.ship_group(
        russia,
        "Kuznetsov replenishment 3",
        dcs.ships.ELNYA,
        position=Point(0, 0, TERRAIN),
    )
    kuznetsov = FakeCarrier("Kuznetsov", Player.RED)
    target = ReplenishmentShipTarget(
        ship_for(kuznetsov), kuznetsov, Point(0, 0, TERRAIN)  # type: ignore[arg-type]
    )
    builder = AntiShipIngressBuilder.__new__(AntiShipIngressBuilder)
    builder.package = SimpleNamespace(target=target)  # type: ignore[assignment]
    builder.mission = mission
    builder.flight = SimpleNamespace(client_count=0)  # type: ignore[assignment]
    waypoint = MovingPoint(Point(0, 0, TERRAIN))

    builder.add_tasks(waypoint)

    attacks = [t for t in waypoint.tasks if isinstance(t, AttackGroup)]
    assert attacks and {t.params["groupId"] for t in attacks} == {ship_group.id}


def test_ships_at_sea_in_older_saves_are_named_on_load() -> None:
    import pickle

    carrier = FakeCarrier("Lincoln", Player.BLUE)
    state = WarehouseState()
    old = SupplyShip(carrier.id, carrier.name, "BLUE", {"x": 1})
    del old.__dict__["name"]
    state.supply_ships.append(old)
    del state.__dict__["ship_serial"]

    loaded = pickle.loads(pickle.dumps(state))

    assert [s.name for s in loaded.supply_ships] == ["Lincoln replenishment 1"]
    assert loaded.ship_serial == 1


def test_the_planner_knows_whose_ship_it_is() -> None:
    kuznetsov: Any = FakeCarrier("Kuznetsov", Player.RED)
    kuznetsov.coalition = "red coalition"
    game = make_game(kuznetsov)
    game.warehouse_logistics.supply_ships = [ship_for(kuznetsov)]
    (target,) = enemy_replenishment_ships(game, Player.BLUE)
    assert target.coalition == "red coalition"


class FakeEscorts:
    """A carrier group's warships, with an umbrella covering the ship's approach."""

    def __init__(self, at: Point) -> None:
        self.position = at
        self.is_dead = False
        self.name = "KOMODO"

    def max_threat_range(self) -> Any:
        return nautical_miles(90)


def protected_world(monkeypatch: pytest.MonkeyPatch, stock: int) -> Any:
    import game.theater.theatergroundobject as tgo_module
    import game.warehouse.sam as sam_module

    monkeypatch.setattr(tgo_module, "NavalGroundObject", FakeEscorts)
    lincoln = FakeCarrier("Lincoln", Player.BLUE)
    lincoln.ground_objects = [FakeEscorts(lincoln.position)]
    game = make_game(lincoln)
    game.settings.logistics_limited_sam_missiles = True
    state = game.warehouse_logistics
    state.supply_ships = [ship_for(lincoln, "Lincoln replenishment 1")]
    from game.warehouse.state import BaseStock

    state.stocks[lincoln.id] = BaseStock(0.0, {"sam.missiles.SM_2": stock})
    monkeypatch.setattr(sam_module, "seed_new_missiles", lambda _game: None)
    monkeypatch.setattr(
        sam_module,
        "authorized_sam",
        lambda _game, _cp: {"sam.missiles.SM_2": 100, "sam.missiles.BGM_109B": 50},
    )
    return game


def test_supply_ships_under_a_well_stocked_umbrella_are_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = protected_world(monkeypatch, stock=80)
    assert list(enemy_replenishment_ships(game, Player.RED)) == []


def test_supply_ships_are_worth_it_once_the_magazines_run_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = protected_world(monkeypatch, stock=20)  # 20% of its interceptors
    (target,) = enemy_replenishment_ships(game, Player.RED)
    assert target.carrier.name == "Lincoln"


def test_an_unescorted_supply_ship_is_fair_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = protected_world(monkeypatch, stock=100)
    lincoln = game.theater.controlpoints[0]
    lincoln.ground_objects[0].is_dead = True  # escorts sunk
    assert len(list(enemy_replenishment_ships(game, Player.RED))) == 1
