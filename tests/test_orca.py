import json
import zipfile
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.meshio import transform_mesh
from auto_slicer.orca_config import (
    build_orca_arrange_command,
    build_orca_command,
    discover_filaments,
    discover_orca_printers,
    extract_plate_gcode,
    process_inherits_for_machine,
    search_filaments,
    write_assemble_list,
    write_process_profile,
)
from auto_slicer.settings_schema import SliceSettings, orca_setting_overrides


def _machine(directory: Path, name: str, **extra: object) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "type": "machine",
        "name": name,
        "instantiation": "true",
        "nozzle_diameter": ["0.4"],
        "printable_area": ["0x0", "220x0", "220x220", "0x220"],
        "printable_height": "250",
    }
    payload.update(extra)
    (directory / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")


def test_default_engine_is_cura() -> None:
    assert SliceSettings().slicer_engine == "cura"
    assert SliceSettings.from_dict({}).slicer_engine == "cura"


def test_orca_discovery_keeps_single_extruder_machines(tmp_path: Path) -> None:
    system = tmp_path / "system" / "Creality" / "machine"
    _machine(system, "Ender")
    _machine(system, "Base", instantiation="false")
    _machine(system, "Twin", nozzle_diameter=["0.4", "0.4"])
    user = tmp_path / "user" / "default" / "machine"
    _machine(user, "Ender", printable_height="300")
    printers = {printer.name: printer for printer in discover_orca_printers(tmp_path)}
    assert set(printers) == {"Ender", "Twin"}
    assert printers["Ender"].slicable
    assert printers["Ender"].machine_width == 220
    assert printers["Ender"].machine_height == 300
    assert printers["Ender"].id == "orca:Ender"
    assert printers["Ender"].engine == "orca"
    assert not printers["Twin"].slicable


def test_filament_temperatures_follow_the_inherited_bed_type(tmp_path: Path) -> None:
    folder = tmp_path / "system" / "Creality" / "filament"
    folder.mkdir(parents=True)
    (folder / "base.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "fdm_filament_pla",
                "instantiation": "false",
                "bed_type": ["Cool Plate"],
                "cool_plate_temp": ["35"],
                "cool_plate_temp_initial_layer": ["40"],
                "hot_plate_temp": ["60"],
                "nozzle_temperature": ["200"],
                "nozzle_temperature_initial_layer": ["200"],
            }
        ),
        encoding="utf-8",
    )
    (folder / "pla.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "Generic PLA",
                "instantiation": "true",
                "inherits": "fdm_filament_pla",
                "nozzle_temperature": ["220"],
                "hot_plate_temp": ["55"],
            }
        ),
        encoding="utf-8",
    )
    found = {item["name"]: item for item in discover_filaments(tmp_path)}
    assert set(found) == {"Generic PLA"}
    pla = found["Generic PLA"]
    assert pla["nozzle_temperature"] == 220
    assert pla["nozzle_temperature_initial"] == 200
    assert pla["bed_plate"] == "Cool Plate"
    assert pla["bed_temperature"] == 35
    assert pla["bed_temperature_initial"] == 40
    assert pla["vendor"] == "Creality"


def test_brand_folders_and_user_presets_are_searchable(tmp_path: Path) -> None:
    brand = tmp_path / "system" / "OrcaFilamentLibrary" / "filament" / "SUNLU"
    brand.mkdir(parents=True)
    (brand / "base.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "SUNLU PLA+ @base",
                "instantiation": "false",
                "inherits": "fdm_filament_pla",
                "nozzle_temperature": ["210"],
                "hot_plate_temp": ["60"],
            }
        ),
        encoding="utf-8",
    )
    (brand / "system.json").write_text(
        json.dumps(
            {
                "type": "filament",
                "name": "SUNLU PLA+ @System",
                "instantiation": "true",
                "inherits": "SUNLU PLA+ @base",
            }
        ),
        encoding="utf-8",
    )
    user = tmp_path / "user" / "default" / "filament"
    user.mkdir(parents=True)
    (user / "tuned.json").write_text(
        json.dumps(
            {
                "name": "SUNLU PLA+ @System - Tuned",
                "inherits": "SUNLU PLA+ @System",
                "nozzle_temperature": ["205"],
            }
        ),
        encoding="utf-8",
    )
    found = discover_filaments(tmp_path)
    names = {item["name"]: item for item in found}
    assert set(names) == {"SUNLU PLA+ @System", "SUNLU PLA+ @System - Tuned"}
    assert names["SUNLU PLA+ @System"]["vendor"] == "SUNLU"
    assert names["SUNLU PLA+ @System"]["nozzle_temperature"] == 210
    assert names["SUNLU PLA+ @System - Tuned"]["nozzle_temperature"] == 205
    assert [item["name"] for item in search_filaments(found, "sunlu")] == [
        "SUNLU PLA+ @System",
        "SUNLU PLA+ @System - Tuned",
    ]


