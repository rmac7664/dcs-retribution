"""Switching a ship or convoy's munitions to air delivery during the turn.

Supply goes by road convoy or cargo ship between bases (transfer orders, see
game/warehouse/supply.py), and by replenishment ship to carriers and LHAs (SupplyShip).
During turn planning, either side can instead fly the munitions in with its own
transport aircraft:

* Munitions only. Fuel stays with the ship or convoy (hundreds of tonnes of fuel isn't
  practical to fly).
* If the aircraft can't take everything, the destination's most urgent items fly
  first and the rest stays with the ship or convoy. A ship or convoy left with
  nothing doesn't go.
* The cargo is loaded where the convoy or ship would leave from: the run's current
  position, or for a replenishment ship the side's main supply base (or the nearest
  land depot the aircraft can reach).
* From there it is an ordinary supply airlift: transport flights in this turn's ATO,
  delivered when they land, lost if shot down.
* The player can undo it until the mission starts; the cargo goes back on the ship or
  convoy. The AI (red) does it on its own when a destination is badly short or its
  sea or road route was hit (ai_send_by_air).
"""

from __future__ import annotations

import itertools
import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, TYPE_CHECKING, Union

from game.theater.controlpoint import ControlPoint, NavalControlPoint, OffMapSpawn

from .state import SupplyShip
from .supply import (
    MunitionMasses,
    SupplyLoad,
    SupplyPlanner,
    URGENT_SHARE,
    cargo_tons,
    main_base,
    supply_of,
)

if TYPE_CHECKING:
    from game import Game
    from game.coalition import Coalition
    from game.squadrons import Squadron
    from game.transfers import TransferOrder

#: A shipment that can be switched to air: a supply run going by road or sea, or a
#: replenishment ship.
Shipment = Union["TransferOrder", SupplyShip]

#: Loads are planned a little under the aircraft's limit, so rounding never leaves a
#: pallet without an aircraft.
CAPACITY_MARGIN = 0.95

#: Most switches the AI makes per turn, so it keeps transports for other work.
AI_MAX_SWITCHES = 2

_switch_ids = itertools.count(1)


@dataclass
class AirSwitch:
    """Where an air delivery's cargo came from, so it can be put back."""

    turn: int
    #: The replenishment ship the cargo came off, or None.
    ship: Optional[SupplyShip]
    #: The road/sea run the cargo came off (still pending with what's left), or None.
    ground: Optional[TransferOrder]
    #: Where the road/sea run was and where it was going.
    position: ControlPoint
    destination: ControlPoint
    id: int = field(default_factory=lambda: next(_switch_ids))


@dataclass
class AirOption:
    """A transport squadron that could fly a shipment's munitions."""

    squadron: Squadron
    #: Where the aircraft load the cargo.
    origin: Optional[ControlPoint]
    #: Aircraft free to fly this turn.
    aircraft: int
    #: Tonnes each aircraft can carry.
    tons_each: float
    #: Tonnes of munitions the shipment carries.
    cargo_tons: float
    #: Why it can't fly the cargo, or None if it can.
    problem: Optional[str] = None

    @property
    def capacity_tons(self) -> float:
        return self.aircraft * self.tons_each

    @property
    def sorties(self) -> int:
        """Aircraft needed to carry all of the shipment's munitions."""
        return max(1, math.ceil(self.cargo_tons / max(0.1, self.tons_each)))

    @property
    def carries_all(self) -> bool:
        return self.capacity_tons * CAPACITY_MARGIN >= self.cargo_tons

    def describe(self) -> str:
        if self.problem:
            return f"{self.squadron} — {self.problem}"
        share = (
            "all of it"
            if self.carries_all
            else f"{min(self.capacity_tons * CAPACITY_MARGIN, self.cargo_tons):.1f} t "
            "of it (the most urgent items)"
        )
        return (
            f"{self.squadron} ({self.squadron.aircraft}) from {self.origin}: "
            f"{self.aircraft} free, {self.tons_each:.1f} t each, "
            f"{self.sorties} needed for {self.cargo_tons:.1f} t — carries {share}"
        )


# What a shipment carries -------------------------------------------------------------


def _coalition(game: Game, shipment: Shipment) -> Coalition:
    from game.theater.player import Player

    if isinstance(shipment, SupplyShip):
        return game.coalition_for(Player[shipment.side])
    return game.coalition_for(shipment.player)


