"""Replenishment ships in the mission: placement, generation, debrief."""

import uuid
from types import SimpleNamespace
from typing import Any

import dcs.ships
import pytest
from dcs import Mission
from dcs.mapping import Point
from dcs.terrain import Caucasus

import game.missiongenerator.replenishmentshipgenerator as generator_module
from game.dcs.shipunittype import ShipUnitType
from game.missiongenerator.missiondata import MissionData
from game.missiongenerator.replenishmentshipgenerator import (
    ReplenishmentShipGenerator,
    start_point,
)
from game.settings import Settings
from game.theater.player import Player
from game.unitmap import UnitMap
from game.utils import nautical_miles
from game.warehouse.plan import MissionWarehousePlan
from game.warehouse.state import BaseStock, SupplyShip, WarehouseState

AIM_120C = "weapons.missiles.AIM_120C"
TERRAIN = Caucasus()


def at(x: float, y: float) -> Point:
    return Point(x, y, TERRAIN)


# Placement -------------------------------------------------------------------------


def test_ship_starts_away_from_the_enemy() -> None:
    rendezvous = at(0, 0)
    enemy = at(0, -100_000)  # due west (y is east-west in DCS)
    start = start_point(rendezvous, enemy, nautical_miles(18), lambda _p: True)
    assert start is not None
    # Directly away from the enemy: further east, 18 nm out.
    assert start.y == pytest.approx(nautical_miles(18).meters, rel=1e-3)
    assert start.x == pytest.approx(0, abs=1)


def test_ship_swings_round_to_find_open_water() -> None:
    rendezvous = at(0, 0)
    enemy = at(0, -100_000)

    def sea_only_north(p: Point) -> bool:
        return p.x > 1000  # land everywhere else

    start = start_point(rendezvous, enemy, nautical_miles(18), sea_only_north)
    assert start is not None and start.x > 1000


def test_no_open_water_means_no_ship() -> None:
    assert start_point(at(0, 0), None, nautical_miles(18), lambda _p: False) is None


# Generation --------------------------------------------------------------------------


class FakeCarrierCp:
    """Stands in for NavalControlPoint (patched into the generator)."""

    def __init__(self, carrier_id: int) -> None:
        self.id = uuid.uuid4()
        self.name = "CVN-73"
        self.carrier_id = carrier_id
        self.position = at(0, 0)
        self.captured = Player.BLUE
        cargo_ship = next(iter(ShipUnitType.for_dcs_type(dcs.ships.LST_Mk2)))
        self.coalition = SimpleNamespace(
            faction=SimpleNamespace(
                country=SimpleNamespace(name="USA"), cargo_ship=cargo_ship
            )
        )

    def runway_is_operational(self) -> bool:
        return True


def build(monkeypatch: pytest.MonkeyPatch, *, disabled: bool = False) -> Any:
    monkeypatch.setattr(generator_module, "NavalControlPoint", FakeCarrierCp)
    mission = Mission(TERRAIN)
    usa = mission.country("USA")
    carrier_group = mission.ship_group(
        usa, "CVN-73 group", dcs.ships.Stennis, position=at(0, 0)
    )
    carrier_group.add_waypoint(at(30_000, 0), speed=50)
    cp = FakeCarrierCp(carrier_group.units[0].id)
    enemy = SimpleNamespace(position=at(0, -200_000), captured=Player.RED)

    settings = Settings()
    settings.logistics_enabled = True
    settings.perf_disable_replenishment_ships = disabled
    state = WarehouseState()
    ship = SupplyShip(cp.id, cp.name, "BLUE", {AIM_120C: 12}, fuel_kg=40_000)
    state.supply_ships.append(ship)

    def find(cp_id: uuid.UUID) -> Any:
        if cp_id != cp.id:
            raise KeyError(cp_id)
        return cp

    game: Any = SimpleNamespace(
        settings=settings,
        warehouse_logistics=state,
        theater=SimpleNamespace(
            find_control_point_by_id=find,
            controlpoints=[cp, enemy],
            is_in_sea=lambda _p: True,
        ),
    )
    mission_data = MissionData()
    mission_data.carriers.append(
        SimpleNamespace(  # type: ignore[arg-type]
            ship_group=carrier_group, unit_name=str(carrier_group.units[0].name)
        )
    )
    unit_map = UnitMap()
    ReplenishmentShipGenerator(mission, game, unit_map, mission_data).generate()
    return SimpleNamespace(
        mission=mission,
        mission_data=mission_data,
        unit_map=unit_map,
        ship=ship,
        carrier_group=carrier_group,
    )


