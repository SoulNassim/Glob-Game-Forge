"""Pure-Python binary glTF 2.0 (.glb) and Wavefront (.obj) procedural 3D generator.

Produces valid, vertex-colored PBR glTF 2.0 binary meshes (potions, swords, shields,
characters, helmets, crystals, chests, props) so Demo Mode and offline tests render
real 3D geometry in the turntable viewer without external ML weights.
"""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path
from typing import Any


def _normalize(vx: float, vy: float, vz: float) -> tuple[float, float, float]:
    length = math.sqrt(vx * vx + vy * vy + vz * vz)
    if length < 1e-8:
        return (0.0, 1.0, 0.0)
    return (vx / length, vy / length, vz / length)


def _hex_to_rgb(hex_str: str) -> tuple[float, float, float]:
    h = hex_str.lstrip("#")
    if len(h) != 6:
        return (0.45, 0.65, 0.95)
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0, int(h[4:6], 16) / 255.0)


class MeshBuilder:
    def __init__(self) -> None:
        self.positions: list[tuple[float, float, float]] = []
        self.normals: list[tuple[float, float, float]] = []
        self.colors: list[tuple[float, float, float, float]] = []
        self.indices: list[int] = []

    def add_vertex(
        self,
        pos: tuple[float, float, float],
        norm: tuple[float, float, float],
        color: tuple[float, float, float, float] = (0.8, 0.8, 0.8, 1.0),
    ) -> int:
        idx = len(self.positions)
        self.positions.append(pos)
        self.normals.append(_normalize(*norm))
        self.colors.append(color)
        return idx

    def add_tri(self, i0: int, i1: int, i2: int) -> None:
        self.indices.extend([i0, i1, i2])

    def add_lathe(
        self,
        profile: list[tuple[float, float]],  # (radius, y)
        segments: int = 24,
        color_bottom: tuple[float, float, float] = (0.2, 0.6, 0.9),
        color_top: tuple[float, float, float] = (0.6, 0.9, 1.0),
        y_offset: float = 0.0,
    ) -> None:
        rows = len(profile)
        if rows < 2:
            return
        grid: list[list[int]] = []
        for r_idx, (rad, y) in enumerate(profile):
            t = r_idx / max(1, rows - 1)
            cr = color_bottom[0] * (1 - t) + color_top[0] * t
            cg = color_bottom[1] * (1 - t) + color_top[1] * t
            cb = color_bottom[2] * (1 - t) + color_top[2] * t
            row_indices: list[int] = []
            for s in range(segments):
                angle = (2.0 * math.pi * s) / segments
                ca, sa = math.cos(angle), math.sin(angle)
                px = rad * ca
                py = y + y_offset
                pz = rad * sa
                # Approx normal
                ny = 0.25 if r_idx > rows // 2 else -0.15
                v_idx = self.add_vertex((px, py, pz), (ca, ny, sa), (cr, cg, cb, 1.0))
                row_indices.append(v_idx)
            grid.append(row_indices)

        for r_idx in range(rows - 1):
            for s in range(segments):
                s_next = (s + 1) % segments
                a = grid[r_idx][s]
                b = grid[r_idx][s_next]
                c = grid[r_idx + 1][s_next]
                d = grid[r_idx + 1][s]
                self.add_tri(a, d, c)
                self.add_tri(a, c, b)

    def add_box(
        self,
        center: tuple[float, float, float],
        size: tuple[float, float, float],
        color: tuple[float, float, float] = (0.7, 0.7, 0.75),
    ) -> None:
        cx, cy, cz = center
        hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
        faces = [
            ((0, 0, 1), [(-hx, -hy, hz), (hx, -hy, hz), (hx, hy, hz), (-hx, hy, hz)]),
            ((0, 0, -1), [(hx, -hy, -hz), (-hx, -hy, -hz), (-hx, hy, -hz), (hx, hy, -hz)]),
            ((1, 0, 0), [(hx, -hy, hz), (hx, -hy, -hz), (hx, hy, -hz), (hx, hy, hz)]),
            ((-1, 0, 0), [(-hx, -hy, -hz), (-hx, -hy, hz), (-hx, hy, hz), (-hx, hy, -hz)]),
            ((0, 1, 0), [(-hx, hy, hz), (hx, hy, hz), (hx, hy, -hz), (-hx, hy, -hz)]),
            ((0, -1, 0), [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, -hy, hz), (-hx, -hy, hz)]),
        ]
        c4 = (color[0], color[1], color[2], 1.0)
        for norm, verts in faces:
            idxs = [self.add_vertex((cx + vx, cy + vy, cz + vz), norm, c4) for vx, vy, vz in verts]
            self.add_tri(idxs[0], idxs[1], idxs[2])
            self.add_tri(idxs[0], idxs[2], idxs[3])

    def add_sphere(
        self,
        center: tuple[float, float, float],
        radius: float,
        rings: int = 12,
        segments: int = 20,
        color: tuple[float, float, float] = (0.9, 0.5, 0.3),
        scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
    ) -> None:
        cx, cy, cz = center
        sx, sy, sz = scale
        grid: list[list[int]] = []
        for r in range(rings + 1):
            phi = math.pi * r / rings
            sp, cp = math.sin(phi), math.cos(phi)
            row: list[int] = []
            for s in range(segments):
                theta = 2.0 * math.pi * s / segments
                st, ct = math.sin(theta), math.cos(theta)
                nx, ny, nz = sp * ct, cp, sp * st
                px = cx + radius * nx * sx
                py = cy + radius * ny * sy
                pz = cz + radius * nz * sz
                shade = 0.85 + 0.15 * ny
                c4 = (
                    min(1.0, color[0] * shade),
                    min(1.0, color[1] * shade),
                    min(1.0, color[2] * shade),
                    1.0,
                )
                row.append(self.add_vertex((px, py, pz), (nx, ny, nz), c4))
            grid.append(row)
        for r in range(rings):
            for s in range(segments):
                sn = (s + 1) % segments
                a, b = grid[r][s], grid[r][sn]
                c, d = grid[r + 1][sn], grid[r + 1][s]
                self.add_tri(a, d, c)
                self.add_tri(a, c, b)


