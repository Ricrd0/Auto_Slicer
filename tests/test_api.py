import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from auto_slicer.app import create_app
from auto_slicer.meshio import write_stl
from auto_slicer.paths import DataPaths
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
    triangle = [((0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (0.0, 10.0, 4.0))]
    (paths.input_dir / "wall.stl").write_bytes(write_stl(triangle))
    return TestClient(create_app(paths))


def test_settings_pose_layout_and_bundle_upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    saved = client.put(
        "/api/settings",
        json={"output_folder_name": "building_1", "rotation_z": 90, "retraction_combing": "infill"},
    )
    assert saved.status_code == 200
    assert saved.json()["output_folder_name"] == "building_1"
    assert saved.json()["infill_pattern"] == "lightning"

    printers = client.get("/api/printers").json()
    shop = next(printer for printer in printers if printer["id"] == "shop_printer")
    assert shop["version_mismatch"] is True
    assert shop["machine_width"] == 220

    pose = client.put(
        "/api/poses",
        json={"path": "wall.stl", "position": [10, 12], "rotation": [0, 0, 90], "z": 4, "scale": 2},
    )
    assert pose.status_code == 200
    layout = client.post("/api/layout", json={"printer_id": "shop_printer", "kind": "model", "model": "wall.stl"})
    assert layout.status_code == 200
    body = layout.json()
    assert body["items"][0]["min_x"] == 10
    assert body["items"][0]["rotation"] == [0, 0, 90]
    assert body["items"][0]["z"] == 4
    assert body["items"][0]["scale"] == 2

    groups = client.put(
        "/api/groups",
        json={"groups": [{"name": "plate", "files": ["wall.stl"]}]},
    )
    group_id = groups.json()["groups"][0]["id"]
    moved = client.put(
        "/api/groups/layout",
        json={"id": group_id, "printer_id": "shop_printer", "file": "wall.stl", "x": 40, "y": 15},
    )
    assert moved.status_code == 200
    assert moved.json()["items"][0]["min_x"] == 40

    bundle = client.get("/api/bundles", params={"printer_id": "shop_printer"})
    assert bundle.status_code == 200
    empty = tmp_path / "fresh"
    fresh = TestClient(
        create_app(
            DataPaths(
                input_dir=tmp_path / "input",
                output_dir=tmp_path / "output",
                cura_config_dir=empty,
                app_dir=tmp_path / "app-fresh",
                frontend_dir=tmp_path / "frontend",
                cura_root=None,
            )
        )
    )
    uploaded = fresh.post("/api/bundles", files={"file": ("printer.zip", bundle.content, "application/zip")})
    assert uploaded.status_code == 200
    assert uploaded.json()["imported"] == ["shop_printer"]
    assert any(printer["id"] == "shop_printer" for printer in fresh.get("/api/printers").json())


def test_browse_folder_and_choose_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    folder = tmp_path / "input"
    (folder / "other.stl").write_bytes((folder / "wall.stl").read_bytes())
    listed = client.get("/api/browse", params={"path": str(folder)})
    assert listed.status_code == 200
    names = {entry["name"] for entry in listed.json()["entries"]}
    assert "wall.stl" in names
    assert "other.stl" in names

    saved = client.put("/api/locations", json={"input_dir": str(folder), "included": ["wall.stl"]})
    assert saved.status_code == 200
    flags = {model["path"]: model["included"] for model in client.get("/api/models").json()}
    assert flags["wall.stl"] is True
    assert flags["other.stl"] is False

    outside = client.get("/api/browse", params={"path": str(tmp_path / "missing")})
    assert outside.status_code == 400


def test_orca_filament_can_be_curated_and_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = _client(tmp_path, monkeypatch)
    root = tmp_path / "orca"
    machine = root / "system" / "Creality" / "machine"
    machine.mkdir(parents=True)
    (machine / "Ender.json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": "Ender",
                "instantiation": "true",
                "nozzle_diameter": ["0.4"],
                "printable_area": ["0x0", "220x0", "220x220", "0x220"],
                "printable_height": "250",
            }
        ),
        encoding="utf-8",
    )
    filament = root / "system" / "Creality" / "filament"
    filament.mkdir()
    (filament / "pla.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "Generic PLA",
                "instantiation": "true",
                "nozzle_temperature": ["220"],
                "nozzle_temperature_initial_layer": ["225"],
                "hot_plate_temp": ["60"],
                "hot_plate_temp_initial_layer": ["65"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AUTO_SLICER_ORCA_FALLBACK", str(root))
    catalog = client.get("/api/filaments/catalog", params={"q": "pla"})
    assert catalog.status_code == 200
    assert catalog.json()[0]["name"] == "Generic PLA"
    added = client.post("/api/filaments", json={"orca_id": "orca-filament:Generic PLA"})
    assert added.status_code == 200
    assert added.json()["nozzle_temperature_initial"] == 225
    library = client.get("/api/filaments").json()
    assert library["selected_id"] == added.json()["id"]
    updated = client.put(
        "/api/filaments",
        json={**added.json(), "bed_temperature_initial": 70},
    )
    assert updated.status_code == 200
    assert updated.json()["bed_temperature_initial"] == 70
