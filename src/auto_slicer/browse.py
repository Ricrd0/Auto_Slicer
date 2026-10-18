from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from auto_slicer.store import Store

_MODEL_SUFFIXES = {".stl", ".3mf"}
_SKIP_DIRS = {"$recycle.bin", "system volume information"}


@dataclass(frozen=True)
class Mount:
    host: str
    local: Path


def mounts_from_env() -> list[Mount]:
    explicit = _mounts_from_env_string()
    explicit_hosts = {item.host.lower() for item in explicit}
    found = [
        mount
        for mount in _mounts_from_host_drives() + _mounts_from_bind_paths()
        if mount.host.lower() not in explicit_hosts
    ]
    found.extend(explicit)
    found.sort(key=lambda item: len(item.host), reverse=True)
    return found


def browse(path: str, extra_roots: list[Path]) -> dict[str, object]:
    mounts = mounts_from_env()
    roots = _roots(mounts, extra_roots)
    if not path.strip():
        entries = [
            {"name": _root_label(root, mounts), "path": to_display(root, mounts), "kind": "dir"}
            for root in roots
        ]
        return {"path": "", "parent": None, "entries": entries}
    local = resolve_allowed_dir(path, extra_roots)
    parent = _parent_display(local, roots, mounts)
    entries: list[dict[str, str]] = []
    try:
        children = list(local.iterdir())
    except OSError as exc:
        raise ValueError("folder cannot be read") from exc
    for child in children:
        try:
            is_dir = child.is_dir()
        except OSError:
            continue
        if is_dir:
            if child.name.startswith(".") or child.name.lower() in _SKIP_DIRS:
                continue
            entries.append({"name": child.name, "path": to_display(child, mounts), "kind": "dir"})
        elif child.suffix.lower() in _MODEL_SUFFIXES:
            entries.append({"name": child.name, "path": to_display(child, mounts), "kind": "file"})
    entries.sort(key=lambda item: (item["kind"] != "dir", item["name"].lower()))
    return {"path": to_display(local, mounts), "parent": parent, "entries": entries}


def resolve_allowed_dir(path: str, extra_roots: list[Path]) -> Path:
    """Resolve a UI display path to a local directory under allowed roots."""
    mounts = mounts_from_env()
    roots = _roots(mounts, extra_roots)
    local = _allowed(to_local(path, mounts), roots)
    if not local.is_dir():
        raise ValueError("folder was not found")
    return local


def model_root(store: Store) -> Path:
    return _stored_dir(store, "input_dir", store.paths.input_dir, create=False)


def gcode_root(store: Store) -> Path:
    return _stored_dir(store, "output_dir", store.paths.output_dir, create=True)


def public_locations(store: Store) -> dict[str, object]:
    data = store.load_locations()
    mounts = mounts_from_env()
    input_dir = str(data.get("input_dir") or "") or to_display(store.paths.input_dir, mounts)
    output_dir = str(data.get("output_dir") or "") or to_display(store.paths.output_dir, mounts)
    return {
        "input_dir": input_dir,
        "output_dir": output_dir,
        "included": data.get("included"),
    }


def update_locations(store: Store, payload: dict[str, object]) -> dict[str, object]:
    current = store.load_locations()
    mounts = mounts_from_env()
    roots = _roots(mounts, [store.paths.input_dir, store.paths.output_dir])
    if "input_dir" in payload:
        assigned = _assign_dir(payload.get("input_dir"), mounts, roots, create=False)
        if assigned != current.get("input_dir"):
            # New folders start with nothing selected for individual slicing.
            current["included"] = []
        current["input_dir"] = assigned
    if "output_dir" in payload:
        current["output_dir"] = _assign_dir(payload.get("output_dir"), mounts, roots, create=True)
    if "included" in payload:
        included = payload.get("included")
        if included is None:
            current["included"] = []
        elif isinstance(included, list):
            current["included"] = [str(item) for item in included]
        else:
            raise ValueError("included must be a list of files")
    store.save_locations(current)
    return public_locations(store)


