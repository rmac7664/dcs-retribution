"""Logistics presets: Light, Realistic and Hardcore.

Each preset sets the logistics options to a sensible combination. Settings not in a
preset are left alone, and any option can still be changed afterwards.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from game.settings import Settings

PRESETS: dict[str, dict[str, Any]] = {
    # Fuel and munitions are limited, but every base restocks where it stands: no
    # supply runs to protect, no costs, no SAM limits.
    "Light": {
        "logistics_enabled": True,
        "logistics_supply_lines": False,
        "logistics_fuel_by_supply_lines": False,
        "logistics_main_base_only": False,
        "logistics_munitions_cost": False,
        "logistics_limited_sam_missiles": False,
        "logistics_meter_missiles": True,
        "logistics_meter_bombs": True,
        "logistics_meter_rockets": False,
        "logistics_munitions_resupply_percent": 50,
        "logistics_fuel_resupply_percent": 50,
        "logistics_munitions_sorties": 4,
        "logistics_base_throughput_tons": 0,
        "logistics_carrier_airlift": True,
    },
    # Supply runs, depots, costs and SAM limits; fuel moves by supply run too.
    "Realistic": {
        "logistics_enabled": True,
        "logistics_supply_lines": True,
        "logistics_fuel_by_supply_lines": True,
        "logistics_main_base_only": False,
        "logistics_munitions_cost": True,
        "logistics_munitions_budget_percent": 30,
        "logistics_munition_price_percent": 100,
        "logistics_limited_sam_missiles": True,
        "logistics_meter_missiles": True,
        "logistics_meter_bombs": True,
        "logistics_meter_rockets": False,
        "logistics_munitions_resupply_percent": 25,
        "logistics_fuel_resupply_percent": 35,
        "logistics_munitions_sorties": 4,
        "logistics_base_throughput_tons": 60,
        "logistics_carrier_airlift": True,
    },
    # Everything comes from the main base, slower and dearer, and rockets count.
    "Hardcore": {
        "logistics_enabled": True,
        "logistics_supply_lines": True,
        "logistics_fuel_by_supply_lines": True,
        "logistics_main_base_only": True,
        "logistics_munitions_cost": True,
        "logistics_munitions_budget_percent": 20,
        "logistics_munition_price_percent": 125,
        "logistics_limited_sam_missiles": True,
        "logistics_meter_missiles": True,
        "logistics_meter_bombs": True,
        "logistics_meter_rockets": True,
        "logistics_munitions_resupply_percent": 15,
        "logistics_fuel_resupply_percent": 25,
        "logistics_munitions_sorties": 3,
        "logistics_base_throughput_tons": 40,
        "logistics_carrier_airlift": True,
    },
}

DESCRIPTIONS = {
    "Light": (
        "Fuel and munitions are limited, but every base restocks where it stands. "
        "No supply runs, no costs, no SAM missile limits."
    ),
    "Realistic": (
        "Depots buy munitions and send them forward by convoy, ship and airlift; "
        "fuel too. Munitions cost money, SAM sites have limited missiles."
    ),
    "Hardcore": (
        "Everything comes from your main supply base, restocks slower and costs "
        "more, and rockets count. Protect your supply lines."
    ),
}


def apply_preset(settings: Settings, name: str) -> list[str]:
    """Applies a preset; returns the options it changed."""
    changed = []
    for option, value in PRESETS[name].items():
        if not hasattr(settings, option):
            continue
        if getattr(settings, option) != value:
            setattr(settings, option, value)
            changed.append(option)
    return changed


def matching_preset(settings: Settings) -> str:
    """The preset the current settings match, or "Custom"."""
    for name, values in PRESETS.items():
        if all(getattr(settings, k, v) == v for k, v in values.items()):
            return name
    return "Custom"