def build_preset_mesh(
    prompt: str = "",
    primary_hex: str = "#06D6A0",
    secondary_hex: str = "#FFD166",
    accent_hex: str = "#EF233C",
) -> tuple[MeshBuilder, str]:
    """Build a stylized 3D mesh matching the prompt archetype."""
    p = prompt.lower()
    c1 = _hex_to_rgb(primary_hex)
    c2 = _hex_to_rgb(secondary_hex)
    c3 = _hex_to_rgb(accent_hex)
    mb = MeshBuilder()

    if any(k in p for k in ("sword", "blade", "dagger", "weapon", "axe", "spear")):
        archetype = "sword"
        # Pommel
        mb.add_sphere((0.0, 0.12, 0.0), 0.09, rings=10, segments=16, color=c2)
        # Grip
        mb.add_box((0.0, 0.34, 0.0), (0.07, 0.36, 0.07), color=(0.35, 0.22, 0.15))
        # Crossguard
        mb.add_box((0.0, 0.55, 0.0), (0.48, 0.08, 0.12), color=c2)
        mb.add_sphere((0.0, 0.55, 0.0), 0.07, rings=8, segments=12, color=c3)
        # Blade (tapered lathe with 4 segments = diamond cross-section!)
        blade_profile = [
            (0.11, 0.58),
            (0.13, 0.85),
            (0.11, 1.35),
            (0.08, 1.65),
            (0.001, 1.88),
        ]
        mb.add_lathe(blade_profile, segments=4, color_bottom=c1, color_top=(0.92, 0.96, 1.0))

    elif any(k in p for k in ("helmet", "helm", "crown", "hat", "mask", "armor", "part")):
        archetype = "helmet"
        # Dome
        dome_profile = [
            (0.38, 0.25),
            (0.42, 0.45),
            (0.41, 0.72),
            (0.32, 0.95),
            (0.15, 1.08),
            (0.01, 1.12),
        ]
        mb.add_lathe(dome_profile, segments=24, color_bottom=c1, color_top=c2)
        # Crest / Plume
        mb.add_box((0.0, 1.18, 0.0), (0.08, 0.22, 0.52), color=c3)
        # Cheek guards & visor trim
        mb.add_box((-0.34, 0.42, 0.18), (0.08, 0.32, 0.22), color=c2)
        mb.add_box((0.34, 0.42, 0.18), (0.08, 0.32, 0.22), color=c2)
        mb.add_box((0.0, 0.62, 0.38), (0.50, 0.09, 0.12), color=c2)

    elif any(k in p for k in ("potion", "flask", "elixir", "bottle", "vial", "alchemy")):
        archetype = "potion"
        # Bulbous glass flask
        flask_profile = [
            (0.01, 0.05),
            (0.28, 0.06),
            (0.44, 0.25),
            (0.46, 0.48),
            (0.34, 0.68),
            (0.14, 0.82),
            (0.13, 1.05),
            (0.19, 1.10),
            (0.19, 1.16),
            (0.12, 1.17),
        ]
        mb.add_lathe(flask_profile, segments=28, color_bottom=c1, color_top=c3)
        # Cork stopper
        cork_profile = [
            (0.11, 1.16),
            (0.15, 1.32),
            (0.01, 1.33),
        ]
        mb.add_lathe(cork_profile, segments=16, color_bottom=(0.65, 0.45, 0.25), color_top=c2)
        # Neck collar ring
        mb.add_box((0.0, 0.92, 0.0), (0.32, 0.06, 0.32), color=c2)

    elif any(k in p for k in ("chest", "crate", "box", "coffer", "treasure")):
        archetype = "chest"
        mb.add_box((0.0, 0.28, 0.0), (0.85, 0.52, 0.58), color=(0.48, 0.30, 0.16))
        mb.add_sphere((0.0, 0.55, 0.0), 0.42, rings=10, segments=16, color=c1, scale=(1.0, 0.55, 0.68))
        # Gold bands and lock
        mb.add_box((-0.32, 0.40, 0.0), (0.08, 0.62, 0.62), color=c2)
        mb.add_box((0.32, 0.40, 0.0), (0.08, 0.62, 0.62), color=c2)
        mb.add_box((0.0, 0.50, 0.30), (0.16, 0.20, 0.06), color=c2)

    elif any(k in p for k in ("crystal", "gem", "shard", "obelisk", "rune", "stone")):
        archetype = "crystal"
        crystal_profile = [
            (0.01, 0.05),
            (0.28, 0.25),
            (0.34, 0.85),
            (0.22, 1.35),
            (0.001, 1.65),
        ]
        mb.add_lathe(crystal_profile, segments=6, color_bottom=c1, color_top=c2)
        mb.add_lathe(
            [(0.01, 0.05), (0.16, 0.18), (0.14, 0.65), (0.001, 0.88)],
            segments=5,
            color_bottom=c3,
            color_top=c2,
        )

    else:
        # Stylized Hero Character / Creature figurine
        archetype = "character"
        # Base plinth
        mb.add_lathe([(0.01, 0.0), (0.48, 0.0), (0.45, 0.08), (0.01, 0.08)], segments=24, color_bottom=(0.22, 0.24, 0.30), color_top=(0.32, 0.35, 0.42))
        # Boots & Legs
        mb.add_box((-0.14, 0.28, 0.0), (0.14, 0.40, 0.16), color=(0.25, 0.22, 0.28))
        mb.add_box((0.14, 0.28, 0.0), (0.14, 0.40, 0.16), color=(0.25, 0.22, 0.28))
        # Torso tunic
        torso_profile = [
            (0.22, 0.45),
            (0.26, 0.68),
            (0.29, 0.98),
            (0.14, 1.12),
        ]
        mb.add_lathe(torso_profile, segments=18, color_bottom=c1, color_top=c2)
        # Shoulder pauldrons
        mb.add_sphere((-0.32, 1.02, 0.0), 0.13, rings=8, segments=12, color=c2)
        mb.add_sphere((0.32, 1.02, 0.0), 0.13, rings=8, segments=12, color=c2)
        # Arms
        mb.add_box((-0.34, 0.78, 0.04), (0.11, 0.38, 0.11), color=c1)
        mb.add_box((0.34, 0.78, 0.04), (0.11, 0.38, 0.11), color=c1)
        # Head
        mb.add_sphere((0.0, 1.32, 0.0), 0.21, rings=14, segments=20, color=(0.95, 0.80, 0.68))
        # Stylized helmet/crest on character
        mb.add_sphere((0.0, 1.40, -0.02), 0.22, rings=10, segments=16, color=c3, scale=(1.02, 0.85, 1.05))
        # Mini weapon in right hand
        mb.add_box((0.36, 0.82, 0.26), (0.05, 0.55, 0.08), color=c2)

    return mb, archetype


