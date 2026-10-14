from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.orca_config import (
    active_orca_dir,
    area_is_center_zero,
    apply_user_orca_machine_override,
    circular_printable_area,
    load_orca_printer,
    orca_display_name,
    resolve_orca_machine,
    user_orca_machine_override,
)
from auto_slicer.settings_schema import (
    COMBING_MODES,
    SliceSettings,
    Z_HOP_ON_SURFACES,
    Z_HOP_TYPES,
    wall_loops_for,
)

GCODE_FLAVORS: tuple[str, ...] = ("Marlin", "Marlin 2", "RepRap", "Klipper", "UltiGCode")
TRANSFER_PROTOCOLS: tuple[str, ...] = ("none", "moonraker", "snapmaker", "sftp")
TRANSFER_MODES: tuple[str, ...] = ("manual", "auto")
_TRANSFER_DEFAULT_PORTS: dict[str, int] = {
    "moonraker": 7125,
    "snapmaker": 8080,
    "sftp": 22,
}
# Snapmaker J1S usable volume (marketing / HMI); Orca's inherited printable_area is 324×200.
_SNAPMAKER_J1S_BED = (300.0, 200.0, 200.0)

_NUMBER_FIELDS = (
    "bed_width",
    "bed_depth",
    "bed_height",
    "nozzle_diameter",
    "retraction_length",
    "retraction_speed",
    "retraction_min_travel",
    "retraction_extrusion_window",
    "retraction_combing_max_distance",
    "z_hop",
    "travel_speed",
    "print_speed",
    "outer_wall_speed",
    "inner_wall_speed",
    "infill_speed",
    "initial_layer_speed",
    "nozzle_temperature",
    "nozzle_temperature_initial",
    "bed_temperature",
    "bed_temperature_initial",
    "idex_offset_x",
)
_PURGE_MARK = ";AUTO_SLICER_PURGE_LINE"
_SPEED_FIELDS = (
    "travel_speed",
    "print_speed",
    "outer_wall_speed",
    "inner_wall_speed",
    "infill_speed",
    "initial_layer_speed",
)
_IDEX_MODE_MACROS = {
    "copy": "ACTIVATE_COPY_MODE",
    "mirror": "ACTIVATE_MIRROR_MODE",
}
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
    "marlin 2": "marlin2",
    "marlin2": "marlin2",
    "reprap": "reprap",
    "klipper": "klipper",
    "ultigcode": "marlin",
}
# CuraEngine rejects values outside this enum. Klipper runs Marlin gcode, and
# Cura labels that enum value "Marlin".
_CURA_FLAVOR = {
    "Marlin": "RepRap (Marlin/Sprinter)",
    "Marlin 2": "RepRap (Marlin/Sprinter)",
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
        "retraction_min_travel": 1.5,
        "retraction_extrusion_window": 2.0,
        "retraction_combing_max_distance": 0.0,
        "combing_override": False,
        "retraction_combing": "noskin",
        "z_hop_enabled": True,
        "z_hop": 0.2,
        "z_hop_type": "Normal Lift",
        "z_hop_on_surfaces": "Top Only",
        "z_hop_override": False,
        "travel_speed": 150.0,
        "print_speed": 60.0,
        "outer_wall_speed": 30.0,
        "inner_wall_speed": 60.0,
        "infill_speed": 80.0,
        "initial_layer_speed": 20.0,
        "temperature_override": False,
        "nozzle_temperature": 200.0,
        "nozzle_temperature_initial": 200.0,
        "bed_temperature": 60.0,
        "bed_temperature_initial": 60.0,
        "cura_start_gcode": "",
        "cura_end_gcode": "",
        "orca_start_gcode": "",
        "orca_end_gcode": "",
        "printer_short_id": "",
        "idex_offset_enabled": False,
        "idex_offset_x": 0.0,
        "idex_copy_mode": False,
        "idex_mirror_mode": False,
        "filament_id_t1": None,
        "extruder_count": 1,
    }


def normalize_machine(data: object, *, printer_name: str | None = None) -> dict[str, object]:
    raw = data if isinstance(data, dict) else {}
    merged = default_machine()
    for key in _NUMBER_FIELDS:
        if key in raw and raw[key] not in (None, ""):
            merged[key] = float(raw[key])
    if "heated_bed" in raw:
        merged["heated_bed"] = bool(raw["heated_bed"])
    if "temperature_override" in raw:
        merged["temperature_override"] = _is_true(raw["temperature_override"])
    if "z_hop_override" in raw:
        merged["z_hop_override"] = _is_true(raw["z_hop_override"])
    if "combing_override" in raw:
        merged["combing_override"] = _is_true(raw["combing_override"])
    if "z_hop_enabled" in raw:
        merged["z_hop_enabled"] = _is_true(raw["z_hop_enabled"])
    elif "z_hop" in raw and raw["z_hop"] not in (None, ""):
        merged["z_hop_enabled"] = float(raw["z_hop"]) > 0
    if "nozzle_temperature_initial" not in raw or raw.get("nozzle_temperature_initial") in (None, ""):
        merged["nozzle_temperature_initial"] = merged["nozzle_temperature"]
    if "bed_temperature_initial" not in raw or raw.get("bed_temperature_initial") in (None, ""):
        merged["bed_temperature_initial"] = merged["bed_temperature"]
    if "gcode_flavor" in raw and raw["gcode_flavor"] not in (None, ""):
        merged["gcode_flavor"] = str(raw["gcode_flavor"])
    if "z_hop_type" in raw and raw["z_hop_type"] not in (None, ""):
        merged["z_hop_type"] = str(raw["z_hop_type"])
    if "z_hop_on_surfaces" in raw and raw["z_hop_on_surfaces"] not in (None, ""):
        merged["z_hop_on_surfaces"] = str(raw["z_hop_on_surfaces"])
    if "retraction_combing" in raw and raw["retraction_combing"] not in (None, ""):
        merged["retraction_combing"] = str(raw["retraction_combing"])
    for key in _SCRIPT_FIELDS:
        if key in raw and raw[key] is not None:
            merged[key] = str(raw[key])
    if "extruder_count" in raw and raw["extruder_count"] not in (None, ""):
        merged["extruder_count"] = max(1, int(float(raw["extruder_count"])))
    short_id = str(raw.get("printer_short_id") or "").strip().upper()
    if not short_id and printer_name:
        short_id = printer_short_id(printer_name)
    merged["printer_short_id"] = short_id
    filament_t1 = raw.get("filament_id_t1")
    merged["filament_id_t1"] = str(filament_t1).strip() if filament_t1 not in (None, "") else None
    merged["idex_offset_enabled"] = _is_true(raw.get("idex_offset_enabled", False))
    merged["idex_copy_mode"] = _is_true(raw.get("idex_copy_mode", False))
    merged["idex_mirror_mode"] = _is_true(raw.get("idex_mirror_mode", False))
    merged = _normalize_idex_coupling(merged, raw)
    flavor = str(merged["gcode_flavor"])
    if flavor not in GCODE_FLAVORS:
        raise ValueError(f"gcode_flavor must be one of {', '.join(GCODE_FLAVORS)}")
    hop_type = str(merged["z_hop_type"])
    if hop_type not in Z_HOP_TYPES:
        raise ValueError(f"z_hop_type must be one of {', '.join(Z_HOP_TYPES)}")
    surfaces = str(merged["z_hop_on_surfaces"])
    if surfaces not in Z_HOP_ON_SURFACES:
        raise ValueError(f"z_hop_on_surfaces must be one of {', '.join(Z_HOP_ON_SURFACES)}")
    combing = str(merged["retraction_combing"])
    if combing not in COMBING_MODES:
        raise ValueError(f"retraction_combing must be one of {', '.join(COMBING_MODES)}")
    for key in ("bed_width", "bed_depth", "bed_height", "nozzle_diameter"):
        if float(merged[key]) <= 0:
            raise ValueError(f"{key} must be positive")
    for key in (
        "retraction_length",
        "retraction_speed",
        "retraction_min_travel",
        "retraction_extrusion_window",
        "retraction_combing_max_distance",
        "z_hop",
        *_SPEED_FIELDS,
        *_TEMPERATURE_FIELDS,
    ):
        if float(merged[key]) < 0:
            raise ValueError(f"{key} cannot be negative")
    if merged["idex_offset_enabled"] and float(merged["idex_offset_x"]) == 0:
        raise ValueError("idex_offset_x must be non-zero when Apply IDEX offset is enabled")
    if merged["idex_copy_mode"] or merged["idex_mirror_mode"]:
        if flavor != "Klipper":
            raise ValueError("copy/mirror modes require the Klipper g-code flavor")
    return merged


