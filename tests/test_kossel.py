from __future__ import annotations

import json
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.machine_sync import (
    merge_owned,
    normalize_machine,
    patch_orca_machine,
)
from auto_slicer.orca_config import (
    area_is_center_zero,
    bundled_orca_profiles_dir,
    circular_printable_area,
    discover_orca_printers,
    load_orca_printer,
    orca_origin,
)
from auto_slicer.settings_schema import SliceSettings


def test_bundled_kossel_profile_is_discovered() -> None:
    root = bundled_orca_profiles_dir()
    assert root is not None
    printers = discover_orca_printers(root)
    match = next(
        (item for item in printers if "Kossel" in item.name and "0.4" in item.name),
        None,
    )
    assert match is not None
    assert match.machine_center_is_zero is True
    assert match.machine_width == 230
    assert match.machine_depth == 230
    assert match.machine_height == 300
    assert match.extruder_count == 1
    linked = load_orca_printer(
        root,
        "orca:Anycubic Kossel Linear Plus 0.4 nozzle",
        display_name="Anycubic Kossel Linear Plus",
    )
    assert linked is not None
    assert linked.machine_center_is_zero is True


def test_merge_owned_seeds_kossel_from_orca_catalog() -> None:
    catalog = discover_orca_printers(bundled_orca_profiles_dir())
    owned = merge_owned([], [], catalog)
    kossel = next(item for item in owned if "Kossel" in str(item["name"]))
    assert kossel["orca_id"] == "orca:Anycubic Kossel Linear Plus 0.4 nozzle"
    assert kossel["cura_id"] is None
    settings = kossel["settings"]
    assert settings["bed_width"] == 230
    assert settings["bed_height"] == 300
    assert settings["gcode_flavor"] == "Klipper"
    assert settings["retraction_length"] == 4
    assert settings["retraction_speed"] == 35
    assert settings["print_speed"] == 80


def test_patch_orca_preserves_circular_center_zero_bed() -> None:
    source = {
        "printer_structure": "delta",
        "printable_area": circular_printable_area(230),
    }
    machine = normalize_machine(
        {
            "bed_width": 230,
            "bed_depth": 230,
            "bed_height": 300,
            "retraction_length": 4,
            "retraction_speed": 35,
        }
    )
    patched = patch_orca_machine(source, machine)
    assert area_is_center_zero(patched["printable_area"])
    assert patched["printable_height"] == "300"
    assert patched["retraction_length"] == ["4"]
    assert patched["retraction_speed"] == ["35"]


def test_orca_origin_uses_bed_center_for_deltas() -> None:
    printer = PrinterProfile(
        id="orca:Kossel",
        name="Kossel",
        definition_id="Kossel",
        definition_path=None,
        extruder_definition_id=None,
        extruder_definition_path=None,
        global_settings=[],
        extruder_settings=[],
        machine_width=230,
        machine_depth=230,
        machine_height=300,
        machine_center_is_zero=True,
        extruder_count=1,
        setting_version=None,
        resource_setting_version=None,
        engine="orca",
    )
    assert orca_origin(SliceSettings(slicer_engine="orca"), printer) == "bed_center"
    assert orca_origin(SliceSettings(slicer_engine="orca"), None) == "front_left"
