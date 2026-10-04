"""Bridges Retribution loadouts (launcher CLSIDs) and DCS warehouse items.

A DCS warehouse stocks individual munitions by resource name, e.g.
"weapons.missiles.AIM_120C". A Retribution pylon holds a launcher, e.g. "LAU-115 with
2 x AIM-120C", which consumes two of those. This module answers "which warehouse items
does this pylon draw, and how many?", using three sources in order of trust:

1. LEARNED: what DCS reported an aircraft actually carried at spawn (see
   dcs_retribution_warehouses.lua). Always right for the DCS version that reported it.
2. DATAMINE: resources/warehouse/launcher_munitions.json, generated from DCS' own
   launcher tables by resources/tools/generate_warehouse_munitions.py. Covers stock DCS.
3. HEURISTIC: for launchers neither source knows (mostly mods), borrow the munition from
   other launchers in the same Retribution weapon group and read the count from the
   name ("2 x AIM-120C"). Treated as unverified until DCS confirms it.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from enum import Enum
from pathlib import Path
from typing import ClassVar, Iterable, Mapping, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from game.data.weapons import Weapon
    from game.settings import Settings

LAUNCHER_DATA = Path("resources/warehouse/launcher_munitions.json")

#: Warehouse categories DCS reports through Unit.getAmmo(). Pods, fuel tanks and gun
#: shells never show up there, so they are never metered or learned.
AMMO_PREFIXES = (
    "weapons.missiles.",
    "weapons.bombs.",
    "weapons.nurs.",
    "weapons.torpedoes.",
)


class MunitionSource(Enum):
    LEARNED = "learned"
    DATAMINE = "datamine"
    HEURISTIC = "heuristic"


def is_ammo(resource: str) -> bool:
    return resource.startswith(AMMO_PREFIXES)


def metered_prefixes(settings: Settings) -> tuple[str, ...]:
    """Warehouse resource prefixes that are limited, per the campaign settings."""
    prefixes: list[str] = []
    if settings.logistics_meter_missiles:
        prefixes += ["weapons.missiles.", "weapons.torpedoes."]
    if settings.logistics_meter_bombs:
        prefixes.append("weapons.bombs.")
    if settings.logistics_meter_rockets:
        prefixes.append("weapons.nurs.")
    return tuple(prefixes)


def ammo_only(munitions: Mapping[str, int]) -> dict[str, int]:
    return {k: v for k, v in munitions.items() if is_ammo(k) and v}


_COUNT_PATTERNS = (
    # "2x/1x AIM-120C" (Super Hornet split stations)
    re.compile(r"(\d+)\s*[x×]\s*/\s*(\d+)\s*[x×]", re.IGNORECASE),
    # "2 x AIM-120C", "LAU-115 with 2 x LAU-127", "2xAIM-9M"
    re.compile(r"(?<![A-Za-z0-9.\-])(\d+)\s*[x×*]\s*(?=[A-Za-z(\[{_])", re.IGNORECASE),
    # "AIM-120C-8 AMRAAM x 2", "CBU-97 * 3", "Ammo AIM-120C*24"
    re.compile(r"[x×*]\s*(\d+)\b"),
)


def count_from_name(name: str) -> int:
    """Best-effort munition count from a launcher's display name."""
    split = _COUNT_PATTERNS[0].search(name)
    if split:
        return int(split.group(1)) + int(split.group(2))
    for pattern in _COUNT_PATTERNS[1:]:
        match = pattern.search(name)
        if match:
            value = int(match.group(1))
            if 0 < value <= 64:
                return value
    return 1


