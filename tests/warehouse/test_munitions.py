import pytest

from game.data.weapons import Weapon
from game.warehouse.munitions import (
    MunitionCatalog,
    MunitionSource,
    count_from_name,
    learn_from_observations,
)

AIM_120C = "weapons.missiles.AIM_120C"


def weapon(clsid: str) -> Weapon:
    found = Weapon.with_clsid(clsid)
    assert found is not None, clsid
    return found


@pytest.mark.parametrize(
    "name,count",
    [
        ("AIM-120C AMRAAM - Active Radar AAM", 1),
        ("LAU-115 with 2 x LAU-127 AIM-120C AMRAAM - Active Radar AAM", 2),
        ("2xAIM-120C", 2),
        ("AIM-120C-8 AMRAAM - Active Radar AAM x 2", 2),
        ("CBU-97 * 3", 3),
        (
            "[ STA 02/03 | 79 / 80 | LAU127 ] - 2x/1x AIM-120C AMRAAM - "
            "Active Radar AAM, (AI Only)",
            3,
        ),
        ("BRU-42 - 3 x ADM-141A TALD", 3),
        ("AN/AAQ-28 LITENING - Targeting Pod", 1),
        ("{SUPERHORNET_PYLON_02_MB_AM_2X_AIM-120C}", 2),
        ("AIM-9X Sidewinder IR AAM", 1),
        ("LAU-127 AIM-9X Sidewinder IR AAM x 2", 2),
    ],
)
def test_count_from_name(name: str, count: int) -> None:
    assert count_from_name(name) == count


def test_datamine_resolves_stock_launchers() -> None:
    catalog = MunitionCatalog({})
    single = catalog.lookup(weapon("{40EF17B7-F508-45de-8566-6FFECC0C1AB8}"))
    double = catalog.lookup(weapon("LAU-115_2*LAU-127_AIM-120C"))
    assert single == ({AIM_120C: 1}, MunitionSource.DATAMINE)
    assert double == ({AIM_120C: 2}, MunitionSource.DATAMINE)


def test_learned_overrides_datamine() -> None:
    clsid = "{40EF17B7-F508-45de-8566-6FFECC0C1AB8}"
    catalog = MunitionCatalog({clsid: {"weapons.missiles.SOMETHING": 1}})
    assert catalog.lookup(weapon(clsid)) == (
        {"weapons.missiles.SOMETHING": 1},
        MunitionSource.LEARNED,
    )


def test_mod_launcher_borrows_munition_from_its_weapon_group() -> None:
    # Super Hornet mod pylon: not in the datamine, but grouped with stock AIM-120Cs.
    catalog = MunitionCatalog({})
    result = catalog.lookup(weapon("{SUPERHORNET_PYLON_02_MB_AM_2X_AIM-120C}"))
    assert result == ({AIM_120C: 2}, MunitionSource.HEURISTIC)


def test_clean_pylon_draws_nothing() -> None:
    assert MunitionCatalog({}).lookup(weapon("<CLEAN>")) == (
        {},
        MunitionSource.DATAMINE,
    )


def test_learns_single_unknown_launcher_from_residual() -> None:
    catalog = MunitionCatalog({})
    learned: dict[str, dict[str, int]] = {}
    clsids = [
        "{40EF17B7-F508-45de-8566-6FFECC0C1AB8}",  # stock AIM-120C
        "{SUPERHORNET_PYLON_02_MB_AM_2X_AIM-120C}",
        "{SUPERHORNET_PYLON_02_MB_AM_2X_AIM-120C}",
    ]
    # DCS reports the mod pylon really carries a mod-specific item.
    observed = {AIM_120C: 1, "weapons.missiles.AIM_120C_SH": 4}
    newly = learn_from_observations(catalog, learned, [(clsids, observed)])
    assert newly == ["{SUPERHORNET_PYLON_02_MB_AM_2X_AIM-120C}"]
    assert learned["{SUPERHORNET_PYLON_02_MB_AM_2X_AIM-120C}"] == {
        "weapons.missiles.AIM_120C_SH": 2
    }


def test_confirms_heuristic_guesses_when_they_add_up() -> None:
    learned: dict[str, dict[str, int]] = {}
    catalog = MunitionCatalog(learned)
    clsids = [
        "{SUPERHORNET_PYLON_02_MB_AM_2X_AIM-120C}",
        "{SUPERHORNET_PYLON_02_AM_1X_AIM-120C}",
    ]
    newly = learn_from_observations(catalog, learned, [(clsids, {AIM_120C: 3})])
    assert sorted(newly) == sorted(clsids)
    assert catalog.lookup(weapon(clsids[0])) == ({AIM_120C: 2}, MunitionSource.LEARNED)


def test_ignores_observations_it_cannot_explain() -> None:
    catalog = MunitionCatalog({})
    learned: dict[str, dict[str, int]] = {}
    clsids = [
        "{SUPERHORNET_PYLON_02_MB_AM_2X_AIM-120C}",
        "{SUPERHORNET_PYLON_02_AM_1X_AIM-120C}",
    ]
    assert learn_from_observations(catalog, learned, [(clsids, {AIM_120C: 7})]) == []
    assert learned == {}
