-- Carrier Deck Clear
--
-- Removes AI aircraft from carrier and ship decks a set time after they land,
-- so an aircraft stuck in the wires (or parked in the landing area) can't foul
-- the deck for everyone behind it. Players are never touched.
--
-- Two ways an aircraft gets picked up:
--   1. DCS reports it landed on a ship  -> removed after the delay.
--   2. Backstop: it has flown this mission and has now sat on a ship's deck,
--      not moving relative to the ship, for the delay, even though DCS never
--      reported a landing (this is the "stuck in the wires" case).
--
-- Aircraft waiting on the deck to launch are never touched: the backstop only
-- looks at aircraft that have already been airborne this mission.
--
-- Removed aircraft are added to RetributionDeckCleared so Retribution's own
-- event handler does not count them as lost.

local options = {
    despawnSecondsAfterLanding = 90,
    clearStuckAircraft = true,
    includeHelicopters = true,
    announce = false,
}

if dcsRetribution and dcsRetribution.plugins and dcsRetribution.plugins.deckclear then
    local cfg = dcsRetribution.plugins.deckclear
    for key, _ in pairs(options) do
        if cfg[key] ~= nil then
            options[key] = cfg[key]
        end
    end
end

local DELAY = math.max(15, tonumber(options.despawnSecondsAfterLanding) or 90)
local SCAN_INTERVAL = 5          -- seconds between backstop scans
local DECK_RADIUS = 200          -- metres from the ship's centre
local DECK_HEIGHT = 40           -- metres above the ship's waterline
local STILL_SPEED = 2.5          -- m/s relative to the ship counts as "not moving"

RetributionDeckCleared = RetributionDeckCleared or {}

local pending = {}       -- unitName -> true while a removal timer is running
local wasAirborne = {}   -- unitName -> true once seen in the air
local stillSince = {}    -- unitName -> mission time it started sitting still on a deck

local function log(msg)
    env.info("DECKCLEAR: " .. msg)
end

local function isShip(unit)
    if not unit or not unit.getDesc then return false end
    local ok, desc = pcall(unit.getDesc, unit)
    return ok and desc and desc.category == Unit.Category.SHIP
end

-- event.place on a ship landing can be the ship Unit or its Airbase object.
local function shipFromPlace(place)
    if not place then return nil end
    local ok, name = pcall(place.getName, place)
    if not ok or not name then return nil end
    local ship = Unit.getByName(name)
    if ship and isShip(ship) then return ship end
    return nil
end

local function isAiAircraft(unit)
    if not unit or not unit.isExist or not unit:isExist() then return false end
    if Object.getCategory(unit) ~= Object.Category.UNIT then return false end
    local desc = unit:getDesc()
    if not desc then return false end
    if desc.category == Unit.Category.HELICOPTER then
        if not options.includeHelicopters then return false end
    elseif desc.category ~= Unit.Category.AIRPLANE then
        return false
    end
    if unit:getPlayerName() ~= nil then return false end
    return true
end

local function velocityDiff(a, b)
    local va, vb = a:getVelocity(), b:getVelocity()
    local dx, dy, dz = va.x - vb.x, va.y - vb.y, va.z - vb.z
    return math.sqrt(dx * dx + dy * dy + dz * dz)
end

local function onDeckOf(unit, ship)
    local p, s = unit:getPoint(), ship:getPoint()
    local dx, dz = p.x - s.x, p.z - s.z
    if dx * dx + dz * dz > DECK_RADIUS * DECK_RADIUS then return false end
    return (p.y - s.y) < DECK_HEIGHT
end

local function removeUnit(unitName, why)
    local unit = Unit.getByName(unitName)
    if not unit or not unit:isExist() then return end
    if unit:getPlayerName() ~= nil then return end
    if unit:inAir() then
        log(unitName .. " is airborne again; not removing")
        return
    end
    RetributionDeckCleared[unitName] = true
    log("removing " .. unitName .. " (" .. why .. ")")
    if options.announce then
        trigger.action.outText("Deck clear: " .. unitName .. " struck below.", 10)
    end
    unit:destroy()
    stillSince[unitName] = nil
end

local function scheduleRemoval(unitName, why)
    if pending[unitName] then return end
    pending[unitName] = true
    log(string.format("%s %s; removing in %d s", unitName, why, DELAY))
    timer.scheduleFunction(function()
        pending[unitName] = nil
        removeUnit(unitName, why)
        return nil
    end, nil, timer.getTime() + DELAY)
end

-- 1. Landing reports -----------------------------------------------------

local handler = {}
function handler:onEvent(event)
    if not event or not event.initiator then return end
    local unit = event.initiator

    if event.id == world.event.S_EVENT_TAKEOFF then
        local ok, name = pcall(unit.getName, unit)
        if ok and name then wasAirborne[name] = true end
        return
    end

    if event.id ~= world.event.S_EVENT_LAND then return end
    if not isAiAircraft(unit) then return end
    local ship = shipFromPlace(event.place)
    if not ship then return end

    local unitName = unit:getName()
    wasAirborne[unitName] = true
    scheduleRemoval(unitName, "landed on " .. ship:getName())
end
world.addEventHandler(handler)

-- 2. Backstop scan -----------------------------------------------------------

-- Only ships aircraft can land on: DCS lists those as airbases of the ship
-- category, named after the ship unit.
local function ships()
    local list = {}
    for _, ab in pairs(world.getAirbases() or {}) do
        local desc = ab:getDesc()
        if desc and desc.category == Airbase.Category.SHIP then
            local ship = Unit.getByName(ab:getName())
            if ship and ship:isExist() then
                list[#list + 1] = ship
            end
        end
    end
    return list
end

local function aircraft()
    local list = {}
    for _, side in pairs({ coalition.side.BLUE, coalition.side.RED, coalition.side.NEUTRAL }) do
        for _, cat in pairs({ Group.Category.AIRPLANE, Group.Category.HELICOPTER }) do
            for _, group in pairs(coalition.getGroups(side, cat) or {}) do
                for _, unit in pairs(group:getUnits() or {}) do
                    list[#list + 1] = unit
                end
            end
        end
    end
    return list
end

local function scan()
    local ok, err = pcall(function()
        local now = timer.getTime()
        local decks = ships()
        if #decks == 0 then return end
        local seen = {}
        for _, unit in pairs(aircraft()) do
            if isAiAircraft(unit) then
                local name = unit:getName()
                if unit:inAir() then
                    wasAirborne[name] = true
                    stillSince[name] = nil
                elseif options.clearStuckAircraft and wasAirborne[name] and not pending[name] then
                    for _, ship in pairs(decks) do
                        if onDeckOf(unit, ship) and velocityDiff(unit, ship) < STILL_SPEED then
                            seen[name] = true
                            if not stillSince[name] then
                                stillSince[name] = now
                            elseif now - stillSince[name] >= DELAY then
                                removeUnit(name, "sat still on " .. ship:getName() .. " after recovering")
                            end
                            break
                        end
                    end
                end
            end
        end
        for name, _ in pairs(stillSince) do
            if not seen[name] then stillSince[name] = nil end
        end
    end)
    if not ok then log("scan error: " .. tostring(err)) end
    return timer.getTime() + SCAN_INTERVAL
end

do
    timer.scheduleFunction(scan, nil, timer.getTime() + SCAN_INTERVAL)
    log(string.format("active: remove AI %d s after landing on a ship (stuck-aircraft backstop %s, helicopters %s)",
        DELAY, tostring(options.clearStuckAircraft), tostring(options.includeHelicopters)))
end
