"""Shared printer profiles that travel with the project.

Local machines keep their own owned-printer file. A master file holds the
shared machine setup (not network credentials). Each owned printer chooses:

- ``master`` — slice with the shared profile
- ``local`` — slice with this machine's copy
- ``author`` — this machine's copy is the master and overwrites it on save
"""

from __future__ import annotations

import json
import os
from pathlib import Path

PROFILE_MODES: tuple[str, ...] = ("master", "local", "author")

_PROFILE_KEYS = ("settings", "cura_settings", "orca_settings")


def profile_mode_of(data: dict[str, object] | None, default: str = "master") -> str:
    raw = default
    if isinstance(data, dict) and data.get("profile_mode") not in (None, ""):
        raw = str(data.get("profile_mode"))
    mode = str(raw or default).strip().lower()
    if mode not in PROFILE_MODES:
        raise ValueError(f"profile_mode must be one of {', '.join(PROFILE_MODES)}")
    return mode


def master_profiles_path() -> Path:
    """Writable shared master file.

    ``AUTO_SLICER_MASTER_PROFILES`` wins (set in Docker to a mounted repo file).
    Otherwise the copy next to the source tree, then the image install path.
    """
    override = os.environ.get("AUTO_SLICER_MASTER_PROFILES")
    if override:
        return Path(override)
    candidates = [
        Path(__file__).resolve().parents[2] / "resources" / "master_profiles.json",
        Path("/opt/auto-slicer/resources/master_profiles.json"),
    ]
    for candidate in candidates:
        if candidate.is_file() or candidate.parent.is_dir():
            return candidate
    return candidates[0]


def load_master_profiles(path: Path | None = None) -> list[dict[str, object]]:
    file_path = path or master_profiles_path()
    if not file_path.is_file():
        return []
    data = json.loads(file_path.read_text(encoding="utf-8"))
    printers = data.get("printers", []) if isinstance(data, dict) else []
    if not isinstance(printers, list):
        return []
    return [item for item in printers if isinstance(item, dict)]


def save_master_profiles(printers: list[dict[str, object]], path: Path | None = None) -> None:
    file_path = path or master_profiles_path()
    file_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"printers": [master_entry(item) for item in printers]}
    file_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def master_entry(record: dict[str, object]) -> dict[str, object]:
    """Shared fields only. Transfer credentials stay on the local machine."""
    return {
        "id": record.get("id"),
        "name": record.get("name"),
        "cura_id": record.get("cura_id"),
        "orca_id": record.get("orca_id"),
        "settings": record.get("settings"),
        "cura_settings": record.get("cura_settings"),
        "orca_settings": record.get("orca_settings"),
    }


def adopt_master_printers(
    owned: list[dict[str, object]],
    masters: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    """Return owned printers plus any shared printer this machine does not have.

    New records follow the master profile and carry no transfer credentials.
    Printers already stored here are left as they are, including profile mode.
    """
    library = masters if masters is not None else load_master_profiles()
    combined = [item for item in owned if isinstance(item, dict)]
    if not library:
        return combined
    for master in library:
        if match_master(master, combined) is not None:
            continue
        name = str(master.get("name") or "").strip()
        if not name:
            continue
        settings = master.get("settings") if isinstance(master.get("settings"), dict) else {}
        combined.append(
            {
                "id": master.get("id") or f"owned:{name}",
                "name": name,
                "cura_id": master.get("cura_id"),
                "orca_id": master.get("orca_id"),
                "settings": settings,
                "cura_settings": master.get("cura_settings"),
                "orca_settings": master.get("orca_settings"),
                "profile_mode": "master",
            }
        )
    return combined


def match_master(
    record: dict[str, object], masters: list[dict[str, object]]
) -> dict[str, object] | None:
    wanted_id = str(record.get("id") or "")
    orca_id = str(record.get("orca_id") or "")
    cura_id = str(record.get("cura_id") or "")
    name = str(record.get("name") or "").strip().lower()
    for master in masters:
        if wanted_id and str(master.get("id") or "") == wanted_id:
            return master
    for master in masters:
        if orca_id and str(master.get("orca_id") or "") == orca_id:
            return master
    for master in masters:
        if cura_id and str(master.get("cura_id") or "") == cura_id:
            return master
    for master in masters:
        if name and str(master.get("name") or "").strip().lower() == name:
            return master
    return None


def apply_master_profile(
    record: dict[str, object],
    masters: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """Return the record used for slicing and for the settings form.

    ``local`` and ``author`` keep this machine's settings. ``master`` overlays
    the shared machine setup and leaves this machine's transfer config in place.
    """
    mode = profile_mode_of(record)
    library = masters if masters is not None else load_master_profiles()
    master = match_master(record, library)
    if mode != "master":
        viewed = dict(record)
        viewed["master_available"] = master is not None
        return viewed
    if master is None:
        viewed = dict(record)
        viewed["master_available"] = False
        return viewed
    merged = dict(record)
    for key in _PROFILE_KEYS:
        if key in master:
            merged[key] = master.get(key)
    merged["profile_mode"] = "master"
    merged["master_available"] = True
    return merged


def form_was_showing_master(existing: dict[str, object] | None, mode: str) -> bool:
    """The settings form shows the shared profile while mode is master.

    Saves in that state, and the save that leaves it, must keep the stored
    local machine setup. Otherwise the overlay would replace the private copy,
    or "use as master" would republish the shared file instead of this machine.
    """
    if existing is None:
        return False
    if mode == "master":
        return True
    return profile_mode_of(existing) == "master"


def publish_master_profile(
    record: dict[str, object],
    path: Path | None = None,
) -> list[dict[str, object]]:
    """Replace or insert this printer in the shared master file."""
    file_path = path or master_profiles_path()
    masters = load_master_profiles(file_path)
    entry = master_entry(record)
    replaced = False
    updated: list[dict[str, object]] = []
    for item in masters:
        if _same_printer(item, entry):
            updated.append(entry)
            replaced = True
        else:
            updated.append(item)
    if not replaced:
        updated.append(entry)
    save_master_profiles(updated, file_path)
    return updated


def _same_printer(left: dict[str, object], right: dict[str, object]) -> bool:
    if left.get("id") and left.get("id") == right.get("id"):
        return True
    if left.get("orca_id") and left.get("orca_id") == right.get("orca_id"):
        return True
    if left.get("cura_id") and left.get("cura_id") == right.get("cura_id"):
        return True
    left_name = str(left.get("name") or "").strip().lower()
    right_name = str(right.get("name") or "").strip().lower()
    return bool(left_name) and left_name == right_name
