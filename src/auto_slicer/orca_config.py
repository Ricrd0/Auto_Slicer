from __future__ import annotations

import json
import math
import os
import re
import time
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
    bundled = bundled_orca_profiles_dir()
    if bundled is not None and _has_machines(bundled):
        return bundled
    return None


def bundled_orca_profiles_dir() -> Path | None:
    """Repo/image-bundled Orca vendor pack (Anycubic Kossel, etc.)."""
    candidates = [
        Path(__file__).resolve().parents[2] / "resources" / "orca_profiles",
        Path("/opt/auto-slicer/resources/orca_profiles"),
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def discover_orca_printers(
    config_dir: Path | None, *, include_bundled: bool = True
) -> list[PrinterProfile]:
    return load_orca_printers(config_dir, include_bundled=include_bundled)


def load_orca_printers(
    config_dir: Path | None,
    *,
    include_bundled: bool = True,
    only_ids: set[str] | None = None,
) -> list[PrinterProfile]:
    """Load Orca machine profiles.

    When ``only_ids`` is set (e.g. ``{"orca:Creality Ender-3 0.4 nozzle"}``), only
    those machines are resolved — used for the owned-printers list so the UI does
    not wait on a full catalog scan.
    """
    if only_ids is not None:
        found: dict[str, PrinterProfile] = {}
        for orca_id in only_ids:
            profile = load_orca_printer(config_dir, orca_id, include_bundled=include_bundled)
            if profile is not None:
                found[profile.id] = profile
        return [found[key] for key in sorted(found)]
    found_by_name: dict[str, PrinterProfile] = {}
    for root in _orca_profile_roots(config_dir, include_bundled=include_bundled):
        for path in _iter_machine_paths(root):
            profile = _load_machine(path, root)
            if profile is not None:
                found_by_name[profile.name] = profile
    return [found_by_name[name] for name in sorted(found_by_name)]


def load_orca_printer(
    config_dir: Path | None,
    orca_id: str,
    *,
    include_bundled: bool = True,
    display_name: str | None = None,
    bed: tuple[float | None, float | None, float | None] | None = None,
    extruder_count: int | None = None,
) -> PrinterProfile | None:
    """Resolve one linked Orca profile without building the full catalog.

    Uses a direct vendor/path guess (then a filename scan) so owned printers stay
    slicable while "Show every Orca printer" remains untouched. Inheritance is
    applied later at slice time from ``definition_path``.
    """
    catalog_name = str(orca_id or "").removeprefix("orca:").strip()
    if not catalog_name:
        return None
    located = find_orca_machine_file(config_dir, catalog_name, include_bundled=include_bundled)
    if located is None:
        return None
    path, root = located
    width = depth = height = None
    nozzles = extruder_count or 1
    if bed is not None:
        width, depth, height = bed
    data = _read_json(path)
    if isinstance(data, dict):
        raw_nozzles = data.get("nozzle_diameter")
        if isinstance(raw_nozzles, list) and raw_nozzles:
            nozzles = len(raw_nozzles)
        if width is None or depth is None:
            area_bed = _bed_size(data.get("printable_area"))
            if area_bed is not None:
                width, depth = area_bed
        if height is None:
            height = _float_or_none(data.get("printable_height"))
    error = None
    if nozzles > 2:
        error = f"at most two extruders are supported; this machine has {nozzles}"
    display = display_name or orca_display_name(catalog_name)
    return PrinterProfile(
        id=f"orca:{catalog_name}",
        name=display,
        definition_id=catalog_name,
        definition_path=str(path),
        extruder_definition_id=None,
        extruder_definition_path=None,
        global_settings=[],
        extruder_settings=[],
        machine_width=width,
        machine_depth=depth,
        machine_height=height,
        machine_center_is_zero=False,
        extruder_count=max(1, nozzles),
        setting_version=None,
        resource_setting_version=None,
        error=error,
        engine="orca",
        config_root=str(root),
    )


def find_orca_machine_file(
    config_dir: Path | None,
    catalog_name: str,
    *,
    include_bundled: bool = True,
) -> tuple[Path, Path] | None:
    """Return ``(machine.json path, config root)`` for a catalog machine name."""
    if not catalog_name:
        return None
    filename = f"{catalog_name}.json"
    for root in _orca_profile_roots(config_dir, include_bundled=include_bundled):
        direct = _direct_machine_path(root, catalog_name)
        if direct is not None:
            return direct, root
        for path in _iter_machine_paths(root):
            if path.name == filename or path.stem == catalog_name:
                return path, root
    return None


def user_orca_machine_override(config_dir: Path | None, base_name: str) -> dict[str, object] | None:
    """Return a user machine preset that inherits ``base_name``, if present.

    Orca keeps system nozzle profiles (often Marlin) and stores the user's
    customized Klipper/start-gcode copy under a separate name that inherits
    the system machine. Slice/arrange should prefer that override.
    """
    if config_dir is None or not base_name:
        return None
    matches: list[tuple[int, dict[str, object]]] = []
    for path in _machine_jsons(config_dir / "user"):
        data = _read_json(path)
        if data is None or not _is_true(data.get("instantiation", "true")):
            continue
        if str(data.get("inherits") or "") != base_name:
            continue
        name = str(data.get("name") or path.stem).lower()
        score = 20
        if "klipper" in name:
            score += 50
        if "0.4" in name:
            score += 5
        matches.append((score, data))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0], reverse=True)
    return dict(matches[0][1])


