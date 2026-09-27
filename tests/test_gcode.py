from pathlib import Path

from auto_slicer.gcode import gcode_info, layer_polylines, parse_gcode_header


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
