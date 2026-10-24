from pathlib import Path

import pytest

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.machine_sync import (
    apply_filaments,
    best_orca_match,
    idex_offset_active,
    idex_output_modes,
    normalize_machine,
    patch_orca_machine,
    printer_short_id,
    with_idex_mode_gcode,
)
from auto_slicer.orca_config import build_orca_command
from auto_slicer.placement import footprint_of, idex_zone, place_group, place_single
from auto_slicer.slicing import output_group_path, output_model_path
from tests.test_placement import _part


def test_printer_short_id_examples() -> None:
    assert printer_short_id("Snapmaker J1S") == "SMJ1S"
    assert printer_short_id("Tenlog TL-D3 Pro") == "TLD3P"


def test_idex_coupling_enables_offset_and_clears_modes() -> None:
    enabled = normalize_machine(
        {
            "idex_copy_mode": True,
            "idex_offset_x": 150,
            "gcode_flavor": "Klipper",
        }
    )
    assert enabled["idex_offset_enabled"] is True
    assert enabled["idex_copy_mode"] is True

    cleared = normalize_machine(
        {
            "idex_offset_enabled": False,
            "idex_copy_mode": True,
            "idex_mirror_mode": True,
            "idex_offset_x": 150,
            "gcode_flavor": "Klipper",
        }
    )
    assert cleared["idex_offset_enabled"] is False
    assert cleared["idex_copy_mode"] is False
    assert cleared["idex_mirror_mode"] is False


def test_apply_offset_requires_nonzero_x() -> None:
    with pytest.raises(ValueError, match="idex_offset_x"):
        normalize_machine({"idex_offset_enabled": True, "idex_offset_x": 0})


def test_idex_offset_x_allows_negative() -> None:
    machine = normalize_machine(
        {
            "bed_width": 300,
            "bed_depth": 290,
            "idex_offset_enabled": True,
            "idex_offset_x": -150,
            "extruder_count": 2,
        }
    )
    assert machine["idex_offset_x"] == -150
    assert idex_offset_active(machine)
    assert idex_zone(300, 290, float(machine["idex_offset_x"])).width == 300
    patched = patch_orca_machine({}, machine, use_idex_zone=True)
    assert patched["printable_area"] == ["0x0", "300x0", "300x290", "0x290"]


def test_copy_mirror_cleared_when_not_klipper() -> None:
    machine = normalize_machine(
        {
            "idex_copy_mode": True,
            "idex_mirror_mode": True,
            "idex_offset_enabled": True,
            "idex_offset_x": 150,
            "gcode_flavor": "Marlin",
        }
    )
    assert machine["idex_copy_mode"] is False
    assert machine["idex_mirror_mode"] is False
    assert machine["idex_offset_enabled"] is True


def test_idex_output_modes_and_gcode_injection() -> None:
    machine = normalize_machine(
        {
            "gcode_flavor": "Klipper",
            "idex_offset_enabled": True,
            "idex_offset_x": 150,
            "idex_copy_mode": True,
            "idex_mirror_mode": True,
            "cura_start_gcode": "G28\nPRIME_LINE\n",
        }
    )
    assert idex_output_modes(machine) == ["copy", "mirror"]
    copy = with_idex_mode_gcode(machine, "copy")
    assert "ACTIVATE_COPY_MODE" in str(copy["cura_start_gcode"])
    assert "ACTIVATE_MIRROR_MODE" not in str(copy["cura_start_gcode"])
    mirror = with_idex_mode_gcode(machine, "mirror")
    assert "ACTIVATE_MIRROR_MODE" in str(mirror["cura_start_gcode"])


def test_output_paths_get_mode_suffixes() -> None:
    model = output_model_path("Tenlog", "Test", "folder/part.stl", False, mode_suffix="copy")
    assert model.as_posix().endswith("folder/part_copy.gcode")
    group = output_group_path("Tenlog", "Test", "pipes", False, mode_suffix="mirror")
    assert group.as_posix().endswith("groups/pipes_mirror.gcode")


def test_blank_output_folder_writes_under_the_printer() -> None:
    model = output_model_path("Tenlog", "  ", "folder/part.stl", False)
    assert model.as_posix() == "Tenlog/folder/part.gcode"
    group = output_group_path("Tenlog", "", "pipes", True)
    assert group.as_posix() == "Tenlog/_test/groups/pipes.gcode"


