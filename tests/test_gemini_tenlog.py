from __future__ import annotations

from auto_slicer.machine_sync import merge_owned, printer_short_id
from auto_slicer.orca_config import bundled_orca_profiles_dir, discover_orca_printers


def test_bundled_gemini_and_tenlog_are_discovered() -> None:
    root = bundled_orca_profiles_dir()
    assert root is not None
    printers = discover_orca_printers(root)
    gemini = next(
        (item for item in printers if "Gemini" in item.name and "0.4" in item.name),
        None,
    )
    tenlog = next(
        (item for item in printers if "TL-D3" in item.name and "0.4" in item.name),
        None,
    )
    assert gemini is not None
    assert gemini.machine_width == 300
    assert gemini.machine_depth == 300
    assert gemini.machine_height == 390
    assert gemini.extruder_count == 2
    assert gemini.machine_center_is_zero is False

    assert tenlog is not None
    assert tenlog.machine_width == 300
    assert tenlog.machine_depth == 290
    assert tenlog.machine_height == 350
    assert tenlog.extruder_count == 2
    assert tenlog.machine_center_is_zero is False


def test_merge_owned_seeds_gemini_and_tenlog() -> None:
    catalog = discover_orca_printers(bundled_orca_profiles_dir())
    owned = merge_owned([], [], catalog)
    gemini = next(item for item in owned if "Gemini" in str(item["name"]))
    tenlog = next(item for item in owned if "TL-D3" in str(item["name"]))

    assert gemini["orca_id"] == "orca:Tronxy Gemini S 0.4 nozzle"
    assert gemini["cura_id"] is None
    assert gemini["settings"]["bed_width"] == 300
    assert gemini["settings"]["bed_depth"] == 300
    assert gemini["settings"]["bed_height"] == 390
    assert gemini["settings"]["extruder_count"] == 2
    assert gemini["settings"]["gcode_flavor"] == "Klipper"
    assert gemini["settings"]["retraction_length"] == 0.8
    assert gemini["settings"]["retraction_speed"] == 35
    assert gemini["settings"]["printer_short_id"] == printer_short_id("Tronxy Gemini S")

    assert tenlog["orca_id"] == "orca:Tenlog TL-D3 Pro 0.4 nozzle"
    assert tenlog["cura_id"] is None
    assert tenlog["settings"]["bed_width"] == 300
    assert tenlog["settings"]["bed_depth"] == 290
    assert tenlog["settings"]["bed_height"] == 350
    assert tenlog["settings"]["extruder_count"] == 2
    assert tenlog["settings"]["gcode_flavor"] == "Klipper"
    assert tenlog["settings"]["retraction_length"] == 0.8
    assert tenlog["settings"]["printer_short_id"] == "TLD3P"


def test_heal_tenlog_preserves_custom_start_gcode() -> None:
    catalog = discover_orca_printers(bundled_orca_profiles_dir())
    custom = "; custom tenlog start\nG28\n"
    owned = merge_owned(
        [
            {
                "id": "owned:tenlog-legacy",
                "name": "Tenlog TL-D3 Pro",
                "cura_id": None,
                "orca_id": None,
                "settings": {
                    "orca_start_gcode": custom,
                    "bed_width": 300,
                    "bed_depth": 290,
                    "bed_height": 350,
                    "extruder_count": 2,
                    "gcode_flavor": "Klipper",
                },
            }
        ],
        [],
        catalog,
    )
    tenlog = next(item for item in owned if "TL-D3" in str(item["name"]))
    assert tenlog["orca_id"] == "orca:Tenlog TL-D3 Pro 0.4 nozzle"
    assert tenlog["settings"]["orca_start_gcode"].rstrip("\n") == custom.rstrip("\n")
    assert tenlog["settings"]["extruder_count"] == 2