def destination_of(game: Game, shipment: Shipment) -> Optional[ControlPoint]:
    if isinstance(shipment, SupplyShip):
        try:
            return game.theater.find_control_point_by_id(shipment.carrier_id)
        except KeyError:
            return None
    return shipment.destination


def munitions_of(shipment: Shipment) -> dict[str, int]:
    """The munitions a shipment still carries."""
    if isinstance(shipment, SupplyShip):
        return {k: v for k, v in shipment.munitions.items() if v > 0}
    load = supply_of(shipment)
    if load is None or shipment.size <= 0:
        return {}
    return dict(load.scaled(shipment.size / max(1, load.carriers)).munitions)


def is_by_ground(transfer: TransferOrder) -> bool:
    """True if a supply run is travelling by road convoy or cargo ship."""
    from game.transfers import CargoShip, Convoy

    return isinstance(transfer.transport, (Convoy, CargoShip))


def switchable(game: Game, shipment: Shipment) -> bool:
    """True if this shipment's munitions could be switched to air delivery."""
    if not getattr(game.settings, "logistics_enabled", False):
        return False
    if not munitions_of(shipment):
        return False
    destination = destination_of(game, shipment)
    if destination is None:
        return False
    if isinstance(shipment, SupplyShip):
        return shipment in game.warehouse_logistics.supply_ships and isinstance(
            destination, NavalControlPoint
        )
    return is_by_ground(shipment) and getattr(shipment, "air_switch", None) is None


def switchable_shipments(game: Game, coalition: Coalition) -> list[Shipment]:
    shipments: list[Shipment] = [
        t
        for t in coalition.transfers.pending_transfers
        if supply_of(t) is not None and switchable(game, t)
    ]
    shipments.extend(
        ship
        for ship in game.warehouse_logistics.supply_ships
        if ship.side == coalition.player.name and switchable(game, ship)
    )
    return shipments


def describe_shipment(game: Game, shipment: Shipment) -> str:
    destination = destination_of(game, shipment)
    to = destination.name if destination is not None else "?"
    munitions = munitions_of(shipment)
    tons = MunitionMasses.tons(munitions)
    what = f"{sum(munitions.values())} munitions ({tons:.1f} t)"
    if isinstance(shipment, SupplyShip):
        return f"Replenishment ship to {to}: {what}"
    by = "convoy" if "Convoy" in type(shipment.transport).__name__ else "cargo ship"
    return f"{shipment.position.name} to {to} by {by}: {what}"


# Which aircraft could fly it ---------------------------------------------------------


def _load_points(
    game: Game, coalition: Coalition, shipment: Shipment
) -> list[ControlPoint]:
    """Where the cargo can be loaded onto aircraft, best first."""
    if not isinstance(shipment, SupplyShip):
        return [shipment.position]
    destination = destination_of(game, shipment)
    depots = [
        cp
        for cp in game.theater.controlpoints
        if cp.captured == coalition.player
        and not isinstance(cp, (NavalControlPoint, OffMapSpawn))
        and SupplyPlanner.is_depot(cp)
    ]
    base = main_base(game, coalition.player)

    def rank(cp: ControlPoint) -> tuple[int, float]:
        far = (
            cp.position.distance_to_point(destination.position)
            if destination is not None
            else 0.0
        )
        return (0 if cp is base else 1, far)

    return sorted(depots, key=rank)


def air_options(game: Game, shipment: Shipment) -> list[AirOption]:
    """Transport squadrons that could fly the shipment's munitions, best first."""
    from game.ato.flighttype import FlightType

    coalition = _coalition(game, shipment)
    destination = destination_of(game, shipment)
    if destination is None:
        return []
    cargo = MunitionMasses.tons(munitions_of(shipment))
    points = _load_points(game, coalition, shipment)
    options = []
    for squadron in coalition.air_wing.iter_squadrons():
        aircraft = squadron.aircraft
        if not aircraft.capable_of(FlightType.TRANSPORT):
            continue
        free = squadron.untasked_crewed_aircraft
        origin = next(
            (
                p
                for p in points
                if destination.can_operate(aircraft)
                and SupplyPlanner.can_fly_leg(
                    aircraft, squadron.location, p, destination
                )
            ),
            None,
        )
        problem = None
        if not destination.can_operate(aircraft):
            problem = f"can't land at {destination.name}"
        elif origin is None:
            problem = (
                "out of reach (helicopters fly legs of 100 nm at most)"
                if aircraft.dcs_unit_type.helicopter
                else "can't load the cargo anywhere in reach"
            )
        elif free <= 0:
            problem = "no aircraft free this turn"
        options.append(
            AirOption(
                squadron, origin, max(0, free), cargo_tons(aircraft), cargo, problem
            )
        )
    options.sort(key=lambda o: (o.problem is not None, -min(o.capacity_tons, cargo)))
    return options


