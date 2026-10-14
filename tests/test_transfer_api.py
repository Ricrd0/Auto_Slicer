from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from auto_slicer.app import create_app
from auto_slicer.machine_sync import normalize_owned
from auto_slicer.meshio import write_stl
from auto_slicer.paths import DataPaths
from auto_slicer.transfer import ProbeResult, UploadResult
from tests.support import write_printer_tree


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    config = tmp_path / "cura-config"
    resources = tmp_path / "resources"
    write_printer_tree(config, resources)
    monkeypatch.setenv("CURA_ENGINE", str(tmp_path / "CuraEngine"))
    monkeypatch.setenv("CURA_RESOURCES", str(resources))
    paths = DataPaths(
        input_dir=tmp_path / "input",
        output_dir=tmp_path / "output",
        cura_config_dir=config,
        app_dir=tmp_path / "app",
        frontend_dir=tmp_path / "frontend",
        cura_root=None,
    )
    paths.input_dir.mkdir()
    paths.output_dir.mkdir()
    paths.app_dir.mkdir()
    triangle = [((0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (0.0, 10.0, 4.0))]
    (paths.input_dir / "wall.stl").write_bytes(write_stl(triangle))
    return TestClient(create_app(paths))


def _seed_owned(client: TestClient, transfer: dict) -> dict:
    owned = normalize_owned(
        {
            "name": "Shop Printer",
            "cura_id": "shop_printer",
            "settings": {},
            "transfer": transfer,
        }
    )
    saved = client.put("/api/owned", json={**owned, "scope": "both"})
    assert saved.status_code == 200, saved.text
    return saved.json()


def test_options_include_transfer_choices(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    options = client.get("/api/options").json()
    assert "moonraker" in options["transfer_protocols"]
    assert "snapmaker" in options["transfer_protocols"]
    assert "sftp" in options["transfer_protocols"]
    assert "manual" in options["transfer_modes"]
    assert "auto" in options["transfer_modes"]


def test_owned_transfer_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    saved = _seed_owned(
        client,
        {"protocol": "moonraker", "host": "192.168.0.50", "api_key": "secret"},
    )
    assert saved["transfer"]["protocol"] == "moonraker"
    assert saved["transfer"]["host"] == "192.168.0.50"
    assert saved["transfer"]["port"] == 7125
    assert saved["transfer"]["mode"] == "manual"


def test_transfer_test_endpoint_updates_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    saved = _seed_owned(
        client, {"protocol": "moonraker", "host": "192.168.0.50"}
    )
    with patch(
        "auto_slicer.app.probe_transfer",
        return_value=ProbeResult(ok=True),
    ):
        result = client.post(f"/api/owned/{saved['id']}/transfer/test")
    assert result.status_code == 200
    body = result.json()
    assert body["ok"] is True
    assert body["transfer"]["reachable"] is True
    assert body["transfer"]["last_probe_ok"] is True


def test_manual_upload_endpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    saved = _seed_owned(
        client,
        {
            "protocol": "moonraker",
            "host": "192.168.0.50",
            "reachable": True,
            "last_probe_ok": True,
        },
    )
    # Create a finished job item with a real gcode file.
    store = client.app.state.store
    job_id = store.create_job("test", "Test")
    out_rel = "Shop Printer/Test/wall.gcode"
    out = store.paths.output_dir / out_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("; gcode\n", encoding="utf-8")
    item_id = store.add_item(
        job_id, saved["cura_id"], "Shop Printer", "model", "wall.stl", out_rel
    )
    store.update_item(item_id, status="done", progress="done")

    with (
        patch(
            "auto_slicer.jobs.probe_transfer",
            return_value=ProbeResult(ok=True),
        ),
        patch(
            "auto_slicer.jobs.upload_file",
            return_value=UploadResult(ok=True, remote_name="wall.gcode"),
        ),
    ):
        result = client.post(f"/api/jobs/items/{item_id}/upload")
    assert result.status_code == 200, result.text
    assert result.json()["upload_status"] == "uploaded"


def test_manual_upload_skips_when_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(tmp_path, monkeypatch)
    saved = _seed_owned(
        client, {"protocol": "moonraker", "host": "192.168.0.50"}
    )
    store = client.app.state.store
    job_id = store.create_job("test", "Test")
    out_rel = "Shop Printer/Test/wall.gcode"
    out = store.paths.output_dir / out_rel
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("; gcode\n", encoding="utf-8")
    item_id = store.add_item(
        job_id, saved["cura_id"], "Shop Printer", "model", "wall.stl", out_rel
    )
    store.update_item(item_id, status="done", progress="done")

    with patch(
        "auto_slicer.jobs.probe_transfer",
        return_value=ProbeResult(ok=False, error="printer offline"),
    ):
        result = client.post(f"/api/jobs/items/{item_id}/upload")
    assert result.status_code == 200
    body = result.json()
    assert body["upload_status"] == "skipped"
    assert "offline" in (body["upload_error"] or "")