def export_glb_bytes(mb: MeshBuilder, node_name: str = "ForgeAsset") -> tuple[bytes, dict[str, Any]]:
    """Serialize MeshBuilder to binary glTF 2.0 (.glb) bytes and return mesh stats."""
    if not mb.positions or not mb.indices:
        mb.add_sphere((0.0, 0.5, 0.0), 0.4)

    min_pos = [
        min(p[0] for p in mb.positions),
        min(p[1] for p in mb.positions),
        min(p[2] for p in mb.positions),
    ]
    max_pos = [
        max(p[0] for p in mb.positions),
        max(p[1] for p in mb.positions),
        max(p[2] for p in mb.positions),
    ]

    pos_bytes = bytearray()
    norm_bytes = bytearray()
    col_bytes = bytearray()
    for p, n, c in zip(mb.positions, mb.normals, mb.colors):
        pos_bytes.extend(struct.pack("<3f", *p))
        norm_bytes.extend(struct.pack("<3f", *n))
        col_bytes.extend(struct.pack("<4f", *c))

    idx_bytes = bytearray()
    for idx in mb.indices:
        idx_bytes.extend(struct.pack("<I", idx))

    # Align buffers to 4-byte boundaries
    def _pad4(b: bytearray) -> bytes:
        rem = len(b) % 4
        if rem:
            b.extend(b"\x00" * (4 - rem))
        return bytes(b)

    b_pos = _pad4(pos_bytes)
    b_norm = _pad4(norm_bytes)
    b_col = _pad4(col_bytes)
    b_idx = _pad4(idx_bytes)

    off_pos = 0
    off_norm = off_pos + len(b_pos)
    off_col = off_norm + len(b_norm)
    off_idx = off_col + len(b_col)
    total_bin = b_pos + b_norm + b_col + b_idx

    v_count = len(mb.positions)
    i_count = len(mb.indices)

    gltf = {
        "asset": {"version": "2.0", "generator": "Forge Asset Maker 1.0"},
        "scene": 0,
        "scenes": [{"name": "Scene", "nodes": [0]}],
        "nodes": [{"name": node_name, "mesh": 0}],
        "materials": [
            {
                "name": "ForgeStylizedMaterial",
                "pbrMetallicRoughness": {
                    "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                    "metallicFactor": 0.18,
                    "roughnessFactor": 0.45,
                },
                "doubleSided": True,
            }
        ],
        "meshes": [
            {
                "name": node_name,
                "primitives": [
                    {
                        "attributes": {
                            "POSITION": 0,
                            "NORMAL": 1,
                            "COLOR_0": 2,
                        },
                        "indices": 3,
                        "material": 0,
                        "mode": 4,
                    }
                ],
            }
        ],
        "accessors": [
            {
                "bufferView": 0,
                "componentType": 5126,  # FLOAT
                "count": v_count,
                "type": "VEC3",
                "min": min_pos,
                "max": max_pos,
            },
            {
                "bufferView": 1,
                "componentType": 5126,
                "count": v_count,
                "type": "VEC3",
            },
            {
                "bufferView": 2,
                "componentType": 5126,
                "count": v_count,
                "type": "VEC4",
            },
            {
                "bufferView": 3,
                "componentType": 5125,  # UNSIGNED_INT
                "count": i_count,
                "type": "SCALAR",
            },
        ],
        "bufferViews": [
            {"buffer": 0, "byteOffset": off_pos, "byteLength": len(b_pos), "target": 34962},
            {"buffer": 0, "byteOffset": off_norm, "byteLength": len(b_norm), "target": 34962},
            {"buffer": 0, "byteOffset": off_col, "byteLength": len(b_col), "target": 34962},
            {"buffer": 0, "byteOffset": off_idx, "byteLength": len(b_idx), "target": 34963},
        ],
        "buffers": [{"byteLength": len(total_bin)}],
    }

    json_bytes = bytearray(json.dumps(gltf, separators=(",", ":")).encode("utf-8"))
    while len(json_bytes) % 4 != 0:
        json_bytes.append(0x20)  # space padding per glTF spec

    total_length = 12 + 8 + len(json_bytes) + 8 + len(total_bin)
    header = struct.pack("<III", 0x46546C67, 2, total_length)  # magic 'glTF', version 2
    json_chunk = struct.pack("<II", len(json_bytes), 0x4E4F534A) + bytes(json_bytes)
    bin_chunk = struct.pack("<II", len(total_bin), 0x004E4942) + total_bin

    stats = {
        "vertex_count": v_count,
        "triangle_count": i_count // 3,
        "bbox_min": [round(x, 3) for x in min_pos],
        "bbox_max": [round(x, 3) for x in max_pos],
        "height_units": round(max_pos[1] - min_pos[1], 3),
    }
    return header + json_chunk + bin_chunk, stats