# Switching ---------------------------------------------------------------------------


def _urgency_order(
    game: Game, destination: ControlPoint, munitions: dict[str, int]
) -> list[tuple[str, int]]:
    """Munitions the destination is shortest of first; then the most valuable."""
    from .supply import MunitionPrices

    state = game.warehouse_logistics
    authorized = state.authorized_munitions(game, destination)
    stock = state.stocks.get(destination.id)
    held = stock.munitions if stock is not None else {}

    def urgency(name: str) -> float:
        want = max(1, authorized.get(name, 1))
        return (want - held.get(name, 0)) / want

    return sorted(
        munitions.items(),
        key=lambda kv: (
            -round(urgency(kv[0]), 2),
            -MunitionPrices.price(kv[0], game.settings),
        ),
    )


def fit_to_capacity(
    game: Game, destination: ControlPoint, munitions: dict[str, int], tons: float
) -> dict[str, int]:
    """The most urgent munitions that fit in `tons`."""
    fitted: dict[str, int] = {}
    used = 0.0
    for name, count in _urgency_order(game, destination, munitions):
        mass = MunitionMasses.mass_kg(name)
        room = int((tons * 1000 - used) // mass) if mass > 0 else count
        take = min(count, room)
        if take > 0:
            fitted[name] = take
            used += take * mass
    return fitted


def _minus(a: dict[str, int], b: dict[str, int]) -> dict[str, int]:
    out = dict(a)
    for name, count in b.items():
        out[name] = out.get(name, 0) - count
        if out[name] <= 0:
            del out[name]
    return out


def _truck_load(game: Game, munitions: dict[str, int], fuel_kg: float) -> SupplyLoad:
    settings = game.settings
    tons = MunitionMasses.tons(munitions, fuel_kg)
    trucks = min(
        settings.logistics_max_trucks_per_shipment,
        max(1, math.ceil(tons / max(0.1, settings.logistics_truck_tons))),
    )
    return SupplyLoad(munitions=munitions, fuel_kg=fuel_kg, tons=tons, carriers=trucks)


def _reload_ground_run(game: Game, transfer: TransferOrder, load: SupplyLoad) -> None:
    """Puts a road/sea run back on the road or shipping lane with `load`."""
    coalition = game.coalition_for(transfer.player)
    transfers = coalition.transfers
    if transfer.transport is not None:
        transfers.cancel_transport(transfer.transport, transfer)
        transfer.transport = None
    truck = (
        next(iter(transfer.units), None) or SupplyPlanner(game, coalition).cargo_truck()
    )
    if truck is None:
        return
    transfer.units.clear()
    transfer.units[truck] = load.carriers
    transfer.supplies = load
    transfer.request_airflift = False
    transfers.arrange_transport(transfer, game.conditions.start_time)


def send_by_air(
    game: Game, shipment: Shipment, option: AirOption
) -> list[TransferOrder]:
    """Flies the shipment's munitions (or the most urgent part) with `option`.

    Returns the airlift supply runs created. Raises ValueError if the option can't
    fly it.
    """
    from game.transfers import AirliftPlanner, TransferOrder

    if option.problem is not None or option.origin is None:
        raise ValueError(option.problem or "no aircraft")
    if not switchable(game, shipment):
        raise ValueError("this shipment can't be sent by air")
    coalition = _coalition(game, shipment)
    destination = destination_of(game, shipment)
    assert destination is not None
    munitions = munitions_of(shipment)
    air = fit_to_capacity(
        game,
        destination,
        munitions,
        min(option.capacity_tons * CAPACITY_MARGIN, option.cargo_tons + 1),
    )
    if not air:
        raise ValueError("the aircraft can't carry any of it")
    truck = SupplyPlanner(game, coalition).cargo_truck()
    if truck is None:
        raise ValueError("no cargo vehicles to load")

    # Take the cargo off the ship or convoy.
    state = game.warehouse_logistics
    if isinstance(shipment, SupplyShip):
        switch = AirSwitch(game.turn, shipment, None, option.origin, destination)
        shipment.munitions = _minus(shipment.munitions, air)
        if shipment.is_empty():
            state.supply_ships.remove(shipment)
    else:
        load = supply_of(shipment)
        assert load is not None
        remaining = load.scaled(shipment.size / max(1, load.carriers))
        rest = _minus(remaining.munitions, air)
        switch = AirSwitch(game.turn, None, shipment, shipment.position, destination)
        if rest or remaining.fuel_kg >= 1:
            _reload_ground_run(
                game, shipment, _truck_load(game, rest, remaining.fuel_kg)
            )
        else:
            if shipment.transport is not None:
                coalition.transfers.cancel_transport(shipment.transport, shipment)
            coalition.transfers.pending_transfers.remove(shipment)
            shipment.units.clear()

    # Fly it: one pallet per aircraft.
    tons = MunitionMasses.tons(air)
    pallets = max(1, math.ceil(tons / max(0.1, option.tons_each * CAPACITY_MARGIN)))
    run = TransferOrder(
        option.origin,
        destination,
        {truck: pallets},
        request_airflift=True,
        supplies=SupplyLoad(munitions=air, tons=tons, carriers=pallets),
    )
    run.air_switch = switch
    coalition.transfers.pending_transfers.append(run)
    planner = AirliftPlanner(game, run, destination)
    squadron = option.squadron
    while (
        squadron.untasked_aircraft
        and squadron.has_available_pilots
        and run.transport is None
    ):
        planner.create_airlift_flight(squadron)
    runs = [f.cargo for f in planner.package.flights if f.cargo is not None]
    for flown in runs:
        flown.air_switch = switch
    if planner.package.flights:
        planner.package.set_tot_asap(game.conditions.start_time)
        game.ato_for(coalition.player).add_package(planner.package)
        _announce_flights(planner.package.flights)
    if run.transport is None:
        # Ran out of aircraft for the last pallets: they go back on the ship/convoy.
        _put_back(game, switch, [run])
    coalition.transfers._send_supply_route_event_stream_update()
    logging.info(
        "Supply: %s sends %s to %s by air (%s, %d aircraft)",
        coalition.player.name,
        SupplyLoad(munitions=air, tons=tons).describe(),
        destination.name,
        squadron,
        sum(f.count for f in planner.package.flights),
    )
    return [r for r in runs if r.transport is not None]


def _announce_flights(flights: Iterable[Any]) -> None:
    from game.server import EventStream
    from game.sim import GameUpdateEvents

    events = GameUpdateEvents()
    for flight in flights:
        events = events.new_flight(flight)
    EventStream.put_nowait(events)


# Undoing -----------------------------------------------------------------------------


def switched_runs(game: Game, transfer: TransferOrder) -> list[TransferOrder]:
    switch = getattr(transfer, "air_switch", None)
    if switch is None:
        return []
    coalition = game.coalition_for(transfer.player)
    return [
        t
        for t in coalition.transfers.pending_transfers
        if getattr(t, "air_switch", None) is switch
    ]


def can_undo(game: Game, transfer: TransferOrder) -> bool:
    """True if this air delivery was switched this turn and can still be undone."""
    switch = getattr(transfer, "air_switch", None)
    return switch is not None and switch.turn == game.turn


def _put_back(game: Game, switch: AirSwitch, runs: list[TransferOrder]) -> None:
    """Returns the runs' cargo to the ship or convoy it came from."""
    from game.transfers import TransferOrder

    munitions: Counter[str] = Counter()
    any_run = runs[0] if runs else None
    for run in runs:
        munitions.update(munitions_of(run))
        coalition = game.coalition_for(run.player)
        if run.transport is not None:
            coalition.transfers.cancel_transport(run.transport, run)
            run.transport = None
        if run in coalition.transfers.pending_transfers:
            coalition.transfers.pending_transfers.remove(run)
        run.units.clear()
    if not munitions or any_run is None:
        return
    state = game.warehouse_logistics
    if switch.ship is not None:
        ship = switch.ship
        for name, count in munitions.items():
            ship.munitions[name] = ship.munitions.get(name, 0) + count
        if ship not in state.supply_ships:
            state.supply_ships.append(ship)
        return
    coalition = game.coalition_for(any_run.player)
    ground = switch.ground
    if ground is not None and ground in coalition.transfers.pending_transfers:
        load = supply_of(ground)
        assert load is not None
        current = load.scaled(ground.size / max(1, load.carriers))
        merged = dict(Counter(current.munitions) + munitions)
        _reload_ground_run(game, ground, _truck_load(game, merged, current.fuel_kg))
        return
    truck = SupplyPlanner(game, coalition).cargo_truck()
    if truck is None:
        return
    load = _truck_load(game, dict(munitions), 0.0)
    run = TransferOrder(
        switch.position, switch.destination, {truck: load.carriers}, supplies=load
    )
    coalition.transfers.pending_transfers.append(run)
    coalition.transfers.arrange_transport(run, game.conditions.start_time)
    if ground is not None:
        switch.ground = run


def undo_send_by_air(game: Game, transfer: TransferOrder) -> None:
    """Puts an air delivery's cargo back on the ship or convoy it came off."""
    if not can_undo(game, transfer):
        raise ValueError("this delivery can't be undone")
    switch = transfer.air_switch
    assert isinstance(switch, AirSwitch)
    runs = switched_runs(game, transfer)
    _put_back(game, switch, runs)
    game.coalition_for(
        transfer.player
    ).transfers._send_supply_route_event_stream_update()
    logging.info(
        "Supply: air delivery to %s undone; cargo back on the %s",
        switch.destination.name,
        "ship" if switch.ship is not None else "convoy or cargo ship",
    )


# The AI ------------------------------------------------------------------------------


def _urgently_short(
    game: Game, destination: ControlPoint, munitions: dict[str, int]
) -> bool:
    """True if the destination holds less than URGENT_SHARE of something on board."""
    state = game.warehouse_logistics
    authorized = state.authorized_munitions(game, destination)
    stock = state.stocks.get(destination.id)
    held = stock.munitions if stock is not None else {}
    return any(
        authorized.get(name, 0) > 0
        and held.get(name, 0) < authorized[name] * URGENT_SHARE
        for name in munitions
    )


def _route_threatened(game: Game, coalition: Coalition, shipment: Shipment) -> bool:
    """True if this way of getting there was hit last mission.

    A replenishment ship to the same carrier was sunk, or this convoy or cargo ship
    lost trucks on the way.
    """
    destination = destination_of(game, shipment)
    sunk = getattr(game.warehouse_logistics, "last_ships_sunk", {}) or {}
    if destination is not None and destination.name in sunk.get(
        coalition.player.name, []
    ):
        return True
    if isinstance(shipment, SupplyShip):
        return False
    load = supply_of(shipment)
    return load is not None and shipment.size < load.carriers


def ai_send_by_air(game: Game, coalition: Coalition) -> list[str]:
    """The AI flies urgent or threatened shipments with spare transports.

    A shipment is switched when its destination is badly short of something it
    carries, or the side lost a supply ship to that destination last turn, and a
    transport squadron in reach has aircraft free. At most AI_MAX_SWITCHES a turn.
    """
    settings = game.settings
    if not settings.logistics_enabled:
        return []
    if coalition.player.is_red and not getattr(
        settings, "logistics_apply_to_opfor", True
    ):
        return []
    done: list[str] = []
    for shipment in switchable_shipments(game, coalition):
        if len(done) >= AI_MAX_SWITCHES:
            break
        destination = destination_of(game, shipment)
        if destination is None:
            continue
        munitions = munitions_of(shipment)
        if not (
            _urgently_short(game, destination, munitions)
            or _route_threatened(game, coalition, shipment)
        ):
            continue
        usable = [o for o in air_options(game, shipment) if o.problem is None]
        if not usable:
            continue
        try:
            if send_by_air(game, shipment, usable[0]):
                done.append(destination.name)
        except ValueError:
            logging.exception("Supply: AI could not switch a shipment to air")
    return done