def to_display(path: Path, mounts: list[Mount]) -> str:
    ordered = sorted(mounts, key=lambda item: len(_path_key(item.local)), reverse=True)
    for mount in ordered:
        rest = _relative_to(path, mount.local)
        if rest is None:
            continue
        if rest == "":
            label = mount.host.rstrip("\\")
            if _looks_like_windows(label) and len(label) == 2:
                return label + "\\"
            return label
        return mount.host + rest.replace("/", "\\")
    text = str(path)
    if os.name == "nt":
        return text
    return text.replace("\\", "/")


def to_local(display: str, mounts: list[Mount]) -> Path:
    text = display.strip()
    if not text:
        raise ValueError("path is empty")
    host = _host_key(text)
    for mount in mounts:
        matched = _consume_prefix(host, mount.host)
        if matched is None:
            continue
        parts = [part for part in matched.replace("/", "\\").split("\\") if part and part != "."]
        if any(part == ".." for part in parts):
            raise ValueError("path cannot contain ..")
        return mount.local.joinpath(*parts) if parts else mount.local
    candidate = Path(host if _looks_like_windows(host) and os.name == "nt" else text)
    if candidate.is_absolute():
        return candidate
    raise ValueError(f"folder is not available: {display}")


def _stored_dir(store: Store, field: str, fallback: Path, create: bool) -> Path:
    raw = str(store.load_locations().get(field) or "")
    if not raw:
        return fallback
    mounts = mounts_from_env()
    roots = _roots(mounts, [store.paths.input_dir, store.paths.output_dir])
    local = _allowed(to_local(raw, mounts), roots)
    if create and not local.exists():
        if not local.parent.is_dir():
            raise ValueError("folder was not found")
        local.mkdir()
    if not local.is_dir():
        raise ValueError("folder was not found")
    return local


def _assign_dir(value: object, mounts: list[Mount], roots: list[Path], create: bool) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    local = _allowed(to_local(text, mounts), roots)
    if create and not local.exists():
        if not local.parent.is_dir():
            raise ValueError("folder was not found")
        local.mkdir()
    if not local.is_dir():
        raise ValueError("folder was not found")
    return to_display(local, mounts)


def _roots(mounts: list[Mount], extra_roots: list[Path]) -> list[Path]:
    roots: list[Path] = []
    for mount in mounts:
        if mount.local.exists():
            _add_root(roots, mount.local)
    for extra in extra_roots:
        if extra.exists():
            _add_root(roots, extra)
    if not mounts and os.name == "nt":
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            drive = Path(f"{letter}:\\")
            if drive.exists():
                _add_root(roots, drive)
    return roots


def _add_root(roots: list[Path], path: Path) -> None:
    if all(existing != path for existing in roots):
        roots.append(path)


def _allowed(path: Path, roots: list[Path]) -> Path:
    for root in roots:
        if _relative_to(path, root) is not None:
            return path
    raise ValueError("that folder is outside the folders this app can open")


def _parent_display(path: Path, roots: list[Path], mounts: list[Mount]) -> str | None:
    if any(_same(path, root) for root in roots):
        return ""
    parent = path.parent
    if parent == path:
        return None
    if any(_relative_to(parent, root) is not None for root in roots):
        return to_display(parent, mounts)
    return ""


def _root_label(root: Path, mounts: list[Mount]) -> str:
    display = to_display(root, mounts)
    return display or root.name or str(root)


def _mounts_from_env_string() -> list[Mount]:
    raw = os.environ.get("AUTO_SLICER_HOST_MOUNTS", "")
    found: list[Mount] = []
    for piece in raw.split(";"):
        if "=" not in piece:
            continue
        host, local = piece.split("=", 1)
        prefix = _host_prefix(host)
        local_path = Path(local.strip())
        if prefix and str(local_path):
            found.append(Mount(prefix, local_path))
    return found


