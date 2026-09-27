from __future__ import annotations

import io
import zipfile

from auto_slicer.meshio import bounds, load_model, read_3mf, read_stl, rotate_xyz, transform_mesh, write_stl


def _triangle() -> list:
    return [((0.0, 0.0, 0.0), (20.0, 0.0, 0.0), (0.0, 10.0, 5.0))]


def test_stl_roundtrip() -> None:
    payload = write_stl(_triangle())
    loaded = read_stl(payload)
    assert len(loaded) == 1
    assert loaded[0][1][0] == 20


def test_ascii_stl() -> None:
    text = """solid test
      facet normal 0 0 1
        outer loop
          vertex 0 0 0
          vertex 1 0 0
          vertex 0 1 2
        endloop
      endfacet
    endsolid test
    """
    loaded = read_stl(text.encode("utf-8"))
    low, high = bounds(loaded)
    assert high[2] == 2


def test_rotation_z_is_applied_before_later_axes() -> None:
    assert rotate_xyz((1.0, 0.0, 0.0), 0, 0, 90)[1] == pytest_approx(1)
    assert abs(rotate_xyz((1.0, 0.0, 0.0), 0, 0, 90)[0]) < 1e-9
    turned = rotate_xyz((0.0, 1.0, 0.0), 90, 0, 0)
    assert abs(turned[1]) < 1e-9
    assert turned[2] == pytest_approx(1)


def test_transform_drops_to_bed_and_uses_center_origin() -> None:
    posed = transform_mesh(_triangle(), (0, 0, 0), 90, 85, 200, 180)
    low, high = bounds(posed)
    assert low[2] == pytest_approx(0)
    assert low[0] == pytest_approx(90 - 100)
    assert low[1] == pytest_approx(85 - 90)
    assert high[2] == pytest_approx(5)


def test_3mf_applies_build_transform_in_millimetres() -> None:
    payload = _threemf("millimeter", "1 0 0 0 1 0 0 0 1 10 20 30", 0, 0, 0)
    triangles = read_3mf(payload)
    xs = sorted(vertex[0] for tri in triangles for vertex in tri)
    ys = sorted(vertex[1] for tri in triangles for vertex in tri)
    zs = sorted(vertex[2] for tri in triangles for vertex in tri)
    assert xs[0] == pytest_approx(10)
    assert ys[0] == pytest_approx(20)
    assert zs[0] == pytest_approx(30)


def test_3mf_micron_unit_scales_to_millimetres() -> None:
    payload = _threemf("micron", None, 1000, 0, 0)
    triangles = read_3mf(payload)
    assert min(vertex[0] for tri in triangles for vertex in tri) == pytest_approx(1)


def test_load_model_uses_the_suffix() -> None:
    assert len(load_model("part.stl", write_stl(_triangle()))) == 1


def _threemf(unit: str, transform: str | None, x: float, y: float, z: float) -> bytes:
    transform_attr = f' transform="{transform}"' if transform else ""
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">
  <resources>
    <object id="1" type="model">
      <mesh>
        <vertices>
          <vertex x="{x}" y="{y}" z="{z}"/>
          <vertex x="{x + 1}" y="{y}" z="{z}"/>
          <vertex x="{x}" y="{y + 1}" z="{z}"/>
        </vertices>
        <triangles><triangle v1="0" v2="1" v3="2"/></triangles>
      </mesh>
    </object>
  </resources>
  <build><item objectid="1"{transform_attr}/></build>
</model>
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("3D/3dmodel.model", xml)
    return buffer.getvalue()


def pytest_approx(value: float):
    from pytest import approx

    return approx(value, abs=1e-6)
