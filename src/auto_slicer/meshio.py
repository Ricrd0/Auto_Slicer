from __future__ import annotations

import io
import math
import struct
import zipfile
from xml.etree import ElementTree

Vertex = tuple[float, float, float]
Triangle = tuple[Vertex, Vertex, Vertex]

_UNIT_TO_MM = {
    "micron": 0.001,
    "millimeter": 1.0,
    "centimeter": 10.0,
    "meter": 1000.0,
    "inch": 25.4,
    "foot": 304.8,
}


def read_stl(data: bytes) -> list[Triangle]:
    if _looks_ascii_stl(data):
        return _read_ascii_stl(data)
    return _read_binary_stl(data)


def write_stl(triangles: list[Triangle]) -> bytes:
    header = b"auto-slicer" + b"\0" * (80 - len(b"auto-slicer"))
    parts = [header, struct.pack("<I", len(triangles))]
    for tri in triangles:
        parts.append(struct.pack("<fff", 0.0, 0.0, 0.0))
        for vertex in tri:
            parts.append(struct.pack("<fff", float(vertex[0]), float(vertex[1]), float(vertex[2])))
        parts.append(struct.pack("<H", 0))
    return b"".join(parts)


def read_3mf(data: bytes) -> list[Triangle]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        model_name = _find_model(archive)
        root = ElementTree.fromstring(archive.read(model_name))
    unit = _attr(root, "unit") or "millimeter"
    scale = _UNIT_TO_MM.get(unit)
    if scale is None:
        raise ValueError(f"unsupported 3MF unit: {unit}")
    objects = _mesh_objects(root)
    triangles: list[Triangle] = []
    build = _child(root, "build")
    items = _children(build, "item") if build is not None else []
    if not items:
        for mesh in objects.values():
            triangles.extend(_scale_mesh(mesh, scale))
        if not triangles:
            raise ValueError("3MF file does not contain a mesh")
        return triangles
    for item in items:
        object_id = _attr(item, "objectid")
        if object_id is None:
            continue
        transform = _parse_transform(_attr(item, "transform"))
        triangles.extend(_object_triangles(object_id, objects, root, scale, transform, set()))
    if not triangles:
        raise ValueError("3MF file does not contain a mesh")
    return triangles


def bounds(triangles: list[Triangle]) -> tuple[Vertex, Vertex]:
    if not triangles:
        raise ValueError("mesh is empty")
    min_v = [math.inf, math.inf, math.inf]
    max_v = [-math.inf, -math.inf, -math.inf]
    for tri in triangles:
        for vertex in tri:
            for axis in range(3):
                min_v[axis] = min(min_v[axis], vertex[axis])
                max_v[axis] = max(max_v[axis], vertex[axis])
    return (min_v[0], min_v[1], min_v[2]), (max_v[0], max_v[1], max_v[2])


def rotate_xyz(vertex: Vertex, rx_deg: float, ry_deg: float, rz_deg: float) -> Vertex:
    """Rotate X, then Y, then Z, in degrees."""
    x, y, z = vertex
    ax, ay, az = (math.radians(rx_deg), math.radians(ry_deg), math.radians(rz_deg))
    cx, sx = math.cos(ax), math.sin(ax)
    cy, sy = math.cos(ay), math.sin(ay)
    cz, sz = math.cos(az), math.sin(az)
    y1 = cx * y - sx * z
    z1 = sx * y + cx * z
    x1 = x
    x2 = cy * x1 + sy * z1
    z2 = -sy * x1 + cy * z1
    y2 = y1
    x3 = cz * x2 - sz * y2
    y3 = sz * x2 + cz * y2
    z3 = z2
    return (x3, y3, z3)


