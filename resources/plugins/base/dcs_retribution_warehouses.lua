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
--   replenishment ship alongside    -> stores go into the carrier's DCS warehouse
--                                      (Retribution credits them itself; not booked)
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
    replenished = {}, -- replenishment ship unit name -> true once it unloaded
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

-- Replenishment ships ------------------------------------------------------------------
-- Each ship sails for where its carrier's launch-and-recovery leg ends. Once within
-- APPROACH_METERS of the carrier it is re-steered at the carrier every minute; within
-- ALONGSIDE_METERS its stores go into the carrier's DCS warehouse, usable straight
-- away. They aren't booked to the ledger: Retribution credits the cargo itself when it
-- reads "replenished", whether or not the mission ends cleanly.

local replenishment = data.replenishment or {}
local ALONGSIDE_METERS = 5556 -- 3 nm
local APPROACH_METERS = 18520 -- 10 nm
local SAIL_SPEED = 6.17 -- 12 kt, in m/s

local function aliveUnit(name)
    local ok, unit = pcall(Unit.getByName, name)
    if ok and unit then
        local okExist, exists = pcall(unit.isExist, unit)
        if okExist and exists then
            return unit
        end
    end
    return nil
end

local function steer(shipUnit, target)
    local group = shipUnit:getGroup()
    if not group then
        return
    end
    local from = shipUnit:getPoint()
    local route = {
        points = {
            { x = from.x, y = from.z, type = "Turning Point", action = "Turning Point",
              speed = SAIL_SPEED, speed_locked = true },
            { x = target.x, y = target.z, type = "Turning Point", action = "Turning Point",
              speed = SAIL_SPEED, speed_locked = true },
        },
    }
    local controller = group:getController()
    pcall(controller.setTask, controller, { id = "Mission", params = { route = route } })
end

local function unload(shipName, ship)
    local warehouse = warehouseFor(ship.carrier)
    if warehouse then
        for item, count in pairs(ship.mun or {}) do
            -- Ships' own missiles ("sam.") aren't DCS warehouse items.
            if not startsWith(item, "sam.") then
                pcall(warehouse.addItem, warehouse, item, count)
            end
        end
        if (ship.fuel or 0) > 0 then
            pcall(warehouse.addLiquid, warehouse, 0, ship.fuel)
        end
    end
    W.replenished[shipName] = true
    dirty_state = true
    log(shipName .. " came alongside " .. tostring(ship.carrier) .. " and unloaded")
end

local function checkReplenishment()
    local atSea = false
    for shipName, ship in pairs(replenishment) do
        if not W.replenished[shipName] then
            local shipUnit = aliveUnit(shipName)
            local carrier = aliveUnit(ship.carrier)
            if shipUnit and carrier then
                atSea = true
                local a, b = shipUnit:getPoint(), carrier:getPoint()
                local dx, dz = a.x - b.x, a.z - b.z
                local distance = math.sqrt(dx * dx + dz * dz)
                if distance <= ALONGSIDE_METERS then
                    unload(shipName, ship)
                elseif distance <= APPROACH_METERS then
                    -- Final approach: chase the carrier itself. Further out the ship
                    -- keeps to its route, since the carrier usually outruns it.
                    steer(shipUnit, b)
                end
            end
        end
    end
    if atSea then
        return timer.getTime() + 60
    end
    return nil
end

local function checkReplenishmentSafely()
    local ok, result = pcall(checkReplenishment)
    if not ok then
        log("replenishment check failed: " .. tostring(result))
        return timer.getTime() + 60
    end
    return result
end

if next(replenishment) then
    timer.scheduleFunction(checkReplenishmentSafely, nil, timer.getTime() + 30)
end

-- CTLD supply crates ------------------------------------------------------------------
--
-- A player-flown supply helicopter carries its cargo as CTLD crates. Each run has its
-- own crate weight, which is how CTLD tells crate types apart. The crates are spawned
-- at the run's crate zone and kept off CTLD's request menu, so they cannot be conjured
-- up. A crate set down within DELIVERY_METERS of the destination is delivered: it is
-- counted, removed, and Retribution credits its share of the cargo after the mission.