def default_transfer() -> dict[str, object]:
    return {
        "protocol": "none",
        "mode": "manual",
        "host": "",
        "port": None,
        "api_key": "",
        "token": "",
        "username": "",
        "password": "",
        "remote_path": "",
        "tls": False,
        "reachable": False,
        "last_probe_ok": False,
        "last_probe_error": None,
    }


def normalize_transfer(data: object | None) -> dict[str, object]:
    """Normalize per-printer remote upload config (not synced to Cura/Orca)."""
    source = data if isinstance(data, dict) else {}
    protocol = str(source.get("protocol") or "none").strip().lower()
    if protocol not in TRANSFER_PROTOCOLS:
        raise ValueError(f"transfer protocol must be one of {', '.join(TRANSFER_PROTOCOLS)}")
    mode = str(source.get("mode") or "manual").strip().lower()
    if mode not in TRANSFER_MODES:
        raise ValueError(f"transfer mode must be one of {', '.join(TRANSFER_MODES)}")
    port_raw = source.get("port")
    port: int | None
    if port_raw in (None, ""):
        port = _TRANSFER_DEFAULT_PORTS.get(protocol)
    else:
        port = int(port_raw)
        if port <= 0 or port > 65535:
            raise ValueError("transfer port must be between 1 and 65535")
    host = str(source.get("host") or "").strip()
    if protocol != "none" and not host:
        raise ValueError("transfer host is required when a protocol is set")
    if mode == "auto" and protocol == "none":
        raise ValueError("auto upload requires a transfer protocol")
    return {
        "protocol": protocol,
        "mode": mode if protocol != "none" else "manual",
        "host": host,
        "port": port,
        "api_key": str(source.get("api_key") or ""),
        "token": str(source.get("token") or ""),
        "username": str(source.get("username") or ""),
        "password": str(source.get("password") or ""),
        "remote_path": str(source.get("remote_path") or "").strip().strip("/"),
        "tls": bool(source.get("tls")),
        "reachable": bool(source.get("reachable")),
        "last_probe_ok": bool(source.get("last_probe_ok")),
        "last_probe_error": (
            str(source["last_probe_error"])
            if source.get("last_probe_error") not in (None, "")
            else None
        ),
    }


def transfer_configured(transfer: dict[str, object] | None) -> bool:
    return bool(transfer) and str(transfer.get("protocol") or "none") != "none"


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
        "settings": normalize_machine(data.get("settings"), printer_name=name),
        "cura_settings": _optional_machine(data.get("cura_settings"), printer_name=name),
        "orca_settings": _optional_machine(data.get("orca_settings"), printer_name=name),
        "transfer": normalize_transfer(data.get("transfer")),
    }


def printer_short_id(name: str) -> str:
    """Build a compact acronym from a printer display name (e.g. SMJ1S, TLD3P)."""
    words = re.findall(r"[A-Za-z0-9]+", name)
    if not words:
        return "IDEX"
    # "Tenlog TL-D3 Pro" → start from the short model token TL.
    if (
        len(words) > 1
        and words[1].isalpha()
        and len(words[1]) <= 3
        and not any(ch.isdigit() for ch in words[1])
    ):
        brand = words[1].upper()
        tail = words[2:]
    else:
        brand = _brand_initials(words[0])
        tail = words[1:]
    rest: list[str] = []
    for word in tail:
        if any(ch.isdigit() for ch in word):
            rest.append(word.upper())
        elif word.isalpha() and len(word) <= 2:
            rest.append(word.upper())
        else:
            rest.append(word[0].upper())
    return re.sub(r"[^A-Z0-9]", "", brand + "".join(rest)) or "IDEX"


def _brand_initials(word: str) -> str:
    letters = [ch for ch in word if ch.isalpha()]
    if not letters:
        return word[:2].upper()
    if len(letters) == 1:
        return letters[0].upper()
    # Snapmaker → SM, Blockmaker → BM, etc.
    if word.lower().endswith("maker") and len(letters) > 5:
        return (letters[0] + "M").upper()
    vowels = set("aeiouAEIOU")
    seen_vowel = letters[0] in vowels
    for ch in letters[1:]:
        if ch in vowels:
            seen_vowel = True
            continue
        if seen_vowel:
            return (letters[0] + ch).upper()
    return (letters[0] + letters[1]).upper()


def idex_offset_active(machine: dict[str, object] | None) -> bool:
    if not machine:
        return False
    return bool(machine.get("idex_offset_enabled")) and float(machine.get("idex_offset_x") or 0) != 0


def idex_zone_width(machine: dict[str, object]) -> float:
    return abs(float(machine["idex_offset_x"])) * 2.0


def idex_output_modes(machine: dict[str, object] | None) -> list[str | None]:
    """Return slice mode tags. ``None`` means a normal (unsuffixed) output."""
    if not machine:
        return [None]
    modes: list[str | None] = []
    if machine.get("idex_copy_mode"):
        modes.append("copy")
    if machine.get("idex_mirror_mode"):
        modes.append("mirror")
    return modes or [None]


