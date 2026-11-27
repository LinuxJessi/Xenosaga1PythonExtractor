"""Episode I motion: ``FPK`` packs of ``FCV`` clips -> poses and glTF animations.

The format is read the way the game reads it.  The executable keeps its
symbols, and the functions below are the ones this module follows (USA
SLUS_204.69; the names are the executable's own):

* ``FCV2_resetPack`` (0x30d908): the clip header.  ``FCV\\0``, u16 format,
  u16 frame count, u16 first frame, u16 last frame, s16 frame offset; from
  format 2 an attribute stream follows at +0x10 (u32 byte size, then records
  of u16 size, u16 type) and the key stream starts after it.  A key's u16
  time is a fraction of the clip's length: from format 2, seconds = u16 *
  frame count / 65535 / 30; below format 2, u16 * (last - first frame) /
  65535 / 30 and no frame offset.
* ``FCV2_getPackAttribute``: each skeleton block's part of the key stream
  starts with a type-4 record: u16 flags, u16 value mask, and in the 16-byte
  form three block indices (translation link, pivot, rotation link).  Any
  other record type ends the key stream (the routine returns ``defaultAttr``,
  flags 0xFFFF, without moving on).
* ``FCV2_getPackValue`` / ``FCV2_getPackVal``: one curve, read each time
  ``get_fcvpack`` / ``get_fcvpack_fix`` / ``get_fcvpack_flag`` (called from
  ``JNT_getVal``) finds a mask bit set.  u16 key count, u16 flags whose low
  nibble is the kind (0 = nothing stored, 1 = one float, 3 = keys; the upper
  bits, 0x8000 on many disc curves, are not the kind); a key is u16 type,
  u16 time, f32 value, and one tangent (type 4) or two (type 1).  Types 1,
  2 and 4 are cubic Hermite, 0 linear, 3 a step.
* ``JNT_computeMatrix`` (0x314a38), the routine ``ACT_resetAnimation``
  registers for every actor, which samples each body block with
  ``JNT_getVal`` (0x30f038) and builds its matrix with
  ``JNT_constructMatrix`` (0x30f938): how the values become matrices.  The
  value mask's bits 0-2 are translation, 3-5 rotation (about Z, Y, X), 6-8
  scale, and 9 marks the root's extra flag value (the game reads it when
  bit 7 is set; every disc record sets bits 7 and 9 together).  Which of
  them a block reads depends on the ``.jnt`` block type: 1 (and 0 among
  the body blocks) a node that may take its position or rotation from
  another block, 5 a node that does not, and 3 / 2 / 4 the root, links and
  end of a chain.  A chain whose end names a target block is solved
  toward it (two bones by the law of cosines, one bone by aiming); the legs
  of the party members are driven this way, two solved chains a frame.
* ``JNT_getStaticVal`` (body blocks) / ``JNT_getStaticVal2`` (the blocks
  after them): a block the clip does not reach keeps its ``.jnt``
  translation and rotation.

Every clip on the disc is laid out with one curve per set mask bit (a
root record: bits 0-8, then the flag when bit 7 is set, which is how the
game reads it), so every clip can be read without knowing its skeleton.
That is a property of the disc's clips, checked by ``fits``: the game itself
reads only the bits ``JNT_getVal`` consults for the block's type.  Posing a
clip needs the ``.jnt`` it was made for.  The game does not compare names:
``FCV2_readAttribute`` and ``JNT_readAttribute`` each turn the 8-byte name
field into a key, u32 word 0 * 31 + u32 word 1 (0 when there is no name),
and ``JNT_computeMatrix`` takes the ``JNT_getVal`` path when the two keys are
equal.  The kit pairs clips with skeletons by that key (``attribute_key``);
on the USA disc that gives exactly the pairs comparing names would.

* ``EXM_CalcExMotion`` (the hair and cloth blocks from ``JNT_hairID`` to
  primary + secondary): simulated a game tick at a time when a clip is baked
  (``simulate``); ``pose`` alone leaves them at rest.  Wind and the hip
  sway are not followed.

Not followed: ``JNT_computeHair`` (only hair_test.c uses it),
``JNT_interpolate`` (blending between two clips), and the move back onto
its parent that ``JNT_constructMatrix`` applies to the root block when the
actor's channel mask has its translation bits clear (``& 7 == 0``, travel
masked off; characters start with 0x1FF, so it does not happen there).
Playback has not been compared with the running game.
"""
from __future__ import annotations

import json
import math
import struct
from dataclasses import dataclass, field
from typing import Optional

FPS = 30.0
# FCV2_setStep(slot->speed) keeps |speed| / 2.  The speed starts at 1/30 and
# a type-2 range with bit 7 set would change it; no range on the disc does.
STEP = 1.0 / 60.0


class FcvError(ValueError):
    """Raised for packs, clips or skeletons this module cannot read."""


# ---------------------------------------------------------------------------
# containers
# ---------------------------------------------------------------------------

@dataclass
class Curve:
    """One value of one block.  ``kind``: 0 nothing stored (reads as 0.0),
    1 a constant, 3 keys.  A key is (type, time u16, value, tangent a,
    tangent b); ``flags`` is the whole u16 the kind came from."""
    kind: int
    flags: int
    value: float = 0.0
    keys: list = field(default_factory=list)


@dataclass
class Channel:
    """A block's record in a clip: the attribute and its curves by mask bit
    (0-2 translation, 3-5 rotation, 6-8 scale).  A root (inherit, 0x8000)
    record's flag value is kept as curve 9: the game reads it when mask bit
    7 is set, whatever bit 9 says."""
    flags: int
    mask: int
    translation_link: int
    pivot_link: int
    rotation_link: int
    curves: dict


@dataclass
class Clip:
    offset: int
    size: int
    format: int
    frame_count: int
    first_frame: int
    last_frame: int
    frame_offset: int
    name: str                  # the skeleton it was made for ("" = unnamed)
    ranges: list               # type-2 records: (selector, speed/32, first, last)
    channels: list
    end_marker: bool
    # FCV2_readAttribute's key for the name field: u32 word 0 * 31 + u32
    # word 1 of the last type-1 record, 0 when there is none
    attribute_key: int = 0

    @property
    def frames(self) -> int:
        return self.last_frame - self.first_frame + 1

    @property
    def duration(self) -> float:
        return (self.last_frame - self.first_frame) / FPS

    def key_count(self) -> int:
        return sum(len(c.keys) for ch in self.channels for c in ch.curves.values())


def parse_fpk(data: bytes) -> list[int]:
    """Clip offsets of an ``FPK`` pack, one per slot (slots can share a clip)."""
    if data[:3] != b"FPK" or len(data) < 0x10:
        raise FcvError("not an FPK pack")
    count = struct.unpack_from("<I", data, 8)[0]
    if count > 4096 or 0x10 + 4 * count > len(data):
        raise FcvError("FPK slot table beyond the file")
    offs = list(struct.unpack_from("<%dI" % count, data, 0x10))
    for o in offs:
        if o + 0x10 > len(data):
            raise FcvError("FPK slot points beyond the file")
    return offs


def _curve(data: bytes, p: int, end: int) -> tuple[Curve, int]:
    if p + 4 > end:
        raise FcvError("curve header beyond the clip")
    n, flags = struct.unpack_from("<HH", data, p)
    kind = flags & 0xF
    if kind == 1:
        if p + 8 > end:
            raise FcvError("constant beyond the clip")
        return Curve(1, flags, struct.unpack_from("<f", data, p + 4)[0]), p + 8
    if kind == 3:
        keys = []
        p += 4
        for _ in range(n):
            if p + 8 > end:
                raise FcvError("key beyond the clip")
            kt, kf = struct.unpack_from("<HH", data, p)
            if kt in (0, 2, 3):
                keys.append((kt, kf, struct.unpack_from("<f", data, p + 4)[0], 0.0, 0.0))
                p += 8
            elif kt == 4:
                if p + 12 > end:
                    raise FcvError("key beyond the clip")
                v, a = struct.unpack_from("<2f", data, p + 4)
                keys.append((kt, kf, v, a, a))
                p += 12
            elif kt == 1:
                if p + 16 > end:
                    raise FcvError("key beyond the clip")
                v, a, b = struct.unpack_from("<3f", data, p + 4)
                keys.append((kt, kf, v, a, b))
                p += 16
            else:
                raise FcvError("unknown key type %d at 0x%x" % (kt, p))
        if not keys:
            raise FcvError("keyed curve with no keys at 0x%x" % p)
        return Curve(3, flags, keys[0][2], keys), p
    return Curve(0, flags), p + 4


def _attribute_key(raw: bytes) -> int:
    """FCV2_readAttribute / JNT_readAttribute: words[0] * 31 + words[1]."""
    w0, w1 = struct.unpack("<II", raw[:8].ljust(8, b"\0"))
    return (w0 * 31 + w1) & 0xFFFFFFFF


def parse_clip(data: bytes, offset: int, end: Optional[int] = None,
               strict: bool = False) -> Clip:
    """Read the clip at ``offset``.  ``end`` is where the next clip (or the
    end of the pack) starts; the read fails when it runs past it.  The key
    stream ends where the game's does: at a 0xFFFF record, or at a record
    that is not type 4 (``FCV2_getPackAttribute`` then returns
    ``defaultAttr``), or when the clip's room runs out.  Bytes left after an
    end like that are allowed unless ``strict`` (the sweep uses it to check
    that every disc clip is read to exactly its last byte)."""
    if end is None:
        end = len(data)
    if data[offset:offset + 4] != b"FCV\0":
        raise FcvError("no FCV clip at 0x%x" % offset)
    if offset + 0x10 > end:
        raise FcvError("FCV header beyond the clip")
    fmt, count, first, last, foff = struct.unpack_from("<HHHHh", data, offset + 4)
    name, ranges, akey = "", [], 0
    if fmt >= 2:
        if offset + 0x14 > end:
            raise FcvError("attribute stream beyond the clip")
        size = struct.unpack_from("<I", data, offset + 0x10)[0]
        if size < 4 or offset + 0x10 + size > end:
            raise FcvError("attribute stream beyond the clip")
        p, stop = offset + 0x14, offset + 0x10 + size
        while p + 4 <= stop:
            rsize, rtype = struct.unpack_from("<HH", data, p)
            if rtype == 0 or rsize < 4 or p + rsize > stop:
                break
            body = data[p + 4:p + rsize]
            if rtype == 1:
                name = body[:8].split(b"\0")[0].decode("ascii", "replace")
                akey = _attribute_key(body[:8])
            elif rtype == 2 and len(body) >= 6:
                sel, speed, f0, f1 = struct.unpack_from("<BbHH", body, 0)
                ranges.append((sel, speed / 32.0, f0, f1))
            p += rsize
        p = stop
    else:
        p = offset + 0x10
    channels, end_marker, stopped = [], False, False
    while p + 8 <= end:
        rsize, rtype = struct.unpack_from("<HH", data, p)
        if rtype != 4:
            stopped = True                   # defaultAttr: the stream ends here
            break
        nxt = p + 4 + rsize
        if nxt > end or (rsize >= 10 and p + 14 > end):
            raise FcvError("clip at 0x%x: a block record runs past the clip (0x%x)"
                           % (offset, p))
        flags, mask = struct.unpack_from("<HH", data, p + 4)
        if flags == 0xFFFF:
            end_marker = stopped = True
            p = nxt
            break
        tl = pl = rl = 0
        if rsize >= 10:
            tl, pl, rl = struct.unpack_from("<HHH", data, p + 8)
        curves = {}
        p = nxt
        if flags & 0x8000:
            # JNT_getVal / JNT_getVal2: translation (bits 0-2), rotation (3-5),
            # scale (6-8), then get_fcvpack_flag on the mask shifted right by
            # 7: one more value when bit 7 is set (kept as curve 9)
            for bit in range(9):
                if mask & (1 << bit):
                    curves[bit], p = _curve(data, p, end)
            if mask & 0x80:
                curves[9], p = _curve(data, p, end)
        else:
            for bit in range(16):
                if mask & (1 << bit):
                    curves[bit], p = _curve(data, p, end)
        channels.append(Channel(flags, mask, tl, pl, rl, curves))
    if p > end or (p != end and (strict or not stopped)):
        raise FcvError("clip at 0x%x: the key stream stops at 0x%x, the clip at 0x%x"
                       % (offset, p, end))
    return Clip(offset, end - offset, fmt, count, first, last, foff, name, ranges,
                channels, end_marker, akey)


def read_pack(data: bytes, strict: bool = False) -> list[tuple[list[int], Clip]]:
    """Every clip of a pack as (slots that point at it, clip), in file order."""
    offs = parse_fpk(data)
    order = sorted(set(offs))
    out = []
    for k, o in enumerate(order):
        end = order[k + 1] if k + 1 < len(order) else len(data)
        out.append(([s for s, x in enumerate(offs) if x == o],
                    parse_clip(data, o, end, strict)))
    return out


# ---------------------------------------------------------------------------
# the skeleton as the motion code sees it
# ---------------------------------------------------------------------------

@dataclass
class Rig:
    """A ``.jnt`` read for posing.  The block ranges, as ``JNT_computeMatrix``
    walks them: ``[0, hair)`` the body, read from the clip with ``JNT_getVal``;
    ``[hair, primary + secondary)`` hair and cloth, simulated while the game
    runs (``EXM_CalcExMotion``, which reads no record); then ``aprim``
    (additional_primary_count) blocks through ``JNT_getVal2``, which go on
    reading the clip from the record after the body's; then static blocks
    (``JNT_getStaticVal2``) up to the table's end.  ``extra`` is
    additional_primary + additional_secondary.  ``hair`` is ``JNT_hairID``:
    ``primary``, plus ``secondary`` when the ``.jnt`` has no type-5 attribute
    record (the tag's word count is then 0), plus the tag's low half when
    its kind is 0x8A."""
    name: str
    primary: int
    secondary: int
    extra: int
    types: list
    parents: list              # the block whose matrix this one starts from
    translations: list
    rotations: list
    directions: list           # +0x38: its sign picks a chain link's bend
    limits: list               # None or (min3, flags, max3)
    deferred: list             # bool: the block has a shape-binding record
    hair: int = -1             # JNT_hairID; -1 = primary (rigs built by hand)
    aprim: int = -1            # additional_primary_count; -1 = extra
    attribute_key: int = 0     # JNT_readAttribute's key for the name (0 = none)


def parse_rig(data: bytes) -> Rig:
    if data[:4] != b"JNT\0" or len(data) < 0x20:
        raise FcvError("not a jnt file")
    version, limit, prim, sec, aprim, asec, asize = struct.unpack_from("<6Hi", data, 4)
    if version >= 2 and asize < 4:
        raise FcvError("jnt attribute size %d is too small" % asize)
    base = 0x10 + asize if version >= 2 else 0x10
    name, akey = "", 0
    words, tag = 0, 0                    # JNT_readAttribute starts from dummyCntFree
    if version >= 2:
        # JNT_readAttribute: every record up to type 0 or the attribute size;
        # the last type-1 record names the skeleton, a type-5 record is the tag
        off = 4
        while 0x10 + off + 4 <= len(data):
            rsize, rtype = struct.unpack_from("<HH", data, 0x10 + off)
            if rtype == 1 and 0x10 + off + 12 <= len(data):
                name = data[0x14 + off:0x1C + off].split(b"\0")[0].decode("ascii", "replace")
                akey = _attribute_key(data[0x14 + off:0x1C + off])
            elif rtype == 5 and 0x10 + off + 12 <= len(data):
                words, tag = struct.unpack_from("<ii", data, 0x14 + off)
            if rtype == 0 or rsize == 0:
                break
            off += rsize
            if off >= asize:
                break
    if base + 64 * limit > len(data):
        raise FcvError("jnt block table beyond the file")
    hair = prim + (sec if words == 0 else 0)
    if (tag & 0xFFFF0000) == 0x008A0000:
        hair += tag & 0xFFFF
    rig = Rig(name, prim, sec, aprim + asec, [], [], [], [], [], [], [],
              min(hair, limit), aprim, akey)
    for i in range(limit):
        o = base + 64 * i
        t, _fl, lo = struct.unpack_from("<HHi", data, o)
        rig.types.append(t)
        rig.parents.append(struct.unpack_from("<H", data, o + 0x0E)[0])
        rig.translations.append(struct.unpack_from("<3f", data, o + 0x10))
        rig.rotations.append(struct.unpack_from("<3f", data, o + 0x20))
        rig.directions.append(struct.unpack_from("<f", data, o + 0x38)[0])
        lim, deferred = None, False
        if lo and 0 < o + lo and o + lo + 4 <= len(data):
            h = o + lo
            vo, do = struct.unpack_from("<HH", data, h)
            deferred = bool(do)
            if vo and h + vo + 28 <= len(data):
                mn = struct.unpack_from("<3f", data, h + vo)
                fl = struct.unpack_from("<H", data, h + vo + 12)[0]
                mx = struct.unpack_from("<3f", data, h + vo + 16)
                lim = (mn, fl, mx)
        rig.limits.append(lim)
        rig.deferred.append(deferred)
    return rig


# the mask bits each block type reads: JNT_getVal (body blocks) reads type 0
# like type 1, JNT_getVal2 (the blocks after hair and cloth) reads nothing
# for it; the other types read the same in both.  A root (inherit) record
# reads bits 0-8 and the flag on bit 7; bit 9, where the disc's clips store
# that flag, is allowed and read by nothing.
_READS = {0: 0x1FF, 1: 0x1FF, 5: 0x1FF, 3: 0x3F, 2: 0x1F8, 4: 0, 6: 0}
_READS_ROOT = 0x3FF


def _ranges(rig: Rig) -> tuple[int, int, int]:
    """(body end, getVal2 start, getVal2 end) as JNT_computeMatrix walks the
    blocks on its normal (EXM_CalcExMotion) path."""
    n = len(rig.types)
    body = min(rig.hair if rig.hair >= 0 else rig.primary, n)
    g2 = min(max(body, rig.primary + rig.secondary), n)
    aprim = rig.aprim if rig.aprim >= 0 else rig.extra
    return body, g2, min(max(g2, rig.primary + rig.secondary + aprim), n)


def _record_fits(typ: int, ch: Channel, in_body: bool) -> bool:
    reads = _READS.get(typ)
    if reads is None:
        return False
    if typ == 0 and not in_body:
        reads = 0
    if ch.flags & 0x8000:
        reads = _READS_ROOT
    return not ch.mask & ~reads


