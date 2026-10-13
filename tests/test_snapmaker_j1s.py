import json
from pathlib import Path

from auto_slicer.machine_sync import machine_from_orca, merge_owned, normalize_machine
from auto_slicer.orca_config import discover_orca_printers, orca_display_name, resolve_orca_machine


def test_orca_display_name_promotes_j1_to_j1s() -> None:
    assert orca_display_name("Snapmaker J1 (0.4 nozzle)") == "Snapmaker J1S"
    assert orca_display_name("Snapmaker J1") == "Snapmaker J1S"
    assert orca_display_name("Creality Ender-3") == "Creality Ender-3"


def test_resolve_orca_machine_reads_inherited_bed_and_marlin2(tmp_path: Path) -> None:
    system = tmp_path / "system" / "Snapmaker" / "machine"
    system.mkdir(parents=True)
    (system / "fdm_common.json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": "fdm_common",
                "instantiation": "false",
                "gcode_flavor": "marlin2",
            }
        ),
        encoding="utf-8",
    )
    (system / "fdm_idex.json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": "fdm_idex",
                "inherits": "fdm_common",
                "instantiation": "false",
                "printable_area": ["0x0", "324x0", "324x200", "0x200"],
                "printable_height": "200",
            }
        ),
        encoding="utf-8",
    )
    (system / "Snapmaker J1 (0.4 nozzle).json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": "Snapmaker J1 (0.4 nozzle)",
                "inherits": "fdm_idex",
                "instantiation": "true",
                "nozzle_diameter": ["0.4", "0.4"],
            }
        ),
        encoding="utf-8",
    )
    printers = discover_orca_printers(tmp_path, include_bundled=False)
    assert len(printers) == 1
    printer = printers[0]
    assert printer.name == "Snapmaker J1S"
    assert printer.id == "orca:Snapmaker J1 (0.4 nozzle)"
    assert printer.machine_width == 324
    assert printer.machine_depth == 200
    assert printer.machine_height == 200
    assert printer.slicable

    machine = machine_from_orca(printer)
    assert machine["bed_width"] == 300
    assert machine["bed_depth"] == 200
    assert machine["bed_height"] == 200
    assert machine["gcode_flavor"] == "Marlin 2"
    assert machine["extruder_count"] == 2
    assert machine["printer_short_id"] == "SMJ1S"

    resolved = resolve_orca_machine(
        json.loads((system / "Snapmaker J1 (0.4 nozzle).json").read_text(encoding="utf-8")),
        tmp_path,
    )
    assert resolved["gcode_flavor"] == "marlin2"
    assert resolved["printable_height"] == "200"


def test_heal_renames_existing_owned_snapmaker(tmp_path: Path) -> None:
    system = tmp_path / "system" / "Snapmaker" / "machine"
    system.mkdir(parents=True)
    (system / "fdm_common.json").write_text(
        json.dumps({"type": "machine", "name": "fdm_common", "instantiation": "false", "gcode_flavor": "marlin2"}),
        encoding="utf-8",
    )
    (system / "fdm_idex.json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": "fdm_idex",
                "inherits": "fdm_common",
                "instantiation": "false",
                "printable_area": ["0x0", "324x0", "324x200", "0x200"],
                "printable_height": "200",
            }
        ),
        encoding="utf-8",
    )
    (system / "j1.json").write_text(
        json.dumps(
            {
                "type": "machine",
                "name": "Snapmaker J1 (0.4 nozzle)",
                "inherits": "fdm_idex",
                "instantiation": "true",
                "nozzle_diameter": ["0.4", "0.4"],
            }
        ),
        encoding="utf-8",
    )
    orca = discover_orca_printers(tmp_path, include_bundled=False)
    existing = [
        {
            "id": "owned:snapmaker",
            "name": "Snapmaker J1 (0.4 nozzle)",
            "cura_id": None,
            "orca_id": "orca:Snapmaker J1 (0.4 nozzle)",
            "settings": normalize_machine(
                {
                    "bed_width": 220,
                    "bed_depth": 220,
                    "bed_height": 250,
                    "gcode_flavor": "Marlin",
                }
            ),
        }
    ]
    owned = merge_owned(existing, [], orca)
    assert owned[0]["name"] == "Snapmaker J1S"
    assert owned[0]["id"] == "owned:snapmaker-j1s"
    assert owned[0]["settings"]["bed_width"] == 300
    assert owned[0]["settings"]["bed_depth"] == 200
    assert owned[0]["settings"]["bed_height"] == 200
    assert owned[0]["settings"]["gcode_flavor"] == "Marlin 2"
