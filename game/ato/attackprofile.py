"""Low-level attack profiles for strike flights.

A strike normally runs in at the aircraft's combat altitude. A flight can instead fly
the last leg low, under the radar, and then attack in one of three ways:

* Low-level sled: stay low and release in level flight. Best with retarded or
  guided bombs; with slick bombs the AI climbs to a safe release height first.
* Pop-up dive: run in low, climb near the target and dive-bomb it.
* Loft: run in low, pull up and toss the bombs from several miles out. DCS AI cannot
  loft, so this is for player flights; an AI flight given it flies the sled.

The profile is set per flight. Keeping the AI off the ground is the main concern:
the low legs are given as a height above the ground (DCS's "RADIO" altitude), which
the AI follows over hills; the height is raised where the terrain is rough or its
height is unknown; and every release altitude is set above the target's own ground
elevation, never below a floor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional, TYPE_CHECKING

from game.ato.flighttype import FlightType
from game.utils import Distance, feet, knots, meters, nautical_miles

if TYPE_CHECKING:
    from dcs.mapping import Point

    from game.ato.flight import Flight


class AttackProfile(Enum):
    NORMAL = "Normal"
    SLED = "Low-level sled"
    POPUP = "Pop-up dive"
    LOFT = "Loft (player flights)"

    @property
    def low_level(self) -> bool:
        return self is not AttackProfile.NORMAL

    @property
    def short_name(self) -> str:
        """The name without the note on who can fly it."""
        return self.value.split(" (")[0]


#: The bombing tasks that can fly a low-level attack. Not CAS or armed recon, which
#: hunt over an area rather than attack one point.
SUPPORTED_TASKS = frozenset(
    {
        FlightType.STRIKE,
        FlightType.BAI,
        FlightType.OCA_RUNWAY,
        FlightType.OCA_AIRCRAFT,
        FlightType.DEAD,
        FlightType.ANTISHIP,
    }
)

#: Tasks where only the route is flown low. Their attacks are left to the AI:
#: anti-ship missiles fly their own profile, and a defended ship is no target for
#: a low bombing pass.
ROUTE_ONLY_TASKS = frozenset({FlightType.ANTISHIP})

#: The low-level height over flat or gently rolling ground.
LOW_HEIGHT = feet(500)

#: The low-level height over hills, or wherever the ground height is unknown.
ROUGH_HEIGHT = feet(1000)

#: Ground that rises and falls more than this along the run-in counts as rough.
ROUGH_RELIEF = meters(250)

#: Beyond this the run-in is mountainous: AI flights fly the normal profile there.
MOUNTAIN_RELIEF = meters(800)

#: How far before the ingress the flight is down at low level.
LOW_RUN_IN = nautical_miles(25)

#: An ingress farther than this from its target is a stand-off launch point.
STANDOFF_RUN_IN = nautical_miles(30)

#: How far past the last target the flight stays low before climbing out.
LOW_EGRESS = nautical_miles(20)

#: Where the pop-up dive tops out, above the target.
POPUP_HEIGHT = feet(3000)

#: How far from the target the pop-up starts.
POPUP_DISTANCE = nautical_miles(4)

#: The lowest release height for slick (unretarded) unguided bombs, to stay clear
#: of their own fragments.
SLICK_RELEASE_HEIGHT = feet(1500)

#: Loft entry speed and climb angle the kneeboard estimate is worked for.
LOFT_SPEED = knots(480)
LOFT_ANGLE_DEG = 30.0
LOFT_G = 4.0

#: How far the drag-free toss range is cut for drag. A rough figure for low-drag
#: bombs; the jet's own release cue is what the pilot flies.
LOFT_DRAG_FACTOR = 0.85

_G = 9.81


#: Heavy bombers, by DCS type name. They bomb in level formation from altitude and
#: are not flown low.
HEAVY_BOMBERS = frozenset(
    {"B-17G", "B-52H", "B-1B", "Tu-22M3", "Tu-95MS", "Tu-142", "Tu-160", "H-6J"}
)


def supported(flight: Flight) -> bool:
    """Whether this flight can be given a low-level profile."""
    if flight.flight_type not in SUPPORTED_TASKS or flight.is_helo:
        return False
    dcs_type = getattr(getattr(flight, "unit_type", None), "dcs_unit_type", None)
    return getattr(dcs_type, "id", None) not in HEAVY_BOMBERS


def profile_of(flight: Flight) -> AttackProfile:
    """The profile chosen for the flight, Normal for anything that cannot fly one."""
    profile = getattr(flight, "attack_profile", AttackProfile.NORMAL)
    if not isinstance(profile, AttackProfile) or not supported(flight):
        return AttackProfile.NORMAL
    return profile


def flown_profile(flight: Flight) -> AttackProfile:
    """The profile the flight will actually fly.

    The AI cannot loft, so an AI flight set to loft flies the sled instead.
    """
    profile = profile_of(flight)
    if profile.low_level and carries_arms(flight):
        return AttackProfile.NORMAL
    if profile is AttackProfile.LOFT and flight.client_count == 0:
        return AttackProfile.SLED
    return profile


def carries_arms(flight: Flight) -> bool:
    """Whether a DEAD flight carries anti-radiation missiles.

    Those need altitude to see and reach the radar, so such a flight keeps the
    normal profile. Bomb and Maverick DEAD flights can go in low.
    """
    from game.data.weapons import WeaponType

    if flight.flight_type is not FlightType.DEAD:
        return False
    try:
        return flight.any_member_has_weapon_of_type(WeaponType.ARM)
    except Exception:
        return False


def route_only(flight: Flight) -> bool:
    """Whether only the route is flown low, with the attack left as it is."""
    return flight.flight_type in ROUTE_ONLY_TASKS


def ground_m(position: Point) -> Optional[float]:
    """The ground height at a point in metres, or None when it cannot be looked up."""
    from game.elevation import elevation_m

    try:
        where = position.latlng()
    except (AttributeError, ValueError):
        return None
    return elevation_m(where.lat, where.lng)


def _samples(
    start: Point, end: Point, every: Distance = nautical_miles(1)
) -> list[Point]:
    length = start.distance_to_point(end)
    count = max(1, math.ceil(length / every.meters))
    heading = start.heading_between_point(end)
    return [
        start.point_from_heading(heading, length * i / count) for i in range(count + 1)
    ]


@dataclass(frozen=True)
class Terrain:
    """What the ground does along a low-level leg."""

    #: Highest minus lowest ground along the leg, or None if any of it is unknown.
    relief: Optional[Distance]

    @property
    def known(self) -> bool:
        return self.relief is not None

    @property
    def rough(self) -> bool:
        return self.relief is None or self.relief.meters > ROUGH_RELIEF.meters

    @property
    def mountainous(self) -> bool:
        return self.relief is not None and self.relief.meters > MOUNTAIN_RELIEF.meters


def terrain_along(legs: Iterable[tuple[Point, Point]]) -> Terrain:
    heights: list[float] = []
    for start, end in legs:
        for point in _samples(start, end):
            height = ground_m(point)
            if height is None:
                return Terrain(None)
            heights.append(height)
    if not heights:
        return Terrain(None)
    return Terrain(meters(max(heights) - min(heights)))


def low_height(terrain: Terrain) -> Distance:
    """How high above the ground to fly the low legs."""
    return ROUGH_HEIGHT if terrain.rough else LOW_HEIGHT


def release_altitude_m(target: Point, height: Distance) -> Optional[float]:
    """An attack altitude above sea level, ``height`` over the target's ground.

    None when the target's ground height is unknown: the AI is then left to pick its
    own attack altitude, which is higher but never into the ground.
    """
    ground = ground_m(target)
    if ground is None:
        return None
    return max(0.0, ground) + height.meters


def is_retarded_or_guided(munition: str) -> bool:
    """True for bombs that are safe to release from a low-level pass."""
    name = munition.rsplit(".", 1)[-1].upper()
    if any(tag in name for tag in ("SNAKEYE", "AIR", "HD", "BALLUTE", "_TU", "SHP")):
        return True
    return name.startswith(("GBU", "KAB", "AGM", "LS_6", "GB-6", "BK90", "DWS39"))


@dataclass(frozen=True)
class Loft:
    """A starting point for a loft attack, for the kneeboard."""

    pull_up: Distance
    release: Distance
    speed_kt: int
    angle_deg: int


def loft_solution(entry_height: Distance = LOW_HEIGHT) -> Loft:
    """Pull-up and release distances from the target for the standard entry.

    Drag-free toss range from the release point, cut for drag, plus the ground
    covered while pulling from level flight into the climb.
    """
    speed = LOFT_SPEED.meters_per_second
    angle = math.radians(LOFT_ANGLE_DEG)
    vx = speed * math.cos(angle)
    vy = speed * math.sin(angle)
    toss = vx * (vy + math.sqrt(vy * vy + 2 * _G * entry_height.meters)) / _G
    release = toss * LOFT_DRAG_FACTOR
    radius = speed * speed / (_G * (LOFT_G - 1))
    pull = radius * math.sin(angle)
    return Loft(
        pull_up=meters(release + pull),
        release=meters(release),
        speed_kt=round(LOFT_SPEED.knots),
        angle_deg=round(LOFT_ANGLE_DEG),
    )


@dataclass(frozen=True)
class AttackSettings:
    """What the AI attack tasks are told for a low-level profile."""

    #: Attack altitude above sea level, or None to leave it to the AI.
    altitude_m: Optional[float]
    #: DCS attack type ("Dive"), or None for a level attack.
    attack_type: Optional[str]


def attack_settings(
    profile: AttackProfile,
    low: Distance,
    target: Point,
    munitions: Iterable[str],
) -> Optional[AttackSettings]:
    """Release altitude and attack type for one target, or None for a normal attack.

    The altitude is always measured from the target's own ground, never below the
    low-level height, and higher for slick bombs whose fragments would reach a
    low-flying aircraft.
    """
    if profile is AttackProfile.POPUP:
        return AttackSettings(release_altitude_m(target, POPUP_HEIGHT), "Dive")
    if profile in (AttackProfile.SLED, AttackProfile.LOFT):
        height = low
        bombs = [
            m for m in munitions if ".bombs." in m or m.startswith("weapons.bombs")
        ]
        if any(not is_retarded_or_guided(bomb) for bomb in bombs):
            height = max(height, SLICK_RELEASE_HEIGHT, key=lambda d: d.meters)
        return AttackSettings(release_altitude_m(target, height), None)
    return None


def briefing_lines(layout: object, route_numbers: Iterable[int] = ()) -> list[str]:
    """What the strike kneeboard page says about the flight's attack profile."""
    name = getattr(layout, "attack_profile", None)
    height_ft = getattr(layout, "low_level_height_ft", None)
    if name is None or height_ft is None:
        return []
    try:
        profile = AttackProfile(name)
    except ValueError:
        return []
    height = feet(height_ft)
    numbers = list(route_numbers)
    point = ""
    if numbers:
        label = "STPT" if len(numbers) == 1 else "STPTs"
        point = f" ({label} {', '.join(str(n) for n in numbers)})"
    lines = [
        f"{profile.short_name.upper()}: {height_ft:,} ft AGL from the descent point "
        f"({LOW_RUN_IN.nautical_miles:.0f} NM before the ingress) to the climb point "
        f"({LOW_EGRESS.nautical_miles:.0f} NM past the last target)."
    ]
    if profile is AttackProfile.LOFT:
        loft = loft_solution(height)
        lines.append(
            f"Pull up{point} {loft.pull_up.nautical_miles:.1f} NM from the target at "
            f"{loft.speed_kt} KTAS, {loft.angle_deg} deg climb at {LOFT_G:g} g. "
            f"Release about {loft.release.nautical_miles:.1f} NM out for low-drag "
            "bombs; fly the jet's own release cue."
        )
    elif profile is AttackProfile.POPUP:
        lines.append(
            f"Pop up{point} {POPUP_DISTANCE.nautical_miles:.0f} NM from the target "
            f"to about {POPUP_HEIGHT.feet:,.0f} ft above it, then roll in and dive."
        )
    else:
        lines.append(
            "Level release from the low-level pass. Slick bombs: release no lower "
            f"than {SLICK_RELEASE_HEIGHT.feet:,.0f} ft above the target for "
            "fragments."
        )
    return lines