def fits(rig: Rig, clip: Clip) -> bool:
    """True when the clip's records line up with the skeleton's body blocks:
    no more records than blocks, and no body record stores a value
    ``JNT_getVal`` would not read for that block's type (it would read the
    next block's record from the wrong place).  A record may store less:
    what it leaves out comes from the ``.jnt`` (scale: 1).  Records after
    the body are read by ``JNT_getVal2`` from the record after the body's
    (see ``pose``), which stops at the first that does not line up; they do
    not decide whether the clip fits.  A clip with no records at all is the
    rest pose (``JNT_getStaticVal`` everywhere), and fits."""
    if len(clip.channels) > len(rig.types):
        return False
    body = _ranges(rig)[0]
    for i, ch in enumerate(clip.channels[:body]):
        if not _record_fits(rig.types[i], ch, True):
            return False
    return True


# ---------------------------------------------------------------------------
# curves
# ---------------------------------------------------------------------------

def eval_curve(curve: Curve, scale: float, t: float) -> float:
    """``FCV2_getPackVal`` at ``t`` seconds (the frame offset already added)."""
    if curve.kind == 1:
        return curve.value
    if curve.kind != 3:
        return 0.0
    keys = curve.keys
    first = keys[0][1] * scale
    if t <= first:
        return keys[0][2]
    near_prev = near_next = None
    near = first
    prev = keys[0]
    for nxt in keys[1:]:
        dist = t - nxt[1] * scale
        if dist < STEP:
            near = abs(dist)
            near_prev, near_next = prev, nxt
            break
        prev = nxt
    if near_prev is None:
        return keys[-1][2]
    if near < STEP * 0.5:
        return near_next[2]
    pt, pf, pv = near_prev[0], near_prev[1] * scale, near_prev[2]
    # the game reads no value from a step key that ends a segment
    nf, nv = near_next[1] * scale, (0.0 if near_next[0] == 3 else near_next[2])
    if pt == 3:
        return pv
    if nf <= pf:            # two keys at one time (a damaged clip): nothing to blend
        return near_next[2]
    if pt == 0:
        return pv + (nv - pv) * (t - pf) / (nf - pf)
    tan_in = near_next[3] if near_next[0] in (1, 4) else 0.0
    tan_out = near_prev[4] if pt == 1 else (near_prev[3] if pt == 4 else 0.0)
    inv = 1.0 / (nf - pf)
    x = t - pf
    x2 = x * x
    x3 = x2 * x
    a = x2 * inv
    b = x3 * inv * inv
    c = 3.0 * x2 * inv * inv
    e = 2.0 * b * inv
    return (pv * (e - c + 1.0) + nv * (c - e)
            + tan_out * (b - a - a + x) + tan_in * (b - a))


# ---------------------------------------------------------------------------
# matrices: rotation (9 floats, row-major, column vectors), scale, position
# ---------------------------------------------------------------------------

_I3 = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)


def _mm(a, b):
    return (a[0] * b[0] + a[1] * b[3] + a[2] * b[6],
            a[0] * b[1] + a[1] * b[4] + a[2] * b[7],
            a[0] * b[2] + a[1] * b[5] + a[2] * b[8],
            a[3] * b[0] + a[4] * b[3] + a[5] * b[6],
            a[3] * b[1] + a[4] * b[4] + a[5] * b[7],
            a[3] * b[2] + a[4] * b[5] + a[5] * b[8],
            a[6] * b[0] + a[7] * b[3] + a[8] * b[6],
            a[6] * b[1] + a[7] * b[4] + a[8] * b[7],
            a[6] * b[2] + a[7] * b[5] + a[8] * b[8])


def _mv(a, v):
    return (a[0] * v[0] + a[1] * v[1] + a[2] * v[2],
            a[3] * v[0] + a[4] * v[1] + a[5] * v[2],
            a[6] * v[0] + a[7] * v[1] + a[8] * v[2])


def _mtv(a, v):
    """transpose(a) * v"""
    return (a[0] * v[0] + a[3] * v[1] + a[6] * v[2],
            a[1] * v[0] + a[4] * v[1] + a[7] * v[2],
            a[2] * v[0] + a[5] * v[1] + a[8] * v[2])


def _rx(a):
    c, s = math.cos(a), math.sin(a)
    return (1.0, 0.0, 0.0, 0.0, c, -s, 0.0, s, c)


def _ry(a):
    c, s = math.cos(a), math.sin(a)
    return (c, 0.0, s, 0.0, 1.0, 0.0, -s, 0.0, c)


def _rz(a):
    c, s = math.cos(a), math.sin(a)
    return (c, -s, 0.0, s, c, 0.0, 0.0, 0.0, 1.0)


def _rzyx(r):
    """CUR_MATRIX_Rzyx4s: the triple is (about Z, about Y, about X)."""
    return _mm(_mm(_rz(r[0]), _ry(r[1])), _rx(r[2]))


def _translate(m, v):
    """CUR_MATRIX_Txyz4s: move along the matrix's own scaled axes."""
    r, s, t = m
    d = _mv(r, (s[0] * v[0], s[1] * v[1], s[2] * v[2]))
    return (r, s, (t[0] + d[0], t[1] + d[1], t[2] + d[2]))


def _rotate(m, rot):
    return (_mm(m[0], rot), m[1], m[2])


def _scale(m, v):
    s = m[1]
    return (m[0], (s[0] * v[0], s[1] * v[1], s[2] * v[2]), m[2])


def _align_x(d):
    """alignXAxis + MATRIX_transpose3: the rotation (about Z, then Y) that
    turns the X axis onto ``d``."""
    x, y, z = d
    rxy = math.hypot(x, y)
    ln = math.sqrt(x * x + y * y + z * z)
    if ln == 0.0:
        return _I3
    if rxy == 0.0:
        a, b = math.atan2(z / ln, 0.0), 0.0
    else:
        a, b = math.atan2(z / ln, rxy / ln), math.atan2(-y / rxy, x / rxy)
    m = _mm(_ry(a), _rz(b))
    return (m[0], m[3], m[6], m[1], m[4], m[7], m[2], m[5], m[8])


_IDENT = (_I3, (1.0, 1.0, 1.0), (0.0, 0.0, 0.0))


def _bend(root_len, upper, lower):
    """The angle at the chain root for a two-bone reach of ``root_len``."""
    a = abs(upper)
    den = (a + a) * root_len
    cos = (root_len * root_len + a * a - lower * lower) / den if den else 1.0
    sin = math.sqrt(max(0.0, 1.0 - cos * cos))
    y = sin if upper >= 0.0 else -sin
    if cos > 1.0:
        cos, y = 1.0, 0.0
    elif cos < -1.0:
        cos, y = -1.0, 0.0
    return math.atan2(y, cos)


def _record_map(rig: Rig, clip: Optional[Clip]) -> dict:
    """{block: record index} for the blocks that take a clip record, as
    ``JNT_computeMatrix`` hands them out on its normal path: the body blocks
    their own record; the ``JNT_getVal2`` blocks the records after the
    body's, in order, up to the first that does not line up with the block."""
    if clip is None:
        return {}
    chs = clip.channels
    body, g2, g2end = _ranges(rig)
    out = {i: i for i in range(min(body, len(chs)))}
    if len(chs) >= body:
        for k, i in enumerate(range(g2, g2end)):
            j = body + k
            if j >= len(chs) or not _record_fits(rig.types[i], chs[j], False):
                break
            out[i] = j
    return out


def pose(rig: Rig, clip: Optional[Clip], frame: float, stats: Optional[dict] = None,
         driven_only: bool = False) -> list:
    """World matrices of every block at ``frame`` (a frame number, 1/30 s;
    ``clip.first_frame`` is the clip's start), as (rotation 9-tuple, scale,
    position) or None for a block that has no matrix.  ``clip`` None gives
    the rest pose.  ``driven_only`` stops after the last block that takes a
    record (the blocks past it only repeat their ``.jnt`` values under moving
    parents).

    The order is ``JNT_computeMatrix``'s.  Body blocks (below ``rig.hair``)
    are sampled the way ``JNT_getVal`` does, or take their ``.jnt`` values
    (``JNT_getStaticVal``) past the clip's last record, and are built the way
    ``JNT_constructMatrix`` does.  Hair and cloth blocks keep their rest pose
    (the game simulates them, see ``simulate``) and read no record.  The next
    ``additional_primary_count`` blocks follow ``JNT_getVal2`` with the
    records after the body's, in order (the game's cursor does not move over
    the hair records), up to the first record whose values that block's type
    would not read (the game reads on out of step there; the kit stops); the
    blocks after them are static (``JNT_getStaticVal2``)."""
    n = len(rig.types)
    world: list = [None] * n
    channels = clip.channels if clip is not None else []
    body, simulated, g2end = _ranges(rig)
    rec_of = _record_map(rig, clip)
    last_driven = max(rec_of) if rec_of else -1
    one = (1.0, 1.0, 1.0)
    # JNT_getVal2's work->channel_scale: a type-1 record without the inherit
    # flag writes only the components it stores, and the rest are kept from
    # the block before (in the game, from whatever ran before; 1 here)
    g2_scale = [1.0, 1.0, 1.0]
    if clip is not None:
        scale = (clip.frame_count / 65535.0 / FPS if clip.format >= 2
                 else (clip.last_frame - clip.first_frame) / 65535.0 / FPS)
        t = frame / FPS + (clip.frame_offset / FPS if clip.format >= 2 else 0.0)
    # JNT_computeMatrix sets block 0 to the unit matrix; when a body record
    # carries the inherit flag (0x8000) it builds from block 1 on, so block 0
    # stays the unit matrix whatever its record holds
    skip_root = any(c.flags & 0x8000 for c in channels[:body])

    def start(i):
        p = rig.parents[i]
        if p == i or p >= n or world[p] is None:
            return _IDENT
        return world[p]

    def read(ch, bit, fallback):
        cv = ch.curves
        return [eval_curve(cv[bit + k], scale, t) if (bit + k) in cv else fallback[k]
                for k in range(3)]

    def sample(i, ch):
        """(translation, rotation, scale) as JNT_getVal / JNT_getVal2 leave
        them; ch None is JNT_getStaticVal (the .jnt values, unit scale)."""
        typ, tr, ro = rig.types[i], rig.translations[i], rig.rotations[i]
        if ch is None:
            return list(tr), list(ro), list(one)
        if ch.flags & 0x8000:
            ct, cr, cs = read(ch, 0, tr), read(ch, 3, ro), read(ch, 6, one)
            if typ not in (0, 1):
                # the rest of the routine runs with an empty mask
                ct = list(tr)
                if typ in (2, 5):
                    cr, cs = list(ro), list(one)
                elif typ == 3:
                    cr = list(ro)
        elif typ in (0, 1, 5):
            ct, cr, cs = read(ch, 0, tr), read(ch, 3, ro), read(ch, 6, one)
        elif typ == 3:
            ct, cr, cs = read(ch, 0, tr), read(ch, 3, ro), list(one)
        elif typ == 2:
            ct, cr, cs = list(tr), read(ch, 3, ro), read(ch, 6, one)
        else:
            ct, cr, cs = list(tr), list(ro), list(one)
        if typ == 2 and rig.limits[i]:
            mn, fl, mx = rig.limits[i]
            for k in range(3):
                if (fl >> k) & 1 and cr[k] < mn[k]:
                    cr[k] = mn[k]
                if (fl >> (k + 3)) & 1 and cr[k] > mx[k]:
                    cr[k] = mx[k]
        return ct, cr, cs

    sampled: list = [None] * n           # JNT_getVal's per-block translation
    chain = None
    for i in range(n):
        typ = rig.types[i]
        tr, ro = rig.translations[i], rig.rotations[i]
        is_body = i < body
        has_rec = i in rec_of
        if i > last_driven and driven_only and not (
                is_body and chain is not None and not chain["closed"]):
            break
        if body <= i < simulated or (not has_rec and not is_body) \
                or (is_body and not has_rec and typ == 0):
            # hair and cloth (EXM_CalcExMotion while the game runs), or a block
            # past the clip after them: JNT_getStaticVal2, the .jnt values.
            # A body block of type 0 past the clip: JNT_getStaticVal leaves
            # whatever its element held, so it gets no matrix here.
            if typ in (1, 2, 3, 4, 5):
                world[i] = _rotate(_translate(start(i), tr), _rzyx(ro))
            continue
        ch = channels[rec_of[i]] if has_rec else None
        cf = ch.flags if ch is not None else 0
        ct, cr, cs = sample(i, ch)
        if not is_body and ch is not None:
            if cf & 0x8000:
                g2_scale = [1.0, 1.0, 1.0] if typ == 5 else list(read(ch, 6, one))
            elif typ == 1:
                for k in range(3):
                    if 6 + k in ch.curves:
                        g2_scale[k] = eval_curve(ch.curves[6 + k], scale, t)
                cs = list(g2_scale)
            elif typ == 5:
                g2_scale = list(cs)
        sampled[i] = ct
        if i == 0 and skip_root:
            world[0] = _IDENT
            continue
        if typ == 1 or (typ == 0 and is_body):
            m = start(i)
            if cf & 0xE:
                if cf & 4:
                    if ch.translation_link < i and world[ch.translation_link] is not None:
                        m = (m[0], m[1], world[ch.translation_link][2])
                else:
                    m = _translate(m, ct)
                if cf & 8:
                    # CUR_MATRIX_SetR copies the three axis rows, scale included
                    if ch.rotation_link < i and world[ch.rotation_link] is not None:
                        m = (world[ch.rotation_link][0], world[ch.rotation_link][1], m[2])
                else:
                    m = _scale(_rotate(m, _rzyx(cr)), cs)
            else:
                m = _rotate(_translate(m, ct), _rzyx(cr))
                if is_body:              # JNT_constructMatrix scales; JNT_getVal2 does not
                    m = _scale(m, cs)
            if rig.deferred[i]:
                if not cf & 4:
                    m = (m[0], m[1], tuple(ct))
                elif ch.translation_link < i:
                    # JNT_constructMatrix: the linked block's sampled translation;
                    # JNT_getVal2: this block's own
                    lt = sampled[ch.translation_link] if is_body else ct
                    if lt is not None:
                        m = (m[0], m[1], tuple(lt))
            world[i] = m
        elif typ == 3:
            m = _rotate(_translate(start(i), ct), _rzyx(cr))
            world[i] = m
            chain = {"base": i, "basis": m, "slots": [None], "length": 0.0, "closed": False}
        elif typ == 2:
            if chain is None:
                raise FcvError("block %d: a chain link before any chain root" % i)
            x = ct[0]
            chain["length"] += x
            if rig.directions[i] >= 0.0:
                x = -x
            chain["slots"].append({"t": [x, ct[1], ct[2]], "r": cr, "s": cs, "ch": ch})
        elif typ == 4:
            if chain is None:
                raise FcvError("block %d: a chain end before any chain root" % i)
            slots, base, basis = chain["slots"], chain["base"], chain["basis"]
            chain["closed"] = True
            solved = (cf & 0xE) and (cf & 4) and ch.translation_link < i \
                and world[ch.translation_link] is not None
            if not solved:                                   # forward: each link in turn
                cur = basis
                for k in range(1, len(slots)):
                    s = slots[k]
                    s["t"][0] = abs(s["t"][0])
                    lch = s["ch"]
                    if lch is not None and lch.flags & 8:
                        if lch.rotation_link < i and world[lch.rotation_link] is not None:
                            cur = _translate(cur, s["t"])
                            cur = (world[lch.rotation_link][0], world[lch.rotation_link][1],
                                   cur[2])
                            world[base + k] = cur
                    else:
                        cur = _scale(_rotate(_translate(cur, s["t"]), _rzyx(s["r"])), s["s"])
                        world[base + k] = cur
                world[i] = _translate(cur, ct)
            else:
                target = world[ch.translation_link][2]
                slots.append({"t": [ct[0], ct[1], ct[2]]})
                count = len(slots)
                if stats is not None:
                    stats["solved_%d" % (count - 1)] = stats.get("solved_%d" % (count - 1), 0) + 1
                origin = basis[2]
                d = _mtv(basis[0], (target[0] - origin[0], target[1] - origin[1],
                                    target[2] - origin[2]))
                if count == 4:
                    reach = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
                    basis = _rotate(basis, _align_x(d))
                    first = slots[1]
                    fch = first["ch"]
                    pl = fch.pivot_link if fch is not None else n
                    if fch is not None and fch.flags & 2 and pl < n and world[pl] is not None:
                        pv = world[pl][2]
                        v = _mtv(basis[0], (pv[0] - basis[2][0], pv[1] - basis[2][1],
                                            pv[2] - basis[2][2]))
                        ln = math.hypot(v[1], v[2])
                        ang = -math.atan2(-v[2] / ln, v[1] / ln) if ln else 0.0
                        basis = _scale(_rotate(basis, _rx(ang)), first["s"])
                    elif is_body:
                        # JNT_constructMatrix turns the basis by the first link's
                        # X rotation and scales it (0x310158..0x310168);
                        # JNT_getVal2 does the same and then reloads the unturned
                        # basis, so there the turn has no effect
                        basis = _scale(_rotate(basis, _rx(first["r"][2])), first["s"])
                    upper, lower = slots[2]["t"][0], slots[3]["t"][0]
                    cur = _rotate(basis, _rz(_bend(reach, upper, lower)))
                    world[base + 1] = cur
                    slots[2]["t"][0] = abs(upper)
                    knee = _translate(cur, slots[2]["t"])
                    d = _mtv(knee[0], (target[0] - knee[2][0], target[1] - knee[2][1],
                                       target[2] - knee[2][2]))
                    cur = _rotate(knee, _align_x(d))
                    world[base + 2] = cur
                    world[i] = _translate(cur, slots[3]["t"])
                    # (a chain the clip scales does not land on its target in
                    # the game either, so only unscaled ones are measured)
                    if stats is not None and all(abs(x - 1.0) < 1e-6 for x in basis[1]) \
                            and abs(abs(upper) - abs(lower)) <= reach <= abs(upper) + abs(lower):
                        stats["measured"] = stats.get("measured", 0) + 1
                        stats["worst_miss"] = max(stats.get("worst_miss", 0.0),
                                                  math.dist(world[i][2], target))
                elif count == 3:
                    cur = _rotate(basis, _align_x(d))
                    cur = _scale(_rotate(cur, _rx(slots[1]["r"][2])), slots[1]["s"])
                    world[base + 1] = cur
                    world[base + 2] = _translate(cur, slots[2]["t"])
                else:
                    # JNT_constructMatrix / JNT_getVal2 have a general branch
                    # for longer chains (law-of-cosines bends on the middle
                    # links); it is not ported: no disc clip solves one, and
                    # it has quirks no data could check (it never writes the
                    # first link's matrix and translates by the absolute
                    # block index's element)
                    raise FcvError("block %d: a solved chain of %d links (the general "
                                   "solver for more than two links is not ported)"
                                   % (i, count - 2))
        elif typ == 5:
            m = _translate(start(i), ct)
            world[i] = _scale(_rotate(m, _rzyx(cr)), cs)
        if is_body and cf & 0x8000 and i >= 4 and world[i] is not None \
                and world[i - 1] is not None and world[i - 1][2][1] != 0.0:
            # JNT_constructMatrix: the inherit block's height is multiplied by
            # (the previous block's .jnt Y + block 0's Y) / the previous block's
            # posed Y; block 0 is the unit matrix here
            r, s_, p = world[i]
            k = (rig.translations[i - 1][1] + (world[0][2][1] if world[0] else 0.0)) \
                / world[i - 1][2][1]
            world[i] = (r, s_, (p[0], p[1] * k, p[2]))
    return world


