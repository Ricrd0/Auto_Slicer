from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile

GCODE_FLAVORS: tuple[str, ...] = ("Marlin", "RepRap", "Klipper", "UltiGCode")

_NUMBER_FIELDS = (
    "bed_width",
    "bed_depth",
    "bed_height",
    "nozzle_diameter",
    "retraction_length",
    "retraction_speed",
    "z_hop",
    "travel_speed",
    "nozzle_temperature",
    "nozzle_temperature_initial",
    "bed_temperature",
    "bed_temperature_initial",
)
_TEMPERATURE_FIELDS = (
    "nozzle_temperature",
    "nozzle_temperature_initial",
    "bed_temperature",
    "bed_temperature_initial",
)
_SCRIPT_FIELDS = (
    "cura_start_gcode",
    "cura_end_gcode",
    "orca_start_gcode",
    "orca_end_gcode",
)
_ORCA_FLAVOR = {
    "marlin": "marlin",
    "reprap": "reprap",
    "klipper": "klipper",
    "ultigcode": "marlin",
}
# CuraEngine rejects values outside this enum. Klipper runs Marlin gcode, and
# Cura labels that enum value "Marlin".
_CURA_FLAVOR = {
    "Marlin": "RepRap (Marlin/Sprinter)",
    "Klipper": "RepRap (Marlin/Sprinter)",
    "RepRap": "RepRap (RepRap)",
    "UltiGCode": "UltiGCode",
}


def default_machine() -> dict[str, object]:
    return {
        "bed_width": 220.0,
        "bed_depth": 220.0,
        "bed_height": 250.0,
        "nozzle_diameter": 0.4,
        "heated_bed": True,
        "gcode_flavor": "Marlin",
        "retraction_length": 5.0,
        "retraction_speed": 45.0,
        "z_hop": 0.2,
        "travel_speed": 150.0,
        "temperature_override": False,
        "nozzle_temperature": 200.0,
        "nozzle_temperature_initial": 200.0,
        "bed_temperature": 60.0,
        "bed_temperature_initial": 60.0,
        "cura_start_gcode": "",
        "cura_end_gcode": "",
        "orca_start_gcode": "",
        "orca_end_gcode": "",
    }


def normalize_machine(data: object) -> dict[str, object]:
    raw = data if isinstance(data, dict) else {}
    merged = default_machine()
    for key in _NUMBER_FIELDS:
        if key in raw and raw[key] not in (None, ""):
            merged[key] = float(raw[key])
    if "heated_bed" in raw:
        merged["heated_bed"] = bool(raw["heated_bed"])
    if "temperature_override" in raw:
        merged["temperature_override"] = _is_true(raw["temperature_override"])
    if "nozzle_temperature_initial" not in raw or raw.get("nozzle_temperature_initial") in (None, ""):
        merged["nozzle_temperature_initial"] = merged["nozzle_temperature"]
    if "bed_temperature_initial" not in raw or raw.get("bed_temperature_initial") in (None, ""):
        merged["bed_temperature_initial"] = merged["bed_temperature"]
    if "gcode_flavor" in raw and raw["gcode_flavor"] not in (None, ""):
        merged["gcode_flavor"] = str(raw["gcode_flavor"])
    for key in _SCRIPT_FIELDS:
        if key in raw and raw[key] is not None:
            merged[key] = str(raw[key])
    flavor = str(merged["gcode_flavor"])
    if flavor not in GCODE_FLAVORS:
        raise ValueError(f"gcode_flavor must be one of {', '.join(GCODE_FLAVORS)}")
    for key in ("bed_width", "bed_depth", "bed_height", "nozzle_diameter"):
        if float(merged[key]) <= 0:
            raise ValueError(f"{key} must be positive")
    for key in ("retraction_length", "retraction_speed", "z_hop", "travel_speed", *_TEMPERATURE_FIELDS):
        if float(merged[key]) < 0:
            raise ValueError(f"{key} cannot be negative")
    return merged


