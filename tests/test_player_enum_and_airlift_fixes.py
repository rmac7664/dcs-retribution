"""Regressions for Player-enum truthiness and the helicopter airlift range check."""

from types import SimpleNamespace
from typing import Any

from dcs.mapping import Point
from dcs.terrain import Caucasus

from game.coalition import Coalition
from game.theater.player import Player
from game.transfers import AirliftPlanner
from game.utils import nautical_miles

TERRAIN = Caucasus()


def point(nm_east: float) -> Point:
    return Point(0, nautical_miles(nm_east).meters, TERRAIN)


def fake_coalition(player: Player, automate: bool) -> tuple[Coalition, list[str]]:
    refunded: list[str] = []
    base = SimpleNamespace(
        ground_unit_orders=SimpleNamespace(
            refund_all=lambda _coalition: refunded.append("ground")
        )
    )
    squadron = SimpleNamespace(refund_orders=lambda: refunded.append("air"))
    coalition = Coalition.__new__(Coalition)
    coalition.player = player
    coalition.game = SimpleNamespace(  # type: ignore[assignment]
        settings=SimpleNamespace(automate_aircraft_reinforcements=automate),
        theater=SimpleNamespace(control_points_for=lambda _player: [base]),
    )
    coalition.air_wing = SimpleNamespace(  # type: ignore[assignment]
        iter_squadrons=lambda: [squadron]
    )
    return coalition, refunded


def test_red_orders_are_refunded_even_when_blue_does_not_automate() -> None:
    coalition, refunded = fake_coalition(Player.RED, automate=False)
    coalition.refund_outstanding_orders()
    assert refunded == ["ground", "air"]


def test_blue_orders_are_kept_when_blue_does_not_automate() -> None:
    coalition, refunded = fake_coalition(Player.BLUE, automate=False)
    coalition.refund_outstanding_orders()
    assert refunded == []


def helicopter_planner(destination_nm: float) -> AirliftPlanner:
    anywhere = SimpleNamespace(can_operate=lambda _aircraft: True)
    origin = SimpleNamespace(can_operate=anywhere.can_operate, position=point(0))
    planner = AirliftPlanner.__new__(AirliftPlanner)
    planner.transfer = SimpleNamespace(  # type: ignore[assignment]
        origin=origin, position=origin
    )
    planner.next_stop = SimpleNamespace(  # type: ignore[assignment]
        can_operate=anywhere.can_operate, position=point(destination_nm)
    )
    return planner


def helicopter() -> Any:
    return SimpleNamespace(
        capable_of=lambda _task: True,
        dcs_unit_type=SimpleNamespace(helicopter=True),
    )


def test_helicopter_airlift_checks_the_leg_to_the_destination() -> None:
    home: Any = SimpleNamespace(position=point(0))
    assert helicopter_planner(60).compatible_with_mission(helicopter(), home)
    # Pickup is at home, so only the 150 nm leg is too long.
    assert not helicopter_planner(150).compatible_with_mission(helicopter(), home)


class FakeTransports:
    def __init__(self, transports: list[Any]) -> None:
        self.transports = transports

    def travelling_to(self, cp: Any) -> list[Any]:
        return [t for t in self.transports if t.destination is cp]

    def __iter__(self) -> Any:
        return iter(self.transports)


def transport(name: str, destination: Any, supplies: Any = None) -> Any:
    order = SimpleNamespace(supplies=supplies)
    return SimpleNamespace(name=name, destination=destination, transfers=[order])


def test_ai_targets_the_enemys_convoys_and_cargo_ships() -> None:
    from game.commander.objectivefinder import ObjectiveFinder

    red_front = SimpleNamespace(name="Red forward base")
    red_rear = SimpleNamespace(name="Red rear base")
    to_front = transport("tanks to the front", red_front)
    rear_supply = transport("munitions to the rear", red_rear, supplies="cargo")
    rear_tanks = transport("tanks to the rear", red_rear)
    red = FakeTransports([to_front, rear_supply, rear_tanks])
    blue = FakeTransports([transport("blue convoy", red_front)])

    def coalition_for(player: Player) -> Any:
        transports = red if player.is_red else blue
        return SimpleNamespace(
            transfers=SimpleNamespace(convoys=transports, cargo_ships=transports)
        )

    front_line = SimpleNamespace(control_point_hostile_to=lambda _player: red_front)
    game: Any = SimpleNamespace(
        settings=SimpleNamespace(
            perf_disable_convoys=False, perf_disable_cargo_ships=False
        ),
        theater=SimpleNamespace(conflicts=lambda: [front_line]),
        coalition_for=coalition_for,
    )
    finder = ObjectiveFinder(game, Player.BLUE)
    # Red's own front-line convoy, plus red supply runs anywhere; never blue's.
    assert list(finder.convoys()) == [to_front, rear_supply]
    assert list(finder.cargo_ships()) == [to_front, rear_supply]

    game.settings.perf_disable_cargo_ships = True
    assert list(finder.cargo_ships()) == []