# ---------------------------------------------------------------------------
# hair and cloth: EXM_CalcExMotion
# ---------------------------------------------------------------------------
#
# While the game runs, JNT_computeMatrix hands the blocks [JNT_hairID,
# primary + secondary) to EXM_CalcExMotion (exm_1.c) once a tick, with the
# .jnt's own values (not the clip's).  A type-3 block starts a chain, rigid
# on its parent; the type-2 links and the type-4 end are queued, and the end
# solves the chain: the first link stays rigid on the root, every later
# joint starts from where it was the tick before, falls by the type's
# gravity, is pulled part of the way toward where the rigid chain would put
# it (less the further down the chain), is put back at its link's length
# from the joint above, and is pushed out of the character's collision
# shapes; then every link is turned to point at the next joint.
# EXM_PresetHair picks, from the .jnt name, a hairpara[] type and flags for
# each chain in turn and one collision table.
#
# Followed here: gravity, the pull, the link length, the floor
# (EX_ANIM_FLAG_NO_GROUND off: 0.07 above the ground under the character),
# EXM_CheckExCollision (spheres, capsules), EXM_CheckSkirtCollision (a chain
# of a skirt type, EX_HAIR_FLAG_SKIRT, is kept out of the cone the legs
# sweep instead), AdjustJrCoatWidth (EX_HAIR_FLAG_COAT_WIDTH), the bend of
# EX_HAIR_FLAG_ALT_SHAPES and OrientExMotionJoint.  Not followed: wind (off
# unless a map turns it on) and the hip sway (moved_hair_sway is only set
# by hair_test.c).  Where the game would divide by a leg section of length
# zero (the PS2 gives the largest float, not infinity) the skirt push for
# that section is left out.

def _f32(x):
    return struct.unpack("<f", struct.pack("<f", x))[0]


# hairpara[]: (gravity, length_response, response xyz, chain_response)
_HAIRPARA = [tuple(_f32(v) if not isinstance(v, tuple) else tuple(_f32(u) for u in v)
                   for v in row) for row in (
    (0.02, 1.0, (0.9, 0.9, 0.9), 0.5), (0.0, 1.0, (1.0, 1.0, 1.0), 100.0),
    (0.02, 1.0, (0.9, 0.9, 0.9), 0.5), (0.03, 1.0, (0.7, 0.7, 0.7), 0.6),
    (0.02, 1.0, (0.9, 0.9, 0.9), 0.3), (0.01, 0.9, (0.9, 1.0, 0.9), 0.7),
    (0.01, 0.9, (0.9, 1.0, 0.9), 1.2), (0.01, 0.9, (0.5, 0.5, 0.5), 2.0),
    (0.0, 0.8, (0.5, 0.5, 0.5), 1.0), (0.0, 1.0, (0.8, 0.8, 0.8), 1.7),
    (0.03, 1.0, (0.9, 0.9, 0.9), 2.0), (0.05, 1.0, (0.0, 0.0, 0.0), 0.5),
    (0.05, 1.0, (0.2, 0.2, 0.2), 0.5), (0.04, 0.9, (0.7, 0.7, 0.7), 0.2),
    (0.05, 1.0, (0.5, 0.5, 0.5), 0.1), (0.5, 1.0, (0.9, 0.9, 0.9), 1.0),
    (0.01, 0.8, (0.2, 0.2, 0.2), 1.0), (0.0, 0.9, (0.9, 0.9, 0.9), 0.3),
    (0.05, 0.8, (0.75, 0.75, 0.75), 0.5), (0.0, 0.8, (0.95, 0.95, 0.95), 2.0),
    (0.05, 0.1, (0.0, 0.0, 0.0), 0.5), (0.0, 0.01, (0.0, 0.0, 0.0), 0.5))]

# EX_HAIR_FLAG_* (ExHairTypeSelection.flags)
_ALT, _KIND5, _KIND6, _KIND7, _SKIRT = 0x1, 0x2, 0x8, 0x10, 0x400
_COAT, _SPLIT_A, _SPLIT_B, _LONG = 0x20, 0x80, 0x100, 0x8000
# the skirt profile angle (EX_SKIRT_FLAG_ANGLE_*) and the hem lifts
_A180, _A90, _A270, _A0, _HEM_A, _HEM_B = 0x800, 0x1000, 0x2000, 0x4000, 0x10000, 0x20000
# EX_HAIR_SKIRT_<degrees>
_S0, _S180 = _SKIRT | _A0, _SKIRT | _A180
_S90, _S270 = _SKIRT | _A90, _SKIRT | _A270
_S45, _S315 = _S0 | _A90, _S0 | _A270
_S135, _S225 = _S180 | _A90, _S180 | _A270
# the four corners most skirts use, and with the back pair's hem lifted
_CORNERS = [_S45, _S315, _S135, _S225]
_CORNERS_A = [_S45, _S315, _S135 | _HEM_A, _S225 | _HEM_A]

# the collision tables: (block, kind, (x, y, z, radius) from the block,
# capsule end (x, y, z, radius)); kinds 1 sphere, 2 capsule, 3 keep inside,
# 4 only for EX_HAIR_FLAG_ALT_SHAPES, 5 only with _KIND5, 6 only with
# _KIND6, 7 only with _KIND7
_Z4 = (0.0, 0.0, 0.0, 0.0)
_HEAD_CAPS = lambda r0, r1, ln: [(51, 2, (0.0, 0.0, 0.0, r0), (ln, 0.0, 0.0, r1)),
                                 (63, 2, (0.0, 0.0, 0.0, r0), (ln, 0.0, 0.0, r1))]
_COL = {
    "kosmos": [(48, 1, (0.025, -0.07, 0.0, 0.125), _Z4), (48, 5, (-0.035, -0.1, 0.0, 0.13), _Z4)]
              + _HEAD_CAPS(0.09, 0.07, 0.1)
              + [(39, 4, (0.1, 0.45, 0.0, 0.55), _Z4), (13, 4, (0.1, -0.37, 0.0, 0.5), _Z4)],
    "shelley": [(48, 1, (0.03, -0.08, 0.0, 0.08), _Z4),
                (51, 2, (-0.02, 0.0, 0.0, 0.1), (0.1, 0.0, 0.0, 0.075)),
                (63, 2, (-0.02, 0.0, 0.0, 0.1), (0.1, 0.0, 0.0, 0.075)),
                (39, 1, (0.1, 0.01, 0.05, 0.08), _Z4), (39, 1, (0.1, 0.01, -0.05, 0.08), _Z4),
                (39, 4, (0.1, 0.52, 0.0, 0.65), _Z4)],
    "human": [(48, 1, (0.01, -0.07, 0.0, 0.1), _Z4), (42, 4, (0.05, 0.15, 0.0, 0.3), _Z4)],
    "shion": [(48, 1, (0.03, -0.1, 0.0, 0.11), _Z4),
              (51, 2, (-0.05, 0.01, 0.01, 0.075), (0.14, -0.01, -0.01, 0.06)),
              (45, 4, (0.0, 0.48, 0.0, 0.6), _Z4), (42, 6, (0.0, 0.26, 0.0, 0.4), _Z4)],
    "chaos": [(48, 1, (0.01, -0.09, 0.0, 0.115), _Z4)],
    "momo": [(48, 1, (0.04, -0.1, 0.0, 0.09), _Z4)] + _HEAD_CAPS(0.09, 0.07, 0.1),
    "hyaku": [(48, 1, (0.04, -0.1, 0.0, 0.09), _Z4)] + _HEAD_CAPS(0.08, 0.06, 0.05),
    "shitan": [(48, 1, (0.04, -0.05, 0.0, 0.07), _Z4), (42, 4, (-0.1, 0.63, 0.0, 0.8), _Z4),
               (42, 1, (0.05, -0.15, 0.0, 0.3), _Z4)],
    "elly": [(48, 1, (0.01, -0.08, 0.0, 0.1), _Z4)] + _HEAD_CAPS(0.08, 0.06, 0.08)
            + [(42, 4, (-0.08, 0.3, 0.0, 0.4), _Z4)],
    "tonny": [(48, 1, (0.04, -0.1, 0.0, 0.09), _Z4),
              (16, 2, (-0.1, 0.0, -0.02, 0.15), (0.4, 0.0, 0.0, 0.14)),
              (26, 2, (-0.1, 0.0, 0.02, 0.15), (0.4, 0.0, 0.0, 0.14))],
    "albelt": [(48, 1, (0.05, -0.08, 0.0, 0.08), _Z4), (36, 4, (-0.05, 0.2, 0.0, 0.4), _Z4)],
    "kebin": [(48, 1, (0.04, -0.06, 0.0, 0.1), _Z4), (48, 3, (0.042, -0.06, 0.0, 0.11), _Z4)],
    "kebinh": [(48, 1, (0.015, -0.07, 0.0, 0.1), _Z4)],
    "shionch": [(48, 1, (0.01, -0.08, 0.0, 0.1), _Z4), (42, 4, (-0.08, 0.3, 0.0, 0.4), _Z4)],
    "helmer": [(55, 6, (0.35, 0.0, -0.22, 0.3), _Z4), (67, 7, (0.35, 0.0, 0.22, 0.3), _Z4)],
    "allen": [(48, 1, (0.03, -0.09, 0.0, 0.09), _Z4)],
    "abel": [(48, 1, (0.01, -0.07, 0.0, 0.1), _Z4), (42, 4, (0.0, 0.2, 0.0, 0.3), _Z4)],
    "mam": [(48, 1, (0.01, -0.07, 0.0, 0.1), _Z4), (42, 4, (0.05, 0.15, 0.0, 0.28), _Z4),
            (42, 6, (0.05, -0.15, 0.0, 0.28), _Z4)],
    "gaignun": [(48, 6, (0.01, -0.07, 0.0, 0.1), _Z4)],
    "off": [(42, 4, (0.05, 0.18, 0.0, 0.3), _Z4)],
    "miyuki": [(48, 1, (0.04, -0.1, 0.0, 0.09), _Z4), (39, 4, (0.12, 0.3, 0.0, 0.4), _Z4)],
    "robot": [(39, 4, (0.08, -0.2, 0.0, 0.4), _Z4)],
}
_COL["momo1"] = _COL["momo"] + [
    (16, 2, (0.0, 0.0, 0.0, 0.13), (0.4, 0.0, 0.0, 0.13)),
    (26, 2, (0.0, 0.0, 0.0, 0.13), (0.4, 0.0, 0.0, 0.13)),
    (17, 2, (0.0, 0.0, 0.0, 0.1), (0.4, 0.0, 0.0, 0.13)),
    (27, 2, (0.0, 0.0, 0.0, 0.1), (0.4, 0.0, 0.0, 0.13))]
_COL["momo4"] = _COL["momo"] + [(39, 4, (0.12, 0.4, 0.0, 0.55), _Z4)]
_COL = {k: [(j, kd, tuple(_f32(x) for x in s), tuple(_f32(x) for x in e))
            for j, kd, s, e in v] for k, v in _COL.items()}

_AUTO = "auto"                           # EX_HAIR_TYPE_AUTO


# skirtColliSize, skirtColliSizeB, skirtColliSizeS, skirtColliFootRad as the
# game starts them (EXM_PresetHair sets all four for every skirted name)
_SKIRT_SIZES = (1.0, 1.0, 1.0, 0.05)


def exm_preset(name: str):
    """``EXM_PresetHair`` for a ``.jnt`` name: (types, table, sizes).
    ``types`` is {chain number: (flags, kind)} (a chain it gives nothing gets
    flags 0, type 0: the game would read what an earlier skeleton left
    there), None (EX_HAIR_TYPE_NONE: every chain type 0, no collision) or
    "auto" (GetHairTypeSelection picks per chain); ``table`` a ``_COL`` key;
    ``sizes`` the skirt collision's (size, back size, side size, foot
    radius)."""
    if not name:
        return None, None, None
    c = lambda k: name[k] if k < len(name) else "\0"
    last = ""
    for ch in name:                       # GetLastWord: up to a non-ASCII byte
        if ord(ch) >= 0x80:
            break
        last = ch
    fl: dict = {}
    kd: dict = {}

    def put(idx, flags, kind):
        for k in idx:
            fl[k], kd[k] = flags, kind

    col = None
    sizes = [_SKIRT_SIZES]

    def sk(flags, kind=16, size=1.0, back=None, side=1.0, foot=0.05):
        """Skirt chains 0.. with these flags, and the skirt sizes (the back
        size is the size unless given)."""
        for k, f in enumerate(flags):
            fl[k], kd[k] = f, kind
        sizes[0] = (size, size if back is None else back, side, foot)
    a, b = c(0), c(1)
    if a == "A":
        if b == "L":
            if c(2) == "B":
                col = "albelt"
                if last == "H":
                    put(range(7), _ALT, 18); put([7, 10], 0, 6); put([8, 9], 0, 7)
                    put([11, 12, 13], 0, 8)
                else:
                    put(range(5), _ALT, 18)
            elif c(2) == "L":
                col = "allen"
                put([0, 1, 2, 6, 7], 0, 10); put([3, 4, 5], 0, 1)
        elif b == "N":
            if c(4) == "W":
                sk([_S0, _S180], size=1.1)
            elif c(4) == "D":
                col = "human"
                sk([_S45 | _HEM_B, _S315 | _HEM_B, _S135 | _HEM_A, _S225 | _HEM_A], size=1.1)
                put([4, 5], 0, 7); put([6], 0, 8)
        elif b == "B":
            col = "abel"
            sk(_CORNERS); put([4], _ALT, 4); put([5, 6], 0, 8)
        elif b == "S":
            sk(_CORNERS, back=0.8)
    elif a == "B":
        if b == "R":
            if last == "D":
                sk([_S180 | _HEM_A], size=1.3, back=1.0)
            else:
                col = "shelley"
                kd[0] = 4
                fl[0] = fl[1] = 0
        elif b == "A":
            col = "human"
            put([0], _ALT, 4)
        elif b == "O":
            if c(5) == "2":
                sk([f | _SPLIT_A for f in (_S90, _S270, _S0, _S45, _S315)], 13,
                   size=1.4, back=1.0)
            elif c(5) == "3":
                return _AUTO, None, None
    elif a == "C":
        if b == "H":
            col = "chaos"
            if last == "H":
                put(range(10), 0, 5); put(range(10, 14), 0, 9)
            else:
                put([0, 1], 0, 6); put([2, 3], 0, 9)
        elif b == "A":
            col = "human"
            if last == "1":
                put(range(5), 0, 6); put([5, 6], 0, 1)
            else:
                put(range(5), 0, 7)
        elif b == "L":
            sk([_S0, _S180], size=1.1, foot=0.07)
    elif a == "E":
        col = "elly"
        sk(_CORNERS_A, size=1.1, foot=0.07); put([4], _ALT, 2); put([5, 6], 0, 5); put([7, 8], 0, 4)
    elif a == "F":
        if b == "R":
            col = "human"
            put([0, 1], 0, 5)
        elif b == "E":
            sk(_CORNERS_A, size=1.4, back=1.1); put([4, 5, 6], 0, 7)
    elif a == "G":
        if b == "A":
            col = "gaignun"
            put([0, 2], _KIND6, 7); put([1], _KIND6, 5); put([3, 4], 0, 7); put([5], 0, 8)
        elif b == "I":
            sk([_S45 | _HEM_B, _S315 | _HEM_B, _S135 | _HEM_A, _S225 | _HEM_A],
               size=1.4, back=1.0)
    elif a == "H":
        if b == "A":
            put([0, 1], 0, 8)
        elif b == "E":
            col = "helmer"
            sk(_CORNERS_A + [_S90, _S270], size=1.7, back=1.1, foot=0.1)
            put([6], _A90 | _KIND6, 18); put([7], _A270 | _KIND7, 18)
        elif b == "Y":
            col = "hyaku"
            put([0, 1, 4, 5], 0, 4); put([2, 3], 0, 7)
    elif a == "I":
        sk([_S45 | _HEM_B, _S315 | _HEM_B, _S135 | _HEM_A, _S225 | _HEM_A],
           size=1.4, back=0.8)
    elif a == "J":
        if b == "R":
            if c(2) == "1":
                put([0], 0, 11)
            else:
                sk([_S0, _S90, _S270, _S45 | _COAT, _S315 | _COAT], 13, size=1.4, back=1.0)
                put([5], 0, 11)
        elif b == "O":
            col = "kebinh"
            put(range(4), 0, 7)
    elif a == "K":
        if b == "O":
            col = "kosmos"
            if last == "H":
                put([0], _ALT, 3); put([1, 2, 3], 0, 5); put([4, 5], _KIND5, 4); put([6, 7], 0, 8)
            elif last != "2":
                put([0], _ALT, 3); put([1, 2], 0, 5); put([3, 4], _KIND5, 6); put([5], 0, 8)
            else:
                return _AUTO, None, None
        elif b == "E":
            if last == "H":
                col = "kebinh"
                put(range(9), 0, 6)
            else:
                col = "kebin"
                if last == "N":
                    sk([_S0, _S90 | _LONG, _S270 | _LONG, _S45 | _LONG, _S315 | _LONG], 13,
                       size=2.0, back=0.4, side=0.6)
                    put([5, 6], 0, 7)
                else:
                    put([0, 1], 0, 7)
    elif a == "L":
        col = "momo"
        put([0, 1], 0, 5); put([2, 3], 0, 4)
    elif a == "M":
        if b == "O":
            if last == "H":
                if c(2) == "M":
                    col = "momo"
                    put(range(5), 0, 5); put([5, 6, 7], 0, 10); put([8, 9], 0, 2); put([10], 0, 8)
                else:
                    sk([_S0, _S180], size=1.1, back=1.0, foot=0.06)
            elif last == "1":
                col = "momo1"
                put([0, 1], 0, 14); put([2, 7], 0, 8); put([3, 4], 0, 5); put([5, 6], 0, 4)
            elif last == "2":
                col = "momo"
                sk(_CORNERS_A, size=1.3, back=0.8); put(range(4, 8), 0, 17); put([8, 13], 0, 8); put([9, 10], 0, 5)
                put([11, 12], 0, 4)
            elif last == "4":
                col = "momo4"
                put([0], _ALT, 4); put([1, 2], 0, 5); put([3, 4], 0, 4)
            else:
                col = "momo"
                put([0, 5], 0, 8); put([1, 2], 0, 5); put([3, 4], 0, 4)
        elif b == "A":
            col = "momo"
            put(range(4), 0, 4)
        elif b == "I":
            col = "miyuki"
            put([0, 2], 0, 4); put([1], _ALT, 2); put([3, 4, 5], 0, 7)
    elif a == "O":
        if b == "B":
            sk(_CORNERS, size=1.3, back=1.0)
        elif b == "F":
            col = "off"
            put([0], _ALT, 4)
    elif a == "P":
        return _AUTO, None, None
    elif a == "R":
        col = "robot"
        sk([_S0, _S0, _S180, _S180], size=1.3, back=1.0, foot=0.15); put([4], _ALT, 14)
    elif a == "S":
        if b == "H":
            if c(2) == "I":
                if c(3) == "O":
                    col = "shion"
                    if last == "H":
                        if c(6) == "C":
                            col = "shionch"
                            sk(_CORNERS_A, 15, size=1.3, back=1.0); put([4], _ALT, 2); put([5, 6, 7], 0, 7)
                        else:
                            put([0], _ALT, 2); put([8], _KIND6, 4); put([1, 2, 7], 0, 8)
                            put([3], 0, 4); put([4, 5, 6], 0, 5)
                    else:
                        put([0], 0, 6); put([1, 4], 0, 8); put([2], _ALT, 2); put([3], 0, 4)
                elif c(3) == "T":
                    col = "shitan"
                    if last == "H":
                        put([0], _ALT, 10); put([1], _ALT, 4); put([2, 3], 0, 4)
                    else:
                        put([0], _ALT, 4); put([1, 2], 0, 4)
                elif c(3) == "_":
                    if c(4) == "D":
                        col = "human"
                        put([0, 1], 0, 6); put([2], 0, 5)
                    elif c(4) == "M":
                        col = "mam"
                        put([0], _KIND6, 7); put([1], _ALT, 2); put([2, 3, 4], 0, 8)
            elif c(2) == "E":
                return _AUTO, None, None
        elif b == "E":
            sk(_CORNERS, size=1.4, back=1.0); put([4, 5], 0, 5)
        elif b == "T":
            col = "human"
            put([0, 1], 0, 5); put([2], 0, 7); put([3, 4], 0, 8)
    elif a == "T":
        col = "tonny"
        put([0, 1], 0, 4); put([2, 3, 4], 0, 5)
    elif a == "U":
        col = "momo"
        put([0, 1], 0, 4)
    elif a == "V":
        sk([_S0, _S90 | _LONG, _S270 | _LONG, _S45 | _LONG, _S315 | _LONG], 13,
           size=2.0, back=0.4, side=0.6)
    elif a == "W":
        sk(_CORNERS_A, size=1.5, back=0.8)
    elif a == "Y":
        sk(_CORNERS); put([4, 5], 0, 7)
    elif a == "Z":
        put([0, 1], 0, 5)
    if 0 not in kd:                       # htypebuf[0].kind still EX_HAIR_TYPE_NONE
        return None, None, None
    return ({k: (fl.get(k, 0), kd.get(k, 0)) for k in set(fl) | set(kd)}, col,
            tuple(_f32(v) for v in sizes[0]))


