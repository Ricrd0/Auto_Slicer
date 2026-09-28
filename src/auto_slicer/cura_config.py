from __future__ import annotations

import configparser
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote_plus


_GEOMETRY_KEYS = (
    "machine_width",
    "machine_depth",
    "machine_height",
    "machine_center_is_zero",
    "machine_extruder_count",
)
_CFG_SUFFIXES = (
    ".xml.fdm_material",
    ".inst.cfg",
    ".global.cfg",
    ".extruder.cfg",
    ".def.json",
    ".cfg",
    ".json",
)
_CONTAINER_DIRS = (
    "machine_instances",
    "extruders",
    "definition_changes",
    "quality_changes",
    "quality",
    "user",
    "materials",
    "variants",
    "intent",
    "definitions",
)


@dataclass
class PrinterProfile:
    id: str
    name: str
    definition_id: str
    definition_path: str | None
    extruder_definition_id: str | None
    extruder_definition_path: str | None
    global_settings: list[tuple[str, str]]
    extruder_settings: list[tuple[str, str]]
    machine_width: float | None
    machine_depth: float | None
    machine_height: float | None
    machine_center_is_zero: bool
    extruder_count: int
    setting_version: int | None
    resource_setting_version: int | None
    warnings: list[str] = field(default_factory=list)
    error: str | None = None
    engine: str = "cura"
    source_files: list[str] = field(default_factory=list)
    global_cfg: str | None = None
    config_root: str = ""

    @property
    def version_mismatch(self) -> bool:
        return (
            self.setting_version is not None
            and self.resource_setting_version is not None
            and self.setting_version != self.resource_setting_version
        )

    @property
    def slicable(self) -> bool:
        return self.error is None and self.extruder_count <= 1 and self.machine_width is not None

    def to_public_dict(self, enabled: bool) -> dict[str, object]:
        warnings = list(self.warnings)
        if self.version_mismatch:
            warnings.append(
                f"Cura setting version {self.setting_version} does not match "
                f"the engine resources ({self.resource_setting_version})"
            )
        return {
            "id": self.id,
            "engine": self.engine,
            "name": self.name,
            "definition_id": self.definition_id,
            "machine_width": self.machine_width,
            "machine_depth": self.machine_depth,
            "machine_height": self.machine_height,
            "extruder_count": self.extruder_count,
            "setting_version": self.setting_version,
            "version_mismatch": self.version_mismatch,
            "warnings": warnings,
            "error": self.error,
            "slicable": self.slicable,
            "enabled": enabled,
        }


@dataclass
class FileIndex:
    by_id: dict[str, Path]
    extruders: list[Path]

    def get(self, container_id: str) -> Path | None:
        return self.by_id.get(container_id)


def discover_printers(config_dir: Path, resources_dir: Path | None) -> list[PrinterProfile]:
    config_dir = active_config_dir(config_dir)
    if resources_dir is None:
        resources_dir = installed_cura_resources()
    index = _build_index([config_dir, resources_dir])
    resource_version = _resource_setting_version(resources_dir, index)
    printers: list[PrinterProfile] = []
    instances = config_dir / "machine_instances"
    if not instances.is_dir():
        return printers
    for path in sorted(instances.glob("*.global.cfg")):
        printers.append(_load_printer(path, config_dir, index, resource_version))
    return printers


def active_config_dir(configured: Path) -> Path:
    """Use the configured folder, or a Cura version folder when that one is empty."""
    found = _directory_with_machines(configured)
    if found is not None:
        return found
    for candidate in _fallback_config_roots():
        found = _directory_with_machines(candidate)
        if found is not None:
            return found
    return configured


def installed_cura_resources() -> Path | None:
    preferred = _version_tuple(os.environ.get("CURA_VERSION", "5.13.0"))
    found: list[tuple[tuple[int, ...], Path]] = []
    for root in _program_roots():
        if not root.is_dir():
            continue
        try:
            children = list(root.iterdir())
        except OSError:
            continue
        for child in children:
            if "cura" not in child.name.lower() or not child.is_dir():
                continue
            resources = child / "share" / "cura" / "resources"
            if (resources / "definitions" / "fdmprinter.def.json").is_file():
                found.append((_version_tuple(child.name), resources))
    if not found:
        return None
    same = [item for item in found if len(item[0]) >= 2 and item[0][:2] == preferred[:2]]
    pool = same or found
    return max(pool, key=lambda item: item[0])[1]


