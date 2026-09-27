from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile, definition_search_dirs, join_search_path
from auto_slicer.paths import DataPaths
from auto_slicer.settings_schema import SliceSettings, cura_setting_overrides


class EngineError(RuntimeError):
    pass


def locate_cura(paths: DataPaths) -> tuple[Path | None, Path | None, str]:
    """Return the CuraEngine binary, resources directory, and library path."""
    engine_env = os.environ.get("CURA_ENGINE")
    resources_env = os.environ.get("CURA_RESOURCES")
    if engine_env and resources_env:
        return Path(engine_env), Path(resources_env), os.environ.get("CURA_LIBRARY_PATH", "")
    root = paths.cura_root
    if root is None or not root.exists():
        return None, None, ""
    engine = _first_file(root, "CuraEngine")
    resources = _resources_dir(root)
    return engine, resources, _library_path(root)


def build_slice_command(
    engine: Path,
    printer: PrinterProfile,
    settings: SliceSettings,
    meshes: list[Path],
    output: Path,
    config_dir: Path,
    resources_dir: Path | None,
) -> list[str]:
    if printer.definition_path is None:
        raise EngineError(printer.error or "printer definition is missing")
    fdmprinter = _require_definition("fdmprinter", resources_dir, config_dir)
    fdmextruder = _require_definition("fdmextruder", resources_dir, config_dir)
    search = definition_search_dirs(config_dir, resources_dir)
    overrides = cura_setting_overrides(settings)
    command = [
        str(engine),
        "slice",
        "-v",
        "-d",
        join_search_path(search),
        "-j",
        fdmprinter,
        "-j",
        printer.definition_path,
    ]
    command.extend(_setting_args(printer.global_settings))
    command.extend(_setting_args(overrides))
    command.extend(["-e0", "-j", fdmextruder])
    if printer.extruder_definition_path:
        command.extend(["-j", printer.extruder_definition_path])
    command.extend(_setting_args(printer.extruder_settings))
    command.extend(_setting_args(overrides))
    for mesh in meshes:
        command.extend(
            [
                "-l",
                str(mesh),
                "-s",
                "center_object=false",
                "-s",
                "mesh_position_x=0",
                "-s",
                "mesh_position_y=0",
                "-s",
                "mesh_position_z=0",
            ]
        )
    command.extend(["-o", str(output)])
    return command


def run_slice(
    command: list[str],
    library_path: str,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[int, str]:
    env = os.environ.copy()
    if library_path:
        previous = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = library_path + (":" + previous if previous else "")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    tail: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        stripped = line.rstrip()
        if stripped:
            tail.append(stripped)
            tail = tail[-40:]
            if on_progress is not None and "progress" in stripped.lower():
                on_progress(stripped)
    code = process.wait()
    return code, "\n".join(tail)


def _setting_args(pairs: list[tuple[str, str]]) -> list[str]:
    args: list[str] = []
    for key, value in pairs:
        args.extend(["-s", f"{key}={value}"])
    return args


def _require_definition(definition_id: str, resources_dir: Path | None, config_dir: Path) -> str:
    for root in filter(None, (resources_dir, config_dir)):
        for directory in (root / "definitions", root / "extruders", root):
            candidate = directory / f"{definition_id}.def.json"
            if candidate.is_file():
                return str(candidate)
    raise EngineError(f"Cura definition {definition_id}.def.json was not found")


def _first_file(root: Path, name: str) -> Path | None:
    for path in root.rglob(name):
        if path.is_file():
            return path
    return None


def _resources_dir(root: Path) -> Path | None:
    for path in root.rglob("definitions"):
        if path.is_dir() and (path / "fdmprinter.def.json").is_file():
            return path.parent
    return None


def _library_path(root: Path) -> str:
    directories: list[str] = []
    for path in root.rglob("*"):
        if path.is_dir() and path.name in {"lib", "lib64"}:
            directories.append(str(path))
    return ":".join(directories)
