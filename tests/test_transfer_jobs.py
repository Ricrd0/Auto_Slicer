from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from auto_slicer.jobs import JobRunner
from auto_slicer.machine_sync import normalize_owned
from auto_slicer.paths import DataPaths
from auto_slicer.store import Store
from auto_slicer.transfer import ProbeResult, UploadResult


@pytest.fixture
def runner(tmp_path: Path) -> JobRunner:
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


def test_auto_upload_skips_when_unreachable(runner: JobRunner, tmp_path: Path) -> None:
    record = normalize_owned(
        {
            "name": "Kossel",
            "settings": {},
            "transfer": {
                "protocol": "moonraker",
                "host": "10.0.0.1",
                "mode": "auto",
                "reachable": True,
                "last_probe_ok": True,
            },
        }
    )
    runner.store.save_owned([record])
    job_id = runner.store.create_job("batch", "Test")
    out_rel = "Kossel/Test/a.gcode"
    local = runner.store.paths.output_dir / out_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("; gcode\n", encoding="utf-8")
    item_id = runner.store.add_item(
        job_id, record["id"], "Kossel", "model", "a.stl", out_rel
    )
    with patch(
        "auto_slicer.jobs.probe_transfer",
        return_value=ProbeResult(ok=False, error="unreachable"),
    ):
        runner._maybe_auto_upload(item_id, record, local, out_rel)
    item = runner.store.get_item(item_id)
    assert item is not None
    assert item["upload_status"] == "skipped"
    assert "unreachable" in (item["upload_error"] or "")


def test_auto_upload_runs_when_reachable(runner: JobRunner) -> None:
    record = normalize_owned(
        {
            "name": "Kossel",
            "settings": {},
            "transfer": {
                "protocol": "moonraker",
                "host": "10.0.0.1",
                "mode": "auto",
                "reachable": True,
                "last_probe_ok": True,
            },
        }
    )
    runner.store.save_owned([record])
    job_id = runner.store.create_job("batch", "Test")
    out_rel = "Kossel/Test/a.gcode"
    local = runner.store.paths.output_dir / out_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("; gcode\n", encoding="utf-8")
    item_id = runner.store.add_item(
        job_id, record["id"], "Kossel", "model", "a.stl", out_rel
    )
    with (
        patch(
            "auto_slicer.jobs.probe_transfer",
            return_value=ProbeResult(ok=True),
        ),
        patch(
            "auto_slicer.jobs.upload_file",
            return_value=UploadResult(ok=True, remote_name="a.gcode"),
        ) as upload,
    ):
        runner._maybe_auto_upload(item_id, record, local, out_rel)
    upload.assert_called_once()
    item = runner.store.get_item(item_id)
    assert item is not None
    assert item["upload_status"] == "uploaded"


def test_manual_mode_does_not_auto_upload(runner: JobRunner) -> None:
    record = normalize_owned(
        {
            "name": "Kossel",
            "settings": {},
            "transfer": {
                "protocol": "moonraker",
                "host": "10.0.0.1",
                "mode": "manual",
                "reachable": True,
                "last_probe_ok": True,
            },
        }
    )
    job_id = runner.store.create_job("batch", "Test")
    out_rel = "Kossel/Test/a.gcode"
    local = runner.store.paths.output_dir / out_rel
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("; gcode\n", encoding="utf-8")
    item_id = runner.store.add_item(
        job_id, record["id"], "Kossel", "model", "a.stl", out_rel
    )
    with patch("auto_slicer.jobs.upload_file") as upload:
        runner._maybe_auto_upload(item_id, record, local, out_rel)
    upload.assert_not_called()
    item = runner.store.get_item(item_id)
    assert item is not None
    assert item["upload_status"] is None