def normalize_owned(data: dict[str, object]) -> dict[str, object]:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("printer name is required")
    cura_id = data.get("cura_id")
    orca_id = data.get("orca_id")
    return {
        "id": str(data.get("id") or f"owned:{_slug(name)}"),
        "name": name,
        "cura_id": str(cura_id) if cura_id else None,
        "orca_id": str(orca_id) if orca_id else None,
        "settings": normalize_machine(data.get("settings")),
        "cura_settings": _optional_machine(data.get("cura_settings")),
        "orca_settings": _optional_machine(data.get("orca_settings")),
    }


def apply_save_scope(
    existing: dict[str, object] | None,
    incoming: dict[str, object],
    scope: str,
) -> dict[str, object]:
    """Store the form for one engine, or for both.

    A single-engine save leaves the shared setup and the other engine alone.
    Saving for both clears those separate copies.
    """
    if scope not in {"both", "cura", "orca"}:
        raise ValueError("scope must be both, cura, or orca")
    form_settings = incoming["settings"]
    merged = dict(incoming)
    if existing is not None:
        merged["settings"] = existing["settings"]
        merged["cura_settings"] = existing.get("cura_settings")
        merged["orca_settings"] = existing.get("orca_settings")
    if scope == "both":
        merged["settings"] = form_settings
        merged["cura_settings"] = None
        merged["orca_settings"] = None
    elif scope == "cura":
        merged["cura_settings"] = form_settings
    else:
        merged["orca_settings"] = form_settings
    return normalize_owned(merged)


def machine_for_engine(record: dict[str, object], engine: str) -> dict[str, object]:
    key = "orca_settings" if engine == "orca" else "cura_settings"
    custom = record.get(key)
    source = custom if isinstance(custom, dict) else record.get("settings")
    return normalize_machine(source)


def match_score(owned_name: str, candidate_name: str) -> int:
    """How closely an Orca profile name matches a printer the user owns."""
    owned = _normalize_name(owned_name)
    candidate = _normalize_name(candidate_name)
    if not owned or not candidate:
        return 0
    if owned == candidate:
        return 100
    if candidate.startswith(owned + " "):
        return 55
    if owned.startswith(candidate + " "):
        return 40
    return 0


def best_orca_match(name: str, printers: list[PrinterProfile]) -> PrinterProfile | None:
    ranked: list[tuple[int, PrinterProfile]] = []
    for printer in printers:
        score = match_score(name, printer.name)
        if score <= 0:
            continue
        if "0.4" in printer.name:
            score += 5
        if printer.extruder_count <= 1:
            score += 10
        ranked.append((score, printer))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def machine_from_cura(printer: PrinterProfile) -> dict[str, object]:
    values: dict[str, str] = {}
    for key, value in printer.global_settings + printer.extruder_settings:
        values[key] = value
    machine = default_machine()
    machine["bed_width"] = printer.machine_width if printer.machine_width is not None else _float_or(values.get("machine_width"), 220)
    machine["bed_depth"] = printer.machine_depth if printer.machine_depth is not None else _float_or(values.get("machine_depth"), 220)
    machine["bed_height"] = printer.machine_height if printer.machine_height is not None else _float_or(values.get("machine_height"), 250)
    machine["nozzle_diameter"] = _float_or(values.get("machine_nozzle_size"), 0.4)
    if "machine_heated_bed" in values:
        machine["heated_bed"] = values["machine_heated_bed"].lower() == "true"
    flavor = values.get("machine_gcode_flavor", "Marlin")
    machine["gcode_flavor"] = flavor if flavor in GCODE_FLAVORS else "Marlin"
    machine["retraction_length"] = _float_or(values.get("retraction_amount"), 5)
    machine["retraction_speed"] = _float_or(values.get("retraction_speed"), 45)
    machine["z_hop"] = _float_or(values.get("retraction_hop"), 0.2)
    machine["travel_speed"] = _float_or(values.get("speed_travel"), 150)
    machine["nozzle_temperature"] = _float_or(values.get("material_print_temperature"), 200)
    machine["nozzle_temperature_initial"] = _float_or(
        values.get("material_print_temperature_layer_0"), float(machine["nozzle_temperature"])
    )
    machine["bed_temperature"] = _float_or(values.get("material_bed_temperature"), 60)
    machine["bed_temperature_initial"] = _float_or(
        values.get("material_bed_temperature_layer_0"), float(machine["bed_temperature"])
    )
    machine["cura_start_gcode"] = values.get("machine_start_gcode", "")
    machine["cura_end_gcode"] = values.get("machine_end_gcode", "")
    return normalize_machine(machine)


