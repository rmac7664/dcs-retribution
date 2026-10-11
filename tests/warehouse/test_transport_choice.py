"""The AI picks the transport squadron that fits the load best."""

from types import SimpleNamespace
from typing import Any

from game.transfers import AirliftPlanner
from game.warehouse.supply import SupplyLoad


def squadron(dcs_id: str, free: int, helicopter: bool = False) -> Any:
    unit = SimpleNamespace(id=dcs_id, helicopter=helicopter, group_size_max=4)
    return SimpleNamespace(
        aircraft=SimpleNamespace(dcs_unit_type=unit),
        untasked_aircraft=free,
        has_available_pilots=True,
        max_fulfillable_aircraft=free,
        name=dcs_id,
    )


def planner(tons: float) -> Any:
    p: Any = AirliftPlanner.__new__(AirliftPlanner)
    p.transfer = SimpleNamespace(
        supplies=SupplyLoad(tons=tons, carriers=1), size=1, transport=None
    )
    return p


C17 = squadron("C-17A", 2)
C130 = squadron("C-130J-30", 4)
C2 = squadron("C2A_Greyhound", 4)
HIP = squadron("Mi-8MT", 4, helicopter=True)


def test_small_load_gets_the_smallest_aircraft_that_carries_it() -> None:
    assert planner(3.0)._pick_squadron([C17, C130, C2, HIP]) is HIP
    assert planner(4.4)._pick_squadron([C17, C130, C2, HIP]) is C2


def test_one_big_aircraft_beats_several_small_ones() -> None:
    # 25 t: one C-17 rather than two C-130s.
    assert planner(25.0)._pick_squadron([C130, C17]) is C17


def test_a_load_nothing_carries_in_one_flight_goes_biggest_first() -> None:
    assert planner(400.0)._pick_squadron([C2, C130, C17]) is C17


def test_squadrons_that_cant_go_are_never_offered() -> None:
    # Carrier runs: the candidate list only holds squadrons that can land there.
    assert planner(25.0)._pick_squadron([C2]) is C2
    assert planner(25.0)._pick_squadron([squadron("C-17A", 0)]) is None
