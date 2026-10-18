from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from auto_slicer.cura_config import PrinterProfile, active_config_dir
from auto_slicer.engine import EngineError, build_slice_command, locate_cura, run_slice
from auto_slicer.gcode import parse_gcode_header, validate_gcode_file
from auto_slicer.machine_sync import (
    cura_extruder_temperature_overrides,
    cura_machine_overrides,
    idex_offset_active,
    orca_filament_overrides,
    orca_process_extras,
    patch_orca_machine,
)
from auto_slicer.meshio import Triangle, load_model, transform_mesh, write_stl
from auto_slicer.orca_arrange import arrange_group, layout_from_arranged_3mf
from auto_slicer.orca_config import (
    active_orca_dir,
    apply_user_orca_machine_override,
    build_orca_command,
    extract_plate_gcode,
    generic_pla,
    locate_orca,
    orca_origin,
    process_inherits_for_machine,
    write_assemble_list,
    write_filament_profile,
    write_process_profile,
)
from auto_slicer.paths import DataPaths, safe_relative, safe_segment
from auto_slicer.placement import (
    Footprint,
    Layout,
    PlacementZone,
    align_longest_to_x,
    footprint_of,
    idex_zone,
    place_group,
    place_single,
)
from auto_slicer.settings_schema import SliceSettings, pack_gap


@dataclass
class PreparedSlice:
    layout: Layout
    meshes: list[Path]
    temp_dir: tempfile.TemporaryDirectory[str]


def list_models(input_dir: Path) -> list[dict[str, object]]:
    models: list[dict[str, object]] = []
    if not input_dir.is_dir():
        return models
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".stl", ".3mf"}:
            continue
        relative = path.relative_to(input_dir).as_posix()
        models.append(
            {
                "path": relative,
                "name": path.name,
                "suffix": path.suffix.lower(),
                "size": path.stat().st_size,
            }
        )
    return models


def read_model_bytes(input_dir: Path, relative: str) -> bytes:
    path = _model_path(input_dir, relative)
    return path.read_bytes()


def model_stl_bytes(input_dir: Path, relative: str) -> bytes:
    path = _model_path(input_dir, relative)
    triangles = load_model(path.name, path.read_bytes())
    return write_stl(triangles)


def effective_rotation(settings: SliceSettings, pose: dict[str, Any] | None) -> tuple[float, float, float]:
    if pose and isinstance(pose.get("rotation"), list) and len(pose["rotation"]) == 3:
        return (float(pose["rotation"][0]), float(pose["rotation"][1]), float(pose["rotation"][2]))
    return (settings.rotation_x, settings.rotation_y, settings.rotation_z)


def resolved_rotation(
    settings: SliceSettings,
    pose: dict[str, Any] | None,
    triangles: list[Triangle],
    scale: float = 1.0,
) -> tuple[float, float, float]:
    """Shared / per-part rotation, optionally auto-aligned longest-edge-to-X."""
    base = effective_rotation(settings, pose)
    # A saved part rotation is an explicit choice — do not re-twist it.
    if pose and isinstance(pose.get("rotation"), list) and len(pose["rotation"]) == 3:
        return base
    if not settings.align_longest_to_x:
        return base
    return align_longest_to_x(triangles, base, scale)


def saved_position(pose: dict[str, Any] | None) -> tuple[float, float] | None:
    if not pose or not isinstance(pose.get("position"), list) or len(pose["position"]) < 2:
        return None
    return float(pose["position"][0]), float(pose["position"][1])


def pose_z(pose: dict[str, Any] | None) -> float:
    if not pose:
        return 0.0
    if pose.get("z") is not None:
        return float(pose["z"])
    position = pose.get("position")
    if isinstance(position, list) and len(position) >= 3:
        return float(position[2])
    return 0.0


def pose_scale(pose: dict[str, Any] | None) -> float:
    if not pose or pose.get("scale") is None:
        return 1.0
    scale = float(pose["scale"])
    if scale <= 0:
        raise ValueError("scale must be positive")
    return scale


def layout_for_model(
    input_dir: Path,
    relative: str,
    printer: PrinterProfile,
    settings: SliceSettings,
    pose: dict[str, Any] | None,
    machine: dict[str, object] | None = None,
) -> Layout:
    part = replace(
        _oriented_footprint(input_dir, relative, settings, pose),
        z=pose_z(pose),
    )
    return place_single(
        part,
        float(printer.machine_width or 0),
        float(printer.machine_depth or 0),
        float(printer.machine_height or 0),
        saved_position(pose),
        _placement_zone(printer, machine),
    )


