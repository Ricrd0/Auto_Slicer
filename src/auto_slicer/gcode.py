from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path


_TIME_RE = re.compile(r"^;TIME:(\d+)", re.MULTILINE)
_ORCA_TIME_RE = re.compile(
    r";\s*(?:estimated printing time \(normal mode\)|model printing time)\s*=\s*"
    r"(?:(\d+)\s*h\s*)?(?:(\d+)\s*m\s*)?(?:(\d+)\s*s)?",
    re.IGNORECASE,
)
_FILAMENT_RE = re.compile(r"^;Filament used:\s*([0-9.]+)", re.MULTILINE)
_ORCA_FILAMENT_MM_RE = re.compile(r";\s*filament used \[mm\]\s*=\s*([0-9.]+)", re.IGNORECASE)
_LAYER_COUNT_RE = re.compile(r"^;LAYER_COUNT:(\d+)", re.MULTILINE)
_LAYER_RE = re.compile(r"^;(?:LAYER:(-?\d+)|LAYER_CHANGE)\s*$", re.IGNORECASE)
_MOVE_RE = re.compile(r"^(G0|G1|G00|G01)\b(.*)$", re.IGNORECASE)
_AXIS_RE = re.compile(r"([XYZE])(-?[0-9]*\.?[0-9]+)", re.IGNORECASE)
_E_AXIS_RE = re.compile(r"(\bE)(-?[0-9]*\.?[0-9]+)", re.IGNORECASE)
# Klipper default: max_extrude_cross_section = 4.0 * nozzle_diameter^2
_DEFAULT_NOZZLE_MM = 0.4
_DEFAULT_FILAMENT_MM = 1.75
_MIN_TRAVEL_MM = 0.05


@dataclass(frozen=True)
class GcodeInfo:
    time_seconds: int | None
    filament_meters: float | None
    layer_count: int


@dataclass(frozen=True)
class Polyline:
    type_name: str
    points: list[list[float]]


@dataclass(frozen=True)
class ExtrusionIssue:
    """One move that would trip Klipper-style max_extrude_cross_section."""

    line_number: int
    line: str
    cross_section: float
    limit: float
    travel_mm: float
    e_mm: float

    @property
    def message(self) -> str:
        return (
            f"line {self.line_number}: extrusion cross-section "
            f"{self.cross_section:.3f}mm^2 exceeds {self.limit:.3f}mm^2 "
            f"(E={self.e_mm:g}mm over {self.travel_mm:g}mm XY) — {self.line}"
        )


def parse_gcode_header(text: str) -> tuple[int | None, float | None]:
    time_match = _TIME_RE.search(text)
    if time_match:
        seconds: int | None = int(time_match.group(1))
    else:
        orca_time = _ORCA_TIME_RE.search(text)
        if orca_time and any(orca_time.groups()):
            hours = int(orca_time.group(1) or 0)
            minutes = int(orca_time.group(2) or 0)
            secs = int(orca_time.group(3) or 0)
            seconds = hours * 3600 + minutes * 60 + secs
        else:
            seconds = None
    filament_match = _FILAMENT_RE.search(text)
    if filament_match:
        filament: float | None = float(filament_match.group(1))
    else:
        orca_filament = _ORCA_FILAMENT_MM_RE.search(text)
        filament = float(orca_filament.group(1)) / 1000.0 if orca_filament else None
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


def klipper_max_extrude_cross_section(nozzle_diameter: float = _DEFAULT_NOZZLE_MM) -> float:
    """Klipper's default ``max_extrude_cross_section`` for a nozzle diameter."""
    return 4.0 * float(nozzle_diameter) ** 2


def validate_extrusion_cross_section(
    text: str,
    *,
    filament_diameter: float = _DEFAULT_FILAMENT_MM,
    nozzle_diameter: float = _DEFAULT_NOZZLE_MM,
    max_cross_section: float | None = None,
    relative_e: bool | None = None,
    min_travel_mm: float = _MIN_TRAVEL_MM,
) -> list[ExtrusionIssue]:
    """Find XY extrusion moves whose filament cross-section exceeds the limit.

    Mirrors Klipper's check that produced
    ``Move exceeds maximum extrusion (0.825mm^2 vs 0.640mm^2)``.

    ``relative_e`` overrides M82/M83 detection when set. When neither M-code
    appears, absolute extrusion is assumed (classic Marlin / Cura default).
    """
    limit = (
        float(max_cross_section)
        if max_cross_section is not None
        else klipper_max_extrude_cross_section(nozzle_diameter)
    )
    filament_area = math.pi * (float(filament_diameter) / 2.0) ** 2
    absolute_xy = True
    e_is_relative = False if relative_e is None else bool(relative_e)
    x = y = 0.0
    e_pos = 0.0
    issues: list[ExtrusionIssue] = []

    for line_number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split(";", 1)[0].strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith("M83"):
            e_is_relative = True
            continue
        if upper.startswith("M82"):
            e_is_relative = False
            continue
        if upper.startswith("G90"):
            absolute_xy = True
            continue
        if upper.startswith("G91"):
            absolute_xy = False
            continue
        if upper.startswith("G92"):
            axes = {m.group(1).upper(): float(m.group(2)) for m in _AXIS_RE.finditer(line)}
            if "E" in axes:
                e_pos = axes["E"]
            if "X" in axes:
                x = axes["X"]
            if "Y" in axes:
                y = axes["Y"]
            continue
        move = _MOVE_RE.match(line)
        if not move:
            continue
        axes = {m.group(1).upper(): float(m.group(2)) for m in _AXIS_RE.finditer(move.group(2))}
        prev_x, prev_y, prev_e = x, y, e_pos
        if absolute_xy:
            x = axes.get("X", x)
            y = axes.get("Y", y)
        else:
            x += axes.get("X", 0.0)
            y += axes.get("Y", 0.0)
        if "E" not in axes:
            continue
        if e_is_relative:
            e_delta = axes["E"]
            e_pos = prev_e + e_delta
        else:
            e_pos = axes["E"]
            e_delta = e_pos - prev_e
        if e_delta <= 1e-9:
            continue
        travel = math.hypot(x - prev_x, y - prev_y)
        if travel < min_travel_mm:
            continue
        cross_section = e_delta * filament_area / travel
        if cross_section > limit + 1e-9:
            issues.append(
                ExtrusionIssue(
                    line_number=line_number,
                    line=line,
                    cross_section=cross_section,
                    limit=limit,
                    travel_mm=travel,
                    e_mm=e_delta,
                )
            )
    return issues