def test_single_model_uses_idex_zone_then_falls_back() -> None:
    name, triangles = _part("wall.stl", 20, 10, 5)
    part = footprint_of(name, triangles, (0, 0, 0))
    zone = idex_zone(300, 290, 150)
    layout = place_single(part, 300, 290, 350, None, zone)
    assert layout.error is None
    assert layout.status is None
    assert layout.items[0].min_x == 140
    assert layout.items[0].min_y == 140

    narrow = idex_zone(300, 290, 100)  # usable width 200
    huge = footprint_of(*_part("big.stl", 280, 10, 5), (0, 0, 0))
    fallback = place_single(huge, 300, 290, 350, None, narrow)
    assert fallback.error is None
    assert fallback.status is not None
    assert "recentred" in fallback.status
    assert fallback.items[0].min_x == 10


def test_group_repacks_when_idex_zone_is_too_small() -> None:
    first = footprint_of(*_part("a.stl", 120, 20), (0, 0, 0))
    second = footprint_of(*_part("b.stl", 120, 20), (0, 0, 0))
    zone = idex_zone(300, 200, 50)  # usable width 100 — each part is 120 wide
    layout = place_group([first, second], 300, 200, 250, gap=5, manual=None, zone=zone)
    assert layout.status is not None
    assert "recentred" in layout.status


def test_patch_orca_machine_shrinks_printable_area_for_idex_zone() -> None:
    machine = normalize_machine(
        {
            "bed_width": 324,
            "bed_depth": 200,
            "idex_offset_enabled": True,
            "idex_offset_x": 150,
            "extruder_count": 2,
        }
    )
    assert idex_offset_active(machine)
    patched = patch_orca_machine({}, machine, use_idex_zone=True)
    assert patched["printable_area"] == ["0x0", "300x0", "300x200", "0x200"]
    assert patched["nozzle_diameter"] == ["0.4", "0.4"]


def test_apply_filaments_stores_t1_metadata() -> None:
    machine = normalize_machine({"extruder_count": 2})
    t0 = {
        "name": "PLA",
        "orca_name": "Generic PLA",
        "nozzle_temperature": 210,
        "nozzle_temperature_initial": 215,
        "bed_temperature": 60,
        "bed_temperature_initial": 60,
    }
    t1 = {
        "name": "PETG",
        "orca_name": "Generic PETG",
        "nozzle_temperature": 240,
        "nozzle_temperature_initial": 245,
        "bed_temperature": 70,
        "bed_temperature_initial": 70,
    }
    merged = apply_filaments(machine, t0, t1)
    assert merged["nozzle_temperature"] == 210
    assert merged["nozzle_temperature_t1"] == 240
    assert merged["filament_inherits_t1"] == "Generic PETG"


def test_orca_command_loads_two_filaments(tmp_path: Path) -> None:
    printer = PrinterProfile(
        id="orca:J1",
        name="J1",
        definition_id="J1",
        definition_path=str(tmp_path / "machine.json"),
        extruder_definition_id=None,
        extruder_definition_path=None,
        global_settings=[],
        extruder_settings=[],
        machine_width=324,
        machine_depth=200,
        machine_height=200,
        machine_center_is_zero=False,
        extruder_count=2,
        setting_version=None,
        resource_setting_version=None,
        engine="orca",
    )
    filaments = [tmp_path / "filament.json", tmp_path / "filament_t1.json"]
    command = build_orca_command(
        Path("orca-slicer"),
        printer,
        [tmp_path / "part.stl"],
        tmp_path / "out.3mf",
        tmp_path,
        tmp_path / "process.json",
        filaments,
        tmp_path / "assemble.json",
    )
    load = command[command.index("--load-filaments") + 1]
    assert str(filaments[0]) in load and str(filaments[1]) in load


def test_snapmaker_j1s_matches_orca_j1() -> None:
    catalog = [
        PrinterProfile(
            id="orca:Snapmaker J1 (0.4 nozzle)",
            name="Snapmaker J1 (0.4 nozzle)",
            definition_id="Snapmaker J1 (0.4 nozzle)",
            definition_path=None,
            extruder_definition_id=None,
            extruder_definition_path=None,
            global_settings=[],
            extruder_settings=[],
            machine_width=324,
            machine_depth=200,
            machine_height=200,
            machine_center_is_zero=False,
            extruder_count=2,
            setting_version=None,
            resource_setting_version=None,
            engine="orca",
        ),
        PrinterProfile(
            id="orca:Creality Ender-3 0.4 nozzle",
            name="Creality Ender-3 0.4 nozzle",
            definition_id="Creality Ender-3 0.4 nozzle",
            definition_path=None,
            extruder_definition_id=None,
            extruder_definition_path=None,
            global_settings=[],
            extruder_settings=[],
            machine_width=220,
            machine_depth=220,
            machine_height=250,
            machine_center_is_zero=False,
            extruder_count=1,
            setting_version=None,
            resource_setting_version=None,
            engine="orca",
        ),
    ]
    match = best_orca_match("Snapmaker J1S", catalog, prefer_dual=True)
    assert match is not None
    assert "Snapmaker J1" in match.name