def layout_for_group(
    input_dir: Path,
    files: list[str],
    printer: PrinterProfile,
    settings: SliceSettings,
    poses: dict[str, dict[str, Any]],
    manual_layout: dict[str, Any] | None,
    machine: dict[str, object] | None = None,
) -> Layout:
    parts = []
    for relative in files:
        pose = poses.get(relative)
        arrange_z = _manual_rotation_z(manual_layout, relative)
        part = _oriented_footprint(input_dir, relative, settings, pose, extra_z=arrange_z)
        parts.append(replace(part, z=pose_z(pose)))
    manual = None
    if isinstance(manual_layout, dict):
        manual = {name: (float(pos["x"]), float(pos["y"])) for name, pos in manual_layout.items()}
    # Frozen / Orca-arranged positions already include spacing; only shelf-pack
    # needs the adhesion-based gap when inventing a layout.
    gap = 0.0 if manual is not None else pack_gap(settings)
    return place_group(
        parts,
        float(printer.machine_width or 0),
        float(printer.machine_depth or 0),
        float(printer.machine_height or 0),
        gap,
        manual,
        _placement_zone(printer, machine),
    )


def _placement_zone(
    printer: PrinterProfile, machine: dict[str, object] | None
) -> PlacementZone | None:
    if not idex_offset_active(machine) or machine is None:
        return None
    return idex_zone(
        float(printer.machine_width or machine["bed_width"]),
        float(printer.machine_depth or machine["bed_depth"]),
        float(machine["idex_offset_x"]),
    )


def freeze_layout(
    layout: Layout,
    moved_file: str,
    x: float,
    y: float,
    previous: dict[str, Any] | None = None,
) -> dict[str, dict[str, float]]:
    positions: dict[str, dict[str, float]] = {}
    for item in layout.items:
        entry: dict[str, float] = {"x": item.min_x, "y": item.min_y}
        prior = previous.get(item.file) if isinstance(previous, dict) else None
        if isinstance(prior, dict) and "rotation_z" in prior:
            entry["rotation_z"] = float(prior["rotation_z"])
        positions[item.file] = entry
    moved: dict[str, float] = {"x": x, "y": y}
    prior_moved = previous.get(moved_file) if isinstance(previous, dict) else None
    if isinstance(prior_moved, dict) and "rotation_z" in prior_moved:
        moved["rotation_z"] = float(prior_moved["rotation_z"])
    elif moved_file in positions and "rotation_z" in positions[moved_file]:
        moved["rotation_z"] = positions[moved_file]["rotation_z"]
    positions[moved_file] = moved
    return positions


def ensure_group_layout(
    input_dir: Path,
    group: dict[str, Any],
    printer: PrinterProfile,
    settings: SliceSettings,
    poses: dict[str, dict[str, Any]],
    machine: dict[str, object] | None = None,
) -> tuple[Layout, bool]:
    """Return a group layout, running Orca auto-arrange when needed.

    When the engine is Orca and the group has no frozen layout, arrange the
    parts, write a reusable ``.3mf`` beside the source files, and freeze the
    resulting front-left positions onto the group. Returns ``(layout, dirty)``
    where ``dirty`` means the caller should persist the group.
    """
    files = list(group["files"])
    use_idex = idex_offset_active(machine)
    offset_key = _offset_key(machine) if use_idex and machine is not None else None
    if use_idex and offset_key is not None:
        reused = _layout_from_offset_archive(input_dir, group, files, offset_key)
        if reused is not None:
            group["manual_layout"] = reused
            layout = layout_for_group(
                input_dir, files, printer, settings, poses, reused, machine
            )
            if layout.error is None:
                return layout, True
    manual = group.get("manual_layout")
    if isinstance(manual, dict) and manual and not use_idex:
        refreshed = _refresh_layout_from_arranged_3mf(input_dir, group, files)
        if refreshed is not None:
            group["manual_layout"] = refreshed
            return (
                layout_for_group(
                    input_dir, files, printer, settings, poses, refreshed, machine
                ),
                True,
            )
        return (
            layout_for_group(input_dir, files, printer, settings, poses, manual, machine),
            False,
        )
    if settings.slicer_engine == "orca":
        short_id = str((machine or {}).get("printer_short_id") or "").strip()
        group_name = str(group.get("name") or "group")
        arrange_name = f"{group_name}_{short_id}" if use_idex and short_id else group_name
        status: str | None = None
        try:
            positions, archive = arrange_group(
                input_dir,
                files,
                printer,
                settings,
                poses,
                arrange_name,
                machine,
                use_idex_zone=use_idex,
            )
            layout = layout_for_group(
                input_dir, files, printer, settings, poses, positions, machine
            )
            if layout.error is not None and use_idex:
                raise ValueError(layout.error or "IDEX arrange did not fit")
        except (EngineError, ValueError):
            if not use_idex:
                raise
            positions, archive = arrange_group(
                input_dir,
                files,
                printer,
                settings,
                poses,
                group_name,
                machine,
                use_idex_zone=False,
            )
            layout = layout_for_group(
                input_dir, files, printer, settings, poses, positions, None
            )
            status = "IDEX offset zone too small; recentred on full bed"
            use_idex = False
            offset_key = None
        if status is not None:
            layout = Layout(
                layout.bed_width,
                layout.bed_depth,
                layout.bed_height,
                layout.items,
                layout.error,
                status,
            )
        group["manual_layout"] = positions
        try:
            relative = archive.relative_to(input_dir).as_posix()
        except ValueError:
            relative = archive.name
        if use_idex and offset_key is not None:
            by_offset = dict(group.get("arranged_3mf_by_offset") or {})
            by_offset[offset_key] = relative
            group["arranged_3mf_by_offset"] = by_offset
            group["arranged_3mf"] = relative
        else:
            group["arranged_3mf"] = relative
        return layout, True
    return layout_for_group(input_dir, files, printer, settings, poses, None, machine), False


