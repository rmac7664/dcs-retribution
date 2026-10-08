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


def test_replenishment_ship_keeps_its_route_until_close(lua_with_ship: Any) -> None:
    lua_with_ship.execute("""
        ship._point = { x = 30000, y = 0, z = 0 }  -- 16 nm out
        run_scheduled()
    """)
    assert to_py(lua_with_ship.globals().tasks) == {}


def test_replenishment_ship_chases_the_carrier_on_final_approach(
    lua_with_ship: Any,
) -> None:
    lua_with_ship.execute("""
        carrier._point = { x = 1000, y = 0, z = 2000 }
        ship._point = { x = 1000, y = 0, z = 15000 }  -- 7 nm off
        run_scheduled()
        result = retributionWarehouses.export(false)
    """)
    g = lua_with_ship.globals()
    route = to_py(g.tasks)["CVN-71 replenishment 1"]["params"]["route"]["points"]
    # Mission routes take DCS x (north) and y (= world z, east).
    assert (route[2]["x"], route[2]["y"]) == (1000, 2000)
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
    # ...but not booked to the ledger: Retribution credits the cargo itself.
    assert result["bases"] == {}
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


CTLD_MOCK = """
-- Repeating tasks run again on the next run_scheduled(), like DCS' timer.
local queue = {}
timer.scheduleFunction = function(f, arg, t) queue[#queue + 1] = {f = f, arg = arg} end
function run_scheduled()
    local due = queue
    queue = {}
    for _, s in ipairs(due) do
        if s.f(s.arg, 0) then queue[#queue + 1] = s end
    end
end
coalition = { side = { RED = 1, BLUE = 2 } }
messages = {}
trigger = {
    misc = { getZone = function(name)
        if name == "Hip 1crate_spawn" then return { point = { x = 0, y = 0, z = 0 } } end
    end },
    action = { outTextForCoalition = function(side, text) messages[#messages + 1] = text end },
}
land = { getHeight = function() return 0 end }
local statics = {}
local nextId = 100
local CrateMT = {}
CrateMT.__index = CrateMT
function CrateMT:getPoint() return self.p end
function CrateMT:isExist() return not self.gone end
function CrateMT:destroy() self.gone = true end
callbacks = {}
ctld = {
    crateLookupTable = { ["200"] = { weight = 200, desc = "Troop truck", unit = "M818" } },
    spawnedCratesBLUE = {},
    spawnedCratesRED = {},
    getNextUnitId = function() nextId = nextId + 1; return nextId end,
    getCrateObject = function(name) return statics[name] end,
    addCallback = function(f) callbacks[#callbacks + 1] = f end,
}
function ctld.spawnCrateStatic(country, id, point, name, weight, side)
    statics[name] = setmetatable({ p = point }, CrateMT)
    local t = ctld.crateLookupTable[tostring(weight)]
    if side == 1 then ctld.spawnedCratesRED[name] = t else ctld.spawnedCratesBLUE[name] = t end
    return statics[name]
end
function crate_names()
    local out = {}
    for name, _ in pairs(ctld.spawnedCratesBLUE) do out[#out + 1] = name end
    table.sort(out)
    return out
end
function move(name, x, z, y) statics[name].p = { x = x, y = y or 0, z = z } end
"""

CRATE_DATA = """
dcsRetributionWarehouses.crates = {
    ["1500"] = { key = "Hip 1", dest = "cpB", dest_name = "Senaki", x = 50000, z = 0,
                 crates = 2, weight = 1500, zone = "Hip 1crate_spawn", side = "blue",
                 unit = "M818", desc = "Supply pallet for Senaki" },
}
"""


@pytest.fixture
def crate_lua() -> Any:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(CTLD_MOCK)
    runtime.execute(DATA)
    runtime.execute(CRATE_DATA)
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    runtime.execute("run_scheduled()")
    return runtime


def test_supply_crates_spawn_spread_out_and_off_the_ctld_menu(crate_lua: Any) -> None:
    names = list(to_py(crate_lua.eval("crate_names()")).values())
    assert len(names) == 2 and all("Supply pallet for Senaki" in n for n in names)
    points = [
        to_py(crate_lua.eval(f'ctld.getCrateObject("{n}"):getPoint()')) for n in names
    ]
    assert points[0] != points[1]
    # Known to CTLD by weight, so it can be loaded, but not on the request menu.
    assert to_py(crate_lua.eval('ctld.crateLookupTable["1500"]'))["unit"] == "M818"
    assert crate_lua.eval("ctld.spawnableCrates") is None


