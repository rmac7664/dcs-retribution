"""Runs dcs_retribution_warehouses.lua against a mock of the DCS scripting API."""

from pathlib import Path
from typing import Any

import pytest

lupa = pytest.importorskip("lupa.lua51")

SCRIPT = Path("resources/plugins/base/dcs_retribution_warehouses.lua")

MOCK_DCS = """
dirty_state = false
local now = 0
local scheduled = {}
timer = {
    getTime = function() return now end,
    scheduleFunction = function(f, arg, t) scheduled[#scheduled + 1] = {f = f, arg = arg} end,
}
function run_scheduled()
    for _, s in ipairs(scheduled) do s.f(s.arg, now) end
    scheduled = {}
end
env = { info = function() end }
world = {
    event = {
        S_EVENT_TAKEOFF = 3, S_EVENT_LAND = 4, S_EVENT_CRASH = 5, S_EVENT_EJECTION = 6,
        S_EVENT_DEAD = 8, S_EVENT_PILOT_DEAD = 9, S_EVENT_BIRTH = 15,
        S_EVENT_PLAYER_LEAVE_UNIT = 21, S_EVENT_UNIT_LOST = 30,
    },
    handlers = {},
    addEventHandler = function(h) world.handlers[#world.handlers + 1] = h end,
}
function fire(id, unit, place)
    for _, h in ipairs(world.handlers) do h:onEvent({id = id, initiator = unit, place = place}) end
end
Object = { Category = { UNIT = 1 }, getCategory = function(o) return o._cat end }
Unit = { Category = { AIRPLANE = 0, HELICOPTER = 1 } }
local units = {}
Unit.getByName = function(name) return units[name] end

local UnitMT = {}
UnitMT.__index = UnitMT
function UnitMT:getName() return self._name end
function UnitMT:getDesc() return { category = 0, fuelMassMax = self._fuelMax } end
function UnitMT:getAmmo()
    local out = {}
    for name, count in pairs(self._ammo) do
        out[#out + 1] = { count = count, desc = { typeName = name } }
    end
    return out
end
function UnitMT:getFuel() return self._fuel / self._fuelMax end
function UnitMT:inAir() return self._air end
function UnitMT:isExist() return self._exists end
function make_unit(name, ammo, fuel, air)
    local u = setmetatable({
        _name = name, _ammo = ammo, _fuel = fuel, _fuelMax = 5000, _air = air,
        _exists = true, _cat = 1,
    }, UnitMT)
    units[name] = u
    return u
end

local warehouses = {}
local WarehouseMT = {}
WarehouseMT.__index = WarehouseMT
function WarehouseMT:getLiquidAmount(t) return self.fuel end
function WarehouseMT:getItemCount(n) return self.items[n] or 0 end
function WarehouseMT:setItem(n, c) self.items[n] = c end
Warehouse = {
    getResourceMap = function()
        return {
            ["weapons.missiles.AIM_120C"] = {4, 4, 7, 106},
            ["weapons.missiles.AIM_9"] = {4, 4, 7, 7},
            ["weapons.shells.M61_20_HE"] = {0, 0, 0, 0},
            ["F-16C_50"] = {1, 1, 6, 274},
        }
    end,
}
local airbases = {}
function make_airbase(name, items)
    local wh = setmetatable({ fuel = 1500000, items = items or {} }, WarehouseMT)
    local ab = { _name = name, _wh = wh }
    function ab:getName() return self._name end
    function ab:getWarehouse() return self._wh end
    airbases[name] = ab
    return ab
end
Airbase = { getByName = function(name) return airbases[name] end }
"""