def definition_search_dirs(config_dir: Path, resources_dir: Path | None) -> list[Path]:
    candidates = []
    if resources_dir is not None:
        candidates.extend(
            [
                resources_dir / "definitions",
                resources_dir / "extruders",
            ]
        )
    candidates.append(config_dir / "definitions")
    return [path for path in candidates if path.is_dir()]


def _load_printer(
    global_cfg: Path,
    config_dir: Path,
    index: FileIndex,
    resource_version: int | None,
) -> PrinterProfile:
    parser = _read_cfg(global_cfg)
    general = parser["general"] if parser.has_section("general") else {}
    metadata = parser["metadata"] if parser.has_section("metadata") else {}
    machine_id = str(general.get("id", global_cfg.name.replace(".global.cfg", "")))
    name = str(general.get("name", machine_id))
    setting_version = _optional_int(metadata.get("setting_version"))
    containers = _container_list(parser)
    definition_id = str(metadata.get("definition") or (containers[-1][1] if containers else ""))
    warnings: list[str] = []
    source_files = [_relative(config_dir, global_cfg)]
    global_settings: list[tuple[str, str]] = []
    for _index, container_id in reversed(containers):
        if container_id == definition_id or container_id == "empty" or container_id.startswith("empty"):
            continue
        loaded = _load_instance(container_id, index, definition_id)
        if loaded is None:
            warnings.append(f"missing container {container_id}")
            continue
        path, values = loaded
        source_files.extend(_user_source(config_dir, path))
        global_settings.extend(values)

    definition_path = index.get(definition_id)
    geometry = _geometry_from_definition(definition_id, index, set())
    for key, value in global_settings:
        if key in _GEOMETRY_KEYS:
            geometry[key] = _coerce_geometry(key, value)

    trains = _extruder_trains(metadata)
    if not trains:
        trains = _extruders_for_machine(index, machine_id, name)
    extruder_settings: list[tuple[str, str]] = []
    extruder_definition_id: str | None = None
    extruder_definition_path: Path | None = None
    if trains:
        extruder_id = trains[0]
        extruder_path = index.get(extruder_id)
        if extruder_path is not None and extruder_path.name.endswith(".extruder.cfg"):
            source_files.extend(_user_source(config_dir, extruder_path))
            extruder_parser = _read_cfg(extruder_path)
            for _index, container_id in reversed(_container_list(extruder_parser)):
                if container_id == "empty" or container_id.startswith("empty"):
                    continue
                definition_hit = index.get(container_id)
                if definition_hit is not None and definition_hit.name.endswith(".def.json"):
                    extruder_definition_id = container_id
                    extruder_definition_path = definition_hit
                    continue
                loaded = _load_instance(container_id, index, definition_id)
                if loaded is None:
                    if definition_hit is None:
                        warnings.append(f"missing extruder container {container_id}")
                    continue
                path, values = loaded
                source_files.extend(_user_source(config_dir, path))
                extruder_settings.extend(values)
        else:
            warnings.append(f"missing extruder stack {extruder_id}")
    if extruder_definition_id is None:
        meta_train = _definition_extruder_id(definition_path)
        if meta_train:
            extruder_definition_id = meta_train
            extruder_definition_path = index.get(meta_train)

    extruder_count = len(trains) if trains else int(_as_float(geometry.get("machine_extruder_count"), 1))
    width = _as_float(geometry.get("machine_width"), None)
    depth = _as_float(geometry.get("machine_depth"), None)
    height = _as_float(geometry.get("machine_height"), None)
    center_is_zero = _as_bool(geometry.get("machine_center_is_zero"), False)
    error = None
    if definition_path is None:
        error = f"printer definition {definition_id or '(missing)'} was not found"
    elif extruder_count > 1:
        error = f"single-extruder only; this machine has {extruder_count} extruders"
    elif width is None or depth is None or height is None:
        error = "machine bed size is not a number in this profile"
    if definition_path is not None:
        source_files.extend(_user_source(config_dir, definition_path))

    unique_sources: list[str] = []
    for item in source_files:
        if item not in unique_sources:
            unique_sources.append(item)
    return PrinterProfile(
        id=machine_id,
        name=name,
        definition_id=definition_id,
        definition_path=str(definition_path) if definition_path else None,
        extruder_definition_id=extruder_definition_id,
        extruder_definition_path=str(extruder_definition_path) if extruder_definition_path else None,
        global_settings=global_settings,
        extruder_settings=extruder_settings,
        machine_width=width,
        machine_depth=depth,
        machine_height=height,
        machine_center_is_zero=center_is_zero,
        extruder_count=extruder_count,
        setting_version=setting_version,
        resource_setting_version=resource_version,
        warnings=warnings,
        error=error,
        source_files=unique_sources,
        global_cfg=_relative(config_dir, global_cfg),
        config_root=str(config_dir.resolve()),
    )


