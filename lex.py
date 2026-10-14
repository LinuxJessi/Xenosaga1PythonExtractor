"""Xenosaga Episode I ``.lex`` models -> glTF 2.0 binary (``.glb``).

A ``.lex`` is a VU1 display list: per mesh, a run of VIF ``UNPACK``
batches (positions+normals, colours, skin weights) terminated by ``MSCNT``,
with material changes spliced in as ``UNPACK``s to VU address 0 carrying a
0x40-tagged block header. The layout is Lakuwu's xenotool reverse
engineering (``lex_file.h`` / ``xeno_lex.c``), re-implemented here from
those format notes; the output was byte-checked against xenotool's own
triangle and weight-group counts on the retail disc (see FORMATS.md
"LEX models").

Texture coordinates are GS ST values: multiply by the TEX0 texture size
the model declares (1024x256 for character sheets — NOT the decoded
atlas size, which can be narrower) to get texels, then wrap each triangle
into its material's rect — the GS region-repeat window — doubled for true-colour
(``pal == 0xFF``) materials, whose rect is in canvas coordinates.

Public API::

    model = parse_lex(data)                 # ParsedLex
    glb = to_glb(model, png_bytes, (w, h))  # bytes, ready for Blender

Skinning: vertices are stored in model space already, so the skin is
exported with identity bind matrices and one joint node per weight group
(``j<id>``, ids from the lex bone tables). The ``.jnt`` hierarchy/pose is
not decoded yet — the joints give Blender named vertex groups, not a posed
armature.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Optional

__all__ = ["ParsedLex", "LexMesh", "LexMaterial", "Skeleton", "parse_lex",
           "parse_jnt", "to_glb", "merge_companion", "LexError"]


class LexError(ValueError):
    pass


@dataclass(frozen=True)
class LexMaterial:
    umin: int
    umax: int
    vmin: int
    vmax: int
    pal: int          # palette byte; 0xFF = true-colour (CT32) material
    pal2: int
    has_texture: bool
    color: tuple = ()  # 12 floats: three RGBA material colours
    uv_type: int = 0   # UVInfo type byte (0x7F = eyelash sheet, 4bpp)

    @property
    def true_color(self) -> bool:
        return self.pal == 0xFF


@dataclass
class LexMesh:
    index: int
    bone: str
    group: str
    material_name: str
    weight_format: int
    bone_idx: int
    # per vertex, parallel lists
    positions: list[tuple[float, float, float]] = field(default_factory=list)
    normals: list[tuple[float, float, float]] = field(default_factory=list)
    uvs: list[tuple[float, float]] = field(default_factory=list)
    colors: list[tuple[float, float, float, float]] = field(default_factory=list)
    joints: list[tuple[int, int, int, int]] = field(default_factory=list)
    weights: list[tuple[float, float, float, float]] = field(default_factory=list)
    # (i0, i1, i2, material index)
    triangles: list[tuple[int, int, int, int]] = field(default_factory=list)
    skipped_batches: int = 0


@dataclass
class ParsedLex:
    name: str
    artist: str
    materials: list[LexMaterial]
    meshes: list[LexMesh]
    joint_ids: list[int]          # weight-group id per glTF joint index
    matrices: list[tuple]         # nmatrix pairs of 4x4 (row-major floats)
    bounds: tuple                 # (min xyz, max xyz) from the header
    warnings: list[str]
    tex_size: Optional[tuple[int, int]] = None  # GS TEX0 (2^TW, 2^TH) the UVs are relative to
    companion_joints: dict = field(default_factory=dict)  # joint id -> companion prefix (merged-in parts)
    companion_skeletons: dict = field(default_factory=dict)  # prefix -> Skeleton (face .jnt; already in body space)

    @property
    def triangle_count(self) -> int:
        return sum(len(m.triangles) for m in self.meshes)


@dataclass
class Skeleton:
    """A ``.jnt`` skeleton: one node per 64-byte block.

    Block layout (fitted on the retail disc, see FORMATS.md): two floats
    at +0/+8 (limits, unused here); u16 type at +16 (1 offset node,
    2 rotation joint, 3 translation, 4 bone end, 6 name record); u16
    parent block index at +30 (self/out of range = root); float3 local
    position at +32; float3 local rotation at +48 = (angle about Z, about
    Y, about X), R = Rz*Ry*Rx. Skin weight-group ids map onto the type-2 blocks in
    order (ids are contiguous per model; the k-th id is the k-th
    rotation joint)."""
    name: str
    types: list[int]
    parents: list[Optional[int]]
    positions: list[tuple[float, float, float]]
    rotations: list[tuple[float, float, float]]
    names: list[str]            # the type-6 name records, in order
    bind_worlds: dict = field(default_factory=dict)  # block -> 4x4 world (row-major) captured from the running game

    @property
    def joint_blocks(self) -> list[int]:
        return [i for i, t in enumerate(self.types) if t == 2]

    def local_matrix(self, i: int) -> list[list[float]]:
        """Local transform of block i. With a captured bind pose, locals are
        derived from the captured world matrices (local = parent^-1 * world)
        so the exported nodes reproduce the pose the mesh was modelled in."""
        if self.bind_worlds:
            w = self.bind_worlds.get(i)
            if w is not None:
                p = self.parents[i]
                pw = self.bind_worlds.get(p) if p is not None else None
                return _matmul(_invert_rigid(pw), w) if pw is not None else w
        return _trs(self.positions[i], self.rotations[i])

    def apply_pose(self, pose: dict) -> None:
        """Install a captured pose: {block index: 16 floats row-major}.
        Root blocks hold the character's placement in the field (the
        children are model-space), so roots are forced to identity."""
        self.bind_worlds = {int(k): [list(v[r * 4:(r + 1) * 4]) for r in range(4)]
                            for k, v in pose.items()}
        for m in self.bind_worlds.values():   # the engine keeps a 1 in each row's 4th slot
            m[3] = [0.0, 0.0, 0.0, 1.0]
        # the type-1 records after the name table (the "post" blocks the
        # 0x20 table points at) are not evaluated nodes; their slots in the
        # engine table hold unrelated data, so keep their rest transforms
        names = [i for i, t in enumerate(self.types) if t == 6]
        if names:
            for i in range(names[-1] + 1, len(self.types)):
                self.bind_worlds.pop(i, None)
        for i, p in enumerate(self.parents):
            if p is None and i in self.bind_worlds:
                self.bind_worlds[i] = [row[:] for row in _IDENT]

    def world_matrices(self) -> list[list[list[float]]]:
        if self.bind_worlds:
            rest = self._rest_worlds()
            return [self.bind_worlds.get(i, rest[i]) for i in range(len(self.types))]
        return self._rest_worlds()

    def _rest_worlds(self) -> list[list[list[float]]]:
        out: list = [None] * len(self.types)

        def get(i: int, depth: int = 0):
            if out[i] is not None:
                return out[i]
            p = self.parents[i]
            parent = _IDENT if (p is None or depth > 512) else get(p, depth + 1)
            out[i] = _matmul(parent, _trs(self.positions[i], self.rotations[i]))
            return out[i]

        for i in range(len(self.types)):
            get(i)
        return out


_IDENT = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]


def _matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def _trs(pos, rot):
    """Local matrix from a .jnt node: the stored triple is (angle about Z,
    angle about Y, angle about X), composed R = Rz(a) * Ry(b) * Rx(c).
    Verified exactly (0.0000 m over 120 nodes) against the engine's own
    matrix table read from the running game."""
    a, b, c = rot
    cz, sz = math.cos(a), math.sin(a)
    cy, sy = math.cos(b), math.sin(b)
    cx, sx = math.cos(c), math.sin(c)
    rz = [[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]]
    ry = [[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]]
    rx = [[1, 0, 0], [0, cx, -sx], [0, sx, cx]]
    m3 = [[sum(rz[i][k] * ry[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    m3 = [[sum(m3[i][k] * rx[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    return [[m3[0][0], m3[0][1], m3[0][2], pos[0]],
            [m3[1][0], m3[1][1], m3[1][2], pos[1]],
            [m3[2][0], m3[2][1], m3[2][2], pos[2]],
            [0.0, 0.0, 0.0, 1.0]]


def _invert_rigid(m):
    """Inverse of a rotation+translation matrix."""
    r = [[m[j][i] for j in range(3)] for i in range(3)]  # transpose
    t = [m[0][3], m[1][3], m[2][3]]
    nt = [-sum(r[i][k] * t[k] for k in range(3)) for i in range(3)]
    return [[r[0][0], r[0][1], r[0][2], nt[0]],
            [r[1][0], r[1][1], r[1][2], nt[1]],
            [r[2][0], r[2][1], r[2][2], nt[2]],
            [0.0, 0.0, 0.0, 1.0]]


def _quat_from_matrix(m) -> tuple[float, float, float, float]:
    """(x, y, z, w) from a 3x3 rotation (rows of a 4x4)."""
    t = m[0][0] + m[1][1] + m[2][2]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        return ((m[2][1] - m[1][2]) / s, (m[0][2] - m[2][0]) / s, (m[1][0] - m[0][1]) / s, 0.25 * s)
    if m[0][0] > m[1][1] and m[0][0] > m[2][2]:
        s = math.sqrt(1.0 + m[0][0] - m[1][1] - m[2][2]) * 2
        return (0.25 * s, (m[0][1] + m[1][0]) / s, (m[0][2] + m[2][0]) / s, (m[2][1] - m[1][2]) / s)
    if m[1][1] > m[2][2]:
        s = math.sqrt(1.0 + m[1][1] - m[0][0] - m[2][2]) * 2
        return ((m[0][1] + m[1][0]) / s, 0.25 * s, (m[1][2] + m[2][1]) / s, (m[0][2] - m[2][0]) / s)
    s = math.sqrt(1.0 + m[2][2] - m[0][0] - m[1][1]) * 2
    return ((m[0][2] + m[2][0]) / s, (m[1][2] + m[2][1]) / s, 0.25 * s, (m[1][0] - m[0][1]) / s)


def parse_jnt(data: bytes) -> Skeleton:
    """Parse a (decompressed) ``JNT\0`` skeleton."""
    if data[:4] != b"JNT\0" or len(data) < 0x20:
        raise LexError("not a jnt file")
    _magic, _u0, count, _a, _b, _c, _d, offset, _e, _f, name = struct.unpack_from("<4sHHHHHHIHH8s", data, 0)
    if offset + 64 * count > len(data):
        raise LexError("jnt block table beyond file")
    types, parents, positions, rotations, names = [], [], [], [], []
    for i in range(count):
        b = data[offset + 64 * i: offset + 64 * (i + 1)]
        t = struct.unpack_from("<H", b, 16)[0]
        parent = struct.unpack_from("<H", b, 30)[0]
        types.append(t)
        parents.append(parent if (parent < count and parent != i) else None)
        positions.append(struct.unpack_from("<3f", b, 32))
        rotations.append(struct.unpack_from("<3f", b, 48))
        if t == 6:
            group = _cstr(b[0:8]); label = _cstr(b[8:24])
            names.append(f"{group}/{label}" if group else label)
    return Skeleton(_cstr(name), types, parents, positions, rotations, names)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

def _uv_rect(uvinfo: bytes) -> tuple[int, int, int, int, bool]:
    t, b = uvinfo[0], uvinfo[1:16]
    if t == 0x00:
        return 0, 0, 0, 0, False
    if t in (0xFF, 0x7F):
        # b[1] is the x origin in 4-px units (xenotool read only its top
        # five bits: x*64 + x1*32; bit 2 adds 16 — the eyelash sheets at
        # x=208/144/240 need it), b[2] high nibble the height in 16s,
        # b[3] bit 7 + b[4] low nibble the y origin (y*64 + y1*32)
        w, h = b[0] & 0xF, b[2] >> 4
        umin = b[1] * 4
        vmin = (b[4] & 0xF) * 64 + (b[3] >> 7) * 32
        if t == 0x7F:
            # eyelash materials ("*_matu"): a 32x32 sheet; the UVs of all
            # 1,019 on the disc stay inside (umin, umin+32) x (vmin, vmin+32)
            return umin, umin + 32, vmin, vmin + 32, True
        return umin, umin + (w + 1) * 16, vmin, vmin + (h + 1) * 16, True
    umin = (b[0] & 0x3F) << 4
    vmin = b[2]
    umax = ((b[1] << 2) | (b[0] >> 6)) + 1
    vmax = ((b[4] << 6) | (b[3] >> 2)) + 1
    return umin, umax, vmin, vmax, True


def _material(uvinfo: bytes, palinfo: bytes, color: bytes) -> LexMaterial:
    umin, umax, vmin, vmax, has = _uv_rect(uvinfo)
    col = struct.unpack("<12f", color) if len(color) == 48 else ()
    return LexMaterial(umin, umax, vmin, vmax, palinfo[5], palinfo[4], has, col,
                       uvinfo[0])


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("ascii", "replace")


def parse_lex(data: bytes) -> ParsedLex:
    """Parse a (decompressed) ``lex\\0`` model. Raises LexError on a file
    that is not a lex; per-mesh oddities are skipped and reported in
    ``warnings`` rather than aborting the model."""
    if data[:4] != b"lex\0" or len(data) < 0xB0:
        raise LexError("not a lex file")
    name, artist = _cstr(data[0x10:0x30]), _cstr(data[0x30:0x3F])
    nmatrix, nmesh = struct.unpack_from("<II", data, 0x40)
    addr = struct.unpack_from("<5I", data, 0x50)
    bmax = struct.unpack_from("<3f", data, 0x70)
    bmin = struct.unpack_from("<3f", data, 0x80)
    if not (0 <= nmesh <= 8192) or 0xB0 + 4 * nmesh > len(data):
        raise LexError("implausible mesh count")
    matrices = []
    for i in range(nmatrix * 2):
        off = addr[0] + 64 * i
        if off + 64 > len(data):
            break
        matrices.append(struct.unpack_from("<16f", data, off))

    materials: list[LexMaterial] = []
    mat_index: dict[LexMaterial, int] = {}

    def material_id(m: LexMaterial) -> int:
        if m not in mat_index:
            mat_index[m] = len(materials)
            materials.append(m)
        return mat_index[m]

    joint_ids: list[int] = []
    joint_index: dict[int, int] = {}

    def joint(bone_id: int) -> int:
        if bone_id not in joint_index:
            joint_index[bone_id] = len(joint_ids)
            joint_ids.append(bone_id)
        return joint_index[bone_id]

    warnings: list[str] = []
    meshes: list[LexMesh] = []
    for mi in range(nmesh):
        (a,) = struct.unpack_from("<I", data, 0xB0 + 4 * mi)
        if a + 0x190 > len(data):
            warnings.append(f"mesh {mi}: header beyond file")
            continue
        wf, doff, dlen, bidx = struct.unpack_from("<4I", data, a + 0x20)
        bone_table = struct.unpack_from("<32H", data, a + 0x30)
        vfmt = data[a + 0xC0] & 0xF0
        color = data[a + 0xD0:a + 0x100]
        mesh = LexMesh(mi, _cstr(data[a:a + 32]), _cstr(data[a + 0x150:a + 0x170]),
                       _cstr(data[a + 0x170:a + 0x190]), wf, bidx)
        cur_mat = material_id(_material(data[a + 0x130:a + 0x140],
                                        data[a + 0x120:a + 0x130], color))
        p, end = a + doff, min(len(data), a + doff + dlen)
        blocks: list[tuple[int, bytes]] = []   # (vu address, payload)
        count = 0
        try:
            while p + 4 <= end:
                imm, num, cmd = struct.unpack_from("<HBB", data, p)
                p += 4
                if cmd in (0x00, 0x01):        # NOP, STCYCL
                    continue
                if cmd == 0x20:                # STMASK
                    p += 4
                    continue
                if cmd == 0x17:                # MSCNT: run the batch
                    if blocks and count and blocks[0][0] == 0:
                        _emit_batch(mesh, blocks, count, vfmt, wf, bone_table,
                                    joint, cur_mat, warnings)
                    elif blocks:
                        mesh.skipped_batches += 1  # VU constant uploads
                    blocks, count = [], 0
                    continue
                if 0x60 <= cmd < 0x80:         # UNPACK
                    utype = cmd & 0xF
                    comps, cbytes = (utype >> 2) + 1, (4, 2, 1, 0)[utype & 3]
                    if cbytes == 0:
                        raise LexError(f"bad unpack type {utype:#x}")
                    vaddr = imm & 0x3FF
                    if vaddr == 0:
                        hdr = data[p:p + 16]
                        p += 16
                        num -= 1
                        if hdr[5] == 0x40:     # material block, not vertices
                            size = num * comps * cbytes
                            blk = data[p:p + size]
                            p += size
                            if size in (128, 64):
                                color = blk[64:112] if size == 128 else color
                                cur_mat = material_id(
                                    _material(blk[16:32], blk[32:48], color))
                            else:
                                warnings.append(
                                    f"mesh {mi}: {size}-byte material block")
                            continue
                        if hdr[5] != 0:
                            raise LexError(f"block header tag {hdr[5]:#x}")
                        count = hdr[0]
                    size = (num * comps * cbytes + 3) & ~3
                    blocks.append((vaddr, data[p:p + size]))
                    p += size
                    continue
                raise LexError(f"VIF command {cmd:#x}")
        except (LexError, struct.error) as e:
            warnings.append(f"mesh {mi} ({mesh.bone}): {e} at {p:#x}; "
                            f"kept {len(mesh.triangles)} triangles")
        meshes.append(mesh)
    return ParsedLex(name, artist, materials, meshes, joint_ids, matrices,
                     (bmin, bmax), warnings, _tex0_size(data))


def _tex0_size(data: bytes) -> Optional[tuple[int, int]]:
    """The texture size the model's UVs are relative to: the (2^TW, 2^TH)
    of the GS TEX0 register writes embedded in the VIF streams (A+D pairs:
    u64 value, u64 register 0x06/0x16), most common wins. Character sheets
    say 1024x256 regardless of how wide the decoded atlas comes out."""
    from collections import Counter
    sizes: Counter = Counter()
    for off in range(0, len(data) - 16, 8):
        reg = struct.unpack_from("<Q", data, off + 8)[0]
        if reg not in (0x06, 0x16):
            continue
        v = struct.unpack_from("<Q", data, off)[0]
        psm, tw, th = (v >> 20) & 0x3F, (v >> 26) & 0xF, (v >> 30) & 0xF
        if 4 <= tw <= 10 and 4 <= th <= 10 and psm in (0, 19, 20, 27, 36, 44):
            sizes[(1 << tw, 1 << th)] += 1
    return sizes.most_common(1)[0][0] if sizes else None


def _emit_batch(mesh: LexMesh, blocks, n: int, vfmt: int, wf: int,
                bone_table, joint, mat: int, warnings: list[str]) -> None:
    """Decode one MSCNT batch of ``n`` strip vertices into the mesh."""
    if vfmt == 0x80:
        if len(blocks) == 1:   # everything in one upload
            b0 = blocks[0][1]
            pos, col, wgt = b0[:n * 32], b0[n * 32:n * 48], b0[n * 48:]
        else:
            pos = blocks[0][1]
            col = blocks[1][1]
            wgt = blocks[2][1] if len(blocks) > 2 else b""
        if len(pos) < n * 32 or len(col) < n * 16:
            raise LexError(f"short vertex batch ({len(pos)}/{n * 32} B)")
        xyzu = list(struct.iter_unpack("<4f", pos[:n * 16]))
        nnnv = list(struct.iter_unpack("<4f", pos[n * 16:n * 32]))
        rgba = list(struct.iter_unpack("<4f", col[:n * 16]))
        positions = [(x, y, z) for x, y, z, _ in xyzu]
        normals = [(x, y, z) for x, y, z, _ in nnnv]
        uvs = [(xyzu[i][3], nnnv[i][3]) for i in range(n)]
        colors = [tuple(min(c / 128.0, 1.0) for c in q) for q in rgba]
    elif vfmt == 0x10:
        if len(blocks) < 3:
            raise LexError("vertex format 0x10 with a single upload")
        pos, vb = blocks[0][1], blocks[1][1]
        if len(pos) < n * 16 or len(vb) < n * 4:
            raise LexError("short vertex batch")
        xyzu = list(struct.iter_unpack("<4f", pos[:n * 16]))
        vs = [v[0] for v in struct.iter_unpack("<f", vb[:n * 4])]
        positions = [(x, y, z) for x, y, z, _ in xyzu]
        normals = [(0.0, 0.0, 0.0)] * n
        uvs = [(xyzu[i][3], vs[i]) for i in range(n)]
        # colours: the first byte upload (xenotool: p[2] if it is V4-8)
        cblk = next((b for _, b in blocks[2:] if len(b) >= n * 4), b"")
        if len(cblk) >= n * 4:
            colors = [tuple(min(c / 128.0, 1.0) for c in cblk[i * 4:i * 4 + 4])
                      for i in range(n)]
        else:
            colors = [(1.0, 1.0, 1.0, 1.0)] * n
        wgt = blocks[2][1] if len(blocks) > 3 else b""
    else:
        raise LexError(f"vertex format {vfmt:#x}")

    joints = [(0, 0, 0, 0)] * n
    weights = [(0.0, 0.0, 0.0, 0.0)] * n

    def jidx(bone_v: int) -> int:
        bi = bone_v // 4 - 184
        if bone_v == 0 or bi < 0 or bi + 1 >= len(bone_table):
            return 0
        return joint(bone_table[bi + 1])

    if wf == 1 and len(wgt) >= n * 32:
        joints, weights = [], []
        for v in range(n):
            ids = struct.unpack_from("<4I", wgt, v * 16)
            ws = struct.unpack_from("<4f", wgt, n * 16 + v * 16)
            joints.append(tuple(jidx(i) for i in ids))
            weights.append(tuple(ws))
    elif wf == 3 and len(wgt) >= n * 16:
        joints, weights = [], []
        for v in range(n):
            ids = struct.unpack_from("<4I", wgt, v * 16)
            joints.append(tuple(jidx(i) for i in ids))
            weights.append(tuple(1.0 if i else 0.0 for i in ids))
    elif wf == 5 and len(wgt) >= n * 16:
        joints, weights = [], []
        for v in range(n):
            i0, i1, w0, w1 = struct.unpack_from("<2I2f", wgt, v * 16)
            joints.append((jidx(i0), jidx(i1), 0, 0))
            weights.append((w0, w1, 0.0, 0.0))
    base = len(mesh.positions)
    mesh.positions += positions
    mesh.normals += normals
    mesh.uvs += uvs
    mesh.colors += colors
    mesh.joints += joints
    mesh.weights += weights
    for v in range(n - 2):
        i0, i1, i2 = v, v + 1, v + 2
        if v & 1:
            i1, i2 = i2, i1
        mesh.triangles.append((base + i0, base + i1, base + i2, mat))


def merge_companion(main: ParsedLex, other: ParsedLex, prefix: str = "",
                    skeleton: Optional["Skeleton"] = None) -> None:
    """Append ``other``'s meshes to ``main`` (a companion model such as
    ``kosmos_face.lex`` — face, eyes, mouth, lashes — that shares the
    body's atlas and skeleton ids), remapping material and joint indices.
    Mesh names get ``prefix`` so the parts stay identifiable in Blender."""
    mat_map = {}
    for i, m in enumerate(other.materials):
        if m not in main.materials:
            main.materials.append(m)
        mat_map[i] = main.materials.index(m)
    # companion joint ids live in the companion's own .jnt, so keep them
    # apart from the body's ids (offset into a private range per companion)
    joint_map = {}
    tag = 100000 * (1 + len({v for v in main.companion_joints.values()}))
    for i, jid in enumerate(other.joint_ids):
        pjid = tag + jid
        if pjid not in main.joint_ids:
            main.joint_ids.append(pjid)
            main.companion_joints[pjid] = prefix
        joint_map[i] = main.joint_ids.index(pjid)
    base_index = max((m.index for m in main.meshes), default=-1) + 1
    for mesh in other.meshes:
        mm = LexMesh(base_index + mesh.index, mesh.bone,
                     (prefix + mesh.group) if mesh.group else prefix.rstrip("/"),
                     mesh.material_name, mesh.weight_format, mesh.bone_idx,
                     list(mesh.positions), list(mesh.normals), list(mesh.uvs),
                     list(mesh.colors),
                     [tuple(joint_map.get(j, 0) for j in js) for js in mesh.joints],
                     list(mesh.weights),
                     [(a, b, c, mat_map[m]) for a, b, c, m in mesh.triangles],
                     mesh.skipped_batches)
        main.meshes.append(mm)
    main.warnings += [f"{prefix}{w}" for w in other.warnings]
    if skeleton is not None:
        main.companion_skeletons[prefix] = skeleton


# ---------------------------------------------------------------------------
# glTF export
# ---------------------------------------------------------------------------

def _wrap_tri(us, lo, hi):
    """Shift a triangle's three coordinates by whole rect periods until the
    triangle overlaps [lo, hi) (GS region repeat, applied per primitive so
    a seam-spanning triangle is not torn)."""
    if hi <= lo:
        return us
    d = hi - lo
    while all(u >= hi for u in us):
        us = [u - d for u in us]
    while all(u < lo for u in us):
        us = [u + d for u in us]
    return us


def to_glb(model: ParsedLex, png: Optional[bytes], tex_size: tuple[int, int],
           name: Optional[str] = None, skeleton: Optional[Skeleton] = None) -> bytes:
    """Serialise ``model`` as a glTF 2.0 binary. ``png`` is the decoded
    atlas (``decode_xtx`` output as PNG bytes) or None for an untextured
    model; ``tex_size`` its (width, height) in pixels."""
    W, H = tex_size if tex_size and tex_size[0] else (1024, 256)
    # UVs are GS ST values relative to the TEX0 texture size, which can
    # exceed the decoded atlas (Cherenkov: TEX0 1024x256, atlas 928x256)
    SW, SH = model.tex_size or (W, H)
    bin_chunk = bytearray()
    views: list[dict] = []
    accessors: list[dict] = []

    def add_view(blob: bytes, target: Optional[int] = None, stride: Optional[int] = None) -> int:
        while len(bin_chunk) % 4:
            bin_chunk.append(0)
        views.append({k: v for k, v in {
            "buffer": 0, "byteOffset": len(bin_chunk), "byteLength": len(blob),
            "target": target, "byteStride": stride}.items() if v is not None})
        bin_chunk.extend(blob)
        return len(views) - 1

    def add_accessor(view: int, ctype: int, count: int, atype: str,
                     mn=None, mx=None, normalized=False) -> int:
        acc = {"bufferView": view, "componentType": ctype, "count": count, "type": atype}
        if mn is not None:
            acc["min"], acc["max"] = list(mn), list(mx)
        if normalized:
            acc["normalized"] = True
        accessors.append(acc)
        return len(accessors) - 1

    has_skin = bool(model.joint_ids) and any(
        any(w for w in ws) for m in model.meshes for ws in m.weights)
    gl_meshes: list[dict] = []
    nodes: list[dict] = []
    scene_nodes: list[int] = []

    # materials: one glTF material per lex material; the atlas is shared
    textures_json: list[dict] = []
    images_json: list[dict] = []
    samplers_json: list[dict] = []
    if png:
        img_view = add_view(png)
        images_json.append({"bufferView": img_view, "mimeType": "image/png"})
        samplers_json.append({"magFilter": 9729, "minFilter": 9987,
                              "wrapS": 10497, "wrapT": 10497})
        textures_json.append({"sampler": 0, "source": 0})
    gl_materials: list[dict] = []
    for i, m in enumerate(model.materials):
        mat = {"name": f"mat{i}_u{m.umin}-{m.umax}_v{m.vmin}-{m.vmax}"
                       + ("_rgba" if m.true_color else "")
                       + ("_lash-4bpp-undecoded" if m.uv_type == 0x7F else ""),
               "pbrMetallicRoughness": {"metallicFactor": 0.0,
                                        "roughnessFactor": 0.9},
               "doubleSided": True,
               "alphaMode": "BLEND" if m.true_color else "MASK"}
        if not m.true_color:
            mat["alphaCutoff"] = 0.5
        if png and m.has_texture:
            mat["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
        gl_materials.append(mat)

    for mesh in model.meshes:
        if not mesh.triangles:
            continue
        # split the mesh by material into primitives; re-index vertices per
        # primitive after wrapping UVs per triangle
        by_mat: dict[int, list[tuple[int, int, int]]] = {}
        for i0, i1, i2, mat in mesh.triangles:
            by_mat.setdefault(mat, []).append((i0, i1, i2))
        prims = []
        for mat, tris in by_mat.items():
            lm = model.materials[mat]
            scale = 2 if lm.true_color else 1
            lo_u, hi_u = lm.umin * scale, lm.umax * scale
            lo_v, hi_v = lm.vmin * scale, lm.vmax * scale
            verts: dict[tuple, int] = {}
            P, N, T, C, J, Wt, idx = [], [], [], [], [], [], []
            for tri in tris:
                us = [mesh.uvs[i][0] * SW for i in tri]
                vs = [mesh.uvs[i][1] * SH for i in tri]
                if lm.has_texture:
                    us = _wrap_tri(us, lo_u, hi_u)
                    vs = _wrap_tri(vs, lo_v, hi_v)
                for k, i in enumerate(tri):
                    key = (i, round(us[k], 3), round(vs[k], 3))
                    if key not in verts:
                        verts[key] = len(P)
                        P.append(mesh.positions[i])
                        N.append(mesh.normals[i])
                        T.append((us[k] / W, vs[k] / H))
                        C.append(mesh.colors[i])
                        J.append(mesh.joints[i])
                        Wt.append(mesh.weights[i])
                    idx.append(verts[key])
            n = len(P)
            mn = [min(p[k] for p in P) for k in range(3)]
            mx = [max(p[k] for p in P) for k in range(3)]
            pv = add_view(b"".join(struct.pack("<3f", *p) for p in P), 34962)
            attrs = {"POSITION": add_accessor(pv, 5126, n, "VEC3", mn, mx)}
            if any(any(x) for x in N):
                nn = []
                for x, y, z in N:
                    l = math.sqrt(x * x + y * y + z * z) or 1.0
                    nn.append((x / l, y / l, z / l))
                nv = add_view(b"".join(struct.pack("<3f", *p) for p in nn), 34962)
                attrs["NORMAL"] = add_accessor(nv, 5126, n, "VEC3")
            tv = add_view(b"".join(struct.pack("<2f", *t) for t in T), 34962)
            attrs["TEXCOORD_0"] = add_accessor(tv, 5126, n, "VEC2")
            cv = add_view(b"".join(struct.pack("<4f", *c) for c in C), 34962)
            attrs["COLOR_0"] = add_accessor(cv, 5126, n, "VEC4")
            if has_skin:
                jv = add_view(b"".join(struct.pack("<4H", *j) for j in J), 34962)
                attrs["JOINTS_0"] = add_accessor(jv, 5123, n, "VEC4")
                wn = []
                for w in Wt:
                    s = sum(w)
                    wn.append(tuple(x / s for x in w) if s > 0 else (1.0, 0.0, 0.0, 0.0))
                wv = add_view(b"".join(struct.pack("<4f", *w) for w in wn), 34962)
                attrs["WEIGHTS_0"] = add_accessor(wv, 5126, n, "VEC4")
            iv = add_view(b"".join(struct.pack("<I", i) for i in idx), 34963)
            ia = add_accessor(iv, 5125, len(idx), "SCALAR")
            prims.append({"attributes": attrs, "indices": ia, "material": mat, "mode": 4})
        gl_meshes.append({"name": f"{mesh.index:02d}_{mesh.group or mesh.bone}",
                          "primitives": prims})
        node = {"name": f"{mesh.index:02d}/{mesh.group}/{mesh.bone}",
                "mesh": len(gl_meshes) - 1}
        if has_skin:
            node["skin"] = 0
        nodes.append(node)
        scene_nodes.append(len(nodes) - 1)

    skins_json: list[dict] = []
    if has_skin and skeleton is not None and skeleton.joint_blocks:
        # the real armature: every .jnt block becomes a node with its local
        # translation + rotation, parented as the file says; the skin's
        # joints are the rotation blocks in order (k-th weight-group id ->
        # k-th rotation block); inverse bind = inverse of the rest-pose
        # world matrix, since vertices are stored in model space
        base = len(nodes)
        worlds = skeleton.world_matrices()
        jblocks = skeleton.joint_blocks
        ids_sorted = sorted(j for j in set(model.joint_ids) if j not in model.companion_joints)
        id_to_block = {jid: jblocks[k] for k, jid in enumerate(ids_sorted) if k < len(jblocks)}
        block_to_id = {b: j for j, b in id_to_block.items()}
        kind = {1: "node", 2: "joint", 3: "offset", 4: "end", 6: "name"}
        for i, t in enumerate(skeleton.types):
            m = skeleton.local_matrix(i)
            q = _quat_from_matrix(m)
            label = (f"j{block_to_id[i]}" if i in block_to_id
                     else f"{kind.get(t, 'blk')}{i}")
            nodes.append({"name": label,
                          "translation": [float(m[0][3]), float(m[1][3]), float(m[2][3])],
                          "rotation": [float(x) for x in q]})
        roots = []
        for i, p in enumerate(skeleton.parents):
            if p is None:
                roots.append(base + i)
            else:
                nodes[base + p].setdefault("children", []).append(base + i)
        # companion skeletons (the face .jnt): their joints are already in
        # body space, so they hang off the same root as separate trees
        node_of_joint = {jid: base + blk for jid, blk in id_to_block.items()}
        world_of_joint = {jid: worlds[blk] for jid, blk in id_to_block.items()}
        for prefix, csk in model.companion_skeletons.items():
            cbase = len(nodes)
            cworlds = csk.world_matrices()
            cids = sorted(j for j, pf in model.companion_joints.items() if pf == prefix)
            cjb = csk.joint_blocks
            cmap = {jid: cjb[k] for k, jid in enumerate(cids) if k < len(cjb)}
            cblock_to_id = {b: j for j, b in cmap.items()}
            for i, t in enumerate(csk.types):
                q = _quat_from_matrix(csk.local_matrix(i))
                label = (f"{prefix}j{cblock_to_id[i] % 100000}" if i in cblock_to_id
                         else f"{prefix}{kind.get(t, 'blk')}{i}")
                cm = csk.local_matrix(i)
                nodes.append({"name": label,
                              "translation": [float(cm[0][3]), float(cm[1][3]), float(cm[2][3])],
                              "rotation": [float(x) for x in q]})
            for i, pp in enumerate(csk.parents):
                if pp is None:
                    roots.append(cbase + i)
                else:
                    nodes[cbase + pp].setdefault("children", []).append(cbase + i)
            for jid, blk in cmap.items():
                node_of_joint[jid] = cbase + blk
                world_of_joint[jid] = cworlds[blk]
        root = len(nodes)
        nodes.append({"name": "skeleton", "children": roots})
        scene_nodes.append(root)
        # glTF joint order must follow model.joint_ids (the JOINTS_0 index)
        joint_nodes = []
        ibm = bytearray()
        for jid in model.joint_ids:
            nd = node_of_joint.get(jid)
            if nd is None:
                joint_nodes.append(root); ibm += struct.pack("<16f", *(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1))
                continue
            joint_nodes.append(nd)
            inv = _invert_rigid(world_of_joint[jid])
            ibm += struct.pack("<16f", *[inv[r][c] for c in range(4) for r in range(4)])  # column-major
        ibv = add_view(bytes(ibm))
        iba = add_accessor(ibv, 5126, len(joint_nodes), "MAT4")
        skins_json.append({"name": skeleton.name or "skin", "skeleton": root,
                           "joints": joint_nodes, "inverseBindMatrices": iba})
    elif has_skin:
        root = len(nodes)
        nodes.append({"name": "skeleton", "children": []})
        scene_nodes.append(root)
        joint_nodes = []
        for jid in model.joint_ids:
            nodes.append({"name": f"j{jid}"})
            joint_nodes.append(len(nodes) - 1)
        nodes[root]["children"] = joint_nodes
        ident = struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)
        ibv = add_view(ident * len(joint_nodes))
        iba = add_accessor(ibv, 5126, len(joint_nodes), "MAT4")
        skins_json.append({"name": "skin", "skeleton": root, "joints": joint_nodes,
                           "inverseBindMatrices": iba})

    warnings = list(model.warnings)
    if any(m.uv_type == 0x7F for m in model.materials):
        warnings.append("eyelash material(s) reference a 4bpp sheet the "
                        "texture decoder does not read yet; the lashes "
                        "sample whatever the atlas PNG shows there")
    gltf = {
        "asset": {"version": "2.0",
                  "generator": "Xenosaga1PythonExtractor lex.py"},
        "scene": 0,
        "scenes": [{"name": name or model.name.lstrip("/"), "nodes": scene_nodes}],
        "nodes": nodes,
        "meshes": gl_meshes,
        "materials": gl_materials,
        "buffers": [{"byteLength": len(bin_chunk)}],
        "bufferViews": views,
        "accessors": accessors,
        "extras": {"source": model.name, "artist": model.artist,
                   "weight_group_ids": model.joint_ids,
                   "warnings": warnings},
    }
    if images_json:
        gltf["images"], gltf["samplers"], gltf["textures"] = images_json, samplers_json, textures_json
    if skins_json:
        gltf["skins"] = skins_json
    js = json.dumps(gltf, separators=(",", ":")).encode()
    while len(js) % 4:
        js += b" "
    while len(bin_chunk) % 4:
        bin_chunk.append(0)
    total = 12 + 8 + len(js) + 8 + len(bin_chunk)
    return (struct.pack("<4sII", b"glTF", 2, total)
            + struct.pack("<I4s", len(js), b"JSON") + js
            + struct.pack("<I4s", len(bin_chunk), b"BIN\0") + bytes(bin_chunk))
