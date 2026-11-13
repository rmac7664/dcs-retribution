from types import SimpleNamespace
from typing import Any

from game import weaponeering
from game.data.units import UnitClass
from game.weaponeering import WeaponKind, parse_table, weapon_kind


def vehicle(unit_class: UnitClass) -> Any:
    return SimpleNamespace(
        is_ship=False,
        is_static=False,
        unit_type=SimpleNamespace(unit_class=unit_class),
    )


def building(category: str) -> Any:
    return SimpleNamespace(
        is_ship=False,
        is_static=True,
        unit_type=None,
        ground_object=SimpleNamespace(category=category),
    )


def test_munitions_are_sorted_into_kinds() -> None:
    assert weapon_kind("weapons.bombs.GBU_12") == WeaponKind("bomb_500", True)
    assert weapon_kind("weapons.bombs.Mk_82") == WeaponKind("bomb_500", False)
    assert weapon_kind("weapons.bombs.GBU_31_V_3B") == WeaponKind("penetrator", True)
    assert weapon_kind("weapons.bombs.GBU_31") == WeaponKind("bomb_2000", True)
    assert weapon_kind("weapons.missiles.AGM_65D") == WeaponKind("missile_small", True)
    assert weapon_kind("weapons.missiles.AGM_65K") == WeaponKind("missile_large", True)
    assert weapon_kind("weapons.nurs.C_8") == WeaponKind("rocket", True)
    # Air-to-air, anti-radiation and training rounds are not counted.
    for name in ("AIM_120C", "AGM_88", "AGM_122", "BDU_33", "TGM_65D"):
        assert weapon_kind(f"weapons.missiles.{name}") is None
    # The C-802AK missile is not mistaken for the S-8 rocket ("C_8").
    assert weapon_kind("weapons.missiles.C_802AK") == WeaponKind("bomb_500", True)


def test_targets_are_sorted_into_kinds() -> None:
    assert weaponeering.target_kind(vehicle(UnitClass.TANK)) == "armor"
    assert weaponeering.target_kind(vehicle(UnitClass.IFV)) == "light_armor"
    assert weaponeering.target_kind(vehicle(UnitClass.LOGISTICS)) == "soft"
    assert weaponeering.target_kind(building("commandcenter")) == "hardened"
    assert weaponeering.target_kind(building("factory")) == "large_building"
    assert weaponeering.target_kind(building("comms")) == "small_building"
    ship = SimpleNamespace(is_ship=True, is_static=False)
    assert weaponeering.target_kind(ship) is None  # type: ignore[arg-type]


def test_the_shipped_table_reads_and_counts() -> None:
    table = weaponeering.table()
    assert table.cells and table.unguided_factor >= 1
    factories = [building("factory")] * 2
    guided = weaponeering.weapons_required(factories, WeaponKind("bomb_500", True))
    unguided = weaponeering.weapons_required(factories, WeaponKind("bomb_500", False))
    assert guided == 8
    assert unguided == round(8 * table.unguided_factor)
    # One cluster bomb covers two soft targets, rounded up.
    trucks = [vehicle(UnitClass.LOGISTICS)] * 3
    assert weaponeering.weapons_required(trucks, WeaponKind("cluster", True)) == 2
    # Not suited at all.
    bunker = [building("commandcenter")]
    assert weaponeering.weapons_required(bunker, WeaponKind("rocket", True)) is None
    assert weaponeering.weapons_required([], WeaponKind("bomb_500", True)) is None


def test_planning_lines_only_offer_weapons_that_suit_every_target() -> None:
    lines = weaponeering.requirements([building("commandcenter"), building("ware")])
    labels = {line.label for line in lines}
    assert "2,000 lb bomb" in labels
    assert "Rocket" not in labels and "Cluster bomb" not in labels
    assert weaponeering.requirements([]) == []


def test_carried_weapons_are_compared_with_the_targets() -> None:
    lines = weaponeering.for_carried(
        [vehicle(UnitClass.TANK), vehicle(UnitClass.TANK)],
        {"weapons.bombs.GBU_12": 4, "weapons.missiles.AIM_120C": 2},
    )
    assert lines == [("GBU-12", 2, 4)]


def test_a_malformed_table_is_ignored_not_fatal() -> None:
    table = parse_table(["not", "a", "table"])
    assert table.cells == {} and table.unguided_factor == 2
    table = parse_table(
        {
            "unguided_factor": "lots",
            "weapons": {"bomb_500": "500 lb bomb"},
            "targets": {
                "soft": {"label": "Soft", "bomb_500": 1, "rocket": "-"},
                "x": 3,
            },
        }
    )
    assert table.cells == {("soft", "bomb_500"): 1.0}
    assert table.unguided_factor == 2


def test_empty_pylons_are_skipped_when_listing_what_is_carried() -> None:
    class Catalog:
        def demand(self, weapons: Any) -> Any:
            assert None not in list(weapons)
            return {"weapons.bombs.GBU_12": 2, "weapons.missiles.AIM_120C": 2}, 0, 0

    loadout: Any = SimpleNamespace(pylons={1: None, 2: object()})
    assert weaponeering.carried([loadout], Catalog()) == {"weapons.bombs.GBU_12": 2}