def _build_index(roots: list[Path | None]) -> FileIndex:
    by_id: dict[str, Path] = {}
    extruders: list[Path] = []
    for root in roots:
        if root is None or not root.exists():
            continue
        try:
            files = [path for path in _container_files(root) if path.is_file()]
        except OSError:
            continue
        files.sort(key=str)
        for path in files:
            keys = _index_keys(path.name)
            if not keys:
                continue
            for key in keys:
                by_id.setdefault(key, path)
            if path.name.endswith(".extruder.cfg"):
                extruders.append(path)
            if path.suffix.lower() == ".cfg" or path.name.endswith(".cfg"):
                try:
                    parser = _read_cfg(path)
                except (configparser.Error, OSError, UnicodeError):
                    continue
                if parser.has_section("general"):
                    general = parser["general"]
                    for field_name in ("id", "name"):
                        value = general.get(field_name)
                        if value:
                            by_id.setdefault(value, path)
    return FileIndex(by_id, extruders)


def _container_files(root: Path):
    for name in _CONTAINER_DIRS:
        directory = root / name
        if directory.is_dir():
            yield from directory.rglob("*")


def _index_keys(name: str) -> list[str]:
    stem = _id_from_filename(name)
    if stem is None:
        return []
    decoded = unquote_plus(stem)
    if decoded == stem:
        return [stem]
    return [stem, decoded]


