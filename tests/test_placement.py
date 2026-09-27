from auto_slicer.meshio import Triangle
from auto_slicer.placement import footprint_of, place_group, place_single


def _part(name: str, width: float, depth: float, height: float = 2) -> tuple[str, list[Triangle]]:
    triangles = [
        (
            (0.0, 0.0, 0.0),
            (width, 0.0, 0.0),
            (0.0, depth, height),
        )
    ]
    return name, triangles


def test_single_model_is_centered_until_a_position_is_saved() -> None:
    name, triangles = _part("wall.stl", 20, 10, 5)
    part = footprint_of(name, triangles, (0, 0, 0))
    layout = place_single(part, 200, 180, 250, None)
    assert layout.error is None
    assert layout.items[0].min_x == 90
    assert layout.items[0].min_y == 85


def test_saved_position_that_leaves_the_bed_is_rejected() -> None:
    name, triangles = _part("wall.stl", 20, 10)
    part = footprint_of(name, triangles, (0, 0, 0))
    layout = place_single(part, 200, 200, 250, (190, 0))
    assert layout.error is not None
    assert layout.items[0].max_x == 210


def test_shelf_pack_leaves_a_gap_and_manual_positions_can_overlap() -> None:
    first = footprint_of(*_part("a.stl", 10, 10), (0, 0, 0))
    second = footprint_of(*_part("b.stl", 10, 10), (0, 0, 0))
    packed = place_group([first, second], 100, 100, 50, gap=5, manual=None)
    assert packed.error is None
    by_file = {item.file: item for item in packed.items}
    assert by_file["a.stl"].min_x == 0
    assert by_file["b.stl"].min_x == 15

    manual = place_group(
        [first, second],
        100,
        100,
        50,
        gap=5,
        manual={"a.stl": (0, 0), "b.stl": (0, 0)},
    )
    assert manual.error is not None
    assert "overlap" in manual.error


def test_manual_layout_is_used_instead_of_the_shelf() -> None:
    first = footprint_of(*_part("a.stl", 10, 10), (0, 0, 0))
    second = footprint_of(*_part("b.stl", 10, 10), (0, 0, 0))
    layout = place_group(
        [first, second],
        220,
        220,
        250,
        gap=5,
        manual={"a.stl": (30, 40), "b.stl": (80, 40)},
    )
    assert layout.error is None
    by_file = {item.file: item for item in layout.items}
    assert (by_file["a.stl"].min_x, by_file["a.stl"].min_y) == (30, 40)
    assert by_file["b.stl"].min_x == 80