def machine_from_orca(printer: PrinterProfile) -> dict[str, object]:
    data: dict[str, object] = {}
    if printer.definition_path:
        path = Path(printer.definition_path)
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    machine = default_machine()
    if printer.machine_width is not None:
        machine["bed_width"] = printer.machine_width
    if printer.machine_depth is not None:
        machine["bed_depth"] = printer.machine_depth
    if printer.machine_height is not None:
        machine["bed_height"] = printer.machine_height
    nozzle = _first_number(data.get("nozzle_diameter"))
    if nozzle is not None:
        machine["nozzle_diameter"] = nozzle
    retraction = _first_number(data.get("retraction_length"))
    if retraction is not None:
        machine["retraction_length"] = retraction
    speed = _first_number(data.get("retraction_speed"))
    if speed is None:
        speed = _first_number(data.get("deretraction_speed"))
    if speed is not None:
        machine["retraction_speed"] = speed
    hop = _first_number(data.get("z_hop"))
    if hop is not None:
        machine["z_hop"] = hop
    flavor = str(data.get("gcode_flavor") or "").lower()
    by_name = {item.lower(): item for item in GCODE_FLAVORS}
    if flavor in by_name:
        machine["gcode_flavor"] = by_name[flavor]
    else:
        for name, orca_name in _ORCA_FLAVOR.items():
            if flavor == orca_name and name in by_name:
                machine["gcode_flavor"] = by_name[name]
                break
    machine["orca_start_gcode"] = str(data.get("machine_start_gcode") or "")
    machine["orca_end_gcode"] = str(data.get("machine_end_gcode") or "")
    return normalize_machine(machine)


def merge_owned(
    existing: list[dict[str, object]],
    cura_printers: list[PrinterProfile],
    orca_printers: list[PrinterProfile],
) -> list[dict[str, object]]:
    """Add a record for each Cura machine that is not already owned."""
    current = [normalize_owned(item) for item in existing]
    known = {str(item["cura_id"]) for item in current if item.get("cura_id")}
    for printer in cura_printers:
        if printer.id in known:
            continue
        match = best_orca_match(printer.name, orca_printers)
        settings = machine_from_cura(printer)
        if match is not None:
            orca_settings = machine_from_orca(match)
            settings["orca_start_gcode"] = orca_settings["orca_start_gcode"]
            settings["orca_end_gcode"] = orca_settings["orca_end_gcode"]
        current.append(
            normalize_owned(
                {
                    "id": f"owned:{printer.id}",
                    "name": printer.name,
                    "cura_id": printer.id,
                    "orca_id": match.id if match is not None else None,
                    "settings": settings,
                }
            )
        )
    return current


def owned_from_orca(printer: PrinterProfile, existing: list[dict[str, object]]) -> dict[str, object]:
    for item in existing:
        if item.get("orca_id") == printer.id:
            return item
    return normalize_owned(
        {
            "id": f"owned:{_slug(printer.name)}",
            "name": printer.name,
            "cura_id": None,
            "orca_id": printer.id,
            "settings": machine_from_orca(printer),
        }
    )


