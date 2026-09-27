from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_segment(name: str) -> str:
    """Return a single path segment with characters that break folders removed."""
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    if cleaned in {"", ".", ".."} or ".." in cleaned:
        raise ValueError(f"invalid name: {name!r}")
    return cleaned


def safe_relative(value: str) -> Path:
    """Return a relative path that cannot escape its root."""
    raw = value.replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        raise ValueError(f"invalid relative path: {value!r}")
    path = Path(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"invalid relative path: {value!r}")
    return path


@dataclass(frozen=True)
class DataPaths:
    input_dir: Path
    output_dir: Path
    cura_config_dir: Path
    app_dir: Path
    frontend_dir: Path
    cura_root: Path | None

    def ensure(self) -> None:
        for directory in (
            self.input_dir,
            self.output_dir,
            self.cura_config_dir,
            self.app_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def from_env() -> DataPaths:
        root = Path.cwd()
        frontend = os.environ.get("AUTO_SLICER_FRONTEND")
        if frontend:
            frontend_dir = Path(frontend)
        else:
            bundled = Path(__file__).resolve().parents[2] / "frontend"
            frontend_dir = bundled
        cura_root = os.environ.get("CURA_ROOT")
        return DataPaths(
            input_dir=Path(os.environ.get("AUTO_SLICER_INPUT", root / "data" / "input")),
            output_dir=Path(os.environ.get("AUTO_SLICER_OUTPUT", root / "data" / "output")),
            cura_config_dir=Path(
                os.environ.get("AUTO_SLICER_CURA_CONFIG", root / "data" / "cura-config")
            ),
            app_dir=Path(os.environ.get("AUTO_SLICER_APP", root / "data" / "app")),
            frontend_dir=frontend_dir,
            cura_root=Path(cura_root) if cura_root else None,
        )
