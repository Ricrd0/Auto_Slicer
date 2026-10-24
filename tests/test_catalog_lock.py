"""Catalog warm must not freeze the printers page or a slice start."""

from __future__ import annotations

import threading
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.jobs import JobRunner
from auto_slicer.paths import DataPaths
from auto_slicer.store import Store


def _runner(tmp_path: Path) -> JobRunner:
    paths = DataPaths(
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        cura_config_dir=tmp_path / "cura",
        app_dir=tmp_path / "app",
        frontend_dir=tmp_path / "frontend",
        cura_root=None,
    )
    paths.ensure()
    return JobRunner(Store(paths))


def _printer() -> PrinterProfile:
    return PrinterProfile(
        id="orca:Tenlog",
        name="Tenlog",
        definition_id="Tenlog",
        definition_path=None,
        extruder_definition_id=None,
        extruder_definition_path=None,
        global_settings=[],
        extruder_settings=[],
        machine_width=300.0,
        machine_depth=300.0,
        machine_height=300.0,
        machine_center_is_zero=False,
        extruder_count=1,
        setting_version=None,
        resource_setting_version=None,
        engine="orca",
    )


def test_second_catalog_warm_returns_while_the_first_is_scanning(tmp_path: Path) -> None:
    runner = _runner(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow_discover(engine_name: str) -> list[PrinterProfile]:
        started.set()
        assert release.wait(timeout=2)
        return []

    runner._discovered = slow_discover  # type: ignore[method-assign]
    first = threading.Thread(target=lambda: runner.warm_catalog("orca"))
    first.start()
    assert started.wait(timeout=2)
    status = runner.warm_catalog("orca")
    assert status["state"] == "loading"
    assert runner.cached_discovered("orca", wait=False) is None
    release.set()
    first.join(timeout=2)
    assert not first.is_alive()


def test_filament_scan_does_not_hold_the_printer_lock(tmp_path: Path, monkeypatch) -> None:
    runner = _runner(tmp_path)
    seen = threading.Event()

    def scan(config_dir: object) -> list[dict[str, object]]:
        assert runner._printer_lock.acquire(timeout=0.2)
        runner._printer_lock.release()
        seen.set()
        return []

    monkeypatch.setattr("auto_slicer.jobs.discover_filaments", scan)
    assert runner.filament_catalog() == []
    assert seen.is_set()


def test_start_batch_does_not_scan_the_engine_catalog(tmp_path: Path, monkeypatch) -> None:
    runner = _runner(tmp_path)
    printer = _printer()
    monkeypatch.setattr(runner, "printers", lambda **kwargs: [printer])

    def explode(engine_name: str) -> list[PrinterProfile]:
        raise AssertionError(f"catalog scan during job start: {engine_name}")

    monkeypatch.setattr(runner, "_discovered", explode)
    try:
        runner.start_batch()
    except ValueError as exc:
        assert "no models or groups" in str(exc)
    else:
        raise AssertionError("expected the empty batch to be rejected")
