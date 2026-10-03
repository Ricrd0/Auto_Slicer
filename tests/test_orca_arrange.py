from __future__ import annotations

import io
import zipfile
from pathlib import Path

from auto_slicer.orca_arrange import arranged_3mf_path, layout_from_arranged_3mf


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