def validate_gcode_file(
    path: Path,
    *,
    filament_diameter: float = _DEFAULT_FILAMENT_MM,
    nozzle_diameter: float = _DEFAULT_NOZZLE_MM,
    max_cross_section: float | None = None,
) -> list[ExtrusionIssue]:
    """Validate a generated G-code file for Klipper extrusion cross-section limits."""
    text = path.read_text(encoding="utf-8", errors="replace")
    return validate_extrusion_cross_section(
        text,
        filament_diameter=filament_diameter,
        nozzle_diameter=nozzle_diameter,
        max_cross_section=max_cross_section,
    )


def sanitize_relative_extrusion_script(
    text: str,
    *,
    assume_relative_e: bool = False,
    filament_diameter: float = _DEFAULT_FILAMENT_MM,
    nozzle_diameter: float = _DEFAULT_NOZZLE_MM,
    max_cross_section: float | None = None,
    min_travel_mm: float = _MIN_TRAVEL_MM,
) -> str:
    """Rewrite start/end scripts that over-extrude under relative E (M83).

    Printer profiles often keep Marlin absolute primes (``E15`` then ``E30``)
    after switching to ``M83``. Under relative extrusion that second pass
    doubles the filament and trips Klipper's ``max_extrude_cross_section``.

    Fixes:
    - When the next prime E is ~2× the previous (absolute accumulation), use
      the previous amount instead.
    - Otherwise clamp E so the move stays under the Klipper limit.
    """
    if not (text or "").strip():
        return text or ""
    limit = (
        float(max_cross_section)
        if max_cross_section is not None
        else klipper_max_extrude_cross_section(nozzle_diameter)
    )
    filament_area = math.pi * (float(filament_diameter) / 2.0) ** 2
    absolute_xy = True
    e_is_relative = bool(assume_relative_e)
    x = y = 0.0
    last_prime_e: float | None = None
    out: list[str] = []

    for raw in text.splitlines():
        code, sep, comment = raw.partition(";")
        line = code.strip()
        if not line:
            out.append(raw)
            continue
        upper = line.upper()
        if upper.startswith("M83"):
            e_is_relative = True
            out.append(raw)
            continue
        if upper.startswith("M82"):
            e_is_relative = False
            last_prime_e = None
            out.append(raw)
            continue
        if upper.startswith("G90"):
            absolute_xy = True
            out.append(raw)
            continue
        if upper.startswith("G91"):
            absolute_xy = False
            out.append(raw)
            continue
        if upper.startswith("G92"):
            axes = {m.group(1).upper(): float(m.group(2)) for m in _AXIS_RE.finditer(line)}
            if "E" in axes:
                last_prime_e = None
            if "X" in axes:
                x = axes["X"]
            if "Y" in axes:
                y = axes["Y"]
            out.append(raw)
            continue
        move = _MOVE_RE.match(line)
        if not move:
            out.append(raw)
            continue
        axes = {m.group(1).upper(): float(m.group(2)) for m in _AXIS_RE.finditer(move.group(2))}
        prev_x, prev_y = x, y
        if absolute_xy:
            x = axes.get("X", x)
            y = axes.get("Y", y)
        else:
            x += axes.get("X", 0.0)
            y += axes.get("Y", 0.0)
        if "E" not in axes or not e_is_relative:
            out.append(raw)
            continue
        e_delta = axes["E"]
        if e_delta <= 1e-9:
            out.append(raw)
            continue
        travel = math.hypot(x - prev_x, y - prev_y)
        if travel < min_travel_mm:
            last_prime_e = None
            out.append(raw)
            continue

        fixed = e_delta
        if last_prime_e is not None and last_prime_e > 0:
            # Classic absolute second pass written into a relative script.
            if abs(e_delta - 2.0 * last_prime_e) <= max(0.05, 0.02 * e_delta):
                fixed = last_prime_e
        max_e = limit * travel / filament_area
        if fixed > max_e + 1e-9:
            fixed = max_e * 0.95
        if abs(fixed - e_delta) > 1e-9:
            body = _replace_e_value(line, fixed)
            if sep:
                note = comment.lstrip()
                out.append(f"{body} ; {note}" if note else f"{body} ;")
            else:
                out.append(body)
            last_prime_e = fixed
        else:
            out.append(raw)
            last_prime_e = e_delta
    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result


def _replace_e_value(code: str, new_e: float) -> str:
    """Replace the first E axis value in a G-code command (comments already stripped)."""
    if float(new_e).is_integer():
        rendered = str(int(new_e))
    else:
        rendered = format(float(new_e), "g")
    return _E_AXIS_RE.sub(rf"\g<1>{rendered}", code, count=1)


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
