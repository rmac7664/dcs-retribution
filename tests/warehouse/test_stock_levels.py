"""Stock targets from flown loadouts, rear-only depots, starting supply."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from game.ato.flighttype import FlightType
from game.settings import Settings
from game.theater.player import Player
from game.warehouse.state import WarehouseState
from game.warehouse.supply import SupplyPlanner

AIM_9 = "weapons.missiles.AIM_9"
AGM_65 = "weapons.missiles.AGM_65D"
MK_82 = "weapons.bombs.Mk_82"


def game_with(**settings: Any) -> Any:
    s = Settings()
    s.logistics_enabled = True
    for k, v in settings.items():
        setattr(s, k, v)
    return SimpleNamespace(settings=s, date=None)


def squadron(count: int, *tasks: FlightType) -> Any:
    return SimpleNamespace(
        aircraft="A-10C",
        owned_aircraft=count,
        primary_task=tasks[0],
        auto_assignable_mission_types=set(tasks),
    )


def test_bases_are_stocked_for_the_missions_their_squadrons_fly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = WarehouseState()
    seen: list[set[FlightType]] = []

    def task_loads(_game: Any, _cp: Any, _aircraft: Any, tasks: Any) -> dict[str, int]:
        seen.append(set(tasks))
        return {AGM_65: 4, AIM_9: 2}

    monkeypatch.setattr(state, "_task_loads", task_loads)
    monkeypatch.setattr(state, "_max_loads", lambda *_: pytest.fail("not needed"))
    base = SimpleNamespace(squadrons=[squadron(11, FlightType.CAS, FlightType.BAI)])

    authorized = state.authorized_munitions(game_with(), base)  # type: ignore[arg-type]

    # 4 sorties x 11 aircraft x the most each mission's loadout carries.
    assert authorized == {AGM_65: 176, AIM_9: 88}
    assert seen == [{FlightType.CAS, FlightType.BAI}]


def test_squadrons_without_armed_mission_loadouts_fall_back_to_every_loadout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = WarehouseState()
    monkeypatch.setattr(state, "_task_loads", lambda *_: {})
    monkeypatch.setattr(state, "_max_loads", lambda *_: {MK_82: 6})
    base = SimpleNamespace(squadrons=[squadron(2, FlightType.STRIKE)])

    assert state.authorized_munitions(game_with(), base) == {MK_82: 48}  # type: ignore[arg-type]


def land_base(
    owner: Player, started: Player, front: bool, depot_alive: bool = True
) -> Any:
    building = SimpleNamespace(category="ammo", is_dead=not depot_alive)
    no_rear_area = SimpleNamespace(theater=SimpleNamespace(controlpoints=[]))
    return SimpleNamespace(
        captured=owner,
        starting_coalition=started,
        has_frontline=front,
        connected_objectives=[building],
        coalition=SimpleNamespace(game=no_rear_area),
    )


def test_only_rear_bases_held_since_the_start_buy_munitions() -> None:
    blue, red = Player.BLUE, Player.RED
    assert SupplyPlanner.is_depot(land_base(blue, blue, front=False))
    # On a front line: supplied by supply run.
    assert not SupplyPlanner.is_depot(land_base(blue, blue, front=True))
    # Captured from the enemy, even with its ammo depot standing.
    assert not SupplyPlanner.is_depot(land_base(blue, red, front=False))
    # No live depot buildings.
    assert not SupplyPlanner.is_depot(land_base(blue, blue, False, depot_alive=False))


def test_starting_supply_scales_each_sides_first_stock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = game_with(
        logistics_player_starting_supply=0.5, logistics_enemy_starting_supply=0.25
    )
    state = WarehouseState()
    monkeypatch.setattr(WarehouseState, "is_managed", staticmethod(lambda *_: True))
    monkeypatch.setattr(
        WarehouseState, "fuel_capacity_kg", staticmethod(lambda *_: 1000.0)
    )
    monkeypatch.setattr(state, "authorized_munitions", lambda *_: {AIM_9: 40, MK_82: 2})

    def base(owner: Player) -> Any:
        return SimpleNamespace(id=uuid.uuid4(), captured=owner)

    blue = state.ensure_stock(game, base(Player.BLUE))
    red = state.ensure_stock(game, base(Player.RED))
    assert blue.jet_fuel_kg == 500 and blue.munitions == {AIM_9: 20, MK_82: 1}
    assert red.jet_fuel_kg == 250 and red.munitions == {AIM_9: 10}


class FakeBase:
    def __init__(self, name: str, x: float, owner: Player, started: Player) -> None:
        from dcs.mapping import Point
        from dcs.terrain import Caucasus

        self.id = uuid.uuid4()
        self.name = name
        self.position = Point(x, 0, Caucasus())
        self.captured = owner
        self.starting_coalition = started

    def runway_is_operational(self) -> bool:
        return True


class FakeAirfield(FakeBase):
    pass


class FakeOffMap(FakeBase):
    """Like OffMapSpawn, not an Airfield."""


def test_rear_area_supplies_arrive_at_the_rear_most_airfield(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import game.warehouse.supply as supply_module

    monkeypatch.setattr(supply_module, "Airfield", FakeAirfield)
    monkeypatch.setattr(supply_module, "OffMapSpawn", FakeOffMap)
    blue, red = Player.BLUE, Player.RED
    rear: Any = FakeAirfield("Vaziani", 0, blue, blue)
    forward: Any = FakeAirfield("Tbilisi", 150_000, blue, blue)
    enemy: Any = FakeAirfield("Sochi", 250_000, red, red)
    enemy_off_map: Any = FakeOffMap("Red rear", -900_000, red, red)
    off_map: Any = FakeOffMap("Fairford", 900_000, blue, blue)
    points = [rear, forward, enemy, enemy_off_map]
    game: Any = SimpleNamespace(
        theater=SimpleNamespace(controlpoints=points + [off_map]),
        warehouse_logistics=SimpleNamespace(main_bases={}),
    )

    # The airfield farthest from the enemy, ignoring the enemy's off-map spawn.
    assert supply_module.main_base(game, blue) is rear
    assert supply_module.airhead(game, blue) is rear

    # The player can pick another; an ineligible pick falls back to automatic.
    supply_module.set_main_base(game, blue, forward)
    assert supply_module.main_base(game, blue) is forward
    with pytest.raises(ValueError):
        supply_module.set_main_base(game, blue, enemy)
    game.warehouse_logistics.main_bases[blue.name] = enemy.id
    assert supply_module.main_base(game, blue) is rear
    supply_module.set_main_base(game, blue, None)
    assert game.warehouse_logistics.main_bases == {}

    # Without an off-map rear area there is still a main base, but no airhead.
    game.theater.controlpoints = points
    assert supply_module.main_base(game, blue) is rear
    assert supply_module.airhead(game, blue) is None


def test_rear_area_runs_waiting_off_map_continue_from_the_airhead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import game.warehouse.supply as supply_module

    monkeypatch.setattr(supply_module, "OffMapSpawn", FakeOffMap)
    airhead: Any = SimpleNamespace(name="Vaziani")
    off_map = FakeOffMap("Fairford", 0, Player.BLUE, Player.BLUE)
    onward = SimpleNamespace(
        supplies="cargo",
        transport=None,
        position=off_map,
        destination=SimpleNamespace(name="Sukhumi"),
    )
    delivered_here = SimpleNamespace(
        supplies="cargo",
        transport=None,
        position=off_map,
        destination=airhead,
        units={"truck": 2},
    )
    flying = SimpleNamespace(
        supplies="cargo", transport="airlift", position=off_map, destination=airhead
    )
    pending = [onward, delivered_here, flying]
    unloaded: list[Any] = []
    monkeypatch.setattr(supply_module, "supply_of", lambda t: t.supplies)
    monkeypatch.setattr(
        supply_module, "deliver", lambda t, cp: unloaded.append((t, cp))
    )
    planner = SupplyPlanner.__new__(SupplyPlanner)
    planner.airhead = airhead
    planner.coalition = SimpleNamespace(  # type: ignore[assignment]
        transfers=SimpleNamespace(pending_transfers=pending)
    )

    planner._land_rear_area_runs()

    assert onward.position is airhead
    assert unloaded == [(delivered_here, airhead)] and delivered_here not in pending
    # Already on its way (has a transport): left alone.
    assert flying.position is off_map and flying in pending
