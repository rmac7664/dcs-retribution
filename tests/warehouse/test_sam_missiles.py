"""SAM missiles drawn from their base's munitions."""

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import game.theater.theatergroundobject as tgo_module
from game.settings import Settings
from game.warehouse import sam
from game.warehouse.state import BaseStock, WarehouseState
from game.warehouse.supply import MunitionMasses, MunitionPrices

BUK = "weapons.missiles.SA9M38M1"
LAUNCHER = "SA-11 Buk LN 9A310M1"


class FakeSam:
    def __init__(self, name: str, launchers: int, dead: int = 0) -> None:
        self.name = name
        self.is_dead = False
        units = [
            SimpleNamespace(
                alive=i >= dead,
                is_vehicle=True,
                is_ship=False,
                unit_name=f"{name} L{i}",
                type=SimpleNamespace(id=LAUNCHER),
            )
            for i in range(launchers)
        ]
        self.groups = [SimpleNamespace(group_name=f"0001 | {name}", units=units)]


def world(stock: int, *sites: Any, enabled: bool = True) -> Any:
    settings = Settings()
    settings.logistics_enabled = True
    settings.logistics_limited_sam_missiles = enabled
    state = WarehouseState()
    cp = SimpleNamespace(
        id=uuid.uuid4(),
        name="Maykop",
        captured=SimpleNamespace(is_neutral=False, is_blue=False),
        ground_objects=list(sites),
    )
    state.stocks[cp.id] = BaseStock(0.0, {sam.sam_key(BUK): stock})
    state.is_managed = lambda _cp, _s: True  # type: ignore[method-assign,assignment]
    state.sam_loads[LAUNCHER] = {BUK: 4}
    game = SimpleNamespace(
        settings=settings,
        warehouse_logistics=state,
        theater=SimpleNamespace(controlpoints=[cp]),
    )
    return game, cp


@pytest.fixture(autouse=True)
def fake_sam_class(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tgo_module, "SamGroundObject", FakeSam)


def test_stock_keys_and_costs() -> None:
    assert sam.sam_key(BUK) == "sam.missiles.SA9M38M1"
    assert sam.sam_key("5V55R") == "sam.missiles.5V55R"
    settings = Settings()
    assert MunitionPrices.price("sam.missiles.SA9M38M1", settings) == pytest.approx(0.6)
    assert MunitionMasses.mass_kg("sam.missiles.MIM_104") == 900
    assert MunitionPrices.price("sam.missiles.unknown", settings) == 0.5


def test_full_stock_gives_full_loads() -> None:
    game, _cp = world(100, FakeSam("SNAKE", 3), FakeSam("ADDER", 2))
    data = sam.allowances(game)
    assert data["sites"]["SNAKE"]["allow"] == {BUK: 12}
    assert data["sites"]["ADDER"]["allow"] == {BUK: 8}
    assert data["units"]["SNAKE L0"] == "SNAKE"
    assert data["sites"]["SNAKE"]["groups"] == ["0001 | SNAKE"]


def test_short_stock_is_shared_by_full_load() -> None:
    # 12 + 8 wanted, 10 held: split 6 / 4.
    game, _cp = world(10, FakeSam("SNAKE", 3), FakeSam("ADDER", 2))
    data = sam.allowances(game)
    assert data["sites"]["SNAKE"]["allow"] == {BUK: 6}
    assert data["sites"]["ADDER"]["allow"] == {BUK: 4}


def test_dead_launchers_carry_nothing() -> None:
    game, _cp = world(100, FakeSam("SNAKE", 3, dead=2))
    assert sam.allowances(game)["sites"]["SNAKE"]["allow"] == {BUK: 4}


def test_unlearned_launchers_are_not_limited() -> None:
    game, _cp = world(100, FakeSam("SNAKE", 3))
    game.warehouse_logistics.sam_loads.clear()
    assert sam.allowances(game)["sites"]["SNAKE"]["allow"] == {}


