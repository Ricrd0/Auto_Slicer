import configparser
import json
from pathlib import Path

from auto_slicer.bundles import export_printer, import_bundle
from auto_slicer.cura_config import active_config_dir, discover_printers
from auto_slicer.engine import build_slice_command
from auto_slicer.settings_schema import SliceSettings
from tests.support import write_printer_tree


def test_stack_order_and_shared_settings_win(tmp_path: Path) -> None:
    config = tmp_path / "config"
    resources = tmp_path / "resources"
    write_printer_tree(config, resources)
    printers = {printer.id: printer for printer in discover_printers(config, resources)}
    shop = printers["shop_printer"]
    assert shop.slicable
    assert shop.version_mismatch
    assert shop.machine_width == 220
    assert [key for key, _value in shop.global_settings] == ["layer_height", "ironing_enabled", "infill_sparse_density"]
    assert shop.global_settings[-1] == ("infill_sparse_density", "20")
    assert printers["dual"].error is not None
    assert printers["missing"].error is not None

    command = build_slice_command(
        Path("/opt/cura/CuraEngine"),
        shop,
        SliceSettings(output_folder_name="building_1"),
        [Path("/tmp/model.stl")],
        Path("/tmp/out.gcode"),
        config,
        resources,
    )
    settings = _settings(command)
    layer_heights = [value for key, value in settings if key == "layer_height"]
    assert layer_heights[0] == "0.28"
    assert layer_heights[-1] == "0.2"
    assert settings[-4:] == [
        ("center_object", "false"),
        ("mesh_position_x", "0"),
        ("mesh_position_y", "0"),
        ("mesh_position_z", "0"),
    ]
    assert command[command.index("-j") + 1].endswith("fdmprinter.def.json")
    machine_index = command.index(str(shop.definition_path))
    assert command.index(str(resources / "definitions" / "fdmprinter.def.json")) < machine_index
    assert "-e0" in command
    assert command[command.index("-l") + 1].endswith("model.stl")


def test_printer_bundle_replaces_and_adds(tmp_path: Path) -> None:
    config = tmp_path / "config"
    resources = tmp_path / "resources"
    write_printer_tree(config, resources)
    shop = next(printer for printer in discover_printers(config, resources) if printer.id == "shop_printer")
    payload = export_printer(shop, config)

    other = tmp_path / "other"
    imported = import_bundle(payload, other)
    assert imported == ["shop_printer"]
    copied = next(printer for printer in discover_printers(other, resources) if printer.id == "shop_printer")
    assert copied.machine_width == 220
    assert copied.global_settings[-1] == ("infill_sparse_density", "20")

    user = config / "definition_changes" / "shop_printer_user.inst.cfg"
    text = user.read_text(encoding="utf-8").replace("20", "12")
    user.write_text(text, encoding="utf-8")
    replaced = import_bundle(export_printer(
        next(printer for printer in discover_printers(config, resources) if printer.id == "shop_printer"),
        config,
    ), other)
    assert replaced == ["shop_printer"]
    updated = next(printer for printer in discover_printers(other, resources) if printer.id == "shop_printer")
    assert updated.global_settings[-1] == ("infill_sparse_density", "12")


def test_version_folder_and_encoded_container_ids(tmp_path: Path) -> None:
    parent = tmp_path / "cura"
    config = parent / "5.11"
    resources = tmp_path / "resources"
    (resources / "definitions").mkdir(parents=True)
    (config / "machine_instances").mkdir(parents=True)
    (config / "user").mkdir(parents=True)
    (config / "extruders").mkdir(parents=True)
    (resources / "definitions" / "fdmprinter.def.json").write_text(
        json.dumps({"name": "FDM", "version": 2, "metadata": {"setting_version": 27}, "overrides": {}}),
        encoding="utf-8",
    )
    (resources / "definitions" / "ender.def.json").write_text(
        json.dumps(
            {
                "name": "Ender",
                "version": 2,
                "inherits": "fdmprinter",
                "overrides": {
                    "machine_width": {"default_value": 220},
                    "machine_depth": {"default_value": 220},
                    "machine_height": {"default_value": 250},
                    "machine_extruder_count": {"default_value": 1},
                },
            }
        ),
        encoding="utf-8",
    )
    (resources / "definitions" / "ender_extruder.def.json").write_text(
        json.dumps({"name": "Extruder", "version": 2, "inherits": "fdmprinter"}),
        encoding="utf-8",
    )
    _write_cfg(
        config / "machine_instances" / "Creality+Ender.global.cfg",
        {
            "general": {"version": "6", "name": "Creality Ender", "id": "Creality Ender"},
            "metadata": {"type": "machine", "setting_version": "27"},
            "containers": {"0": "Creality Ender_user", "1": "ender"},
        },
    )
    _write_cfg(
        config / "user" / "Creality+Ender_user.inst.cfg",
        {
            "general": {"version": "4", "name": "Creality Ender_user"},
            "metadata": {"type": "user"},
            "values": {"layer_height": "0.16"},
        },
    )
    _write_cfg(
        config / "extruders" / "ender_extruder_0+%231.extruder.cfg",
        {
            "general": {"version": "6", "name": "Extruder 1", "id": "ender_extruder_0 #1"},
            "metadata": {"type": "extruder_train", "machine": "Creality Ender", "position": "0", "enabled": "True"},
            "containers": {"0": "ender_extruder"},
        },
    )
    assert active_config_dir(parent) == config
    printers = discover_printers(parent, resources)
    assert len(printers) == 1
    ender = printers[0]
    assert ender.slicable
    assert ender.machine_width == 220
    assert ender.global_settings == [("layer_height", "0.16")]
    assert ender.extruder_definition_id == "ender_extruder"
    assert ender.extruder_count == 1


def _write_cfg(path: Path, sections: dict[str, dict[str, str]]) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    for name, values in sections.items():
        parser[name] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)


def _settings(command: list[str]) -> list[tuple[str, str]]:
    pairs = []
    for index, token in enumerate(command):
        if token == "-s" and index + 1 < len(command):
            key, value = command[index + 1].split("=", 1)
            pairs.append((key, value))
    return pairs