def owned_for_printer(printer: PrinterProfile, owned: list[dict[str, object]]) -> dict[str, object] | None:
    if printer.engine == "orca":
        matches = [item for item in owned if item.get("orca_id") == printer.id]
    else:
        matches = [item for item in owned if item.get("cura_id") == printer.id]
    if not matches:
        return None
    matches.sort(key=lambda item: match_score(printer.name, str(item["name"])), reverse=True)
    return matches[0]


def filter_owned_orca(
    printers: list[PrinterProfile], owned: list[dict[str, object]]
) -> list[PrinterProfile]:
    wanted = {str(item["orca_id"]) for item in owned if item.get("orca_id")}
    return [printer for printer in printers if printer.id in wanted]


def with_synced_bed(printer: PrinterProfile, machine: dict[str, object]) -> PrinterProfile:
    return replace(
        printer,
        machine_width=float(machine["bed_width"]),
        machine_depth=float(machine["bed_depth"]),
        machine_height=float(machine["bed_height"]),
    )


def cura_machine_overrides(machine: dict[str, object]) -> list[tuple[str, str]]:
    bed = "0" if not machine["heated_bed"] else _num(float(machine["bed_temperature"]))
    bed_initial = "0" if not machine["heated_bed"] else _num(float(machine["bed_temperature_initial"]))
    nozzle = _num(float(machine["nozzle_temperature"]))
    nozzle_initial = _num(float(machine["nozzle_temperature_initial"]))
    values = [
        ("machine_width", _num(float(machine["bed_width"]))),
        ("machine_depth", _num(float(machine["bed_depth"]))),
        ("machine_height", _num(float(machine["bed_height"]))),
        ("machine_nozzle_size", _num(float(machine["nozzle_diameter"]))),
        ("machine_heated_bed", "true" if machine["heated_bed"] else "false"),
        ("machine_gcode_flavor", _CURA_FLAVOR.get(str(machine["gcode_flavor"]), "RepRap (Marlin/Sprinter)")),
        ("retraction_amount", _num(float(machine["retraction_length"]))),
        ("retraction_speed", _num(float(machine["retraction_speed"]))),
        ("retraction_hop", _num(float(machine["z_hop"]))),
        ("speed_travel", _num(float(machine["travel_speed"]))),
        ("material_print_temperature", nozzle),
        ("material_print_temperature_layer_0", nozzle_initial),
        ("material_initial_print_temperature", nozzle_initial),
        ("material_final_print_temperature", nozzle),
        ("default_material_print_temperature", nozzle),
        ("material_bed_temperature", bed),
        ("material_bed_temperature_layer_0", bed_initial),
        ("default_material_bed_temperature", bed),
    ]
    if machine["cura_start_gcode"]:
        values.append(("machine_start_gcode", str(machine["cura_start_gcode"])))
    if machine["cura_end_gcode"]:
        values.append(("machine_end_gcode", str(machine["cura_end_gcode"])))
    return values


def patch_orca_machine(data: dict[str, object], machine: dict[str, object]) -> dict[str, object]:
    patched = dict(data)
    width = _num(float(machine["bed_width"]))
    depth = _num(float(machine["bed_depth"]))
    patched["printable_area"] = ["0x0", f"{width}x0", f"{width}x{depth}", f"0x{depth}"]
    patched["printable_height"] = _num(float(machine["bed_height"]))
    patched["nozzle_diameter"] = [_num(float(machine["nozzle_diameter"]))]
    patched["gcode_flavor"] = _ORCA_FLAVOR.get(str(machine["gcode_flavor"]).lower(), "marlin")
    patched["retraction_length"] = [_num(float(machine["retraction_length"]))]
    patched["retraction_speed"] = [_num(float(machine["retraction_speed"]))]
    patched["deretraction_speed"] = [_num(float(machine["retraction_speed"]))]
    patched["z_hop"] = [_num(float(machine["z_hop"]))]
    if machine["orca_start_gcode"]:
        patched["machine_start_gcode"] = machine["orca_start_gcode"]
    if machine["orca_end_gcode"]:
        patched["machine_end_gcode"] = machine["orca_end_gcode"]
    return patched


