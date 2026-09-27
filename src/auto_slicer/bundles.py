from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.paths import safe_relative


def export_printer(printer: PrinterProfile, config_dir: Path) -> bytes:
    return _zip_members(_members_for_printer(printer, config_dir))


def export_all(printers: list[PrinterProfile], config_dir: Path) -> bytes:
    members: list[tuple[str, bytes]] = []
    for printer in printers:
        for name, payload in _members_for_printer(printer, config_dir):
            members.append((f"printers/{_folder(printer.id)}/{name}", payload))
    return _zip_members(members)


def import_bundle(payload: bytes, config_dir: Path) -> list[str]:
    config_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if not name.endswith("/")]
        if "manifest.json" in names:
            _extract_one(archive, "", config_dir)
            manifest = json.loads(archive.read("manifest.json"))
            return [str(manifest["machine_id"])]
        imported: list[str] = []
        prefixes = sorted({name.split("/", 2)[1] for name in names if name.startswith("printers/") and name.count("/") >= 2})
        if not prefixes:
            raise ValueError("bundle is missing manifest.json")
        for prefix in prefixes:
            root = f"printers/{prefix}/"
            manifest_name = root + "manifest.json"
            if manifest_name not in names:
                raise ValueError(f"bundle entry {prefix} is missing manifest.json")
            _extract_one(archive, root, config_dir)
            manifest = json.loads(archive.read(manifest_name))
            imported.append(str(manifest["machine_id"]))
        return imported


def _members_for_printer(printer: PrinterProfile, config_dir: Path) -> list[tuple[str, bytes]]:
    if printer.config_root:
        config_dir = Path(printer.config_root)
    files: list[str] = []
    for relative in printer.source_files:
        path = config_dir / relative
        if path.is_file():
            files.append(relative)
    manifest = {
        "machine_id": printer.id,
        "name": printer.name,
        "setting_version": printer.setting_version,
        "files": files,
    }
    members = [("manifest.json", json.dumps(manifest, indent=2).encode("utf-8"))]
    for relative in files:
        members.append((f"files/{relative}", (config_dir / relative).read_bytes()))
    return members


def _extract_one(archive: zipfile.ZipFile, prefix: str, config_dir: Path) -> None:
    manifest_name = f"{prefix}manifest.json"
    manifest = json.loads(archive.read(manifest_name))
    files = manifest.get("files")
    if not isinstance(files, list) or not manifest.get("machine_id"):
        raise ValueError("bundle manifest is invalid")
    for relative in files:
        if not isinstance(relative, str):
            raise ValueError("bundle file list is invalid")
        safe_relative(relative)
        bundled = f"{prefix}files/{relative}"
        try:
            payload = archive.read(bundled)
        except KeyError as exc:
            raise ValueError(f"bundle is missing {relative}") from exc
        destination = config_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)


def _zip_members(members: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in members:
            archive.writestr(name, payload)
    return buffer.getvalue()


def _folder(machine_id: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in machine_id) or "printer"