local crateRuns = data.crates or {} -- crate weight (string) -> run
local DELIVERY_METERS = 3000
local CRATE_SPACING = 12
W.crates_delivered = {} -- run key (lead unit name) -> crates delivered

local function runForWeight(weight)
    if weight == nil then
        return nil
    end
    return crateRuns[tostring(math.floor(weight + 0.5))]
end

local function nearDestination(run, point)
    local dx, dz = point.x - run.x, point.z - run.z
    return dx * dx + dz * dz <= DELIVERY_METERS * DELIVERY_METERS
end

local function countCrate(run)
    local done = W.crates_delivered[run.key] or 0
    if done >= (run.crates or 0) then
        return false -- never more than the run carried
    end
    W.crates_delivered[run.key] = done + 1
    dirty_state = true
    local side = run.side == "red" and coalition.side.RED or coalition.side.BLUE
    trigger.action.outTextForCoalition(side, string.format(
        "Supply crate delivered to %s (%d of %d)",
        tostring(run.dest_name), done + 1, run.crates or 0), 10)
    log(string.format("%s: supply crate %d of %d delivered to %s",
        tostring(run.key), done + 1, run.crates or 0, tostring(run.dest_name)))
    return true
end

local function spawnSupplyCrates()
    if type(ctld) ~= "table" or type(ctld.spawnCrateStatic) ~= "function" then
        log("CTLD is not loaded; supply crates were not spawned")
        return nil
    end
    for weight, run in pairs(crateRuns) do
        ctld.crateLookupTable[weight] = {
            weight = tonumber(weight), desc = run.desc, unit = run.unit,
        }
        local zone = trigger.misc.getZone(run.zone)
        if zone then
            local red = run.side == "red"
            local country = red and 0 or 2
            local sideId = red and 1 or 2
            local perRow = math.max(1, math.ceil(math.sqrt(run.crates or 1)))
            for i = 0, (run.crates or 0) - 1 do
                -- Laid out in a grid: CTLD's own spawner stacks every crate on the
                -- zone centre.
                local x = zone.point.x + (i % perRow) * CRATE_SPACING
                local z = zone.point.z + math.floor(i / perRow) * CRATE_SPACING
                local point = { x = x, y = land.getHeight({ x = x, y = z }), z = z }
                local id = ctld.getNextUnitId()
                local name = string.format("%s #%i", tostring(run.desc), id)
                ctld.spawnCrateStatic(country, id, point, name, tonumber(weight), sideId)
            end
            log(string.format("spawned %d supply crates for %s at %s",
                run.crates or 0, tostring(run.dest_name), tostring(run.zone)))
        else
            log("supply crate zone not found: " .. tostring(run.zone))
        end
    end
    if type(ctld.addCallback) == "function" then
        ctld.addCallback(function(args)
            -- Unpacked instead of just set down: the cargo is supplies, not a vehicle.
            if type(args) ~= "table" or args.action ~= "unpack" then
                return
            end
            local details = args.crate and args.crate.details
            local run = runForWeight(details and details.weight)
            if not run then
                return
            end
            local point = args.unit and args.unit:isExist() and args.unit:getPoint()
            local group = args.spawnedGroup
            if group and group.isExist and group:isExist() then
                local first = group:getUnit(1)
                if first then
                    point = first:getPoint()
                end
                group:destroy()
            end
            if point and nearDestination(run, point) then
                countCrate(run)
            end
        end)
    end
    return nil
end

local function onTheGround(object)
    local point = object:getPoint()
    return point.y - land.getHeight({ x = point.x, y = point.z }) < 2
end

local function checkCrates()
    if type(ctld) ~= "table" then
        return nil
    end
    for _, crates in ipairs({ ctld.spawnedCratesBLUE or {}, ctld.spawnedCratesRED or {} }) do
        local arrived = {}
        for name, crateType in pairs(crates) do
            local run = runForWeight(type(crateType) == "table" and crateType.weight)
            if run then
                local object = ctld.getCrateObject(name)
                if object and object:isExist() and onTheGround(object)
                    and nearDestination(run, object:getPoint()) then
                    arrived[name] = { run = run, object = object }
                end
            end
        end
        for name, crate in pairs(arrived) do
            if countCrate(crate.run) then
                crates[name] = nil
                pcall(crate.object.destroy, crate.object)
            end
        end
    end
    return timer.getTime() + 15
