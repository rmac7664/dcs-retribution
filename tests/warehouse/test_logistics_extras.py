"""Presets, priorities, usage-aware buying, fuel depots, turn report, main base only."""

import uuid
from types import SimpleNamespace
from typing import Any

from game.settings import Settings
from game.theater.player import Player
from game.warehouse import fuel, presets, priority, report, usage
from game.warehouse.state import BaseStock, WarehouseState


def test_presets_apply_and_are_recognised() -> None:
    settings = Settings()
    changed = presets.apply_preset(settings, "Hardcore")
    assert "logistics_main_base_only" in changed
    assert settings.logistics_enabled and settings.logistics_main_base_only
    assert presets.matching_preset(settings) == "Hardcore"
    presets.apply_preset(settings, "Light")
    assert not settings.logistics_supply_lines
    assert presets.matching_preset(settings) == "Light"
    settings.logistics_truck_tons = 3  # not part of any preset
    assert presets.matching_preset(settings) == "Light"
    settings.logistics_munitions_sorties = 9
    assert presets.matching_preset(settings) == "Custom"


def base(player: Player, frontline: bool = False) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(), name="X", captured=player, has_active_frontline=frontline
    )


def test_player_marks_priority_and_ai_prioritises_its_front() -> None:
    game: Any = SimpleNamespace(warehouse_logistics=WarehouseState())
    mine = base(Player.BLUE, frontline=True)
    assert not priority.is_priority(game, mine)
    priority.set_priority(game, mine, True)
    assert priority.is_priority(game, mine)
    priority.set_priority(game, mine, False)
    assert not priority.is_priority(game, mine)
    assert priority.is_priority(game, base(Player.RED, frontline=True))
    assert not priority.is_priority(game, base(Player.RED))


def usage_game() -> Any:
    blue_base = SimpleNamespace(name="Tel Nof", captured=Player.BLUE)
    return SimpleNamespace(
        warehouse_logistics=WarehouseState(),
        theater=SimpleNamespace(controlpoints=[blue_base]),
        blue=SimpleNamespace(player=Player.BLUE),
        red=SimpleNamespace(player=Player.RED),
    )


def test_usage_decays_and_unused_items_go_dormant() -> None:
    game = usage_game()
    state = game.warehouse_logistics
    aim = "weapons.missiles.AIM_120C"
    alcm = "weapons.missiles.AGM_86"
    usage.record_usage(state, game, {"Tel Nof": {aim: 8}})
    assert usage.usage_of(game, "BLUE")[aim] == 8
    assert not usage.is_dormant(game, "BLUE", alcm)  # not enough history yet
    usage.record_usage(state, game, {})
    usage.record_usage(state, game, {"Tel Nof": {aim: 2}})
    assert usage.usage_of(game, "BLUE")[aim] == 8 * 0.25 + 2
    assert usage.is_dormant(game, "BLUE", alcm)
    assert not usage.is_dormant(game, "BLUE", aim)
    assert not usage.is_dormant(game, "BLUE", "sam.missiles.SM_2")


def test_destroyed_fuel_depot_burns_its_share() -> None:
    depots = [
        SimpleNamespace(id=uuid.uuid4(), category="fuel", is_dead=False)
        for _ in range(2)
    ]
    cp = SimpleNamespace(
        id=uuid.uuid4(),
        name="Tel Nof",
        captured=Player.BLUE,
        connected_objectives=depots + [SimpleNamespace(category="ammo", is_dead=True)],
    )
    state = WarehouseState()
    state.stocks[cp.id] = BaseStock(100_000.0)
    game: Any = SimpleNamespace(
        warehouse_logistics=state, theater=SimpleNamespace(controlpoints=[cp])
    )
    assert fuel.fuel_depot_losses(game) == []  # first look: nothing burns
    depots[0].is_dead = True
    (loss,) = fuel.fuel_depot_losses(game)
    assert loss[:2] == ("BLUE", "Tel Nof")
    assert state.stocks[cp.id].jet_fuel_kg == 100_000 * (1 - fuel.BURN_SHARE / 2)
    assert fuel.fuel_depot_losses(game) == []  # only once


def test_old_saves_dont_burn_fuel_for_depots_already_dead() -> None:
    dead = SimpleNamespace(id=uuid.uuid4(), category="fuel", is_dead=True)
    cp = SimpleNamespace(
        id=uuid.uuid4(), name="A", captured=Player.BLUE, connected_objectives=[dead]
    )
    state = WarehouseState()
    state.stocks[cp.id] = BaseStock(50_000.0)
    game: Any = SimpleNamespace(
        warehouse_logistics=state, theater=SimpleNamespace(controlpoints=[cp])
    )
    assert fuel.fuel_depot_losses(game) == []
    assert state.stocks[cp.id].jet_fuel_kg == 50_000


def test_turn_log_feeds_last_turn_summary() -> None:
    settings = Settings()
    game: Any = SimpleNamespace(warehouse_logistics=WarehouseState(), settings=settings)
    report.record_delivery(game, "BLUE", "Tel Nof", 18.0, 0.0)
    report.record_delivery(game, "BLUE", "Ovda", 10.0, 4.0)
    report.end_turn(game)
    lines = report.summary_lines(game, Player.BLUE)
    assert "Delivered last turn: 28 t." in lines
    assert "Lost on the way last turn: 4 t." in lines
    assert game.warehouse_logistics.turn_log == {}


def test_main_base_only_makes_the_main_base_the_only_depot(monkeypatch: Any) -> None:
    import game.warehouse.supply as supply

    settings = Settings()
    settings.logistics_main_base_only = True
    hub = SimpleNamespace(name="Hub")
    other = SimpleNamespace(name="Depot")
    game = SimpleNamespace(settings=settings)
    monkeypatch.setattr(supply, "main_base", lambda _g, _p: hub)
    for cp in (hub, other):
        cp.captured = SimpleNamespace(is_neutral=False)
        cp.coalition = SimpleNamespace(game=game)
    assert supply.SupplyPlanner.is_depot(hub)  # type: ignore[arg-type]
    assert not supply.SupplyPlanner.is_depot(other)  # type: ignore[arg-type]