def orca_process_extras(machine: dict[str, object]) -> dict[str, str]:
    return {"travel_speed": _num(float(machine["travel_speed"]))}


def apply_filament(machine: dict[str, object], filament: dict[str, object] | None) -> dict[str, object]:
    """Use the shared filament temperatures unless this printer overrides them.

    The filament name is still inherited so Orca keeps that profile's other
    settings. Temperature keys are replaced afterwards.
    """
    merged = dict(machine)
    if filament is not None and not merged.get("temperature_override"):
        for key in _TEMPERATURE_FIELDS:
            merged[key] = float(filament[key])
    if filament is not None and filament.get("orca_name"):
        merged["filament_inherits"] = str(filament["orca_name"])
        merged["filament_name"] = str(filament["name"])
    else:
        merged["filament_inherits"] = "fdm_filament_pla"
        merged["filament_name"] = str(filament["name"]) if filament is not None else "Generic PLA"
    return merged


def normalize_filament(data: dict[str, object], existing_ids: set[str] | None = None) -> dict[str, object]:
    name = str(data.get("name") or "").strip()
    if not name:
        raise ValueError("filament name is required")
    orca_name = str(data.get("orca_name") or "").strip()
    filament = {
        "id": str(data.get("id") or _unique_filament_id(name, existing_ids or set())),
        "name": name,
        "orca_name": orca_name or None,
        "nozzle_temperature": float(data.get("nozzle_temperature", 200)),
        "nozzle_temperature_initial": float(data.get("nozzle_temperature_initial", data.get("nozzle_temperature", 200))),
        "bed_temperature": float(data.get("bed_temperature", 60)),
        "bed_temperature_initial": float(data.get("bed_temperature_initial", data.get("bed_temperature", 60))),
    }
    for key in _TEMPERATURE_FIELDS:
        if float(filament[key]) < 0:
            raise ValueError(f"{key} cannot be negative")
    return filament


def orca_filament_overrides(machine: dict[str, object]) -> dict[str, list[str]]:
    """Put the saved temperatures on every Orca bed type.

    Orca keeps nozzle and bed temperatures on the filament, and the active
    plate (cool, engineering, hot, or textured) chooses which bed key is used.
    """
    bed = "0" if not machine["heated_bed"] else _num(float(machine["bed_temperature"]))
    bed_initial = "0" if not machine["heated_bed"] else _num(float(machine["bed_temperature_initial"]))
    nozzle = _num(float(machine["nozzle_temperature"]))
    nozzle_initial = _num(float(machine["nozzle_temperature_initial"]))
    values = {
        "nozzle_temperature": [nozzle],
        "nozzle_temperature_initial_layer": [nozzle_initial],
    }
    for plate in ("cool_plate", "eng_plate", "hot_plate", "textured_plate"):
        values[f"{plate}_temp"] = [bed]
        values[f"{plate}_temp_initial_layer"] = [bed_initial]
    return values


def _normalize_name(name: str) -> str:
    text = name.lower()
    text = re.sub(r"\d+(?:\.\d+)?\s*mm", " ", text)
    text = re.sub(r"0\.\d+\s*nozzle", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _num(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return format(value, "g")


def _float_or(value: str | None, fallback: float) -> float:
    if value is None:
        return fallback
    try:
        return float(value)
    except ValueError:
        return fallback


def _first_number(value: object) -> float | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "printer"


def _unique_filament_id(name: str, existing_ids: set[str]) -> str:
    base = f"filament:{_slug(name)}"
    if base not in existing_ids:
        return base
    number = 2
    while f"{base}-{number}" in existing_ids:
        number += 1
    return f"{base}-{number}"


def _optional_machine(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return normalize_machine(value)
    raise ValueError("engine settings must be an object")


def _is_true(value: object) -> bool:
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)