DATA = """
dcsRetributionWarehouses = {
    version = 1,
    prefixes = {"weapons.missiles.", "weapons.torpedoes.", "weapons.bombs."},
    bases = {
        ["Batumi"] = { id = "cpA", fuel_kg = 1500000, calibrate = false,
                       mun = { ["weapons.missiles.AIM_120C"] = 40 } },
        ["CVN-71"] = { id = "cpB", fuel_kg = 2500000, calibrate = true, mun = {} },
    },
    units = {
        ["Viper 1"] = { home = "cpA", key = "k1" },
        ["Viper 2"] = { home = "cpA", key = "k1" },
        ["Parked 1"] = { home = "cpA" },
        ["Tomcat 1"] = { home = "cpB" },
    },
    learn = { k1 = true },
    cargo = { ["Herc 1"] = "cpB", ["Herc 2"] = "cpB" },
    ws = { ["weapons.missiles.AIM_120C"] = {4, 4, 7, 106} },
    wantResourceMap = true,
}
"""


def to_py(value: Any) -> Any:
    if lupa.lua_type(value) == "table":
        return {k: to_py(v) for k, v in value.items()}
    return value


@pytest.fixture
def lua() -> Any:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(DATA)
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    return runtime


def test_ledger_tracks_spawn_rearm_landing_loss_and_mission_end(lua: Any) -> None:
    lua.execute("""
        local A = make_airbase("Batumi", { ["weapons.missiles.AIM_120C"] = 40 })
        local B = make_airbase("CVN-71")
        local ev = world.event
        local ammo = function(amraam, nine)
            return { ["weapons.missiles.AIM_120C"] = amraam,
                     ["weapons.missiles.AIM_9"] = nine,
                     ["weapons.shells.M61_20_HE"] = 510 }
        end

        -- Viper 1 spawns cold at Batumi, flies, fires two AMRAAMs, lands on the carrier,
        -- rearms there and takes off again. Still airborne at mission end.
        local v1 = make_unit("Viper 1", ammo(4, 2), 4000, false)
        fire(ev.S_EVENT_BIRTH, v1, A)
        v1._fuel = 3900
        fire(ev.S_EVENT_TAKEOFF, v1, A)
        v1._air = true
        v1._ammo = ammo(2, 2); v1._fuel = 1500
        fire(ev.S_EVENT_LAND, v1, B)
        v1._air = false
        v1._ammo = ammo(4, 2); v1._fuel = 5000
        fire(ev.S_EVENT_TAKEOFF, v1, B)
        v1._air = true
        v1._fuel = 4200

        -- Viper 2 spawns at Batumi and is shot down: its weapons never come back.
        local v2 = make_unit("Viper 2", ammo(4, 2), 4000, false)
        fire(ev.S_EVENT_BIRTH, v2, A)
        fire(ev.S_EVENT_TAKEOFF, v2, A)
        v2._air = true
        fire(ev.S_EVENT_CRASH, v2, nil)
        v2._exists = false

        -- A parked aircraft that never flies gives everything back.
        local p1 = make_unit("Parked 1", ammo(2, 0), 2500, false)
        fire(ev.S_EVENT_BIRTH, p1, A)

        -- An air-started Tomcat charges its home carrier, and is credited at the end.
        local t1 = make_unit("Tomcat 1", { ["weapons.missiles.AIM_54C_Mk47"] = 4 }, 3000, true)
        fire(ev.S_EVENT_BIRTH, t1, nil)
        t1._ammo = { ["weapons.missiles.AIM_54C_Mk47"] = 2 }; t1._fuel = 1000

        run_scheduled()
        result = retributionWarehouses.export(true)
        """)
    result = to_py(lua.globals().result)
    a = result["bases"]["cpA"]
    b = result["bases"]["cpB"]

    # Batumi: Viper 1 took 4+2 and 4000 kg and, airborne at the end, is credited what
    # it still carries (4+2, 4200 kg) back to its home. Viper 2 took 4+2 and 4000 kg and
    # was lost. The parked jet took 2 and 2500 kg and gave them back.
    assert a["mun"]["weapons.missiles.AIM_120C"] == -4 + 4 - 4 - 2 + 2
    assert a["mun"]["weapons.missiles.AIM_9"] == -2 + 2 - 2
    assert a["fuel"] == pytest.approx(-4000 + 4200 - 4000)
    # Shells are never metered.
    assert "weapons.shells.M61_20_HE" not in a["mun"]

    # Carrier: Viper 1 landed with 2+2 and 1500 kg (returned to the carrier's stock),
    # then drew 4+2 and 5000 kg to take off again. The air-started Tomcat drew 4
    # Phoenix and 3000 kg from its home carrier and returned 2 and 1000 kg at the end.
    assert b["mun"]["weapons.missiles.AIM_120C"] == 2 - 4
    assert b["mun"]["weapons.missiles.AIM_9"] == 2 - 2
    assert b["mun"]["weapons.missiles.AIM_54C_Mk47"] == -4 + 2
    assert b["fuel"] == pytest.approx(1500 - 5000 - 3000 + 1000)