def transform_mesh(
    triangles: list[Triangle],
    rotation: tuple[float, float, float],
    min_x: float,
    min_y: float,
    machine_width: float,
    machine_depth: float,
) -> list[Triangle]:
    """Rotate, drop onto Z=0, and place the footprint in bed-center coordinates.

    Stored positions are millimetres from the front-left of the bed. CuraEngine
    expects mesh coordinates whose origin is the bed center, then adds half the
    bed size when ``machine_center_is_zero`` is false. Writing center-origin
    coordinates and leaving that printer setting alone lands the part on the
    same front-left position for both kinds of origin.
    """
    rotated = [
        tuple(rotate_xyz(vertex, rotation[0], rotation[1], rotation[2]) for vertex in tri)
        for tri in triangles
    ]
    (low_x, low_y, low_z), _high = bounds(rotated)
    dx = (min_x - machine_width / 2.0) - low_x
    dy = (min_y - machine_depth / 2.0) - low_y
    dz = -low_z
    return [
        tuple((vertex[0] + dx, vertex[1] + dy, vertex[2] + dz) for vertex in tri)
        for tri in rotated
    ]


def load_model(path_name: str, data: bytes) -> list[Triangle]:
    suffix = path_name.lower().rsplit(".", 1)[-1]
    if suffix == "stl":
        return read_stl(data)
    if suffix == "3mf":
        return read_3mf(data)
    raise ValueError(f"unsupported model type: {path_name}")


def _looks_ascii_stl(data: bytes) -> bool:
    stripped = data.lstrip()
    if not stripped.lower().startswith(b"solid"):
        return False
    count_offset = 80
    if len(data) < count_offset + 4:
        return True
    count = struct.unpack_from("<I", data, count_offset)[0]
    return len(data) != 84 + count * 50


def _read_binary_stl(data: bytes) -> list[Triangle]:
    if len(data) < 84:
        raise ValueError("STL is too small")
    count = struct.unpack_from("<I", data, 80)[0]
    expected = 84 + count * 50
    if len(data) < expected:
        raise ValueError("STL triangle count does not match the file size")
    triangles: list[Triangle] = []
    offset = 84
    for _ in range(count):
        numbers = struct.unpack_from("<12fH", data, offset)
        vertices = (
            (numbers[3], numbers[4], numbers[5]),
            (numbers[6], numbers[7], numbers[8]),
            (numbers[9], numbers[10], numbers[11]),
        )
        triangles.append(vertices)
        offset += 50
    return triangles


def _read_ascii_stl(data: bytes) -> list[Triangle]:
    text = data.decode("utf-8", errors="replace")
    triangles: list[Triangle] = []
    current: list[Vertex] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].lower() == "vertex":
            current.append((float(parts[1]), float(parts[2]), float(parts[3])))
            if len(current) == 3:
                triangles.append((current[0], current[1], current[2]))
                current = []
    if not triangles:
        raise ValueError("ASCII STL does not contain a triangle")
    return triangles


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attr(element: ElementTree.Element, name: str) -> str | None:
    for key, value in element.attrib.items():
        if _local(key) == name:
            return value
    return None


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in list(element) if _local(child.tag) == name]


def _child(element: ElementTree.Element, name: str) -> ElementTree.Element | None:
    found = _children(element, name)
    return found[0] if found else None


def _find_model(archive: zipfile.ZipFile) -> str:
    names = [name for name in archive.namelist() if name.lower().endswith(".model")]
    preferred = [name for name in names if name.lower().endswith("3d/3dmodel.model")]
    if preferred:
        return preferred[0]
    if names:
        return names[0]
    raise ValueError("3MF archive has no model part")


def _parse_transform(value: str | None) -> tuple[float, ...]:
    if not value:
        return (1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0)
    parts = [float(part) for part in value.split()]
    if len(parts) != 12:
        raise ValueError("3MF transform must contain 12 numbers")
    return tuple(parts)


def _apply_transform(vertex: Vertex, transform: tuple[float, ...], scale: float) -> Vertex:
    x, y, z = vertex
    m00, m01, m02, m10, m11, m12, m20, m21, m22, m30, m31, m32 = transform
    return (
        (m00 * x + m01 * y + m02 * z + m30) * scale,
        (m10 * x + m11 * y + m12 * z + m31) * scale,
        (m20 * x + m21 * y + m22 * z + m32) * scale,
    )


