"""Generates resources/warehouse/launcher_munitions.json.

The DCS warehouse counts individual munitions ("weapons.missiles.AIM_120C"), while
Retribution loadouts are built from launcher CLSIDs ("LAU-115 with 2 x AIM-120C"). This
script builds the bridge between the two from a checkout of the DCS Lua datamine
(https://github.com/Quaggles/dcs-lua-datamine), which mirrors DCS' own launcher and
weapon tables after every DCS update.

    python resources/tools/generate_warehouse_munitions.py <path to dcs-lua-datamine>

Only stock DCS content is covered by the datamine. Mod weapons, and anything this script
can't resolve, are learned at runtime instead: the mission script reports what each
aircraft actually carried at spawn and Retribution fills in the gaps (see
game/warehouse/munitions.py).

Re-run this after DCS updates that add or change weapons.
"""

from __future__ import annotations

import argparse
import collections
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import lupa  # type: ignore[import-untyped]

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "resources" / "warehouse" / "launcher_munitions.json"
MOOSE = ROOT / "resources" / "plugins" / "base" / "Moose.lua"

# Second element of a weapon wsType -> warehouse resource category.
WS_CATEGORY = {4: "missiles", 5: "bombs", 7: "nurs", 8: "torpedoes"}


def norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def to_py(obj: Any) -> Any:
    if lupa.lua_type(obj) == "table":
        keys = list(obj.keys())
        if keys and all(isinstance(k, int) for k in keys):
            if sorted(keys) == list(range(1, len(keys) + 1)):
                return [to_py(obj[k]) for k in sorted(keys)]
        return {k: to_py(v) for k, v in obj.items()}
    return obj


def load_tables(directory: Path, root_key: str) -> Any:
    """Executes every datamine file under a directory into a single Lua table."""
    lua = lupa.LuaRuntime(unpack_returned_tuples=True)
    lua.execute("""
        local mt = {}
        mt.__index = function(t, k)
            local n = setmetatable({}, mt); rawset(t, k, n); return n
        end
        _G["%s"] = setmetatable({}, mt)
        """ % root_key)
    for path in sorted(directory.glob("**/*.lua")):
        source = path.read_text(encoding="utf-8", errors="replace")
        # The datamine writes shared sub-tables as inspect-style references.
        source = re.sub(r"<table \d+>", "{}", source)
        source = re.sub(r"<\d+>", "", source)
        try:
            lua.execute(source)
        except lupa.LuaError as ex:
            print(f"Skipping {path.name}: {ex}", file=sys.stderr)
    return to_py(lua.globals()[root_key])