def _offset_key(machine: dict[str, object]) -> str:
    value = float(machine["idex_offset_x"])
    if value.is_integer():
        return str(int(value))
    return format(value, "g")


def _layout_from_offset_archive(
    input_dir: Path,
    group: dict[str, Any],
    files: list[str],
    offset_key: str,
) -> dict[str, dict[str, float]] | None:
    by_offset = group.get("arranged_3mf_by_offset")
    if not isinstance(by_offset, dict):
        return None
    arranged = by_offset.get(offset_key)
    if not arranged:
        return None
    archive = input_dir / safe_relative(str(arranged))
    if not archive.is_file():
        return None
    try:
        return layout_from_arranged_3mf(archive.read_bytes(), files)
    except EngineError:
        return None


def _manual_rotation_z(manual_layout: dict[str, Any] | None, relative: str) -> float:
    if not isinstance(manual_layout, dict):
        return 0.0
    position = manual_layout.get(relative)
    if not isinstance(position, dict) or position.get("rotation_z") is None:
        return 0.0
    return float(position["rotation_z"])


def _refresh_layout_from_arranged_3mf(
    input_dir: Path,
    group: dict[str, Any],
    files: list[str],
) -> dict[str, dict[str, float]] | None:
    """Rebuild frozen layout from the saved project when rotation data is missing."""
    manual = group.get("manual_layout")
    if not isinstance(manual, dict) or not manual:
        return None
    if all(isinstance(pos, dict) and pos.get("rotation_z") is not None for pos in manual.values()):
        return None
    arranged = group.get("arranged_3mf")
    if not arranged:
        return None
    archive = input_dir / safe_relative(str(arranged))
    if not archive.is_file():
        return None
    return layout_from_arranged_3mf(archive.read_bytes(), files)


def prepare_slice(
    input_dir: Path,
    printer: PrinterProfile,
    layout: Layout,
    settings: SliceSettings,
) -> PreparedSlice:
    if layout.error:
        raise ValueError(layout.error)
    if printer.machine_width is None or printer.machine_depth is None:
        raise ValueError("printer bed size is missing")
    temporary = tempfile.TemporaryDirectory(prefix="auto-slicer-")
    root = Path(temporary.name)
    meshes: list[Path] = []
    for item in layout.items:
        source = _model_path(input_dir, item.file)
        triangles = load_model(source.name, source.read_bytes())
        posed = transform_mesh(
            triangles,
            item.rotation,
            item.min_x,
            item.min_y,
            printer.machine_width,
            printer.machine_depth,
            orca_origin(settings, printer),
            z=item.z,
            scale=item.scale,
        )
        destination = root / f"{len(meshes)}.stl"
        destination.write_bytes(write_stl(posed))
        meshes.append(destination)
    return PreparedSlice(layout, meshes, temporary)