def test_airborne_aircraft_are_credited_to_their_home_base(lua: Any) -> None:
    lua.execute("""
        local A = make_airbase("Batumi")
        make_airbase("CVN-71")
        local v1 = make_unit("Viper 1", { ["weapons.missiles.AIM_120C"] = 4 }, 4000, false)
        fire(world.event.S_EVENT_BIRTH, v1, A)
        fire(world.event.S_EVENT_TAKEOFF, v1, A)
        v1._air = true
        v1._ammo = { ["weapons.missiles.AIM_120C"] = 1 }
        v1._fuel = 1000
        result = retributionWarehouses.export(true)
        """)
    a = to_py(lua.globals().result)["bases"]["cpA"]
    assert a["mun"]["weapons.missiles.AIM_120C"] == -3
    assert a["fuel"] == pytest.approx(-3000)


def test_learns_spawn_loadouts_and_reports_resource_map(lua: Any) -> None:
    lua.execute("""
        local A = make_airbase("Batumi")
        make_airbase("CVN-71")
        local v1 = make_unit("Viper 1", {
            ["weapons.missiles.AIM_120C"] = 4,
            ["weapons.nurs.HYDRA_70_M151"] = 19,
            ["weapons.shells.M61_20_HE"] = 510,
        }, 4000, false)
        fire(world.event.S_EVENT_BIRTH, v1, A)
        partial = retributionWarehouses.export(false)
        result = retributionWarehouses.export(true)
        """)
    partial = to_py(lua.globals().partial)
    result = to_py(lua.globals().result)
    # Rockets are learned (they're ammo) even though they aren't metered; shells aren't.
    assert result["learned"]["k1"] == {
        "weapons.missiles.AIM_120C": 4,
        "weapons.nurs.HYDRA_70_M151": 19,
    }
    assert "weapons.nurs.HYDRA_70_M151" not in result["bases"]["cpA"]["mun"]
    # The resource map is only sent at mission end, and only weapons with a wsType.
    assert "resource_map" not in partial
    assert result["resource_map"] == {
        "weapons.missiles.AIM_120C": {1: 4, 2: 4, 3: 7, 4: 106},
        "weapons.missiles.AIM_9": {1: 4, 2: 4, 3: 7, 4: 7},
    }


def test_stale_wstypes_are_corrected_by_name(lua: Any) -> None:
    lua.execute("""
        Warehouse.getResourceMap = function()
            return { ["weapons.missiles.AIM_120C"] = {4, 4, 7, 999} }
        end
        local A = make_airbase("Batumi", { ["weapons.missiles.AIM_120C"] = 0 })
        make_airbase("CVN-71")
        run_scheduled()
        count = A:getWarehouse():getItemCount("weapons.missiles.AIM_120C")
        result = retributionWarehouses.export(true)
        """)
    assert lua.globals().count == 40
    assert to_py(lua.globals().result)["stale"] is True


def test_script_is_inert_without_data() -> None:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    assert runtime.globals().retributionWarehouses is None


