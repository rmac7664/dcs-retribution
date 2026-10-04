# Base logistics (limited DCS warehouses)

Enabled per campaign with **Settings → Campaign Management → Logistics → Limited fuel and
munitions at bases**. Airfields and carriers/LHAs owned by a side get a finite stock of
aviation fuel and of the munition categories you choose to limit (missiles and bombs by
default; rockets optional; pods, tanks and gun ammunition never).

## How a turn works

1. **Mission generation** (`game/warehouse/plan.py`). Loadouts are fitted to what's on the
   shelf at each departure base, flight by flight. A pylon the base can't fill falls back
   along the same weapon family chain date restrictions use (AIM-120C → AIM-120B → AIM-7M
   …) or is left empty, and you get a "Logistics: <base>" message. Spare (untasked)
   airframes are parked clean so they don't tie up stock. The stock is then written into
   each base's DCS warehouse (`unlimitedFuel/unlimitedMunitions = false`), so the rearm
   and refuel menus in DCS show what the base really has.
2. **In DCS** (`resources/plugins/base/dcs_retribution_warehouses.lua`). The mission script
   keeps its own ledger from unit events: spawning on the ground draws the aircraft's
   load from that base, a rearm/refuel is drawn at takeoff, landing at a managed base
   returns what's still aboard, losses return nothing, and anything still alive at the
   end is returned to where it is (or its home base if airborne). The ledger goes into
   `state.json` as `warehouse_logistics`.
3. **Debrief** (`WarehouseState.apply_mission_results`). The ledger is applied to stock
   (only if the mission ended cleanly). DCS' resource map (item name → wsType) is cached,
   and launchers the mission reported on are learned.
4. **Turn end** (`WarehouseState.resupply`). Fuel is topped up by a percentage of capacity
   and each munition by a percentage of its authorized level (N sorties of the biggest
   load each squadron's aircraft can carry).

## Why the first mission "calibrates"

A limited DCS warehouse lists items by wsType, and DCS renumbers wsTypes between versions,
so they can't be shipped with Retribution. The mission script reports the live map at the
end of every mission. Until Retribution has one (the first mission after enabling the
option), and whenever a planned loadout uses a launcher whose contents DCS hasn't
confirmed (mostly mod weapons), the affected base keeps unlimited munitions *in DCS* for
that mission. Fuel is limited from the first mission and the ledger always meters
munitions, so the campaign's stock stays correct either way.

## Launcher → munition data

`launcher_munitions.json` maps DCS launcher CLSIDs (what a pylon holds) to warehouse items
and counts, e.g. `LAU-115_2*LAU-127_AIM-120C → {weapons.missiles.AIM_120C: 2}`. It is
generated from the [DCS Lua datamine](https://github.com/Quaggles/dcs-lua-datamine):

    python resources/tools/generate_warehouse_munitions.py <path to dcs-lua-datamine>

Re-run it after DCS updates that add weapons. Launchers it can't resolve (mods) are
guessed from their Retribution weapon group and verified against what DCS reports at
spawn; see `game/warehouse/munitions.py`.

## Checking it in DCS

`state.json` also carries, per base, a warehouse snapshot taken 5 s after mission start and
at mission end (`warehouse_logistics.diag`). Comparing the start snapshot with the stock
Retribution wrote shows whether DCS itself deducts the loadouts of aircraft placed in the
mission, and confirms the jet fuel unit (the mission editor stores tons).
