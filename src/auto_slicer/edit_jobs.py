"""In-memory batch G-code edit jobs with progress for the UI."""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from auto_slicer.browse import mounts_from_env, resolve_allowed_dir, to_display
from auto_slicer.gcode_edit import (
    batch_edit_files,
    default_workers,
    list_gcode_files,
    resolve_replacement,
)
from auto_slicer.store import Store


@dataclass
class EditJob:
    id: str
    status: str
    mode: str
    directory: str
    find: str = ""
    replace: str = ""
    dry_run: bool = False
    backup: bool = False
    workers: int = 1
    assume_relative_e: bool = False
    total: int = 0
    processed: int = 0
    modified: int = 0
    replacements: int = 0
    errors: list[str] = field(default_factory=list)
    message: str = ""
    current_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        percent = int(round(100 * self.processed / self.total)) if self.total else 0
        if self.status == "done" and self.total:
            percent = 100
        return {
            "id": self.id,
            "status": self.status,
            "mode": self.mode,
            "directory": self.directory,
            "find": self.find,
            "replace": self.replace,
            "dry_run": self.dry_run,
            "backup": self.backup,
            "workers": self.workers,
            "assume_relative_e": self.assume_relative_e,
            "total": self.total,
            "processed": self.processed,
            "modified": self.modified,
            "replacements": self.replacements,
            "errors": list(self.errors[:50]),
            "error_count": len(self.errors),
            "message": self.message,
            "current_file": self.current_file,
            "percent": percent,
        }


class EditJobRunner:
    """Runs one batch edit at a time (independent of slice jobs)."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._job: EditJob | None = None

    def current(self) -> dict[str, Any]:
        with self._lock:
            return self._job.to_dict() if self._job is not None else {}

    def stop(self) -> dict[str, Any]:
        self._cancel.set()
        with self._lock:
            if self._job is not None and self._job.status in {"queued", "running"}:
                self._job.status = "stopped"
                self._job.message = "Stopping…"
            return self._job.to_dict() if self._job is not None else {}

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a gcode edit job is already running")
            self._cancel.clear()
            mode = str(payload.get("mode") or "replace").strip().lower()
            if mode not in {"replace", "sanitize"}:
                raise ValueError("mode must be replace or sanitize")
            directory = str(payload.get("directory") or "").strip()
            if not directory:
                raise ValueError("directory is required")
            find = str(payload.get("find") or "")
            replace_raw = str(payload.get("replace") or "")
            dry_run = bool(payload.get("dry_run", False))
            backup = bool(payload.get("backup", False))
            assume_relative = bool(payload.get("assume_relative_e", False))
            workers = int(payload.get("workers") or default_workers())
            workers = max(1, min(workers, 32))
            if mode == "replace" and not find:
                raise ValueError("find text is required")
            replace = replace_raw
            if mode == "replace" and replace_raw:
                try:
                    replace = resolve_replacement(replace_raw)
                except OSError as exc:
                    raise ValueError(f"could not read replacement file: {exc}") from exc
            job = EditJob(
                id=uuid.uuid4().hex,
                status="queued",
                mode=mode,
                directory=directory,
                find=find,
                replace=replace_raw if mode == "replace" else "",
                dry_run=dry_run,
                backup=backup,
                workers=workers,
                assume_relative_e=assume_relative,
                message="Scanning for .gcode files…",
            )
            self._job = job
            self._thread = threading.Thread(
                target=self._run,
                args=(job, replace if mode == "replace" else ""),
                daemon=True,
            )
            self._thread.start()
            return job.to_dict()

    def _run(self, job: EditJob, replace_text: str) -> None:
        try:
            root = resolve_allowed_dir(
                job.directory,
                [self.store.paths.input_dir, self.store.paths.output_dir],
            )
            with self._lock:
                job.directory = to_display(root, mounts_from_env())
            files = list_gcode_files(root)
            with self._lock:
                job.total = len(files)
                job.status = "running"
                job.message = (
                    f"Found {len(files)} .gcode file(s)"
                    + (" (dry run)" if job.dry_run else "")
                )
            if not files:
                with self._lock:
                    job.status = "done"
                    job.message = "No .gcode files found under that folder"
                return
            if self._cancel.is_set():
                with self._lock:
                    job.status = "stopped"
                    job.message = "Stopped before processing"
                return

            def on_progress(processed: int, total: int, path: Path | None) -> None:
                with self._lock:
                    job.processed = processed
                    job.total = total
                    if path is not None:
                        job.current_file = path.name

            result = batch_edit_files(
                files,
                mode=job.mode,
                find_str=job.find,
                replace_str=replace_text,
                dry_run=job.dry_run,
                backup=job.backup,
                workers=job.workers,
                assume_relative_e=job.assume_relative_e,
                on_progress=on_progress,
                should_cancel=self._cancel.is_set,
            )
            with self._lock:
                job.processed = result.processed
                job.modified = result.modified
                job.replacements = result.replacements
                job.errors = list(result.errors)
                job.current_file = None
                if result.cancelled or self._cancel.is_set():
                    job.status = "stopped"
                    job.message = (
                        f"Stopped after {result.processed}/{job.total} file(s); "
                        f"modified {result.modified}"
                    )
                else:
                    job.status = "done"
                    mode_label = "dry run — " if job.dry_run else ""
                    job.message = (
                        f"{mode_label}Processed {result.processed} file(s). "
                        f"Modified {result.modified}. "
                        f"Replacements: {result.replacements}."
                    )
                    if result.errors:
                        job.message += f" Errors: {len(result.errors)}."
        except (OSError, ValueError) as exc:
            with self._lock:
                job.status = "failed"
                job.message = str(exc)
                job.errors = [str(exc)]