def _scale_mesh(triangles: list[Triangle], scale: float) -> list[Triangle]:
    if scale == 1:
        return triangles
    return [
        tuple((vertex[0] * scale, vertex[1] * scale, vertex[2] * scale) for vertex in tri)
        for tri in triangles
    ]


def _mesh_objects(root: ElementTree.Element) -> dict[str, list[Triangle]]:
    objects: dict[str, list[Triangle]] = {}
    resources = _child(root, "resources")
    if resources is None:
        return objects
    for obj in _children(resources, "object"):
        object_id = _attr(obj, "id")
        mesh = _child(obj, "mesh")
        if object_id is None or mesh is None:
            continue
        vertices_el = _child(mesh, "vertices")
        triangles_el = _child(mesh, "triangles")
        if vertices_el is None or triangles_el is None:
            continue
        vertices = [
            (float(_attr(vertex, "x") or 0), float(_attr(vertex, "y") or 0), float(_attr(vertex, "z") or 0))
            for vertex in _children(vertices_el, "vertex")
        ]
        triangles: list[Triangle] = []
        for tri in _children(triangles_el, "triangle"):
            indexes = [int(_attr(tri, key) or 0) for key in ("v1", "v2", "v3")]
            triangles.append((vertices[indexes[0]], vertices[indexes[1]], vertices[indexes[2]]))
        objects[object_id] = triangles
    return objects


def _object_triangles(
    object_id: str,
    objects: dict[str, list[Triangle]],
    root: ElementTree.Element,
    scale: float,
    transform: tuple[float, ...],
    stack: set[str],
) -> list[Triangle]:
    if object_id in stack:
        raise ValueError("3MF component cycle")
    if object_id in objects:
        return [
            tuple(_apply_transform(vertex, transform, scale) for vertex in tri) for tri in objects[object_id]
        ]
    resources = _child(root, "resources")
    if resources is None:
        return []
    for obj in _children(resources, "object"):
        if _attr(obj, "id") != object_id:
            continue
        components = _child(obj, "components")
        if components is None:
            return []
        collected: list[Triangle] = []
        stack.add(object_id)
        try:
            for component in _children(components, "component"):
                child_id = _attr(component, "objectid")
                if child_id is None:
                    continue
                child_transform = _multiply(
                    transform, _parse_transform(_attr(component, "transform"))
                )
                collected.extend(
                    _object_triangles(child_id, objects, root, scale, child_transform, stack)
                )
        finally:
            stack.remove(object_id)
        return collected
    return []


def _multiply(parent: tuple[float, ...], child: tuple[float, ...]) -> tuple[float, ...]:
    """Combine two 3MF 3x4 transforms. Child is applied first."""
    a00, a01, a02, a10, a11, a12, a20, a21, a22, a30, a31, a32 = child
    b00, b01, b02, b10, b11, b12, b20, b21, b22, b30, b31, b32 = parent
    return (
        b00 * a00 + b01 * a10 + b02 * a20,
        b00 * a01 + b01 * a11 + b02 * a21,
        b00 * a02 + b01 * a12 + b02 * a22,
        b10 * a00 + b11 * a10 + b12 * a20,
        b10 * a01 + b11 * a11 + b12 * a21,
        b10 * a02 + b11 * a12 + b12 * a22,
        b20 * a00 + b21 * a10 + b22 * a20,
        b20 * a01 + b21 * a11 + b22 * a21,
        b20 * a02 + b21 * a12 + b22 * a22,
        b00 * a30 + b01 * a31 + b02 * a32 + b30,
        b10 * a30 + b11 * a31 + b12 * a32 + b31,
        b20 * a30 + b21 * a31 + b22 * a32 + b32,
    )
