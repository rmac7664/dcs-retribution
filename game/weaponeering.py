"""Desired weapon effects: how many weapons it takes to destroy a target.

A planning aid, not a damage model. Targets and weapons are each sorted into a
few broad kinds, and resources/weaponeering.yaml says how many weapons of each
kind one target of each kind takes. The figures are meant to be corrected by
hand from experience.
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, TYPE_CHECKING

import yaml

from game.data.units import UnitClass

if TYPE_CHECKING:
    from game.ato.loadouts import Loadout
    from game.theater.theatergroup import TheaterUnit

TABLE_PATH = Path("resources/weaponeering.yaml")

#: Vehicle classes that are not soft. Everything else on wheels or tracks is.
_VEHICLE_KINDS = {
    UnitClass.TANK: "armor",
    UnitClass.APC: "light_armor",
    UnitClass.IFV: "light_armor",
    UnitClass.ATGM: "light_armor",
    UnitClass.RECON: "light_armor",
    UnitClass.ARTILLERY: "light_armor",
    UnitClass.SHORAD: "light_armor",
    UnitClass.TELAR: "light_armor",
    UnitClass.FORTIFICATION: "small_building",
}

#: Building objectives by their category. Anything not listed is a small building.
_BUILDING_KINDS = {
    "commandcenter": "hardened",
    "ww2bunker": "hardened",
    "ammo": "large_building",
    "factory": "large_building",
    "power": "large_building",
    "ware": "large_building",
    "oil": "large_building",
}

#: Weapon kinds by DCS munition name, first match wins. Names are the part after
#: "weapons.bombs." and so on, e.g. "GBU_12". Munitions that match nothing here
#: (air-to-air, anti-radiation, anti-ship, training, flares) are not counted.
_WEAPON_KINDS: tuple[tuple[str, str], ...] = (
    (r"^(BDU|CATM|TGM|MK76|MK106|LUU|SAB|OH58D|BEER|IAB|RN-|AGM_122)", ""),
    # Stand-off land-attack missiles with a warhead of about 500 lb.
    (r"^(AGM_84[EH]|X_59M|KD_63|CM[-_]802AKG|C_802AK)", "bomb_500"),
    (r"^(GBU_31_V_[34]B|GBU_24E|GBU_27|GBU_28|BetAB|BETAB)", "penetrator"),
    (r"^(CBU_97|CBU_105|AGM_154B|GB-6-SFW)", "sfw"),
    (
        r"^(CBU|ROCKEYE|RBK|BL_755|BLG66|BLU-|AGM_154A?$|BK90|DWS39|GB-6$|BKF)",
        "cluster",
    ),
    (
        r"^(Mk_84|MK_84|GBU_10|GBU_24|GBU_31|GBU_15|GBU_8|GBU_43|HB_F4E_GBU15|"
        r"AGM_130|KAB_1500|FAB_1500)",
        "bomb_2000",
    ),
    (
        r"^(Mk_83|GBU_16|GBU_32|AN_M65|AN_M66|FAB[-_]500|KAB_500|BR_500|SC_500|"
        r"SD_500|SAMP400|AGM_62|LS_6_500|ODAB|AGM_154C|GB-6-HE)",
        "bomb_1000",
    ),
    (
        r"^(Mk_82|MK_82|GBU_12|GBU_38|GBU_54|FAB[-_]250|AN_M64|SAMP250|M_117|"
        r"BR_250|SC_250|SD_250|British_.*500LB|BOLT|LS_6$|250-[23]|Type_200)",
        "bomb_500",
    ),
    (
        r"^(Mk_81|GBU_39|FAB_100|FAB_50|OFAB|AN_M57|AN_M30|SAMP125|SC_50|"
        r"British_.*250LB|LS_6_100|BAP_100|BAT-120|AO_25|P-50)",
        "bomb_250",
    ),
    (r"^(AGM_65[EFGKL]|RB75T|X_29|Kh-66|S_25L|AGM_12C)", "missile_large"),
    (r"^(AGM_65|RB75|X_25M[LR]|AGM_12|C_701)", "missile_small"),
    (
        r"^(AGM_114|Vikhr|Ataka|AT_6|TOW|HOT3|SPIKE|AKD-10|HJ-12|BRM-1)",
        "atgm",
    ),
    (r"^(C_24|C_25|S-24|S-25|Tiny Tim|WGr21|C_13|Zuni|HVAR)", "rocket_heavy"),
    (
        r"^(HYDRA|FFAR|C_5$|C_8$|S_5|S5M|SNEB|ARF8|ARAK|RS-82|M8rocket|R4M|"
        r"British_.*3INCH|Rkt_90)",
        "rocket",
    ),
)

#: Bombs that are steered onto the target. Other bombs get the unguided factor.
_GUIDED = re.compile(
    r"^(GBU|KAB|AGM|LS_6|GB-6|BK90|DWS39|BOLT|CBU_103|CBU_105|HB_F4E_GBU|X_59M|KD_63|"
    r"CM[-_]802AKG|C_802AK)"
)
_FREE_FALL_KINDS = frozenset(
    {
        "bomb_250",
        "bomb_500",
        "bomb_1000",
        "bomb_2000",
        "penetrator",
        "cluster",
        "sfw",
    }
)


@dataclass(frozen=True)
class WeaponKind:
    """One column of the table, as a particular munition delivers it."""

    key: str
    guided: bool


@dataclass(frozen=True)
class Table:
    unguided_factor: float
    weapon_labels: Mapping[str, str]
    target_labels: Mapping[str, str]
    #: (target kind, weapon kind) -> weapons per target. Missing = not suited.
    cells: Mapping[tuple[str, str], float]

    def per_target(self, target: str, weapon: WeaponKind) -> Optional[float]:
        base = self.cells.get((target, weapon.key))
        if base is None:
            return None
        if not weapon.guided and weapon.key in _FREE_FALL_KINDS:
            return base * self.unguided_factor
        return base


def parse_table(data: Any) -> Table:
    """Builds the table from the parsed file, skipping anything malformed."""
    if not isinstance(data, dict):
        data = {}
    weapons = data.get("weapons")
    targets = data.get("targets")
    weapon_labels = (
        {str(k): str(v) for k, v in weapons.items()}
        if isinstance(weapons, dict)
        else {}
    )
    target_labels: dict[str, str] = {}
    cells: dict[tuple[str, str], float] = {}
    for target, row in (targets.items() if isinstance(targets, dict) else ()):
        if not isinstance(row, dict):
            continue
        target_labels[str(target)] = str(row.get("label", target))
        for weapon, value in row.items():
            if weapon == "label" or isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and value > 0:
                cells[(str(target), str(weapon))] = float(value)
    try:
        factor = float(data.get("unguided_factor", 2))
    except (TypeError, ValueError):
        factor = 2.0
    return Table(max(1.0, factor), weapon_labels, target_labels, cells)


@lru_cache(maxsize=1)
def table() -> Table:
    try:
        with TABLE_PATH.open(encoding="utf-8") as table_file:
            return parse_table(yaml.safe_load(table_file))
    except (OSError, yaml.YAMLError):
        logging.exception("Could not read %s", TABLE_PATH)
        return parse_table({})


def weapon_kind(munition: str) -> Optional[WeaponKind]:
    """The kind of a DCS munition such as "weapons.bombs.GBU_12", if it has one."""
    name = munition.rsplit(".", 1)[-1] if munition.startswith("weapons.") else munition
    for pattern, key in _WEAPON_KINDS:
        if re.match(pattern, name):
            if not key:
                return None
            return WeaponKind(
                key, bool(_GUIDED.match(name)) or key not in _FREE_FALL_KINDS
            )
    return None


def target_kind(unit: TheaterUnit) -> Optional[str]:
    """The kind of target a unit is, or None for one this table does not cover."""
    if unit.is_ship:
        return None
    if unit.is_static:
        category = getattr(unit.ground_object, "category", "")
        return _BUILDING_KINDS.get(category, "small_building")
    try:
        unit_type = unit.unit_type
    except (KeyError, StopIteration):
        unit_type = None
    unit_class = getattr(unit_type, "unit_class", None)
    if not isinstance(unit_class, UnitClass):
        return "soft"
    return _VEHICLE_KINDS.get(unit_class, "soft")


def weapons_required(units: Iterable[TheaterUnit], weapon: WeaponKind) -> Optional[int]:
    """Weapons of one kind to destroy all of these targets.

    None when the weapon suits none of them. Targets it does not suit, and ones
    the table does not cover, are left out of the count.
    """
    total = 0.0
    suited = False
    for unit in units:
        kind = target_kind(unit)
        if kind is None:
            continue
        needed = table().per_target(kind, weapon)
        if needed is None:
            continue
        suited = True
        total += needed
    return math.ceil(total - 1e-9) if suited else None


@dataclass(frozen=True)
class Requirement:
    label: str
    count: int


def requirements(units: Iterable[TheaterUnit]) -> list[Requirement]:
    """What it takes to destroy these targets with each kind of guided weapon.

    One line per weapon kind that suits every target the table covers, in the
    table's order, for the planning windows.
    """
    units = list(units)
    covered = [unit for unit in units if target_kind(unit) is not None]
    if not covered:
        return []
    lines = []
    for key, label in table().weapon_labels.items():
        weapon = WeaponKind(key, guided=True)
        if any(
            table().per_target(target_kind(unit) or "", weapon) is None
            for unit in covered
        ):
            continue
        count = weapons_required(covered, weapon)
        if count:
            lines.append(Requirement(label, count))
    return lines


def carried(loadouts: Iterable[Loadout], catalog: Any) -> Counter[str]:
    """Air-to-ground munitions carried by these aircraft, by DCS munition name."""
    total: Counter[str] = Counter()
    for loadout in loadouts:
        # An empty pylon is stored as None.
        weapons = [weapon for weapon in loadout.pylons.values() if weapon is not None]
        demand, _unverified, _unknown = catalog.demand(weapons)
        for munition, count in demand.items():
            if weapon_kind(munition) is not None:
                total[munition] += count
    return total


def munition_label(munition: str) -> str:
    """ "GBU-12" for "weapons.bombs.GBU_12"."""
    return munition.rsplit(".", 1)[-1].replace("_", "-")


def for_carried(
    units: Iterable[TheaterUnit], munitions: Mapping[str, int]
) -> list[tuple[str, Optional[int], int]]:
    """(munition, needed for these targets, carried) for each carried munition."""
    units = list(units)
    lines = []
    for munition, count in munitions.items():
        kind = weapon_kind(munition)
        if kind is None:
            continue
        lines.append((munition_label(munition), weapons_required(units, kind), count))
    return lines


def catalog_for(game: Any) -> Any:
    """The munitions catalog that says what each pylon carries."""
    from game.warehouse.munitions import MunitionCatalog

    state = getattr(game, "warehouse_logistics", None)
    return MunitionCatalog(getattr(state, "learned", {}) or {})


def summary_for_flights(
    flights: Iterable[Any], targets: Iterable[Any], game: Any
) -> str:
    """One sentence comparing what strike flights carry with what targets need.

    ``targets`` are ground objectives; everything alive at each is counted.
    """
    units = [
        unit for target in targets for unit in getattr(target, "strike_targets", [])
    ]
    loadouts = [
        member.loadout for flight in flights for member in flight.iter_members()
    ]
    lines = for_carried(units, carried(loadouts, catalog_for(game)))
    if not lines:
        return "The strike flights carry no weapons this estimate covers."
    parts = []
    for label, needed, count in lines:
        if needed is None:
            parts.append(f"{count}× {label} carried (not suited to these targets)")
        else:
            verdict = "enough" if count >= needed else "short"
            parts.append(f"{count}× {label} carried, about {needed} needed ({verdict})")
    return "Weapons: " + "; ".join(parts) + "."
