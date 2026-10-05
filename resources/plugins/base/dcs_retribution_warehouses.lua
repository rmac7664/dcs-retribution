-- DCS Retribution base logistics: meters fuel and munitions drawn from managed bases.
--
-- Retribution writes each managed base's stock into its DCS warehouse and passes the
-- details in the dcsRetributionWarehouses table. This script keeps an independent
-- ledger of what aircraft take from and bring back to each base, using unit events and
-- Unit.getAmmo()/getFuel(), so the accounting does not depend on how DCS itself debits
-- warehouses for aircraft placed in the mission. The ledger is written to state.json by
-- write_state() in dcs_retribution.lua.
--
--   spawn on the ground at a base   -> debit that base with what the aircraft carries
--   take off                        -> debit anything loaded since (rearm/refuel)
--   land at a managed base          -> credit what is still aboard (fuel included)
--   player leaves an aircraft parked-> credit what is still aboard
--   still alive at mission end      -> credit what is aboard to where it is (or home)
--   destroyed                       -> nothing comes back
--
-- It also reports, once per mission, DCS' resource map (warehouse item name -> wsType),
-- which Retribution needs to write limited warehouses, and what aircraft with not yet
-- verified launchers actually carried, so Retribution can learn them.

local data = dcsRetributionWarehouses
if type(data) ~= "table" or type(data.bases) ~= "table" then
    return
end

local W = {
    deltas = {},    -- control point id -> { fuel = kg, mun = { [item] = count } }
    learned = {},   -- loadout key -> { [item] = count }
    diag = {},      -- control point id -> { start = {...}, finish = {...} }
    delivered = {}, -- supply airlift unit name -> true once landed at its destination
    stale = false,
    finalized = false,
}
retributionWarehouses = W

local tracked = {}  -- unit name -> { home, aboard, fuel, ground, done }
local idByAirbase = {}
for dcsName, base in pairs(data.bases) do
    idByAirbase[dcsName] = base.id
end
local units = data.units or {}
local learnKeys = data.learn or {}
local cargo = data.cargo or {}  -- supply airlift unit name -> destination control point id
local prefixes = data.prefixes or {}

local function log(message)
    if logger then
        logger:info("[warehouses] " .. message)
    else
        env.info("[DCSRetribution warehouses] " .. message)
    end
end

