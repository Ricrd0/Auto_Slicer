import json
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.machine_sync import (
    apply_filament,
    apply_save_scope,
    best_orca_match,
    cura_machine_overrides,
    filter_owned_orca,
    machine_for_engine,
    merge_owned,
    normalize_machine,
    normalize_owned,
    orca_filament_overrides,
    patch_orca_machine,
    sync_owned_with_orca_user_profiles,
)
from auto_slicer.orca_config import apply_user_orca_machine_override, user_orca_machine_override


def test_z_hop_options_are_written_for_both_engines() -> None:
    machine = normalize_machine({"z_hop": 0.4, "z_hop_type": "Spiral Lift"})
    cura = dict(cura_machine_overrides(machine))
    assert cura["retraction_hop"] == "0.4"
    assert cura["retraction_hop_enabled"] == "true"
    patched = patch_orca_machine({}, machine)
    assert patched["z_hop"] == ["0.4"]
    assert patched["z_hop_types"] == ["Spiral Lift"]
    filament = orca_filament_overrides(machine)
    assert filament["filament_z_hop"] == ["0.4"]
    assert filament["filament_z_hop_types"] == ["Spiral Lift"]
    disabled = dict(cura_machine_overrides(normalize_machine({"z_hop": 0})))
    assert disabled["retraction_hop_enabled"] == "false"


def _printer(name: str, engine: str = "orca", **extra: object) -> PrinterProfile:
    values = {
        "id": f"{engine}:{name}",
        "name": name,
        "definition_id": name,
        "definition_path": None,
        "extruder_definition_id": None,
        "extruder_definition_path": None,
        "global_settings": [],
        "extruder_settings": [],
        "machine_width": 220.0,
        "machine_depth": 220.0,
        "machine_height": 250.0,
        "machine_center_is_zero": False,
        "extruder_count": 1,
        "setting_version": None,
        "resource_setting_version": None,
        "engine": engine,
    }
    values.update(extra)
    return PrinterProfile(**values)


def test_orca_match_prefers_the_same_model() -> None:
    catalog = [
        _printer("Creality Ender-3 S1 0.4 nozzle"),
        _printer("Creality Ender-3 0.4 nozzle"),
        _printer("Creality Ender-3 V3 0.4 nozzle"),
    ]
    match = best_orca_match("Creality Ender-3", catalog)
    assert match is not None
    assert match.name == "Creality Ender-3 0.4 nozzle"
    s1 = best_orca_match("Creality Ender-3 S1", catalog)
    assert s1 is not None
    assert s1.name == "Creality Ender-3 S1 0.4 nozzle"


def test_default_orca_list_is_the_linked_profiles() -> None:
    cura = _printer("Creality Ender-3", engine="cura", id="ender")
    cura.global_settings = [("machine_nozzle_size", "0.4"), ("retraction_amount", "6")]
    orca = [
        _printer("Creality Ender-3 0.4 nozzle"),
        _printer("Artillery Genius 0.4 nozzle"),
    ]
    owned = merge_owned([], [cura], orca)
    assert owned[0]["orca_id"] == "orca:Creality Ender-3 0.4 nozzle"
    assert owned[0]["settings"]["retraction_length"] == 6
    visible = filter_owned_orca(orca, owned)
    assert [printer.name for printer in visible] == ["Creality Ender-3 0.4 nozzle"]
    again = merge_owned(owned, [cura], orca)
    assert len(again) == 1


def test_synced_values_apply_to_both_engine_profiles() -> None:
    machine = merge_owned([], [_printer("Ender", engine="cura", id="ender")], [])[0]["settings"]
    machine["bed_width"] = 235
    machine["orca_start_gcode"] = "G28"
    cura = dict(cura_machine_overrides(machine))
    assert cura["machine_width"] == "235"
    assert cura["retraction_amount"] == "5"
    assert "machine_start_gcode" not in cura
    patched = patch_orca_machine({"type": "machine", "retraction_length": ["1"]}, machine)
    assert patched["printable_area"] == ["0x0", "235x0", "235x220", "0x220"]
    assert patched["retraction_length"] == ["5"]
    assert patched["machine_start_gcode"] == "G28"
    assert cura["machine_gcode_flavor"] == "RepRap (Marlin/Sprinter)"
    machine["gcode_flavor"] = "Klipper"
    assert dict(cura_machine_overrides(machine))["machine_gcode_flavor"] == "RepRap (Marlin/Sprinter)"
    assert patch_orca_machine({}, machine)["gcode_flavor"] == "klipper"
    machine["nozzle_temperature"] = 210
    machine["nozzle_temperature_initial"] = 215
    machine["bed_temperature"] = 60
    machine["bed_temperature_initial"] = 65
    cura = dict(cura_machine_overrides(machine))
    assert cura["material_print_temperature"] == "210"
    assert cura["material_print_temperature_layer_0"] == "215"
    assert cura["material_initial_print_temperature"] == "215"
    assert cura["material_bed_temperature"] == "60"
    assert cura["material_bed_temperature_layer_0"] == "65"
    filament = orca_filament_overrides(machine)
    assert filament["nozzle_temperature"] == ["210"]
    assert filament["nozzle_temperature_initial_layer"] == ["215"]
    assert filament["hot_plate_temp"] == ["60"]
    assert filament["hot_plate_temp_initial_layer"] == ["65"]
    assert filament["textured_plate_temp_initial_layer"] == ["65"]
    assert filament["cool_plate_temp"] == ["60"]