def with_idex_mode_gcode(machine: dict[str, object], mode: str | None) -> dict[str, object]:
    """Return a copy with copy/mirror macros injected into start g-code for this slice."""
    if mode not in _IDEX_MODE_MACROS:
        return machine
    merged = dict(machine)
    macro = _IDEX_MODE_MACROS[mode]
    for key in ("cura_start_gcode", "orca_start_gcode"):
        merged[key] = _inject_idex_macro(str(merged.get(key) or ""), macro)
    return merged


def apply_filaments(
    machine: dict[str, object],
    filament_t0: dict[str, object] | None,
    filament_t1: dict[str, object] | None = None,
) -> dict[str, object]:
    """Apply T0 filament temps/names, and stash T1 filament metadata for dual loads."""
    merged = apply_filament(machine, filament_t0)
    secondary = filament_t1 if filament_t1 is not None else filament_t0
    if secondary is not None:
        if not merged.get("temperature_override"):
            merged["nozzle_temperature_t1"] = float(secondary["nozzle_temperature"])
            merged["nozzle_temperature_initial_t1"] = float(secondary["nozzle_temperature_initial"])
        else:
            merged["nozzle_temperature_t1"] = float(merged["nozzle_temperature"])
            merged["nozzle_temperature_initial_t1"] = float(merged["nozzle_temperature_initial"])
        if secondary.get("orca_name"):
            merged["filament_inherits_t1"] = str(secondary["orca_name"])
            merged["filament_name_t1"] = str(secondary["name"])
        else:
            merged["filament_inherits_t1"] = "fdm_filament_pla"
            merged["filament_name_t1"] = str(secondary["name"])
    else:
        merged["nozzle_temperature_t1"] = float(merged["nozzle_temperature"])
        merged["nozzle_temperature_initial_t1"] = float(merged["nozzle_temperature_initial"])
        merged["filament_inherits_t1"] = str(merged.get("filament_inherits") or "fdm_filament_pla")
        merged["filament_name_t1"] = str(merged.get("filament_name") or "Generic PLA")
    return merged


def _normalize_idex_coupling(
    machine: dict[str, object], raw: dict[str, object]
) -> dict[str, object]:
    merged = dict(machine)
    # Explicitly unticking apply-offset clears copy/mirror (UI sends both).
    if "idex_offset_enabled" in raw and not _is_true(raw.get("idex_offset_enabled")):
        merged["idex_offset_enabled"] = False
        merged["idex_copy_mode"] = False
        merged["idex_mirror_mode"] = False
    elif merged["idex_copy_mode"] or merged["idex_mirror_mode"]:
        merged["idex_offset_enabled"] = True
    if str(merged["gcode_flavor"]) != "Klipper":
        merged["idex_copy_mode"] = False
        merged["idex_mirror_mode"] = False
    return merged


def _inject_idex_macro(script: str, macro: str) -> str:
    lines = [
        line
        for line in script.splitlines()
        if line.strip() not in _IDEX_MODE_MACROS.values()
    ]
    insert_at = 0
    for index, line in enumerate(lines):
        if line.strip().startswith("G28"):
            insert_at = index + 1
            break
    lines.insert(insert_at, macro)
    return "\n".join(lines).rstrip() + ("\n" if script.endswith("\n") or not script else "")


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
        merged["transfer"] = _merge_transfer_probe(
            existing.get("transfer") if isinstance(existing.get("transfer"), dict) else None,
            incoming.get("transfer") if isinstance(incoming.get("transfer"), dict) else None,
        )
    if scope == "both":
        merged["settings"] = form_settings
        merged["cura_settings"] = None
        merged["orca_settings"] = None
    elif scope == "cura":
        merged["cura_settings"] = form_settings
    else:
        merged["orca_settings"] = form_settings
    return normalize_owned(merged)


def _merge_transfer_probe(
    existing: dict[str, object] | None,
    incoming: dict[str, object] | None,
) -> dict[str, object]:
    """Keep last probe result unless connection fields changed."""
    base = dict(incoming or default_transfer())
    if existing is None:
        return base
    if _transfer_connection_unchanged(existing, base):
        base["reachable"] = bool(existing.get("reachable"))
        base["last_probe_ok"] = bool(existing.get("last_probe_ok"))
        base["last_probe_error"] = existing.get("last_probe_error")
    else:
        base["reachable"] = False
        base["last_probe_ok"] = False
        base["last_probe_error"] = None
    return base


def _transfer_connection_unchanged(
    existing: dict[str, object], incoming: dict[str, object]
) -> bool:
    for key in ("protocol", "host", "api_key", "token", "username", "password"):
        if str(existing.get(key) or "") != str(incoming.get(key) or ""):
            return False
    if bool(existing.get("tls")) != bool(incoming.get("tls")):
        return False
    if existing.get("port") != incoming.get("port"):
        return False
    return True


def machine_for_engine(record: dict[str, object], engine: str) -> dict[str, object]:
    key = "orca_settings" if engine == "orca" else "cura_settings"
    custom = record.get(key)
    source = custom if isinstance(custom, dict) else record.get("settings")
    return normalize_machine(source, printer_name=str(record.get("name") or ""))


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
    # Snapmaker J1S ↔ Snapmaker J1, and similar suffix variants.
    if owned.startswith(candidate) or candidate.startswith(owned):
        return 50
    return 0