def _mounts_from_host_drives() -> list[Mount]:
    raw = os.environ.get("AUTO_SLICER_HOST_DRIVES", "").strip()
    if not raw:
        return []
    root = Path(raw)
    if not root.is_dir():
        return []
    try:
        children = list(root.iterdir())
    except OSError:
        return []
    found: list[Mount] = []
    for child in children:
        name = child.name
        if len(name) != 1 or name.lower() not in "abcdefghijklmnopqrstuvwxyz":
            continue
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        found.append(Mount(_host_prefix(f"{name.upper()}:\\"), child))
    found.sort(key=lambda item: item.host.lower())
    return found


def _mounts_from_bind_paths() -> list[Mount]:
    wanted: set[str] = set()
    for key in ("AUTO_SLICER_INPUT", "AUTO_SLICER_OUTPUT"):
        raw = os.environ.get(key, "").strip()
        if raw:
            wanted.add(_path_key(Path(raw)))
    if not wanted:
        return []
    found: list[Mount] = []
    for mount_point, root, source, options in _parse_mountinfo(_mountinfo_text()):
        if _path_key(Path(mount_point)) not in wanted:
            continue
        mount = _windows_bind_mount(mount_point, root, source, options)
        if mount is not None:
            found.append(mount)
    return found


def _mountinfo_text() -> str:
    path = Path("/proc/self/mountinfo")
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _parse_mountinfo(text: str) -> list[tuple[str, str, str, str]]:
    records: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        left_fields = left.split()
        right_fields = right.split()
        if len(left_fields) < 5 or len(right_fields) < 2:
            continue
        options = _unescape_mount_field(right_fields[2]) if len(right_fields) > 2 else ""
        records.append(
            (
                _unescape_mount_field(left_fields[4]),
                _unescape_mount_field(left_fields[3]),
                _unescape_mount_field(right_fields[1]),
                options,
            )
        )
    return records


def _windows_bind_mount(mount_point: str, root: str, source: str, options: str) -> Mount | None:
    letter = _drive_letter(source, options)
    if letter is None:
        return None
    local = Path(mount_point)
    if not str(local):
        return None
    return Mount(_host_prefix(_windows_host(letter, root)), local)


def _drive_letter(source: str, options: str) -> str | None:
    matched = re.search(r"path=([A-Za-z]):", options)
    if matched:
        return matched.group(1).upper()
    matched = re.search(r"^([A-Za-z]):", source)
    if matched:
        return matched.group(1).upper()
    return None


def _windows_host(letter: str, root: str) -> str:
    rest = root.replace("/", "\\").strip("\\")
    if not rest:
        return f"{letter}:\\"
    return f"{letter}:\\{rest}"


def _unescape_mount_field(value: str) -> str:
    parts: list[str] = []
    index = 0
    while index < len(value):
        escaped = value[index] == "\\" and index + 3 < len(value) and value[index + 1 : index + 4].isdigit()
        if escaped:
            parts.append(chr(int(value[index + 1 : index + 4], 8)))
            index += 4
            continue
        parts.append(value[index])
        index += 1
    return "".join(parts)


def _host_prefix(value: str) -> str:
    text = _host_key(value)
    if not text.endswith("\\"):
        text += "\\"
    return text


def _host_key(value: str) -> str:
    text = value.strip().replace("/", "\\")
    if _looks_like_windows(text):
        text = text[0].upper() + text[1:]
    return text


def _looks_like_windows(value: str) -> bool:
    return len(value) >= 2 and value[1] == ":" and value[0].isalpha()


def _consume_prefix(host: str, prefix: str) -> str | None:
    left = host.rstrip("\\")
    right = prefix.rstrip("\\")
    if left.lower() == right.lower():
        return ""
    if host.lower().startswith(prefix.lower()):
        return host[len(prefix) :].lstrip("\\")
    return None


def _relative_to(path: Path, root: Path) -> str | None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        try:
            relative = path.absolute().relative_to(root.absolute())
        except (OSError, ValueError):
            return None
    text = relative.as_posix()
    if text == ".":
        return ""
    return text


def _same(path: Path, root: Path) -> bool:
    return _relative_to(path, root) == ""


def _path_key(path: Path) -> str:
    return str(path).replace("\\", "/").rstrip("/")
