from __future__ import annotations

import base64
import io
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
    mesh_color: tuple[int, int, int] = (200, 200, 200),
) -> bytes:
    """Render a simple orthographic projection of a mesh as PNG.
    
    Returns PNG bytes suitable for embedding in gcode.
    """
    if not PIL_AVAILABLE:
        raise ImportError("PIL/Pillow is required for thumbnail generation")
    
    if not triangles:
        raise ValueError("No triangles to render")
    
    width, height = size
    
    # Extract all vertices (Triangle is tuple[Vertex, Vertex, Vertex])
    vertices = []
    for tri in triangles:
        vertices.extend([tri[0], tri[1], tri[2]])
    
    # Calculate bounds
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    min_z, max_z = min(zs), max(zs)
    
    # Use X-Y plane (top view) for projection
    range_x = max_x - min_x if max_x > min_x else 1.0
    range_y = max_y - min_y if max_y > min_y else 1.0
    
    # Scale to fit with padding
    padding = 0.1
    scale = min(width, height) * (1.0 - 2 * padding) / max(range_x, range_y)
    
    # Create image
    image = Image.new('RGB', (width, height), bg_color)
    draw = ImageDraw.Draw(image)
    
    def project(x: float, y: float) -> tuple[float, float]:
        """Project 3D point to 2D image coordinates."""
        px = (x - min_x) * scale + width * padding
        py = height - ((y - min_y) * scale + height * padding)
        return px, py
    
    # Draw triangles with simple z-ordering (painter's algorithm)
    tri_data = []
    for tri in triangles:
        center_z = (tri[0][2] + tri[1][2] + tri[2][2]) / 3.0
        tri_data.append((center_z, tri))
    
    tri_data.sort(key=lambda item: item[0])
    
    # Draw each triangle
    for _, tri in tri_data:
        p1 = project(tri[0][0], tri[0][1])
        p2 = project(tri[1][0], tri[1][1])
        p3 = project(tri[2][0], tri[2][1])
        
        # Calculate shading based on Z height
        z_avg = (tri[0][2] + tri[1][2] + tri[2][2]) / 3.0
        z_range = max_z - min_z if max_z > min_z else 1.0
        z_factor = (z_avg - min_z) / z_range
        
        # Lighter color for higher Z
        shade_factor = 0.5 + 0.5 * z_factor
        color = tuple(int(c * shade_factor) for c in mesh_color)
        
        draw.polygon([p1, p2, p3], fill=color, outline=None)
    
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
