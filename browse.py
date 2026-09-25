"""browse.py — convert extracted assets into immediately-viewable formats.

The Episode I counterpart of the Episode III kit's ``browse_bundle.py``:
after ``extract`` has produced ``OUTDIR/dump/``, this builds a sibling
``OUTDIR/browse/`` tree you can actually look at and listen to.

* ``textures`` — decode ``.xtx`` to PNG (pure Python, stdlib zlib).

  XS1's ``.xtx`` is a **virtual GS memory dump**, not a plain image (format
  understanding owed to Lakuwu's xenotool). Header: u32 magic, u32 total
  size, u32 sub-image count, u32 header table offset; then one 20-byte
  record per sub-image: ``u16 width, u16 buffer_width, u16 height, u16 pad,
  u32 gs_offset, u32 size, u32 file_addr``. Each sub-image (32-byte
  sub-header, then raster CT32 rows) is composed onto a CT32 canvas —
  256 or 512 pixels wide per ``buffer_width`` 4 / 8 — at the page position
  encoded by ``gs_offset`` (4096-byte GS blocks, ``buffer_width/2`` per
  row). The composed canvas is then unswizzled as one big **PSMT8 8-bpp
  indexed** image at double the CT32 dimensions. 16x16 sub-images are
  256-entry CSM1 palettes (two middle 8-entry runs of each 32 swapped).
  Palette sources, in priority order: embedded palette sub-image, paired
  ``.lex`` material, the consuming overlay's texture descriptors
  (``scan_ovl_cluts``; needs ``extract --code``), corner scan; candidates
  and per-material repaints are ranked by render coherence
  (``_region_noise``) and the chosen CLUT tile is blanked from the output
  (see docs/FORMATS.md and GitHub issue #1). Files with no palette are written as grayscale
  index images. PS2 alpha is 7-bit (128 = opaque) and is scaled
  ``min(a*2, 255)``. ARX-compressed ``.xtx`` are decompressed transparently.

  After the standalone ``.xtx``, every other dump file is swept for
  **embedded** XTX blobs (validated by header: u32 total size, sub-image
  count 1-64, in-bounds header table). Effect libraries (``.esd``/``.esp``),
  scene archives (``.a``), battle data (``.bin``) and the NLNK/NBGL/NBXX UI
  containers (``.npr``/``.rbg``/``.bxx``) carry ~3,000 textures this way —
  more than the standalone set. Scene archives, ``.fpk`` packs and ``.arc``
  bundles keep their members **ARX-compressed in place**; those are
  decompressed first (``iter_arx_containers``) and the XTX inside are
  paletted by the ``lex`` models packed beside them, nearest member first.
  Blobs byte-identical to an already-decoded texture are recorded as
  duplicates, not re-written; the sweep is logged per-carrier in
  ``browse/embedded_textures.csv`` (``packed`` = "arx" for members that
  only exist after decompression).

* ``audio`` — decode ``.vds``/``.vdm`` streamed audio to 16-bit WAV.

  The streams are headerless PS2 SPU ADPCM, **stereo, block-interleaved
  every 0x400 bytes** (64 frames per channel per block; verified by
  channel-envelope correlation ~0.95 at exactly that granularity). The
  sample rate is 48000 Hz — the constant the scene classes pass to
  ``xeno.Sound.streamPlay`` next to the stream's file id (lifted with the
  ``classes`` command). Genuinely mono streams are detected per file and
  kept mono.

* ``battle_audio`` — carve the battle voice/SE banks
  (``yamamoto/snd/sed/*.bin``) into per-sample WAVs.

  Each ``.bin`` wraps alternating ``seds``/``swdm`` chunks behind a small
  offset table (grammar in docs/FORMATS.md); the ``swdm`` chunks are
  ordinary wave banks, decoded at each sample's **native rate** derived
  from its base_pitch. Stereo halves (``X.aif.L/R``, ``X_L/R``, ``X.L/R``)
  are stitched into one stereo file.

* ``images`` — copy ``.jpg`` straight across, and unpack ``PS2ICON3D``
  ``.res`` resources (the HDD-install bundle) into their sections: the
  boot CNF, the ``icon.sys`` metadata and the PS2 memory-card ``.ico``
  3D icon model.

* ``text`` — copy ``.txt`` (recoded Shift-JIS -> UTF-8), plus a sniff pass
  over ``.dat``/``.info``/``.lst``/``.uml``/``.res``/``.esp``: files that
  are mostly printable (NUL-separated string tables like the item
  descriptions in ``evtitem.dat`` or the casino dialogue in ``CASINO.res``)
  are exported as readable ``*.strings.txt``.

* ``movies`` — transcode ``.pss``/``.ipu`` (MPEG-2 PS / PS2 IPU) to H.264
  MP4 **via ffmpeg when one can be found** (bundled tools/, PATH, common
  install dirs — same probe order as the Episode III kit). Without ffmpeg
  the kind is skipped with a note; raw ``.pss`` files also play directly
  in VLC / ffplay.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import wave
import zlib
from pathlib import Path
from typing import Iterable, Optional

import arx

ALL_KINDS = ("textures", "audio", "banks", "battle_audio", "images", "text", "movies")

VOICE_RATE = 48000  # from xeno.Sound.streamPlay(id, 48000, ...) in the .evt scripts


# ---------------------------------------------------------------------------
# PNG writer (stdlib only)
# ---------------------------------------------------------------------------

def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def write_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter: none
        raw += rgba[y * stride : (y + 1) * stride]
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# XTX -> PNG
# ---------------------------------------------------------------------------

_ALPHA_SCALE = bytes(min(a * 2, 255) for a in range(256))


def _plausible_clut(pal: Optional[list[bytes]]) -> bool:
    """Reject palette tiles that are clearly not palettes (empty canvas,
    flat fills). Material palette pointers describe *runtime VRAM* slots,
    which usually — but not always — match the tile's position in the file
    canvas; a pointer into unloaded space reads back near-empty."""
    if pal is None:
        return False
    distinct = len({p[:3] for p in pal})
    return distinct >= 16 and any(p[3] for p in pal)


def _clut_at(canvas: bytearray, clen: int, palx: int, paly: int) -> Optional[list[bytes]]:
    """Read a 256-entry CSM1 palette from the CT32 canvas at (palx, paly)."""
    if palx + 16 > clen or paly + 16 > clen:
        return None
    pal = []
    for ey in range(16):
        row = ((paly + ey) * clen + palx) * 4
        for ex in range(16):
            p = canvas[row + ex * 4 : row + ex * 4 + 4]
            pal.append(bytes((p[0], p[1], p[2], min(p[3] * 2, 255))))
    for g in range(8):  # CSM1 storage order -> logical order
        for j in range(8):
            k, m = g * 32 + 8 + j, g * 32 + 16 + j
            pal[k], pal[m] = pal[m], pal[k]
    return pal


def _clut_tile_like(canvas: bytearray, clen: int, px: int, py: int) -> bool:
    """Does the 16x16 tile at (px, py) look like CLUT data? (all raw alpha
    <= 0x80 — GS 7-bit — and a rich colour count)."""
    if px < 0 or py < 0 or px + 16 > clen or py + 16 > clen:
        return False
    distinct = set()
    for ey in range(16):
        row = ((py + ey) * clen + px) * 4
        for ex in range(16):
            r, g, b, a = canvas[row + ex * 4 : row + ex * 4 + 4]
            if a > 0x80:
                return False
            distinct.add((r, g, b))
    return len(distinct) >= 64


def _scan_for_clut(canvas: bytearray, clen: int
                   ) -> Iterable[tuple[list[bytes], tuple[int, int]]]:
    """Yield candidate palette tiles of a canvas with no other palette source.

    Menu/backdrop textures (casino, UI, dev-folder art) park their CLUT as a
    16x16 CT32 tile in an unused corner of the same canvas; the consuming
    code (e.g. OV11.OVL's texture descriptors) addresses it by GS block
    pointer. Standalone we scan for it: raw alpha <= 0x80 throughout and a
    rich colour count, trying the conventional spots first, then all
    16px-aligned tiles bottom-right first, then the finer 8px grid (CBPs
    can address half-block tiles). Yields (palette, (tile_x, tile_y)) in
    preference order; the caller keeps the first that renders sanely.
    """
    spots = [(0, 224), (240, 240), (224, 240), (176, 240), (112, 64), (128, 0)]
    spots += [(x, y) for y in range(clen - 16, -1, -16)
              for x in range(clen - 16, -1, -16)]
    spots += [(x, y) for y in range(clen - 16, -1, -8)
              for x in range(clen - 16, -1, -8) if x % 16 or y % 16]
    for px, py in spots:
        if _clut_tile_like(canvas, clen, px, py):
            yield _clut_at(canvas, clen, px, py), (px, py)


# PSMCT32 block layout within a 64x32-pixel GS page: 8 cols x 4 rows of
# 8x8-pixel blocks, in this (non-raster) order.
_PSMCT32_BLOCKS = (
    (0, 1, 4, 5, 16, 17, 20, 21),
    (2, 3, 6, 7, 18, 19, 22, 23),
    (8, 9, 12, 13, 24, 25, 28, 29),
    (10, 11, 14, 15, 26, 27, 30, 31),
)
_BLOCK_XY = {blk: (c * 8, r * 8)
             for r, row in enumerate(_PSMCT32_BLOCKS)
             for c, blk in enumerate(row)}


def _cbp_to_xy(cbp: int, clen: int) -> tuple[int, int]:
    """CLUT base pointer (GS block address) -> pixel coords in the canvas."""
    pages_per_row = clen // 64
    page, bip = cbp // 32, cbp % 32
    bx, by = _BLOCK_XY[bip]
    return (page % pages_per_row) * 64 + bx, (page // pages_per_row) * 32 + by


def scan_ovl_cluts(code_dir: Path) -> dict[str, list[int]]:
    """Map lowercase .xtx basename -> CBP palette-pointer candidates, read
    from the OV*.OVL overlays' texture descriptors.

    Each overlay that references a texture by path also holds 28-byte
    descriptors {u32 u, v, w-1, h-1, 0, cbp, texslot} whose CBP is the
    ground-truth palette location (see FORMATS.md). Descriptor->texture
    binding needs runtime tracing, so every texture referenced by an
    overlay gets that overlay's whole (deduped) CBP set as candidates —
    the per-file plausibility check and scoring pick the right one.
    """
    import re

    index: dict[str, list[int]] = {}
    if not code_dir.is_dir():
        return index
    for ovl in sorted(code_dir.glob("OV*.OVL")):
        data = ovl.read_bytes()
        names = {m.group(1).lower().rsplit(b"\\", 1)[-1].decode()
                 for m in re.finditer(rb"data\\([\x20-\x7e]{1,96}?\.xtx)", data,
                                      re.IGNORECASE)}
        if not names:
            continue
        cbps = []
        for off in range(0, len(data) - 28, 4):
            u, v, w, h, zero, cbp, slot = struct.unpack_from("<7I", data, off)
            if (zero == 0 and 0 < cbp < 0x800 and u < 1024 and v < 1024
                    and 0 < w + 1 <= 1024 and 0 < h + 1 <= 1024 and slot < 32
                    and cbp not in cbps):
                cbps.append(cbp)
        if not cbps:
            continue
        for name in names:
            merged = index.setdefault(name, [])
            merged += [c for c in cbps if c not in merged]
    return index


def _region_noise(idx: bytearray, W: int, pal: list[bytes],
                  u0: int, u1: int, v0: int, v1: int) -> tuple[float, float]:
    """(coherence ratio, opaque fraction) of a region rendered under a
    palette. The ratio is the mean L1 RGB distance of horizontally adjacent
    opaque pixels divided by the same for pixels 16 apart (rows sampled).
    Real art is smooth up close but varied at distance — a correct palette
    scores well under 1; a wrong palette renders dither noise, adjacent ~=
    far, ~1 (the exact garbling of GitHub issue #1). Being a ratio it
    cannot be gamed by washed-out low-contrast palettes the way an absolute
    noise measure can. A palette that hides the region (mostly transparent)
    or flattens it (no distance-16 variation) gets ratio inf. Callers
    choosing a BASE palette must also weigh the opaque fraction: transparent
    pixels are excluded from the ratio, so a wrong palette that maps garbled
    regions to alpha 0 would otherwise look spuriously coherent."""
    adj = adjn = far = farn = opaque = count = 0
    for v in range(v0, v1, 2):
        irow = v * W
        row = [pal[idx[irow + u]] for u in range(u0, u1)]
        n = len(row)
        for i, p in enumerate(row):
            count += 1
            if not p[3]:
                continue
            opaque += 1
            if i + 1 < n and row[i + 1][3]:
                q = row[i + 1]
                adj += (abs(p[0] - q[0]) + abs(p[1] - q[1])
                        + abs(p[2] - q[2]))
                adjn += 1
            if i + 16 < n and row[i + 16][3]:
                q = row[i + 16]
                far += (abs(p[0] - q[0]) + abs(p[1] - q[1])
                        + abs(p[2] - q[2]))
                farn += 1
    opq = opaque / count if count else 0.0
    if not count or opq < 0.3 or not adjn or not farn:
        return float("inf"), opq
    fbar = far / farn
    if fbar < 2.0:
        return float("inf"), opq  # flat render carries no information
    return (adj / adjn) / fbar, opq


def _rgba_noise(buf, stride: int, u0: int, u1: int, v0: int, v1: int) -> float:
    """Coherence ratio of a region of an RGBA buffer (same adjacent÷16-apart
    measure as ``_region_noise``, but on already-rendered pixels)."""
    adj = adjn = far = farn = 0
    for v in range(v0, v1, 2):
        row = v * stride
        for u in range(u0, u1):
            o = (row + u) * 4
            if not buf[o + 3]:
                continue
            r, g, b = buf[o], buf[o + 1], buf[o + 2]
            if u + 1 < u1 and buf[o + 7]:
                adj += (abs(r - buf[o + 4]) + abs(g - buf[o + 5])
                        + abs(b - buf[o + 6]))
                adjn += 1
            if u + 16 < u1 and buf[o + 67]:
                far += (abs(r - buf[o + 64]) + abs(g - buf[o + 65])
                        + abs(b - buf[o + 66]))
                farn += 1
    if not adjn or not farn:
        return float("inf")
    fbar = far / farn
    if fbar < 2.0:
        return float("inf")
    return (adj / adjn) / fbar


def _candidate_score(idx: bytearray, W: int, H: int, pal: list[bytes]) -> float:
    """Whole-image score for a BASE-palette candidate: coherence ratio plus
    a stiff penalty for transparency, so a palette cannot win by hiding
    hard-to-explain pixels behind alpha 0. Lower is better; inf = reject."""
    ratio, opq = _region_noise(idx, W, pal, 0, W, 0, H)
    return ratio + max(0.0, 0.95 - opq) * 2.0


def lex_materials(lex: bytes) -> list[tuple[int, int, int, int, int, int]]:
    """(palx, paly, umin, umax, vmin, vmax) per mesh material of a .lex model.

    Reads only the fixed-offset mesh headers (palette byte at +0x125, UV info
    at +0x130 — layout from xenotool's lex_file.h); the extra material blocks
    embedded in the VIF vertex streams are not chased.
    """
    if lex[:4] != b"lex\x00" or len(lex) < 0xB0:
        return []
    (nmesh,) = struct.unpack_from("<I", lex, 0x44)
    if not (0 < nmesh <= 4096):
        return []
    mats = []
    for i in range(nmesh):
        off = 0xB0 + 4 * i
        if off + 4 > len(lex):
            break
        (addr,) = struct.unpack_from("<I", lex, off)
        if addr + 0x190 > len(lex):
            continue
        pal2, pal = lex[addr + 0x124], lex[addr + 0x125]
        if pal == 0xFF:
            continue
        t = lex[addr + 0x130]
        b = lex[addr + 0x131 : addr + 0x140]
        if t == 0x00:
            continue
        if t == 0xFF:
            w, x1, x = b[0] & 0xF, (b[1] >> 3) & 1, b[1] >> 4
            h, y1, y = b[2] >> 4, b[3] >> 7, b[4] & 0xF
            umin, vmin = x * 64 + x1 * 32, y * 64 + y1 * 32
            umax, vmax = umin + (w + 1) * 16, vmin + (h + 1) * 16
        else:  # 0x0a and friends
            umin = (b[0] & 0x3F) << 4
            vmin = b[2]
            umax = ((b[1] << 2) | (b[0] >> 6)) + 1
            vmax = ((b[4] << 6) | (b[3] >> 2)) + 1
        pal_hi, pal_lo = pal >> 4, pal & 0xF
        palx = (pal_hi % 2) * 256 + (pal_lo // 2) * 32 + (pal2 >> 7) * 16
        paly = (pal_hi // 2) * 32 + (pal_lo % 2) * 16
        mats.append((palx, paly, umin, umax, vmin, vmax))
    return mats


def decode_xtx(data: bytes, lex: bytes | list[bytes] = b"",
               cbp_candidates: Optional[list[int]] = None,
               ) -> Optional[tuple[int, int, bytes, str]]:
    """(width, height, RGBA, palette_source) of the composed texture, or None.

    ``lex`` — the paired model file, when there is one; its materials say
    which palette tile in the canvas colours which UV region. A LIST of lex
    blobs merges materials from companion models that share the atlas
    (``kosmos_face.lex`` has no ``kosmos_face.xtx`` — its meshes bind
    palettes onto ``kosmos.xtx``'s canvas, e.g. the red eye tile).
    ``cbp_candidates`` — CLUT base pointers from the consuming overlay's
    texture descriptors (``scan_ovl_cluts``); when one of them lands on a
    plausible palette tile it is ground truth and wins over guessing.
    palette_source is one of "embedded", "lex", "ovl", "scan", "gray".
    """
    if data[:4] != b"XTX\x00" or len(data) < 0x24:
        return None
    _total, count, hdr_addr = struct.unpack_from("<III", data, 4)
    if not (1 <= count <= 64) or hdr_addr + 20 * count > len(data):
        return None
    subs = []
    for i in range(count):
        base = hdr_addr + 20 * i
        w, bufw, h = struct.unpack_from("<HHH", data, base)
        gs_off, _size, addr = struct.unpack_from("<III", data, base + 8)
        if addr + 32 + w * h * 4 > len(data):
            return None
        subs.append((w, bufw, h, gs_off, addr))
    bufw = subs[0][1] or 8
    clen = {4: 256, 8: 512}.get(bufw)
    if clen is None:
        return None
    canvas = bytearray(clen * clen * 4)  # CT32 canvas, clen x clen pixels
    placed: list[tuple[int, int, int, int]] = []  # (w, h, x0, y0) per sub
    for w, _, h, gs_off, addr in subs:
        px = data[addr + 32 : addr + 32 + w * h * 4]
        block = gs_off // 4096
        x0 = (block % (bufw // 2)) * 64
        y0 = (block // (bufw // 2)) * 32
        if x0 + w > clen or y0 + h > clen:
            continue
        for y in range(h):
            dst = ((y0 + y) * clen + x0) * 4
            canvas[dst : dst + w * 4] = px[y * w * 4 : (y + 1) * w * 4]
        placed.append((w, h, x0, y0))
    # 16x16 sub-images are palette tiles: composed into the canvas (materials
    # address them there) but not part of the visible image extent
    pal_tiles = [(x0, y0) for w, h, x0, y0 in placed
                 if w == 16 and h == 16 and count > 1]
    if placed and len(pal_tiles) == len(placed):
        # nothing left as art — a tiny all-16x16 sprite file (e.g. the 1P/2P
        # indicators): keep only tiles that truly read as CLUT data (first
        # one if all do), and let the rest render as image
        looks = [(x0, y0) for x0, y0 in pal_tiles
                 if _clut_tile_like(canvas, clen, x0, y0)]
        pal_tiles = (looks if len(looks) < len(pal_tiles) else looks[:1])
    embedded_pal: Optional[list[bytes]] = None
    if pal_tiles:
        embedded_pal = _clut_at(canvas, clen, *pal_tiles[0])
    max_x = max_y = 0
    for w, h, x0, y0 in placed:
        if (x0, y0) in pal_tiles:
            continue
        max_x = max(max_x, (x0 + w) * 2)
        max_y = max(max_y, (y0 + h) * 2)
    if not max_x or not max_y:
        return None

    # The canvas holds PSMT8 indices swizzled into CT32; unswizzle the
    # cropped region ("unswizzle8", the widely shared PS2 routine).
    W, H, tw = max_x, max_y, clen * 2
    idx = bytearray(W * H)
    for y in range(H):
        block_row = (y & ~0xF) * tw
        swap_selector = (((y + 2) >> 2) & 1) * 4
        col_row = ((((y & ~3) >> 1) + (y & 1)) & 7) * tw * 2
        byte_y = (y >> 1) & 1
        drow = y * W
        for x in range(W):
            idx[drow + x] = canvas[
                block_row + (x & ~0xF) * 2 + col_row
                + ((x + swap_selector) & 7) * 4 + byte_y + ((x >> 2) & 2)]

    lexes = lex if isinstance(lex, list) else ([lex] if lex else [])
    mats = [m for lx in lexes for m in lex_materials(lx)]

    # palette selection: embedded tile > lex material > OVL descriptor CBP >
    # corner scan > grayscale. pal_xy = the chosen tile's canvas coords when
    # we are certain the tile IS a CLUT (so it can be blanked from output).
    pal_xy: Optional[tuple[int, int]] = None
    source = "gray"
    base_pal = embedded_pal
    if base_pal is not None:
        source = "embedded"
    if base_pal is None and mats:
        cand = _clut_at(canvas, clen, mats[0][0], mats[0][1])
        if _plausible_clut(cand):
            base_pal, source = cand, "lex"
    if base_pal is None and cbp_candidates:
        best = 1.2  # only accept OVL candidates that render coherently
        for cbp in cbp_candidates:
            px, py = _cbp_to_xy(cbp, clen)
            if not _clut_tile_like(canvas, clen, px, py):
                continue
            cand = _clut_at(canvas, clen, px, py)
            if not _plausible_clut(cand):
                continue
            s = _candidate_score(idx, W, H, cand)
            if s < best:
                best, base_pal, pal_xy, source = s, cand, (px, py), "ovl"
    if base_pal is None:
        # best of the first couple dozen scan candidates (the true CLUT is
        # near-always parked early in the spot order); first-hit is not
        # enough — a wrong tile can look acceptable while a later one is
        # clearly better, and vice versa
        best, tried = 1.2, 0
        for cand, xy in _scan_for_clut(canvas, clen):
            if not _plausible_clut(cand):
                continue
            s = _candidate_score(idx, W, H, cand)
            if s < best:
                best, base_pal, pal_xy, source = s, cand, xy, "scan"
            tried += 1
            if tried >= 24 or best < 0.45:
                break
    base_lut = ([bytes(p) for p in base_pal] if base_pal
                else [bytes((i, i, i, 255)) for i in range(256)])

    out = bytearray(W * H * 4)
    for y in range(H):
        drow = y * W * 4
        irow = y * W
        for x in range(W):
            out[drow + x * 4 : drow + x * 4 + 4] = base_lut[idx[irow + x]]

    def paint(pal, u0, u1, v0, v1):
        for v in range(v0, v1):
            drow = v * W * 4
            irow = v * W
            for u in range(u0, u1):
                out[drow + u * 4 : drow + u * 4 + 4] = pal[idx[irow + u]]

    # per-material regions override the base palette — unless the material's
    # palette renders its own UV rect clearly noisier than the base palette
    # does (material palx/paly are runtime-VRAM slots that only sometimes
    # match the file canvas; a plausible-but-wrong tile used to garble
    # exactly its UV rect — GitHub issue #1)
    painted: list[tuple[int, int, int, int]] = []
    pals: list[list[bytes]] = []
    for palx, paly, umin, umax, vmin, vmax in mats:
        pal = _clut_at(canvas, clen, palx, paly)
        if pal is None or not _plausible_clut(pal):
            continue
        if all(pal != p for p in pals):
            pals.append(pal)
        if pal == base_pal:
            continue
        v0, v1 = max(0, vmin), min(H, vmax)
        u0, u1 = max(0, umin), min(W, umax)
        if v1 <= v0 or u1 <= u0:
            continue
        mn, _ = _region_noise(idx, W, pal, u0, u1, v0, v1)
        bn, _ = (_region_noise(idx, W, base_pal, u0, u1, v0, v1) if base_pal
                 else (mn, 0.0))
        if mn > bn * 1.3:
            continue
        paint(pal, u0, u1, v0, v1)
        painted.append((u0, u1, v0, v1))

    # regions no parsed material covers (their materials hide in the VIF
    # vertex streams): per 64px block, if some known palette renders it
    # clearly smoother than the base palette, use it — this un-garbles the
    # issue-#1 char atlases without touching regions base already explains.
    # The candidate pool is the parsed material palettes PLUS every
    # CLUT-looking tile parked in the canvas — map atlases (MC_*.xtx) bind
    # several parked CLUTs through VIF-stream materials we never parse, so
    # the right palette is often in the canvas but absent from ``pals``.
    if base_pal is not None:
        for cand, _xy in _scan_for_clut(canvas, clen):
            if len(pals) >= 14:
                break
            if _plausible_clut(cand) and all(cand != p for p in pals):
                pals.append(cand)
    if base_pal is not None and len(pals) > 1:
        for v0 in range(0, H, 64):
            v1 = min(H, v0 + 64)
            for u0 in range(0, W, 64):
                u1 = min(W, u0 + 64)
                cov = any(u0 >= pu0 and u1 <= pu1 and v0 >= pv0 and v1 <= pv1
                          for pu0, pu1, pv0, pv1 in painted)
                if cov:
                    continue
                bn, _ = _region_noise(idx, W, base_pal, u0, u1, v0, v1)
                if bn == float("inf") or bn < 0.75:  # base looks fine; keep it
                    continue
                best_pal, best_n = None, bn
                for pal in pals:
                    if pal == base_pal:
                        continue
                    n, _ = _region_noise(idx, W, pal, u0, u1, v0, v1)
                    if n < best_n:
                        best_pal, best_n = pal, n
                if best_pal is not None and best_n < bn * 0.5:
                    paint(best_pal, u0, u1, v0, v1)

    # fine-grained rescue with TRUSTED palettes only: the parsed materials'
    # rects don't cover the whole atlas (more materials hide in the VIF
    # vertex streams), but the missing regions are almost always outfit art
    # continuing past a parsed rect — rendered by one of the SAME material
    # palettes. 32px blocks outside every painted rect, visibly imperfect
    # under base (bn > 0.25), repaint when a material palette renders them
    # near-perfectly (n < 0.25 and < 0.6*bn — thresholds fitted on the
    # KOS-MOS atlas: true fixes measured <= 0.16, false repaints >= 0.51).
    if base_pal is not None and mats:
        mat_pals: list[list[bytes]] = []
        for palx, paly, *_ in mats:
            mp = _clut_at(canvas, clen, palx, paly)
            if mp is not None and _plausible_clut(mp) \
                    and all(mp != q for q in mat_pals):
                mat_pals.append(mp)
        for v0 in range(0, H, 32):
            v1 = min(H, v0 + 32)
            for u0 in range(0, W, 32):
                u1 = min(W, u0 + 32)
                if any(u0 >= pu0 and u1 <= pu1 and v0 >= pv0 and v1 <= pv1
                       for pu0, pu1, pv0, pv1 in painted):
                    continue
                bn, _ = _region_noise(idx, W, base_pal, u0, u1, v0, v1)
                if bn <= 0.25 or bn == float("inf"):
                    continue
                best_pal, best_n = None, min(0.25, bn * 0.6)
                for mp in mat_pals:
                    if mp == base_pal:
                        continue
                    n, _ = _region_noise(idx, W, mp, u0, u1, v0, v1)
                    if n < best_n:
                        best_pal, best_n = mp, n
                if best_pal is not None:
                    paint(best_pal, u0, u1, v0, v1)

    # raw CT32 regions: a canvas can mix 8bpp paletted art with TRUE-COLOUR
    # pixel regions (KOS-MOS's hair-strand sheets — issue #1's last holdout).
    # No palette can ever render those. Per 64px block: if the current render
    # still reads as dither noise (ratio >= 0.9; every correctly-paletted
    # block on the KOS-MOS atlas measures below that) but the canvas read
    # directly as CT32 is more coherent, draw the block from the canvas —
    # each canvas pixel covers 2x2 output pixels.
    for v0 in range(0, H, 64):
        v1 = min(H, v0 + 64)
        for u0 in range(0, W, 64):
            u1 = min(W, u0 + 64)
            on = _rgba_noise(out, W, u0, u1, v0, v1)
            if on < 0.9 or on == float("inf"):
                continue
            cn = _rgba_noise(canvas, clen, u0 // 2, u1 // 2, v0 // 2, v1 // 2)
            if cn >= on:
                continue
            for y in range(v0, v1):
                crow = (y // 2) * clen
                drow = y * W * 4
                for x in range(u0, u1):
                    o = (crow + x // 2) * 4
                    out[drow + x * 4] = canvas[o]
                    out[drow + x * 4 + 1] = canvas[o + 1]
                    out[drow + x * 4 + 2] = canvas[o + 2]
                    out[drow + x * 4 + 3] = _ALPHA_SCALE[canvas[o + 3]]

    # CLUT tiles are palette data, not art: blank the chosen tile plus the
    # connected cluster of palette-looking tiles around it (card sheets etc.
    # park a strip of colour-variant CLUTs together) when it falls inside
    # the visible extent — the "square of noise in the corner" of issue #1
    if pal_xy is not None:
        cluster = {pal_xy}
        frontier = [pal_xy]
        while frontier and len(cluster) <= 16:
            cx, cy = frontier.pop()
            for dx in (-8, 0, 8):
                for dy in (-8, 0, 8):
                    t = (cx + dx, cy + dy)
                    if (t not in cluster
                            and _clut_tile_like(canvas, clen, t[0], t[1])):
                        cluster.add(t)
                        frontier.append(t)
        if len(cluster) > 16:
            # a real palette strip is a handful of tiles; a sprawling
            # "cluster" means the flood fill wandered into colourful art
            # (card faces, object sprites) — blank only the CLUT itself
            cluster = {pal_xy}
        if len(cluster) * 32 * 32 > W * H * 0.06:
            cluster = set()  # parked CLUTs are a sliver of a big canvas;
            # on a small texture the "tile" is likelier art — leave it
        for tx, ty in cluster:
            px0, py0 = tx * 2, ty * 2
            for y in range(max(0, py0), min(H, py0 + 32)):
                drow = y * W * 4
                for x in range(max(0, px0), min(W, px0 + 32)):
                    out[drow + x * 4 : drow + x * 4 + 4] = b"\x00\x00\x00\x00"
    return W, H, bytes(out), source


def iter_embedded_xtx(data: bytes) -> Iterable[tuple[int, bytes]]:
    """Yield (offset, blob) for each plausible XTX embedded in ``data``.

    A hit must look like a real header — u32 total size in bounds, sub-image
    count 1-64, header table inside the blob — which filters the stray
    ``XTX\\0`` byte patterns that occur in animation and movie data.
    """
    i, n = 0, len(data)
    while True:
        i = data.find(b"XTX\x00", i)
        if i < 0:
            return
        if i + 16 <= n:
            total, count, hdr = struct.unpack_from("<III", data, i + 4)
            if 0x24 <= total <= n - i and 1 <= count <= 64 and hdr + 20 * count <= total:
                yield i, data[i : i + total]
                i += total
                continue
        i += 4


def iter_arx_containers(data: bytes) -> Iterable[tuple[int, int, bytes]]:
    """Yield ``(offset, span, payload)`` for every ARX container inside ``data``.

    Scene archives (``.a``), ``.fpk`` packs, ``.arc`` bundles and a few
    ``.bin``/``.npr`` files store their members ARX-compressed in place:
    a 16-byte header (magic, uncompressed size, container size including
    the header, 0), the 30-word LUT, then the bit stream. The members —
    XTX textures, the ``lex`` models that bind their palettes, FPK packs,
    FL00 event containers — only exist after decompression. Because the
    ARX coder passes literal words through verbatim, the *header* of a
    packed XTX still reads as ``XTX\0`` + sane sizes inside the stream,
    which is exactly the "undecodable in-``.a`` variant" the raw sweep used
    to report (528 on the retail disc): callers must skip raw hits that
    fall inside a container's span. A hit must carry a sane header and
    decompress to exactly the announced size; anything else is skipped.
    """
    i, n = 0, len(data)
    while True:
        i = data.find(arx.MAGIC, i)
        if i < 0:
            return
        if i + 16 <= n:
            usize, csize, zero = struct.unpack_from("<III", data, i + 4)
            if zero == 0 and usize > 0 and 16 + 30 * 4 < csize <= n - i:
                try:
                    payload = arx.decompress(data[i:i + csize])
                except (arx.ARXError, struct.error, ValueError):
                    payload = b""
                if len(payload) == usize:
                    yield i, csize, payload
                    i += csize
                    continue
        i += 4


# extensions that never carry embedded XTX art (movies, audio, plain media);
# everything else in the dump gets swept
_NO_XTX_CARRIER = (".xtx", ".pss", ".ipu", ".vds", ".vdm", ".jpg", ".jpeg", ".txt")


# ---------------------------------------------------------------------------
# SPU ADPCM (.vds/.vdm) -> WAV
# ---------------------------------------------------------------------------

_SPU_FILTERS = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))
_SIGNED_NIBBLE = tuple(n - 16 if n >= 8 else n for n in range(16))


def decode_spu_adpcm(data: bytes) -> bytes:
    """Decode headerless SPU ADPCM to 16-bit little-endian mono PCM."""
    import array

    out = array.array("h")
    h1 = h2 = 0
    nib = _SIGNED_NIBBLE
    for base in range(0, len(data) - 15, 16):
        hdr = data[base]
        shift = hdr & 0x0F
        filt = hdr >> 4
        if filt > 4 or shift > 12:  # invalid frame; keep sync, emit silence
            out.extend((0,) * 28)
            continue
        f0, f1 = _SPU_FILTERS[filt]
        up = 12 - shift
        for b in data[base + 2 : base + 16]:
            for n in (nib[b & 0x0F], nib[b >> 4]):
                # predictor divides by 64 rounding toward zero (matches the
                # SPU / ffmpeg adpcm_psx exactly; plain >> 6 floors instead)
                p = h1 * f0 + h2 * f1
                s = (n << up) + (p // 64 if p >= 0 else -((-p) // 64))
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                h2 = h1
                h1 = s
                out.append(s)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


STEREO_BLOCK = 0x400  # L/R interleave granularity of .vds/.vdm streams


def decode_voice_stream(data: bytes) -> tuple[bytes, int]:
    """Decode a .vds/.vdm stream -> (interleaved 16-bit PCM, channel count).

    The streams are stereo, block-interleaved every 0x400 bytes (64 SPU
    frames per channel per block) — found empirically: deinterleaved halves
    of retail files correlate strongly at exactly this granularity and at no
    other, with envelope correlation ~0.95. Each channel is decoded with its
    own predictor chain. A per-file envelope check keeps genuinely mono
    streams mono instead of shredding them into 21 ms chunks.
    """
    G = STEREO_BLOCK
    pairs = len(data) // (2 * G)
    if pairs < 4:
        return decode_spu_adpcm(data), 1
    left = b"".join(data[i : i + G] for i in range(0, pairs * 2 * G, 2 * G))
    right = b"".join(data[i + G : i + 2 * G] for i in range(0, pairs * 2 * G, 2 * G))
    import array

    L = array.array("h")
    L.frombytes(decode_spu_adpcm(left))
    R = array.array("h")
    R.frombytes(decode_spu_adpcm(right))
    n = min(len(L), len(R))

    # envelope correlation: ~1 for L/R of one recording, ~0 for a mono
    # stream wrongly split into alternating time chunks
    win = 4800
    eL = [sum(abs(L[j]) for j in range(i, i + win, 8)) for i in range(0, n - win, win)]
    eR = [sum(abs(R[j]) for j in range(i, i + win, 8)) for i in range(0, n - win, win)]
    m = len(eL)
    if m >= 4:
        sa, sb = sum(eL) / m, sum(eR) / m
        num = da = db = 0.0
        for a, b in zip(eL, eR):
            x, y = a - sa, b - sb
            num += x * y
            da += x * x
            db += y * y
        if num / ((da * db) ** 0.5 + 1e-9) < 0.5:
            return decode_spu_adpcm(data), 1  # genuinely mono

    out = array.array("h", bytes(4 * n))
    out[0::2] = L[:n]
    out[1::2] = R[:n]
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes(), 2


def write_wav(path: Path, pcm: bytes, rate: int, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# ---------------------------------------------------------------------------
# SWD wave banks / SMD sequences (Procyon Studio sequenced BGM)
# ---------------------------------------------------------------------------

BANK_RATE = 32000  # audition rate; true per-note pitch comes from the sequences


def parse_swd(data: bytes) -> Optional[list[tuple[str, int, int, int]]]:
    """(name, start, end, rate) of the SPU samples in a swdm bank.

    rate is the sample's native playback rate derived from its base_pitch
    (48000 * 2^(pitch/256/12), see FORMATS.md) — retail banks tune samples
    individually, so 32007 Hz-style values are intentional cents detune.
    """
    if data[:4] != b"swdm" or len(data) < 0x80:
        return None
    body_size, body_off = struct.unpack_from("<II", data, 0x24)
    if body_off < 0x70 or body_off + body_size > len(data):
        return None
    entries = []
    off = 0x50
    while off + 32 <= body_off:
        (rel,) = struct.unpack_from("<I", data, off)
        name = data[off + 16 : off + 32].rstrip(b"\x00")
        if rel >= body_size or (name and not all(32 <= c < 127 for c in name)):
            break
        if not name:  # zero-named placeholder slot; real entries may follow
            off += 32
            continue
        (base_pitch,) = struct.unpack_from("<h", data, off + 10)
        rate = round(48000 * 2 ** (base_pitch / 256 / 12))
        if not 4000 <= rate <= 48000:
            rate = BANK_RATE
        entries.append((name.decode(), rel, rate))
        off += 32
    if not entries:
        return None
    order = sorted(range(len(entries)), key=lambda i: entries[i][1])
    out = []
    for k, i in enumerate(order):
        name, rel, rate = entries[i]
        end = entries[order[k + 1]][1] if k + 1 < len(order) else body_size
        out.append((name, body_off + rel, body_off + end, rate))
    return out


def parse_sed_bin(data: bytes) -> Optional[list[tuple[bytes, int, int]]]:
    """(magic, start, end) chunk ranges of a battle sed .bin wrapper.

    yamamoto/snd/sed/*.bin wrap alternating "seds" (SFX program metadata)
    and "swdm" (wave bank) chunks: u32 even chunk_count, u32 total size
    (== file size), then chunk_count ascending u32 offsets. Sniff-based —
    returns None for anything else named .bin.
    """
    if len(data) < 16:
        return None
    count, total = struct.unpack_from("<II", data, 0)
    if count < 2 or count > 16 or count % 2 or total != len(data):
        return None
    if 8 + 4 * count > len(data):
        return None
    offs = list(struct.unpack_from(f"<{count}I", data, 8))
    if offs[0] < 8 + 4 * count or offs != sorted(offs) or offs[-1] >= total:
        return None
    chunks = []
    for i, start in enumerate(offs):
        end = offs[i + 1] if i + 1 < count else total
        magic = data[start : start + 4]
        if magic not in (b"seds", b"swdm"):
            return None
        chunks.append((magic, start, end))
    return chunks


def _trim_sample(data: bytes, start: int, end: int) -> bytes:
    """Cut a bank sample after its first ADPCM end-flagged frame."""
    for base in range(start, min(end, len(data)) - 15, 16):
        if data[base + 1] & 1:
            return data[start : base + 16]
    return data[start:end]


def smd_info(data: bytes) -> Optional[list[str]]:
    """The ASCII metadata strings of an smdm sequence (title, game, ...)."""
    if data[:4] != b"smdm":
        return None
    strs = []
    p = 0x2C
    while len(strs) < 5 and p < min(len(data), 0x200):
        q = data.find(b"\x00", p)
        if q <= p:
            break
        s = data[p:q]
        if not all(32 <= c < 127 for c in s):
            break
        strs.append(s.decode())
        p = q + 1
    return strs


# ---------------------------------------------------------------------------
# ffmpeg detection (same probe order as the Episode III kit)
# ---------------------------------------------------------------------------

_FFMPEG_POSIX = ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                 "/opt/homebrew/bin/ffmpeg", "/snap/bin/ffmpeg")
_FFMPEG_WIN = (r"C:\ffmpeg\bin\ffmpeg.exe",
               r"C:\Program Files\ffmpeg\bin\ffmpeg.exe")


def detect_ffmpeg() -> Optional[str]:
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for tools in (
            exe_dir / "tools",                        # manual drop next to the exe
            exe_dir / "_internal" / "tools",          # bundled, one-folder build
            exe_dir.parent / "Frameworks" / "tools",  # bundled, macOS .app
        ):
            cand = tools / name
            if cand.is_file():
                return str(cand)
    hit = shutil.which("ffmpeg")
    if hit:
        return hit
    for cand in _FFMPEG_WIN if os.name == "nt" else _FFMPEG_POSIX:
        p = Path(os.path.expandvars(cand))
        if p.is_file():
            return str(p)
    return None


def extract_pss_audio(data: bytes) -> Optional[tuple[bytes, int, int]]:
    """(PCM, channels, rate) from a .pss movie's private stream, or None.

    Xenosaga movies mux audio as MPEG private stream 1 (0xBD): each PES
    payload starts with a 4-byte substream tag (ff a1 00 00); concatenated
    payloads form a Sony ADS stream — "SShd" header declaring format 0x10
    (SPU ADPCM), rate, channels and interleave (0x400 — the same layout as
    the .vds voice streams), then "SSbd" + body. ffmpeg misparses these
    packets, which is why plain conversion yields video-only files.
    """
    chunks = []
    i = 0
    n = len(data)
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 6 > n:
            break
        sid = data[i + 3]
        if sid == 0xBA:
            i += 14
            continue
        if sid == 0xB9:
            break
        (ln,) = struct.unpack_from(">H", data, i + 4)
        if sid == 0xBD and i + 9 <= n:
            hdr_len = data[i + 8]
            pstart = i + 9 + hdr_len + 4  # + substream tag
            pend = min(i + 6 + ln, n)
            if pstart < pend:
                chunks.append(data[pstart:pend])
        i += 6 + ln
    raw = b"".join(chunks)
    if raw[:4] != b"SShd" or len(raw) < 40:
        return None
    (hsize,) = struct.unpack_from("<I", raw, 4)
    fmt, rate, ch, inter = struct.unpack_from("<IIII", raw, 8)
    body_at = 8 + hsize
    if fmt != 0x10 or raw[body_at : body_at + 4] != b"SSbd":
        return None
    body = raw[body_at + 8 :]
    if ch == 2 and inter:
        pairs = len(body) // (2 * inter)
        left = b"".join(body[k : k + inter]
                        for k in range(0, pairs * 2 * inter, 2 * inter))
        right = b"".join(body[k + inter : k + 2 * inter]
                         for k in range(0, pairs * 2 * inter, 2 * inter))
        import array

        L = array.array("h")
        L.frombytes(decode_spu_adpcm(left))
        R = array.array("h")
        R.frombytes(decode_spu_adpcm(right))
        m = min(len(L), len(R))
        out = array.array("h", bytes(4 * m))
        out[0::2] = L[:m]
        out[1::2] = R[:m]
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes(), 2, rate
    return decode_spu_adpcm(body), 1, rate


def convert_movie(ffmpeg: str, src: Path, dest: Path) -> bool:
    """Convert one movie.

    For movies carrying audio this produces THREE files so fan projects
    (undubs/redubs) get the tracks already divorced:
        <name>.mp4        muxed video + audio
        <name>.video.mp4  video only (the same encode, remux — no 2nd pass)
        <name>.audio.wav  the demuxed stream, decoded to PCM
    Video-only movies produce just <name>.mp4.
    """
    audio = None
    if src.suffix.lower() == ".pss":
        try:
            audio = extract_pss_audio(src.read_bytes())
        except Exception:
            audio = None

    def run(args) -> bool:
        try:
            return subprocess.run(args, capture_output=True).returncode == 0
        except OSError:
            return False

    base = [ffmpeg, "-y", "-loglevel", "error"]
    demux = ["-f", "ipu"] if src.suffix.lower() == ".ipu" else []
    enc = ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-an"]
    if not audio:
        return run(base + demux + ["-i", str(src)] + enc + [str(dest)])

    pcm, ch, rate = audio
    video_only = dest.with_suffix("") .with_name(dest.stem + ".video.mp4")
    audio_wav = dest.with_name(dest.stem + ".audio.wav")
    write_wav(audio_wav, pcm, rate, ch)
    if not run(base + demux + ["-i", str(src)] + enc + [str(video_only)]):
        return False
    # mux = stream copy of the encode + AAC of the wav; the wav stays on disk
    return run(base + ["-i", str(video_only), "-i", str(audio_wav),
                       "-map", "0:v:0", "-map", "1:a:0",
                       "-c:v", "copy", "-c:a", "aac", "-shortest", str(dest)])


# ---------------------------------------------------------------------------
# The bundle builder
# ---------------------------------------------------------------------------

def _dump_files(dump: Path, exts: tuple[str, ...]) -> Iterable[Path]:
    for p in sorted(dump.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            yield p


def _load(src: Path, bump) -> bytes:
    """Read a dump file, transparently ARX-decompressing (empty on failure)."""
    data = src.read_bytes()
    if arx.is_arx(data):
        try:
            data = arx.decompress(data)
            bump("arx_decompressed")
        except arx.ARXError:
            bump("arx_failed")
            return b""
    return data


def build_browse(out_dir: Path, kinds: Iterable[str], rate: int = VOICE_RATE,
                 log=lambda s: print(s, file=sys.stderr)) -> dict:
    dump = out_dir / "dump"
    if not dump.is_dir():
        raise FileNotFoundError(f"{dump} not found — run extract first")
    browse = out_dir / "browse"
    stats: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        stats[key] = stats.get(key, 0) + n

    kinds = set(kinds)

    if "textures" in kinds:
        import csv as _csv
        import hashlib

        tdir = browse / "textures_png"
        srcs = list(_dump_files(dump, (".xtx",)))
        cbp_index = scan_ovl_cluts(browse / "code")  # {} without extract --code
        seen: dict[str, str] = {}  # sha1 of XTX bytes -> written PNG (dedup)
        log(f"textures: decoding {len(srcs)} .xtx -> PNG "
            f"({len(cbp_index)} with overlay palette hints) ...")
        # companion models: a .lex with no .xtx of its own (kosmos_face.lex)
        # binds its materials onto the sibling atlas whose stem is the
        # longest prefix of the lex stem (kosmos_h_face -> kosmos_h.xtx)
        xtx_stems = {s.parent: [] for s in srcs}
        for s in srcs:
            xtx_stems[s.parent].append(s.stem.lower())
        companions: dict[Path, list[Path]] = {}
        for s in srcs:
            for lx in s.parent.glob("*.lex"):
                stem = lx.stem.lower()
                if stem in xtx_stems[s.parent]:
                    continue  # has its own atlas pairing
                owners = [t for t in xtx_stems[s.parent] if stem.startswith(t)]
                if owners and max(owners, key=len) == s.stem.lower():
                    companions.setdefault(s, []).append(lx)
        for i, src in enumerate(srcs, 1):
            data = _load(src, bump)
            lex: list[bytes] = []
            sib = src.with_suffix(".lex")
            if sib.is_file():
                lex.append(_load(sib, bump))
            for extra in sorted(companions.get(src, [])):
                lex.append(_load(extra, bump))
            cbps = cbp_index.get(src.name.lower())
            decoded = decode_xtx(data, lex, cbps) if data else None
            if decoded is None:
                bump("textures_undecodable")
                continue
            w, h, rgba, source = decoded
            dest = tdir / src.relative_to(dump).with_suffix(".png")
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_png(dest, w, h, rgba)
            seen.setdefault(hashlib.sha1(data).hexdigest(),
                            str(dest.relative_to(browse)))
            bump("textures_png")
            bump(f"textures_pal_{source}")
            if i % 50 == 0:
                log(f"  {i}/{len(srcs)}  ({src.relative_to(dump)})")
        log(f"textures: {stats.get('textures_png', 0)} PNGs "
            f"({stats.get('textures_undecodable', 0)} undecodable; palettes: "
            f"{stats.get('textures_pal_embedded', 0)} embedded, "
            f"{stats.get('textures_pal_lex', 0)} lex, "
            f"{stats.get('textures_pal_ovl', 0)} overlay, "
            f"{stats.get('textures_pal_scan', 0)} scanned, "
            f"{stats.get('textures_pal_gray', 0)} grayscale)")

        # sweep every other dump file for embedded XTX (effect libraries,
        # scene archives, UI containers — see module docstring)
        carriers = [p for p in sorted(dump.rglob("*"))
                    if p.is_file() and p.suffix.lower() not in _NO_XTX_CARRIER]
        log(f"textures: sweeping {len(carriers)} other files for embedded XTX ...")
        edir = tdir / "_embedded"
        rows = []
        for i, src in enumerate(carriers, 1):
            data = _load(src, bump)
            if not data:
                continue
            rel = src.relative_to(dump)
            # members compressed in place (scene archives, packs) come first:
            # their XTX only exist after decompression, and the literal
            # header words inside a compressed stream must not be taken for
            # raw hits (that was the old "undecodable in-.a variant")
            containers = list(iter_arx_containers(data))
            spans = [(o, o + span) for o, span, _ in containers]
            lex_pool = [(o, pay) for o, _, pay in containers
                        if pay[:4] == b"lex\x00"]
            # (offset label, png stem, blob, lex models, packed?)
            hits: list[tuple[str, str, bytes, list[bytes], bool]] = []
            for off, blob in iter_embedded_xtx(data):
                if any(a <= off < b for a, b in spans):
                    continue
                hits.append((f"0x{off:x}", f"{off:06x}", blob, [], False))
            for o, _, pay in containers:
                bump("textures_embedded_arx")
                # a scene's textures are paletted by the lex models packed
                # beside them: nearest member first (its material 0 seeds
                # the base palette), the rest merged as companions
                lexes = [lp for _, lp in
                         sorted(lex_pool, key=lambda t: abs(t[0] - o))]
                for k, blob in iter_embedded_xtx(pay):
                    label = f"0x{o:x}" + (f"+0x{k:x}" if k else "")
                    stem = f"{o:06x}" + (f"_{k:x}" if k else "")
                    hits.append((label, stem, blob, lexes, True))
            for label, stem, blob, lexes, packed in hits:
                pk = "arx" if packed else ""
                sha = hashlib.sha1(blob).hexdigest()
                dup = seen.get(sha)
                if dup is not None:
                    bump("textures_embedded_dup")
                    rows.append([str(rel), label, len(blob), sha[:12],
                                 "", dup, pk])
                    continue
                decoded = decode_xtx(blob, lexes)
                if decoded is None:
                    bump("textures_embedded_undecodable")
                    rows.append([str(rel), label, len(blob), sha[:12],
                                 "", "(undecodable)", pk])
                    continue
                w, h, rgba, source = decoded
                dest = edir / rel.parent / f"{rel.name}_{stem}.png"
                dest.parent.mkdir(parents=True, exist_ok=True)
                write_png(dest, w, h, rgba)
                seen[sha] = str(dest.relative_to(browse))
                bump("textures_embedded_png")
                bump(f"textures_pal_{source}")
                rows.append([str(rel), label, len(blob), sha[:12],
                             str(dest.relative_to(browse)), "", pk])
            if i % 500 == 0:
                log(f"  {i}/{len(carriers)}  "
                    f"({stats.get('textures_embedded_png', 0)} embedded PNGs so far)")
        if rows:
            browse.mkdir(parents=True, exist_ok=True)
            with open(browse / "embedded_textures.csv", "w", newline="") as fh:
                w = _csv.writer(fh)
                w.writerow(["carrier", "offset", "size", "sha1",
                            "written", "duplicate_of", "packed"])
                w.writerows(rows)
        log(f"textures: {stats.get('textures_embedded_png', 0)} embedded PNGs "
            f"({stats.get('textures_embedded_dup', 0)} duplicates skipped, "
            f"{stats.get('textures_embedded_undecodable', 0)} undecodable; "
            f"{stats.get('textures_embedded_arx', 0)} ARX-packed members "
            f"decompressed) -> textures_png/_embedded/ + embedded_textures.csv")

    if "audio" in kinds:
        adir = browse / "audio"
        srcs = list(_dump_files(dump, (".vds", ".vdm")))
        log(f"audio: decoding {len(srcs)} voice streams -> WAV at {rate} Hz ...")
        for i, src in enumerate(srcs, 1):
            data = _load(src, bump)
            if not data:
                continue
            dest = adir / src.relative_to(dump).with_suffix(".wav")
            dest.parent.mkdir(parents=True, exist_ok=True)
            pcm, channels = decode_voice_stream(data)
            write_wav(dest, pcm, rate, channels)
            bump("audio_stereo" if channels == 2 else "audio_mono")
            if i % 5 == 0:
                log(f"  {i}/{len(srcs)}  ({src.relative_to(dump)})")
        log(f"audio: {stats.get('audio_stereo', 0)} stereo + "
            f"{stats.get('audio_mono', 0)} mono WAVs at {rate} Hz")

    if "banks" in kinds:
        import csv as _csv

        bdir = browse / "soundbanks"
        swds = list(_dump_files(dump, (".swd",)))
        smds = list(_dump_files(dump, (".smd",)))
        log(f"banks: carving {len(swds)} .swd wave banks -> per-instrument WAVs "
            f"(audition rate {BANK_RATE} Hz) ...")
        for i, src in enumerate(swds, 1):
            data = _load(src, bump)
            bank = parse_swd(data) if data else None
            if not bank:
                bump("banks_skipped")
                continue
            outd = bdir / src.relative_to(dump).with_suffix("")
            outd.mkdir(parents=True, exist_ok=True)
            for name, s, e, _rate in bank:
                safe = "".join(c if c.isalnum() or c in "._-#" else "_" for c in name)
                pcm = decode_spu_adpcm(_trim_sample(data, s, e))
                write_wav(outd / f"{safe}.wav", pcm, BANK_RATE)
                bump("bank_samples")
            bump("banks")
            if i % 25 == 0:
                log(f"  {i}/{len(swds)}  ({src.relative_to(dump)})")
        rows = []
        for src in smds:
            data = _load(src, bump)
            strs = smd_info(data) if data else None
            if strs is None:
                continue
            rows.append({
                "file": str(src.relative_to(dump)), "size": len(data),
                "title": strs[0] if strs else "",
                "game": strs[1] if len(strs) > 1 else "",
                "composer": strs[2] if len(strs) > 2 else "",
                "studio": strs[3] if len(strs) > 3 else "",
                "notes": strs[4] if len(strs) > 4 else "",
                "music": int(len(data) >= 5000),  # tiny SMDs are ambience stubs
            })
        if rows:
            bdir.mkdir(parents=True, exist_ok=True)
            with open(bdir / "smd_catalog.csv", "w", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=[
                    "file", "size", "title", "game", "composer", "studio",
                    "notes", "music"])
                w.writeheader()
                w.writerows(rows)
        log(f"banks: {stats.get('banks', 0)} banks -> "
            f"{stats.get('bank_samples', 0)} instrument samples; "
            f"{len(rows)} sequences catalogued in soundbanks/smd_catalog.csv "
            f"({sum(r['music'] for r in rows)} look like real music)")

    if "battle_audio" in kinds:
        import array as _array

        vdir = browse / "battle_audio"
        srcs = list(_dump_files(dump, (".bin",)))
        log(f"battle_audio: scanning {len(srcs)} .bin for battle sound banks ...")
        for i, src in enumerate(srcs, 1):
            data = _load(src, bump)
            chunks = parse_sed_bin(data) if data else None
            if not chunks:
                bump("battle_skipped_not_sed")
                continue
            outd = vdir / src.relative_to(dump).with_suffix("")
            # decode every swdm chunk; sample rate is per-sample native
            decoded: dict[str, tuple[bytes, int]] = {}
            for magic, cs, ce in chunks:
                if magic != b"swdm":
                    continue
                bank = parse_swd(data[cs:ce])
                if not bank:
                    continue
                for name, s, e, srate in bank:
                    safe = "".join(c if c.isalnum() or c in "._-#" else "_"
                                   for c in name)
                    if safe in decoded:  # same name in a later chunk pair
                        safe = f"{len(decoded)}_{safe}"
                    pcm = decode_spu_adpcm(_trim_sample(data, cs + s, cs + e))
                    decoded[safe] = (pcm, srate)
            if not decoded:
                bump("battle_banks_empty")
                continue
            # stitch stereo halves (X.aif.L/X.aif.R, X_L/X_R, X.L/X.R)
            for lname, rname in [(n, n[:-1] + "R") for n in list(decoded)
                                 if n.endswith((".L", "_L"))]:
                if rname not in decoded:
                    continue
                (lpcm, lrate), (rpcm, _) = decoded[lname], decoded[rname]
                L = _array.array("h"); L.frombytes(lpcm)
                R = _array.array("h"); R.frombytes(rpcm)
                n = min(len(L), len(R))
                st = _array.array("h", bytes(4 * n))
                st[0::2] = L[:n]
                st[1::2] = R[:n]
                if sys.byteorder == "big":
                    st.byteswap()
                del decoded[lname], decoded[rname]
                decoded[lname[:-2] + ".stereo"] = (st.tobytes(), lrate)
                bump("battle_stereo_pairs")
            outd.mkdir(parents=True, exist_ok=True)
            for safe, (pcm, srate) in decoded.items():
                channels = 2 if safe.endswith(".stereo") else 1
                write_wav(outd / f"{safe}.wav", pcm, srate, channels)
                bump("battle_samples")
            bump("battle_banks")
            if i % 50 == 0:
                log(f"  {i}/{len(srcs)}  ({src.relative_to(dump)})")
        log(f"battle_audio: {stats.get('battle_banks', 0)} banks -> "
            f"{stats.get('battle_samples', 0)} sample WAVs "
            f"({stats.get('battle_stereo_pairs', 0)} stereo pairs stitched, "
            f"{stats.get('battle_skipped_not_sed', 0)} .bin skipped as non-sed)")

    if "images" in kinds:
        idir = browse / "images"
        for src in _dump_files(dump, (".jpg", ".jpeg")):
            data = _load(src, bump)
            if not data:
                continue
            dest = idir / src.relative_to(dump)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            bump("images")
        # PS2ICON3D .res = the HDD-install resource bundle: a section table at
        # 0x10 of (offset, size) pairs — boot CNF, icon.sys, then the memory
        # card .ico 3D icon model (usually listed twice for copy/delete)
        _ICON_SECTIONS = ("boot.cnf", "icon.sys", "icon.ico", "icon2.ico")
        for src in _dump_files(dump, (".res",)):
            data = _load(src, bump)
            if data[:9] != b"PS2ICON3D":
                continue
            outd = idir / src.relative_to(dump).with_suffix("")
            outd.mkdir(parents=True, exist_ok=True)
            written = set()
            for k in range(4):
                off, size = struct.unpack_from("<II", data, 0x10 + 8 * k)
                if not size or off + size > len(data) or (off, size) in written:
                    continue
                written.add((off, size))
                (outd / _ICON_SECTIONS[k]).write_bytes(data[off : off + size])
                bump("icon_sections")
            if written:
                bump("icon_res")
        log(f"images: {stats.get('images', 0)} copied, "
            f"{stats.get('icon_res', 0)} PS2ICON3D resources unpacked "
            f"({stats.get('icon_sections', 0)} sections)")

    if "text" in kinds:
        xdir = browse / "text"
        for src in _dump_files(dump, (".txt",)):
            data = _load(src, bump)
            if not data:
                continue
            dest = xdir / src.relative_to(dump)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # game text is Shift-JIS with inline \NN control codes; recode to
            # UTF-8 so editors show the Japanese instead of mojibake
            try:
                dest.write_text(data.decode("cp932"), encoding="utf-8")
                bump("text_utf8")
            except UnicodeDecodeError:
                dest.write_bytes(data)
                bump("text_raw")
        # sniff pass: binary-extension files that are really NUL-separated
        # string tables (item descriptions, casino dialogue, dev configs)
        import re as _re

        for src in _dump_files(dump, (".dat", ".info", ".lst", ".uml",
                                      ".res", ".esp")):
            data = _load(src, bump)
            if not data:
                continue
            probe = data[:65536]
            printable = sum(1 for c in probe if 32 <= c < 127)
            nonzero = sum(1 for c in probe if c)
            if printable < 64 or printable / max(1, nonzero) <= 0.8:
                continue
            # game text is Shift-JIS but the dev-config .info files are
            # EUC-JP; EUC-JP misread as cp932 shows up as walls of halfwidth
            # katakana, so decode both ways and keep the cleaner reading
            def _mojibake(t: str) -> int:
                return sum(1 for c in t if "｡" <= c <= "ﾟ"
                           or c == "�")
            candidates = [data.decode(enc, "replace")
                          for enc in ("cp932", "euc_jp")]
            text = min(candidates, key=_mojibake)
            text = _re.sub("\x00+", "\n", text)
            dest = xdir / src.relative_to(dump)
            dest = dest.with_name(dest.name + ".strings.txt")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            bump("text_sniffed")
        log(f"text: {stats.get('text_utf8', 0)} transcoded to UTF-8, "
            f"{stats.get('text_raw', 0)} copied raw, "
            f"{stats.get('text_sniffed', 0)} string tables sniffed out of "
            f"binary-extension files")

    if "movies" in kinds:
        ffmpeg = detect_ffmpeg()
        if not ffmpeg:
            log("movies: SKIPPED — no ffmpeg found (install it or drop one in "
                "tools/ next to the exe). Raw .pss files play in VLC/ffplay.")
            stats["movies_skipped_no_ffmpeg"] = 1
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            mdir = browse / "movies"
            work = []
            for src in _dump_files(dump, (".pss", ".ipu")):
                with open(src, "rb") as fh:
                    if fh.read(4) == b"ARX\x00":
                        bump("movies_arx_skipped")
                        continue
                dest = mdir / src.relative_to(dump).with_suffix(".mp4")
                dest.parent.mkdir(parents=True, exist_ok=True)
                work.append((src, dest))
            jobs = max(2, (os.cpu_count() or 4) // 2)
            log(f"movies: converting {len(work)} files with {ffmpeg} "
                f"({jobs} at a time — this is the slow step)")
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futs = {pool.submit(convert_movie, ffmpeg, s, d): s for s, d in work}
                done = 0
                for fut in as_completed(futs):
                    done += 1
                    if fut.result():
                        bump("movies_mp4")
                    else:
                        bump("movies_failed")
                        log(f"  FAILED {futs[fut].relative_to(dump)}")
                    if done % 20 == 0 or done == len(work):
                        log(f"  {done}/{len(work)}")
            log(f"movies: {stats.get('movies_mp4', 0)} converted, "
                f"{stats.get('movies_failed', 0)} failed")

    return stats
