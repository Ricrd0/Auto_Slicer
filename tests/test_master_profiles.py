import json
from pathlib import Path

from auto_slicer.machine_sync import apply_save_scope, normalize_owned, printers_for_page
from auto_slicer.master_profiles import (
    adopt_master_printers,
    apply_master_profile,
    form_was_showing_master,
    load_master_profiles,
    publish_master_profile,
)


def test_master_mode_uses_shared_settings_and_keeps_local_transfer(tmp_path: Path) -> None:
    master_path = tmp_path / "master_profiles.json"
    local = normalize_owned(
        {
            "id": "owned:ender",
            "name": "Ender",
            "settings": {"nozzle_temperature": 190, "print_speed": 40},
            "transfer": {"protocol": "moonraker", "host": "10.0.0.8"},
            "profile_mode": "master",
        }
    )
    publish_master_profile(
        normalize_owned(
            {
                "id": "owned:ender",
                "name": "Ender",
                "settings": {"nozzle_temperature": 210, "print_speed": 60},
                "transfer": {"protocol": "moonraker", "host": "should-not-copy"},
            }
        ),
        master_path,
    )
    effective = apply_master_profile(local, load_master_profiles(master_path))
    assert effective["settings"]["nozzle_temperature"] == 210
    assert effective["settings"]["print_speed"] == 60
    assert effective["transfer"]["host"] == "10.0.0.8"
    stored = json.loads(master_path.read_text(encoding="utf-8"))
    assert "transfer" not in stored["printers"][0]


def test_local_mode_ignores_master(tmp_path: Path) -> None:
    master_path = tmp_path / "master_profiles.json"
    local = normalize_owned(
        {
            "name": "Ender",
            "settings": {"print_speed": 40},
            "profile_mode": "local",
        }
    )
    publish_master_profile(
        normalize_owned({"name": "Ender", "settings": {"print_speed": 90}}),
        master_path,
    )
    effective = apply_master_profile(local, load_master_profiles(master_path))
    assert effective["settings"]["print_speed"] == 40


def test_author_save_replaces_master_entry(tmp_path: Path) -> None:
    master_path = tmp_path / "master_profiles.json"
    publish_master_profile(
        normalize_owned({"id": "owned:ender", "name": "Ender", "settings": {"print_speed": 40}}),
        master_path,
    )
    existing = normalize_owned(
        {"id": "owned:ender", "name": "Ender", "settings": {"print_speed": 40}, "profile_mode": "author"}
    )
    incoming = normalize_owned(
        {
            "id": "owned:ender",
            "name": "Ender",
            "settings": {"print_speed": 55},
            "profile_mode": "author",
        }
    )
    saved = apply_save_scope(existing, incoming, "both")
    publish_master_profile(saved, master_path)
    masters = load_master_profiles(master_path)
    assert len(masters) == 1
    assert masters[0]["settings"]["print_speed"] == 55


def test_leaving_master_keeps_the_stored_local_setup() -> None:
    existing = normalize_owned(
        {
            "id": "owned:ender",
            "name": "Ender",
            "settings": {"print_speed": 40},
            "profile_mode": "master",
            "transfer": {"protocol": "moonraker", "host": "10.0.0.8"},
        }
    )
    incoming = normalize_owned(
        {
            "id": "owned:ender",
            "name": "Ender",
            "settings": {"print_speed": 90},
            "profile_mode": "local",
            "transfer": {"protocol": "moonraker", "host": "10.0.0.9"},
        }
    )
    saved = apply_save_scope(existing, incoming, "both")
    assert form_was_showing_master(existing, "local")
    saved["settings"] = existing["settings"]
    saved["cura_settings"] = existing.get("cura_settings")
    saved["orca_settings"] = existing.get("orca_settings")
    saved = normalize_owned(saved)
    assert saved["settings"]["print_speed"] == 40
    assert saved["transfer"]["host"] == "10.0.0.9"
    assert saved["profile_mode"] == "local"


def test_adopt_master_printers_adds_missing_printers_only() -> None:
    masters = [
        {
            "id": "owned:ender",
            "name": "Ender",
            "orca_id": "orca:ender",
            "settings": {"print_speed": 60},
        },
        {
            "id": "owned:sidewinder",
            "name": "Sidewinder",
            "orca_id": "orca:sidewinder",
            "settings": {"bed_width": 300, "bed_depth": 300, "bed_height": 400},
        },
    ]
    local = [
        normalize_owned(
            {"id": "owned:ender", "name": "Ender", "orca_id": "orca:ender", "profile_mode": "local"}
        )
    ]
    adopted = adopt_master_printers(local, masters)
    assert [item["name"] for item in adopted] == ["Ender", "Sidewinder"]
    assert adopted[0]["profile_mode"] == "local"
    added = normalize_owned(adopted[1])
    assert added["profile_mode"] == "master"
    assert added["transfer"]["protocol"] == "none"
    assert added["settings"]["bed_width"] == 300
    page = printers_for_page([], [added], engine="orca")
    assert page[0].name == "Sidewinder"

