import os
from pathlib import Path

from auto_slicer.jobs import slice_worker_count
from auto_slicer.paths import DataPaths
from auto_slicer.store import Store


def test_slice_worker_count_defaults_overlap_without_oversubscribing(monkeypatch) -> None:
    monkeypatch.delenv("AUTO_SLICER_SLICE_WORKERS", raising=False)
    monkeypatch.setattr(os, "cpu_count", lambda: 16)
    assert slice_worker_count(1) == 1
    assert slice_worker_count(10) == 4
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    assert slice_worker_count(10) == 2
    monkeypatch.setenv("AUTO_SLICER_SLICE_WORKERS", "3")
    assert slice_worker_count(10) == 3
    assert slice_worker_count(2) == 2


def test_cancel_open_items_marks_queued_and_running(tmp_path: Path) -> None:
    paths = DataPaths(
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        cura_config_dir=tmp_path / "cura",
        app_dir=tmp_path / "app",
        frontend_dir=tmp_path / "frontend",
        cura_root=None,
    )
    paths.ensure()
    store = Store(paths)
    job_id = store.create_job("batch", "out")
    queued = store.add_item(job_id, "p1", "Printer 1", "model", "a.stl", "a.gcode")
    running = store.add_item(job_id, "p2", "Printer 2", "model", "b.stl", "b.gcode")
    done = store.add_item(job_id, "p3", "Printer 3", "model", "c.stl", "c.gcode")
    store.update_item(running, status="running", progress="slicing")
    store.update_item(done, status="done", progress="done")
    store.update_job(job_id, "running")
    changed = store.cancel_open_items(job_id)
    assert changed == 2
    record = store.job_record(job_id)
    assert record is not None
    by_id = {item["id"]: item for item in record["items"]}
    assert by_id[queued]["status"] == "cancelled"
    assert by_id[running]["status"] == "cancelled"
    assert by_id[done]["status"] == "done"
