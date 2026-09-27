from __future__ import annotations

import configparser
import json
from pathlib import Path


def write_printer_tree(config_dir: Path, resources_dir: Path) -> None:
    (resources_dir / "definitions").mkdir(parents=True)
    (resources_dir / "extruders").mkdir(parents=True)
    (config_dir / "machine_instances").mkdir(parents=True)
    (config_dir / "definition_changes").mkdir(parents=True)
    (config_dir / "definitions").mkdir(parents=True)

    (resources_dir / "definitions" / "fdmprinter.def.json").write_text(
        json.dumps(
            {
                "name": "FDM Printer",
                "version": 2,
                "metadata": {"setting_version": 25},
                "overrides": {
                    "machine_width": {"default_value": 100},
                    "machine_depth": {"default_value": 100},
                    "machine_height": {"default_value": 100},
                    "machine_center_is_zero": {"default_value": False},
                },
            }
        ),
        encoding="utf-8",
    )
    (resources_dir / "extruders" / "fdmextruder.def.json").write_text(
        json.dumps({"name": "FDM Extruder", "version": 2, "metadata": {}}),
        encoding="utf-8",
    )
    (config_dir / "definitions" / "shop_printer.def.json").write_text(
        json.dumps(
            {
                "name": "Shop Printer",
                "version": 2,
                "inherits": "fdmprinter",
                "metadata": {"machine_extruder_trains": {"0": "shop_extruder"}},
                "overrides": {
                    "machine_width": {"default_value": 220},
                    "machine_depth": {"default_value": 220},
                    "machine_height": {"default_value": 250},
                },
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "definitions" / "shop_extruder.def.json").write_text(
        json.dumps({"name": "Shop Extruder", "version": 2, "inherits": "fdmextruder"}),
        encoding="utf-8",
    )
    _cfg(
        config_dir / "machine_instances" / "shop_printer.global.cfg",
        {
            "general": {"version": "4", "name": "Shop Printer", "id": "shop_printer"},
            "metadata": {
                "type": "machine",
                "setting_version": "22",
                "definition": "shop_printer",
                "machine_extruder_trains": '{"0": "shop_printer_extruder_0"}',
            },
            "containers": {
                "0": "shop_printer_user",
                "1": "shop_printer_settings",
                "2": "shop_printer",
            },
        },
    )
    _cfg(
        config_dir / "definition_changes" / "shop_printer_settings.inst.cfg",
        {
            "general": {"version": "4", "name": "settings", "id": "shop_printer_settings"},
            "metadata": {"type": "definition_changes"},
            "values": {"layer_height": "0.28", "ironing_enabled": "False"},
        },
    )
    _cfg(
        config_dir / "definition_changes" / "shop_printer_user.inst.cfg",
        {
            "general": {"version": "4", "name": "user", "id": "shop_printer_user"},
            "metadata": {"type": "user"},
            "values": {"infill_sparse_density": "20"},
        },
    )
    _cfg(
        config_dir / "machine_instances" / "shop_printer_extruder_0.extruder.cfg",
        {
            "general": {"version": "4", "name": "extruder", "id": "shop_printer_extruder_0"},
            "metadata": {"type": "extruder_train", "position": "0"},
            "containers": {
                "0": "shop_printer_extruder_user",
                "1": "shop_extruder",
            },
        },
    )
    _cfg(
        config_dir / "definition_changes" / "shop_printer_extruder_user.inst.cfg",
        {
            "general": {"version": "4", "name": "extruder user", "id": "shop_printer_extruder_user"},
            "metadata": {"type": "user"},
            "values": {"layer_height": "0.32"},
        },
    )
    _cfg(
        config_dir / "machine_instances" / "dual.global.cfg",
        {
            "general": {"version": "4", "name": "Dual", "id": "dual"},
            "metadata": {
                "type": "machine",
                "setting_version": "25",
                "definition": "shop_printer",
                "machine_extruder_trains": '{"0": "shop_printer_extruder_0", "1": "shop_printer_extruder_0"}',
            },
            "containers": {"0": "shop_printer"},
        },
    )
    _cfg(
        config_dir / "machine_instances" / "missing.global.cfg",
        {
            "general": {"version": "4", "name": "Missing", "id": "missing"},
            "metadata": {
                "type": "machine",
                "setting_version": "25",
                "definition": "does_not_exist",
            },
            "containers": {"0": "does_not_exist"},
        },
    )


def _cfg(path: Path, sections: dict[str, dict[str, str]]) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    for name, values in sections.items():
        parser[name] = values
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        parser.write(handle)
