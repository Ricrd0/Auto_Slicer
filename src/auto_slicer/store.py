from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any

from auto_slicer.paths import DataPaths
from auto_slicer.settings_schema import SliceSettings


class Store:
    def __init__(self, paths: DataPaths) -> None:
        self.paths = paths
        self._lock = threading.Lock()
        self._db = sqlite3.connect(paths.app_dir / "auto_slicer.db", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                output_folder TEXT,
                created_at TEXT NOT NULL,
                finished_at TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS job_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL,
                printer_id TEXT NOT NULL,
                printer_name TEXT NOT NULL,
                item_type TEXT NOT NULL,
                item_name TEXT NOT NULL,
                status TEXT NOT NULL,
                progress TEXT,
                output_path TEXT,
                time_seconds INTEGER,
                filament_meters REAL,
                error TEXT
            );
            """
        )
        self._db.commit()

    def load_settings(self) -> SliceSettings:
        path = self.paths.app_dir / "settings.json"
        if not path.is_file():
            return SliceSettings()
        return SliceSettings.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_settings(self, settings: SliceSettings) -> None:
        settings.validate()
        self._write_json(self.paths.app_dir / "settings.json", settings.to_dict())

    def load_enabled(self) -> dict[str, bool]:
        path = self.paths.app_dir / "printers.json"
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(key): bool(value) for key, value in data.items()}

    def set_enabled(self, printer_id: str, enabled: bool) -> None:
        current = self.load_enabled()
        current[printer_id] = enabled
        self._write_json(self.paths.app_dir / "printers.json", current)

    def load_poses(self) -> dict[str, dict[str, Any]]:
        path = self.paths.app_dir / "orientations.json"
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}

    def save_pose(self, relative: str, pose: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
        poses = self.load_poses()
        if pose is None:
            poses.pop(relative, None)
        else:
            poses[relative] = pose
        self._write_json(self.paths.app_dir / "orientations.json", poses)
        return poses

    def load_groups(self) -> list[dict[str, Any]]:
        path = self.paths.app_dir / "groups.json"
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        groups = data.get("groups", []) if isinstance(data, dict) else []
        return groups if isinstance(groups, list) else []

    def save_groups(self, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        normalized = [_normalize_group(group) for group in groups]
        self._write_json(self.paths.app_dir / "groups.json", {"groups": normalized})
        return normalized

    def create_job(self, kind: str, output_folder: str) -> str:
        job_id = uuid.uuid4().hex
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, kind, status, output_folder, created_at) VALUES (?, ?, ?, ?, datetime('now'))",
                (job_id, kind, "queued", output_folder),
            )
            self._db.commit()
        return job_id

    def add_item(
        self,
        job_id: str,
        printer_id: str,
        printer_name: str,
        item_type: str,
        item_name: str,
        output_path: str,
    ) -> int:
        with self._lock:
            cursor = self._db.execute(
                """
                INSERT INTO job_items (
                    job_id, printer_id, printer_name, item_type, item_name, status, output_path
                ) VALUES (?, ?, ?, ?, ?, 'queued', ?)
                """,
                (job_id, printer_id, printer_name, item_type, item_name, output_path),
            )
            self._db.commit()
            return int(cursor.lastrowid)

    def update_job(self, job_id: str, status: str, error: str | None = None, finished: bool = False) -> None:
        with self._lock:
            if finished:
                self._db.execute(
                    "UPDATE jobs SET status = ?, error = ?, finished_at = datetime('now') WHERE id = ?",
                    (status, error, job_id),
                )
            else:
                self._db.execute(
                    "UPDATE jobs SET status = ?, error = ? WHERE id = ?",
                    (status, error, job_id),
                )
            self._db.commit()

    def update_item(self, item_id: int, **fields: object) -> None:
        if not fields:
            return
        columns = ", ".join(f"{key} = ?" for key in fields)
        with self._lock:
            self._db.execute(
                f"UPDATE job_items SET {columns} WHERE id = ?",
                (*fields.values(), item_id),
            )
            self._db.commit()

    def job_record(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            items = self._db.execute(
                "SELECT * FROM job_items WHERE job_id = ? ORDER BY id", (job_id,)
            ).fetchall()
        return _job_dict(row, items)

    def latest_job(self) -> dict[str, Any] | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            items = self._db.execute(
                "SELECT * FROM job_items WHERE job_id = ? ORDER BY id", (row["id"],)
            ).fetchall()
        return _job_dict(row, items)

    def list_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
            result = []
            for row in rows:
                items = self._db.execute(
                    "SELECT * FROM job_items WHERE job_id = ? ORDER BY id", (row["id"],)
                ).fetchall()
                result.append(_job_dict(row, items))
        return result

    def load_locations(self) -> dict[str, Any]:
        path = self.paths.app_dir / "locations.json"
        data: dict[str, Any] = {"input_dir": "", "output_dir": "", "included": None}
        if not path.is_file():
            return data
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return data
        data["input_dir"] = str(raw.get("input_dir") or "")
        data["output_dir"] = str(raw.get("output_dir") or "")
        included = raw.get("included")
        data["included"] = [str(item) for item in included] if isinstance(included, list) else None
        return data

    def save_locations(self, locations: dict[str, Any]) -> dict[str, Any]:
        included = locations.get("included")
        payload = {
            "input_dir": str(locations.get("input_dir") or ""),
            "output_dir": str(locations.get("output_dir") or ""),
            "included": [str(item) for item in included] if isinstance(included, list) else None,
        }
        self._write_json(self.paths.app_dir / "locations.json", payload)
        return payload

    def _write_json(self, path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _normalize_group(group: dict[str, Any]) -> dict[str, Any]:
    files = group.get("files") or []
    manual = group.get("manual_layout")
    normalized_manual = None
    if isinstance(manual, dict):
        normalized_manual = {}
        for name, position in manual.items():
            if isinstance(position, dict) and "x" in position and "y" in position:
                normalized_manual[str(name)] = {"x": float(position["x"]), "y": float(position["y"])}
    return {
        "id": str(group.get("id") or uuid.uuid4().hex),
        "name": str(group.get("name") or "group"),
        "files": [str(item) for item in files],
        "manual_layout": normalized_manual,
    }


def _job_dict(row: sqlite3.Row, items: list[sqlite3.Row]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "status": row["status"],
        "output_folder": row["output_folder"],
        "created_at": row["created_at"],
        "finished_at": row["finished_at"],
        "error": row["error"],
        "items": [
            {
                "id": item["id"],
                "printer_id": item["printer_id"],
                "printer_name": item["printer_name"],
                "item_type": item["item_type"],
                "item_name": item["item_name"],
                "status": item["status"],
                "progress": item["progress"],
                "output_path": item["output_path"],
                "time_seconds": item["time_seconds"],
                "filament_meters": item["filament_meters"],
                "error": item["error"],
            }
            for item in items
        ],
    }
