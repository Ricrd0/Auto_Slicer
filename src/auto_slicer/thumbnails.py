from __future__ import annotations

import base64
import io
import math
import re
from pathlib import Path

try:
    from PIL import Image, ImageDraw
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

import numpy as np

from auto_slicer.meshio import Triangle


def render_thumbnail(
    triangles: list[Triangle],
    size: tuple[int, int],
    bg_color: tuple[int, int, int] = (32, 32, 32),
    mesh_color: tuple[int, int, int] = (0, 174, 66),  # Orca green #00AE42
    decimate_threshold: int = 100000,
) -> bytes:
    """Render an isometric 3/4 view of a mesh with Lambert shading.
    
    Creates a thumbnail similar to OrcaSlicer's plate preview:
    - Angled isometric view (not top-down)
    - Per-face Lambert shading based on normals
    - Orca green filament color
    - Dark background
    - Model centered and scaled to fill frame
    
    Args:
        triangles: List of triangles to render
        size: Output image size (width, height)
        bg_color: Background RGB color
        mesh_color: Base mesh RGB color (before lighting)
        decimate_threshold: Decimate if more than this many triangles
    
    Returns PNG bytes suitable for embedding in gcode.
    """
    if not PIL_AVAILABLE:
        raise ImportError("PIL/Pillow is required for thumbnail generation")
    
    if not triangles:
        raise ValueError("No triangles to render")
    
    width, height = size
    
    # Decimate if mesh is too large (simple stride-based decimation)
    if len(triangles) > decimate_threshold:
        stride = len(triangles) // decimate_threshold + 1
        triangles = triangles[::stride]
    
    # Calculate mesh bounds
    xs, ys, zs = [], [], []
    for tri in triangles:
        for v in tri:
            xs.append(v[0])
            ys.append(v[1])
            zs.append(v[2])
    
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)
    
    # Center the model
    center_x = (min_x + max_x) / 2.0
    center_y = (min_y + max_y) / 2.0
    center_z = (min_z + max_z) / 2.0
    
    # Model size
    size_x = max_x - min_x if max_x > min_x else 1.0
    size_y = max_y - min_y if max_y > min_y else 1.0
    size_z = max_z - min_z if max_z > min_z else 1.0
    max_size = max(size_x, size_y, size_z)
    
    # Isometric view: rotate 45° around Z, then ~35° around X
    # This gives a nice 3/4 view like OrcaSlicer
    rot_z = math.radians(45)  # Rotate around Z (yaw)
    rot_x = math.radians(35)  # Rotate around X (pitch)
    
    cos_z, sin_z = math.cos(rot_z), math.sin(rot_z)
    cos_x, sin_x = math.cos(rot_x), math.sin(rot_x)
    
    def rotate_and_project(x: float, y: float, z: float) -> tuple[float, float, float]:
        """Apply isometric rotation and return rotated 3D point."""
        # Center
        x, y, z = x - center_x, y - center_y, z - center_z
        
        # Rotate around Z axis (yaw)
        x2 = x * cos_z - y * sin_z
        y2 = x * sin_z + y * cos_z
        z2 = z
        
        # Rotate around X axis (pitch)
        y3 = y2 * cos_x - z2 * sin_x
        z3 = y2 * sin_x + z2 * cos_x
        x3 = x2
        
        return x3, y3, z3
    
    def project_to_screen(x3: float, y3: float, z3: float) -> tuple[float, float]:
        """Orthographic projection to screen coordinates."""
        # Scale to fit in image with padding
        padding = 0.08
        scale = min(width, height) * (1.0 - 2 * padding) / max_size
        
        # Project (orthographic, ignore Z for projection)
        px = x3 * scale + width / 2
        py = height / 2 - y3 * scale
        
        return px, py
    
    def calculate_normal(tri: Triangle) -> tuple[float, float, float]:
        """Calculate face normal vector."""
        v0, v1, v2 = tri
        
        # Edge vectors
        e1 = (v1[0] - v0[0], v1[1] - v0[1], v1[2] - v0[2])
        e2 = (v2[0] - v0[0], v2[1] - v0[1], v2[2] - v0[2])
        
        # Cross product
        nx = e1[1] * e2[2] - e1[2] * e2[1]
        ny = e1[2] * e2[0] - e1[0] * e2[2]
        nz = e1[0] * e2[1] - e1[1] * e2[0]
        
        # Normalize
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length > 0:
            nx, ny, nz = nx / length, ny / length, nz / length
        
        return nx, ny, nz
    
    # Light direction (from upper-front-right, typical for 3D rendering)
    light_dir = (0.3, -0.5, 0.8)
    light_length = math.sqrt(sum(x * x for x in light_dir))
    light_dir = tuple(x / light_length for x in light_dir)
    
    # Ambient light
    ambient = 0.35
    
    # Transform triangles and calculate lighting
    transformed = []
    for tri in triangles:
        # Calculate normal in original space
        normal = calculate_normal(tri)
        
        # Rotate normal
        nx, ny, nz = normal
        # Rotate around Z
        nx2 = nx * cos_z - ny * sin_z
        ny2 = nx * sin_z + ny * cos_z
        nz2 = nz
        # Rotate around X
        ny3 = ny2 * cos_x - nz2 * sin_x
        nz3 = ny2 * sin_x + nz2 * cos_x
        nx3 = nx2
        
        rotated_normal = (nx3, ny3, nz3)
        
        # Backface culling: skip if facing away (normal Z < 0 means back face)
        if nz3 < 0:
            continue
        
        # Lambert shading: dot product of normal and light direction
        dot = sum(n * l for n, l in zip(rotated_normal, light_dir))
        diffuse = max(0.0, dot)
        
        # Total lighting
        intensity = ambient + (1.0 - ambient) * diffuse
        intensity = max(0.0, min(1.0, intensity))
        
        # Transform vertices
        v0_rot = rotate_and_project(*tri[0])
        v1_rot = rotate_and_project(*tri[1])
        v2_rot = rotate_and_project(*tri[2])
        
        # Average Z for depth sorting
        z_avg = (v0_rot[2] + v1_rot[2] + v2_rot[2]) / 3.0
        
        p0 = project_to_screen(*v0_rot)
        p1 = project_to_screen(*v1_rot)
        p2 = project_to_screen(*v2_rot)
        
        # Calculate lit color
        color = tuple(int(c * intensity) for c in mesh_color)
        
        transformed.append((z_avg, [p0, p1, p2], color))
    
    # Sort by depth (painter's algorithm: back to front)
    transformed.sort(key=lambda item: item[0])
    
    # Create image
    image = Image.new('RGB', (width, height), bg_color)
    draw = ImageDraw.Draw(image)
    
    # Draw triangles back to front
    for _, points, color in transformed:
        draw.polygon(points, fill=color)
    
    # Convert to PNG bytes
    buffer = io.BytesIO()
    image.save(buffer, format='PNG', optimize=True)
    return buffer.getvalue()


