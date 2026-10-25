"""Reading DCS-ATC's frequency files for matching radio presets."""

from pathlib import Path

from dcs.terrain import Caucasus, Nevada

from game.radio.dcsatc import DcsAtcFrequencies, SKYEYE_AWACS, strip_jsonc
from game.radio.radios import MHz, kHz

CONFIG = """{
  // Lotsen-Frequenzen (MHz)
  "Frequencies": { "ATIS": 263.5, "Ground": 264.5, "Tower": 265.0, "Approach": 266.5,
                   "Range": 267.5, "AWACS": 251.5 },
  "AtisFreqs": { "Kutaisi": 262.0 },  // own ATIS
}"""

FREQS = """{
  // header comment, "with quotes" // and slashes
  // ===== Caucasus =====
  "CenterSectors": [
    { "Name": "Tbilisi Control", "Freq": 285.25, "Lat": 42.2, "Lon": 42.6, "RadiusNm": 450 },
  ],
  // "Senaki-Kolkhi": { "ATIS": 263.5, "Ground": [261.0, 132.0] },   // id 23
  // ===== Nevada =====
  "CenterSectors": [
    { "Name": "Nellis Control", "Freq": 360.65, "Lat": 36.2, "Lon": -115.0, "RadiusNm": 100, "Side": "red" },
  ],
  "Nellis": { "ATIS": 270.1, "Ground": [275.8, 121.8], "Tower": [327, 132.55], "Approach": [273.55, 124.95] },   // id 4
}"""


def write(tmp_path: Path) -> Path:
    (tmp_path / "config.jsonc").write_text(CONFIG, "utf-8")
    (tmp_path / "frequencies.jsonc").write_text(FREQS, "utf-8")
    return tmp_path


def test_strip_jsonc_keeps_slashes_in_strings() -> None:
    assert (
        strip_jsonc('{"a": "x//y", // c\n "b": [1,],}') == '{"a": "x//y", \n "b": [1]}'
    )


def test_no_files_means_nothing_changes(tmp_path: Path) -> None:
    assert DcsAtcFrequencies.load(tmp_path, Caucasus()) is None


def test_map_sections_only_apply_on_their_map(tmp_path: Path) -> None:
    caucasus = DcsAtcFrequencies.load(write(tmp_path), Caucasus())
    assert caucasus is not None
    assert [c.name for c in caucasus.centers] == ["Tbilisi Control"]
    assert caucasus.airfields == []  # Senaki line is commented out
    nevada = DcsAtcFrequencies.load(tmp_path, Nevada())
    assert nevada is not None
    assert [e.name for e in nevada.airfields] == ["Nellis"]


def test_shared_map_frequency_is_one_station(tmp_path: Path) -> None:
    data = DcsAtcFrequencies.load(write(tmp_path), Caucasus())
    assert data is not None
    kutaisi = data.airport_named("Kutaisi")
    stations = data.airfield_stations(kutaisi, MHz(263))
    assert [(s.label, s.freq) for s in stations] == [
        ("Kutaisi ATIS", MHz(262)),
        ("Kutaisi Ground/Tower/Approach", MHz(263)),
    ]
    assert data.center_at(kutaisi.position).name == "Tbilisi Control"  # type: ignore


def test_own_frequencies_get_their_own_channels(tmp_path: Path) -> None:
    data = DcsAtcFrequencies.load(write(tmp_path), Nevada())
    assert data is not None
    nellis = data.airport_named("Nellis")
    stations = data.airfield_stations(nellis, MHz(327))
    assert [(s.label, s.freq) for s in stations] == [
        ("Nellis ATIS", kHz(270100)),
        ("Nellis Ground", kHz(275800)),
        ("Nellis Tower", MHz(327)),
        ("Nellis Approach", kHz(273550)),
    ]
    # A red-only Center doesn't count for blue.
    assert data.center_at(nellis.position, "blue") is None  # type: ignore
    assert data.center_at(nellis.position, "red") is not None  # type: ignore


def test_everything_dcs_atc_listens_on_is_reserved(tmp_path: Path) -> None:
    data = DcsAtcFrequencies.load(write(tmp_path), Nevada())
    assert data is not None
    taken = data.all_frequencies(Nevada())
    for f in (SKYEYE_AWACS, MHz(265), kHz(263500), kHz(275800), kHz(360650)):
        assert f in taken


def test_preset_uses_a_frequency_the_radio_can_tune() -> None:
    from game.radio.dcsatc import Station
    from game.radio.radios import get_radio

    uhf = get_radio("AN/ARC-164")
    vhf_only = Station("Akrotiri ATIS", MHz(125))
    both = Station("Nellis Tower", MHz(327), (kHz(132550),))
    vhf_first = Station("Kola Tower", MHz(125), (MHz(250),))
    assert vhf_only.tunable_on(uhf) is None
    assert both.tunable_on(uhf) == MHz(327)
    assert vhf_first.tunable_on(uhf) == MHz(250)
    assert vhf_only.tunable_on(None) == MHz(125)  # radio unknown: as before


def test_red_crown_ships_and_built_in_defaults(tmp_path: Path) -> None:
    from game.radio.dcsatc import is_red_crown_type

    assert is_red_crown_type("USS_Arleigh_Burke_IIa")
    assert is_red_crown_type("TICONDEROG")
    assert is_red_crown_type("PERRY")
    assert not is_red_crown_type("CVN_71")
    data = DcsAtcFrequencies.load(write(tmp_path), Caucasus())
    assert data is not None
    taken = data.all_frequencies(Caucasus())
    # Carrier 127.5 and Red Crown 252.5 aren't in config.jsonc but DCS-ATC uses them.
    assert kHz(127500) in taken and kHz(252500) in taken
