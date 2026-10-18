"""Batch find/replace and extrusion fixes for generated G-code files.

Adapted from the standalone ``gcode_editor.py`` used under
``H:\\Projects\\Scripts\\GCode Editors``.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from auto_slicer.gcode import sanitize_relative_extrusion_script

ProgressCallback = Callable[[int, int, Path | None], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True)
class FileEditResult:
    path: Path
    modified: bool
    replacements: int
    error: str | None = None


@dataclass(frozen=True)
class BatchEditResult:
    processed: int
    modified: int
    replacements: int
    errors: tuple[str, ...]
    cancelled: bool = False


def resolve_replacement(value: str) -> str:
    """Use literal text, or read a ``.gcode`` file when ``value`` points at one."""
    replacement_path = Path(value)
    if replacement_path.suffix.lower() != ".gcode" or not replacement_path.is_file():
        return value
    return replacement_path.read_text(encoding="utf-8")


def process_replace_file(
    filepath: Path,
    find_str: str,
    replace_str: str,
    *,
    dry_run: bool,
    backup: bool,
) -> FileEditResult:
    """Find/replace in one G-code file."""
    if not find_str:
        return FileEditResult(filepath, False, 0, error="find string is empty")
    try:
        content = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        return FileEditResult(filepath, False, 0, error=str(exc))
    if find_str not in content:
        return FileEditResult(filepath, False, 0)
    count = content.count(find_str)
    new_content = content.replace(find_str, replace_str)
    if not dry_run:
        try:
            if backup:
                filepath.with_suffix(filepath.suffix + ".bak").write_text(content, encoding="utf-8")
            filepath.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            return FileEditResult(filepath, False, 0, error=str(exc))
    return FileEditResult(filepath, True, count)


def process_sanitize_file(
    filepath: Path,
    *,
    dry_run: bool,
    backup: bool,
    assume_relative_e: bool,
    nozzle_diameter: float = 0.4,
) -> FileEditResult:
    """Rewrite absolute-style primes that over-extrude under relative E."""
    try:
        content = filepath.read_text(encoding="utf-8")
    except OSError as exc:
        return FileEditResult(filepath, False, 0, error=str(exc))
    relative = assume_relative_e or ("M83" in content.upper())
    fixed = sanitize_relative_extrusion_script(
        content,
        assume_relative_e=relative,
        nozzle_diameter=nozzle_diameter,
    )
    if fixed == content:
        return FileEditResult(filepath, False, 0)
    if not dry_run:
        try:
            if backup:
                filepath.with_suffix(filepath.suffix + ".bak").write_text(content, encoding="utf-8")
            filepath.write_text(fixed, encoding="utf-8")
        except OSError as exc:
            return FileEditResult(filepath, False, 0, error=str(exc))
    return FileEditResult(filepath, True, 1)


def list_gcode_files(root: Path) -> list[Path]:
    if not root.is_dir():
        raise ValueError(f"directory was not found: {root}")
    return sorted(path for path in root.rglob("*.gcode") if path.is_file())


def default_workers() -> int:
    return max(1, (os.cpu_count() or 2) - 1)


def batch_edit_files(
    files: list[Path],
    *,
    mode: str,
    find_str: str = "",
    replace_str: str = "",
    dry_run: bool = False,
    backup: bool = False,
    workers: int | None = None,
    assume_relative_e: bool = False,
    nozzle_diameter: float = 0.4,
    on_progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> BatchEditResult:
    """Process many G-code files with find/replace or extrusion sanitize."""
    if mode not in {"replace", "sanitize"}:
        raise ValueError("mode must be replace or sanitize")
    if mode == "replace" and not find_str:
        raise ValueError("find string is required for replace mode")
    total = len(files)
    if total == 0:
        return BatchEditResult(0, 0, 0, ())
    worker_count = max(1, int(workers if workers is not None else default_workers()))
    processed = 0
    modified = 0
    replacements = 0
    errors: list[str] = []
    cancelled = False

    def _one(path: Path) -> FileEditResult:
        if mode == "sanitize":
            return process_sanitize_file(
                path,
                dry_run=dry_run,
                backup=backup,
                assume_relative_e=assume_relative_e,
                nozzle_diameter=nozzle_diameter,
            )
        return process_replace_file(
            path,
            find_str,
            replace_str,
            dry_run=dry_run,
            backup=backup,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = {pool.submit(_one, path): path for path in files}
        for future in as_completed(futures):
            if should_cancel is not None and should_cancel():
                cancelled = True
                for pending in futures:
                    pending.cancel()
                break
            path = futures[future]
            processed += 1
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 — surface per-file failures
                errors.append(f"{path}: {exc}")
                if on_progress is not None:
                    on_progress(processed, total, path)
                continue
            if result.error:
                errors.append(f"{result.path}: {result.error}")
            elif result.modified:
                modified += 1
                replacements += result.replacements
            if on_progress is not None:
                on_progress(processed, total, path)
    return BatchEditResult(
        processed=processed,
        modified=modified,
        replacements=replacements,
        errors=tuple(errors),
        cancelled=cancelled,
    )