end

local function checkCratesSafely()
    local ok, result = pcall(checkCrates)
    if not ok then
        log("supply crate check failed: " .. tostring(result))
        return timer.getTime() + 15
    end
    return result
end

if next(crateRuns) then
    -- CTLD sets itself up a couple of seconds into the mission.
    timer.scheduleFunction(function()
        local ok, err = pcall(spawnSupplyCrates)
        if not ok then
            log("spawning supply crates failed: " .. tostring(err))
        end
        return nil
    end, nil, timer.getTime() + 4)
    timer.scheduleFunction(checkCratesSafely, nil, timer.getTime() + 20)
end

-- SAM missiles ------------------------------------------------------------------------
--
-- With "Limited SAM missiles", each land SAM site may fire only its allowance of
-- missiles this mission (its share of its base's stock; see game/warehouse/sam.py).
-- Launches are charged to the base. A group with nothing left to fire is set to hold
-- fire (re-applied every few seconds in case an IADS script turns it back on), and a
-- missile fired past the allowance is destroyed at launch. Launcher loads are reported
-- so Retribution learns what each SAM type carries.

local sam = data.sam or {}
local samSites = sam.sites or {}  -- site key -> { cp, allow = { missile = n }, groups }
local samUnits = sam.units or {}  -- unit name -> site key
local samFired = {}               -- site key -> { missile = launches }
local samHolding = {}             -- group name -> true while held
W.sam_used = {}                   -- control point id -> { missile = launches }
W.sam_loads = {}                  -- DCS unit type -> { missile = count when full }

local function isMissile(name)
    return startsWith(name, "weapons.missiles.")
end

local function normalizedMissile(name)
    if type(name) ~= "string" then
        return nil
    end
    if not startsWith(name, "weapons.") then
        name = "weapons.missiles." .. name
    end
    return name
end

local function weaponName(weapon)
    local ok, name = pcall(weapon.getTypeName, weapon)
    if not ok then
        return nil
    end
    return normalizedMissile(name)
end

local MISSILE = (Weapon and Weapon.Category and Weapon.Category.MISSILE) or 1

local function samMissilesOf(unit)
    -- A SAM's missiles, by name. Unlike aircraft weapons, ground units' missiles are
    -- often reported without the "weapons.missiles." prefix, so go by category.
    local result = {}
    local ok, ammo = pcall(unit.getAmmo, unit)
    if ok and type(ammo) == "table" then
        for _, entry in ipairs(ammo) do
            local desc = entry.desc or {}
            local raw = desc.typeName
            if raw and (desc.category == MISSILE or isMissile(raw)) then
                local name = normalizedMissile(raw)
                result[name] = (result[name] or 0) + (entry.count or 0)
            end
        end
    end
    return result
end

local function holdFire(groupName)
    local ok, group = pcall(Group.getByName, groupName)
    if not ok or not group then
        return
    end
    local controller = group:getController()
    if controller then
        local okCat, category = pcall(group.getCategory, group)
        local naval = okCat and Group.Category and category == Group.Category.SHIP
        local options = naval and AI.Option.Naval or AI.Option.Ground
        pcall(controller.setOption, controller, options.id.ROE,
            options.val.ROE.WEAPON_HOLD)
    end
    if not samHolding[groupName] then
        samHolding[groupName] = true
        log("SAM group " .. groupName .. " is out of missiles and holds fire")
    end
end

local function samLeft(key, missile)
    local site = samSites[key]
    local limit = site and site.allow and site.allow[missile]
    if limit == nil then
        return nil -- not limited (not learned yet)
    end
    local fired = (samFired[key] and samFired[key][missile]) or 0
    return limit - fired
end

local function groupOutOfMissiles(key, group)
    -- True if every missile this group's launchers carry has no allowance left.
    local any = false
    local okUnits, units = pcall(group.getUnits, group)
    if not okUnits or type(units) ~= "table" then
        return false
    end
    for _, unit in ipairs(units) do
        for missile, count in pairs(samMissilesOf(unit)) do
            if count > 0 then
                any = true
                local left = samLeft(key, missile)
                if left == nil or left > 0 then
                    return false
                end
            end
        end
    end
    return any
end

local function onSamShot(event)
    local shooter, weapon = event.initiator, event.weapon
    if not shooter or not weapon or not shooter.getName then
        return
    end
    local okName, unitName = pcall(shooter.getName, shooter)
    local key = okName and samUnits[unitName]
    if not key then
        return
    end
    local site = samSites[key]
    local missile = weaponName(weapon)
    if not site or not missile then
        return
    end
    local left = samLeft(key, missile)
    local okGroup, group = pcall(shooter.getGroup, shooter)
    if left ~= nil and left <= 0 then
        -- Past the allowance: this missile was never there.
        pcall(weapon.destroy, weapon)
        if okGroup and group then
            holdFire(group:getName())
        end
        return
    end
    samFired[key] = samFired[key] or {}
    samFired[key][missile] = (samFired[key][missile] or 0) + 1
    local used = W.sam_used[site.cp] or {}
    used[missile] = (used[missile] or 0) + 1
    W.sam_used[site.cp] = used
    dirty_state = true
    if okGroup and group and groupOutOfMissiles(key, group) then
        holdFire(group:getName())
    end
end

local samEvents = {}
function samEvents:onEvent(event)
    if event.id ~= world.event.S_EVENT_SHOT then
        return
    end
    local ok, err = pcall(onSamShot, event)
    if not ok then
        log("SAM shot handling failed: " .. tostring(err))
    end
end

local function describeAmmo(unit)
    local parts = {}
    local ok, ammo = pcall(unit.getAmmo, unit)
    if ok and type(ammo) == "table" then
        for _, entry in ipairs(ammo) do
            local desc = entry.desc or {}
            parts[#parts + 1] = string.format("%s(cat %s) x%s", tostring(desc.typeName),
                tostring(desc.category), tostring(entry.count))
        end
    end
    return table.concat(parts, ", ")
end

local function samStart()
    local described = {}
    local present = 0
    for unitName, key in pairs(samUnits) do
        local unit = aliveUnit(unitName)
        if unit then
            present = present + 1
            local okT, typeName = pcall(unit.getTypeName, unit)
            if okT and typeName and not described[typeName] then
                described[typeName] = true
                log("SAM unit type " .. tostring(typeName) .. " carries: " .. describeAmmo(unit))
            end
            local missiles = samMissilesOf(unit)
            if next(missiles) then
                local okType, unitType = pcall(unit.getTypeName, unit)
                if okType and unitType then
                    local known = W.sam_loads[unitType] or {}
                    for missile, count in pairs(missiles) do
                        known[missile] = math.max(known[missile] or 0, count)
                    end
                    W.sam_loads[unitType] = known
                end
            end
        end
    end
    log(string.format("SAM tracking: %d of the tracked SAM units are in this mission", present))
    for key, site in pairs(samSites) do
        for _, groupName in ipairs(site.groups or {}) do
            local ok, group = pcall(Group.getByName, groupName)
            if ok and group and groupOutOfMissiles(key, group) then
                holdFire(groupName)
            end
        end
    end
    dirty_state = true
end

local function samKeepHolding()
    for groupName, _ in pairs(samHolding) do
        holdFire(groupName)
    end
    return timer.getTime() + 5
end

if next(samUnits) then
    world.addEventHandler(samEvents)
    timer.scheduleFunction(function()
        local ok, err = pcall(samStart)
        if not ok then
            log("SAM setup failed: " .. tostring(err))
        end
        return nil
    end, nil, timer.getTime() + 6)
    timer.scheduleFunction(function()
        local ok, result = pcall(samKeepHolding)
        if not ok then
            log("SAM hold check failed: " .. tostring(result))
        end
        return timer.getTime() + 5
    end, nil, timer.getTime() + 10)
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
        replenished = W.replenished,
        crates_delivered = W.crates_delivered,
        sam_used = W.sam_used,
        sam_loads = W.sam_loads,
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