class MunitionCatalog:
    """Looks up the warehouse munitions consumed by each launcher."""

    _datamine: ClassVar[Optional[dict[str, dict[str, int]]]] = None
    _datamine_version: ClassVar[Optional[str]] = None

    def __init__(self, learned: Mapping[str, Mapping[str, int]]) -> None:
        self.learned = learned

    @classmethod
    def datamine(cls) -> dict[str, dict[str, int]]:
        if cls._datamine is None:
            try:
                with LAUNCHER_DATA.open(encoding="utf-8") as data_file:
                    data = json.load(data_file)
                cls._datamine = {
                    clsid: {str(k): int(v) for k, v in munitions.items()}
                    for clsid, munitions in data.get("launchers", {}).items()
                }
                cls._datamine_version = data.get("dcs_version")
            except (OSError, ValueError) as ex:
                logging.error(f"Could not load {LAUNCHER_DATA}: {ex}")
                cls._datamine = {}
        return cls._datamine

    def lookup(self, weapon: Weapon) -> Optional[tuple[dict[str, int], MunitionSource]]:
        """Warehouse items drawn by one launcher, or None if unknown."""
        if weapon.clsid == "<CLEAN>":
            return {}, MunitionSource.DATAMINE
        learned = self.learned.get(weapon.clsid)
        if learned is not None:
            return dict(learned), MunitionSource.LEARNED
        known = self.datamine().get(weapon.clsid)
        if known is not None:
            return dict(known), MunitionSource.DATAMINE
        guess = self._heuristic(weapon)
        if guess is not None:
            return guess, MunitionSource.HEURISTIC
        return None

    def _known(self, clsid: str) -> Optional[dict[str, int]]:
        learned = self.learned.get(clsid)
        if learned is not None:
            return dict(learned)
        return self.datamine().get(clsid)

    def _heuristic(self, weapon: Weapon) -> Optional[dict[str, int]]:
        if weapon.weapon_group.name == "Unknown":
            return None
        resources: set[str] = set()
        for sibling in weapon.weapon_group.weapons:
            known = self._known(sibling.clsid)
            if known:
                resources.update(ammo_only(known))
        if len(resources) != 1:
            # No sibling to borrow from, or the group mixes munitions (e.g. AIM-120C
            # covers C-5, C-7 and C-8, which may be distinct items for some mods).
            return None
        try:
            name = weapon.name
        except KeyError:
            name = weapon.clsid
        return {resources.pop(): count_from_name(name)}

    def demand(
        self, weapons: Iterable[Weapon]
    ) -> tuple[Counter[str], list[Weapon], list[Weapon]]:
        """Total ammo drawn by a set of launchers.

        Returns (demand, unverified launchers, unknown launchers).
        """
        total: Counter[str] = Counter()
        unverified: list[Weapon] = []
        unknown: list[Weapon] = []
        for weapon in weapons:
            result = self.lookup(weapon)
            if result is None:
                unknown.append(weapon)
                continue
            munitions, source = result
            if source is MunitionSource.HEURISTIC:
                unverified.append(weapon)
            total.update(ammo_only(munitions))
        return total, unverified, unknown


def learn_from_observations(
    catalog: MunitionCatalog,
    learned: dict[str, dict[str, int]],
    observations: Iterable[tuple[list[str], Mapping[str, int]]],
) -> list[str]:
    """Updates `learned` from (pylon CLSIDs, ammo DCS reported at spawn) pairs.

    A launcher is learned when an observation pins it down: every other launcher on the
    aircraft is already trusted and the remainder divides evenly across the copies of
    the unknown one, or the heuristic guesses for all unknown launchers add up to exactly
    what DCS reported. Returns the CLSIDs learned.
    """
    from game.data.weapons import Weapon

    pending = [(clsids, ammo_only(observed)) for clsids, observed in observations]
    newly_learned: list[str] = []
    progress = True
    while progress and pending:
        progress = False
        remaining = []
        for clsids, observed in pending:
            trusted: Counter[str] = Counter()
            untrusted: Counter[str] = Counter()
            for clsid in clsids:
                known = learned.get(clsid)
                if known is None:
                    known = catalog.datamine().get(clsid)
                if known is not None:
                    trusted.update(ammo_only(known))
                else:
                    untrusted[clsid] += 1
            if not untrusted:
                if dict(trusted) != observed:
                    logging.warning(
                        "Warehouse logistics: DCS reported %s for launchers %s, "
                        "expected %s",
                        observed,
                        clsids,
                        dict(trusted),
                    )
                continue
            residual = Counter(observed)
            residual.subtract(trusted)
            if any(v < 0 for v in residual.values()):
                remaining.append((clsids, observed))
                continue
            residual = Counter({k: v for k, v in residual.items() if v})
            if len(untrusted) == 1:
                clsid, copies = next(iter(untrusted.items()))
                if all(v % copies == 0 for v in residual.values()):
                    learned[clsid] = {k: v // copies for k, v in residual.items()}
                    newly_learned.append(clsid)
                    progress = True
                    continue
            guesses: dict[str, dict[str, int]] = {}
            for clsid in untrusted:
                weapon = Weapon.with_clsid(clsid)
                guess = catalog.lookup(weapon) if weapon is not None else None
                if guess is None:
                    break
                guesses[clsid] = ammo_only(guess[0])
            else:
                guessed: Counter[str] = Counter()
                for clsid, copies in untrusted.items():
                    for k, v in guesses[clsid].items():
                        guessed[k] += v * copies
                if dict(guessed) == dict(residual):
                    for clsid in untrusted:
                        learned[clsid] = guesses[clsid]
                        newly_learned.append(clsid)
                    progress = True
                    continue
            remaining.append((clsids, observed))
        pending = remaining
    return newly_learned
