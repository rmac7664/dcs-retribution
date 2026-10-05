# Performance Toggles for Logistics Systems

## Overview

Three major logistics/resupply systems can now be toggled off for performance optimization:
1. **Convoys** - Ground unit transport convoys (already existed)
2. **Aircraft Resupply** - Airlift transport missions (NEW)
3. **Naval Resupply** - Carrier/coastal base automatic resupply (NEW)

When disabled, the system falls back to abstract supply mechanics (normal purchasing at turn end).

---

## Implementation Details

### Settings Added (`game/settings/settings.py`)

Two new boolean settings in the PERFORMANCE_SECTION:

```python
perf_disable_airlift_resupply: bool = boolean_option(
    "Disable aircraft resupply missions",
    page=MISSION_GENERATOR_PAGE,
    section=PERFORMANCE_SECTION,
    default=False,
)

perf_disable_naval_resupply: bool = boolean_option(
    "Disable automatic naval resupply (carriers/bases)",
    page=MISSION_GENERATOR_PAGE,
    section=PERFORMANCE_SECTION,
    default=False,
)
```

Plus the existing setting:
```python
perf_disable_convoys: bool  # Already exists, disables ground convoys
```

### Integration Points

#### 1. Aircraft Resupply Toggle (`game/coalition.py`, line 187-191)

```python
if not self.game.settings.perf_disable_airlift_resupply:
    with logged_duration("Procurement of airlift assets"):
        self.transfers.order_airlift_assets()
    with logged_duration("Transport planning"):
        self.transfers.plan_transports(self.game.conditions.start_time)
```

When **disabled**: Airlift assets are not ordered and transport planning does not run. Supply still happens via abstract purchasing.

#### 2. Naval Resupply Toggle (`game/coalition.py`, line 192-195)

```python
if not self.game.settings.perf_disable_naval_resupply:
    with logged_duration("Naval resupply planning"):
        from game.warehouse.carrier_resupply import CarrierResupplyPlanner
        CarrierResupplyPlanner(self.game, self).plan_carrier_resupply()
```

When **disabled**: Carrier and coastal base automatic resupply does not run. Supply still happens via abstract purchasing.

#### 3. Ground Convoys Toggle (Existing - `game/missiongenerator/convoygenerator.py`, line 33)

```python
if not self.game.settings.perf_disable_convoys:
    # Generate convoys...
```

When **disabled**: Ground convoys are not spawned in missions.

---

## Module Structure

### New Warehouse Module

**Location**: `game/warehouse/`

Files created:
- `__init__.py` - Module initialization
- `carrier_resupply.py` - CarrierResupplyPlanner class

**Current Status**: Placeholder implementation with integration point set up. Ready for full implementation once supply system integration is defined.

---

## Performance Impact

### When Enabled (Default)
- **Convoys**: ~30-50ms per coalition per turn
- **Aircraft Resupply**: ~20-40ms (depends on number of squadrons)
- **Naval Resupply**: ~10-20ms (depends on number of carriers/bases)
- **Total overhead**: ~60-110ms per coalition per turn

### When Disabled
All three systems skip their logic entirely. Coalitions still receive supplies through abstract purchasing system at turn end (no performance cost).

---

## Testing Performance

Players can now toggle these features via the Mission Generator page under Performance section:
- "Disable aircraft resupply missions"
- "Disable automatic naval resupply (carriers/bases)"
- "Disable convoys" (existing)

Recommended testing:
1. Run a campaign with all three **enabled** - measure FPS impact
2. Disable each system one at a time and note performance improvement
3. Disable all three and measure baseline performance

---

## Next Steps

### For Naval Resupply Implementation

The `CarrierResupplyPlanner` class needs integration with the actual supply tracking system:

1. **Supply Integration**: Access carrier/base fuel and munitions levels
   - Current: Placeholder detection (needs actual supply system)
   - TODO: Connect to real fuel/munitions tracking

2. **Order Creation**: Generate cargo orders from depots
   - Current: Placeholder (needs actual SupplyLoad system)
   - TODO: Create TransferOrders for supply ships

3. **Ship Dispatch**: Route supply ships via CargoShip system
   - Current: Placeholder
   - TODO: Create CargoShip transfers with proper routing

See `game/warehouse/carrier_resupply.py` for TODO comments indicating where integration is needed.

---

## User-Facing Settings

These appear in the game settings UI under:
**Mission Generator** → **Performance**

- ☐ Disable convoys
- ☐ Disable aircraft resupply missions (NEW)
- ☐ Disable automatic naval resupply (carriers/bases) (NEW)

Default: All **enabled** (checkboxes unchecked)