def test_crates_set_down_at_the_destination_are_delivered(crate_lua: Any) -> None:
    first, second = to_py(crate_lua.eval("crate_names()")).values()
    crate_lua.execute(f"""
        move("{first}", 50500, 300)        -- set down at Senaki
        move("{second}", 50500, 300, 40)   -- still hanging under the helicopter
        run_scheduled()
        """)
    crate_lua.execute("run_scheduled(); run_scheduled()")  # counted only once
    result = to_py(crate_lua.eval("retributionWarehouses.export(false)"))
    assert result["crates_delivered"] == {"Hip 1": 1}
    assert crate_lua.eval(f'ctld.spawnedCratesBLUE["{first}"]') is None
    assert crate_lua.eval(f'ctld.getCrateObject("{first}"):isExist()') is False
    assert crate_lua.eval(f'ctld.spawnedCratesBLUE["{second}"]') is not None
    assert list(to_py(crate_lua.globals().messages).values()) == [
        "Supply crate delivered to Senaki (1 of 2)"
    ]


def test_unpacked_supply_crates_count_and_leave_no_truck(crate_lua: Any) -> None:
    crate_lua.execute("""
        local group = { gone = false }
        function group:isExist() return not self.gone end
        function group:getUnit() return { getPoint = function() return { x = 50100, y = 0, z = 0 } end } end
        function group:destroy() self.gone = true end
        unpacked = group
        local heli = { isExist = function() return true end,
                       getPoint = function() return { x = 50100, y = 0, z = 0 } end }
        for _, cb in ipairs(callbacks) do
            cb({ action = "unpack", unit = heli, spawnedGroup = group,
                 crate = { details = { weight = 1500, unit = "M818" } } })
            -- An ordinary CTLD crate is left alone.
            cb({ action = "unpack", unit = heli, spawnedGroup = { isExist = function() return true end },
                 crate = { details = { weight = 200, unit = "M818" } } })
        end
        """)
    result = to_py(crate_lua.eval("retributionWarehouses.export(false)"))
    assert result["crates_delivered"] == {"Hip 1": 1}
    assert crate_lua.eval("unpacked.gone") is True


SAM_MOCK = """
local queue = {}
timer.scheduleFunction = function(f, arg, t) queue[#queue + 1] = {f = f, arg = arg} end
function run_scheduled()
    local due = queue
    queue = {}
    for _, s in ipairs(due) do
        if s.f(s.arg, 0) then queue[#queue + 1] = s end
    end
end
world.event.S_EVENT_SHOT = 1
AI = { Option = { Ground = { id = { ROE = 0 }, val = { ROE = { WEAPON_HOLD = 4 } } } } }
held = {}
local groups = {}
function make_group(name, members)
    local controller = { setOption = function(self, id, value) held[name] = value end }
    local g = { _name = name, _units = members }
    function g:getName() return self._name end
    function g:getUnits() return self._units end
    function g:getController() return controller end
    for _, u in ipairs(members) do
        u.getGroup = function() return g end
        u.getTypeName = function() return u._type end
    end
    groups[name] = g
    return g
end
Group = { getByName = function(name) return groups[name] end }
function launch(unit, missile)
    local weapon = { _type = missile, destroyed = false }
    function weapon:getTypeName() return self._type end
    function weapon:destroy() self.destroyed = true end
    unit._ammo[missile] = unit._ammo[missile] - 1
    for _, h in ipairs(world.handlers) do
        h:onEvent({ id = world.event.S_EVENT_SHOT, initiator = unit, weapon = weapon })
    end
    return weapon
end
"""

SAM_DATA = """
dcsRetributionWarehouses.sam = {
    sites = {
        ["SNAKE"] = { cp = "cpA", allow = { ["weapons.missiles.SA9M38M1"] = 3 },
                      groups = { "0101 | SNAKE" } },
        ["NEWBIE"] = { cp = "cpA", allow = {}, groups = { "0102 | NEWBIE" } },
    },
    units = { ["L1"] = "SNAKE", ["L2"] = "SNAKE", ["N1"] = "NEWBIE" },
}
"""


@pytest.fixture
def sam_lua() -> Any:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(SAM_MOCK)
    runtime.execute(DATA)
    runtime.execute(SAM_DATA)
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    runtime.execute("""
        l1 = make_unit("L1", { ["weapons.missiles.SA9M38M1"] = 4 }, 0, false)
        l1._type = "SA-11 Buk LN 9A310M1"
        l2 = make_unit("L2", { ["weapons.missiles.SA9M38M1"] = 4 }, 0, false)
        l2._type = "SA-11 Buk LN 9A310M1"
        n1 = make_unit("N1", { ["weapons.missiles.5V55R"] = 4 }, 0, false)
        n1._type = "S-300PS 5P85C ln"
        make_group("0101 | SNAKE", { l1, l2 })
        make_group("0102 | NEWBIE", { n1 })
        run_scheduled()
        """)
    return runtime


def test_sam_launcher_loads_are_learned(sam_lua: Any) -> None:
    result = to_py(sam_lua.eval("retributionWarehouses.export(false)"))
    assert result["sam_loads"] == {
        "SA-11 Buk LN 9A310M1": {"weapons.missiles.SA9M38M1": 4},
        "S-300PS 5P85C ln": {"weapons.missiles.5V55R": 4},
    }


