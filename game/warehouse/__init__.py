"""Base logistics: finite fuel and munitions, backed by DCS warehouses.

state.py      Campaign-persistent stock per base, resupply, applying mission results.
munitions.py  Which DCS warehouse items each launcher (pylon store) draws.
plan.py       Per-mission fitting of loadouts to stock and writing the warehouses.

The mission side lives in resources/plugins/base/dcs_retribution_warehouses.lua.
"""

from .state import BaseStock, WarehouseState

__all__ = ["BaseStock", "WarehouseState"]