def apply_user_orca_machine_override(
    source: dict[str, object], config_dir: Path | None
) -> dict[str, object]:
    """Overlay a matching user preset onto a system machine definition."""
    base_name = str(source.get("name") or "")
    override = user_orca_machine_override(config_dir, base_name)
    if override is None:
        return dict(source)
    merged = dict(source)
    for key, value in override.items():
        if key in {
            "type",
            "name",
            "inherits",
            "from",
            "instantiation",
            "setting_id",
            "compatible_prints",
            "compatible_printers",
        }:
            continue
        if value is None or value == "":
            continue
        merged[key] = value
    return merged


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
    *,
    inherits: str | None = None,
    compatible_printers: list[str] | None = None,
) -> None:
    profile: dict[str, object] = {
        "type": "process",
        "name": "Auto Slicer",
        "inherits": inherits or "fdm_process_common",
        "from": "user",
        "instantiation": "true",
    }
    profile.update(orca_setting_overrides(settings))
    if extra:
        profile.update(extra)
    if compatible_printers:
        profile["compatible_printers"] = compatible_printers
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def write_filament_profile(
    path: Path,
    extra: dict[str, object] | None = None,
    *,
    name: str = "Generic PLA",
    inherits: str = "fdm_filament_pla",
    compatible_printers: list[str] | None = None,
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
    if compatible_printers:
        profile["compatible_printers"] = compatible_printers
    path.write_text(json.dumps(profile, indent=2), encoding="utf-8")


def process_inherits_for_machine(machine_json: dict[str, object] | None) -> str | None:
    """Prefer the machine's default print profile so temporary process JSON can inherit."""
    if not machine_json:
        return None
    profile = machine_json.get("default_print_profile")
    if isinstance(profile, str) and profile.strip():
        return profile.strip()
    if isinstance(profile, list) and profile:
        value = str(profile[0]).strip()
        return value or None
    return None


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
    Orca adds ``pos_*`` on top of those vertices. Parts stay separate: we never
    set ``assemble_index`` (values greater than 0 would merge objects) and the
    CLI never receives ``--assemble``.
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
    filament_path: Path | list[Path],
    assemble_path: Path,
) -> list[str]:
    if printer.definition_path is None:
        raise EngineError(printer.error or "Orca machine profile is missing")
    if not meshes:
        raise EngineError("there is no mesh to slice")
    filaments = filament_path if isinstance(filament_path, list) else [filament_path]
    return [
        str(binary),
        "--datadir",
        str(config_dir),
        "--load-settings",
        f"{process_path};{printer.definition_path}",
        "--load-filaments",
        ";".join(str(path) for path in filaments),
        "--load-assemble-list",
        str(assemble_path),
        "--slice",
        "0",
        "--export-3mf",
        str(output_3mf),
        "--min-save",
    ]