class Resolver:
    def __init__(self, datamine: Path) -> None:
        g = datamine / "_G"
        launchers = load_tables(g / "launcher", "launcher")
        self.launchers: dict[str, dict[str, Any]] = {
            v["CLSID"]: v
            for v in launchers.values()
            if isinstance(v, dict) and isinstance(v.get("CLSID"), str)
        }

        # Munition definitions (name, model, first three wsType numbers).
        self.munitions: dict[str, dict[str, Any]] = {}
        for sub in ("weapons_table", "bombs", "rockets", "torpedoes"):
            if (g / sub).exists():
                self._collect(load_tables(g / sub, sub))

        # Every resource name we know of. Legacy core weapons (AIM-9M, R-27R...) are
        # not defined in Lua so they only appear in MOOSE's storage enums.
        self.universe: set[str] = set(self.munitions)
        for path in g.glob("**/*.lua"):
            text = path.read_text(encoding="utf-8", errors="replace")
            self.universe.update(
                re.findall(r'_unique_resource_name = "(weapons\.[^"]+)"', text)
            )
        if MOOSE.exists():
            self.universe.update(
                re.findall(
                    r'ENUMS\.Storage\.weapons\.[A-Za-z_]+\.[A-Za-z0-9_]+="(weapons\.[^"]+)"',
                    MOOSE.read_text(encoding="utf-8", errors="replace"),
                )
            )

        self.by_ws3: dict[tuple[int, int, int], list[str]] = collections.defaultdict(
            list
        )
        for name, data in self.munitions.items():
            ws = data.get("ws")
            if isinstance(ws, list) and len(ws) >= 3:
                self.by_ws3[(ws[0], ws[1], ws[2])].append(name)

    def _collect(self, table: Any) -> None:
        if isinstance(table, dict):
            name = table.get("_unique_resource_name")
            if isinstance(name, str):
                self.munitions[name] = {
                    "name": table.get("name"),
                    "model": table.get("model"),
                    "display": table.get("display_name") or table.get("displayName"),
                    "ws": table.get("ws_type") or table.get("wsTypeOfWeapon"),
                }
                return
            for value in table.values():
                self._collect(value)
        elif isinstance(table, list):
            for value in table:
                self._collect(value)

    @staticmethod
    def _elements(launcher: dict[str, Any]) -> list[dict[str, Any]]:
        elements = launcher.get("Elements")
        if not isinstance(elements, list):
            return []
        return [e for e in elements if isinstance(e, dict)]

    def _count(self, launcher: dict[str, Any]) -> int:
        count = launcher.get("Count")
        if isinstance(count, (int, float)) and count > 0:
            return int(count)
        payload = [e for e in self._elements(launcher) if not e.get("IsAdapter")]
        return max(1, len(payload))

    def _by_name(
        self, launcher: dict[str, Any], category: Optional[str]
    ) -> Optional[str]:
        prefix = f"weapons.{category}." if category else "weapons."
        pool = [n for n in self.universe if n.startswith(prefix)]
        shapes = {
            norm(e.get("ShapeName"))
            for e in self._elements(launcher)
            if e.get("ShapeName") and not e.get("IsAdapter")
        }
        exact = [n for n in pool if norm(n.split(".", 2)[2]) in shapes]
        if len(exact) == 1:
            return exact[0]
        display = norm(launcher.get("displayName", ""))
        contained = [
            n
            for n in (exact or pool)
            if len(norm(n.split(".", 2)[2])) >= 3
            and norm(n.split(".", 2)[2]) in display
        ]
        if contained:
            return max(contained, key=lambda n: len(norm(n.split(".", 2)[2])))
        return None

    def _by_ws(self, ws: list[Any], launcher: dict[str, Any]) -> Optional[str]:
        candidates = self.by_ws3.get((ws[0], ws[1], ws[2]), [])
        shapes = {
            norm(e.get("ShapeName"))
            for e in self._elements(launcher)
            if e.get("ShapeName") and not e.get("IsAdapter")
        }
        shaped = [
            c
            for c in candidates
            if norm(self.munitions[c].get("model")) in shapes
            or norm(self.munitions[c].get("name")) in shapes
        ]
        if len(shaped) == 1:
            return shaped[0]
        display = norm(launcher.get("displayName", ""))
        named = [
            c
            for c in (shaped or candidates)
            if self.munitions[c].get("display")
            and norm(self.munitions[c]["display"]) in display
        ]
        if named:
            return max(named, key=lambda c: len(norm(self.munitions[c]["display"])))
        # Legacy weapons have no Lua definition; fall back to matching by name.
        return self._by_name(launcher, WS_CATEGORY.get(ws[1]))

    def resolve(self, clsid: str, depth: int = 0) -> Optional[dict[str, int]]:
        launcher = self.launchers.get(clsid)
        if launcher is None or depth > 4:
            return None
        weapon = launcher.get("wsTypeOfWeapon")
        if isinstance(weapon, str) and weapon.startswith("weapons."):
            return {weapon: self._count(launcher)}
        unique = launcher.get("_unique_resource_name")
        if isinstance(unique, str):
            # Pods, tanks and single stores defined as their own resource.
            return {unique: 1}
        sub_clsids = [
            e["payload_CLSID"]
            for e in self._elements(launcher)
            if e.get("payload_CLSID")
        ]
        if sub_clsids:
            total: collections.Counter[str] = collections.Counter()
            for sub in sub_clsids:
                resolved = self.resolve(sub, depth + 1)
                if resolved is None:
                    return None
                total.update(resolved)
            return dict(total)
        attribute = launcher.get("attribute")
        if isinstance(attribute, str) and attribute.startswith("weapons."):
            return {attribute: self._count(launcher)}
        for ws in (weapon, attribute):
            if isinstance(ws, list) and len(ws) >= 3 and ws[0] == 4:
                match = self._by_ws(ws, launcher)
                if match is not None:
                    return {match: self._count(launcher)}
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("datamine", type=Path, help="dcs-lua-datamine checkout")
    args = parser.parse_args()

    resolver = Resolver(args.datamine)
    resolved: dict[str, dict[str, int]] = {}
    for clsid in sorted(resolver.launchers):
        munitions = resolver.resolve(clsid)
        if munitions:
            resolved[clsid] = dict(sorted(munitions.items()))

    # The datamine tags every commit with the DCS version it was mined from.
    try:
        subject = subprocess.run(
            ["git", "-C", str(args.datamine), "log", "-1", "--format=%s"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        subject = ""
    version = re.search(r"\d+\.\d+\.\d+\.\d+", subject)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", encoding="utf-8", newline="\n") as out:
        json.dump(
            {
                "source": "dcs-lua-datamine",
                "dcs_version": version.group(0) if version else None,
                "launchers": resolved,
            },
            out,
            indent=1,
            sort_keys=False,
        )
        out.write("\n")
    print(
        f"Resolved {len(resolved)} of {len(resolver.launchers)} launchers -> {OUTPUT}"
    )


if __name__ == "__main__":
    main()