def output_model_path(
    printer_dir: str,
    folder: str,
    relative: str,
    test: bool,
    mode_suffix: str | None = None,
) -> Path:
    model = safe_relative(relative)
    stem = model.stem + (f"_{mode_suffix}" if mode_suffix else "")
    gcode_name = model.with_name(f"{safe_segment(stem)}.gcode")
    parts = [safe_segment(printer_dir), safe_segment(folder)]
    if test:
        parts.append("_test")
    return Path(*parts, *gcode_name.parts)


def output_group_path(
    printer_dir: str,
    folder: str,
    group_name: str,
    test: bool,
    mode_suffix: str | None = None,
) -> Path:
    parts = [safe_segment(printer_dir), safe_segment(folder)]
    if test:
        parts.append("_test")
    parts.append("groups")
    name = safe_segment(group_name) + (f"_{mode_suffix}" if mode_suffix else "")
    return Path(*parts, f"{name}.gcode")


def run_prepared_slice(
    paths: DataPaths,
    printer: PrinterProfile,
    settings: SliceSettings,
    prepared: PreparedSlice,
    output: Path,
    on_progress: Any = None,
    machine: dict[str, object] | None = None,
    *,
    should_cancel: Any = None,
    on_process_start: Any = None,
    on_process_finish: Any = None,
) -> tuple[int | None, float | None]:
    if settings.slicer_engine == "orca":
        return _run_orca(
            printer,
            settings,
            prepared,
            output,
            on_progress,
            machine,
            should_cancel=should_cancel,
            on_process_start=on_process_start,
            on_process_finish=on_process_finish,
        )
    engine, resources, library_path = locate_cura(paths)
    if engine is None or resources is None:
        raise EngineError("CuraEngine was not found. Build the Docker image or set CURA_ENGINE and CURA_RESOURCES.")
    overrides = cura_machine_overrides(machine) if machine else None
    command = build_slice_command(
        engine,
        printer,
        settings,
        prepared.meshes,
        output,
        active_config_dir(paths.cura_config_dir),
        resources,
        overrides,
    )
    if machine is not None and printer.extruder_count > 1:
        # Per-tool temps are applied after each -eN block by rewriting via a
        # second pass of overrides appended in build_slice_command; inject T1
        # temps into machine_overrides already used for both tools, then patch
        # the command's second extruder block temperatures below.
        command = _apply_cura_t1_temps(command, machine)
    output.parent.mkdir(parents=True, exist_ok=True)
    code, tail = run_slice(
        command,
        library_path,
        on_progress,
        should_cancel=should_cancel,
        on_start=on_process_start,
        on_finish=on_process_finish,
    )
    if code != 0 or not output.is_file() or output.stat().st_size == 0:
        if output.is_file():
            output.unlink()
        raise EngineError(tail or f"CuraEngine exited with status {code}")
    return _header_from_file(output, machine=machine)


def _apply_cura_t1_temps(command: list[str], machine: dict[str, object]) -> list[str]:
    """Replace T0 material temps in the -e1 block with T1 values."""
    try:
        e1 = command.index("-e1")
    except ValueError:
        return command
    temps = dict(cura_extruder_temperature_overrides(machine, 1))
    updated = list(command)
    for index in range(e1, len(updated) - 1):
        if updated[index] != "-s":
            continue
        key, _, value = updated[index + 1].partition("=")
        if key in temps:
            updated[index + 1] = f"{key}={temps[key]}"
    return updated