def test_ships_at_sea_are_put_in_the_mission(monkeypatch: pytest.MonkeyPatch) -> None:
    result = build(monkeypatch)

    (info,) = result.mission_data.replenishment_ships
    assert info.carrier_unit_name == str(result.carrier_group.units[0].name)
    assert info.munitions == {AIM_120C: 12} and info.fuel_kg == 40_000
    assert result.unit_map.replenishment_ship(info.unit_name) is result.ship

    group = next(
        g
        for g in result.mission.country("USA").ship_group
        if str(g.name) == info.group_name
    )
    assert group.units[0].type == dcs.ships.Ship_Tilde_Supply.id
    # It sails for the end of the carrier's launch-and-recovery leg, from 18 nm out.
    assert group.points[-1].position.distance_to_point(at(30_000, 0)) < 1
    assert group.points[0].position.distance_to_point(at(30_000, 0)) == pytest.approx(
        nautical_miles(18).meters, rel=1e-3
    )


def test_the_mission_script_is_told_about_each_ship(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = build(monkeypatch)
    plan = MissionWarehousePlan.__new__(MissionWarehousePlan)
    plan.prefixes = ("weapons.missiles.",)
    plan.bases = {}
    plan.units = {}
    plan.learn_keys = set()
    plan.cargo_units = {}
    plan.state = WarehouseState()

    data = plan.lua_data(result.mission_data)

    (info,) = result.mission_data.replenishment_ships
    assert data["replenishment"][info.unit_name] == {
        "carrier": info.carrier_unit_name,
        "cp": info.carrier_cp_id,
        "mun": {AIM_120C: 12},
        "fuel": 40_000,
    }


def test_disabled_ships_stay_off_the_map(monkeypatch: pytest.MonkeyPatch) -> None:
    result = build(monkeypatch, disabled=True)
    assert result.mission_data.replenishment_ships == []
    assert result.unit_map.replenishment_ships == {}


# Debrief -----------------------------------------------------------------------------


def debrief_setup(
    killed: list[str], replenished: dict[str, bool], mission_ended: bool
) -> Any:
    carrier = SimpleNamespace(id=uuid.uuid4(), name="CVN-73")
    settings = Settings()
    settings.logistics_enabled = True
    messages: list[str] = []
    game: Any = SimpleNamespace(
        settings=settings,
        theater=SimpleNamespace(find_control_point_by_id=lambda _id: carrier),
        message=lambda title, _text="": messages.append(title),
    )
    state = WarehouseState()
    state.stocks[carrier.id] = BaseStock(0.0)
    unloaded = SupplyShip(carrier.id, carrier.name, "BLUE", {AIM_120C: 12}, 5000)
    sunk = SupplyShip(carrier.id, carrier.name, "BLUE", {AIM_120C: 8}, 1000)
    sailing = SupplyShip(carrier.id, carrier.name, "BLUE", {AIM_120C: 4}, 0)
    state.supply_ships.extend([unloaded, sunk, sailing])
    ships = {"Ship 1": unloaded, "Ship 2": sunk, "Ship 3": sailing}

    from game.debriefing import Debriefing

    debriefing = Debriefing.__new__(Debriefing)
    debriefing.state_data = SimpleNamespace(killed_ground_units=killed)  # type: ignore[assignment]
    debriefing.unit_map = SimpleNamespace(  # type: ignore[assignment]
        replenishment_ship=ships.get
    )
    state.apply_replenishment(
        game, debriefing, {"replenished": replenished}, mission_ended
    )
    return SimpleNamespace(
        state=state, carrier=carrier, sailing=sailing, messages=messages
    )


def test_unloaded_ships_are_booked_by_the_mission_results() -> None:
    result = debrief_setup(["Ship 2"], {"Ship 1": True}, mission_ended=True)
    # Ship 1's cargo comes in through the carrier's ledger, not twice.
    assert result.state.stocks[result.carrier.id].munitions == {}
    # Ship 2 was sunk; only Ship 3, still sailing, is left at sea.
    assert result.state.supply_ships == [result.sailing]
    assert result.messages == ["Logistics: replenishment ship sunk"]


def test_unloaded_cargo_is_kept_even_if_the_mission_did_not_end_cleanly() -> None:
    result = debrief_setup([], {"Ship 1": True}, mission_ended=False)
    stock = result.state.stocks[result.carrier.id]
    assert stock.munitions == {AIM_120C: 12} and stock.jet_fuel_kg == 5000
    assert len(result.state.supply_ships) == 2