def _exm_auto(name: str, root: int):
    """GetHairTypeSelection: (flags, kind, table, skirt sizes) from the
    name's first letter and the chain root's block number (hair_type_code
    shares its halfword with saved_index)."""
    a = name[0] if name else ""
    if a == "P":
        t = {74: (_S0, 16), 79: (_S180, 16), 84: (0, 8), 87: (0, 5), 91: (0, 5),
             95: (0, 7), 100: (0, 7)}
        return t.get(root, (0, 0)) + ("human", (_f32(0.8), 1.0, 1.0, _f32(0.05)))
    if a == "S":
        t = {74: (_S0, 16), 77: (_S180, 16), 80: (_ALT, 2), 86: (0, 5), 90: (0, 5),
             94: (0, 4), 101: (0, 4)}
        return t.get(root, (0, 0)) + ("shelley", (1.0, 1.0, 1.0, _f32(0.05)))
    if a == "B":
        t = {113: (_S45 | _SPLIT_B, 16), 117: (_S0 | _SPLIT_B, 16),
             121: (_S315 | _SPLIT_B, 16), 125: (_S135 | _SPLIT_B | _HEM_A, 16),
             128: (_S225 | _SPLIT_B | _HEM_A, 16), 131: (0, 4), 135: (0, 4), 139: (0, 4),
             150: (0, 4), 155: (0, 4), 142: (0, 5), 146: (0, 5)}
        return t.get(root, (0, 0)) + (None, (1.0, 1.0, 1.0, _f32(0.15)))
    if a == "K":
        t = {74: (0, 12), 77: (0, 12), 80: (0, 12), 83: (0, 12), 86: (_ALT, 3),
             92: (0, 5), 95: (0, 5), 98: (_KIND5, 4), 103: (_KIND5, 4), 108: (0, 8)}
        return t.get(root, (0, 0)) + ("kosmos", None)
    return 0, 0, None, None


def _exm_collide(world, p, scale, prims, flags):
    """EXM_CheckExCollision: the averaged push and how many shapes pushed."""
    cx = cy = cz = 0.0
    count = 0
    for joint, kind, st, en in prims:
        if flags & _KIND6 and kind != 6:
            continue
        if flags & _KIND7 and kind != 7:
            continue
        if joint >= len(world) or world[joint] is None:
            continue
        sm = _translate(world[joint], st[:3])
        o = sm[2]
        sphere = False
        if kind in (1, 3):
            sphere = True
        elif kind == 5:
            sphere = bool(flags & _KIND5)
        elif kind == 7:
            sphere = bool(flags & _KIND7)
        elif kind == 6:
            sphere = bool(flags & _KIND6)
        if sphere or kind == 4:
            if sphere and flags & _ALT:
                continue
            if kind == 4 and not flags & _ALT:
                continue
            dx, dy, dz = p[0] - o[0], p[1] - o[1], p[2] - o[2]
            sep = dx * dx + dy * dy + dz * dz
            rad = st[3] * scale
            if sep == 0.0:
                continue
            if (rad * rad < sep) if kind == 3 else (sep < rad * rad):
                k = rad / math.sqrt(sep) - 1.0
                count += 1
                cx, cy, cz = cx + dx * k, cy + dy * k, cz + dz * k
        elif kind == 2 and not flags & _ALT:
            e = _translate(sm, en[:3])[2]
            ax, ay, az = e[0] - o[0], e[1] - o[1], e[2] - o[2]
            ln = ax * ax + ay * ay + az * az
            proj = (ax * p[0] + ay * p[1] + az * p[2] - (ax * o[0] + ay * o[1] + az * o[2]))
            proj = proj / ln if ln != 0.0 else 0.0
            if proj <= -0.1 or proj >= 1.1:
                continue
            dx = p[0] - (o[0] + ax * proj)
            dy = p[1] - (o[1] + ay * proj)
            dz = p[2] - (o[2] + az * proj)
            sep = dx * dx + dy * dy + dz * dz
            rad = st[3] if en[3] == st[3] else st[3] + (en[3] - st[3]) * proj
            if proj <= 0.0:
                rad *= (proj + 0.1) * 10.0
            if proj >= 1.0:
                rad *= (1.1 - proj) * 10.0
            rad *= scale
            if sep < rad * rad:
                if sep != 0.0:
                    k = rad / math.sqrt(sep) - 1.0
                    cx, cy, cz = cx + dx * k, cy + dy * k, cz + dz * k
                count += 1
    if count:
        cx, cy, cz = cx / count, cy / count, cz / count
    return (cx, cy, cz), count


# CheckSkirtCollisionSub's sections, by rootBorn: the blocks whose positions
# give (first start, second start, first end, second end): the two thighs
# (16/26 down to the knees 17/27 or the shins 18/28, 20/30), the long-coat
# pair (54/66 down to 56/68 or 60/72) and the split skirt's 16/29 pair
_SKIRT_SECTIONS = {0: (16, 26, 17, 27), 1: (16, 26, 20, 30), 2: (54, 66, 56, 68),
                   3: (54, 66, 60, 72), 4: (16, 29, 17, 30), 5: (16, 29, 20, 33),
                   6: (16, 26, 18, 28), 7: (16, 26, 20, 30)}
_SKIRT_ROOT, _SKIRT_HIP = 11, 13          # EX_JOINT_ROOT (x, z), EX_JOINT_HIP (y)
_Q1, _Q2, _Q3, _Q5, _Q6, _Q7 = (_f32(math.pi * k / 4) for k in (1, 2, 3, 5, 6, 7))
_PI = _f32(math.pi)


def _skirt_profile(flags):
    """The profile angle and the (side, depth) responses the
    EX_SKIRT_FLAG_ANGLE_* bits pick."""
    h = d = _f32(0.2)
    if flags & _A180:
        if flags & _A90:
            return _Q3, _f32(0.7), _f32(0.7)
        if flags & _A270:
            return _Q5, _f32(0.7), _f32(0.7)
        return _PI, 1.0, d
    if flags & _A0:
        if flags & _A90:
            return _Q1, _f32(0.7), _f32(0.7)
        if flags & _A270:
            return _Q7, _f32(0.7), _f32(0.7)
        return 0.0, 1.0, d
    if flags & _A90:
        return _Q2, h, 1.0
    return _Q6, h, 1.0


def _skirt_sub(world, p, ref, flags, sizes, born):
    """CheckSkirtCollisionSub for one section (``born`` is rootBorn): pushes
    ``p`` (a list, world space) out of the cone the two legs sweep, seen
    along the character's facing, and with a hem lift keeps it above the
    leg ends.  The game moves the section joints into root-relative space
    and back in place; here they are copies."""
    size, size_b, size_s, foot = sizes
    ang, hr, dr = _skirt_profile(flags)
    reverse = bool(flags & _SPLIT_B)
    sec = _SKIRT_SECTIONS[born]
    need = (_SKIRT_ROOT, _SKIRT_HIP, 14 if reverse else 36) + sec
    if max(need) >= len(world) or any(world[j] is None for j in need):
        return
    basis = world[14 if reverse else 36]
    ax = _translate((basis[0], basis[1], (0.0, 0.0, 0.0)), (0.0, 0.0, 1.0))[2]
    oc, os_ = ax[0], -ax[2]
    hip = world[_SKIRT_HIP][2]
    if reverse:
        a = -_Q2 - ang
        t = _translate(world[14], (0.0, math.cos(a), math.sin(a)))[2]
        rx, ds = t[0] - hip[0], t[2] - hip[2]
        rz = oc * ds + os_ * rx
        rx = oc * rx - os_ * ds
    else:
        t = _translate(world[_SKIRT_HIP], (0.0, math.cos(ang), math.sin(ang)))[2]
        ds, rx = t[0] - hip[0], t[2] - hip[2]
        rz = oc * ds - os_ * rx
        rx = oc * rx + os_ * ds
    root = world[_SKIRT_ROOT][2]
    ox, oz = root[0], root[2]
    fs, ss, fe, se = ([world[j][2][0] - ox, world[j][2][1], world[j][2][2] - oz] for j in sec)
    p[0] -= ox
    p[2] -= oz

    def edge(start, end):
        seg = math.dist(end, start)
        if seg == 0.0:
            return None
        lm = math.dist(p, start) / seg
        x = (end[0] - start[0]) * lm + start[0]
        z = (end[2] - start[2]) * lm + start[2]
        scale = size
        if ang < _Q2 or _Q6 < ang:
            scale *= size_b
        if _Q1 < ang < _Q3 or _Q5 < ang < _Q7:
            scale *= size_s
        if lm > 1.0:
            scale /= lm
        x, z = x * scale, z * scale
        return oc * x - os_ * z, oc * z + os_ * x

    first, second = edge(fs, fe), edge(ss, se)
    if first is not None and second is not None:
        fx, sx = max(first[0], second[0]), min(first[0], second[0])
        fz, sz = max(first[1], second[1]), min(first[1], second[1])
        fx += foot * 0.5
        sx -= foot * 0.5
        fz += foot
        sz -= foot
        lx = oc * p[0] - os_ * p[2]
        lz = oc * p[2] + os_ * p[0]
        if rz > 0.0:
            if lx < fx:
                lx += (fx - lx) * rz * dr
        elif rz < 0.0:
            if sx < lx:
                lx += (lx - sx) * rz * dr
        if rx > 0.0:
            if lz < fz:
                lz += (fz - lz) * rx * hr
        elif rx < 0.0:
            if sz < lz:
                lz += (lz - sz) * rx * hr
        p[0] = oc * lx + os_ * lz
        p[2] = oc * lz - os_ * lx

    side = 0                                  # which ends the hem lift tests
    if flags & _HEM_A and born == 0:
        side = 1                              # the front: ends ahead of the hip
    elif flags & _HEM_A and born == 6:
        side = 2                              # every end, no reference test
    elif flags & _HEM_B and born == 1:
        side = -1                             # the back
    if side:
        hy = hip[1]
        p[1] -= hy
        rr = math.hypot(ref[0] - ox, ref[2] - oz)
        for e in (fe, se):
            ey = e[1] - hy
            if side != 2:
                along = oc * e[2] + os_ * e[0]
                if (along < 0.0) if side == 1 else (along > 0.0):
                    continue
            reach = math.hypot(e[0], e[2])
            if side != 2 and not rr < reach:
                continue
            dist = math.hypot(p[0], p[2])
            k = reach / dist if dist != 0.0 else 1.0
            if k != 0.0 and p[1] * k < ey + foot + foot:
                p[1] = (ey + foot + foot) / k
        p[1] += hy
    p[0] += ox
    p[2] += oz


def _skirt_collide(world, p, ref, flags, sizes):
    """EXM_CheckSkirtCollision: the sections ``flags`` select, in order."""
    if flags & _SPLIT_A:
        borns = (4, 5)
    elif flags & _SPLIT_B:
        borns = (6, 7)
    else:
        borns = (0, 1, 2, 3) if flags & _LONG else (0, 1)
    for born in borns:
        _skirt_sub(world, p, ref, flags, sizes, born)


_JR_COAT = tuple(_f32(v) for v in (0.0985, 0.1285, 0.1985))


def _coat_pull(width, pos, joint):
    """AdjustJrCoatWidth: halfway toward the point ``width`` from ``joint``
    along the joint-to-``pos`` line."""
    d = (pos[0] - joint[0], pos[1] - joint[1], pos[2] - joint[2])
    ln = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2]) or 1.0
    w = width / ln
    return [(joint[a] + d[a] * w) * 0.5 + pos[a] * 0.5 for a in range(3)]


def _coat_width(world, p, root, index):
    """EX_HAIR_FLAG_COAT_WIDTH (Jr.'s coat): ``p`` is pulled to 0.25 from
    block 74 + index, and the matching joints of the coat's other chains
    toward ``p``.  The game picks those by the chain's root (91: block
    index + 80, 96: index + 86, any other: block index, as it does)."""
    j = 74 + index
    if j < len(world) and world[j] is not None:
        p[:] = _coat_pull(_f32(0.25), p, world[j][2])
    base = {91: index + 80, 96: index + 86}.get(root, 0)
    if index < 4:
        pulls = [(base, _JR_COAT[index - 1])]
    else:
        pulls = [(base, _JR_COAT[2]), (base + 1, _JR_COAT[2])]
    for c, width in pulls:
        if c < len(world) and world[c] is not None:
            m = world[c]
            world[c] = (m[0], m[1], tuple(_coat_pull(width, m[2], p)))


def exm_chains(rig: Rig) -> list:
    """The chains EXM_CalcExMotion solves, in order: (root, [queued blocks],
    flags, kind, collision).  ``collision`` is a ``_COL`` key, or for a skirt
    chain (EX_HAIR_FLAG_SKIRT) the skirt sizes; None runs the chain with no
    collision at all."""
    end = min(rig.primary + rig.secondary, len(rig.types))
    start = rig.hair if rig.hair >= 0 else rig.primary
    types, col, sizes = exm_preset(rig.name)
    out = []
    ibuf = 0
    i = start
    while i < end:
        if rig.types[i] != 3:
            i += 1
            continue
        root, j = i, i + 1
        while j < end and rig.types[j] == 2:
            j += 1
        if j >= end or rig.types[j] != 4:
            i = j
            continue
        queued = list(range(root + 1, j + 1))
        if types is None:
            flags, kind, table = 0, 0, None
        elif types == _AUTO:
            flags, kind, table, auto_sizes = _exm_auto(rig.name, root)
            if flags & _SKIRT:
                table = auto_sizes
        else:
            flags, kind = types.get(ibuf, (0, 0))
            table = sizes if flags & _SKIRT else col
            ibuf += 1
        out.append((root, queued, flags, kind, table))
        i = j + 1
    return out


