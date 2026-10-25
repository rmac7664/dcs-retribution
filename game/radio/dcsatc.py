"""Radio presets that match DCS-ATC (personal branch).

DCS-ATC (voice ATC over SRS) keeps its frequencies in two files in
``Saved Games\\DCS-ATC``, next to ``Saved Games\\DCS``:

* ``config.jsonc``: default station frequencies ("Frequencies") and per-airfield
  ATIS frequencies ("AtisFreqs").
* ``frequencies.jsonc``: per-map sections (``// ===== <map> =====``) with Center
  sectors and, for airfields that have them, their own ATIS, Ground, Tower,
  Departure and Approach frequencies. Commented-out airfield lines mean "use the
  DCS map frequency".

When those files are found, player flights get preset channels for what DCS-ATC
listens on (departure, arrival and divert airfields, the Centers on the route,
SkyEye AWACS), and Retribution never hands one of those frequencies to a flight
or package. Without the files nothing changes.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from dcs.mapping import LatLng, Point
from dcs.terrain.terrain import Airport, Terrain

from game.radio.radios import Radio, RadioFrequency, kHz, MHz

#: SkyEye (the group's GCI bot) always listens here.
SKYEYE_AWACS = MHz(269)

#: DCS-ATC's built-in station defaults that config.jsonc doesn't list.
BUILT_IN_DEFAULTS = {"carrier": [kHz(127500)], "redcrown": [kHz(252500)]}


def is_red_crown_type(unit_type_id: str) -> bool:
    """Ships DCS-ATC uses as Red Crown (cruisers/destroyers with air search)."""
    return any(k in unit_type_id for k in ("TICONDEROG", "Arleigh_Burke", "PERRY"))


CONTROLLERS = ("Ground", "Tower", "Departure", "Approach")

_MAP_MARKER = re.compile(r"^\s*//\s*=====\s*(.+?)\s*=====")
_AIRFIELD_LINE = re.compile(r'^\s*"([^"]+)"\s*:\s*(\{.*\})\s*,?\s*(?://\s*id\s*(\d+))?')
_ID_COMMENT = re.compile(r"//\s*id\s*(\d+)")


def strip_jsonc(text: str) -> str:
    """JSON with comments and trailing commas -> plain JSON."""
    out = []
    i = 0
    in_string = False
    while i < len(text):
        c = text[i]
        if in_string:
            out.append(c)
            if c == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_string = False
        elif c == '"':
            in_string = True
            out.append(c)
        elif text.startswith("//", i):
            while i < len(text) and text[i] != "\n":
                i += 1
            continue
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end < 0 else end + 2
            continue
        else:
            out.append(c)
        i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def to_frequency(mhz: float) -> RadioFrequency:
    khz = int(round(mhz * 1000))
    return kHz(khz)


def _values(value: Any) -> list[RadioFrequency]:
    items = value if isinstance(value, list) else [value]
    out = []
    for v in items:
        if isinstance(v, (int, float)) and 30 <= v <= 400:
            out.append(to_frequency(float(v)))
    return out


def _preferred(freqs: list[RadioFrequency]) -> Optional[RadioFrequency]:
    """UHF first (what Retribution's inter-flight presets use), else the first."""
    for f in freqs:
        if f.hertz >= 225_000_000:
            return f
    return freqs[0] if freqs else None


@dataclass
class Center:
    name: str
    freq: RadioFrequency
    position: Point
    radius_m: float
    side: str = ""


@dataclass
class AirfieldEntry:
    """Own frequencies for one airfield from frequencies.jsonc."""

    name: str
    airport_id: Optional[int]
    values: dict[str, list[RadioFrequency]] = field(default_factory=dict)


@dataclass(frozen=True)
class Station:
    label: str
    freq: RadioFrequency
    #: Other frequencies the same station answers on (e.g. its VHF one).
    alternatives: tuple[RadioFrequency, ...] = ()

    def tunable_on(self, radio: Optional[Radio]) -> Optional[RadioFrequency]:
        """The frequency to preset on `radio` (UHF first), None if it can't."""
        options = (self.freq,) + self.alternatives
        if radio is None:
            return self.freq
        for f in options:
            if any(r.minimum.hertz <= f.hertz < r.maximum.hertz for r in radio.ranges):
                return f
        return None


@dataclass
class DcsAtcFrequencies:
    """What DCS-ATC listens on for the current map."""

    folder: Path
    terrain: Optional[Terrain] = None
    defaults: dict[str, list[RadioFrequency]] = field(default_factory=dict)
    atis_by_airfield: dict[str, RadioFrequency] = field(default_factory=dict)
    airfields: list[AirfieldEntry] = field(default_factory=list)
    centers: list[Center] = field(default_factory=list)

    # Loading -------------------------------------------------------------------

    @staticmethod
    def default_folder(dcs_saved_games: Path) -> Path:
        return dcs_saved_games.parent / "DCS-ATC"

    @classmethod
    def load(cls, folder: Path, terrain: Terrain) -> Optional[DcsAtcFrequencies]:
        """Reads DCS-ATC's files for `terrain`'s map, or None if not installed."""
        freqs_file = folder / "frequencies.jsonc"
        config_file = folder / "config.jsonc"
        if not freqs_file.exists() and not config_file.exists():
            return None
        data = cls(folder, terrain)
        if config_file.exists():
            try:
                config = json.loads(strip_jsonc(config_file.read_text("utf-8")))
            except (OSError, ValueError) as e:
                logging.warning("DCS-ATC: can't read %s (%s)", config_file, e)
                config = {}
            for name, value in (config.get("Frequencies") or {}).items():
                data.defaults[name.lower()] = _values(value)
            for name, value in (config.get("AtisFreqs") or {}).items():
                if found := _values(value):
                    data.atis_by_airfield[_norm(name)] = found[0]
        if freqs_file.exists():
            try:
                data._read_frequencies(freqs_file.read_text("utf-8"), terrain)
            except OSError as e:
                logging.warning("DCS-ATC: can't read %s (%s)", freqs_file, e)
        logging.info(
            "DCS-ATC: %d airfields with own frequencies, %d Center points on %s",
            len(data.airfields),
            len(data.centers),
            type(terrain).__name__,
        )
        return data

    def _read_frequencies(self, text: str, terrain: Terrain) -> None:
        wanted = _norm(type(terrain).__name__)
        current: Optional[str] = None  # None = before any map section: all maps
        in_centers = False
        for raw in text.splitlines():
            marker = _MAP_MARKER.match(raw)
            if marker:
                current = _norm(marker.group(1))
                in_centers = False
                continue
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            applies = current is None or current == wanted
            if in_centers:
                if line.startswith("]"):
                    in_centers = False
                    continue
                if applies:
                    self._read_center(line, terrain)
                continue
            if line.startswith('"CenterSectors"'):
                in_centers = not line.rstrip(",").endswith("]")
                continue
            match = _AIRFIELD_LINE.match(raw)
            if match is None or not applies:
                continue
            try:
                obj = json.loads(strip_jsonc(match.group(2)))
            except ValueError:
                logging.warning("DCS-ATC: can't read line %r", line)
                continue
            if not isinstance(obj, dict):
                continue
            id_match = _ID_COMMENT.search(raw)
            entry = AirfieldEntry(
                match.group(1), int(id_match.group(1)) if id_match else None
            )
            for key, value in obj.items():
                role = key.lower()
                if role in ("atis", "ground", "tower", "departure", "approach"):
                    entry.values[role] = _values(value)
            self.airfields.append(entry)

    def _read_center(self, line: str, terrain: Terrain) -> None:
        text = strip_jsonc(line).strip().rstrip(",")
        try:
            obj = json.loads(text)
        except ValueError:
            return
        freqs = _values(obj.get("Freq"))
        if not freqs:
            return
        if "Lat" in obj and "Lon" in obj:
            position = Point.from_latlng(
                LatLng(float(obj["Lat"]), float(obj["Lon"])), terrain
            )
        elif "X" in obj and "Z" in obj:
            position = Point(float(obj["X"]), float(obj["Z"]), terrain)
        else:
            return
        radius = float(obj.get("RadiusNm", 450)) * 1852
        self.centers.append(
            Center(
                str(obj.get("Name", "Center")),
                freqs[0],
                position,
                radius,
                str(obj.get("Side", "")).lower(),
            )
        )

    # Lookups -------------------------------------------------------------------

    def entry_for(self, airport: Airport) -> Optional[AirfieldEntry]:
        for entry in self.airfields:
            if entry.airport_id is not None and entry.airport_id == airport.id:
                return entry
        key = _norm(airport.name)
        exact = [e for e in self.airfields if _norm(e.name) == key]
        if exact:
            return exact[0]
        partial = [e for e in self.airfields if _norm(e.name) and _norm(e.name) in key]
        return partial[0] if len(partial) == 1 else None

    def airport_named(self, name: str) -> Optional[Airport]:
        if self.terrain is None:
            return None
        airports: dict[str, Airport] = self.terrain.airports
        return airports.get(name)

    def airfield_stations(
        self, airport: Optional[Airport], map_freq: Optional[RadioFrequency]
    ) -> list[Station]:
        """ATIS and controllers for one airfield, controllers sharing a
        frequency on one channel ("Ground/Tower/Approach")."""
        if airport is None:
            return []
        from game.atcdata import AtcData

        entry = self.entry_for(airport)
        own = entry.values if entry is not None else {}
        name = airport.name
        map_options: list[RadioFrequency] = []
        if map_freq is not None:
            map_options.append(map_freq)
        if (atc := AtcData.from_pydcs(airport)) is not None:
            for f in (atc.uhf, atc.vhf_am):
                if f not in map_options:
                    map_options.append(f)

        def station(label: str, options: list[RadioFrequency]) -> Optional[Station]:
            first = _preferred(options)
            if first is None:
                return None
            rest = tuple(f for f in options if f != first)
            return Station(label, first, rest)

        stations: list[Station] = []
        atis_options = own.get("atis", [])
        if not atis_options and (by_name := self.atis_by_airfield.get(_norm(name))):
            atis_options = [by_name]
        if not atis_options:
            atis_options = self.defaults.get("atis", [])
        if (atis := station(f"{name} ATIS", atis_options)) is not None:
            stations.append(atis)
        by_key: dict[tuple[RadioFrequency, ...], list[str]] = {}
        for role in CONTROLLERS:
            options = own.get(role.lower(), [])
            if not options:
                if role == "Departure":
                    continue  # Approach handles departures unless set
                options = map_options or self.defaults.get(role.lower(), [])
            if not options:
                continue
            first = _preferred(options)
            assert first is not None
            key = (first,) + tuple(f for f in options if f != first)
            by_key.setdefault(key, []).append(role)
        for key, roles in by_key.items():
            stations.append(Station(f"{name} {'/'.join(roles)}", key[0], key[1:]))
        return stations

    def center_at(self, position: Point, side: str = "blue") -> Optional[Center]:
        best: Optional[tuple[float, Center]] = None
        for center in self.centers:
            if center.side and center.side != side:
                continue
            d = position.distance_to_point(center.position)
            if d <= center.radius_m and (best is None or d < best[0]):
                best = (d, center)
        return best[1] if best else None

    def centers_along(
        self, points: Iterable[Point], side: str = "blue"
    ) -> list[Center]:
        found: list[Center] = []
        for p in points:
            c = self.center_at(p, side)
            if c is not None and all(c.freq != f.freq for f in found):
                found.append(c)
        return found

    def all_frequencies(self, terrain: Terrain) -> set[RadioFrequency]:
        """Everything DCS-ATC (and SkyEye) listens on, so nothing else gets it."""
        out: set[RadioFrequency] = {SKYEYE_AWACS}
        for key, values in BUILT_IN_DEFAULTS.items():
            out.update(self.defaults.get(key, values))
        for values in self.defaults.values():
            out.update(values)
        out.update(self.atis_by_airfield.values())
        for entry in self.airfields:
            for values in entry.values.values():
                out.update(values)
        out.update(c.freq for c in self.centers)
        return out