local function startsWith(text, prefix)
    return string.sub(text, 1, #prefix) == prefix
end

local function isMetered(name)
    for _, prefix in ipairs(prefixes) do
        if startsWith(name, prefix) then
            return true
        end
    end
    return false
end

local function isAmmo(name)
    return startsWith(name, "weapons.missiles.") or startsWith(name, "weapons.bombs.")
        or startsWith(name, "weapons.nurs.") or startsWith(name, "weapons.torpedoes.")
end

local function ammoOf(unit, filter)
    local result = {}
    local ok, ammo = pcall(unit.getAmmo, unit)
    if ok and type(ammo) == "table" then
        for _, entry in ipairs(ammo) do
            local name = entry.desc and entry.desc.typeName
            if name and filter(name) then
                result[name] = (result[name] or 0) + (entry.count or 0)
            end
        end
    end
    return result
end

local function fuelOf(unit)
    local okFuel, fraction = pcall(unit.getFuel, unit)
    local okDesc, desc = pcall(unit.getDesc, unit)
    if okFuel and okDesc and fraction and desc and desc.fuelMassMax then
        return fraction * desc.fuelMassMax
    end
    return 0
end

local function inAir(unit)
    local ok, airborne = pcall(unit.inAir, unit)
    return ok and airborne
end

local function baseAt(place)
    if place and place.getName then
        local ok, name = pcall(place.getName, place)
        if ok and name then
            return idByAirbase[name]
        end
    end
    return nil
end

local function book(cp, munitions, fuel, sign)
    if not cp then
        return
    end
    local delta = W.deltas[cp]
    if not delta then
        delta = { fuel = 0, mun = {} }
        W.deltas[cp] = delta
    end
    delta.fuel = delta.fuel + sign * (fuel or 0)
    for name, count in pairs(munitions) do
        delta.mun[name] = (delta.mun[name] or 0) + sign * count
    end
    dirty_state = true
end

local function isAircraft(object)
    if not object or not object.getDesc then
        return false
    end
    local okCat, category = pcall(Object.getCategory, object)
    if not okCat or category ~= Object.Category.UNIT then
        return false
    end
    local ok, desc = pcall(object.getDesc, object)
    return ok and desc and (desc.category == Unit.Category.AIRPLANE or desc.category == Unit.Category.HELICOPTER)
end

local function onBirth(event)
    local unit = event.initiator
    local name = unit:getName()
    local info = units[name]
    local airborne = inAir(unit)
    local cp = nil
    if not airborne then
        cp = baseAt(event.place)
    end
    if not cp and info then
        cp = info.home
    end
    if not cp then
        return
    end
    local aboard = ammoOf(unit, isMetered)
    local fuel = fuelOf(unit)
    book(cp, aboard, fuel, -1)
    tracked[name] = {
        home = (info and info.home) or cp,
        aboard = aboard,
        fuel = fuel,
        ground = (not airborne) and cp or nil,
    }
    if info and info.key and learnKeys[info.key] and not W.learned[info.key] then
        W.learned[info.key] = ammoOf(unit, isAmmo)
    end
end

local function onTakeoff(event)
    local unit = event.initiator
    local entry = tracked[unit:getName()]
    if not entry or entry.done then
        return
    end
    local cp = baseAt(event.place) or entry.ground or entry.home
    local now = ammoOf(unit, isMetered)
    local loaded, unloaded = {}, {}
    for item, count in pairs(now) do
        local change = count - (entry.aboard[item] or 0)
        if change > 0 then loaded[item] = change end
    end
    for item, count in pairs(entry.aboard) do
        local change = count - (now[item] or 0)
        if change > 0 then unloaded[item] = change end
    end
    local fuel = fuelOf(unit)
    -- Fuel burned while taxiing isn't returned; only a refuel draws from the base.
    local refuel = math.max(0, fuel - entry.fuel)
    book(cp, loaded, refuel, -1)
    book(cp, unloaded, 0, 1)
    entry.aboard = now
    entry.fuel = fuel
    entry.ground = nil
end

local function onLand(event)
    local unit = event.initiator
    local name = unit:getName()
    local cp = baseAt(event.place)
    if cargo[name] and cp == cargo[name] and not W.delivered[name] then
        W.delivered[name] = true
        dirty_state = true
        log(name .. " delivered its supply cargo")
    end
    local entry = tracked[name]
    if not entry or entry.done then
        return
    end
    local now = ammoOf(unit, isMetered)
    local fuel = fuelOf(unit)
    if cp then
        -- Whatever is aboard goes back into the base's stock; taking off again draws it
        -- back out. This also covers AI that despawn after landing.
        book(cp, now, fuel, 1)
        entry.aboard = {}
        entry.fuel = 0
        entry.ground = cp
    else
        entry.aboard = now
        entry.fuel = fuel
        entry.ground = nil
    end
end

local function onPlayerLeave(event)
    local unit = event.initiator
    local entry = tracked[unit:getName()]
    if not entry or entry.done then
        return
    end
    if not inAir(unit) then
        book(entry.ground or entry.home, entry.aboard, entry.fuel, 1)
    end
    entry.done = true
end

local function onLost(event)
    local unit = event.initiator
    if not unit or not unit.getName then
        return
    end
    local ok, name = pcall(unit.getName, unit)
    local entry = ok and tracked[name]
    if entry then
        entry.done = true
    end
end

local handlers = {
    [world.event.S_EVENT_BIRTH] = onBirth,
    [world.event.S_EVENT_TAKEOFF] = onTakeoff,
    [world.event.S_EVENT_LAND] = onLand,
    [world.event.S_EVENT_PLAYER_LEAVE_UNIT] = onPlayerLeave,
    [world.event.S_EVENT_CRASH] = onLost,
    [world.event.S_EVENT_DEAD] = onLost,
    [world.event.S_EVENT_PILOT_DEAD] = onLost,
    [world.event.S_EVENT_EJECTION] = onLost,
    [world.event.S_EVENT_UNIT_LOST] = onLost,
}

local eventHandler = {}
function eventHandler:onEvent(event)
    local handler = handlers[event.id]
    if not handler or not event.initiator then
        return
    end
    if handler ~= onLost and not isAircraft(event.initiator) then
        return
    end
    local ok, err = pcall(handler, event)
    if not ok then
        log("event " .. tostring(event.id) .. " failed: " .. tostring(err))
    end
end
world.addEventHandler(eventHandler)

local function warehouseFor(dcsName)
    local ok, airbase = pcall(Airbase.getByName, dcsName)
    if not ok or not airbase then
        return nil
    end
    local okW, warehouse = pcall(airbase.getWarehouse, airbase)
    if okW then
        return warehouse
    end
    return nil
end

local function snapshot(dcsName, base)
    local warehouse = warehouseFor(dcsName)
    if not warehouse then
        return { missing = true }
    end
    local result = { items = {} }
    local okFuel, fuel = pcall(warehouse.getLiquidAmount, warehouse, 0)
    result.jet_fuel = okFuel and fuel or nil
    if not base.calibrate then
        for item, _ in pairs(base.mun or {}) do
            local ok, count = pcall(warehouse.getItemCount, warehouse, item)
            if ok then
                result.items[item] = count
            end
        end
    end
    return result
end

local function resourceMap()
    local ok, map = pcall(Warehouse.getResourceMap)
    if not ok or type(map) ~= "table" then
        ok, map = pcall(Warehouse.getResourceMap, Warehouse)
    end
    if not ok or type(map) ~= "table" then
        return nil
    end
    local weapons = {}
    for name, ws in pairs(map) do
        if type(name) == "string" and startsWith(name, "weapons.")
            and not startsWith(name, "weapons.shells.") and type(ws) == "table"
            and (ws[1] or 0) ~= 0 then
            weapons[name] = { ws[1], ws[2], ws[3], ws[4] }
        end
    end
    return weapons
end

local function sameWs(a, b)
    return a and b and a[1] == b[1] and a[2] == b[2] and a[3] == b[3] and a[4] == b[4]
end

local function onMissionStarted()
    -- If DCS renumbered warehouse items since Retribution cached them (a DCS update),
    -- the counts written into the mission landed on the wrong items. Fix them by name.
    if type(data.ws) == "table" then
        local fresh = resourceMap()
        if fresh then
            for item, ws in pairs(data.ws) do
                if not sameWs(ws, fresh[item]) then
                    W.stale = true
                    break
                end
            end
        end
    end
    for dcsName, base in pairs(data.bases) do
        if W.stale and not base.calibrate then
            local warehouse = warehouseFor(dcsName)
            if warehouse then
                for item, count in pairs(base.mun or {}) do
                    pcall(warehouse.setItem, warehouse, item, count)
                end
            end
        end
        W.diag[base.id] = { start = snapshot(dcsName, base) }
    end
    if W.stale then
        log("DCS warehouse item IDs changed since they were cached; corrected stock by name")
    end
    dirty_state = true
    return nil
end
timer.scheduleFunction(onMissionStarted, nil, timer.getTime() + 5)

local function finalize()
    if W.finalized then
        return
    end
    W.finalized = true
    for name, entry in pairs(tracked) do
        if not entry.done then
            local unit = Unit.getByName(name)
            if unit and unit:isExist() then
                local airborne = inAir(unit)
                local cp = ((not airborne) and entry.ground) or entry.home
                book(cp, ammoOf(unit, isMetered), fuelOf(unit), 1)
            else
                -- Despawned without a loss event (e.g. AI removed after RTB).
                book(entry.ground or entry.home, entry.aboard, entry.fuel, 1)
            end
            entry.done = true
        end
    end
    for dcsName, base in pairs(data.bases) do
        local diag = W.diag[base.id] or {}
        diag.finish = snapshot(dcsName, base)
        W.diag[base.id] = diag
    end
end

function W.export(missionEnded)
    local result = {
        version = 1,
        bases = W.deltas,
        learned = W.learned,
        delivered = W.delivered,
        stale = W.stale,
    }
    if missionEnded then
        result.diag = W.diag
        local ok, err = pcall(finalize)
        if not ok then
            log("finalize failed: " .. tostring(err))
        end
        if data.wantResourceMap then
            local okMap, map = pcall(resourceMap)
            if okMap and map then
                result.resource_map = map
            end
        end
    end
    return result
end

log("tracking " .. tostring(#prefixes) .. " metered categories at managed bases")