def _run_orca(
    printer: PrinterProfile,
    settings: SliceSettings,
    prepared: PreparedSlice,
    output: Path,
    on_progress: Any,
    machine: dict[str, object] | None,
    *,
    should_cancel: Any = None,
    on_process_start: Any = None,
    on_process_finish: Any = None,
) -> tuple[int | None, float | None]:
    binary, library_path = locate_orca()
    if binary is None:
        raise EngineError("OrcaSlicer was not found. Build the Docker image or set ORCA_SLICER.")
    config_dir = active_orca_dir()
    if config_dir is None:
        raise EngineError("Orca configuration was not found. Mount %APPDATA%\\OrcaSlicer or set ORCA_CONFIG.")
    root = Path(prepared.temp_dir.name)
    process_path = root / "process.json"
    assemble_path = root / "assemble.json"
    machine_path = Path(printer.definition_path) if printer.definition_path else None
    source: dict[str, object] = {}
    if machine_path is not None and machine_path.is_file():
        loaded = json.loads(machine_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            source = apply_user_orca_machine_override(loaded, config_dir)
    machine_name = str(source.get("name") or printer.name)
    compatible = [machine_name] if machine_name else None
    write_process_profile(
        process_path,
        settings,
        orca_process_extras(machine, settings) if machine else None,
        inherits=process_inherits_for_machine(source),
        compatible_printers=compatible,
    )
    write_assemble_list(assemble_path, prepared.meshes)
    if machine_path is not None and machine_path.is_file() and (machine is not None or source):
        patched = root / "machine.json"
        machine_json = dict(source)
        if machine is not None:
            machine_json = patch_orca_machine(machine_json, machine)
        patched.write_text(json.dumps(machine_json, indent=2), encoding="utf-8")
        printer = _printer_with_machine_path(printer, patched)
    filament: Path | list[Path] | None = generic_pla(config_dir)
    filament_path = root / "filament.json"
    if machine is not None:
        write_filament_profile(
            filament_path,
            orca_filament_overrides(machine, tool=0),
            name=str(machine.get("filament_name") or "Generic PLA"),
            inherits=str(machine.get("filament_inherits") or "fdm_filament_pla"),
            compatible_printers=compatible,
        )
        filaments: list[Path] = [filament_path]
        if int(machine.get("extruder_count") or printer.extruder_count or 1) > 1:
            filament_t1 = root / "filament_t1.json"
            write_filament_profile(
                filament_t1,
                orca_filament_overrides(machine, tool=1),
                name=str(machine.get("filament_name_t1") or machine.get("filament_name") or "Generic PLA"),
                inherits=str(
                    machine.get("filament_inherits_t1")
                    or machine.get("filament_inherits")
                    or "fdm_filament_pla"
                ),
                compatible_printers=compatible,
            )
            filaments.append(filament_t1)
        filament = filaments
    elif filament is None:
        write_filament_profile(filament_path, compatible_printers=compatible)
        filament = filament_path
    archive = root / "plate.3mf"
    command = build_orca_command(
        binary,
        printer,
        prepared.meshes,
        archive,
        config_dir,
        process_path,
        filament,
        assemble_path,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    code, tail = run_slice(
        command,
        library_path,
        on_progress,
        should_cancel=should_cancel,
        on_start=on_process_start,
        on_finish=on_process_finish,
    )
    if code != 0 or not archive.is_file() or archive.stat().st_size == 0:
        raise EngineError(tail or f"OrcaSlicer exited with status {code}")
    extract_plate_gcode(archive, output)
    if not output.is_file() or output.stat().st_size == 0:
        raise EngineError(tail or "OrcaSlicer did not produce gcode")
    return _header_from_file(output, machine=machine)


def _printer_with_machine_path(printer: PrinterProfile, path: Path) -> PrinterProfile:
    return replace(printer, definition_path=str(path))


def _header_from_file(
    path: Path,
    *,
    machine: dict[str, object] | None = None,
) -> tuple[int | None, float | None]:
    nozzle = 0.4
    if machine is not None and machine.get("nozzle_diameter") not in (None, ""):
        nozzle = float(machine["nozzle_diameter"])
    issues = validate_gcode_file(path, nozzle_diameter=nozzle)
    if issues:
        detail = issues[0].message
        try:
            path.unlink()
        except OSError:
            pass
        raise EngineError(
            "Generated G-code failed extrusion validation "
            f"(would exceed Klipper max_extrude_cross_section): {detail}"
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_gcode_header(text[:8000] + "\n" + text[-8000:])


def _footprint(
    input_dir: Path, relative: str, rotation: tuple[float, float, float], scale: float = 1.0
) -> Footprint:
    path = _model_path(input_dir, relative)
    triangles = load_model(path.name, path.read_bytes())
    return footprint_of(relative, triangles, rotation, scale)


def _oriented_footprint(
    input_dir: Path,
    relative: str,
    settings: SliceSettings,
    pose: dict[str, Any] | None,
    *,
    extra_z: float = 0.0,
) -> Footprint:
    path = _model_path(input_dir, relative)
    triangles = load_model(path.name, path.read_bytes())
    scale = pose_scale(pose)
    rx, ry, rz = resolved_rotation(settings, pose, triangles, scale)
    return footprint_of(relative, triangles, (rx, ry, rz + float(extra_z)), scale)


def _model_path(input_dir: Path, relative: str) -> Path:
    path = input_dir / safe_relative(relative)
    if not path.is_file():
        raise FileNotFoundError(relative)
    return path
