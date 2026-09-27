from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_TIME_RE = re.compile(r"^;TIME:(\d+)", re.MULTILINE)
_FILAMENT_RE = re.compile(r"^;Filament used:\s*([0-9.]+)", re.MULTILINE)
_LAYER_COUNT_RE = re.compile(r"^;LAYER_COUNT:(\d+)", re.MULTILINE)
_LAYER_RE = re.compile(r"^;LAYER:(-?\d+)\s*$")
_MOVE_RE = re.compile(r"^(G0|G1|G00|G01)\b(.*)$", re.IGNORECASE)
_AXIS_RE = re.compile(r"([XYZE])(-?[0-9]*\.?[0-9]+)", re.IGNORECASE)


@dataclass(frozen=True)
class GcodeInfo:
    time_seconds: int | None
    filament_meters: float | None
    layer_count: int


@dataclass(frozen=True)
class Polyline:
    type_name: str
    points: list[list[float]]


def parse_gcode_header(text: str) -> tuple[int | None, float | None]:
    time_match = _TIME_RE.search(text)
    filament_match = _FILAMENT_RE.search(text)
    seconds = int(time_match.group(1)) if time_match else None
    filament = float(filament_match.group(1)) if filament_match else None
    return seconds, filament


def gcode_info(path: Path) -> GcodeInfo:
    text = path.read_text(encoding="utf-8", errors="replace")
    seconds, filament = parse_gcode_header(text)
    count_match = _LAYER_COUNT_RE.search(text)
    if count_match:
        layer_count = int(count_match.group(1))
    else:
        layer_count = sum(1 for line in text.splitlines() if _LAYER_RE.match(line.strip()))
    return GcodeInfo(seconds, filament, layer_count)


def layer_polylines(path: Path, index: int, include_travel: bool) -> list[Polyline]:
    ranges = _layer_ranges(path)
    if index < 0 or index >= len(ranges):
        raise IndexError("layer index out of range")
    start, end = ranges[index]
    data = path.read_bytes()[start:end].decode("utf-8", errors="replace")
    return _moves_to_polylines(data, include_travel)


def _layer_ranges(path: Path) -> list[tuple[int, int]]:
    data = path.read_bytes()
    starts: list[int] = []
    offset = 0
    for line in data.splitlines(keepends=True):
        if _LAYER_RE.match(line.decode("utf-8", errors="replace").strip()):
            starts.append(offset)
        offset += len(line)
    ranges: list[tuple[int, int]] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(data)
        ranges.append((start, end))
    return ranges


def _moves_to_polylines(text: str, include_travel: bool) -> list[Polyline]:
    absolute = True
    x = y = z = 0.0
    e = 0.0
    current_type = "UNKNOWN"
    polylines: list[Polyline] = []
    active_type: str | None = None
    active_points: list[list[float]] = []

    def flush() -> None:
        nonlocal active_points
        if active_type is not None and len(active_points) >= 2:
            polylines.append(Polyline(active_type, list(active_points)))
        active_points = []

    for raw in text.splitlines():
        comment = raw.split(";", 1)[1].strip() if ";" in raw else ""
        line = raw.split(";", 1)[0].strip()
        if comment.upper().startswith("TYPE:"):
            current_type = comment.split(":", 1)[1].strip() or current_type
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("G90"):
            absolute = True
            continue
        if upper.startswith("G91"):
            absolute = False
            continue
        move = _MOVE_RE.match(line)
        if not move:
            continue
        axes = {match.group(1).upper(): float(match.group(2)) for match in _AXIS_RE.finditer(move.group(2))}
        previous = (x, y, z, e)
        if absolute:
            x = axes.get("X", x)
            y = axes.get("Y", y)
            z = axes.get("Z", z)
            e = axes.get("E", e)
        else:
            x += axes.get("X", 0.0)
            y += axes.get("Y", 0.0)
            z += axes.get("Z", 0.0)
            e += axes.get("E", 0.0)
        travel = move.group(1).upper() in {"G0", "G00"} or e <= previous[3] + 1e-9
        type_name = "TRAVEL" if travel else current_type
        if type_name == "TRAVEL" and not include_travel:
            flush()
            active_type = None
            continue
        start = [previous[0], previous[1], previous[2]]
        end = [x, y, z]
        if type_name != active_type or (active_points and active_points[-1] != start):
            flush()
            active_type = type_name
            active_points = [start, end]
        else:
            active_points.append(end)
    flush()
    return polylines