def test_supply_airlifters_are_delivered_only_when_landing_at_destination(
    lua: Any,
) -> None:
    lua.execute("""
        local A = make_airbase("Batumi")
        local B = make_airbase("CVN-71")
        local h1 = make_unit("Herc 1", {}, 9000, true)
        local h2 = make_unit("Herc 2", {}, 9000, true)
        fire(world.event.S_EVENT_LAND, h1, B)  -- reached its destination
        fire(world.event.S_EVENT_LAND, h2, A)  -- diverted
        result = retributionWarehouses.export(true)
        """)
    assert to_py(lua.globals().result)["delivered"] == {"Herc 1": True}


REPLENISHMENT_DATA = """
dcsRetributionWarehouses.replenishment = {
    ["CVN-71 replenishment 1"] = {
        carrier = "CVN-71", cp = "cpB",
        mun = { ["weapons.missiles.AIM_120C"] = 12 }, fuel = 40000,
    },
}
"""

SHIP_MOCKS = """
local probe = make_unit("probe", {}, 0, false)
local U = getmetatable(probe).__index
function U:getPoint() return self._point end
tasks = {}
function U:getGroup()
    local unit = self
    return { getController = function()
        return { setTask = function(_, task) tasks[unit._name] = task end }
    end }
end
local W = getmetatable(make_airbase("probe"):getWarehouse()).__index
function W:addItem(n, c) self.items[n] = (self.items[n] or 0) + c end
function W:addLiquid(t, amount) self.fuel = self.fuel + amount end
"""


@pytest.fixture
def lua_with_ship() -> Any:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(SHIP_MOCKS)
    runtime.execute(DATA)
    runtime.execute(REPLENISHMENT_DATA)
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    runtime.execute("""
        carrier_base = make_airbase("CVN-71")
        carrier = make_unit("CVN-71", {}, 0, false)
        carrier._point = { x = 0, y = 0, z = 0 }
        ship = make_unit("CVN-71 replenishment 1", {}, 0, false)
    """)
    return runtime


def test_replenishment_ship_is_steered_at_the_carrier_until_alongside(
    lua_with_ship: Any,
) -> None:
    lua_with_ship.execute("""
        ship._point = { x = 30000, y = 0, z = 0 }
        run_scheduled()
        result = retributionWarehouses.export(false)
    """)
    g = lua_with_ship.globals()
    task = to_py(g.tasks)["CVN-71 replenishment 1"]
    # Heading for where the carrier is now.
    assert task["params"]["route"]["points"][2]["x"] == 0
    assert to_py(g.result)["replenished"] == {}
    assert to_py(g.carrier_base._wh["items"]) == {}


def test_replenishment_ship_unloads_into_the_carrier_when_alongside(
    lua_with_ship: Any,
) -> None:
    lua_with_ship.execute("""
        ship._point = { x = 3000, y = 0, z = 0 }
        run_scheduled()
        result = retributionWarehouses.export(false)
    """)
    g = lua_with_ship.globals()
    result = to_py(g.result)
    assert result["replenished"] == {"CVN-71 replenishment 1": True}
    # Usable in DCS straight away...
    assert to_py(g.carrier_base._wh["items"]) == {"weapons.missiles.AIM_120C": 12}
    assert g.carrier_base._wh.fuel == 1500000 + 40000
    # ...and booked to the carrier's ledger for Retribution.
    assert result["bases"]["cpB"]["mun"] == {"weapons.missiles.AIM_120C": 12}
    assert result["bases"]["cpB"]["fuel"] == 40000
    assert g.dirty_state is True


def test_sunk_replenishment_ship_unloads_nothing(lua_with_ship: Any) -> None:
    lua_with_ship.execute("""
        ship._point = { x = 3000, y = 0, z = 0 }
        ship._exists = false
        run_scheduled()
        result = retributionWarehouses.export(false)
    """)
    g = lua_with_ship.globals()
    assert to_py(g.result)["replenished"] == {}
    assert to_py(g.carrier_base._wh["items"]) == {}