def build_orca_arrange_command(
    binary: Path,
    printer: PrinterProfile,
    meshes: list[Path],
    output_3mf: Path,
    config_dir: Path,
    process_path: Path,
    filament_path: Path | list[Path],
    settings: SliceSettings,
) -> list[str]:
    """Arrange separate STLs onto one plate and export a full project 3MF.

    Intentionally omits ``--assemble`` so parts remain individual objects, and
    omits ``--min-save`` so transforms and meshes are kept for reuse.
    """
    if printer.definition_path is None:
        raise EngineError(printer.error or "Orca machine profile is missing")
    if not meshes:
        raise EngineError("there is no mesh to arrange")
    filaments = filament_path if isinstance(filament_path, list) else [filament_path]
    command = [
        str(binary),
        "--datadir",
        str(config_dir),
        "--load-settings",
        f"{process_path};{printer.definition_path}",
        "--load-filaments",
        ";".join(str(path) for path in filaments),
        "--arrange=1",
        f"--allow-rotations={'1' if settings.orca_arrange_rotate else '0'}",
        f"--allow-multicolor-oneplate={'1' if settings.orca_arrange_multicolor else '0'}",
        "--export-3mf",
        str(output_3mf),
    ]
    command.extend(str(mesh) for mesh in meshes)
    return command


def extract_plate_gcode(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".gcode")]
        plate = [name for name in names if "plate_1" in name.replace("\\", "/").lower()]
        chosen = plate or names
        if not chosen:
            raise EngineError("Orca did not write plate gcode into the 3MF")
        destination.write_bytes(archive.read(sorted(chosen)[0]))


def orca_origin(settings: SliceSettings, printer: PrinterProfile | None = None) -> str:
    """Mesh origin for the active engine.

    Orca usually uses front-left printable areas. Delta / center-zero beds
    (negative printable_area coordinates) need bed-centered mesh coords.
    """
    if settings.slicer_engine != "orca":
        return "bed_center"
    if printer is not None and printer.machine_center_is_zero:
        return "bed_center"
    return "front_left"


def area_is_center_zero(area: object) -> bool:
    if not isinstance(area, list):
        return False
    for point in area:
        if not isinstance(point, str) or "x" not in point.lower():
            continue
        left, right = point.lower().split("x", 1)
        try:
            if float(left) < -1e-6 or float(right) < -1e-6:
                return True
        except ValueError:
            continue
    return False


def circular_printable_area(diameter: float, *, segments: int = 72) -> list[str]:
    """Polygon approximating a circular bed centered at the origin."""
    radius = max(float(diameter), 0.0) / 2.0
    points: list[str] = []
    for index in range(max(8, int(segments))):
        angle = 2.0 * math.pi * index / segments
        x = radius * math.cos(angle)
        y = radius * math.sin(angle)
        points.append(f"{x:g}x{y:g}")
    return points


def resolve_orca_machine(
    data: dict[str, object], config_dir: Path | None
) -> dict[str, object]:
    """Merge ``inherits`` parents so bed size and flavor are visible on nozzle presets."""
    if config_dir is None:
        return dict(data)
    indexed = _cached_machine_profile_index(config_dir)
    return _resolve_machine_data(data, indexed, set())


def orca_display_name(name: str) -> str:
    """Stable UI/output name for Orca catalog profiles (e.g. J1 nozzle → J1S)."""
    if re.match(r"(?i)^snapmaker\s+j1(\b|$)", name.strip()):
        return "Snapmaker J1S"
    return name


def _orca_profile_roots(config_dir: Path | None, *, include_bundled: bool) -> list[Path]:
    roots: list[Path] = []
    if config_dir is not None and config_dir.is_dir():
        roots.append(config_dir)
    if include_bundled:
        bundled = bundled_orca_profiles_dir()
        if bundled is not None and bundled not in roots:
            roots.append(bundled)
    return roots