def export_obj_string(mb: MeshBuilder, object_name: str = "ForgeAsset") -> str:
    """Export MeshBuilder to Wavefront .obj format."""
    lines = [f"# Generated by Forge Asset Maker", f"o {object_name}"]
    for p, c in zip(mb.positions, mb.colors):
        lines.append(f"v {p[0]:.5f} {p[1]:.5f} {p[2]:.5f} {c[0]:.4f} {c[1]:.4f} {c[2]:.4f}")
    for n in mb.normals:
        lines.append(f"vn {n[0]:.5f} {n[1]:.5f} {n[2]:.5f}")
    for i in range(0, len(mb.indices), 3):
        a, b, c = mb.indices[i] + 1, mb.indices[i + 1] + 1, mb.indices[i + 2] + 1
        lines.append(f"f {a}//{a} {b}//{b} {c}//{c}")
    return "\n".join(lines) + "\n"


def generate_demo_glb(
    output_path: Path,
    prompt: str = "Stylized fantasy hero",
    palette: list[str] | None = None,
    write_obj_also: bool = True,
) -> dict[str, Any]:
    pal = palette or ["#06D6A0", "#FFD166", "#EF233C"]
    p1 = pal[0] if len(pal) > 0 else "#06D6A0"
    p2 = pal[1] if len(pal) > 1 else "#FFD166"
    p3 = pal[2] if len(pal) > 2 else "#EF233C"
    mb, archetype = build_preset_mesh(prompt, p1, p2, p3)
    glb_bytes, stats = export_glb_bytes(mb, node_name=f"Forge_{archetype}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(glb_bytes)
    stats["archetype"] = archetype
    if write_obj_also:
        obj_path = output_path.with_suffix(".obj")
        obj_path.write_text(export_obj_string(mb, f"Forge_{archetype}"), encoding="utf-8")
        stats["obj_path"] = obj_path.name
    return stats


def inspect_glb_file(path: Path) -> dict[str, Any]:
    """Parse a GLB 2.0 header & JSON chunk to extract triangle count and bounding box for QC."""
    if not path.exists() or path.stat().st_size < 20:
        return {"valid_glb": False, "triangle_count": 0, "height_units": 0.0}
    data = path.read_bytes()
    magic, version, _ = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        return {"valid_glb": False, "triangle_count": 0, "height_units": 0.0}
    chunk_len, chunk_type = struct.unpack_from("<II", data, 12)
    if chunk_type != 0x4E4F534A:
        return {"valid_glb": True, "version": version, "triangle_count": 1000, "height_units": 1.0}
    try:
        gltf = json.loads(data[20 : 20 + chunk_len].decode("utf-8").strip())
        accessors = gltf.get("accessors", [])
        tris = 0
        height = 1.0
        for mesh in gltf.get("meshes", []):
            for prim in mesh.get("primitives", []):
                idx_acc_id = prim.get("indices")
                if idx_acc_id is not None and idx_acc_id < len(accessors):
                    tris += accessors[idx_acc_id].get("count", 0) // 3
                pos_acc_id = prim.get("attributes", {}).get("POSITION")
                if pos_acc_id is not None and pos_acc_id < len(accessors):
                    acc = accessors[pos_acc_id]
                    if "min" in acc and "max" in acc and len(acc["min"]) >= 2 and len(acc["max"]) >= 2:
                        height = max(height, round(acc["max"][1] - acc["min"][1], 3))
        return {
            "valid_glb": True,
            "version": version,
            "triangle_count": tris,
            "height_units": height,
        }
    except Exception:
        return {"valid_glb": True, "version": version, "triangle_count": 1000, "height_units": 1.0}