def encode_thumbnail_block(
    png_data: bytes,
    width: int,
    height: int,
) -> str:
    """Encode a PNG thumbnail as a gcode comment block.
    
    Returns a string suitable for insertion into gcode, including
    the `; thumbnail begin` and `; thumbnail end` markers.
    """
    encoded = base64.b64encode(png_data).decode('ascii')
    
    # Split into 78-character lines (standard for Orca/Prusa format)
    line_length = 78
    lines = [encoded[i:i+line_length] for i in range(0, len(encoded), line_length)]
    
    result = [f"; thumbnail begin {width}x{height} {len(encoded)}"]
    result.extend(f"; {line}" for line in lines)
    result.append("; thumbnail end")
    result.append("")
    
    return "\n".join(result)


def parse_thumbnail_sizes(thumbnails_setting: str | None) -> list[tuple[int, int]]:
    """Parse thumbnail sizes from printer profile setting.
    
    Examples:
        "48x48/PNG,300x300/PNG" -> [(48, 48), (300, 300)]
        "48x48, 300x300" -> [(48, 48), (300, 300)]
        ["48x48/PNG", "300x300/PNG"] -> [(48, 48), (300, 300)]
    """
    if not thumbnails_setting:
        return []
    
    if isinstance(thumbnails_setting, list):
        thumbnails_setting = ",".join(thumbnails_setting)
    
    sizes = []
    for spec in thumbnails_setting.split(','):
        spec = spec.strip()
        # Match patterns like "48x48" or "48x48/PNG"
        match = re.match(r'(\d+)x(\d+)', spec)
        if match:
            width = int(match.group(1))
            height = int(match.group(2))
            sizes.append((width, height))
    
    return sizes


def inject_thumbnails_into_gcode(
    gcode_path: Path,
    triangles: list[Triangle],
    thumbnail_sizes: list[tuple[int, int]],
) -> None:
    """Render thumbnails and inject them into a gcode file in-place.
    
    Inserts thumbnail blocks after any HEADER_BLOCK_END marker,
    or at the beginning if no such marker exists.
    """
    if not PIL_AVAILABLE:
        return
    
    if not thumbnail_sizes or not triangles:
        return
    
    # Read existing gcode
    gcode_text = gcode_path.read_text(encoding='utf-8', errors='replace')
    
    # Generate thumbnail blocks
    thumbnail_blocks = []
    for width, height in thumbnail_sizes:
        try:
            png_data = render_thumbnail(triangles, (width, height))
            block = encode_thumbnail_block(png_data, width, height)
            thumbnail_blocks.append(block)
        except Exception:
            # Skip this thumbnail on error, don't fail the whole slice
            continue
    
    if not thumbnail_blocks:
        return
    
    # Find injection point (after HEADER_BLOCK_END or at start)
    injection_point = 0
    lines = gcode_text.split('\n')
    
    for i, line in enumerate(lines):
        if 'HEADER_BLOCK_END' in line:
            injection_point = i + 1
            break
    
    # Insert thumbnails
    combined_thumbnails = "\n".join(thumbnail_blocks)
    lines.insert(injection_point, combined_thumbnails)
    
    # Write back
    gcode_path.write_text("\n".join(lines), encoding='utf-8')


def extract_thumbnail_from_gcode(
    gcode_path: Path,
    size: tuple[int, int] | None = None,
) -> bytes | None:
    """Extract a thumbnail PNG from gcode.
    
    If size is specified, extracts that specific size. Otherwise returns
    the first thumbnail found.
    
    Returns PNG bytes or None if not found.
    """
    text = gcode_path.read_text(encoding='utf-8', errors='replace')
    
    size_pattern = f"{size[0]}x{size[1]}" if size else r'\d+x\d+'
    begin_pattern = re.compile(rf'; thumbnail begin {size_pattern} (\d+)')
    
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        match = begin_pattern.match(lines[i])
        if match:
            # Found a thumbnail block
            encoded_length = int(match.group(1))
            i += 1
            
            # Collect base64 lines
            encoded_parts = []
            while i < len(lines) and not lines[i].startswith('; thumbnail end'):
                line = lines[i].strip()
                if line.startswith('; '):
                    encoded_parts.append(line[2:])
                i += 1
            
            # Decode
            try:
                encoded = ''.join(encoded_parts)
                return base64.b64decode(encoded)
            except Exception:
                pass
        
        i += 1
    
    return None
