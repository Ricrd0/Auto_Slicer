from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.engine import EngineError
from auto_slicer.settings_schema import SliceSettings, orca_setting_overrides


def locate_orca() -> tuple[Path | None, str]:
    """Return the OrcaSlicer binary and a library path for the extracted AppImage."""
    binary = os.environ.get("ORCA_SLICER")
    library = os.environ.get("ORCA_LIBRARY_PATH", "")
    if binary and Path(binary).is_file():
        if not library:
            root = os.environ.get("ORCA_ROOT")
            library = _library_path(Path(root)) if root else ""
        return Path(binary), library
    root_env = os.environ.get("ORCA_ROOT")
    if not root_env:
        return None, ""
    root = Path(root_env)
    found = _first_named(root, "orca-slicer")
    if found is None:
        return None, library or _library_path(root)
    return found, library or _library_path(root)


def active_orca_dir() -> Path | None:
    """Return the Orca config root that contains system or user machine profiles."""
    candidates: list[Path] = []
    for key in ("ORCA_CONFIG", "AUTO_SLICER_ORCA_FALLBACK"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "OrcaSlicer")
    home = Path.home()
    candidates.extend(
        [
            home / ".config" / "OrcaSlicer",
            home / ".local" / "share" / "OrcaSlicer",
            Path("/host-orca"),
        ]
    )
    for candidate in candidates:
        if _has_machines(candidate):
            return candidate
    return None


def discover_orca_printers(config_dir: Path | None) -> list[PrinterProfile]:
    if config_dir is None or not config_dir.is_dir():
        return []
    found: dict[str, PrinterProfile] = {}
    for kind in ("system", "user"):
        for path in _machine_jsons(config_dir / kind):
            profile = _load_machine(path, config_dir)
            if profile is not None:
                found[profile.name] = profile
    return [found[name] for name in sorted(found)]


def generic_pla(config_dir: Path) -> Path | None:
    for path in config_dir.glob("system/*/filament/Generic PLA @System.json"):
        if path.is_file():
            return path
    library = config_dir / "system" / "OrcaFilamentLibrary" / "filament" / "Generic PLA @System.json"
    if library.is_file():
        return library
    return None


def write_process_profile(path: Path, settings: SliceSettings) -> None:
    profile = {
        "type": "process",
        "name": "Auto Slicer",
        "inherits": "fdm_process_common",
        "from": "user",
        "instantiation": "true",
    }
    profile.update(orca_setting_overrides(settings))
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def write_filament_profile(path: Path) -> None:
    profile = {
        "type": "filament",
        "name": "Generic PLA",
        "inherits": "fdm_filament_pla",
        "from": "user",
        "instantiation": "true",
    }
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def write_assemble_list(path: Path, meshes: list[Path]) -> None:
    """Place already-positioned meshes with a zero translation.

    ``prepare_slice`` writes Orca meshes in front-left bed coordinates, and
    Orca adds ``pos_*`` on top of those vertices.
    """
    document = {
        "plates": [
            {
                "plate_name": "Plate 1",
                "need_arrange": False,
                "objects": [
                    {
                        "path": str(mesh),
                        "count": 1,
                        "filaments": [1],
                        "pos_x": [0.0],
                        "pos_y": [0.0],
                        "pos_z": [0.0],
                    }
                    for mesh in meshes
                ],
            }
        ]
    }
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def build_orca_command(
    binary: Path,
    printer: PrinterProfile,
    meshes: list[Path],
    output_3mf: Path,
    config_dir: Path,
    process_path: Path,
    filament_path: Path,
    assemble_path: Path,
) -> list[str]:
    if printer.definition_path is None:
        raise EngineError(printer.error or "Orca machine profile is missing")
    if not meshes:
        raise EngineError("there is no mesh to slice")
    return [
        str(binary),
        "--datadir",
        str(config_dir),
        "--load-settings",
        f"{process_path};{printer.definition_path}",
        "--load-filaments",
        str(filament_path),
        "--load-assemble-list",
        str(assemble_path),
        "--slice",
        "0",
        "--export-3mf",
        str(output_3mf),
        "--min-save",
        "1",
    ]


def extract_plate_gcode(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".gcode")]
        plate = [name for name in names if "plate_1" in name.replace("\\", "/").lower()]
        chosen = plate or names
        if not chosen:
            raise EngineError("Orca did not write plate gcode into the 3MF")
        destination.write_bytes(archive.read(sorted(chosen)[0]))


def orca_origin(settings: SliceSettings) -> str:
    return "front_left" if settings.slicer_engine == "orca" else "bed_center"


def _load_machine(path: Path, config_dir: Path) -> PrinterProfile | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("type") != "machine":
        return None
    if not _is_true(data.get("instantiation")):
        return None
    name = str(data.get("name") or path.stem)
    nozzles = data.get("nozzle_diameter")
    extruder_count = len(nozzles) if isinstance(nozzles, list) and nozzles else 0
    bed = _bed_size(data.get("printable_area"))
    height = _float_or_none(data.get("printable_height"))
    error = None
    if extruder_count == 0:
        error = "nozzle_diameter is missing"
    elif extruder_count > 1:
        error = "only single-extruder printers can be sliced"
    if bed is None:
        error = error or "printable_area is missing"
    width, depth = bed if bed is not None else (None, None)
    return PrinterProfile(
        id=f"orca:{name}",
        name=name,
        definition_id=name,
        definition_path=str(path),
        extruder_definition_id=None,
        extruder_definition_path=None,
        global_settings=[],
        extruder_settings=[],
        machine_width=width,
        machine_depth=depth,
        machine_height=height,
        machine_center_is_zero=False,
        extruder_count=extruder_count or 1,
        setting_version=None,
        resource_setting_version=None,
        error=error,
        engine="orca",
        config_root=str(config_dir),
    )


def _machine_jsons(preset_root: Path) -> list[Path]:
    if not preset_root.is_dir():
        return []
    paths: list[Path] = []
    try:
        children = list(preset_root.iterdir())
    except OSError:
        return paths
    for child in children:
        machine = child / "machine" if child.is_dir() else None
        if machine is not None and machine.is_dir():
            paths.extend(sorted(machine.glob("*.json")))
    return paths


def _has_machines(root: Path) -> bool:
    return any(_machine_jsons(root / kind) for kind in ("system", "user"))


def _bed_size(area: object) -> tuple[float, float] | None:
    if not isinstance(area, list):
        return None
    xs: list[float] = []
    ys: list[float] = []
    for point in area:
        if not isinstance(point, str) or "x" not in point.lower():
            continue
        left, right = point.lower().split("x", 1)
        try:
            xs.append(float(left))
            ys.append(float(right))
        except ValueError:
            continue
    if not xs or not ys:
        return None
    return max(xs) - min(xs), max(ys) - min(ys)


def _float_or_none(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_true(value: object) -> bool:
    return value is True or value == "true"


def _first_named(root: Path, name: str) -> Path | None:
    if not root.is_dir():
        return None
    for path in root.rglob("*"):
        if path.is_file() and path.name.lower() == name:
            return path
    return None


def _library_path(root: Path) -> str:
    directories: list[str] = []
    if not root.is_dir():
        return ""
    for path in root.rglob("*"):
        if path.is_dir() and path.name in {"lib", "lib64"}:
            directories.append(str(path))
    return ":".join(directories)
