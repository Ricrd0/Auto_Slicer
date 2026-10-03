from __future__ import annotations

import io
import zipfile
from pathlib import Path

from auto_slicer.cura_config import PrinterProfile
from auto_slicer.meshio import write_stl
from auto_slicer.orca_arrange import arranged_3mf_path
from auto_slicer.settings_schema import SliceSettings
from auto_slicer.slicing import ensure_group_layout, layout_for_group
from auto_slicer.store import _normalize_group


def _box_stl(path: Path, width: float, depth: float, height: float = 5.0) -> None:
    triangles = [
        ((0.0, 0.0, 0.0), (width, 0.0, 0.0), (width, depth, 0.0)),
        ((0.0, 0.0, 0.0), (width, depth, 0.0), (0.0, depth, 0.0)),
        ((0.0, 0.0, height), (width, depth, height), (width, 0.0, height)),
        ((0.0, 0.0, height), (0.0, depth, height), (width, depth, height)),
    ]
    path.write_bytes(write_stl(triangles))


def _printer(width: float = 300, depth: float = 300) -> PrinterProfile:
    return PrinterProfile(
        id="p",
        name="p",
        definition_id="p",
        definition_path=None,
        extruder_definition_id=None,
        extruder_definition_path=None,
        global_settings=[],
        extruder_settings=[],
        machine_width=width,
        machine_depth=depth,
        machine_height=400,
        machine_center_is_zero=False,
        extruder_count=1,
        setting_version=None,
        resource_setting_version=None,
        engine="orca",
    )


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


def test_manual_rotation_z_changes_footprint_and_clears_false_overlap(tmp_path: Path) -> None:
    _box_stl(tmp_path / "a.stl", 80, 20)
    _box_stl(tmp_path / "b.stl", 80, 20)
    files = ["a.stl", "b.stl"]
    manual = {
        "a.stl": {"x": 180.0, "y": 20.0, "rotation_z": -90.0},
        "b.stl": {"x": 150.0, "y": 20.0, "rotation_z": -90.0},
    }
    layout = layout_for_group(tmp_path, files, _printer(), SliceSettings(), {}, manual)
    assert layout.error is None
    by_file = {item.file: item for item in layout.items}
    assert abs((by_file["a.stl"].max_x - by_file["a.stl"].min_x) - 20.0) < 1e-6
    assert abs((by_file["a.stl"].max_y - by_file["a.stl"].min_y) - 80.0) < 1e-6
    assert by_file["a.stl"].rotation[2] == -90.0


def test_normalize_group_keeps_rotation_z() -> None:
    group = _normalize_group(
        {
            "name": "pipes",
            "files": ["a.stl"],
            "manual_layout": {"a.stl": {"x": 1.0, "y": 2.0, "rotation_z": -90.0}},
            "arranged_3mf": "a.3mf",
        }
    )
    assert group["manual_layout"]["a.stl"]["rotation_z"] == -90.0


def test_ensure_group_layout_refreshes_missing_rotation_from_3mf(tmp_path: Path) -> None:
    folder = tmp_path / "Tankers"
    folder.mkdir()
    _box_stl(folder / "a.stl", 80, 20)
    _box_stl(folder / "b.stl", 80, 20)
    files = ["Tankers/a.stl", "Tankers/b.stl"]
    archive = arranged_3mf_path(tmp_path, files, "small pipes")
    archive.write_bytes(
        _parts_3mf(
            [
                ("a.stl", "0 1 0 -1 0 0 0 0 1 180 100 0"),
                ("b.stl", "0 1 0 -1 0 0 0 0 1 150 100 0"),
            ]
        )
    )
    group = {
        "id": "g1",
        "name": "small pipes",
        "files": files,
        "manual_layout": {
            "Tankers/a.stl": {"x": 180.0, "y": 20.0},
            "Tankers/b.stl": {"x": 150.0, "y": 20.0},
        },
        "arranged_3mf": archive.relative_to(tmp_path).as_posix(),
    }
    layout, dirty = ensure_group_layout(tmp_path, group, _printer(), SliceSettings(), {})
    assert dirty
    assert group["manual_layout"]["Tankers/a.stl"]["rotation_z"] == -90.0
    assert layout.error is None