def test_saving_for_one_engine_leaves_the_other_alone() -> None:
    existing = normalize_owned({"id": "owned:ender", "name": "Ender", "settings": {"nozzle_temperature": 200}})
    incoming = normalize_owned(
        {"id": "owned:ender", "name": "Ender", "settings": {"nozzle_temperature": 210, "cura_start_gcode": "G28"}}
    )
    cura_only = apply_save_scope(existing, incoming, "cura")
    assert cura_only["settings"]["nozzle_temperature"] == 200
    assert cura_only["cura_settings"]["nozzle_temperature"] == 210
    assert cura_only["cura_settings"]["cura_start_gcode"] == "G28"
    assert cura_only["orca_settings"] is None
    assert machine_for_engine(cura_only, "cura")["nozzle_temperature"] == 210
    assert machine_for_engine(cura_only, "orca")["nozzle_temperature"] == 200
    orca_only = apply_save_scope(cura_only, incoming, "orca")
    assert orca_only["cura_settings"]["nozzle_temperature"] == 210
    assert orca_only["orca_settings"]["nozzle_temperature"] == 210
    assert orca_only["settings"]["nozzle_temperature"] == 200
    both = apply_save_scope(orca_only, incoming, "both")
    assert both["settings"]["nozzle_temperature"] == 210
    assert both["cura_settings"] is None
    assert both["orca_settings"] is None


def test_missing_initial_temperatures_follow_the_print_temperature() -> None:
    machine = normalize_machine({"nozzle_temperature": 205, "bed_temperature": 55})
    assert machine["nozzle_temperature_initial"] == 205
    assert machine["bed_temperature_initial"] == 55
    assert machine["temperature_override"] is False


def test_user_klipper_override_is_preferred_for_orca_machine(tmp_path: Path) -> None:
    system = tmp_path / "system" / "Creality" / "machine"
    user = tmp_path / "user" / "default" / "machine"
    system.mkdir(parents=True)
    user.mkdir(parents=True)
    (system / "Creality Ender-3 0.4 nozzle.json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": "Creality Ender-3 0.4 nozzle",
                "instantiation": "true",
                "nozzle_diameter": ["0.4"],
                "printable_area": ["0x0", "220x0", "220x220", "0x220"],
                "printable_height": "250",
                "gcode_flavor": "marlin",
            }
        ),
        encoding="utf-8",
    )
    (user / "Creality Ender-3 - Klipper.json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": "Creality Ender-3 - Klipper",
                "inherits": "Creality Ender-3 0.4 nozzle",
                "instantiation": "true",
                "gcode_flavor": "klipper",
                "machine_start_gcode": "G28 ; klipper",
            }
        ),
        encoding="utf-8",
    )
    override = user_orca_machine_override(tmp_path, "Creality Ender-3 0.4 nozzle")
    assert override is not None
    assert override["gcode_flavor"] == "klipper"
    merged = apply_user_orca_machine_override(
        {"name": "Creality Ender-3 0.4 nozzle", "gcode_flavor": "marlin"},
        tmp_path,
    )
    assert merged["gcode_flavor"] == "klipper"
    assert merged["machine_start_gcode"] == "G28 ; klipper"
    owned = [
        normalize_owned(
            {
                "name": "Creality Ender-3",
                "orca_id": "orca:Creality Ender-3 0.4 nozzle",
                "settings": {"gcode_flavor": "Marlin", "bed_width": 230},
            }
        )
    ]
    healed = sync_owned_with_orca_user_profiles(owned, tmp_path)
    assert healed[0]["settings"]["gcode_flavor"] == "Klipper"
    assert healed[0]["settings"]["orca_start_gcode"] == "G28 ; klipper"


def test_shared_filament_temperatures_apply_unless_the_printer_overrides() -> None:
    machine = normalize_machine({"temperature_override": False, "nozzle_temperature": 190, "bed_temperature": 50})
    filament = {
        "name": "Generic PLA",
        "orca_name": "Generic PLA",
        "nozzle_temperature": 220,
        "nozzle_temperature_initial": 225,
        "bed_temperature": 60,
        "bed_temperature_initial": 65,
    }
    applied = apply_filament(machine, filament)
    assert applied["nozzle_temperature"] == 220
    assert applied["nozzle_temperature_initial"] == 225
    assert applied["bed_temperature_initial"] == 65
    assert applied["filament_inherits"] == "Generic PLA"
    machine["temperature_override"] = True
    overridden = apply_filament(machine, filament)
    assert overridden["nozzle_temperature"] == 190
    assert overridden["filament_inherits"] == "Generic PLA"
