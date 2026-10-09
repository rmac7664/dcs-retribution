"""The main supply base: who may be one, locking it in, and re-picking when lost."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from dcs.mapping import Point
from dcs.terrain import Caucasus

import game.warehouse.supply as supply
from game.theater.player import Player

NM = 1852.0


class FakeBase:
    def __init__(
        self, name: str, x_nm: float, owner: Player, started: Player | None = None
    ) -> None:
        self.id = uuid.uuid4()
        self.name = name
        self.position = Point(x_nm * NM, 0, Caucasus())
        self.captured = owner
        self.starting_coalition = started or owner
        self.has_active_frontline = False
        self.runway_ok = True

    def runway_is_operational(self) -> bool:
        return self.runway_ok


class FakeAirfield(FakeBase):
    pass


class FakeShip(FakeBase):
    pass


class FakeFob(FakeBase):
    pass


class FakeOffMap(FakeBase):
    pass


@pytest.fixture(autouse=True)
def fake_types(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(supply, "Airfield", FakeAirfield)
    monkeypatch.setattr(supply, "NavalControlPoint", FakeShip)
    monkeypatch.setattr(supply, "Fob", FakeFob)
    monkeypatch.setattr(supply, "OffMapSpawn", FakeOffMap)


def make_game(points: list[Any]) -> Any:
    messages: list[tuple[str, str]] = []
    return SimpleNamespace(
        theater=SimpleNamespace(controlpoints=points),
        warehouse_logistics=SimpleNamespace(main_bases={}, revealed_main_bases=set()),
        blue=SimpleNamespace(player=Player.BLUE, ato=SimpleNamespace(packages=[])),
        red=SimpleNamespace(player=Player.RED),
        messages=messages,
        message=lambda title, text="": messages.append((title, text)),
    )


BLUE, RED = Player.BLUE, Player.RED


def test_options_are_the_safer_half_held_since_the_start() -> None:
    a: Any = FakeAirfield("A", 0, BLUE)
    b: Any = FakeAirfield("B", 40, BLUE)
    c: Any = FakeAirfield("C", 80, BLUE)
    captured: Any = FakeAirfield("Captured", -10, BLUE, started=RED)
    closed: Any = FakeAirfield("Closed", -20, BLUE)
    closed.runway_ok = False
    off_map: Any = FakeOffMap("Rear", -500, BLUE)
    enemy: Any = FakeAirfield("Enemy", 120, RED)
    enemy_off_map: Any = FakeOffMap("Enemy rear", 900, RED)
    game = make_game([a, b, c, captured, closed, off_map, enemy, enemy_off_map])

    # Three eligible airfields -> the safer two (ceil of half), farthest first.
    assert supply.main_base_options(game, BLUE) == [a, b]
    assert supply.main_base(game, BLUE) is a


def test_carrier_then_fob_when_no_airfield() -> None:
    fob: Any = FakeFob("FOB", 0, BLUE)
    ship: Any = FakeShip("CVN", 10, BLUE)
    enemy: Any = FakeAirfield("Enemy", 100, RED)
    game = make_game([fob, ship, enemy])
    assert supply.main_base_options(game, BLUE) == [ship]
    ship.runway_ok = False  # sunk
    assert supply.main_base_options(game, BLUE) == [fob]


def test_side_that_lost_its_starting_bases_still_gets_one() -> None:
    taken: Any = FakeAirfield("Taken", 0, BLUE, started=RED)
    enemy: Any = FakeAirfield("Enemy", 100, RED)
    game = make_game([taken, enemy])
    assert supply.main_base(game, BLUE) is taken


def test_warnings_for_front_line_and_nearby_enemy_airfield() -> None:
    near: Any = FakeAirfield("Near", 0, BLUE)
    far: Any = FakeAirfield("Far", -200, BLUE)
    enemy: Any = FakeAirfield("Enemy", 30, RED)
    game = make_game([near, far, enemy])
    assert supply.main_base_warnings(game, far) == []
    near.has_active_frontline = True
    warnings = supply.main_base_warnings(game, near)
    assert warnings[0] == "it is on a front line"
    assert "30 nm from the enemy airfield Enemy" in warnings[1]


def test_pick_is_locked_and_survives_the_front_moving() -> None:
    a: Any = FakeAirfield("A", 0, BLUE)
    b: Any = FakeAirfield("B", 40, BLUE)
    c: Any = FakeAirfield("C", 80, BLUE)
    enemy: Any = FakeAirfield("Enemy", 120, RED)
    game = make_game([a, b, c, enemy])
    supply.set_main_base(game, BLUE, b)
    assert supply.update_main_bases(game) == []
    assert supply.main_base(game, BLUE) is b
    assert supply.main_base(game, RED) is enemy
    assert game.warehouse_logistics.main_bases[RED.name] == enemy.id

    # The enemy closes in so B is no longer in the safer half: it stays the base.
    enemy.position = Point(45 * NM, 0, Caucasus())
    assert b not in supply.main_base_options(game, BLUE)
    assert supply.main_base(game, BLUE) is b
    # A damaged runway doesn't lose it either.
    b.runway_ok = False
    assert supply.main_base(game, BLUE) is b


def test_lost_main_base_is_replaced_with_a_message() -> None:
    a: Any = FakeAirfield("A", 0, BLUE)
    b: Any = FakeAirfield("B", 40, BLUE)
    enemy: Any = FakeAirfield("Enemy", 120, RED)
    game = make_game([a, b, enemy])
    supply.update_main_bases(game)
    assert supply.main_base(game, BLUE) is a
    a.captured = RED
    messages = supply.update_main_bases(game)
    assert supply.main_base(game, BLUE) is b
    assert messages == ["A was lost, so B is now our main supply base."]


def test_enemy_main_base_revealed_by_strike_or_scouting() -> None:
    home: Any = FakeAirfield("Home", 0, BLUE)
    enemy: Any = FakeAirfield("Enemy", 120, RED)
    game = make_game([home, enemy])
    assert not supply.main_base_known(game, enemy)
    assert supply.main_base_known(game, home)

    near = SimpleNamespace(position=Point(115 * NM, 0, Caucasus()))
    far = SimpleNamespace(position=Point(60 * NM, 0, Caucasus()))
    elsewhere = SimpleNamespace(target=home, flights=[SimpleNamespace(points=[far])])
    game.blue.ato.packages = [elsewhere]
    assert not supply.reveal_enemy_main_base(game)

    scouting = SimpleNamespace(target=home, flights=[SimpleNamespace(points=[near])])
    game.blue.ato.packages = [scouting]
    assert supply.reveal_enemy_main_base(game)
    assert supply.main_base_known(game, enemy)
    assert game.messages[-1][1] == "Enemy is the enemy's main supply base."

    # Striking something at the base also reveals it.
    game.warehouse_logistics.revealed_main_bases.clear()
    strike = SimpleNamespace(target=SimpleNamespace(control_point=enemy), flights=[])
    game.blue.ato.packages = [strike]
    assert supply.reveal_enemy_main_base(game)
