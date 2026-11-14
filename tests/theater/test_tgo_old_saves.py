from game.theater.theatergroundobject import SamGroundObject


def test_ground_objects_saved_before_ground_movement_are_not_in_transit() -> None:
    sam = SamGroundObject.__new__(SamGroundObject)
    sam.__setstate__({"name": "KITTEN"})
    assert sam.in_transit is False
