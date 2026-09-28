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


def write_process_profile(
    path: Path,
    settings: SliceSettings,
    extra: dict[str, str] | None = None,
) -> None:
    profile = {
        "type": "process",
        "name": "Auto Slicer",
        "inherits": "fdm_process_common",
        "from": "user",
        "instantiation": "true",
    }
    profile.update(orca_setting_overrides(settings))
    if extra:
        profile.update(extra)
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def write_filament_profile(
    path: Path,
    extra: dict[str, object] | None = None,
    *,
    name: str = "Generic PLA",
    inherits: str = "fdm_filament_pla",
) -> None:
    profile: dict[str, object] = {
        "type": "filament",
        "name": name,
        "inherits": inherits,
        "from": "user",
        "instantiation": "true",
    }
    if extra:
        profile.update(extra)
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def discover_filaments(config_dir: Path | None) -> list[dict[str, object]]:
    """Return instantiated Orca filaments with temperatures resolved through inherits."""
    if config_dir is None or not config_dir.is_dir():
        return []
    indexed: dict[str, dict[str, object]] = {}
    vendors: dict[str, str] = {}
    selectable: set[str] = set()
    for kind in ("system", "user"):
        for path in _filament_jsons(config_dir / kind):
            data = _read_json(path)
            if data is None or not _is_filament_profile(data):
                continue
            name = str(data.get("name") or path.stem)
            indexed[name] = data
            vendors[name] = _filament_brand(path)
            if _is_selectable_filament(data):
                selectable.add(name)
    found: list[dict[str, object]] = []
    for name in sorted(selectable):
        resolved = _resolve_filament(name, indexed, set())
        nozzle = _first_number(resolved.get("nozzle_temperature"))
        nozzle_initial = _first_number(resolved.get("nozzle_temperature_initial_layer"))
        bed, bed_initial, plate = _bed_temperatures(resolved)
        nozzle_value = nozzle if nozzle is not None else 200.0
        bed_value = bed if bed is not None else 60.0
        found.append(
            {
                "id": f"orca-filament:{name}",
                "name": name,
                "vendor": vendors.get(name, ""),
                "nozzle_temperature": nozzle_value,
                "nozzle_temperature_initial": nozzle_initial if nozzle_initial is not None else nozzle_value,
                "bed_temperature": bed_value,
                "bed_temperature_initial": bed_initial if bed_initial is not None else bed_value,
                "bed_plate": plate,
            }
        )
    return found


def search_filaments(filaments: list[dict[str, object]], query: str) -> list[dict[str, object]]:
    text = query.strip().lower()
    if len(text) < 2:
        return []
    return [
        item
        for item in filaments
        if text in str(item["name"]).lower() or text in str(item.get("vendor", "")).lower()
    ]


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


_BED_PLATES = (
    ("cool plate", "cool_plate", "Cool Plate"),
    ("engineering plate", "eng_plate", "Engineering Plate"),
    ("hot plate", "hot_plate", "Hot Plate"),
    ("textured pei plate", "textured_plate", "Textured PEI Plate"),
    ("textured plate", "textured_plate", "Textured PEI Plate"),
)
_PLATE_FALLBACK = ("hot_plate", "cool_plate", "textured_plate", "eng_plate")


def _filament_jsons(preset_root: Path) -> list[Path]:
    return _preset_jsons(preset_root, "filament", nested=True)


def _is_filament_profile(data: dict[str, object]) -> bool:
    kind = data.get("type")
    if kind == "filament":
        return True
    # User presets saved by Orca often omit type and instantiation.
    return kind is None and bool(data.get("name"))


def _is_selectable_filament(data: dict[str, object]) -> bool:
    if "instantiation" in data:
        return _is_true(data.get("instantiation"))
    return True


def _filament_brand(path: Path) -> str:
    folder = path.parent
    if folder.name == "filament":
        brand = folder.parent.name
        return "" if brand == "default" else brand
    return folder.name


def _machine_jsons(preset_root: Path) -> list[Path]:
    return _preset_jsons(preset_root, "machine")


def _preset_jsons(preset_root: Path, kind: str, nested: bool = False) -> list[Path]:
    if not preset_root.is_dir():
        return []
    paths: list[Path] = []
    pattern = "**/*.json" if nested else "*.json"
    try:
        children = list(preset_root.iterdir())
    except OSError:
        return paths
    for child in children:
        folder = child / kind if child.is_dir() else None
        if folder is not None and folder.is_dir():
            paths.extend(sorted(folder.glob(pattern)))
    return paths


def _read_json(path: Path) -> dict[str, object] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _resolve_filament(name: str, indexed: dict[str, dict[str, object]], seen: set[str]) -> dict[str, object]:
    if name in seen or name not in indexed:
        return {}
    seen.add(name)
    data = indexed[name]
    parent = str(data.get("inherits") or "")
    merged = _resolve_filament(parent, indexed, seen) if parent else {}
    for key, value in data.items():
        if key in {"type", "name", "inherits", "from", "instantiation", "setting_id", "filament_id"}:
            continue
        merged[key] = value
    return merged


def _bed_temperatures(resolved: dict[str, object]) -> tuple[float | None, float | None, str]:
    bed_type = _first_text(resolved.get("bed_type")).lower()
    for label, plate, title in _BED_PLATES:
        if bed_type == label and f"{plate}_temp" in resolved:
            return _plate_pair(resolved, plate, title)
    for plate in _PLATE_FALLBACK:
        if f"{plate}_temp" in resolved:
            title = next(item[2] for item in _BED_PLATES if item[1] == plate)
            return _plate_pair(resolved, plate, title)
    return None, None, "Hot Plate"


def _plate_pair(resolved: dict[str, object], plate: str, title: str) -> tuple[float | None, float | None, str]:
    temp = _first_number(resolved.get(f"{plate}_temp"))
    initial = _first_number(resolved.get(f"{plate}_temp_initial_layer"))
    return temp, initial if initial is not None else temp, title


def _first_text(value: object) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    return "" if value is None else str(value)


def _first_number(value: object) -> float | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