def _direct_machine_path(root: Path, catalog_name: str) -> Path | None:
    """Prefer a vendor/folder guess so owned printers skip a full catalog walk."""
    if not catalog_name:
        return None
    filename = f"{catalog_name}.json"
    vendor = catalog_name.split()[0]
    candidates: list[Path] = []
    for kind in ("system", "user"):
        if vendor:
            candidates.append(root / kind / vendor / "machine" / filename)
        candidates.append(root / kind / "default" / "machine" / filename)
    if vendor:
        candidates.append(root / vendor / "machine" / filename)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _iter_machine_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for kind in ("system", "user"):
        paths.extend(_machine_jsons(root / kind))
    paths.extend(_machine_jsons(root))
    return paths


def _machine_catalog_name(path: Path) -> str:
    data = _read_json(path)
    if data is not None and data.get("name"):
        return str(data["name"])
    return path.stem


_MACHINE_INDEX_CACHE: dict[str, tuple[float, dict[str, dict[str, object]]]] = {}
_MACHINE_INDEX_TTL = 300.0


def _cached_machine_profile_index(config_dir: Path) -> dict[str, dict[str, object]]:
    key = str(config_dir)
    now = time.monotonic()
    cached = _MACHINE_INDEX_CACHE.get(key)
    if cached is not None and now - cached[0] < _MACHINE_INDEX_TTL:
        return cached[1]
    indexed = _machine_profile_index(config_dir)
    _MACHINE_INDEX_CACHE[key] = (now, indexed)
    return indexed


def _load_machine(path: Path, config_dir: Path) -> PrinterProfile | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("type") != "machine":
        return None
    if not _is_true(data.get("instantiation")):
        return None
    catalog_name = str(data.get("name") or path.stem)
    resolved = resolve_orca_machine(data, config_dir)
    nozzles = resolved.get("nozzle_diameter")
    if not isinstance(nozzles, list) or not nozzles:
        nozzles = data.get("nozzle_diameter")
    extruder_count = len(nozzles) if isinstance(nozzles, list) and nozzles else 0
    bed = _bed_size(resolved.get("printable_area"))
    height = _float_or_none(resolved.get("printable_height"))
    error = None
    if extruder_count == 0:
        error = "nozzle_diameter is missing"
    elif extruder_count > 2:
        error = f"at most two extruders are supported; this machine has {extruder_count}"
    if bed is None:
        error = error or "printable_area is missing"
    width, depth = bed if bed is not None else (None, None)
    display = orca_display_name(catalog_name)
    return PrinterProfile(
        id=f"orca:{catalog_name}",
        name=display,
        definition_id=catalog_name,
        definition_path=str(path),
        extruder_definition_id=None,
        extruder_definition_path=None,
        global_settings=[],
        extruder_settings=[],
        machine_width=width,
        machine_depth=depth,
        machine_height=height,
        machine_center_is_zero=area_is_center_zero(resolved.get("printable_area")),
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


def _machine_profile_index(config_dir: Path) -> dict[str, dict[str, object]]:
    indexed: dict[str, dict[str, object]] = {}
    roots = [config_dir / "system", config_dir / "user", config_dir]
    for root in roots:
        for path in _machine_jsons(root):
            data = _read_json(path)
            if data is None or data.get("type") not in {None, "machine"}:
                continue
            name = str(data.get("name") or path.stem)
            indexed[name] = data
            # Inherit targets are often bare ids without the display name.
            stem = path.stem
            if stem not in indexed:
                indexed[stem] = data
    return indexed


def _resolve_machine_data(
    data: dict[str, object],
    indexed: dict[str, dict[str, object]],
    seen: set[str],
) -> dict[str, object]:
    name = str(data.get("name") or "")
    if name:
        if name in seen:
            return {}
        seen.add(name)
    parent = str(data.get("inherits") or "").strip()
    merged: dict[str, object] = {}
    if parent:
        parent_data = indexed.get(parent)
        if parent_data is not None:
            merged = _resolve_machine_data(parent_data, indexed, seen)
    for key, value in data.items():
        if key in {"type", "inherits", "from", "instantiation", "setting_id"}:
            continue
        if value in (None, "", [], {}):
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