def test_a_site_fires_its_allowance_then_holds_fire(sam_lua: Any) -> None:
    sam_lua.execute("""
        w1 = launch(l1, "weapons.missiles.SA9M38M1")
        w2 = launch(l2, "weapons.missiles.SA9M38M1")
        """)
    assert sam_lua.eval('held["0101 | SNAKE"]') is None
    sam_lua.execute('w3 = launch(l1, "weapons.missiles.SA9M38M1")')
    # Third of three: the site is out and holds fire.
    assert sam_lua.eval('held["0101 | SNAKE"]') == 4
    assert sam_lua.eval("w3.destroyed") is False
    # A fourth launch (an IADS script turned it back on) never happens.
    sam_lua.execute('w4 = launch(l2, "weapons.missiles.SA9M38M1")')
    assert sam_lua.eval("w4.destroyed") is True
    # Unlearned sites fire freely and are charged; nothing else is.
    sam_lua.execute('launch(n1, "weapons.missiles.5V55R")')
    result = to_py(sam_lua.eval("retributionWarehouses.export(false)"))
    assert result["sam_used"] == {
        "cpA": {"weapons.missiles.SA9M38M1": 3, "weapons.missiles.5V55R": 1}
    }
    assert sam_lua.eval('held["0102 | NEWBIE"]') is None


def test_a_site_with_nothing_allowed_holds_fire_from_the_start() -> None:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(SAM_MOCK)
    runtime.execute(DATA)
    runtime.execute(SAM_DATA.replace("= 3 }", "= 0 }"))
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    runtime.execute("""
        l1 = make_unit("L1", { ["weapons.missiles.SA9M38M1"] = 4 }, 0, false)
        l2 = make_unit("L2", { ["weapons.missiles.SA9M38M1"] = 4 }, 0, false)
        make_group("0101 | SNAKE", { l1, l2 })
        run_scheduled()
        """)
    assert runtime.eval('held["0101 | SNAKE"]') == 4


def test_sam_missiles_reported_without_a_prefix_are_learned() -> None:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(SAM_MOCK)
    runtime.execute(DATA)
    runtime.execute(SAM_DATA)
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    runtime.execute("""
        Weapon = { Category = { SHELL = 0, MISSILE = 1 } }
        l1 = make_unit("L1", {}, 0, false)
        l1._type = "SA-11 Buk LN 9A310M1"
        function l1:getAmmo()
            return { { count = 4, desc = { typeName = "SA9M38M1", category = 1 } },
                     { count = 500, desc = { typeName = "23mm_HE", category = 0 } } }
        end
        make_group("0101 | SNAKE", { l1 })
        run_scheduled()
        """)
    result = to_py(runtime.eval("retributionWarehouses.export(false)"))
    assert result["sam_loads"]["SA-11 Buk LN 9A310M1"] == {
        "weapons.missiles.SA9M38M1": 4
    }


def test_ships_out_of_missiles_hold_fire_with_naval_options() -> None:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(SAM_MOCK)
    runtime.execute("""
        AI.Option.Naval = { id = { ROE = 0 }, val = { ROE = { WEAPON_HOLD = 4 } } }
        AI.Option.Ground.val.ROE.WEAPON_HOLD = 99
        Group.Category = { GROUND = 2, SHIP = 3 }
        """)
    runtime.execute(DATA)
    runtime.execute(SAM_DATA.replace("= 3 }", "= 0 }"))
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    runtime.execute("""
        l1 = make_unit("L1", { ["weapons.missiles.SA9M38M1"] = 4 }, 0, false)
        g = make_group("0101 | SNAKE", { l1 })
        function g:getCategory() return Group.Category.SHIP end
        run_scheduled()
        """)
    assert runtime.eval('held["0101 | SNAKE"]') == 4


def test_loads_are_learned_under_retributions_unit_type() -> None:
    runtime = lupa.LuaRuntime(unpack_returned_tuples=True)
    runtime.execute(MOCK_DCS)
    runtime.execute(SAM_MOCK)
    runtime.execute(DATA)
    runtime.execute(SAM_DATA)
    runtime.execute('dcsRetributionWarehouses.sam.types = { ["L1"] = "Stennis" }')
    runtime.execute(SCRIPT.read_text(encoding="utf-8"))
    runtime.execute("""
        l1 = make_unit("L1", { ["weapons.missiles.RIM_116A"] = 42 }, 0, false)
        l1._type = "CVN_71"
        make_group("0101 | SNAKE", { l1 })
        run_scheduled()
        """)
    result = to_py(runtime.eval("retributionWarehouses.export(false)"))
    assert result["sam_loads"] == {"Stennis": {"weapons.missiles.RIM_116A": 42}}
