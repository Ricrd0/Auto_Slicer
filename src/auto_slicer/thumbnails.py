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
    
    # Isometric view: rotate 45° around Z, then ~35° around X
    # This gives a nice 3/4 view like OrcaSlicer
    rot_z = math.radians(45)  # Rotate around Z (yaw)
    rot_x = math.radians(35)  # Rotate around X (pitch)
    
    cos_z, sin_z = math.cos(rot_z), math.sin(rot_z)
    cos_x, sin_x = math.cos(rot_x), math.sin(rot_x)
    
    def rotate_point(x: float, y: float, z: float) -> tuple[float, float, float]:
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
    
    # First pass: rotate all vertices to find projected 2D bounding box
    projected_xs = []
    projected_ys = []
    
    for tri in triangles:
        for v in tri:
            x3, y3, z3 = rotate_point(*v)
            projected_xs.append(x3)
            projected_ys.append(y3)
    
    # Calculate 2D bounding box of projected points
    proj_min_x, proj_max_x = min(projected_xs), max(projected_xs)
    proj_min_y, proj_max_y = min(projected_ys), max(projected_ys)
    
    proj_width = proj_max_x - proj_min_x if proj_max_x > proj_min_x else 1.0
    proj_height = proj_max_y - proj_min_y if proj_max_y > proj_min_y else 1.0
    
    # Calculate scale to fit projected bbox with margin (8%)
    margin = 0.08
    scale_x = width * (1.0 - 2 * margin) / proj_width
    scale_y = height * (1.0 - 2 * margin) / proj_height
    scale = min(scale_x, scale_y)  # Preserve aspect ratio
    
    # Center offset to position model in frame
    proj_center_x = (proj_min_x + proj_max_x) / 2.0
    proj_center_y = (proj_min_y + proj_max_y) / 2.0
    
    def project_to_screen(x3: float, y3: float, z3: float) -> tuple[float, float, float]:
        """Orthographic projection to screen coordinates, returns (px, py, z)."""
        # Scale and center
        px = (x3 - proj_center_x) * scale + width / 2
        py = height / 2 - (y3 - proj_center_y) * scale
        
        return px, py, z3
    
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
    
    def _rasterize_triangle(
        p0: tuple[float, float, float],
        p1: tuple[float, float, float],
        p2: tuple[float, float, float],
        color: tuple[int, int, int],
        color_buffer: np.ndarray,
        z_buffer: np.ndarray,
        img_width: int,
        img_height: int,
    ) -> None:
        """Rasterize a triangle with z-buffer testing."""
        x0, y0, z0 = p0
        x1, y1, z1 = p1
        x2, y2, z2 = p2
        
        # Bounding box
        min_x = max(0, int(min(x0, x1, x2)))
        max_x = min(img_width - 1, int(max(x0, x1, x2)) + 1)
        min_y = max(0, int(min(y0, y1, y2)))
        max_y = min(img_height - 1, int(max(y0, y1, y2)) + 1)
        
        # Edge function helper
        def edge_function(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
            return (cx - ax) * (by - ay) - (cy - ay) * (bx - ax)
        
        # Calculate triangle area (for barycentric coordinates)
        area = edge_function(x0, y0, x1, y1, x2, y2)
        if abs(area) < 0.0001:  # Degenerate triangle
            return
        
        # Scan bounding box
        for y in range(min_y, max_y + 1):
            for x in range(min_x, max_x + 1):
                px, py = x + 0.5, y + 0.5  # Pixel center
                
                # Barycentric coordinates
                w0 = edge_function(x1, y1, x2, y2, px, py)
                w1 = edge_function(x2, y2, x0, y0, px, py)
                w2 = edge_function(x0, y0, x1, y1, px, py)
                
                # Check if point is inside triangle
                if w0 >= 0 and w1 >= 0 and w2 >= 0:
                    # Normalize barycentric coordinates
                    w0 /= area
                    w1 /= area
                    w2 /= area
                    
                    # Interpolate Z
                    z = w0 * z0 + w1 * z1 + w2 * z2
                    
                    # Z-buffer test
                    if z < z_buffer[y, x]:
                        z_buffer[y, x] = z
                        color_buffer[y, x] = color
    
    # Light direction (from upper-front-right, typical for 3D rendering)
    light_dir = (0.3, -0.5, 0.8)
    light_length = math.sqrt(sum(x * x for x in light_dir))
    light_dir = tuple(x / light_length for x in light_dir)
    
    # Ambient light
    ambient = 0.35
    
    # Create color buffer and z-buffer
    color_buffer = np.full((height, width, 3), bg_color[:3], dtype=np.uint8)
    z_buffer = np.full((height, width), float('inf'), dtype=np.float32)
    
    # Transform triangles and rasterize with z-buffer
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
        v0_rot = rotate_point(*tri[0])
        v1_rot = rotate_point(*tri[1])
        v2_rot = rotate_point(*tri[2])
        
        p0 = project_to_screen(*v0_rot)
        p1 = project_to_screen(*v1_rot)
        p2 = project_to_screen(*v2_rot)
        
        # Calculate lit color
        color = tuple(int(c * intensity) for c in mesh_color)
        
        # Rasterize triangle with z-buffer
        _rasterize_triangle(
            p0, p1, p2,
            color,
            color_buffer,
            z_buffer,
            width, height
        )
    
    # Convert numpy array to PIL Image
    image = Image.fromarray(color_buffer, 'RGB')
    
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
