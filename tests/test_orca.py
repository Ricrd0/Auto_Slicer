import json
import zipfile
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.meshio import transform_mesh
from auto_slicer.orca_config import (
    build_orca_command,
    discover_orca_printers,
    extract_plate_gcode,
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
    assert "--arrange" not in text
    assert str(mesh) not in command


def test_shared_settings_map_onto_orca_keys() -> None:
    values = orca_setting_overrides(SliceSettings())
    assert values["layer_height"] == "0.2"
    assert values["ironing_type"] == "topmost"
    assert values["seam_position"] == "back"
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
