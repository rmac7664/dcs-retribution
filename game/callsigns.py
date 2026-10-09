"""Support for working with DCS group callsigns."""

import logging
import re
from typing import Any

from dcs.flyingunit import FlyingUnit
from dcs.unitgroup import FlyingGroup


def callsign_for_support_unit(group: FlyingGroup[Any]) -> str:
    # Either something like Overlord11 for Western AWACS, or else just a number.
    # Convert to either "Overlord" or "Flight 123".
    lead = group.units[0]
    raw_callsign = lead.callsign_as_str()
    try:
        return f"Flight {int(raw_callsign)}"
    except ValueError:
        return f"{raw_callsign[:-2]} {raw_callsign[-2]}"


def create_group_callsign_from_unit(lead: FlyingUnit) -> str:
    raw_callsign = lead.callsign_as_str()
    if not lead.callsign_is_western:
        # Callsigns for non-Western countries are just a number per flight,
        # similar to tail numbers.
        return f"Flight {raw_callsign}"

    # Callsign from pydcs is in the format `<name><group ID><unit ID>`,
    # where unit ID is guaranteed to be a single digit but the group ID may
    # be more.
    match = re.search(r"^(\D+)(\d+)(\d)$", raw_callsign)
    if match is None:
        logging.error(f"Could not parse unit callsign: {raw_callsign}")
        return f"Flight {raw_callsign}"
    return f"{match.group(1)} {match.group(2)}"


#: Real-world callsigns for aircraft that DCS gives no type-specific list (DCS only
#: offers its generic Enfield, Springfield... for these). Keyed by the start of the
#: DCS type id. Only offered to US and Combined Joint Task Forces flights.
#:
#: F-14: Tomcat-era US Navy squadron callsigns (Avia Magazine F-14 order of battle,
#: Wikipedia squadron articles).
EXTRA_CALLNAMES: dict[str, list[str]] = {
    "F-14": [
        "Aardvark",  # VF-114
        "Black Lion",  # VF-213
        "Bullet",  # VF-2
        "Camelot",  # VF-14
        "Dakota",  # VF-142
        "Dback",  # VF-102
        "Dog",  # VF-143
        "Eagles",  # VF-51
        "Fast Eagle",  # VF-41
        "Felix",  # VF-31
        "Freelance",  # VF-21
        "Gunfighter",  # VF-101
        "Gypsy",  # VF-32
        "Knight",  # VF-154
        "Nickel",  # VF-211
        "Rage",  # VF-24
        "Ripper",  # VF-11
        "Starfighter",  # VF-33 (from 1987)
        "Sundowner",  # VF-111
        "Tarbox",  # VF-33
        "Victory",  # VF-84, VF-103
        "Wichita",  # VF-1
    ],
}


def _uses_extra_callnames(country_name: str) -> bool:
    return country_name == "USA" or "Combined Joint Task Forces" in country_name


def extra_callnames(dcs_type_id: str, country_name: str) -> list[str]:
    """Extra callsigns offered for this aircraft type flown by this country."""
    if not _uses_extra_callnames(country_name):
        return []
    for prefix, names in EXTRA_CALLNAMES.items():
        if dcs_type_id.startswith(prefix):
            return list(names)
    return []


def dcs_callnames(country: Any, dcs_type: Any) -> list[str]:
    """The callsigns DCS itself knows for this type and country, in DCS's order.

    This mirrors pydcs: the country's generic list for the category, then the
    aircraft type's own list (e.g. the Hornet's "Ragin", "Sting"...).
    """
    category = "Air" if dcs_type.category == "Interceptor" else dcs_type.category
    names = list(country.callsign.get(category, []))
    callnames = getattr(dcs_type, "callnames", None) or {}
    if "Combined Joint Task Forces" in country.name:
        for type_names in callnames.values():
            names.extend(type_names)
    else:
        names.extend(callnames.get(country.shortname, []))
    return names


#: Callsign numbers run 1-9 in DCS.
MAX_CALLSIGN_NUMBER = 9


def apply_custom_callsign(group: FlyingGroup[Any], name: str, number: int) -> None:
    """Gives a group a callsign DCS doesn't have in its own list.

    DCS identifies callsigns by their position in its list, so the group keeps the
    first generic callsign's position (that's what DCS's AI voices will say) and the
    custom name is set as the callsign's name, which is what Retribution, the
    kneeboard and DCS's labels show.
    """
    for index, unit in enumerate(group.units, start=1):
        unit.callsign_dict = {
            1: 1,
            2: number,
            3: index,
            "name": f"{name}{number}{index}",
        }
