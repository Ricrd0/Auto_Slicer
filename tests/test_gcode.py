import json
from pathlib import Path

import pytest

from auto_slicer.engine import EngineError
from auto_slicer.gcode import (
    gcode_info,
    klipper_max_extrude_cross_section,
    layer_polylines,
    parse_gcode_header,
    sanitize_relative_extrusion_script,
    validate_extrusion_cross_section,
    validate_gcode_file,
)
from auto_slicer.machine_sync import apply_shared_slice_to_machine, normalize_machine, with_purge_line
from auto_slicer.settings_schema import SliceSettings
from auto_slicer.slicing import _header_from_file


SAMPLE = """;TIME:120
;Filament used: 1.5m
;LAYER_COUNT:2
;LAYER:0
;TYPE:WALL-OUTER
G1 X0 Y0 E0
G1 X10 Y0 E1
G0 X10 Y5
G1 X0 Y5 E2
;LAYER:1
;TYPE:SKIN
G1 X0 Y0 E3
G1 X5 Y0 E4
"""

# Classic absolute-style purge (E12 then E24) that trips Klipper under M83.
BUGGY_ABSOLUTE_PURGE = """M83
G92 E0
G1 Z0.3 F3000
G1 X5 Y10 F6000
G1 Y80 E12 F1500
G1 X5.4 F6000
G1 Y10 E24 F1500
G92 E0
"""


def test_orca_header_and_layer_changes(tmp_path: Path) -> None:
    text = """;LAYER_CHANGE
;Z:0.2
G1 X0 Y0 E1
;LAYER_CHANGE
; estimated printing time (normal mode) = 1h 2m 3s
; filament used [mm] = 1500
"""
    seconds, filament = parse_gcode_header(text)
    assert seconds == 3723
    assert filament == 1.5
    path = tmp_path / "part.gcode"
    path.write_text(text, encoding="utf-8")
    assert gcode_info(path).layer_count == 2


def test_header_comments() -> None:
    seconds, filament = parse_gcode_header(SAMPLE)
    assert seconds == 120
    assert filament == 1.5


def test_layer_render_skips_travel_by_default(tmp_path: Path) -> None:
    path = tmp_path / "part.gcode"
    path.write_text(SAMPLE, encoding="utf-8")
    info = gcode_info(path)
    assert info.layer_count == 2
    layer = layer_polylines(path, 0, include_travel=False)
    assert [item.type_name for item in layer] == ["WALL-OUTER", "WALL-OUTER"]
    assert layer[0].points[-1] == [10, 0, 0]
    with_travel = layer_polylines(path, 0, include_travel=True)
    assert any(item.type_name == "TRAVEL" for item in with_travel)
    skin = layer_polylines(path, 1, include_travel=False)
    assert skin[0].type_name == "SKIN"


def test_validator_flags_absolute_purge_under_relative_e() -> None:
    issues = validate_extrusion_cross_section(BUGGY_ABSOLUTE_PURGE)
    assert issues
    assert abs(issues[0].cross_section - 0.825) < 0.01
    assert issues[0].limit == klipper_max_extrude_cross_section(0.4)


def test_validator_accepts_absolute_purge_under_m82() -> None:
    text = BUGGY_ABSOLUTE_PURGE.replace("M83", "M82")
    assert validate_extrusion_cross_section(text) == []


def test_validator_accepts_current_purge_line_under_m83() -> None:
    block = "M83\n" + with_purge_line("G28\n", bed_depth=220)
    assert validate_extrusion_cross_section(block) == []


def test_validator_accepts_normal_print_moves() -> None:
    text = """M83
G1 X0 Y0 F3000
G1 X10 Y0 E0.4 F1200
G1 X10 Y10 E0.4
"""
    assert validate_extrusion_cross_section(text) == []


def test_validate_gcode_file(tmp_path: Path) -> None:
    path = tmp_path / "bad.gcode"
    path.write_text(BUGGY_ABSOLUTE_PURGE, encoding="utf-8")
    assert validate_gcode_file(path)


def test_slice_pipeline_rejects_over_extrusion(tmp_path: Path) -> None:
    path = tmp_path / "bad.gcode"
    path.write_text(BUGGY_ABSOLUTE_PURGE, encoding="utf-8")
    with pytest.raises(EngineError, match="max_extrude_cross_section"):
        _header_from_file(path, machine={"nozzle_diameter": 0.4})
    assert not path.exists()


def test_sanitize_rewrites_absolute_prime_under_m83() -> None:
    fixed = sanitize_relative_extrusion_script(BUGGY_ABSOLUTE_PURGE)
    assert "E24" not in fixed
    assert validate_extrusion_cross_section(fixed) == []


def test_sanitize_sidewinder_style_prime() -> None:
    script = """G90
M83
G92 E0
G1 X10 Y.5 Z0.25 F5000.0
G1 X100 Y.5 Z0.25 F1500.0 E15 ; Draw the first line
G1 X100 Y.2 Z0.25 F5000.0
G1 X10 Y.2 Z0.25 F1500.0 E30 ; Draw the second line
G92 E0
"""
    assert validate_extrusion_cross_section(script)
    fixed = sanitize_relative_extrusion_script(script)
    assert "E30" not in fixed
    assert "E15 ; Draw the second line" in fixed
    assert validate_extrusion_cross_section(fixed) == []


def test_shared_slice_sanitizes_klipper_start_gcode() -> None:
    machine = normalize_machine(
        {
            "gcode_flavor": "Klipper",
            "orca_start_gcode": (
                "M83\nG92 E0\nG1 X10 Y0.5 F5000\n"
                "G1 X100 Y0.5 E15 F1500\nG1 X100 Y0.2 F5000\n"
                "G1 X10 Y0.2 E30 F1500\n"
            ),
        }
    )
    applied = apply_shared_slice_to_machine(machine, SliceSettings(purge_line=False))
    assert "E30" not in str(applied["orca_start_gcode"])
    assert validate_extrusion_cross_section(str(applied["orca_start_gcode"])) == []


def test_bundled_machine_start_and_end_gcode_is_klipper_safe() -> None:
    root = Path(__file__).resolve().parents[1] / "resources" / "orca_profiles"
    scripts: list[tuple[str, str, str]] = []
    for path in sorted(root.rglob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        for key in ("machine_start_gcode", "machine_end_gcode"):
            script = data.get(key)
            if isinstance(script, str) and script.strip():
                scripts.append((path.name, key, script))
    assert scripts, "expected bundled machine start/end g-code"
    assert any(key == "machine_end_gcode" for _, key, _ in scripts)
    for name, key, script in scripts:
        # End scripts often omit M83; force relative so retract+relative moves stay honest.
        relative = True if key == "machine_end_gcode" else None
        issues = validate_extrusion_cross_section(script, relative_e=relative)
        assert issues == [], f"{name} {key}: {issues[0].message if issues else ''}"
