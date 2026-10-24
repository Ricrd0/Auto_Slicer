from __future__ import annotations

import io
import json
import re
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.engine import EngineError, run_slice
from auto_slicer.machine_sync import orca_filament_overrides, patch_orca_machine
from auto_slicer.meshio import (
    bounds,
    load_model,
    read_3mf_placements,
    transform_mesh,
    transform_z_degrees,
    write_stl,
)
from auto_slicer.orca_config import (
    active_orca_dir,
    apply_user_orca_machine_override,
    area_is_center_zero,
    build_orca_arrange_command,
    compatible_printer_names,
    generic_pla,
    locate_orca,
    process_inherits_for_machine,
    write_filament_profile,
    write_process_profile,
    _bed_size,
)
from auto_slicer.paths import safe_relative, safe_segment
from auto_slicer.placement import Footprint, align_longest_to_x, place_group
from auto_slicer.settings_schema import SliceSettings


def arrange_group(
    input_dir: Path,
    files: list[str],
    printer: PrinterProfile,
    settings: SliceSettings,
    poses: dict[str, dict[str, Any]],
    group_name: str,
    machine: dict[str, object] | None = None,
    *,
    use_idex_zone: bool = False,
) -> tuple[dict[str, dict[str, float]], Path]:
    """Auto-arrange group parts with Orca and save a reusable project 3MF.

    Returns front-left ``manual_layout`` positions and the written 3MF path.
    Parts are never merged into a single object.
    """
    if not files:
        raise ValueError("group has no models")
    binary, library_path = locate_orca()
    if binary is None:
        raise EngineError("OrcaSlicer was not found. Build the Docker image or set ORCA_SLICER.")
    config_dir = active_orca_dir()
    if config_dir is None:
        raise EngineError("Orca configuration was not found. Mount %APPDATA%\\OrcaSlicer or set ORCA_CONFIG.")
    if printer.definition_path is None:
        raise EngineError(printer.error or "Orca machine profile is missing")

    destination = arranged_3mf_path(input_dir, files, group_name)
    with tempfile.TemporaryDirectory(prefix="auto-slicer-arrange-") as temporary:
        root = Path(temporary)
        meshes = _posed_meshes(input_dir, files, settings, poses, root)
        process_path = root / "process.json"
        filament_path = root / "filament.json"
        machine_path = Path(printer.definition_path)
        source: dict[str, object] = {}
        if machine_path.is_file():
            loaded = json.loads(machine_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                source = apply_user_orca_machine_override(loaded, config_dir)
        compatible = compatible_printer_names(source, printer.name)
        write_process_profile(
            process_path,
            settings,
            _arrange_process_extras(settings),
            inherits=process_inherits_for_machine(source),
            compatible_printers=compatible,
        )
        machine_json = dict(source)
        if machine is not None:
            machine_json = patch_orca_machine(
                machine_json, machine, use_idex_zone=use_idex_zone
            )
        machine_json["printer_structure"] = "i3" if arrange_uses_y_alignment(settings) else "undefine"
        patched = root / "machine.json"
        patched.write_text(json.dumps(machine_json, indent=2), encoding="utf-8")
        arranged_printer = replace(printer, definition_path=str(patched))
        filaments: Path | list[Path]
        if machine is not None:
            write_filament_profile(
                filament_path,
                orca_filament_overrides(machine, tool=0),
                name=str(machine.get("filament_name") or "Generic PLA"),
                inherits=str(machine.get("filament_inherits") or "fdm_filament_pla"),
                compatible_printers=compatible,
            )
            filaments = [filament_path]
            if int(machine.get("extruder_count") or printer.extruder_count or 1) > 1:
                filament_t1 = root / "filament_t1.json"
                write_filament_profile(
                    filament_t1,
                    orca_filament_overrides(machine, tool=1),
                    name=str(
                        machine.get("filament_name_t1") or machine.get("filament_name") or "Generic PLA"
                    ),
                    inherits=str(
                        machine.get("filament_inherits_t1")
                        or machine.get("filament_inherits")
                        or "fdm_filament_pla"
                    ),
                    compatible_printers=compatible,
                )
                filaments.append(filament_t1)
        else:
            found = generic_pla(config_dir)
            if found is None:
                write_filament_profile(filament_path, compatible_printers=compatible)
                filaments = filament_path
            else:
                filaments = found
        export_path = root / "arranged.3mf"
        command = build_orca_arrange_command(
            binary,
            arranged_printer,
            meshes,
            export_path,
            config_dir,
            process_path,
            filaments,
            settings,
        )
        code, tail = run_slice(command, library_path)
        if code != 0 or not export_path.is_file() or export_path.stat().st_size == 0:
            raise EngineError(tail or f"OrcaSlicer arrange exited with status {code}")
        payload = export_path.read_bytes()
        if _occupied_plate_count(payload) > 1:
            raise EngineError("arranged parts did not fit on one plate")
        bed = _bed_size(machine_json.get("printable_area"))
        if bed is not None:
            payload = _space_arranged_project(
                payload,
                settings,
                bed[0],
                bed[1],
                area_is_center_zero(machine_json.get("printable_area")),
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        layout = layout_from_arranged_3mf(payload, files)
    return layout, destination


def arrange_uses_y_alignment(settings: SliceSettings) -> bool:
    """Whether a group should turn each part so its long edge follows Y.

    Unchecked, the long edge stays on X. Checked, Orca's arrange yaws it onto Y.
    """
    return settings.orca_arrange_align_y


def _space_arranged_project(
    data: bytes,
    settings: SliceSettings,
    bed_width: float,
    bed_depth: float,
    center_zero: bool,
) -> bytes:
    """Move arranged parts so the gap between them is the group offset.

    Orca's CLI packs parts until they touch. Pack the arranged footprints with
    the offset and shift the build-item transforms so the saved project matches.
    """
    if bed_width <= 0 or bed_depth <= 0:
        return data
    parts = read_3mf_placements(data)
    footprints: list[Footprint] = []
    current: dict[str, tuple[float, float]] = {}
    for name, mesh, _transform in parts:
        low, high = bounds(mesh)
        current[name] = (low[0], low[1])
        footprints.append(
            Footprint(
                file=name,
                width=max(high[0] - low[0], 0.0),
                depth=max(high[1] - low[1], 0.0),
                height=max(high[2] - low[2], 0.0),
                rotation=(0.0, 0.0, 0.0),
            )
        )
    layout = place_group(
        footprints,
        bed_width,
        bed_depth,
        max((part.height for part in footprints), default=1.0),
        settings.orca_arrange_spacing,
        None,
    )
    if layout.error or not layout.items:
        return data
    targets = {item.file: (item.min_x, item.min_y) for item in layout.items}
    if center_zero:
        targets = {
            name: (x - bed_width / 2.0, y - bed_depth / 2.0) for name, (x, y) in targets.items()
        }
    deltas: list[tuple[float, float]] = []
    for name, _mesh, _transform in parts:
        target = targets.get(name)
        origin = current.get(name)
        if target is None or origin is None:
            return data
        deltas.append((target[0] - origin[0], target[1] - origin[1]))
    if all(abs(dx) < 1e-6 and abs(dy) < 1e-6 for dx, dy in deltas):
        return data
    return _shift_build_items(data, deltas)


def _shift_build_items(data: bytes, deltas: list[tuple[float, float]]) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        model_names = [name for name in archive.namelist() if name.lower().endswith("3d/3dmodel.model")]
        if not model_names:
            return data
        model_name = model_names[0]
        model = archive.read(model_name).decode("utf-8")
        entries = [(name, archive.read(name)) for name in archive.namelist() if name != model_name]
    pattern = re.compile(r'(<item\b[^>]*\btransform=")([^"]+)(")')
    matches = list(pattern.finditer(model))
    if len(matches) != len(deltas):
        return data
    pieces: list[str] = []
    cursor = 0
    for match, (dx, dy) in zip(matches, deltas, strict=True):
        numbers = [float(part) for part in match.group(2).split()]
        if len(numbers) != 12:
            return data
        numbers[9] += dx
        numbers[10] += dy
        pieces.append(model[cursor : match.start()])
        pieces.append(match.group(1))
        pieces.append(" ".join(format(number, ".6f") for number in numbers))
        pieces.append(match.group(3))
        cursor = match.end()
    pieces.append(model[cursor:])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries:
            archive.writestr(name, content)
        archive.writestr(model_name, "".join(pieces).encode("utf-8"))
    return buffer.getvalue()


def _occupied_plate_count(data: bytes) -> int:
    """How many plates in an Orca project actually hold a model."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if "Metadata/model_settings.config" not in archive.namelist():
                return 1
            root = ET.fromstring(archive.read("Metadata/model_settings.config"))
    except (zipfile.BadZipFile, ET.ParseError, KeyError):
        return 1
    return sum(1 for plate in root.findall("plate") if plate.find("model_instance") is not None)


def arranged_3mf_path(input_dir: Path, files: list[str], group_name: str) -> Path:
    folder = _group_directory(input_dir, files)
    return folder / f"{safe_segment(group_name)}.3mf"


def layout_from_arranged_3mf(data: bytes, files: list[str]) -> dict[str, dict[str, float]]:
    """Front-left positions plus the Z rotation Orca applied while arranging.

    Align-to-Y / allow-rotations can spin parts on the plate. Positions alone
    are not enough — slicing must reuse that Z rotation or footprints disagree
    with the saved project and look like overlaps / out-of-bed errors.
    A project that spilled onto a second plate is rejected so the caller can
    arrange again on a larger bed instead of keeping the plate-to-plate gap.
    """
    if _occupied_plate_count(data) > 1:
        raise EngineError("arranged parts did not fit on one plate")
    parts = read_3mf_placements(data)
    by_base = {Path(relative).name.lower(): relative for relative in files}
    positions: dict[str, dict[str, float]] = {}
    unused = list(files)
    for name, mesh, transform in parts:
        base = Path(name).name.lower()
        relative = by_base.get(base)
        if relative is None and unused:
            relative = unused.pop(0)
        elif relative in unused:
            unused.remove(relative)
        if relative is None:
            continue
        low, _high = bounds(mesh)
        positions[relative] = {
            "x": float(low[0]),
            "y": float(low[1]),
            "rotation_z": float(transform_z_degrees(transform)),
        }
    missing = [relative for relative in files if relative not in positions]
    if missing:
        raise EngineError("arranged 3MF is missing " + ", ".join(missing))
    return positions


def _posed_meshes(
    input_dir: Path,
    files: list[str],
    settings: SliceSettings,
    poses: dict[str, dict[str, Any]],
    root: Path,
) -> list[Path]:
    meshes: list[Path] = []
    used_names: set[str] = set()
    for relative in files:
        source = input_dir / safe_relative(relative)
        if not source.is_file():
            raise FileNotFoundError(relative)
        pose = poses.get(relative)
        triangles = load_model(source.name, source.read_bytes())
        scale = _pose_scale(pose)
        rotation = _resolved_rotation(settings, pose, triangles, scale)
        posed = transform_mesh(
            triangles,
            rotation,
            0.0,
            0.0,
            1.0,
            1.0,
            "front_left",
            z=_pose_z(pose),
            scale=scale,
        )
        stem = safe_segment(source.stem)
        candidate = f"{stem}.stl"
        suffix = 2
        while candidate.lower() in used_names:
            candidate = f"{stem}_{suffix}.stl"
            suffix += 1
        used_names.add(candidate.lower())
        destination = root / candidate
        destination.write_bytes(write_stl(posed))
        meshes.append(destination)
    return meshes


def _arrange_process_extras(settings: SliceSettings) -> dict[str, str]:
    """Turn the group offset into the gap Orca leaves between arranged parts.

    Orca's arrange has no distance flag. With none set, it inflates each part by
    the process brim, so the gap is twice that brim (an 8 mm brim leaves 16 mm).
    Half the requested offset is written as an arrange-only brim, which makes
    the gap equal the offset. Skirt and raft are cleared so they do not add
    space. Slicing still uses the real adhesion settings.
    """
    half = settings.orca_arrange_spacing / 2.0
    return {
        "brim_type": "outer_only" if half > 0 else "no_brim",
        "brim_width": _number(half),
        "skirt_loops": "0",
        "raft_layers": "0",
    }


def _group_directory(input_dir: Path, files: list[str]) -> Path:
    parents = [(input_dir / safe_relative(relative)).parent for relative in files]
    common = parents[0]
    for parent in parents[1:]:
        while common != parent and common not in parent.parents:
            if common.parent == common:
                return input_dir
            common = common.parent
        while parent != common and parent not in common.parents:
            parent = parent.parent
    try:
        common.relative_to(input_dir)
    except ValueError:
        return input_dir
    return common


def _effective_rotation(
    settings: SliceSettings, pose: dict[str, Any] | None
) -> tuple[float, float, float]:
    if pose and isinstance(pose.get("rotation"), list) and len(pose["rotation"]) == 3:
        return (float(pose["rotation"][0]), float(pose["rotation"][1]), float(pose["rotation"][2]))
    return (settings.rotation_x, settings.rotation_y, settings.rotation_z)


def _resolved_rotation(
    settings: SliceSettings,
    pose: dict[str, Any] | None,
    triangles: list,
    scale: float,
) -> tuple[float, float, float]:
    base = _effective_rotation(settings, pose)
    if pose and isinstance(pose.get("rotation"), list) and len(pose["rotation"]) == 3:
        return base
    if settings.orca_arrange_align_y:
        return base
    return align_longest_to_x(triangles, base, scale)


def _pose_z(pose: dict[str, Any] | None) -> float:
    if not pose:
        return 0.0
    if pose.get("z") is not None:
        return float(pose["z"])
    position = pose.get("position")
    if isinstance(position, list) and len(position) >= 3:
        return float(position[2])
    return 0.0


def _pose_scale(pose: dict[str, Any] | None) -> float:
    if not pose or pose.get("scale") is None:
        return 1.0
    scale = float(pose["scale"])
    if scale <= 0:
        raise ValueError("scale must be positive")
    return scale


def _number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return format(value, "g")
