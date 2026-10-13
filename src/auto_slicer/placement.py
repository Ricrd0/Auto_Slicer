from __future__ import annotations

from dataclasses import dataclass, replace

from auto_slicer.meshio import Triangle, bounds, rotate_xyz


@dataclass(frozen=True)
class Footprint:
    file: str
    width: float
    depth: float
    height: float
    rotation: tuple[float, float, float]
    z: float = 0.0
    scale: float = 1.0


@dataclass(frozen=True)
class PlacedPart:
    file: str
    min_x: float
    min_y: float
    max_x: float
    max_y: float
    size_z: float
    rotation: tuple[float, float, float]
    z: float = 0.0
    scale: float = 1.0


@dataclass(frozen=True)
class Layout:
    bed_width: float
    bed_depth: float
    bed_height: float
    items: tuple[PlacedPart, ...]
    error: str | None
    status: str | None = None


@dataclass(frozen=True)
class PlacementZone:
    """Axis-aligned printable zone in front-left bed coordinates."""

    width: float
    depth: float
    origin_x: float = 0.0
    origin_y: float = 0.0

    @property
    def center_x(self) -> float:
        return self.origin_x + self.width / 2.0

    @property
    def center_y(self) -> float:
        return self.origin_y + self.depth / 2.0


def idex_zone(bed_width: float, bed_depth: float, offset_x: float) -> PlacementZone:
    """Usable copy/mirror area: width = 2 * |offset_x|, full bed depth, origin at front-left."""
    del bed_width  # full bed width is not the zone width
    return PlacementZone(width=abs(offset_x) * 2.0, depth=bed_depth, origin_x=0.0, origin_y=0.0)


def rotated_bounds(
    triangles: list[Triangle], rotation: tuple[float, float, float], scale: float = 1.0
) -> tuple[float, float, float, float, float, float]:
    if not triangles:
        raise ValueError("mesh is empty")
    if scale <= 0:
        raise ValueError("scale must be positive")
    rotated = [
        tuple(
            rotate_xyz(
                (vertex[0] * scale, vertex[1] * scale, vertex[2] * scale),
                rotation[0],
                rotation[1],
                rotation[2],
            )
            for vertex in tri
        )
        for tri in triangles
    ]
    low, high = bounds(rotated)
    return low[0], low[1], low[2], high[0], high[1], high[2]


def footprint_of(
    file: str, triangles: list[Triangle], rotation: tuple[float, float, float], scale: float = 1.0
) -> Footprint:
    low_x, low_y, low_z, high_x, high_y, high_z = rotated_bounds(triangles, rotation, scale)
    return Footprint(
        file=file,
        width=high_x - low_x,
        depth=high_y - low_y,
        height=high_z - low_z,
        rotation=rotation,
        scale=scale,
    )


def center_position(
    part: Footprint,
    bed_width: float,
    bed_depth: float,
    zone: PlacementZone | None = None,
) -> tuple[float, float]:
    if zone is None:
        return (bed_width - part.width) / 2.0, (bed_depth - part.depth) / 2.0
    return zone.origin_x + (zone.width - part.width) / 2.0, zone.origin_y + (zone.depth - part.depth) / 2.0


