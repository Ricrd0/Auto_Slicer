from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from auto_slicer.cura_config import PrinterProfile, active_config_dir
from auto_slicer.engine import EngineError, build_slice_command, locate_cura, run_slice
from auto_slicer.gcode import parse_gcode_header
from auto_slicer.meshio import load_model, transform_mesh, write_stl
from auto_slicer.orca_config import (
    active_orca_dir,
    build_orca_command,
    extract_plate_gcode,
    generic_pla,
    locate_orca,
    orca_origin,
    write_assemble_list,
    write_filament_profile,
    write_process_profile,
)
from auto_slicer.paths import DataPaths, safe_relative, safe_segment
from auto_slicer.placement import Footprint, Layout, footprint_of, place_group, place_single
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


def saved_position(pose: dict[str, Any] | None) -> tuple[float, float] | None:
    if not pose or not isinstance(pose.get("position"), list) or len(pose["position"]) != 2:
        return None
    return float(pose["position"][0]), float(pose["position"][1])


def layout_for_model(
    input_dir: Path,
    relative: str,
    printer: PrinterProfile,
    settings: SliceSettings,
    pose: dict[str, Any] | None,
) -> Layout:
    part = _footprint(input_dir, relative, effective_rotation(settings, pose))
    return place_single(
        part,
        float(printer.machine_width or 0),
        float(printer.machine_depth or 0),
        float(printer.machine_height or 0),
        saved_position(pose),
    )


def layout_for_group(
    input_dir: Path,
    files: list[str],
    printer: PrinterProfile,
    settings: SliceSettings,
    poses: dict[str, dict[str, Any]],
    manual_layout: dict[str, Any] | None,
) -> Layout:
    parts = [_footprint(input_dir, relative, effective_rotation(settings, poses.get(relative))) for relative in files]
    manual = None
    if isinstance(manual_layout, dict):
        manual = {name: (float(pos["x"]), float(pos["y"])) for name, pos in manual_layout.items()}
    return place_group(
        parts,
        float(printer.machine_width or 0),
        float(printer.machine_depth or 0),
        float(printer.machine_height or 0),
        pack_gap(settings),
        manual,
    )


def freeze_layout(layout: Layout, moved_file: str, x: float, y: float) -> dict[str, dict[str, float]]:
    positions = {item.file: {"x": item.min_x, "y": item.min_y} for item in layout.items}
    positions[moved_file] = {"x": x, "y": y}
    return positions


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
            orca_origin(settings),
        )
        destination = root / f"{len(meshes)}.stl"
        destination.write_bytes(write_stl(posed))
        meshes.append(destination)
    return PreparedSlice(layout, meshes, temporary)


def output_model_path(printer_dir: str, folder: str, relative: str, test: bool) -> Path:
    model = safe_relative(relative)
    gcode_name = model.with_suffix(".gcode")
    parts = [safe_segment(printer_dir), safe_segment(folder)]
    if test:
        parts.append("_test")
    return Path(*parts, gcode_name)


def output_group_path(printer_dir: str, folder: str, group_name: str, test: bool) -> Path:
    parts = [safe_segment(printer_dir), safe_segment(folder)]
    if test:
        parts.append("_test")
    parts.append("groups")
    return Path(*parts, f"{safe_segment(group_name)}.gcode")


def run_prepared_slice(
    paths: DataPaths,
    printer: PrinterProfile,
    settings: SliceSettings,
    prepared: PreparedSlice,
    output: Path,
    on_progress: Any = None,
) -> tuple[int | None, float | None]:
    if settings.slicer_engine == "orca":
        return _run_orca(printer, settings, prepared, output, on_progress)
    engine, resources, library_path = locate_cura(paths)
    if engine is None or resources is None:
        raise EngineError("CuraEngine was not found. Build the Docker image or set CURA_ENGINE and CURA_RESOURCES.")
    command = build_slice_command(
        engine,
        printer,
        settings,
        prepared.meshes,
        output,
        active_config_dir(paths.cura_config_dir),
        resources,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    code, tail = run_slice(command, library_path, on_progress)
    if code != 0 or not output.is_file() or output.stat().st_size == 0:
        if output.is_file():
            output.unlink()
        raise EngineError(tail or f"CuraEngine exited with status {code}")
    return _header_from_file(output)


def _run_orca(
    printer: PrinterProfile,
    settings: SliceSettings,
    prepared: PreparedSlice,
    output: Path,
    on_progress: Any,
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
    write_process_profile(process_path, settings)
    write_assemble_list(assemble_path, prepared.meshes)
    filament = generic_pla(config_dir)
    if filament is None:
        filament = root / "filament.json"
        write_filament_profile(filament)
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
    code, tail = run_slice(command, library_path, on_progress)
    if code != 0 or not archive.is_file() or archive.stat().st_size == 0:
        raise EngineError(tail or f"OrcaSlicer exited with status {code}")
    extract_plate_gcode(archive, output)
    if not output.is_file() or output.stat().st_size == 0:
        raise EngineError(tail or "OrcaSlicer did not produce gcode")
    return _header_from_file(output)


def _header_from_file(path: Path) -> tuple[int | None, float | None]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return parse_gcode_header(text[:8000] + "\n" + text[-8000:])


def _footprint(input_dir: Path, relative: str, rotation: tuple[float, float, float]) -> Footprint:
    path = _model_path(input_dir, relative)
    triangles = load_model(path.name, path.read_bytes())
    return footprint_of(relative, triangles, rotation)


def _model_path(input_dir: Path, relative: str) -> Path:
    path = input_dir / safe_relative(relative)
    if not path.is_file():
        raise FileNotFoundError(relative)
    return path
