from __future__ import annotations

from dataclasses import dataclass

from auto_slicer.meshio import Triangle, bounds, rotate_xyz


@dataclass(frozen=True)
class Footprint:
    file: str
    width: float
    depth: float
    height: float
    rotation: tuple[float, float, float]


@dataclass(frozen=True)
class PlacedPart:
    file: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    size_z: float
    rotation: tuple[float, float, float]


@dataclass(frozen=True)
class Layout:
    bed_width: float
    bed_depth: float
    bed_height: float
    items: tuple[PlacedPart, ...]
    error: str | None


def rotated_bounds(
    triangles: list[Triangle], rotation: tuple[float, float, float]
) -> tuple[float, float, float, float, float, float]:
    if not triangles:
        raise ValueError("mesh is empty")
    rotated = [
        tuple(rotate_xyz(vertex, rotation[0], rotation[1], rotation[2]) for vertex in tri)
        for tri in triangles
    ]
    low, high = bounds(rotated)
    return low[0], low[1], low[2], high[0], high[1], high[2]


def footprint_of(file: str, triangles: list[Triangle], rotation: tuple[float, float, float]) -> Footprint:
    low_x, low_y, low_z, high_x, high_y, high_z = rotated_bounds(triangles, rotation)
    return Footprint(
        file=file,
        width=high_x - low_x,
        depth=high_y - low_y,
        height=high_z - low_z,
        rotation=rotation,
    )


def center_position(part: Footprint, bed_width: float, bed_depth: float) -> tuple[float, float]:
    return (bed_width - part.width) / 2.0, (bed_depth - part.depth) / 2.0


def place_single(
    part: Footprint,
    bed_width: float,
    bed_depth: float,
    bed_height: float,
    position: tuple[float, float] | None,
) -> Layout:
    min_x, min_y = position if position is not None else center_position(part, bed_width, bed_depth)
    placed = _placed(part, min_x, min_y)
    error = _fit_error([placed], bed_width, bed_depth, bed_height, gap=0)
    return Layout(bed_width, bed_depth, bed_height, (placed,), error)


def place_group(
    parts: list[Footprint],
    bed_width: float,
    bed_depth: float,
    bed_height: float,
    gap: float,
    manual: dict[str, tuple[float, float]] | None,
) -> Layout:
    if not parts:
        return Layout(bed_width, bed_depth, bed_height, (), "group has no models")
    if manual is None:
        placed, pack_error = shelf_pack(parts, bed_width, bed_depth, gap)
        error = pack_error or _fit_error(placed, bed_width, bed_depth, bed_height, gap)
        return Layout(bed_width, bed_depth, bed_height, tuple(placed), error)
    placed = []
    missing = []
    for part in parts:
        if part.file not in manual:
            missing.append(part.file)
            continue
        min_x, min_y = manual[part.file]
        placed.append(_placed(part, min_x, min_y))
    error = None
    if missing:
        error = "manual layout is missing " + ", ".join(missing)
    else:
        error = _fit_error(placed, bed_width, bed_depth, bed_height, gap)
    return Layout(bed_width, bed_depth, bed_height, tuple(placed), error)


def shelf_pack(
    parts: list[Footprint], bed_width: float, bed_depth: float, gap: float
) -> tuple[list[PlacedPart], str | None]:
    ordered = sorted(parts, key=lambda part: part.depth, reverse=True)
    x = 0.0
    y = 0.0
    row_depth = 0.0
    placed: list[PlacedPart] = []
    for part in ordered:
        if part.width > bed_width or part.depth > bed_depth:
            return placed + [_placed(part, 0.0, 0.0)], f"{part.file} does not fit the bed"
        if x > 0 and x + part.width > bed_width:
            y += row_depth + gap
            x = 0.0
            row_depth = 0.0
        if y + part.depth > bed_depth:
            return placed + [_placed(part, x, y)], f"{part.file} does not fit the bed"
        placed.append(_placed(part, x, y))
        row_depth = max(row_depth, part.depth)
        x += part.width + gap
    return placed, None


def _placed(part: Footprint, min_x: float, min_y: float) -> PlacedPart:
    return PlacedPart(
        file=part.file,
        min_x=min_x,
        min_y=min_y,
        max_x=min_x + part.width,
        max_y=min_y + part.depth,
        size_z=part.height,
        rotation=part.rotation,
    )


def _fit_error(
    placed: list[PlacedPart],
    bed_width: float,
    bed_depth: float,
    bed_height: float,
    gap: float,
) -> str | None:
    for part in placed:
        if part.size_z > bed_height:
            return f"{part.file} is taller than the printer"
        if part.min_x < -1e-6 or part.min_y < -1e-6 or part.max_x > bed_width + 1e-6 or part.max_y > bed_depth + 1e-6:
            return f"{part.file} is outside the bed"
    for index, left in enumerate(placed):
        for right in placed[index + 1 :]:
            if not _separated(left, right, gap):
                return f"{left.file} overlaps {right.file}"
    return None


def _separated(left: PlacedPart, right: PlacedPart, gap: float) -> bool:
    return (
        left.max_x + gap <= right.min_x + 1e-6
        or right.max_x + gap <= left.min_x + 1e-6
        or left.max_y + gap <= right.min_y + 1e-6
        or right.max_y + gap <= left.min_y + 1e-6
    )