def place_single(
    part: Footprint,
    bed_width: float,
    bed_depth: float,
    bed_height: float,
    position: tuple[float, float] | None,
    zone: PlacementZone | None = None,
) -> Layout:
    if position is not None:
        min_x, min_y = position
        placed = _placed(part, min_x, min_y)
        error = _fit_error([placed], bed_width, bed_depth, bed_height, gap=0)
        return Layout(bed_width, bed_depth, bed_height, (placed,), error)
    if zone is not None:
        min_x, min_y = center_position(part, bed_width, bed_depth, zone)
        placed = _placed(part, min_x, min_y)
        zone_error = _fit_error(
            [placed],
            zone.origin_x + zone.width,
            zone.origin_y + zone.depth,
            bed_height,
            gap=0,
            origin_x=zone.origin_x,
            origin_y=zone.origin_y,
        )
        if zone_error is None:
            return Layout(bed_width, bed_depth, bed_height, (placed,), None)
        # Fall back to full bed centre with a status note.
        min_x, min_y = center_position(part, bed_width, bed_depth)
        placed = _placed(part, min_x, min_y)
        error = _fit_error([placed], bed_width, bed_depth, bed_height, gap=0)
        return Layout(
            bed_width,
            bed_depth,
            bed_height,
            (placed,),
            error,
            "IDEX offset zone too small; recentred on full bed",
        )
    min_x, min_y = center_position(part, bed_width, bed_depth)
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
    zone: PlacementZone | None = None,
) -> Layout:
    if not parts:
        return Layout(bed_width, bed_depth, bed_height, (), "group has no models")
    pack_width = zone.width if zone is not None else bed_width
    pack_depth = zone.depth if zone is not None else bed_depth
    if manual is None:
        placed, pack_error = shelf_pack(parts, pack_width, pack_depth, gap)
        status = None
        if pack_error is None:
            placed = center_group(placed, bed_width, bed_depth, zone)
            error = _fit_error(
                placed,
                (zone.origin_x + zone.width) if zone else bed_width,
                (zone.origin_y + zone.depth) if zone else bed_depth,
                bed_height,
                gap,
                origin_x=zone.origin_x if zone else 0.0,
                origin_y=zone.origin_y if zone else 0.0,
            )
        elif zone is not None:
            # Retry on the full bed when the IDEX zone cannot hold the pack.
            placed, pack_error = shelf_pack(parts, bed_width, bed_depth, gap)
            if pack_error is None:
                placed = center_group(placed, bed_width, bed_depth)
            error = pack_error or _fit_error(placed, bed_width, bed_depth, bed_height, gap)
            status = "IDEX offset zone too small; recentred on full bed"
        else:
            error = pack_error or _fit_error(placed, bed_width, bed_depth, bed_height, gap)
        return Layout(bed_width, bed_depth, bed_height, tuple(placed), error, status)
    placed = []
    missing = []
    for part in parts:
        if part.file not in manual:
            missing.append(part.file)
            continue
        min_x, min_y = manual[part.file]
        placed.append(_placed(part, min_x, min_y))
    error = None
    status = None
    if missing:
        error = "manual layout is missing " + ", ".join(missing)
    else:
        # Keep relative spacing from arrange/manual edits, but centre the whole
        # group on this printer's bed (or IDEX zone) the same way a single STL is.
        placed = center_group(placed, bed_width, bed_depth, zone)
        if zone is not None:
            zone_error = _fit_error(
                placed,
                zone.origin_x + zone.width,
                zone.origin_y + zone.depth,
                bed_height,
                gap,
                origin_x=zone.origin_x,
                origin_y=zone.origin_y,
            )
            if zone_error is not None:
                placed = center_group(placed, bed_width, bed_depth)
                error = _fit_error(placed, bed_width, bed_depth, bed_height, gap)
                status = "IDEX offset zone too small; recentred on full bed"
            else:
                error = None
        else:
            error = _fit_error(placed, bed_width, bed_depth, bed_height, gap)
    return Layout(bed_width, bed_depth, bed_height, tuple(placed), error, status)


def center_group(
    placed: list[PlacedPart],
    bed_width: float,
    bed_depth: float,
    zone: PlacementZone | None = None,
) -> list[PlacedPart]:
    """Translate placed parts so their combined footprint is bed- or zone-centred."""
    if not placed:
        return placed
    min_x = min(part.min_x for part in placed)
    min_y = min(part.min_y for part in placed)
    max_x = max(part.max_x for part in placed)
    max_y = max(part.max_y for part in placed)
    if zone is None:
        dx = (bed_width - (max_x - min_x)) / 2.0 - min_x
        dy = (bed_depth - (max_y - min_y)) / 2.0 - min_y
    else:
        dx = zone.origin_x + (zone.width - (max_x - min_x)) / 2.0 - min_x
        dy = zone.origin_y + (zone.depth - (max_y - min_y)) / 2.0 - min_y
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return placed
    return [
        replace(
            part,
            min_x=part.min_x + dx,
            min_y=part.min_y + dy,
            max_x=part.max_x + dx,
            max_y=part.max_y + dy,
        )
        for part in placed
    ]


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
        z=part.z,
        scale=part.scale,
    )


def _fit_error(
    placed: list[PlacedPart],
    bed_width: float,
    bed_depth: float,
    bed_height: float,
    gap: float,
    *,
    origin_x: float = 0.0,
    origin_y: float = 0.0,
) -> str | None:
    for part in placed:
        if part.size_z > bed_height:
            return f"{part.file} is taller than the printer"
        if (
            part.min_x < origin_x - 1e-6
            or part.min_y < origin_y - 1e-6
            or part.max_x > bed_width + 1e-6
            or part.max_y > bed_depth + 1e-6
        ):
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