def test_bases_stock_two_loads_and_new_missiles_are_seeded() -> None:
    game, cp = world(0, FakeSam("SNAKE", 3))
    game.warehouse_logistics.stocks[cp.id].munitions.clear()
    assert sam.authorized_sam(game, cp) == {"sam.missiles.SA9M38M1": 24}

    game.settings.logistics_enemy_starting_supply = 0.5
    sam.seed_new_missiles(game)
    sam.seed_new_missiles(game)  # only once
    assert game.warehouse_logistics.stocks[cp.id].munitions == {
        "sam.missiles.SA9M38M1": 12
    }


def test_nothing_when_switched_off() -> None:
    game, cp = world(0, FakeSam("SNAKE", 3), enabled=False)
    assert sam.authorized_sam(game, cp) == {}


def test_launches_are_charged_and_loads_learned() -> None:
    game, cp = world(10, FakeSam("SNAKE", 3))
    used = sam.apply_results(
        game,
        {
            "sam_used": {str(cp.id): {BUK: 4, "weapons.missiles.5V55R": 20}},
            "sam_loads": {"S-300PS 5P85C ln": {"weapons.missiles.5V55R": 4}},
        },
    )
    stock = game.warehouse_logistics.stocks[cp.id].munitions
    assert stock["sam.missiles.SA9M38M1"] == 6
    assert stock["sam.missiles.5V55R"] == 0  # never below zero
    assert used == {"Maykop": {"sam.missiles.SA9M38M1": 4, "sam.missiles.5V55R": 20}}
    assert game.warehouse_logistics.sam_loads["S-300PS 5P85C ln"] == {
        "weapons.missiles.5V55R": 4
    }


@pytest.mark.parametrize(
    "dcs_name,price",
    [
        ("SA5B55", 1.5),  # S-300PS
        ("SA9M38M1", 0.6),  # Buk
        ("SA3M9M", 0.4),  # Kub
        ("SA9M330", 0.4),  # Tor
        ("SA9M311", 0.1),  # Tunguska
        ("SA9M333", 0.08),  # Strela-10
        ("SA9M33", 0.2),  # Osa
        ("SA9M31", 0.05),  # Strela-1
        ("MIM_104", 4.0),  # Patriot
        ("weapons.missiles.AIM_120C", 1.0),  # NASAMS
        ("weapons.missiles.FIM_92C", 0.04),  # Avenger, Linebacker
    ],
)
def test_names_dcs_reports_are_priced_by_type(dcs_name: str, price: float) -> None:
    assert MunitionPrices.price(sam.sam_key(dcs_name), Settings()) == pytest.approx(
        price
    )


class FakeShips:
    def __init__(self, name: str) -> None:
        self.name = name
        self.is_dead = False
        ship = SimpleNamespace(
            alive=True,
            is_vehicle=False,
            is_ship=True,
            unit_name=f"{name} Tico",
            type=SimpleNamespace(id="TICONDEROG"),
        )
        self.groups = [SimpleNamespace(group_name=f"0002 | {name}", units=[ship])]


def test_warships_draw_on_their_base(monkeypatch: pytest.MonkeyPatch) -> None:
    import game.warehouse.sam as sam_module

    class FakeCarrierCp:
        pass

    monkeypatch.setattr(tgo_module, "NavalGroundObject", FakeShips)
    monkeypatch.setattr(sam_module, "NavalControlPoint", FakeCarrierCp)
    game, land = world(100, FakeSam("SNAKE", 1), FakeShips("KINGFISHER"))
    carrier: Any = FakeCarrierCp()
    carrier.id = uuid.uuid4()
    carrier.name = "CVN-74"
    carrier.captured = SimpleNamespace(is_neutral=False, is_blue=True)
    # A carrier group's escorts count; a land SAM type listed under it would not.
    carrier.ground_objects = [FakeShips("KOMODO"), FakeSam("STRAY", 1)]
    game.theater.controlpoints.append(carrier)
    game.warehouse_logistics.stocks[carrier.id] = BaseStock(0.0)
    game.warehouse_logistics.sam_loads["TICONDEROG"] = {"SM_2": 122}

    sites = {s.key: s.cp for s in sam.sam_sites(game)}

    assert sites == {"SNAKE": land, "KINGFISHER": land, "KOMODO": carrier}
    assert sam.authorized_sam(game, carrier) == {"sam.missiles.SM_2": 244}
    assert MunitionPrices.price("sam.missiles.SM_2", Settings()) == pytest.approx(2.0)