def exm_step(rig: Rig, world: list, previous: Optional[dict], chains=None) -> dict:
    """One tick of ``EXM_CalcExMotion`` over ``world`` (a ``pose`` result,
    changed in place): ``previous`` holds each simulated joint's position
    the tick before (None: the first tick, which starts them where the rigid
    chain puts them).  Returns the positions for the next tick."""
    if chains is None:
        chains = exm_chains(rig)
    previous = previous or {}
    now: dict = {}
    solved: list = []
    for root, queued, flags, kind, table in chains:
        if world[root] is None:
            continue
        gravity, lresp, resp, cresp = _HAIRPARA[kind]
        skirt = table if flags & _SKIRT else None
        prims = _COL.get(table) if table and skirt is None else None
        first = queued[0]
        world[first] = _rotate(_translate(world[root], rig.translations[first]),
                               _rzyx(rig.rotations[first]))
        count = len(queued)
        for k in range(1, count):
            blk, parent = queued[k], world[queued[k - 1]]
            tr = rig.translations[blk]
            cur = _rotate(_translate(parent, tr), _rzyx(rig.rotations[blk]))
            tgt = cur[2]
            pp = previous.get(blk, tgt)
            p = [pp[0], pp[1] - gravity, pp[2]]
            r = cresp / ((k - 1) + cresp)
            for a in range(3):
                p[a] += (tgt[a] - p[a]) * resp[a] * r
            if flags & _COAT:
                _coat_width(world, p, root, k)
            if p[1] < 0.07:                      # the floor, 0.07 above the ground
                p[1] = 0.07
            pt = parent[2]
            d = (p[0] - pt[0], p[1] - pt[1], p[2] - pt[2])
            dist = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
            if dist != 0.0:
                w = lresp / dist
                for a in range(3):
                    p[a] += w * d[a] * (tr[0] * cur[1][a] - dist)
            else:
                p[1] -= gravity
            if skirt is not None:
                _skirt_collide(world, p, parent[2], flags, skirt)
            elif prims:
                corr, hits = _exm_collide(world, p, cur[1][0], prims, flags)
                if hits:
                    p[0] = (p[0] - pp[0]) * 0.9 + corr[0] + pp[0]
                    p[1] += corr[1]
                    p[2] = (p[2] - pp[2]) * 0.9 + corr[2] + pp[2]
            world[blk] = (cur[0], cur[1], tuple(p))
            solved.append(blk)
        bend = 0.0
        if flags & _ALT and len(world) > 48 and world[39] is not None \
                and world[48] is not None:
            def flat(m, v):
                e = _translate(m, v)[2]
                x, z = e[0] - m[2][0], e[2] - m[2][2]
                ln = math.sqrt(x * x + 0.0001 * 0.0001 + z * z)
                return (x / ln, 0.0001 / ln, z / ln)
            f1, f2 = flat(world[39], (0.0, 0.0, 1.0)), flat(world[48], (1.0, 0.0, 0.0))
            dot = max(-1.0, min(1.0, f1[0] * f2[0] + f1[1] * f2[1] + f1[2] * f2[2]))
            bend = math.asin(dot) / count
        for k in range(1, count):                # OrientExMotionJoint
            blk = root + k
            m, nxt = world[blk], world[blk + 1]
            d = _mtv(m[0], (nxt[2][0] - m[2][0], nxt[2][1] - m[2][1], nxt[2][2] - m[2][2]))
            h = math.hypot(d[0], d[1])
            ln = math.sqrt(d[0] * d[0] + d[1] * d[1] + d[2] * d[2])
            if h == 0.0 or ln == 0.0:
                continue
            r = _mm(m[0], _ry(-math.atan2(d[2] / ln, h / ln)))
            r = _mm(r, _rz(-math.atan2(-d[1] / h, d[0] / h)))
            if flags & _ALT:
                r = _mm(r, _rx(k * bend))
            world[blk] = (r, m[1], m[2])
    # read back after every chain: the coat width moves joints of chains
    # already solved this tick, and the game's previous matrices see that
    for blk in solved:
        now[blk] = world[blk][2]
    return now


def simulate(rig: Rig, clip: Clip, frames: list, stats: Optional[dict] = None,
             settle: int = 120, chains=None) -> list:
    """World matrices at each of ``frames`` with the hair and cloth chains
    run through ``EXM_CalcExMotion`` a game tick (``STEP``) at a time.  The
    chains first settle for ``settle`` ticks on the first frame; a clip that
    ends where it starts (a loop) is then played once more before the
    frames are taken, so its first frame follows on from its last.
    ``chains`` replaces ``exm_chains(rig)`` (the self-test runs them with no
    collision table)."""
    if chains is None:
        chains = exm_chains(rig)
    if not chains:
        return [pose(rig, clip, float(f), stats) for f in frames]
    first, last = frames[0], frames[-1]
    tick = STEP * FPS
    held = pose(rig, clip, float(first))
    prev = None
    for _ in range(settle):
        w = list(held)
        prev = exm_step(rig, w, prev, chains)
    steps = max(1, int(round((last - first) / tick)))
    looped = False
    if last > first:
        end = pose(rig, clip, float(last))
        body = rig.hair if rig.hair >= 0 else rig.primary
        looped = all(a is None and b is None or (a is not None and b is not None and
                     math.dist(a[2], b[2]) < 0.02)
                     for a, b in zip(held[:body], end[:body]))
    if looped:
        for s in range(1, steps + 1):
            w = pose(rig, clip, first + s * tick)
            prev = exm_step(rig, w, prev, chains)
    out = []
    want = {float(f) for f in frames}
    s = 0
    while True:
        t = first + s * tick
        w = pose(rig, clip, t, stats if t in want else None)
        prev = exm_step(rig, w, prev, chains)
        if t in want:
            out.append(w)
        if t >= last:
            break
        s += 1
    return out


# ---------------------------------------------------------------------------
# baking to glTF
# ---------------------------------------------------------------------------

def _quat(m):
    t = m[0] + m[4] + m[8]
    if t > 0:
        s = math.sqrt(t + 1.0) * 2
        return ((m[7] - m[5]) / s, (m[2] - m[6]) / s, (m[3] - m[1]) / s, 0.25 * s)
    if m[0] > m[4] and m[0] > m[8]:
        s = math.sqrt(max(1e-12, 1.0 + m[0] - m[4] - m[8])) * 2
        return (0.25 * s, (m[1] + m[3]) / s, (m[2] + m[6]) / s, (m[7] - m[5]) / s)
    if m[4] > m[8]:
        s = math.sqrt(max(1e-12, 1.0 + m[4] - m[0] - m[8])) * 2
        return ((m[1] + m[3]) / s, 0.25 * s, (m[5] + m[7]) / s, (m[2] - m[6]) / s)
    s = math.sqrt(max(1e-12, 1.0 + m[8] - m[0] - m[4])) * 2
    return ((m[2] + m[6]) / s, (m[5] + m[7]) / s, 0.25 * s, (m[3] - m[1]) / s)


def _local(parent, m):
    """(translation, quaternion, scale) of ``m`` in ``parent``'s space."""
    if parent is None:
        r, s, t = m
        return t, _quat(r), s
    pr, ps, pt = parent
    d = _mtv(pr, (m[2][0] - pt[0], m[2][1] - pt[1], m[2][2] - pt[2]))
    tl = (d[0] / ps[0], d[1] / ps[1], d[2] / ps[2])
    r = _mm((pr[0], pr[3], pr[6], pr[1], pr[4], pr[7], pr[2], pr[5], pr[8]), m[0])
    # a matrix starts as a copy of its parent's, scale included, so the
    # block's own scale is the ratio (exact when the parent's is uniform)
    s = (m[1][0] / ps[0], m[1][1] / ps[1], m[1][2] / ps[2])
    return tl, _quat(r), s


def _basis(r, s):
    """R * diag(s): the matrix a block's (rotation, scale) stands for."""
    return (r[0] * s[0], r[1] * s[1], r[2] * s[2],
            r[3] * s[0], r[4] * s[1], r[5] * s[2],
            r[6] * s[0], r[7] * s[1], r[8] * s[2])


def _qmat(q):
    x, y, z, w = q
    return (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w),
            2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w),
            2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y))


def _inexact(parent, m) -> bool:
    """True when ``m`` cannot be written as a TRS under ``parent`` in a glTF
    hierarchy.  The game keeps scale on the right (R_parent * R * diag(s));
    glTF composes R_p * diag(s_p) * R_local * diag(s_local), which is not
    the same when the parent's scale is not uniform and the block is turned
    against it (the local matrix would need shear)."""
    if parent is None or max(parent[1]) - min(parent[1]) <= 1e-6 * max(1.0, max(map(abs, parent[1]))):
        return False
    _tl, q, sl = _local(parent, m)
    got = _mm(_basis(parent[0], parent[1]), _basis(_qmat(q), sl))
    want = _basis(m[0], m[1])
    tol = 1e-5 * max(1.0, max(abs(x) for x in m[1]))
    return any(abs(x - y) > tol for x, y in zip(got, want))


def _scales(clip: Clip) -> bool:
    """True when some record of the clip can scale a block (a scale curve
    that is not a constant 1): only then can a parent's scale be
    non-uniform."""
    for ch in clip.channels:
        for bit in (6, 7, 8):
            cv = ch.curves.get(bit)
            if cv is None:
                continue
            if cv.kind == 1 and cv.value == 1.0:
                continue
            if cv.kind == 3 and all(k[2] == 1.0 and k[3] == 0.0 and k[4] == 0.0
                                    for k in cv.keys):
                continue
            return True
    return False


def bake(rig: Rig, clip: Clip, stats: Optional[dict] = None,
         flat: frozenset = frozenset()) -> dict:
    """Sample the clip at every frame: {"times": [...], "tracks": {block:
    {"translation"/"rotation"/"scale": [value per frame]}}, "inexact": set}
    for the blocks the clip drives, each relative to its parent block.
    Blocks in ``flat`` are written in skeleton space instead (``add_to_glb``
    hangs them straight off the skeleton node) and always get a track.
    ``inexact`` lists the blocks whose pose a parent-relative TRS cannot
    hold (see ``_inexact``); it is only looked for when the clip scales."""
    frames = list(range(clip.first_frame, clip.last_frame + 1)) or [clip.first_frame]
    body = _ranges(rig)[0]
    # hair and cloth blocks take no clip record; the chains EXM_CalcExMotion
    # solves are simulated (see simulate) and get a track of their own
    chains = exm_chains(rig)
    driven = [i for i in _record_map(rig, clip)
              if rig.types[i] in (1, 2, 3, 4, 5) or (rig.types[i] == 0 and i < body)]
    driven = sorted(set(driven) | {b for _r, queued, *_ in chains for b in queued} | set(flat))
    tracks = {i: {"translation": [], "rotation": [], "scale": []} for i in driven}
    n = len(rig.types)
    check = _scales(clip)
    inexact: set = set()
    worlds = simulate(rig, clip, frames, stats) if chains else None
    for k, f in enumerate(frames):
        world = worlds[k] if worlds else pose(rig, clip, float(f), stats,
                                              driven_only=not (check or flat))
        if check:
            for i, m in enumerate(world):
                p = rig.parents[i]
                if m is not None and p != i and p < n and _inexact(world[p], m):
                    inexact.add(i)
        for i in driven:
            m = world[i]
            if m is None:
                continue
            p = rig.parents[i]
            parent = world[p] if (p != i and p < n and i not in flat) else None
            tl, q, s = _local(parent, m)
            tk = tracks[i]
            if tk["rotation"]:
                pq = tk["rotation"][-1]
                if pq[0] * q[0] + pq[1] * q[1] + pq[2] * q[2] + pq[3] * q[3] < 0:
                    q = (-q[0], -q[1], -q[2], -q[3])
            tk["translation"].append(tl)
            tk["rotation"].append(q)
            tk["scale"].append(s)
    return {"times": [(f - clip.first_frame) / FPS for f in frames],
            "tracks": {i: t for i, t in tracks.items() if len(t["rotation"]) == len(frames)},
            "inexact": inexact}


def _read_glb(glb: bytes) -> tuple[dict, bytearray]:
    if glb[:4] != b"glTF":
        raise FcvError("not a .glb")
    jlen = struct.unpack_from("<I", glb, 12)[0]
    js = json.loads(glb[20:20 + jlen])
    p = 20 + jlen
    blen = struct.unpack_from("<I", glb, p)[0] if p + 8 <= len(glb) else 0
    return js, bytearray(glb[p + 8:p + 8 + blen])


def _write_glb(js: dict, blob: bytearray) -> bytes:
    while len(blob) % 4:
        blob.append(0)
    js["buffers"] = [{"byteLength": len(blob)}]
    j = json.dumps(js, separators=(",", ":")).encode()
    while len(j) % 4:
        j += b" "
    total = 12 + 8 + len(j) + 8 + len(blob)
    return (struct.pack("<4sII", b"glTF", 2, total)
            + struct.pack("<I4s", len(j), b"JSON") + j
            + struct.pack("<I4s", len(blob), b"BIN\0") + bytes(blob))


def _same(vals, tol=1e-5) -> bool:
    a = vals[0]
    return all(abs(x - y) <= tol for v in vals[1:] for x, y in zip(a, v))