def _directory_with_machines(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    if _has_machines(root):
        return root
    try:
        children = [child for child in root.iterdir() if child.is_dir() and _has_machines(child)]
    except OSError:
        return None
    if not children:
        return None
    return _prefer_version(children)


def _has_machines(root: Path) -> bool:
    instances = root / "machine_instances"
    return instances.is_dir() and any(instances.glob("*.global.cfg"))


def _prefer_version(paths: list[Path]) -> Path:
    preferred = _version_tuple(os.environ.get("CURA_VERSION", "5.13.0"))

    def rank(path: Path) -> tuple[bool, tuple[int, ...]]:
        version = _version_tuple(path.name)
        same = len(version) >= 2 and len(preferred) >= 2 and version[:2] == preferred[:2]
        return (same, version)

    return max(paths, key=rank)


def _version_tuple(name: str) -> tuple[int, ...]:
    numbers: list[int] = []
    token = ""
    for char in name:
        if char.isdigit():
            token += char
            continue
        if token:
            numbers.append(int(token))
            token = ""
        elif numbers and char not in ".- ":
            break
    if token:
        numbers.append(int(token))
    return tuple(numbers[:3])


def _fallback_config_roots() -> list[Path]:
    roots: list[Path] = []
    extra = os.environ.get("AUTO_SLICER_CURA_FALLBACK")
    if extra:
        roots.append(Path(extra))
    host_mount = Path("/host-cura")
    if host_mount.is_dir():
        roots.append(host_mount)
    if os.name == "nt":
        roaming = os.environ.get("APPDATA")
        local = os.environ.get("LOCALAPPDATA")
        if roaming:
            roots.append(Path(roaming) / "cura")
        if local:
            roots.append(Path(local) / "cura")
    else:
        home = Path.home()
        roots.extend([home / ".local" / "share" / "cura", home / ".config" / "cura"])
    return roots


def _program_roots() -> list[Path]:
    if os.name == "nt":
        roots = []
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            value = os.environ.get(key)
            if value:
                roots.append(Path(value))
        return roots
    return [Path("/opt"), Path("/usr/share"), Path.home() / ".local" / "share"]


def _extruders_for_machine(index: FileIndex, machine_id: str, machine_name: str) -> list[str]:
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for path in index.extruders:
        try:
            parser = _read_cfg(path)
        except (configparser.Error, OSError, UnicodeError):
            continue
        metadata = parser["metadata"] if parser.has_section("metadata") else {}
        machine = str(metadata.get("machine", ""))
        if machine not in {machine_id, machine_name}:
            continue
        if str(metadata.get("enabled", "true")).strip().lower() == "false":
            continue
        general = parser["general"] if parser.has_section("general") else {}
        train_id = str(general.get("id") or _id_from_filename(path.name) or "")
        if not train_id or train_id in seen:
            continue
        seen.add(train_id)
        position = _optional_int(metadata.get("position"))
        found.append((position if position is not None else 0, train_id))
    found.sort()
    return [train_id for _position, train_id in found]


def _id_from_filename(name: str) -> str | None:
    for suffix in _CFG_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return None


def _read_cfg(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read(path, encoding="utf-8-sig")
    return parser


def _container_list(parser: configparser.ConfigParser) -> list[tuple[int, str]]:
    if not parser.has_section("containers"):
        return []
    items: list[tuple[int, str]] = []
    for key, value in parser["containers"].items():
        try:
            items.append((int(key), value.strip()))
        except ValueError:
            continue
    items.sort()
    return items


def _load_instance(
    container_id: str, index: FileIndex, definition_id: str = ""
) -> tuple[Path, list[tuple[str, str]]] | None:
    path = index.get(container_id)
    suffix = f"_{definition_id}" if definition_id else ""
    if path is None and suffix and container_id.endswith(suffix):
        path = index.get(container_id[: -len(suffix)])
    if path is None or path.name.endswith(".def.json"):
        return None
    if path.name.endswith(".xml.fdm_material"):
        return path, []
    if path.suffix.lower() != ".cfg" and not path.name.endswith(".cfg"):
        return None
    parser = _read_cfg(path)
    if not parser.has_section("values"):
        return path, []
    values: list[tuple[str, str]] = []
    for key, raw in parser["values"].items():
        normalized = _normalize_setting(raw)
        if normalized is None:
            continue
        values.append((key, normalized))
    return path, values


def _normalize_setting(raw: str) -> str | None:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1]
    if not value:
        return None
    if value.startswith("="):
        value = value[1:].strip()
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered
    return value


def _geometry_from_definition(definition_id: str, index: FileIndex, seen: set[str]) -> dict[str, object]:
    if not definition_id or definition_id in seen:
        return {}
    seen.add(definition_id)
    path = index.get(definition_id)
    if path is None or not path.name.endswith(".def.json"):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    merged: dict[str, object] = {}
    parent = data.get("inherits")
    if isinstance(parent, str) and parent:
        merged.update(_geometry_from_definition(parent, index, seen))
    metadata = data.get("metadata")
    if isinstance(metadata, dict) and "setting_version" in metadata:
        merged["setting_version"] = metadata["setting_version"]
    overrides = data.get("overrides")
    if isinstance(overrides, dict):
        for key, spec in overrides.items():
            if key not in _GEOMETRY_KEYS or not isinstance(spec, dict):
                continue
            if "default_value" in spec:
                merged[key] = spec["default_value"]
            elif "value" in spec:
                merged[key] = spec["value"]
    return merged


def _definition_extruder_id(definition_path: Path | None) -> str | None:
    if definition_path is None:
        return None
    try:
        data = json.loads(definition_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        return None
    trains = metadata.get("machine_extruder_trains")
    if isinstance(trains, dict) and trains:
        first = trains.get("0", next(iter(trains.values())))
        return str(first)
    return None


def _resource_setting_version(resources_dir: Path | None, index: FileIndex) -> int | None:
    if resources_dir is None:
        return None
    path = index.get("fdmprinter")
    if path is None or not path.name.endswith(".def.json"):
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    metadata = data.get("metadata")
    if isinstance(metadata, dict):
        return _optional_int(metadata.get("setting_version"))
    return None


def _extruder_trains(metadata: configparser.SectionProxy | dict[str, str]) -> list[str]:
    raw = metadata.get("machine_extruder_trains") if hasattr(metadata, "get") else None
    if not raw:
        return []
    text = str(raw).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed = json.loads(text.replace("'", '"'))
        except json.JSONDecodeError:
            return []
    if not isinstance(parsed, dict):
        return []
    ordered = []
    for key in sorted(parsed, key=lambda item: int(item) if str(item).isdigit() else str(item)):
        ordered.append(str(parsed[key]))
    return ordered


def _user_source(config_dir: Path, path: Path) -> list[str]:
    try:
        path.resolve().relative_to(config_dir.resolve())
    except ValueError:
        return []
    return [_relative(config_dir, path)]


def _relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value))
    except ValueError:
        return None


def _as_float(value: object, default: float | None) -> float | None:
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text or text.startswith("=") or any(char.isalpha() for char in text):
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    text = str(value).strip().lower()
    if text in {"true", "yes", "1", "on"}:
        return True
    if text in {"false", "no", "0", "off"}:
        return False
    return default


def _coerce_geometry(key: str, value: str) -> object:
    if key == "machine_center_is_zero":
        return _as_bool(value, False)
    if key == "machine_extruder_count":
        parsed = _as_float(value, None)
        return int(parsed) if parsed is not None else value
    parsed = _as_float(value, None)
    return parsed if parsed is not None else value


def join_search_path(directories: list[Path]) -> str:
    return os.pathsep.join(str(path) for path in directories)