def best_orca_match(
    name: str,
    printers: list[PrinterProfile],
    *,
    prefer_dual: bool = False,
) -> PrinterProfile | None:
    ranked: list[tuple[int, PrinterProfile]] = []
    for printer in printers:
        score = match_score(name, printer.name)
        if score <= 0:
            continue
        if "0.4" in printer.name:
            score += 5
        if prefer_dual:
            if printer.extruder_count > 1:
                score += 10
        elif printer.extruder_count <= 1:
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
    machine["extruder_count"] = max(1, int(printer.extruder_count or 1))
    machine["printer_short_id"] = printer_short_id(printer.name)
    if "machine_heated_bed" in values:
        machine["heated_bed"] = values["machine_heated_bed"].lower() == "true"
    flavor = values.get("machine_gcode_flavor", "Marlin")
    machine["gcode_flavor"] = flavor if flavor in GCODE_FLAVORS else "Marlin"
    # Tenlog/Klipper profiles often keep Cura's Marlin flavor label while start
    # g-code already calls Klipper macros — prefer Klipper when those appear.
    start = values.get("machine_start_gcode", "")
    if "ACTIVATE_COPY_MODE" in start or "ACTIVATE_MIRROR_MODE" in start:
        machine["gcode_flavor"] = "Klipper"
    machine["retraction_length"] = _float_or(values.get("retraction_amount"), 5)
    machine["retraction_speed"] = _float_or(values.get("retraction_speed"), 45)
    machine["z_hop"] = _float_or(values.get("retraction_hop"), 0.2)
    machine["travel_speed"] = _float_or(values.get("speed_travel"), 150)
    machine["print_speed"] = _float_or(values.get("speed_print"), 60)
    machine["outer_wall_speed"] = _float_or(values.get("speed_wall_0"), float(machine["print_speed"]) * 0.5)
    machine["inner_wall_speed"] = _float_or(values.get("speed_wall_x"), float(machine["print_speed"]))
    machine["infill_speed"] = _float_or(values.get("speed_infill"), 80)
    machine["initial_layer_speed"] = _float_or(values.get("speed_layer_0"), 20)
    machine["nozzle_temperature"] = _float_or(values.get("material_print_temperature"), 200)
    machine["nozzle_temperature_initial"] = _float_or(
        values.get("material_print_temperature_layer_0"), float(machine["nozzle_temperature"])
    )
    machine["bed_temperature"] = _float_or(values.get("material_bed_temperature"), 60)
    machine["bed_temperature_initial"] = _float_or(
        values.get("material_bed_temperature_layer_0"), float(machine["bed_temperature"])
    )
    machine["cura_start_gcode"] = start
    machine["cura_end_gcode"] = values.get("machine_end_gcode", "")
    display = orca_display_name(printer.name)
    machine["printer_short_id"] = printer_short_id(display)
    _apply_snapmaker_j1s_defaults(machine, display)
    return normalize_machine(machine, printer_name=display)


def machine_from_orca(printer: PrinterProfile) -> dict[str, object]:
    data: dict[str, object] = {}
    if printer.definition_path:
        path = Path(printer.definition_path)
        if path.is_file():
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
    config_root = Path(printer.config_root) if printer.config_root else None
    data = apply_user_orca_machine_override(data, config_root)
    data = resolve_orca_machine(data, config_root)
    machine = default_machine()
    bed = _bed_size_from_area(data.get("printable_area"))
    if bed is not None:
        machine["bed_width"], machine["bed_depth"] = bed
    elif printer.machine_width is not None and printer.machine_depth is not None:
        machine["bed_width"] = printer.machine_width
        machine["bed_depth"] = printer.machine_depth
    height = _first_number(data.get("printable_height"))
    if height is not None:
        machine["bed_height"] = height
    elif printer.machine_height is not None:
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
    hop_type = data.get("z_hop_types")
    if isinstance(hop_type, list) and hop_type:
        hop_type = hop_type[0]
    if isinstance(hop_type, str) and hop_type in Z_HOP_TYPES:
        machine["z_hop_type"] = hop_type
    flavor = str(data.get("gcode_flavor") or "").lower().replace("_", " ")
    by_name = {item.lower(): item for item in GCODE_FLAVORS}
    if flavor in by_name:
        machine["gcode_flavor"] = by_name[flavor]
    else:
        for name, orca_name in _ORCA_FLAVOR.items():
            if flavor == orca_name or flavor == name:
                machine["gcode_flavor"] = by_name.get(name, "Marlin")
                break
    machine["orca_start_gcode"] = str(data.get("machine_start_gcode") or "")
    machine["orca_end_gcode"] = str(data.get("machine_end_gcode") or "")
    machine["extruder_count"] = max(1, int(printer.extruder_count or 1))
    display = orca_display_name(printer.name)
    machine["printer_short_id"] = printer_short_id(display)
    _apply_snapmaker_j1s_defaults(machine, display)
    return normalize_machine(machine, printer_name=display)


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
        match = best_orca_match(
            printer.name,
            orca_printers,
            prefer_dual=printer.extruder_count > 1,
        )
        settings = machine_from_cura(printer)
        if match is not None:
            orca_settings = machine_from_orca(match)
            settings["orca_start_gcode"] = orca_settings["orca_start_gcode"]
            settings["orca_end_gcode"] = orca_settings["orca_end_gcode"]
            # Keep Klipper when Cura start g-code already uses Klipper macros.
            if settings.get("gcode_flavor") != "Klipper":
                settings["gcode_flavor"] = orca_settings["gcode_flavor"]
            settings["extruder_count"] = max(
                int(settings.get("extruder_count") or 1),
                int(orca_settings.get("extruder_count") or 1),
                int(match.extruder_count or 1),
            )
        current.append(
            normalize_owned(
                {
                    "id": f"owned:{printer.id}",
                    "name": orca_display_name(printer.name),
                    "cura_id": printer.id,
                    "orca_id": match.id if match is not None else None,
                    "settings": settings,
                }
            )
        )
    return _heal_tenlog_owned(
        _heal_gemini_owned(
            _heal_kossel_owned(_heal_snapmaker_j1s_owned(current, orca_printers), orca_printers),
            orca_printers,
        ),
        orca_printers,
    )


def sync_owned_with_orca_user_profiles(
    owned: list[dict[str, object]], config_dir: Path | None
) -> list[dict[str, object]]:
    """Heal owned Marlin flavor when the user's Orca preset is Klipper.

    Cura seeds owned printers as Marlin. Many Orca users keep a
    ``… - Klipper`` preset that inherits the system nozzle profile; without
    adopting that flavor, Orca CLI validation fails with exit -51.
    """
    if config_dir is None:
        return owned
    updated: list[dict[str, object]] = []
    for record in owned:
        item = dict(record)
        orca_id = item.get("orca_id")
        if not orca_id:
            updated.append(item)
            continue
        base_name = str(orca_id).removeprefix("orca:")
        override = user_orca_machine_override(config_dir, base_name)
        if override is None or str(override.get("gcode_flavor") or "").lower() != "klipper":
            updated.append(item)
            continue
        changed = False
        for key in ("settings", "orca_settings"):
            settings = item.get(key)
            if not isinstance(settings, dict):
                continue
            if settings.get("gcode_flavor") != "Marlin":
                continue
            healed = dict(settings)
            healed["gcode_flavor"] = "Klipper"
            if not healed.get("orca_start_gcode") and override.get("machine_start_gcode"):
                healed["orca_start_gcode"] = str(override["machine_start_gcode"])
            if not healed.get("orca_end_gcode") and override.get("machine_end_gcode"):
                healed["orca_end_gcode"] = str(override["machine_end_gcode"])
            item[key] = normalize_machine(healed)
            changed = True
        updated.append(normalize_owned(item) if changed else item)
    return updated