def add_to_glb(glb: bytes, rig: Rig, sources: list) -> tuple[bytes, list]:
    """Append one glTF animation per clip to a ``.glb`` written by
    ``lex.to_glb`` with this skeleton.  ``sources`` is [(name, Clip)]; a clip
    that does not fit the skeleton, or cannot be posed, is left out and the
    others are still written.  Returns (glb, [summary per clip: name,
    frames, written, reason]).

    When some clip scales a block unevenly and turns a child against it
    (a few boss and cutscene clips), the child's pose cannot be held by a
    TRS under its parent (see ``_inexact``).  Those blocks and everything
    below them are then hung straight off the skeleton node, keyed in
    skeleton space in every animation of the file, which the game's
    rotation-times-scale matrices always fit exactly."""
    js, blob = _read_glb(glb)
    nodes = js.get("nodes", [])
    base = next((i for i, nd in enumerate(nodes) if "mesh" not in nd), None)
    root = next((i for i, nd in enumerate(nodes) if nd.get("name") == "skeleton"), None)
    if base is None or root is None or base + len(rig.types) > root:
        raise FcvError("the .glb has no armature for this skeleton")
    views, accessors = js.setdefault("bufferViews", []), js.setdefault("accessors", [])

    # pass 1: bake every clip that fits; collect the blocks a parent-relative
    # TRS cannot hold
    baked_all: list = []
    summary_of: dict = {}
    flat: set = set()
    for k, (name, clip) in enumerate(sources):
        if not fits(rig, clip):
            summary_of[k] = {"name": name, "frames": clip.frames, "written": False,
                             "reason": "its records do not line up with this skeleton"}
            continue
        stats: dict = {}
        try:
            baked = bake(rig, clip, stats)
        except (FcvError, ZeroDivisionError, OverflowError, ValueError, IndexError) as e:
            summary_of[k] = {"name": name, "frames": clip.frames, "written": False,
                             "reason": "could not be posed (%s)" % e}
            continue
        flat |= baked["inexact"]
        baked_all.append((k, name, clip, baked, stats))
    if flat:
        n = len(rig.types)
        kids: dict = {}
        for i, p in enumerate(rig.parents):
            if p != i and p < n:
                kids.setdefault(p, []).append(i)
        todo = list(flat)
        while todo:
            for c in kids.get(todo.pop(), ()):
                if c not in flat:
                    flat.add(c)
                    todo.append(c)
        _hang_off_root(nodes, root, [base + i for i in sorted(flat)])
        rebaked = []
        for k, name, clip, _b, _s in baked_all:
            stats = {}
            try:
                rebaked.append((k, name, clip, bake(rig, clip, stats, frozenset(flat)), stats))
            except (FcvError, ZeroDivisionError, OverflowError, ValueError, IndexError) as e:
                summary_of[k] = {"name": name, "frames": clip.frames, "written": False,
                                 "reason": "could not be posed (%s)" % e}
        baked_all = rebaked

    def accessor(fmt: str, rows: list, atype: str, minmax: bool = False) -> int:
        while len(blob) % 4:
            blob.append(0)
        raw = b"".join(struct.pack(fmt, *r) for r in rows)
        views.append({"buffer": 0, "byteOffset": len(blob), "byteLength": len(raw)})
        blob.extend(raw)
        acc = {"bufferView": len(views) - 1, "componentType": 5126,
               "count": len(rows), "type": atype}
        if minmax:
            acc["min"], acc["max"] = [min(r[0] for r in rows)], [max(r[0] for r in rows)]
        accessors.append(acc)
        return len(accessors) - 1

    # a block with no skinned joint at or below it moves no vertex (the foot
    # and hand targets the chains are solved toward); Blender imports such a
    # node as an empty with an action slot of its own, so they are not keyed
    joints = {j for sk in js.get("skins", []) for j in sk.get("joints", [])}
    carries: dict = {}

    def carry(i: int) -> bool:
        if i not in carries:
            carries[i] = False
            carries[i] = i in joints or any(carry(c) for c in nodes[i].get("children", ()))
        return carries[i]

    anims = js.setdefault("animations", [])
    for k, name, clip, baked, stats in baked_all:
        if not all(math.isfinite(x) for tk in baked["tracks"].values()
                   for vals in tk.values() for v in vals for x in v):
            summary_of[k] = {"name": name, "frames": clip.frames, "written": False,
                             "reason": "its pose is not finite (a damaged clip)"}
            continue
        times = baked["times"]
        full = accessor("<f", [(t,) for t in times], "SCALAR", True)
        ends = accessor("<f", [(times[0],), (times[-1],)], "SCALAR", True) \
            if len(times) > 1 else full
        samplers, channels = [], []
        for blk, tk in sorted(baked["tracks"].items()):
            if joints and not carry(base + blk):
                continue
            node = nodes[base + blk]
            rest = {"translation": tuple(node.get("translation", (0.0, 0.0, 0.0))),
                    "rotation": tuple(node.get("rotation", (0.0, 0.0, 0.0, 1.0))),
                    "scale": tuple(node.get("scale", (1.0, 1.0, 1.0)))}
            for path, fmt, atype in (("translation", "<3f", "VEC3"),
                                     ("rotation", "<4f", "VEC4"), ("scale", "<3f", "VEC3")):
                vals = tk[path]
                if _same(vals):
                    r = rest[path]
                    if path == "rotation" and sum(a * b for a, b in zip(vals[0], r)) < 0:
                        r = tuple(-x for x in r)
                    if all(abs(a - b) <= 1e-4 for a, b in zip(vals[0], r)):
                        continue                    # the rest pose: nothing to key
                    out = accessor(fmt, [vals[0], vals[0]] if ends != full else [vals[0]], atype)
                    samplers.append({"input": ends, "output": out, "interpolation": "LINEAR"})
                else:
                    out = accessor(fmt, vals, atype)
                    samplers.append({"input": full, "output": out, "interpolation": "LINEAR"})
                channels.append({"sampler": len(samplers) - 1,
                                 "target": {"node": base + blk, "path": path}})
        if not channels:                             # a clip that holds the rest pose
            node = nodes[base]
            out = accessor("<3f", [tuple(node.get("translation", (0.0, 0.0, 0.0)))] *
                           (2 if ends != full else 1), "VEC3")
            samplers.append({"input": ends, "output": out, "interpolation": "LINEAR"})
            channels.append({"sampler": 0, "target": {"node": base, "path": "translation"}})
        anims.append({"name": name, "samplers": samplers, "channels": channels,
                      "extras": {"skeleton": clip.name, "frames": clip.frames, "fps": 30,
                                 "first_frame": clip.first_frame,
                                 "last_frame": clip.last_frame,
                                 "frame_offset": clip.frame_offset,
                                 "ranges": [list(r) for r in clip.ranges],
                                 "blocks_driven": len(clip.channels),
                                 "chains_solved": sum(v for sk, v in stats.items()
                                                      if sk.startswith("solved_"))
                                 // max(1, len(times))}})
        if flat:
            anims[-1]["extras"]["skeleton_space_blocks"] = len(flat)
        summary_of[k] = {"name": name, "frames": clip.frames, "written": True,
                         "channels": len(channels)}
    if not anims:
        js.pop("animations", None)
    return _write_glb(js, blob), [summary_of[k] for k in sorted(summary_of)]


def _trs_matrix(nd: dict):
    """(R * diag(s), t) of a glTF node's TRS."""
    q = nd.get("rotation", (0.0, 0.0, 0.0, 1.0))
    return (_basis(_qmat(q), nd.get("scale", (1.0, 1.0, 1.0))),
            tuple(nd.get("translation", (0.0, 0.0, 0.0))))


def _hang_off_root(nodes: list, root: int, move: list) -> None:
    """Make each node in ``move`` a direct child of ``root`` (the skeleton
    node, which has no transform), keeping its rest-pose place."""
    parent = {c: i for i, nd in enumerate(nodes) for c in nd.get("children", ())}

    def world(i):
        m, t = _trs_matrix(nodes[i])
        p = parent.get(i)
        while p is not None and p != root:
            pm, pt = _trs_matrix(nodes[p])
            t = tuple(a + b for a, b in zip(_mv(pm, t), pt))
            m = _mm(pm, m)
            p = parent.get(p)
        return m, t
    rest = {i: world(i) for i in move}
    for i in move:
        m, t = rest[i]
        sc = [math.sqrt(m[c] ** 2 + m[3 + c] ** 2 + m[6 + c] ** 2) for c in range(3)]
        r = tuple(m[3 * row + c] / (sc[c] or 1.0) for row in range(3) for c in range(3))
        p = parent.get(i)
        if p is not None and p != root:
            nodes[p]["children"].remove(i)
            if not nodes[p]["children"]:
                del nodes[p]["children"]
            nodes[root].setdefault("children", []).append(i)
        nodes[i]["translation"] = [float(x) for x in t]
        nodes[i]["rotation"] = [float(x) for x in _quat(r)]
        if any(abs(x - 1.0) > 1e-6 for x in sc):
            nodes[i]["scale"] = [float(x) for x in sc]


# ---------------------------------------------------------------------------
# the disc
# ---------------------------------------------------------------------------

def _load(path) -> bytes:
    import arx
    data = open(path, "rb").read()
    if not arx.is_arx(data):
        return data
    try:
        return arx.decompress(data)
    except struct.error:
        raise FcvError("ARX file cut short (%d bytes)" % len(data)) from None


def pack_label(path, dump) -> str:
    """``chain0/motion/kosmos_cf.fpk`` -> ``motion/kosmos_cf``."""
    from pathlib import Path
    rel = Path(path).relative_to(dump).with_suffix("")
    parts = rel.parts[1:] if rel.parts and rel.parts[0].startswith(("chain", "layer")) else rel.parts
    return "/".join(parts)


def clip_name(label: str, slots: list) -> str:
    return "%s#%s" % (label, ",".join("%02d" % s for s in slots))


class MotionIndex:
    """Every pack under a dump's ``motion`` directories, read once, so
    ``browse`` can ask which clips were made for a skeleton.  The dump's
    folders are looked through the way ``find_in_dump`` does: the dump
    itself and each folder in it (``chain0``, ``layer1``, ...) as a root.
    ``by_name`` is keyed by the clip's attribute key (see ``Clip``); unnamed
    clips (key 0) are not indexed, by the kit's choice (in the game they
    would match a skeleton with no name)."""

    def __init__(self, dump, subdirs=("motion",)):
        from pathlib import Path
        self.dump = Path(dump)
        self.by_name: dict = {}
        self.failed: list = []
        roots = [self.dump] + (sorted(d for d in self.dump.iterdir() if d.is_dir())
                               if self.dump.is_dir() else [])
        packs, seen = [], set()
        for root in roots:
            for p in sorted(root.glob("*/*.fpk")):
                rp = p.resolve()
                if p.parent.name.lower() in subdirs and rp not in seen:
                    seen.add(rp)
                    packs.append(p)
        for p in packs:
            try:
                clips = read_pack(_load(p))
            except (FcvError, ValueError, struct.error, OSError) as e:
                self.failed.append((p, str(e)))
                continue
            label = pack_label(p, self.dump)
            for slots, clip in clips:
                if clip.attribute_key:
                    self.by_name.setdefault(clip.attribute_key, []).append(
                        (clip_name(label, slots), clip))

    def clips_for(self, rig: Rig) -> list:
        """The clips whose attribute key is this skeleton's and whose records
        line up with its blocks."""
        if not rig.attribute_key:
            return []
        return [(nm, c) for nm, c in self.by_name.get(rig.attribute_key, [])
                if fits(rig, c)]


def sweep(dump, log=print) -> dict:
    """Read every clip of every ``.fpk`` under ``dump`` (strictly: to exactly
    its last byte), pair each with every skeleton whose attribute key it
    carries and whose blocks it lines up with, and pose the first, middle
    and last frame of each pair (every matrix number finite).  Returns the
    counts; a clip counts as posed when all its pairs pose."""
    from collections import Counter
    from pathlib import Path
    dump = Path(dump)
    stats: Counter = Counter()
    rigs: dict = {}
    for p in sorted(dump.rglob("*.jnt")):
        try:
            rig = parse_rig(_load(p))
        except (FcvError, ValueError, struct.error, OSError):
            stats["skeletons_unread"] += 1
            continue
        stats["skeletons"] += 1
        rigs.setdefault(rig.attribute_key, []).append((p, rig))
    reasons: Counter = Counter()
    for p in sorted(dump.rglob("*.fpk")):
        stats["packs"] += 1
        try:
            data = _load(p)
            offs = parse_fpk(data)
        except (FcvError, ValueError, struct.error, OSError) as e:
            stats["packs_unread"] += 1
            log("  %s: %s" % (p.relative_to(dump), e))
            continue
        order = sorted(set(offs))
        for k, o in enumerate(order):
            end = order[k + 1] if k + 1 < len(order) else len(data)
            stats["clips"] += 1
            try:
                clip = parse_clip(data, o, end, strict=True)
            except (FcvError, struct.error) as e:
                stats["clips_unread"] += 1
                reasons[str(e).split(":")[0][:60]] += 1
                log("  %s clip at 0x%x: %s" % (p.relative_to(dump), o, e))
                continue
            stats["clips_read"] += 1
            stats["frames"] += clip.frames
            stats["keys"] += clip.key_count()
            if not clip.attribute_key:
                stats["clips_unnamed"] += 1
                continue
            cands = rigs.get(clip.attribute_key, [])
            if not cands:
                stats["clips_no_skeleton_of_that_name"] += 1
                continue
            fitting = [(jp, rig) for jp, rig in cands if fits(rig, clip)]
            if not fitting:
                stats["clips_named_but_not_lining_up"] += 1
                continue
            stats["clips_paired"] += 1
            all_ok = True
            for jp, rig in fitting:
                stats["pairs"] += 1
                try:
                    for f in sorted({clip.first_frame,
                                     (clip.first_frame + clip.last_frame) // 2,
                                     clip.last_frame}):
                        for m in pose(rig, clip, float(f)):
                            if m is not None and not all(math.isfinite(x)
                                                         for x in m[0] + m[1] + m[2]):
                                raise FcvError("a pose that is not finite")
                    stats["pairs_posed"] += 1
                except (FcvError, ZeroDivisionError, OverflowError, ValueError,
                        IndexError) as e:
                    all_ok = False
                    stats["pairs_pose_failed"] += 1
                    log("  %s clip at 0x%x on %s: %s" % (p.relative_to(dump), o, jp.name, e))
            stats["clips_posed" if all_ok else "clips_pose_failed"] += 1
    out = dict(stats)
    out["unread_reasons"] = dict(reasons)
    return out


# ---------------------------------------------------------------------------
# self-test (no disc needed)
# ---------------------------------------------------------------------------

def _mk_curve(keys=None, const=None) -> bytes:
    if keys is None and const is None:
        return struct.pack("<HH", 0, 0)
    if const is not None:
        return struct.pack("<HHf", 0, 1, const)
    out = struct.pack("<HH", len(keys), 3)
    for k in keys:
        if k[0] in (0, 2, 3):
            out += struct.pack("<HHf", k[0], k[1], k[2])
        elif k[0] == 4:
            out += struct.pack("<HHff", k[0], k[1], k[2], k[3])
        else:
            out += struct.pack("<HHfff", k[0], k[1], k[2], k[3], k[4])
    return out


def _mk_clip(name: bytes, frames: int, records: list) -> bytes:
    attrs = struct.pack("<HH8s", 12, 1, name)
    body = b"FCV\0" + struct.pack("<HHHHhH", 3, frames, 0, frames - 1, 0, 0)
    body += struct.pack("<I", 4 + len(attrs)) + attrs
    for flags, mask, links, curves in records:
        if links is None:
            body += struct.pack("<HHHH", 4, 4, flags, mask)
        else:
            body += struct.pack("<HHHHHHHH", 12, 4, flags, mask, *links, 0)
        body += b"".join(curves)
    return body + struct.pack("<HHHH", 4, 4, 0xFFFF, 0)


def _mk_jnt(name: bytes, blocks: list, primary: Optional[int] = None, secondary: int = 0,
            tag: Optional[tuple] = None, aprim: int = 0) -> bytes:
    prim = len(blocks) if primary is None else primary
    head = b"JNT\0" + struct.pack("<6H", 3, len(blocks), prim, secondary, aprim, 0)
    attrs = struct.pack("<HH8s", 12, 1, name)
    if tag is not None:                      # a type-5 record: (word count, tag)
        attrs += struct.pack("<HHii", 12, 5, *tag)
    head += struct.pack("<i", 4 + len(attrs)) + attrs
    out = bytearray(head)
    for typ, parent, tr, ro, direction in blocks:
        b = bytearray(64)
        struct.pack_into("<H", b, 0, typ)
        struct.pack_into("<H", b, 0x0E, parent)
        struct.pack_into("<3f", b, 0x10, *tr)
        struct.pack_into("<3f", b, 0x20, *ro)
        struct.pack_into("<f", b, 0x38, direction)
        out += b
    return bytes(out)


def self_test() -> list[tuple[str, bool, str]]:
    """Checks on hand-made packs: [(name, passed, detail)]."""
    res = []

    def check(name, ok, detail=""):
        res.append((name, bool(ok), detail))

    # curves: linear, step, Hermite against the closed form, hold at both ends
    sc = 30 / 65535.0 / FPS                    # a 30-frame clip
    k = lambda f: int(round(f * 65535 / 30))
    lin = Curve(3, 3, 0.0, [(0, k(0), 0.0, 0.0, 0.0), (0, k(10), 5.0, 0.0, 0.0)])
    check("linear key", abs(eval_curve(lin, sc, 4 / FPS) - 2.0) < 1e-3,
          "%.4f" % eval_curve(lin, sc, 4 / FPS))
    stp = Curve(3, 3, 0.0, [(3, k(0), 1.0, 0.0, 0.0), (0, k(10), 5.0, 0.0, 0.0)])
    check("step key", eval_curve(stp, sc, 9 / FPS) == 1.0 and
          abs(eval_curve(stp, sc, 10 / FPS) - 5.0) < 1e-6)
    her = Curve(3, 3, 0.0, [(1, k(0), 1.0, 0.0, 3.0), (4, k(10), 2.0, -1.5, -1.5)])
    x, span = 4 / FPS, 10 / FPS
    u = x / span
    want = ((2 * u ** 3 - 3 * u ** 2 + 1) * 1.0 + (-2 * u ** 3 + 3 * u ** 2) * 2.0
            + (u ** 3 - 2 * u ** 2 + u) * span * 3.0 + (u ** 3 - u ** 2) * span * -1.5)
    got = eval_curve(her, sc, x)
    check("Hermite key", abs(got - want) < 1e-3, "%.5f vs %.5f" % (got, want))
    check("hold before and after", eval_curve(her, sc, -1.0) == 1.0 and
          eval_curve(her, sc, 99.0) == 2.0)

    # a pack with two slots on one clip, and a clip that stops short
    jnt = _mk_jnt(b"TEST", [
        (1, 0, (0, 0, 0), (0, 0, 0), 0.0),            # 0 root
        (1, 0, (0, 0, 0), (0, 0, 0), 0.0),            # 1 foot target
        (3, 0, (0, 1.0, 0), (0, 0, 0), 0.0),          # 2 chain root (hip)
        (2, 2, (0, 0, 0), (0, 0, 0), -1.0),           # 3 upper link
        (2, 3, (0.5, 0, 0), (0, 0, 0), -1.0),         # 4 lower link
        (4, 4, (0.5, 0, 0), (0, 0, 0), 0.0),          # 5 end, solved onto block 1
    ])
    clip = _mk_clip(b"TEST", 11, [
        (0, 0x1FF, None, [_mk_curve()] * 6 + [_mk_curve(const=1.0)] * 3),
        (0, 0x1FF, None, [_mk_curve(keys=[(0, 0, 0.3), (0, 65535 * 10 // 11, 0.6)]),
                          _mk_curve(const=0.4), _mk_curve()] + [_mk_curve()] * 3
                         + [_mk_curve(const=1.0)] * 3),
        (0, 0x3F, None, [_mk_curve(), _mk_curve(const=1.0), _mk_curve()] + [_mk_curve()] * 3),
        (0, 0x1F8, None, [_mk_curve()] * 3 + [_mk_curve(const=1.0)] * 3),
        (0, 0x1F8, None, [_mk_curve()] * 3 + [_mk_curve(const=1.0)] * 3),
        (5, 0, (1, 0, 0), []),
    ])
    table = struct.pack("<4sIIHH", b"FPK\0", 3, 2, 0, 4)
    pack = table + struct.pack("<II", 0x20, 0x20) + struct.pack("<II", 1, 1) + clip
    clips = read_pack(pack)
    check("pack: two slots, one clip", len(clips) == 1 and clips[0][0] == [0, 1])
    c = clips[0][1]
    check("clip header", (c.name, c.frames, len(c.channels), c.end_marker)
          == ("TEST", 11, 6, True), repr((c.name, c.frames, len(c.channels))))
    try:
        parse_clip(pack + b"\0\0\0\0", 0x20, strict=True)
        check("strict: a clip that stops short is refused", False)
    except FcvError:
        check("strict: a clip that stops short is refused", True)
    # FCV2_getPackAttribute: a record that is not type 4 ends the stream, and
    # what follows an end is not read (allowed unless strict)
    try:
        padded = parse_clip(pack + b"\0" * 8, 0x20)
        zero = parse_clip(pack[:-8] + b"\0" * 8 + struct.pack("<HHHH", 4, 4, 0, 0), 0x20)
        check("an end followed by padding or a zero record is read",
              len(padded.channels) == 6 and padded.end_marker
              and len(zero.channels) == 6 and not zero.end_marker)
    except FcvError as e:
        check("an end followed by padding or a zero record is read", False, str(e))
    try:
        parse_clip(pack[:-8] + b"\0" * 4, 0x20)
        check("a stream that runs out mid-record is refused", False)
    except FcvError:
        check("a stream that runs out mid-record is refused", True)
    try:
        parse_clip(pack[:-8] + struct.pack("<HHHHH", 12, 4, 0, 0, 0), 0x20)
        check("a block record cut by the clip's end is refused", False)
    except FcvError:
        check("a block record cut by the clip's end is refused", True)
    try:
        parse_clip(pack[:-12], 0x20)
        check("a cut clip is refused", False)
    except (FcvError, struct.error):
        check("a cut clip is refused", True)
    rig = parse_rig(jnt)
    check("skeleton", (rig.name, rig.types) == ("TEST", [1, 1, 3, 2, 2, 4]))
    check("clip lines up with its skeleton", fits(rig, c))
    other = parse_rig(_mk_jnt(b"TEST", [(2, 0, (0, 0, 0), (0, 0, 0), 0.0)] * 6))
    check("and not with another", not fits(other, c))
    st: dict = {}
    worst = bones = 0.0
    for f in range(11):
        w = pose(rig, c, float(f), st)
        tgt, end, hip, knee = w[1][2], w[5][2], w[2][2], w[4][2]
        worst = max(worst, math.dist(tgt, end))
        bones = max(bones, abs(math.dist(hip, knee) - 0.5), abs(math.dist(knee, end) - 0.5))
    check("two-bone chain reaches its target", worst < 1e-5, "%.2e" % worst)
    check("bone lengths kept", bones < 1e-5, "%.2e" % bones)
    check("solver ran every frame", st.get("solved_3") == 11, repr(st))
    rest = pose(rig, None, 0.0)
    check("rest pose", all(abs(a - b) < 1e-6 for a, b in zip(rest[5][2], (1.0, 1.0, 0.0))),
          repr(rest[5][2]))

    # glTF: animations land on the armature nodes of a .glb
    nodes = [{"name": "m", "mesh": 0}] + [{"name": "b%d" % i, "translation": list(t)}
                                          for i, (_, _, t, _, _) in enumerate(
                                              [(1, 0, (0, 0, 0), 0, 0), (1, 0, (0, 0, 0), 0, 0),
                                               (3, 0, (0, 1.0, 0), 0, 0), (2, 2, (0, 0, 0), 0, 0),
                                               (2, 3, (0.5, 0, 0), 0, 0), (4, 4, (0.5, 0, 0), 0, 0)])]
    nodes.append({"name": "skeleton", "children": [1]})
    glb = _write_glb({"asset": {"version": "2.0"}, "nodes": nodes}, bytearray())
    out, summary = add_to_glb(glb, rig, [("test#00,01", c)])
    js, blob = _read_glb(out)
    an = js["animations"][0]
    ok = (an["name"] == "test#00,01" and summary[0]["written"]
          and all(1 <= ch["target"]["node"] <= 6 for ch in an["channels"])
          and all(v["byteOffset"] + v["byteLength"] <= len(blob) for v in js["bufferViews"]))
    check("glTF animation written", ok, "%d channels" % len(an["channels"]))
    moving = {ch["target"]["node"] for ch in an["channels"]
              if js["accessors"][an["samplers"][ch["sampler"]]["output"]]["count"] == 11}
    check("the moving blocks are keyed per frame", {2, 4} <= moving, repr(sorted(moving)))

    # the first link of a solved two-bone chain without a pivot: its X rotation
    # turns the leg about the hip-to-target line (JNT_constructMatrix)
    def leg_clip(roll):
        return parse_clip(_mk_clip(b"TEST", 11, [
            (0, 0x1FF, None, [_mk_curve()] * 6 + [_mk_curve(const=1.0)] * 3),
            (0, 0x1FF, None, [_mk_curve(const=0.6), _mk_curve(const=0.4), _mk_curve()]
             + [_mk_curve()] * 3 + [_mk_curve(const=1.0)] * 3),
            (0, 0x3F, None, [_mk_curve(), _mk_curve(const=1.0), _mk_curve()] + [_mk_curve()] * 3),
            (0, 0x1F8, None, [_mk_curve()] * 2 + [_mk_curve(const=roll)]
             + [_mk_curve(const=1.0)] * 3),
            (0, 0x1F8, None, [_mk_curve()] * 3 + [_mk_curve(const=1.0)] * 3),
            (5, 0, (1, 0, 0), []),
        ]), 0)
    w0, w1 = pose(rig, leg_clip(0.0), 0.0), pose(rig, leg_clip(0.7), 0.0)
    hip, tgt = w0[2][2], w0[1][2]
    axis = [b - a for a, b in zip(hip, tgt)]
    al = math.sqrt(sum(x * x for x in axis))
    axis = [x / al for x in axis]

    def off(p):
        v = [b - a for a, b in zip(hip, p)]
        d = sum(x * y for x, y in zip(v, axis))
        return [x - d * y for x, y in zip(v, axis)]
    a, b = off(w0[4][2]), off(w1[4][2])
    turn = math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))
                                   / math.sqrt(sum(x * x for x in a) * sum(x * x for x in b)))))
    check("leg: the first link's X rotation turns the knee", abs(turn - 0.7) < 1e-5
          and math.dist(w1[5][2], tgt) < 1e-5, "%.5f rad" % turn)

    # body, inherit and hair: block 0 stays the unit matrix when a body record
    # carries the inherit flag; the inherit block's height is rescaled; a type-0
    # body block is posed like type 1; a type-1 block's scale reaches its
    # children; hair blocks keep their rest pose whatever the clip stores
    hj = parse_rig(_mk_jnt(b"HAIR", [
        (1, 0, (0, 0, 0), (0, 0, 0), 0.0),            # 0 root
        (1, 0, (0, 1.0, 0), (0, 0, 0), 0.0),          # 1
        (0, 1, (0, 1.0, 0), (0, 0, 0), 0.0),          # 2 type 0
        (1, 2, (0, 1.0, 0), (0, 0, 0), 0.0),          # 3
        (1, 3, (0, 0.5, 0), (0, 0, 0), 0.0),          # 4 inherit block
        (1, 4, (1.0, 0, 0), (0, 0, 0), 0.0),          # 5
        (1, 5, (0, 0, 1.0), (0, 0, 0), 0.0),          # 6 hair (simulated)
    ], primary=6, secondary=1, tag=(2, 0x870000)))
    t3 = lambda x, y, z: [_mk_curve(const=x), _mk_curve(const=y), _mk_curve(const=z)]
    hc = parse_clip(_mk_clip(b"HAIR", 2, [
        (0, 0x1FF, None, t3(5, 0, 0) + t3(0, 0, 0) + t3(1, 1, 1)),
        (0, 0x1FF, None, t3(0, 2, 0) + t3(0, 0, 0) + t3(1, 1, 1)),
        (0, 0x1FF, None, t3(0, 1, 0) + t3(0, 0, 0) + t3(1, 1, 1)),
        (0, 0x1FF, None, t3(0, 1, 0) + t3(0, 0, 0) + t3(1, 1, 1)),
        (0x8000, 0x3FF, None, t3(0, 0.5, 0) + t3(0, 0, 0) + t3(2, 2, 2) + [_mk_curve(const=1)]),
        (0, 0x1FF, None, t3(1, 0, 0) + t3(0, 0, 0) + t3(3, 3, 3)),
        (0, 0x1FF, None, t3(0, 0, 9) + t3(0, 0, 0) + t3(1, 1, 1)),
    ]), 0)
    hw = pose(hj, hc, 0.0)
    check("hair id from the type-5 record", (hj.hair, fits(hj, hc)) == (6, True),
          repr((hj.hair, fits(hj, hc))))
    check("inherit: block 0 stays the unit matrix", hw[0] == _IDENT, repr(hw[0]))
    check("type-0 body block posed like type 1", hw[2] is not None
          and abs(hw[2][2][1] - 3.0) < 1e-6, repr(hw[2] and hw[2][2]))
    # (4 + 0.5) * (block 3's .jnt Y 1.0 / block 3's posed Y 4.0)
    check("inherit block's height rescaled", abs(hw[4][2][1] - 1.125) < 1e-6,
          repr(hw[4][2]))
    check("type-1 scale reaches the children", abs(hw[5][2][0] - 2.0) < 1e-6
          and abs(hw[6][2][2] - 6.0) < 1e-6, repr((hw[5][2], hw[6][2])))
    check("hair block keeps its rest pose", abs(hw[6][2][2] - 6.0) < 1e-6
          and 6 not in bake(hj, hc)["tracks"], repr(hw[6][2]))
    # after the body: JNT_getVal2 runs only for additional_primary_count
    # blocks, reading the records after the body's; the rest are static
    two = parse_clip(_mk_clip(b"HAIR", 2, [(0, 0x1FF, None, t3(0, 0, 0) * 2 + t3(1, 1, 1)),
                                           (0, 0x1FF, None, t3(5, 7, 9) + t3(0, 0, 0)
                                            + t3(1, 1, 1))]), 0)
    blk = [(1, 0, (0, 0, 0), (0, 0, 0), 0.0), (1, 0, (0, 1.0, 0), (0, 0, 0), 0.0)]
    static = parse_rig(_mk_jnt(b"HAIR", blk, primary=1))
    driven = parse_rig(_mk_jnt(b"HAIR", blk, primary=1, aprim=1))
    ws, wd = pose(static, two, 0.0), pose(driven, two, 0.0)
    check("blocks after additional_primary_count stay static",
          fits(static, two) and ws[1][2] == (0.0, 1.0, 0.0) and 1 not in bake(static, two)["tracks"]
          and wd[1][2] == (5.0, 7.0, 9.0), repr((ws[1][2], wd[1][2])))
    after = parse_rig(_mk_jnt(b"HAIR", [(1, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                        (0, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                        (1, 0, (0, 1.0, 0), (0, 0, 0), 0.0)],
                              primary=1, aprim=2))
    three = parse_clip(_mk_clip(b"HAIR", 2, [(0, 0x1FF, None, t3(0, 0, 0) * 2 + t3(1, 1, 1))] * 2
                                + [(0, 0x1FF, None, t3(5, 0, 0) + t3(0, 0, 0) + t3(1, 1, 1))]), 0)
    check("type 0 after the body reads no values: JNT_getVal2 stops there",
          _record_map(after, three) == {0: 0} and pose(after, three, 0.0)[2][2] == (0.0, 1.0, 0.0))
    # the JNT_getVal2 blocks of a skeleton with hair take the records right
    # after the body's (EXM_CalcExMotion reads none)
    hx = parse_rig(_mk_jnt(b"HAIR", [(1, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                     (1, 0, (0, 0, 0), (0, 0, 0), 0.0),     # hair, simulated
                                     (1, 0, (0, 1.0, 0), (0, 0, 0), 0.0)],
                           primary=1, secondary=1, tag=(1, 0x008A0000), aprim=1))
    check("JNT_getVal2 reads on from the record after the body's",
          _record_map(hx, two) == {0: 0, 2: 1} and pose(hx, two, 0.0)[2][2] == (5.0, 7.0, 9.0))
    # JNT_getVal2 type 1 without the inherit flag keeps the scale components
    # its record leaves out from the block before (work->channel_scale)
    sj = parse_rig(_mk_jnt(b"HAIR", [(1, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                     (5, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                     (1, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                     (1, 2, (1.0, 0, 0), (0, 0, 0), 0.0)],
                           primary=1, aprim=3))
    sc_clip = parse_clip(_mk_clip(b"HAIR", 2, [
        (0, 0x1FF, None, t3(0, 0, 0) * 2 + t3(1, 1, 1)),
        (0, 0x1FF, None, t3(0, 0, 0) * 2 + t3(2, 1, 1)),
        (2, 0x080, None, [_mk_curve(const=3.0)])]), 0)
    sw = pose(sj, sc_clip, 0.0)
    check("JNT_getVal2: type 1 keeps the channel scale it does not store",
          sw[2][1] == (2.0, 3.0, 1.0) and abs(sw[3][2][0] - 2.0) < 1e-9, repr(sw[2][1]))
    # a root (inherit) record: the flag follows the scales when mask bit 7 is
    # set (get_fcvpack_flag gets the mask shifted right by 7); bit 9 alone
    # makes the game read nothing more
    c80 = parse_clip(_mk_clip(b"HAIR", 2, [
        (0x8000, 0x0080, None, [_mk_curve(const=2.0), _mk_curve(const=1.0)]),
        (0, 0x0001, None, [_mk_curve(const=0.5)])]), 0)
    c200 = parse_clip(_mk_clip(b"HAIR", 2, [
        (0x8000, 0x0200, None, []), (0, 0x0001, None, [_mk_curve(const=0.5)])]), 0)
    check("root record: the flag is read on bit 7, not bit 9",
          (len(c80.channels), sorted(c80.channels[0].curves), c80.channels[0].curves[7].value,
           len(c200.channels), sorted(c200.channels[0].curves))
          == (2, [7, 9], 2.0, 2, []) and fits(hj, c80) and fits(hj, c200))
    check("a clip with no records is the rest pose",
          fits(hj, parse_clip(_mk_clip(b"HAIR", 2, []), 0)))
    check("attribute key: 31 * word 0 + word 1 of the 8 name bytes",
          _attribute_key(b"AAAAaaaa") == _attribute_key(b"BAAABaaa")
          and _attribute_key(b"A\0\0\0\0\0\0\0") != _attribute_key(b"A\0\0\0B\0\0\0")
          and hj.attribute_key == hc.attribute_key == _attribute_key(b"HAIR\0\0\0\0"))

    # EXM_CalcExMotion: EXM_PresetHair's types for KOS-MOS (the back sheet is
    # type 3 with the ALT_SHAPES collision against the back spheres), and a
    # chain held out sideways from its root sags under type 5's gravity (the
    # pull toward the rigid chain is 0.9 * 0.7 / 1.7 a tick at the tip: about
    # 0.014 m), keeps its link length, and gets a track of its own
    kt, kc, _ks = exm_preset("KOSMOS")
    check("EXM_PresetHair: KOSMOS chain types", kc == "kosmos" and kt == {
        0: (_ALT, 3), 1: (0, 5), 2: (0, 5), 3: (_KIND5, 6), 4: (_KIND5, 6), 5: (0, 8)}, repr(kt))
    check("EXM_PresetHair: an unknown name is EX_HAIR_TYPE_NONE", exm_preset("QQ") == (None, None, None))
    xj = parse_rig(_mk_jnt(b"ZZ", [(1, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                   (3, 0, (0, 2.0, 0), (0, 0, 0), 0.0),
                                   (2, 1, (0, 0, 0), (0, 0, 0), 0.0),
                                   (2, 2, (0.5, 0, 0), (0, 0, 0), 0.0),
                                   (4, 3, (0.5, 0, 0), (0, 0, 0), 0.0)],
                           primary=1, secondary=4, tag=(1, 0x008A0000)))
    xc = parse_clip(_mk_clip(b"ZZ", 2, [(0, 0x1FF, None, t3(0, 0, 0) * 2 + t3(1, 1, 1))]), 0)
    xw = simulate(xj, xc, [xc.first_frame])[0]
    link = math.dist(xw[3][2], xw[4][2])
    check("EXM: a sideways chain falls", (xj.hair, exm_chains(xj)[0][:2]) == (1, (1, [2, 3, 4]))
          and 2.0 - 0.02 < xw[4][2][1] < 2.0 - 0.01 and abs(link - 0.5) < 1e-3,
          repr((xw[3][2], xw[4][2], link)))
    check("EXM: simulated links are baked", 3 in bake(xj, xc)["tracks"])

    # EXM_CheckSkirtCollision, EX_HAIR_SKIRT_0 (depth response 0.2): the
    # hip's y axis and block 36's z axis face +X, both thighs hang straight
    # down at x = 0.1, the foot radius is 0.05, so the front edge is at 0.125.
    # A point at x = 0 between the legs moves 0.2 of the way there in section
    # 0 (to 0.025), then again in section 1 (whose shins reach y = 0: to
    # 0.045); a point already in front of the legs stays where it is
    sw = [None] * 37
    hold = lambda r, t: (r, (1.0, 1.0, 1.0), t)
    for j in (11, 14):
        sw[j] = hold(_I3, (0.0, 0.0, 0.0))
    sw[13] = hold((0.0, 1.0, 0.0, -1.0, 0.0, 0.0, 0.0, 0.0, 1.0), (0.0, 1.0, 0.0))
    sw[36] = hold((0.0, 0.0, 1.0, 0.0, 1.0, 0.0, -1.0, 0.0, 0.0), (0.0, 1.2, 0.0))
    for z, top, knee, shin, low in ((0.1, 16, 17, 18, 20), (-0.1, 26, 27, 28, 30)):
        sw[top] = hold(_I3, (0.1, 1.0, z))
        sw[knee] = sw[shin] = hold(_I3, (0.1, 0.5, z))
        sw[low] = hold(_I3, (0.1, 0.0, z))
    sizes = (1.0, 1.0, 1.0, _f32(0.05))
    inside, outside = [0.0, 0.75, 0.0], [0.3, 0.75, 0.0]
    _skirt_collide(sw, inside, (0.0, 1.0, 0.0), _S0, sizes)
    _skirt_collide(sw, outside, (0.0, 1.0, 0.0), _S0, sizes)
    check("EXM skirt: a point between the legs is pushed out in front of them",
          abs(inside[0] - 0.045) < 1e-6 and inside[1:] == [0.75, 0.0]
          and outside == [0.3, 0.75, 0.0], repr((inside, outside)))
    check("EXM coat width: halfway to 0.25 from the joint",
          _coat_pull(0.25, (1.0, 0.0, 0.0), (0.0, 0.0, 0.0)) == [0.625, 0.0, 0.0])
    jt, _jc, js = exm_preset("JR")
    check("EXM_PresetHair: Jr.'s coat keeps its skirt flags and sizes",
          jt[3] == (_S45 | _COAT, 13) and jt[1] == (_S90, 13) and js == (
              _f32(1.4), 1.0, 1.0, _f32(0.05)), repr((jt, js)))

    # a parent scaled unevenly (2, 1, 1) and a child turned 90 degrees about Z
    # under it: the game's child basis is Rz * diag(2, 1, 1), which no TRS
    # under the parent can hold, so the child is hung off the skeleton node
    # and keyed in skeleton space; child-space (1, 0, 0) must land on (0, 2, 0)
    nj = parse_rig(_mk_jnt(b"NU", [(1, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                   (5, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                   (1, 1, (0, 0, 0), (math.pi / 2, 0, 0), 0.0)], primary=3))
    nc = parse_clip(_mk_clip(b"NU", 2, [(0, 0x1FF, None, t3(0, 0, 0) * 2 + t3(1, 1, 1)),
                                        (0, 0x1FF, None, t3(0, 0, 0) * 2 + t3(2, 1, 1))]), 0)
    nn = [{"name": "m", "mesh": 0}, {"name": "b0", "children": [2]},
          {"name": "b1", "children": [3]},
          {"name": "b2", "rotation": [0.0, 0.0, math.sin(math.pi / 4), math.cos(math.pi / 4)]},
          {"name": "skeleton", "children": [1]}]
    nglb, nsum = add_to_glb(_write_glb({"asset": {"version": "2.0"}, "nodes": nn}, bytearray()),
                            nj, [("nu", nc)])
    njs, nblob = _read_glb(nglb)

    def first_value(acc):
        a = njs["accessors"][acc]
        v = njs["bufferViews"][a["bufferView"]]
        w = {"VEC3": 3, "VEC4": 4}[a["type"]]
        return struct.unpack_from("<%df" % w, nblob, v["byteOffset"])
    trs = {i: dict(nd) for i, nd in enumerate(njs["nodes"])}
    an = njs["animations"][0]
    for chn in an["channels"]:
        trs[chn["target"]["node"]][chn["target"]["path"]] = first_value(
            an["samplers"][chn["sampler"]]["output"])
    up = {c: i for i, nd in enumerate(njs["nodes"]) for c in nd.get("children", ())}

    def place(i, pt):
        while i is not None:
            m, t = _trs_matrix(trs[i])
            pt = tuple(x + y for x, y in zip(_mv(m, pt), t))
            i = up.get(i)
        return pt
    got = place(3, (1.0, 0.0, 0.0))
    check("uneven parent scale: the child is keyed in skeleton space",
          nsum[0]["written"] and up.get(3) == 4 and math.dist(got, (0.0, 2.0, 0.0)) < 1e-5,
          repr((got, up.get(3))))

    # one clip that cannot be posed does not take the others with it
    cj = parse_rig(_mk_jnt(b"CH", [(1, 0, (0, 0, 0), (0, 0, 0), 0.0),
                                   (2, 0, (0, 0, 0), (0, 0, 0), 0.0)]))
    good = parse_clip(_mk_clip(b"CH", 2, [(0, 0x1FF, None, t3(1, 0, 0) + t3(0, 0, 0)
                                           + t3(1, 1, 1))]), 0)
    broken = parse_clip(_mk_clip(b"CH", 2, [(0, 0x1FF, None, t3(1, 0, 0) + t3(0, 0, 0)
                                             + t3(1, 1, 1)),
                                            (0, 0x1F8, None, t3(0, 0, 0) + t3(1, 1, 1))]), 0)
    cn = [{"name": "m", "mesh": 0}, {"name": "b0", "children": [2]}, {"name": "b1"},
          {"name": "skeleton", "children": [1]}]
    _, csum = add_to_glb(_write_glb({"asset": {"version": "2.0"}, "nodes": cn}, bytearray()),
                         cj, [("good", good), ("broken", broken)])
    check("a clip that cannot be posed is left out, the others written",
          [c["written"] for c in csum] == [True, False], repr(csum))

    # damaged input: a header cut short, two keys at one time, a value that is not finite
    try:
        parse_clip(b"FCV\0" + struct.pack("<HHHHh", 3, 2, 0, 1, 0) + b"\0\0", 0)
        check("a cut attribute size is refused", False)
    except FcvError:
        check("a cut attribute size is refused", True)
    dup = Curve(3, 3, 0.0, [(0, 100, 0.0, 0, 0), (0, 100, 1.0, 0, 0), (0, 200, 2.0, 0, 0)])
    try:
        v = eval_curve(dup, 1 / 30.0, 100 / 30.0 + 0.75 * STEP)
        check("two keys at one time", math.isfinite(v), repr(v))
    except ZeroDivisionError:
        check("two keys at one time", False, "ZeroDivisionError")
    bad = parse_clip(_mk_clip(b"TEST", 11, [
        (0, 0x1FF, None, [_mk_curve(const=float("nan"))] + [_mk_curve()] * 5
         + [_mk_curve(const=1.0)] * 3)]), 0)
    _, summ = add_to_glb(glb, rig, [("nan", bad)])
    check("a clip whose pose is not finite is left out", not summ[0]["written"],
          summ[0].get("reason", ""))
    return res


PARTY = (("motion\\kosmos_cf.fpk", "char\\pc\\kosmos.jnt"),
         ("motion\\shion_cf.fpk", "char\\pc\\shion1.jnt"),
         ("motion\\ziggy_cf.fpk", "char\\pc\\ziggy.jnt"),
         ("motion\\momo_cf.fpk", "char\\pc\\momo.jnt"),
         ("motion\\chaos_cf.fpk", "char\\pc\\chaos.jnt"),
         ("motion\\jr_cf.fpk", "char\\pc\\jr.jnt"))


def selftest(iso: Optional[str] = None) -> int:
    """0 when every check passes.  Without a disc: the hand-made packs.  With
    one: the six party members' field packs read to their last byte, line up
    with their skeletons, solve exactly their two legs at every frame, and
    every solved chain that is unscaled and can reach its target lands on
    it (at least one must be measured); and KOS-MOS's run goes through
    ``simulate`` (see ``_check_run_hair``)."""
    bad = 0
    if iso is None:
        for name, ok, detail in self_test():
            print("%-4s %s%s" % ("ok" if ok else "FAIL", name, ("  " + detail) if detail else ""))
            bad += not ok
        return 1 if bad else 0
    from repack import read_entry
    for pack_path, jnt_path in PARTY:
        rig = parse_rig(read_entry(iso, 0, jnt_path))
        clips = read_pack(read_entry(iso, 0, pack_path), strict=True)
        fit = [c for _, c in clips if c.attribute_key == rig.attribute_key and fits(rig, c)]
        stats: dict = {}
        frames = 0
        for c in fit:
            for f in range(c.first_frame, c.last_frame + 1):
                pose(rig, c, float(f), stats)
                frames += 1
        # each frame solves exactly the two legs (two-link chains); the miss
        # is measured on the solved chains that are unscaled and in reach
        solved = sum(v for k, v in stats.items() if k.startswith("solved_"))
        measured = stats.get("measured", 0)
        miss = stats.get("worst_miss", float("inf"))
        ok = (len(fit) == len(clips) and frames > 0
              and stats.get("solved_3", 0) == 2 * frames and solved == 2 * frames
              and measured > 0 and miss < 1e-4)
        bad += not ok
        print("%-4s %s on %s: %d of %d clips line up, %d frames posed, %d chains solved, "
              "%d of them unscaled and in reach, worst miss over those %.1e m"
              % ("ok" if ok else "FAIL", pack_path, jnt_path, len(fit), len(clips), frames,
                 solved, measured, miss))
    for check in (_check_run_hair, _check_coat):
        ok, detail = check(iso)
        bad += not ok
        print("%-4s %s" % ("ok" if ok else "FAIL", detail))
    return 1 if bad else 0


# the KOS-MOS run (kosmos_cf slot 3) and her back sheet: the chain from block
# 95 (type 3 with ALT_SHAPES, length_response 1.0); block 99 is its last
# link with weights and block 42 her chest, whose +y points the way she faces
RUN_CLIP = ("motion\\kosmos_cf.fpk", 3, "char\\pc\\kosmos.jnt")


def _check_run_hair(iso) -> tuple[bool, str]:
    """``simulate`` on disc data: every simulated block finite at every
    frame; block 99 behind the chest (y < 0 in block 42's space) at every
    frame, where the rigid chain puts it through her torso; and, with the
    collision tables left out, every link of a chain whose length_response
    is 1.0 keeps its length to 1e-3.  The collision push comes after the
    length step in EXM_CalcExMotion, so with it a link stretches (up to
    about 8 mm here) in the game's own arithmetic as well."""
    from repack import read_entry
    pack_path, slot, jnt_path = RUN_CLIP
    rig = parse_rig(read_entry(iso, 0, jnt_path))
    clip = next((c for sl, c in read_pack(read_entry(iso, 0, pack_path), strict=True)
                 if slot in sl), None)
    if clip is None or not fits(rig, clip):
        return False, "%s#%02d does not line up with %s" % (pack_path, slot, jnt_path)
    frames = list(range(clip.first_frame, clip.last_frame + 1))
    chains = exm_chains(rig)
    back = next((ch for ch in chains if ch[0] == 95), None)
    if back is None or 99 not in back[1] or len(rig.types) <= 99:
        return False, "%s has no back-sheet chain from block 95" % jnt_path
    worlds = simulate(rig, clip, frames, chains=chains)
    blocks = sorted({b for _r, queued, *_ in chains for b in queued})
    finite = all(w[b] is not None and all(math.isfinite(x) for x in w[b][0] + w[b][1] + w[b][2])
                 for w in worlds for b in blocks)

    def depth(w, b, a):
        m, p = w[a], w[b][2]
        return _mtv(m[0], (p[0] - m[2][0], p[1] - m[2][1], p[2] - m[2][2]))[1]

    behind = max(depth(w, 99, 42) for w in worlds) if finite else float("inf")
    rigid = max(depth(pose(rig, clip, float(f)), 99, 42) for f in frames)
    free = simulate(rig, clip, frames, chains=[(r, q, fl, k, None) for r, q, fl, k, _t in chains])
    stretch = 0.0
    for r, queued, _fl, kind, _t in chains:
        if _HAIRPARA[kind][1] != 1.0:
            continue
        for k in range(1, len(queued)):
            b, a = queued[k], queued[k - 1]
            for w in free:
                stretch = max(stretch, abs(math.dist(w[b][2], w[a][2])
                                           - rig.translations[b][0] * w[b][1][0]))
    ok = finite and behind < 0.0 and stretch < 1e-3
    return ok, ("%s#%02d on %s, simulated: %d frames, %d chains, %s; block 99 at least "
                "%.3f m behind the chest at every frame (the rigid chain: up to %.3f m in "
                "front); worst link stretch "
                "with no collision %.1e m"
                % (pack_path, slot, jnt_path, len(frames), len(chains),
                   "every block finite" if finite else "a block NOT finite",
                   -behind, rigid, stretch))


# Jr.'s field clip 2 and his coat: five skirt chains from block 74 (types 13,
# two of them with EX_HAIR_FLAG_COAT_WIDTH), against his legs (blocks 16 to
# 18 and 26 to 28)
COAT_CLIP = ("motion\\jr_cf.fpk", 2, "char\\pc\\jr.jnt")


def _seg_dist(p, a, b):
    ab = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    ln = ab[0] * ab[0] + ab[1] * ab[1] + ab[2] * ab[2]
    t = 0.0 if ln == 0.0 else max(0.0, min(1.0, sum(ab[i] * (p[i] - a[i]) for i in range(3)) / ln))
    return math.dist(p, (a[0] + ab[0] * t, a[1] + ab[1] * t, a[2] + ab[2] * t))


def _check_coat(iso) -> tuple[bool, str]:
    """``simulate`` with the skirt collision on disc data: every coat block
    finite at every frame, and the coat's closest approach to either leg at
    least 4 cm (held at its rest pose it comes within 2.5 cm)."""
    from repack import read_entry
    pack_path, slot, jnt_path = COAT_CLIP
    rig = parse_rig(read_entry(iso, 0, jnt_path))
    clip = next((c for sl, c in read_pack(read_entry(iso, 0, pack_path), strict=True)
                 if slot in sl), None)
    if clip is None or not fits(rig, clip):
        return False, "%s#%02d does not line up with %s" % (pack_path, slot, jnt_path)
    frames = list(range(clip.first_frame, clip.last_frame + 1))
    chains = exm_chains(rig)
    coat = [b for ch in chains if ch[2] & _SKIRT for b in ch[1][1:]]
    if len(coat) < 10 or not any(ch[2] & _COAT for ch in chains):
        return False, "%s has no coat chains" % jnt_path

    def closest(worlds):
        return min(_seg_dist(w[b][2], w[a][2], w[e][2])
                   for w in worlds for b in coat for a, e in ((16, 18), (26, 28)))

    worlds = simulate(rig, clip, frames, chains=chains)
    finite = all(all(math.isfinite(x) for x in w[b][2]) for w in worlds for b in coat)
    near = closest(worlds) if finite else 0.0
    rest = closest(simulate(rig, clip, frames,
                            chains=[ch for ch in chains if not ch[2] & _SKIRT]))
    ok = finite and near >= 0.04 and rest < 0.025
    return ok, ("%s#%02d on %s: %d frames, %d coat blocks, %s, closest to a leg %.3f m "
                "(%.3f m held at rest)"
                % (pack_path, slot, jnt_path, len(frames), len(coat),
                   "every block finite" if finite else "a block NOT finite", near, rest))


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Episode I motion packs (.fpk of FCV clips)")
    ap.add_argument("pack", nargs="?", help="a .fpk file (as extracted) to list")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        bad = 0
        for name, ok, detail in self_test():
            print("%-4s %s%s" % ("ok" if ok else "FAIL", name, ("  " + detail) if detail else ""))
            bad += not ok
        return 1 if bad else 0
    if not a.pack:
        ap.error("give a .fpk file or --self-test")
    try:
        entries = read_pack(_load(a.pack))
    except (FcvError, ValueError, struct.error, OSError) as e:
        print("%s: %s" % (a.pack, e))
        return 1
    for slots, c in entries:
        print("%-12s %-8s %4d frames  %3d blocks  %5d keys%s" % (
            ",".join("%02d" % s for s in slots), c.name or "-", c.frames,
            len(c.channels), c.key_count(),
            "".join("  %s range %d-%d" % ("play" if r[0] & 0x80 else "reset", r[2], r[3])
                    for r in c.ranges)))
    return 0


# ---------------------------------------------------------------------------
# cli.py model / motion
# ---------------------------------------------------------------------------

def find_in_dump(dump, path: str):
    """A dump file from its in-game path (``char\\pc\\kosmos.lex``), with or
    without the ``chainN`` folder; case does not matter."""
    from pathlib import Path
    dump = Path(dump)
    parts = [p for p in path.replace("\\", "/").split("/") if p]
    if Path(path).is_file():
        return Path(path)
    if not dump.is_dir():
        return None
    roots = [dump] + sorted(d for d in dump.iterdir() if d.is_dir())
    for root in roots:
        cur = root
        for part in parts:
            if not cur.is_dir():
                cur = None
                break
            hit = [c for c in cur.iterdir() if c.name.lower() == part.lower()]
            if not hit:
                cur = None
                break
            cur = hit[0]
        if cur is not None and cur.is_file():
            return cur
    return None


def cli_model(dump, path: str, out: str, motions: Optional[list], no_motion: bool) -> int:
    from pathlib import Path
    import browse
    import lex
    dump = Path(dump)
    if not dump.is_dir() and not Path(path).is_file():
        print("no dump folder at %s (give the dump folder of an extracted disc)" % dump)
        return 2
    specs = []
    if not no_motion:
        for spec in motions or ():
            ppath, hashed, slots = spec.partition("#")
            try:
                parts = [x.strip() for x in slots.split(",")] if hashed else []
                if hashed and (not parts or not all(parts)):
                    raise ValueError(slots)
                want = {int(x) for x in parts} if hashed else None
            except ValueError:
                print("bad slot list '%s' in --motion %s (give slot numbers, e.g. #1,5)"
                      % (slots, spec))
                return 2
            pk = find_in_dump(dump, ppath)
            if pk is None:
                print("no pack %s in %s" % (ppath, dump))
                return 2
            try:
                entries = read_pack(_load(pk))
            except (FcvError, ValueError, struct.error, OSError) as e:
                print("%s: %s" % (ppath, e))
                return 1
            have = sorted({x for sl, _ in entries for x in sl})
            missing = want - set(have) if want is not None else set()
            if missing:
                print("pack %s has no slot %s (its slots are %s)" % (
                    ppath, ",".join(str(x) for x in sorted(missing)),
                    "%d-%d" % (have[0], have[-1]) if have else "none"))
                return 2
            specs.append((pk, want, entries))
    src = find_in_dump(dump, path)
    if src is None or src.suffix.lower() != ".lex":
        print("no model %s in %s (give the in-game path of a .lex, as "
              "`cli.py list` prints it)" % (path, dump))
        return 2
    builder = browse.ModelBuilder(dump, dump.parent / "browse" / "code")
    try:
        built = builder.build(src)
    except lex.LexError as e:
        print("%s: %s" % (src.relative_to(dump), e))
        return 1
    if built is None:
        print("%s: nothing to draw" % src.relative_to(dump))
        return 1
    glb, model, tex, rig = built
    print("%s: %d meshes, %d triangles, %s" % (
        src.relative_to(dump), len([m for m in model.meshes if m.triangles]),
        model.triangle_count, "textured" if tex else "no texture"))
    summary: list = []
    failed = False
    if not no_motion:
        if rig is None:
            jnt = src.with_suffix(".jnt")
            if not jnt.is_file():
                print("  no motion: the model has no .jnt skeleton beside it")
            elif jnt.with_suffix(".pose.json").is_file():
                print("  no motion: a captured pose (%s) sits beside the .jnt, and the "
                      "model is written in that pose" % jnt.with_suffix(".pose.json").name)
            else:
                print("  no motion: the .jnt skeleton beside it could not be read")
        else:
            sources = []
            index = None
            if specs:
                for pk, want, entries in specs:
                    label = pack_label(pk, dump) if dump in pk.parents else pk.stem
                    for sl, clip in entries:
                        if want is not None:
                            sl = [s for s in sl if s in want]
                            if not sl:
                                continue
                        name = clip_name(label, sl)
                        if clip.attribute_key != rig.attribute_key:
                            name += " [made for %s]" % (clip.name or "an unnamed skeleton")
                        sources.append((name, clip))
            else:
                index = MotionIndex(dump)
                sources = index.clips_for(rig)
                for fp, why in index.failed:
                    print("  pack %s not read: %s" % (
                        fp.relative_to(dump) if dump in fp.parents else fp, why))
            if sources:
                try:
                    glb, summary = add_to_glb(glb, rig, sources)
                except (FcvError, ValueError, ZeroDivisionError, IndexError) as e:
                    print("  no motion: %s" % e)
                    failed = bool(specs)
            elif index is not None and index.failed:
                print("  no motion: no pack that could be read names the skeleton %s "
                      "(%d not read)" % (rig.name or "(unnamed)", len(index.failed)))
            else:
                print("  no motion: no pack names the skeleton %s" % (rig.name or "(unnamed)"))
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_bytes(glb)
    written = [c for c in summary if c["written"]]
    for c in summary:
        if c["written"]:
            print("  %-34s %4d frames  %3d channels" % (c["name"], c["frames"], c["channels"]))
        else:
            print("  %-34s left out: %s" % (c["name"], c["reason"]))
    print("%s: %d bytes, %d animations" % (out, len(glb), len(written)))
    # clips named with --motion that could not be written fail the command
    # (the .glb is still written with the others); the automatic choice
    # does not
    if specs and (failed or len(written) < len(summary)):
        print("  not every clip asked for with --motion was written")
        return 1
    return 0


def cli_motion(dump, pack: Optional[str], do_sweep: bool) -> int:
    from pathlib import Path
    dump = Path(dump)
    if not dump.is_dir() and not (pack and Path(pack).is_file()):
        print("no dump folder at %s (give the dump folder of an extracted disc)" % dump)
        return 2
    if pack:
        pk = find_in_dump(dump, pack)
        if pk is None:
            print("no pack %s in %s" % (pack, dump))
            return 2
        return main([str(pk)])
    if not do_sweep:
        print("give --pack PATH to list one pack, or --sweep to read them all")
        return 2
    st = sweep(dump)
    reasons = st.pop("unread_reasons", {})
    print("packs                               %6d (%d unread)" % (
        st.get("packs", 0), st.get("packs_unread", 0)))
    print("clips                               %6d" % st.get("clips", 0))
    print("  read to their last byte           %6d" % st.get("clips_read", 0))
    print("  not read                          %6d" % st.get("clips_unread", 0))
    for why, n in sorted(reasons.items()):
        print("    %-32s %6d" % (why, n))
    print("  frames / keys                     %6d / %d" % (st.get("frames", 0), st.get("keys", 0)))
    print("skeletons (.jnt)                    %6d" % st.get("skeletons", 0))
    print("clips paired with a skeleton        %6d" % st.get("clips_paired", 0))
    print("  posed at first, middle, last frame%6d" % st.get("clips_posed", 0))
    print("  pose failed                       %6d" % st.get("clips_pose_failed", 0))
    print("clip-skeleton pairs                 %6d" % st.get("pairs", 0))
    print("  posed                             %6d" % st.get("pairs_posed", 0))
    print("  pose failed                       %6d" % st.get("pairs_pose_failed", 0))
    print("clips not paired                    %6d" % (
        st.get("clips_unnamed", 0) + st.get("clips_no_skeleton_of_that_name", 0)
        + st.get("clips_named_but_not_lining_up", 0)))
    print("  no name in the clip               %6d" % st.get("clips_unnamed", 0))
    print("  no skeleton of that name          %6d" % st.get("clips_no_skeleton_of_that_name", 0))
    print("  named, records do not line up     %6d" % st.get("clips_named_but_not_lining_up", 0))
    if not st.get("packs"):
        print("no .fpk packs under %s" % dump)
        return 2
    return 1 if (st.get("packs_unread") or st.get("clips_unread")
                 or st.get("clips_pose_failed")) else 0


if __name__ == "__main__":
    raise SystemExit(main())
