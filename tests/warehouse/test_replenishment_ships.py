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
from game.utils import knots, nautical_miles
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
    # This carrier finishes its short leg long before the ship arrives, so they meet
    # at its end; the ship starts 70 nm out (the default) and sails in at 18 kt.
    assert group.points[-1].position.distance_to_point(at(30_000, 0)) < 1
    assert group.points[0].position.distance_to_point(at(30_000, 0)) == pytest.approx(
        nautical_miles(70).meters, rel=1e-3
    )
    assert group.points[-1].speed == pytest.approx(knots(18).meters_per_second)


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


def debrief_setup(killed: list[str], replenished: dict[str, bool]) -> Any:
    carrier = SimpleNamespace(id=uuid.uuid4(), name="CVN-73")
    settings = Settings()
    settings.logistics_enabled = True
    messages: list[str] = []
    game: Any = SimpleNamespace(
        settings=settings,
        theater=SimpleNamespace(find_control_point_by_id=lambda _id: carrier),
        message=lambda title, _text="": messages.append(title),
    )
    carrier.captured = SimpleNamespace(name="BLUE")
    carrier.runway_is_operational = lambda: True
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
    state.apply_replenishment(game, debriefing, {"replenished": replenished})
    return SimpleNamespace(
        game=game, state=state, carrier=carrier, sailing=sailing, messages=messages
    )


def test_unloaded_cargo_is_credited_once_and_sunk_cargo_is_lost() -> None:
    result = debrief_setup(["Ship 2"], {"Ship 1": True})
    stock = result.state.stocks[result.carrier.id]
    assert stock.munitions == {AIM_120C: 12} and stock.jet_fuel_kg == 5000
    # Ship 2 was sunk; only Ship 3, still sailing, is left at sea.
    assert result.state.supply_ships == [result.sailing]
    assert result.messages == ["Logistics: replenishment ship sunk"]

    # At the end of the turn only the ship still at sea arrives: Ship 1's cargo is
    # not delivered a second time and Ship 2's never arrives.
    arrived, lost = result.state.arrive_supply_ships(result.game, "BLUE")
    assert (arrived, lost) == (1, [])
    assert stock.munitions == {AIM_120C: 16}
    assert result.state.supply_ships == []


def test_meeting_point_is_where_the_carrier_will_be_when_the_ship_arrives() -> None:
    from dcs.point import MovingPoint

    from game.missiongenerator.replenishmentshipgenerator import (
        position_after,
        sailing_seconds,
    )

    def waypoint(x: float, speed_ms: float) -> MovingPoint:
        p = MovingPoint(at(x, 0))
        p.speed = speed_ms
        return p

    # 70 nm at 18 kt is just under four hours.
    seconds = sailing_seconds(nautical_miles(70))
    assert seconds == pytest.approx(70 / 18 * 3600)
    # A long leg at 11 m/s (21 kt): the carrier is still on it when the ship arrives.
    leg = nautical_miles(200).meters
    meet = position_after([waypoint(0, 11.0), waypoint(leg, 11.0)], seconds)
    assert meet.x == pytest.approx(11.0 * seconds, rel=1e-3)
    # A short leg the carrier finishes early: it waits at the end.
    assert position_after([waypoint(0, 11), waypoint(5000, 11)], seconds).x == (
        pytest.approx(5000)
    )
    # A stopped carrier stays put.
    assert position_after([waypoint(0, 0), waypoint(5000, 0)], seconds).x == 0


def test_red_and_blue_supply_ships_look_different() -> None:
    lst = next(iter(ShipUnitType.for_dcs_type(dcs.ships.LST_Mk2)))
    blue = ReplenishmentShipGenerator.ship_type("BLUE", lst)
    red = ReplenishmentShipGenerator.ship_type("RED", lst)
    assert blue.dcs_unit_type is dcs.ships.Ship_Tilde_Supply
    assert red.dcs_unit_type is dcs.ships.ELNYA


def test_a_sunk_carrier_is_no_longer_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    import game.warehouse.state as state_module

    class Carrier:
        is_carrier = True
        is_lha = False

        def __init__(self, afloat: bool) -> None:
            self.afloat = afloat
            self.captured = SimpleNamespace(is_neutral=False, is_blue=True)

        def runway_is_operational(self) -> bool:
            return self.afloat

    monkeypatch.setattr(state_module, "NavalControlPoint", Carrier)
    settings = Settings()
    settings.logistics_enabled = True
    afloat: Any = Carrier(afloat=True)
    sunk: Any = Carrier(afloat=False)
    assert WarehouseState.is_managed(afloat, settings)
    assert not WarehouseState.is_managed(sunk, settings)


def test_ships_sail_in_from_the_main_supply_base(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import game.warehouse.supply as supply_module

    north = SimpleNamespace(position=at(500_000, 0))
    monkeypatch.setattr(supply_module, "main_base", lambda _game, _side: north)
    result = build(monkeypatch)
    group = next(
        g
        for g in result.mission.country("USA").ship_group
        if "replenishment" in str(g.name) or g.units[0].type == "Ship_Tilde_Supply"
    )
    start, end = group.points[0].position, group.points[-1].position
    # It comes from the north (x grows northwards in DCS), 70 nm out.
    assert start.x - end.x == pytest.approx(nautical_miles(70).meters, rel=1e-3)
