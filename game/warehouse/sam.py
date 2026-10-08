"""SAM missiles (and ships' missiles) drawn from their base's munitions.

With "Limited SAM missiles" on, each land SAM site's missiles come out of the stock of
the base it belongs to (its control point), and are bought and shipped like any other
munition. DCS can't spawn a launcher partly loaded, so the limit is enforced by the
mission script instead: each site gets an allowance of missiles for the mission (its
full load, or its share of what the base holds), the script counts its launches, and a
site that has used its allowance holds fire. A missile fired past the allowance is
destroyed at launch, so the limit holds even if something else (Skynet) re-enables it.

Missile names and loads come from DCS: the script reports what each launcher type
carries, and the first mission with a new SAM type only learns it (that site fires
freely, its launches are still charged).

Stock keys are "sam." + the DCS weapon name without its "weapons." prefix, so SAM
missiles never reach the DCS airbase warehouses and stay apart from aircraft weapons.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, TYPE_CHECKING

from game.theater.controlpoint import NavalControlPoint, OffMapSpawn

if TYPE_CHECKING:
    from game import Game
    from game.theater import ControlPoint
    from game.theater.theatergroundobject import TheaterGroundObject
    from .state import WarehouseState

SAM_PREFIX = "sam."

#: A base is stocked for this many full loads of each of its SAM sites: what's in the
#: launchers plus one reload.
SAM_LOADS_STOCKED = 2

#: (pattern on the DCS missile name, price in $M, mass in kg). First match wins.
SAM_MISSILES: list[tuple[re.Pattern[str], float, float]] = [
    (re.compile(p, re.IGNORECASE), price, mass)
    for p, price, mass in [
        # DCS names SAM missiles by their Russian/US designators, e.g. "SA5B55"
        # (S-300's 5V55), "SA9M38M1" (Buk), "MIM_104" (Patriot). The "SA" there is
        # not a NATO SA-number, so those aren't matched.
        # Ships
        (r"SM_?[236]|RIM_?6[67]|RIM_?156|RIM_?174|standard", 2.0, 700),
        (r"RIM_?162|ESSM", 1.0, 280),
        (r"RIM_?116|\bRAM\b", 0.9, 75),
        (r"RIM_?7|sea_?sparrow", 0.4, 230),
        (r"BGM_?109|tomahawk", 1.5, 1300),
        (r"RGM_?84|AGM_?84|harpoon", 1.2, 690),
        # Land
        (r"MIM_?104|PAC-?\d|patriot", 4.0, 900),
        (r"48N6|5[VB]55|S-?300|S_300", 1.5, 1800),
        (r"5[VB]28|S-?200", 0.8, 7000),
        (r"AIM_?120|AMRAAM|NASAMS", 1.0, 160),
        (r"MIM_?23|hawk", 0.8, 600),
        (r"V-?75\d|V_?750|5[VB]27|V-?600|S-?75\b|S-?125", 0.3, 950),
        (r"9M38|9M317|buk", 0.6, 700),
        (r"3M9|kub", 0.4, 600),
        (r"9M33[01]|\btor\b", 0.4, 170),
        (r"9M311|57E6|tunguska|pantsir", 0.1, 60),
        (r"9M333|9M37|strela-?10", 0.08, 40),
        (r"9M33|\bosa\b", 0.2, 130),
        (r"9M31|strela", 0.05, 30),
        (r"roland", 0.15, 70),
        (r"rapier", 0.15, 45),
        (r"HQ-?7|crotale", 0.2, 85),
        (r"MIM_?72|chaparral", 0.1, 90),
        (r"FIM_?92|stinger|igla|9M39|9M313|mistral|starstreak", 0.04, 15),
    ]
]
DEFAULT_SAM_PRICE = 0.5
DEFAULT_SAM_MASS_KG = 400.0


def sam_key(weapon: str) -> str:
    """The stock key for a DCS SAM missile name (with or without "weapons.")."""
    if weapon.startswith(SAM_PREFIX):
        return weapon
    if weapon.startswith("weapons."):
        return SAM_PREFIX + weapon[len("weapons.") :]
    return SAM_PREFIX + "missiles." + weapon


def is_sam(resource: str) -> bool:
    return resource.startswith(SAM_PREFIX)


def _lookup(resource: str) -> Optional[tuple[float, float]]:
    short = resource.split(".", 2)[-1]
    for pattern, price, mass in SAM_MISSILES:
        if pattern.search(short):
            return price, mass
    return None


def sam_price(resource: str) -> float:
    found = _lookup(resource)
    return found[0] if found else DEFAULT_SAM_PRICE


def sam_mass_kg(resource: str) -> float:
    found = _lookup(resource)
    return found[1] if found else DEFAULT_SAM_MASS_KG


def enabled(game: Game) -> bool:
    settings = game.settings
    return bool(
        settings.logistics_enabled
        and getattr(settings, "logistics_limited_sam_missiles", False)
    )


def sam_loads(state: WarehouseState) -> dict[str, dict[str, int]]:
    """DCS unit type -> missiles one launcher of it carries (learned from DCS)."""
    loads = getattr(state, "sam_loads", None)
    if loads is None:
        loads = {}
        state.sam_loads = loads
    return loads


@dataclass
class SamSite:
    """One land SAM site (ground object) and the base its missiles come from."""

    cp: ControlPoint
    tgo: TheaterGroundObject
    #: DCS unit name -> DCS unit type, for live units.
    units: dict[str, str] = field(default_factory=dict)
    #: DCS group names of the site.
    groups: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return str(self.tgo.name)

    def full_load(self, loads: dict[str, dict[str, int]]) -> Counter[str]:
        """Missiles (DCS names) its live launchers carry when full, where known."""
        total: Counter[str] = Counter()
        for unit_type in self.units.values():
            total.update(loads.get(unit_type, {}))
        return total

    def unlearned(self, loads: dict[str, dict[str, int]]) -> bool:
        return any(t not in loads for t in self.units.values())


def sam_sites(game: Game, cp: Optional[ControlPoint] = None) -> Iterator[SamSite]:
    """SAM sites and missile-armed ships at managed bases (all bases, or just `cp`).

    Land SAM sites draw on their base. Ships draw on the base they belong to: a
    carrier or LHA group (escorts included) on that carrier's stock, which its
    replenishment ships refill; other naval groups on their control point's stock.
    """
    from game.theater.theatergroundobject import NavalGroundObject, SamGroundObject

    state = game.warehouse_logistics
    points = [cp] if cp is not None else list(game.theater.controlpoints)
    for point in points:
        if isinstance(point, OffMapSpawn):
            continue
        if point.captured.is_neutral or not state.is_managed(point, game.settings):
            continue
        naval_base = isinstance(point, NavalControlPoint)
        for tgo in point.ground_objects:
            if tgo.is_dead:
                continue
            if isinstance(tgo, NavalGroundObject):
                pass
            elif naval_base or not isinstance(tgo, SamGroundObject):
                continue
            site = SamSite(point, tgo)
            for group in tgo.groups:
                live = [
                    u for u in group.units if u.alive and (u.is_vehicle or u.is_ship)
                ]
                if not live:
                    continue
                site.groups.append(group.group_name)
                for unit in live:
                    site.units[unit.unit_name] = str(unit.type.id)
            if site.units:
                yield site


def authorized_sam(game: Game, cp: ControlPoint) -> dict[str, int]:
    """SAM missiles a base is stocked for: SAM_LOADS_STOCKED loads of its sites."""
    if not enabled(game):
        return {}
    loads = sam_loads(game.warehouse_logistics)
    total: Counter[str] = Counter()
    for site in sam_sites(game, cp):
        for name, count in site.full_load(loads).items():
            total[sam_key(name)] += count * SAM_LOADS_STOCKED
    return dict(total)


def seed_new_missiles(game: Game) -> None:
    """Fills stock for SAM missiles a base has never held (just learned or enabled).

    Without this, turning the option on mid-campaign, or learning a new SAM type,
    would leave those sites with nothing to fire. They start at the side's starting
    supply level, as a base did when the campaign began.
    """
    state = game.warehouse_logistics
    seeded: set[str] = getattr(state, "sam_seeded", None) or set()
    state.sam_seeded = seeded
    settings = game.settings
    for cp in game.theater.controlpoints:
        stock = state.stocks.get(cp.id)
        if stock is None:
            continue
        fill = (
            settings.logistics_player_starting_supply
            if cp.captured.is_blue
            else settings.logistics_enemy_starting_supply
        )
        for name, count in authorized_sam(game, cp).items():
            mark = f"{cp.id}|{name}"
            if mark in seeded:
                continue
            seeded.add(mark)
            stock.munitions[name] = stock.munitions.get(name, 0) + int(count * fill)


def allowances(game: Game) -> dict[str, Any]:
    """The mission script's SAM data: sites, their allowances, and unit lookup.

    Each base's stock of a missile is shared between its sites in proportion to their
    full loads; no site gets more than its full load.
    """
    state = game.warehouse_logistics
    loads = sam_loads(state)
    sites: dict[str, Any] = {}
    units: dict[str, str] = {}
    by_cp: dict[Any, list[SamSite]] = {}
    for site in sam_sites(game):
        by_cp.setdefault(site.cp.id, []).append(site)
    for cp_id, cp_sites in by_cp.items():
        stock = state.stocks.get(cp_id)
        held = stock.munitions if stock is not None else {}
        full = {site.key: site.full_load(loads) for site in cp_sites}
        totals: Counter[str] = Counter()
        for load in full.values():
            totals.update(load)
        allow: dict[str, dict[str, int]] = {site.key: {} for site in cp_sites}
        for name, total in totals.items():
            have = max(0, int(held.get(sam_key(name), 0)))
            if have >= total:
                for site in cp_sites:
                    if full[site.key][name]:
                        allow[site.key][name] = full[site.key][name]
                continue
            # Largest-remainder split of what the base holds, capped at full loads.
            wants = {site.key: full[site.key][name] for site in cp_sites}
            exact = {k: have * w / total for k, w in wants.items() if w}
            given = {k: math.floor(v) for k, v in exact.items()}
            left = have - sum(given.values())
            for key in sorted(exact, key=lambda k: (given[k] - exact[k], k)):
                if left <= 0:
                    break
                if given[key] < wants[key]:
                    given[key] += 1
                    left -= 1
            for key, count in given.items():
                allow[key][name] = count
        for site in cp_sites:
            sites[site.key] = {
                "cp": str(cp_id),
                "allow": allow[site.key],
                "groups": site.groups,
            }
            for unit_name in site.units:
                units[unit_name] = site.key
    return {"sites": sites, "units": units}


def apply_results(game: Game, data: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Charges SAM launches to their bases and stores learned launcher loads.

    Returns base name -> missiles used, for the debriefing summary.
    """
    if not data.get("sam_loads") and not data.get("sam_used"):
        return {}
    state = game.warehouse_logistics
    loads = sam_loads(state)
    for unit_type, load in _as_dict(data.get("sam_loads")).items():
        missiles = {str(k): int(v) for k, v in _as_dict(load).items() if int(v) > 0}
        if missiles:
            known = loads.setdefault(str(unit_type), {})
            for name, count in missiles.items():
                known[name] = max(known.get(name, 0), count)
    used_by_base: dict[str, dict[str, int]] = {}
    by_id = {str(cp.id): cp for cp in game.theater.controlpoints}
    for cp_id, used in _as_dict(data.get("sam_used")).items():
        cp = by_id.get(str(cp_id))
        if cp is None or cp.id not in state.stocks:
            continue
        stock = state.stocks[cp.id]
        spent: dict[str, int] = {}
        for name, count in _as_dict(used).items():
            key = sam_key(str(name))
            count = int(count)
            if count <= 0:
                continue
            stock.munitions[key] = max(0, stock.munitions.get(key, 0) - count)
            spent[key] = count
        if spent:
            used_by_base[cp.name] = spent
    return used_by_base


def _as_dict(value: Any) -> dict[Any, Any]:
    return value if isinstance(value, dict) else {}
