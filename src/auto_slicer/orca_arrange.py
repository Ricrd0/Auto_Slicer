from __future__ import annotations

import json
import tempfile
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
    build_orca_arrange_command,
    generic_pla,
    locate_orca,
    process_inherits_for_machine,
    write_filament_profile,
    write_process_profile,
)
from auto_slicer.paths import safe_relative, safe_segment
from auto_slicer.settings_schema import SliceSettings


def arrange_group(
    input_dir: Path,
    files: list[str],
    printer: PrinterProfile,
    settings: SliceSettings,
    poses: dict[str, dict[str, Any]],
    group_name: str,
    machine: dict[str, object] | None = None,
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
        machine_name = str(source.get("name") or printer.name)
        compatible = [machine_name] if machine_name else None
        write_process_profile(
            process_path,
            settings,
            _arrange_process_extras(settings),
            inherits=process_inherits_for_machine(source),
            compatible_printers=compatible,
        )
        machine_json = dict(source)
        if machine is not None:
            machine_json = patch_orca_machine(machine_json, machine)
        machine_json["printer_structure"] = "i3" if settings.orca_arrange_align_y else "undefine"
        patched = root / "machine.json"
        patched.write_text(json.dumps(machine_json, indent=2), encoding="utf-8")
        arranged_printer = replace(printer, definition_path=str(patched))
        if machine is not None:
            write_filament_profile(
                filament_path,
                orca_filament_overrides(machine),
                name=str(machine.get("filament_name") or "Generic PLA"),
                inherits=str(machine.get("filament_inherits") or "fdm_filament_pla"),
                compatible_printers=compatible,
            )
            filament: Path = filament_path
        else:
            found = generic_pla(config_dir)
            if found is None:
                write_filament_profile(filament_path, compatible_printers=compatible)
                filament = filament_path
            else:
                filament = found
        export_path = root / "arranged.3mf"
        command = build_orca_arrange_command(
            binary,
            arranged_printer,
            meshes,
            export_path,
            config_dir,
            process_path,
            filament,
            settings,
        )
        code, tail = run_slice(command, library_path)
        if code != 0 or not export_path.is_file() or export_path.stat().st_size == 0:
            raise EngineError(tail or f"OrcaSlicer arrange exited with status {code}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(export_path.read_bytes())
        layout = layout_from_arranged_3mf(destination.read_bytes(), files)
    return layout, destination


def arranged_3mf_path(input_dir: Path, files: list[str], group_name: str) -> Path:
    folder = _group_directory(input_dir, files)
    return folder / f"{safe_segment(group_name)}.3mf"


def layout_from_arranged_3mf(data: bytes, files: list[str]) -> dict[str, dict[str, float]]:
    """Front-left positions plus the Z rotation Orca applied while arranging.

    Align-to-Y / allow-rotations can spin parts on the plate. Positions alone
    are not enough — slicing must reuse that Z rotation or footprints disagree
    with the saved project and look like overlaps / out-of-bed errors.
    """
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
        rotation = _effective_rotation(settings, pose)
        triangles = load_model(source.name, source.read_bytes())
        posed = transform_mesh(
            triangles,
            rotation,
            0.0,
            0.0,
            1.0,
            1.0,
            "front_left",
            z=_pose_z(pose),
            scale=_pose_scale(pose),
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
    """Map Spacing onto Orca Auto inflation (brim width when distance is Auto)."""
    if settings.orca_arrange_spacing > 0:
        return {
            "brim_type": "outer_only",
            "brim_width": _number(settings.orca_arrange_spacing),
        }
    return {}


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