def owned_from_orca(printer: PrinterProfile, existing: list[dict[str, object]]) -> dict[str, object]:
    for item in existing:
        if item.get("orca_id") == printer.id:
            return item
    display = orca_display_name(printer.name)
    return normalize_owned(
        {
            "id": f"owned:{_slug(display)}",
            "name": display,
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


def printers_for_page(
    discovered: list[PrinterProfile],
    owned: list[dict[str, object]],
    *,
    engine: str,
    catalog: bool = False,
) -> list[PrinterProfile]:
    """Printers page list: owned machines, using the linked engine profile when present.

    Unlinked owned printers still appear (with a note) so Cura-only machines show
    under Orca and Orca-only machines show under Cura. Linked Orca printers are
    resolved from disk by id (no full catalog scan) when the catalog list is empty.
    """
    if catalog:
        return list(discovered)
    by_id = {printer.id: printer for printer in discovered}
    result: list[PrinterProfile] = []
    linked: set[str] = set()
    for item in owned:
        link_id = item.get("orca_id") if engine == "orca" else item.get("cura_id")
        name = str(item.get("name") or "Printer")
        settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
        settings = settings if isinstance(settings, dict) else {}
        if link_id and str(link_id) in by_id:
            printer = by_id[str(link_id)]
            result.append(replace(printer, name=name))
            linked.add(str(link_id))
            continue
        if link_id and engine == "orca":
            resolved = _resolve_owned_orca_link(str(link_id), name, settings)
            if resolved is not None:
                result.append(resolved)
                linked.add(str(link_id))
                continue
            result.append(
                _owned_stub_profile(
                    str(link_id),
                    name,
                    settings,
                    engine=engine,
                    error="Orca machine profile file was not found.",
                )
            )
            linked.add(str(link_id))
            continue
        if link_id:
            result.append(
                _owned_stub_profile(
                    str(link_id),
                    name,
                    settings,
                    engine=engine,
                    error=None,
                    warnings=(
                        ["Cura profile details still loading — bed size from saved settings."]
                        if not discovered
                        else []
                    ),
                )
            )
            linked.add(str(link_id))
            continue
        note = (
            "No Orca profile linked — open Printer settings to choose one."
            if engine == "orca"
            else "No Cura profile linked — open Printer settings after adding this machine in Cura."
        )
        result.append(
            _owned_stub_profile(
                str(item.get("id") or f"owned:{_slug(name)}"),
                name,
                settings,
                engine=engine,
                error=note,
            )
        )
    if engine != "orca":
        for printer in discovered:
            if printer.id not in linked:
                result.append(printer)
    return result


def _resolve_owned_orca_link(
    orca_id: str, name: str, settings: dict[str, object]
) -> PrinterProfile | None:
    bed_width = float(settings["bed_width"]) if settings.get("bed_width") not in (None, "") else None
    bed_depth = float(settings["bed_depth"]) if settings.get("bed_depth") not in (None, "") else None
    bed_height = float(settings["bed_height"]) if settings.get("bed_height") not in (None, "") else None
    extruders = None
    if settings.get("extruder_count") not in (None, ""):
        extruders = max(1, int(float(settings["extruder_count"])))
    return load_orca_printer(
        active_orca_dir(),
        orca_id,
        display_name=name,
        bed=(bed_width, bed_depth, bed_height),
        extruder_count=extruders,
    )


def _owned_stub_profile(
    profile_id: str,
    name: str,
    settings: dict[str, object],
    *,
    engine: str,
    error: str | None,
    warnings: list[str] | None = None,
) -> PrinterProfile:
    width = float(settings["bed_width"]) if settings.get("bed_width") not in (None, "") else None
    depth = float(settings["bed_depth"]) if settings.get("bed_depth") not in (None, "") else None
    height = float(settings["bed_height"]) if settings.get("bed_height") not in (None, "") else None
    extruders = max(1, int(float(settings.get("extruder_count") or 1)))
    return PrinterProfile(
        id=profile_id,
        name=name,
        definition_id="",
        definition_path=None,
        extruder_definition_id=None,
        extruder_definition_path=None,
        global_settings=[],
        extruder_settings=[],
        machine_width=width,
        machine_depth=depth,
        machine_height=height,
        machine_center_is_zero=False,
        extruder_count=extruders,
        setting_version=None,
        resource_setting_version=None,
        warnings=list(warnings or []),
        error=error,
        engine=engine,
    )


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
    extruders = max(1, int(machine.get("extruder_count") or 1))
    values = [
        ("machine_width", _num(float(machine["bed_width"]))),
        ("machine_depth", _num(float(machine["bed_depth"]))),
        ("machine_height", _num(float(machine["bed_height"]))),
        ("machine_nozzle_size", _num(float(machine["nozzle_diameter"]))),
        ("machine_extruder_count", str(extruders)),
        ("machine_heated_bed", "true" if machine["heated_bed"] else "false"),
        ("machine_gcode_flavor", _CURA_FLAVOR.get(str(machine["gcode_flavor"]), "RepRap (Marlin/Sprinter)")),
        ("retraction_amount", _num(float(machine["retraction_length"]))),
        ("retraction_speed", _num(float(machine["retraction_speed"]))),
        ("retraction_min_travel", _num(float(machine["retraction_min_travel"]))),
        ("retraction_extrusion_window", _num(float(machine["retraction_extrusion_window"]))),
        ("retraction_combing_max_distance", _num(float(machine["retraction_combing_max_distance"]))),
        ("retraction_combing", str(machine["retraction_combing"])),
        ("retraction_hop", _num(_effective_z_hop(machine))),
        ("retraction_hop_enabled", "true" if _z_hop_active(machine) else "false"),
        ("speed_travel", _num(float(machine["travel_speed"]))),
        ("speed_print", _num(float(machine["print_speed"]))),
        ("speed_wall_0", _num(float(machine["outer_wall_speed"]))),
        ("speed_wall_x", _num(float(machine["inner_wall_speed"]))),
        ("speed_infill", _num(float(machine["infill_speed"]))),
        ("speed_layer_0", _num(float(machine["initial_layer_speed"]))),
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


def cura_extruder_temperature_overrides(
    machine: dict[str, object], tool: int
) -> list[tuple[str, str]]:
    if tool <= 0:
        nozzle = _num(float(machine["nozzle_temperature"]))
        nozzle_initial = _num(float(machine["nozzle_temperature_initial"]))
    else:
        nozzle = _num(float(machine.get("nozzle_temperature_t1", machine["nozzle_temperature"])))
        nozzle_initial = _num(
            float(machine.get("nozzle_temperature_initial_t1", machine["nozzle_temperature_initial"]))
        )
    return [
        ("material_print_temperature", nozzle),
        ("material_print_temperature_layer_0", nozzle_initial),
        ("material_initial_print_temperature", nozzle_initial),
        ("material_final_print_temperature", nozzle),
        ("default_material_print_temperature", nozzle),
    ]


def patch_orca_machine(
    data: dict[str, object],
    machine: dict[str, object],
    *,
    use_idex_zone: bool = False,
) -> dict[str, object]:
    patched = dict(data)
    width = float(machine["bed_width"])
    depth = float(machine["bed_depth"])
    if use_idex_zone and idex_offset_active(machine):
        width = idex_zone_width(machine)
        width_s = _num(width)
        depth_s = _num(depth)
        patched["printable_area"] = ["0x0", f"{width_s}x0", f"{width_s}x{depth_s}", f"0x{depth_s}"]
    elif area_is_center_zero(data.get("printable_area")) or str(
        data.get("printer_structure") or ""
    ).lower() == "delta":
        # Keep a circular / center-origin bed instead of forcing a front-left rectangle.
        diameter = min(width, depth)
        patched["printable_area"] = circular_printable_area(diameter)
    else:
        width_s = _num(width)
        depth_s = _num(depth)
        patched["printable_area"] = ["0x0", f"{width_s}x0", f"{width_s}x{depth_s}", f"0x{depth_s}"]
    patched["printable_height"] = _num(float(machine["bed_height"]))
    nozzle = _num(float(machine["nozzle_diameter"]))
    extruders = max(1, int(machine.get("extruder_count") or 1))
    hop = _num(_effective_z_hop(machine))
    hop_type = str(machine["z_hop_type"])
    surfaces = str(machine.get("z_hop_on_surfaces") or "Top Only")
    if extruders > 1:
        patched["nozzle_diameter"] = [nozzle, nozzle]
        patched["retraction_length"] = [
            _num(float(machine["retraction_length"])),
            _num(float(machine["retraction_length"])),
        ]
        patched["retraction_speed"] = [
            _num(float(machine["retraction_speed"])),
            _num(float(machine["retraction_speed"])),
        ]
        patched["deretraction_speed"] = [
            _num(float(machine["retraction_speed"])),
            _num(float(machine["retraction_speed"])),
        ]
        patched["z_hop"] = [hop, hop]
        patched["z_hop_types"] = [hop_type, hop_type]
        patched["retract_lift_enforce"] = [surfaces, surfaces]
    else:
        patched["nozzle_diameter"] = [nozzle]
        patched["retraction_length"] = [_num(float(machine["retraction_length"]))]
        patched["retraction_speed"] = [_num(float(machine["retraction_speed"]))]
        patched["deretraction_speed"] = [_num(float(machine["retraction_speed"]))]
        patched["z_hop"] = [hop]
        patched["z_hop_types"] = [hop_type]
        patched["retract_lift_enforce"] = [surfaces]
    patched["gcode_flavor"] = _ORCA_FLAVOR.get(str(machine["gcode_flavor"]).lower(), "marlin")
    if machine["orca_start_gcode"]:
        patched["machine_start_gcode"] = machine["orca_start_gcode"]
    if machine["orca_end_gcode"]:
        patched["machine_end_gcode"] = machine["orca_end_gcode"]
    return patched


def orca_process_extras(machine: dict[str, object], settings: SliceSettings | None = None) -> dict[str, str]:
    extras = {
        "travel_speed": _num(float(machine["travel_speed"])),
        "outer_wall_speed": _num(float(machine["outer_wall_speed"])),
        "inner_wall_speed": _num(float(machine["inner_wall_speed"])),
        "sparse_infill_speed": _num(float(machine["infill_speed"])),
        "internal_solid_infill_speed": _num(float(machine["infill_speed"])),
        "initial_layer_speed": _num(float(machine["initial_layer_speed"])),
        "initial_layer_infill_speed": _num(float(machine["initial_layer_speed"])),
    }
    if settings is not None:
        extras["wall_loops"] = str(
            wall_loops_for(settings.wall_thickness, float(machine["nozzle_diameter"]))
        )
    return extras


def apply_shared_z_hop(
    machine: dict[str, object],
    z_hop: float,
    z_hop_type: str,
    *,
    z_hop_enabled: bool = True,
    z_hop_on_surfaces: str = "Top Only",
) -> dict[str, object]:
    """Use shared slice Z hop unless this printer overrides it."""
    if machine.get("z_hop_override"):
        return machine
    merged = dict(machine)
    merged["z_hop_enabled"] = bool(z_hop_enabled)
    merged["z_hop"] = float(z_hop)
    merged["z_hop_type"] = str(z_hop_type)
    merged["z_hop_on_surfaces"] = str(z_hop_on_surfaces)
    return merged


def apply_shared_slice_to_machine(
    machine: dict[str, object],
    settings: SliceSettings,
) -> dict[str, object]:
    """Apply shared slice settings that printers may override, plus start-gcode purge."""
    merged = apply_shared_z_hop(
        machine,
        settings.z_hop,
        settings.z_hop_type,
        z_hop_enabled=settings.z_hop_enabled,
        z_hop_on_surfaces=settings.z_hop_on_surfaces,
    )
    if not merged.get("combing_override"):
        merged = dict(merged)
        merged["retraction_combing"] = settings.retraction_combing
    if settings.purge_line:
        merged = dict(merged)
        depth = float(merged.get("bed_depth") or 200.0)
        for key in ("cura_start_gcode", "orca_start_gcode"):
            merged[key] = with_purge_line(str(merged.get(key) or ""), bed_depth=depth)
    return merged


def with_purge_line(script: str, *, bed_depth: float = 200.0) -> str:
    """Append a bed-edge purge line to custom start G-code when missing.

    Empty scripts are left alone so the slicer profile's own start G-code
    (which often already primes) is preserved.
    """
    text = script or ""
    if not text.strip() or _PURGE_MARK in text:
        return text
    y_end = min(80.0, max(30.0, float(bed_depth) - 20.0))
    block = (
        f"{_PURGE_MARK}\n"
        "G92 E0\n"
        "G1 Z0.3 F3000\n"
        "G1 X5 Y10 F6000\n"
        f"G1 Y{_num(y_end)} E12 F1500\n"
        "G1 X5.4 F6000\n"
        "G1 Y10 E24 F1500\n"
        "G92 E0\n"
        "G1 Z2 F3000\n"
        ";AUTO_SLICER_PURGE_LINE_END\n"
    )
    return text.rstrip() + "\n" + block


def _z_hop_active(machine: dict[str, object]) -> bool:
    return bool(machine.get("z_hop_enabled", True)) and float(machine.get("z_hop") or 0) > 0


def _effective_z_hop(machine: dict[str, object]) -> float:
    return float(machine["z_hop"]) if _z_hop_active(machine) else 0.0


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


def orca_filament_overrides(machine: dict[str, object], *, tool: int = 0) -> dict[str, list[str]]:
    """Put the saved temperatures on every Orca bed type.

    Orca keeps nozzle and bed temperatures on the filament, and the active
    plate (cool, engineering, hot, or textured) chooses which bed key is used.
    """
    bed = "0" if not machine["heated_bed"] else _num(float(machine["bed_temperature"]))
    bed_initial = "0" if not machine["heated_bed"] else _num(float(machine["bed_temperature_initial"]))
    if tool <= 0:
        nozzle = _num(float(machine["nozzle_temperature"]))
        nozzle_initial = _num(float(machine["nozzle_temperature_initial"]))
    else:
        nozzle = _num(float(machine.get("nozzle_temperature_t1", machine["nozzle_temperature"])))
        nozzle_initial = _num(
            float(machine.get("nozzle_temperature_initial_t1", machine["nozzle_temperature_initial"]))
        )
    hop = _num(_effective_z_hop(machine))
    values = {
        "nozzle_temperature": [nozzle],
        "nozzle_temperature_initial_layer": [nozzle_initial],
        "filament_z_hop": [hop],
        "filament_z_hop_types": [str(machine["z_hop_type"])],
        "filament_retract_lift_enforce": [str(machine.get("z_hop_on_surfaces") or "Top Only")],
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


def _optional_machine(
    value: object, *, printer_name: str | None = None
) -> dict[str, object] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return normalize_machine(value, printer_name=printer_name)
    raise ValueError("engine settings must be an object")


def _is_true(value: object) -> bool:
    if isinstance(value, str):
        return value.lower() == "true"
    return bool(value)


def _heal_kossel_owned(
    owned: list[dict[str, object]], orca_printers: list[PrinterProfile]
) -> list[dict[str, object]]:
    """Ensure the bundled Anycubic Kossel Linear Plus Orca profile is owned."""
    target_id = "orca:Anycubic Kossel Linear Plus 0.4 nozzle"
    match = next((printer for printer in orca_printers if printer.id == target_id), None)
    if match is None:
        match = best_orca_match("Anycubic Kossel Linear Plus", orca_printers, prefer_dual=False)
    if match is None:
        return owned
    updated: list[dict[str, object]] = []
    found = False
    for record in owned:
        name = str(record.get("name") or "").lower()
        orca_id = str(record.get("orca_id") or "")
        if orca_id == match.id or "kossel" in name:
            item = dict(record)
            item["name"] = "Anycubic Kossel Linear Plus"
            item["id"] = "owned:anycubic-kossel-linear-plus"
            item["orca_id"] = match.id
            settings = machine_from_orca(match)
            _apply_kossel_defaults(settings)
            item["settings"] = settings
            if isinstance(item.get("orca_settings"), dict):
                orca_settings = machine_from_orca(match)
                _apply_kossel_defaults(orca_settings)
                item["orca_settings"] = orca_settings
            updated.append(normalize_owned(item))
            found = True
            continue
        updated.append(record)
    if not found:
        settings = machine_from_orca(match)
        _apply_kossel_defaults(settings)
        updated.append(
            normalize_owned(
                {
                    "id": "owned:anycubic-kossel-linear-plus",
                    "name": "Anycubic Kossel Linear Plus",
                    "cura_id": None,
                    "orca_id": match.id,
                    "settings": settings,
                }
            )
        )
    return updated


def _apply_kossel_defaults(machine: dict[str, object]) -> None:
    machine["bed_width"] = 230.0
    machine["bed_depth"] = 230.0
    machine["bed_height"] = 300.0
    machine["nozzle_diameter"] = 0.4
    machine["gcode_flavor"] = "Klipper"
    machine["retraction_length"] = 4.0
    machine["retraction_speed"] = 35.0
    machine["print_speed"] = 80.0
    machine["outer_wall_speed"] = 40.0
    machine["inner_wall_speed"] = 80.0
    machine["infill_speed"] = 100.0
    machine["initial_layer_speed"] = 25.0
    machine["travel_speed"] = 150.0
    machine["printer_short_id"] = printer_short_id("Anycubic Kossel Linear Plus")


def _is_gemini(name: str) -> bool:
    lowered = name.lower()
    return "gemini" in lowered and "tronxy" in lowered


def _is_tenlog(name: str) -> bool:
    lowered = name.lower()
    return "tenlog" in lowered or "tl-d3" in lowered or "tld3" in lowered.replace(" ", "")


def _apply_idex_dd_defaults(machine: dict[str, object], display: str, bed: tuple[float, float, float]) -> None:
    machine["bed_width"], machine["bed_depth"], machine["bed_height"] = bed
    machine["nozzle_diameter"] = 0.4
    machine["gcode_flavor"] = "Klipper"
    machine["extruder_count"] = max(2, int(machine.get("extruder_count") or 1))
    machine["retraction_length"] = 0.8
    machine["retraction_speed"] = 35.0
    machine["print_speed"] = 80.0
    machine["outer_wall_speed"] = 40.0
    machine["inner_wall_speed"] = 80.0
    machine["infill_speed"] = 100.0
    machine["initial_layer_speed"] = 25.0
    machine["travel_speed"] = 150.0
    machine["printer_short_id"] = printer_short_id(display)


def _heal_gemini_owned(
    owned: list[dict[str, object]], orca_printers: list[PrinterProfile]
) -> list[dict[str, object]]:
    """Ensure the bundled Tronxy Gemini S Orca IDEX profile is owned."""
    target_id = "orca:Tronxy Gemini S 0.4 nozzle"
    match = next((printer for printer in orca_printers if printer.id == target_id), None)
    if match is None:
        match = best_orca_match("Tronxy Gemini S", orca_printers, prefer_dual=True)
    if match is None:
        return owned
    updated: list[dict[str, object]] = []
    found = False
    for record in owned:
        name = str(record.get("name") or "")
        orca_id = str(record.get("orca_id") or "")
        if orca_id == match.id or _is_gemini(name) or _is_gemini(orca_id):
            item = dict(record)
            item["name"] = "Tronxy Gemini S"
            item["id"] = "owned:tronxy-gemini-s"
            item["orca_id"] = match.id
            settings = machine_from_orca(match)
            _apply_idex_dd_defaults(settings, "Tronxy Gemini S", (300.0, 300.0, 390.0))
            item["settings"] = settings
            if isinstance(item.get("orca_settings"), dict):
                orca_settings = machine_from_orca(match)
                _apply_idex_dd_defaults(orca_settings, "Tronxy Gemini S", (300.0, 300.0, 390.0))
                item["orca_settings"] = orca_settings
            updated.append(normalize_owned(item))
            found = True
            continue
        updated.append(record)
    if not found:
        settings = machine_from_orca(match)
        _apply_idex_dd_defaults(settings, "Tronxy Gemini S", (300.0, 300.0, 390.0))
        updated.append(
            normalize_owned(
                {
                    "id": "owned:tronxy-gemini-s",
                    "name": "Tronxy Gemini S",
                    "cura_id": None,
                    "orca_id": match.id,
                    "settings": settings,
                }
            )
        )
    return updated


def _heal_tenlog_owned(
    owned: list[dict[str, object]], orca_printers: list[PrinterProfile]
) -> list[dict[str, object]]:
    """Ensure the bundled Tenlog TL-D3 Pro Orca IDEX profile is owned.

    Preserves any existing custom Orca start/end g-code the user already saved.
    """
    target_id = "orca:Tenlog TL-D3 Pro 0.4 nozzle"
    match = next((printer for printer in orca_printers if printer.id == target_id), None)
    if match is None:
        match = best_orca_match("Tenlog TL-D3 Pro", orca_printers, prefer_dual=True)
    if match is None:
        return owned
    updated: list[dict[str, object]] = []
    found = False
    for record in owned:
        name = str(record.get("name") or "")
        orca_id = str(record.get("orca_id") or "")
        if orca_id == match.id or _is_tenlog(name) or _is_tenlog(orca_id):
            item = dict(record)
            prior_settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
            prior_start = str(prior_settings.get("orca_start_gcode") or "").strip()
            prior_end = str(prior_settings.get("orca_end_gcode") or "").strip()
            item["name"] = "Tenlog TL-D3 Pro"
            item["id"] = "owned:tenlog-tl-d3-pro"
            item["orca_id"] = match.id
            settings = machine_from_orca(match)
            _apply_idex_dd_defaults(settings, "Tenlog TL-D3 Pro", (300.0, 290.0, 350.0))
            if prior_start:
                settings["orca_start_gcode"] = prior_start
            if prior_end:
                settings["orca_end_gcode"] = prior_end
            item["settings"] = settings
            if isinstance(item.get("orca_settings"), dict):
                orca_settings = machine_from_orca(match)
                _apply_idex_dd_defaults(orca_settings, "Tenlog TL-D3 Pro", (300.0, 290.0, 350.0))
                prior_orca = item["orca_settings"]
                prior_orca_start = str(prior_orca.get("orca_start_gcode") or "").strip()
                prior_orca_end = str(prior_orca.get("orca_end_gcode") or "").strip()
                if prior_orca_start:
                    orca_settings["orca_start_gcode"] = prior_orca_start
                if prior_orca_end:
                    orca_settings["orca_end_gcode"] = prior_orca_end
                item["orca_settings"] = orca_settings
            updated.append(normalize_owned(item))
            found = True
            continue
        updated.append(record)
    if not found:
        settings = machine_from_orca(match)
        _apply_idex_dd_defaults(settings, "Tenlog TL-D3 Pro", (300.0, 290.0, 350.0))
        updated.append(
            normalize_owned(
                {
                    "id": "owned:tenlog-tl-d3-pro",
                    "name": "Tenlog TL-D3 Pro",
                    "cura_id": None,
                    "orca_id": match.id,
                    "settings": settings,
                }
            )
        )
    return updated


def _is_snapmaker_j1(name: str) -> bool:
    return bool(re.match(r"(?i)^snapmaker\s+j1", name.strip())) or "snapmaker j1" in name.lower()


def _apply_snapmaker_j1s_defaults(machine: dict[str, object], name: str) -> None:
    if not _is_snapmaker_j1(name):
        return
    machine["bed_width"], machine["bed_depth"], machine["bed_height"] = _SNAPMAKER_J1S_BED
    # Orca's Snapmaker stack uses marlin2; keep Klipper if the user already chose it.
    if str(machine.get("gcode_flavor") or "") not in {"Klipper"}:
        machine["gcode_flavor"] = "Marlin 2"
    machine["extruder_count"] = max(2, int(machine.get("extruder_count") or 1))
    machine["printer_short_id"] = printer_short_id("Snapmaker J1S")


def _heal_snapmaker_j1s_owned(
    owned: list[dict[str, object]], orca_printers: list[PrinterProfile]
) -> list[dict[str, object]]:
    """Rename legacy J1 nozzle profiles to J1S and refresh bed/flavor."""
    by_orca = {printer.id: printer for printer in orca_printers}
    healed: list[dict[str, object]] = []
    seen_j1s = False
    for record in owned:
        item = dict(record)
        name = str(item.get("name") or "")
        orca_id = str(item.get("orca_id") or "")
        legacy_name = "(0.4 nozzle)" in name.lower() and _is_snapmaker_j1(name)
        if not (_is_snapmaker_j1(name) or _is_snapmaker_j1(orca_id)):
            healed.append(item)
            continue
        if seen_j1s and legacy_name:
            # Drop duplicate legacy Snapmaker J1 (0.4 nozzle) once a J1S exists.
            continue
        item["name"] = "Snapmaker J1S"
        item["id"] = "owned:snapmaker-j1s"
        match = by_orca.get(orca_id) if orca_id else None
        if match is None:
            match = best_orca_match("Snapmaker J1S", orca_printers, prefer_dual=True)
            if match is not None:
                item["orca_id"] = match.id
        refreshed = machine_from_orca(match) if match is not None else None
        for key in ("settings", "orca_settings", "cura_settings"):
            current = item.get(key)
            if key != "settings" and not isinstance(current, dict):
                continue
            base = dict(current) if isinstance(current, dict) else {}
            if refreshed is not None:
                base.update(
                    {
                        "bed_width": refreshed["bed_width"],
                        "bed_depth": refreshed["bed_depth"],
                        "bed_height": refreshed["bed_height"],
                        "gcode_flavor": refreshed["gcode_flavor"],
                        "extruder_count": refreshed["extruder_count"],
                        "printer_short_id": refreshed["printer_short_id"],
                    }
                )
                if refreshed.get("orca_start_gcode") and not base.get("orca_start_gcode"):
                    base["orca_start_gcode"] = refreshed["orca_start_gcode"]
                if refreshed.get("orca_end_gcode") and not base.get("orca_end_gcode"):
                    base["orca_end_gcode"] = refreshed["orca_end_gcode"]
            else:
                _apply_snapmaker_j1s_defaults(base, "Snapmaker J1S")
            item[key] = normalize_machine(base, printer_name="Snapmaker J1S")
        healed.append(normalize_owned(item))
        seen_j1s = True
    return healed


def _bed_size_from_area(area: object) -> tuple[float, float] | None:
    if not isinstance(area, list):
        return None
    xs: list[float] = []
    ys: list[float] = []
    for point in area:
        if not isinstance(point, str) or "x" not in point.lower():
            continue
        left, right = point.lower().split("x", 1)
        try:
            xs.append(float(left))
            ys.append(float(right))
        except ValueError:
            continue
    if not xs or not ys:
        return None
    return max(xs) - min(xs), max(ys) - min(ys)