def test_orca_command_slices_without_arranging(tmp_path: Path) -> None:
    printer = PrinterProfile(
        id="orca:Ender",
        name="Ender",
        definition_id="Ender",
        definition_path=str(tmp_path / "machine.json"),
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
    )
    mesh = tmp_path / "part.stl"
    command = build_orca_command(
        Path("orca-slicer"),
        printer,
        [mesh],
        tmp_path / "out.3mf",
        tmp_path,
        tmp_path / "process.json",
        tmp_path / "filament.json",
        tmp_path / "assemble.json",
    )
    text = " ".join(command)
    assert "--slice" in command and "0" in command
    assert "--export-3mf" in command
    assert "--load-settings" in command
    assert "--load-assemble-list" in command
    assert "--min-save" in command
    assert command[-1] == "--min-save"
    assert "--arrange" not in text
    assert "--assemble" not in text
    assert str(mesh) not in command


def test_assemble_list_keeps_parts_as_separate_objects(tmp_path: Path) -> None:
    meshes = [tmp_path / "a.stl", tmp_path / "b.stl"]
    path = tmp_path / "assemble.json"
    write_assemble_list(path, meshes)
    payload = json.loads(path.read_text(encoding="utf-8"))
    plate = payload["plates"][0]
    assert plate["need_arrange"] is False
    assert len(plate["objects"]) == 2
    for obj in plate["objects"]:
        assert "assemble_index" not in obj


def test_arrange_command_keeps_parts_separate_and_exports_full_3mf(tmp_path: Path) -> None:
    printer = PrinterProfile(
        id="orca:Ender",
        name="Ender",
        definition_id="Ender",
        definition_path=str(tmp_path / "machine.json"),
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
    )
    meshes = [tmp_path / "a.stl", tmp_path / "b.stl"]
    command = build_orca_arrange_command(
        Path("orca-slicer"),
        printer,
        meshes,
        tmp_path / "arranged.3mf",
        tmp_path,
        tmp_path / "process.json",
        tmp_path / "filament.json",
        SliceSettings(
            orca_arrange_spacing=0,
            orca_arrange_rotate=False,
            orca_arrange_multicolor=True,
            orca_arrange_align_y=True,
        ),
    )
    text = " ".join(command)
    assert "--arrange=1" in command
    assert "--allow-rotations=0" in command
    assert "--allow-multicolor-oneplate=1" in command
    assert "--assemble" not in text
    assert "--min-save" not in text
    assert "--load-assemble-list" not in text
    assert str(meshes[0]) in command and str(meshes[1]) in command


def test_process_profile_uses_the_machine_default_print_profile(tmp_path: Path) -> None:
    assert process_inherits_for_machine({"default_print_profile": "0.20mm Standard @Artillery X1"}) == (
        "0.20mm Standard @Artillery X1"
    )
    path = tmp_path / "process.json"
    write_process_profile(
        path,
        SliceSettings(),
        inherits="0.20mm Standard @Artillery X1",
        compatible_printers=["Artillery Sidewinder X1 0.4 nozzle"],
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["inherits"] == "0.20mm Standard @Artillery X1"
    assert payload["compatible_printers"] == ["Artillery Sidewinder X1 0.4 nozzle"]


def test_orca_seam_is_stored_separately_from_cura() -> None:
    values = orca_setting_overrides(SliceSettings(orca_seam="aligned_back"))
    assert values["seam_position"] == "aligned_back"
    loaded = SliceSettings.from_dict({"z_seam_type": "random"})
    assert loaded.orca_seam == "random"


def test_shared_settings_map_onto_orca_keys() -> None:
    values = orca_setting_overrides(SliceSettings())
    assert values["layer_height"] == "0.2"
    assert values["ironing_type"] == "topmost"
    assert values["seam_position"] == "back"
    assert values["seam_slope_type"] == "external"
    assert values["seam_slope_conditional"] == "1"
    off = orca_setting_overrides(SliceSettings(orca_scarf_joint="none", orca_scarf_conditional=False))
    assert off["seam_slope_type"] == "none"
    assert off["seam_slope_conditional"] == "0"
    assert values["sparse_infill_pattern"] == "lightning"
    assert values["sparse_infill_density"] == "5%"
    assert values["brim_type"] == "outer_only"
    assert values["brim_width"] == "8"
    assert values["enable_support"] == "0"


def test_extracts_plate_gcode_from_the_project(tmp_path: Path) -> None:
    archive = tmp_path / "plate.3mf"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("Metadata/plate_1.gcode", ";LAYER_CHANGE\nG1 X1 Y1 E1\n")
    destination = tmp_path / "part.gcode"
    extract_plate_gcode(archive, destination)
    assert "LAYER_CHANGE" in destination.read_text(encoding="utf-8")


def test_front_left_origin_keeps_the_bed_corner() -> None:
    triangle = [((0.0, 0.0, 1.0), (10.0, 0.0, 1.0), (0.0, 8.0, 1.0))]
    placed = transform_mesh(triangle, (0, 0, 0), 30, 40, 220, 220, "front_left")
    xs = [vertex[0] for tri in placed for vertex in tri]
    ys = [vertex[1] for tri in placed for vertex in tri]
    zs = [vertex[2] for tri in placed for vertex in tri]
    assert min(xs) == 30
    assert min(ys) == 40
    assert min(zs) == 0
