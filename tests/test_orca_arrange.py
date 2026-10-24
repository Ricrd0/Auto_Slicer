from __future__ import annotations

import io
import zipfile
from pathlib import Path

from auto_slicer.engine import EngineError
from auto_slicer.orca_arrange import (
    _resolved_rotation,
    _space_arranged_project,
    arrange_uses_y_alignment,
    arranged_3mf_path,
    layout_from_arranged_3mf,
)
from auto_slicer.settings_schema import SliceSettings


def test_arranged_3mf_lands_beside_group_files(tmp_path: Path) -> None:
    folder = tmp_path / "BL_11TH" / "Grind Box"
    folder.mkdir(parents=True)
    files = [
        "BL_11TH/Grind Box/a.stl",
        "BL_11TH/Grind Box/b.stl",
    ]
    path = arranged_3mf_path(tmp_path, files, "Grind Box Complete")
    assert path == folder / "Grind Box Complete.3mf"


def test_layout_from_arranged_3mf_uses_front_left_bounds() -> None:
    payload = _parts_3mf(
        [
            ("a.stl", "1 0 0 0 1 0 0 0 1 10 20 0"),
            ("b.stl", "1 0 0 0 1 0 0 0 1 40 50 0"),
        ]
    )
    positions = layout_from_arranged_3mf(payload, ["folder/a.stl", "folder/b.stl"])
    assert positions["folder/a.stl"] == {"x": 10.0, "y": 20.0, "rotation_z": 0.0}
    assert positions["folder/b.stl"] == {"x": 40.0, "y": 50.0, "rotation_z": 0.0}


def test_layout_from_arranged_3mf_keeps_plate_rotation() -> None:
    # cos=-0, sin=-1 → about -90° around Z (Orca Align-to-Y style)
    payload = _parts_3mf(
        [
            ("pipe.stl", "0 1 0 -1 0 0 0 0 1 100 50 0"),
        ]
    )
    positions = layout_from_arranged_3mf(payload, ["folder/pipe.stl"])
    assert positions["folder/pipe.stl"]["rotation_z"] == -90.0


def test_layout_from_arranged_3mf_rejects_a_second_plate() -> None:
    payload = _parts_3mf([("a.stl", "1 0 0 0 1 0 0 0 1 0 0 0")])
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        entries = {name: archive.read(name) for name in archive.namelist()}
    settings = entries["Metadata/model_settings.config"].decode("utf-8")
    entries["Metadata/model_settings.config"] = settings.replace(
        "</config>",
        "<plate><model_instance/></plate><plate><model_instance/></plate></config>",
    ).encode("utf-8")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    try:
        layout_from_arranged_3mf(buffer.getvalue(), ["folder/a.stl"])
        raise AssertionError("expected a multi-plate project to be rejected")
    except EngineError as exc:
        assert "one plate" in str(exc)


def test_align_to_y_checkbox_selects_the_group_orientation() -> None:
    assert arrange_uses_y_alignment(SliceSettings()) is False
    assert arrange_uses_y_alignment(SliceSettings(align_longest_to_x=True, orca_arrange_align_y=True)) is True
    assert arrange_uses_y_alignment(SliceSettings(orca_arrange_align_y=False)) is False
    triangles = [
        ((0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (0.0, 40.0, 2.0)),
    ]
    along_x = _resolved_rotation(SliceSettings(), None, triangles, 1.0)
    assert along_x[2] == 90.0
    along_y = _resolved_rotation(SliceSettings(orca_arrange_align_y=True), None, triangles, 1.0)
    assert along_y[2] == 0.0


def test_group_offset_separates_parts_by_the_setting() -> None:
    payload = _parts_3mf(
        [
            ("a.stl", "1 0 0 0 1 0 0 0 1 0 0 0"),
            ("b.stl", "1 0 0 0 1 0 0 0 1 1 0 0"),
        ]
    )
    spaced = _space_arranged_project(payload, SliceSettings(orca_arrange_spacing=5), 100, 100, False)
    positions = layout_from_arranged_3mf(spaced, ["a.stl", "b.stl"])
    gap = positions["b.stl"]["x"] - (positions["a.stl"]["x"] + 1)
    assert abs(gap - 5) < 1e-4


def _parts_3mf(parts: list[tuple[str, str]]) -> bytes:
    object_xml = []
    build_items = []
    settings = ['<?xml version="1.0" encoding="UTF-8"?>', "<config>"]
    for index, (name, transform) in enumerate(parts, start=1):
        object_xml.append(
            f"""
    <object id="{index}" type="model">
      <mesh>
        <vertices>
          <vertex x="0" y="0" z="0"/>
          <vertex x="1" y="0" z="0"/>
          <vertex x="0" y="1" z="0"/>
        </vertices>
        <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
      </mesh>
    </object>"""
        )
        build_items.append(f'<item objectid="{index}" transform="{transform}"/>')
        settings.append(f'<object id="{index}"><metadata key="name" value="{name}"/></object>')
    settings.append("</config>")
    model = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    {"".join(object_xml)}
  </resources>
  <build>
    {"".join(build_items)}
  </build>
</model>
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", model)
        archive.writestr("Metadata/model_settings.config", "\n".join(settings))
    return buffer.getvalue()
