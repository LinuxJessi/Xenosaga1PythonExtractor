"""font.py — Episode I's bitmap fonts (``font0.tex`` / ``font1.tex``).

Both files hold the same 4,096-glyph table in two faces (font0 is the
dialogue face, font1 the menu face). Byte layout, cell addressing and the
proportional-metrics table are documented in docs/FORMATS.md § Fonts; the
short version:

    0x00000  0x20-byte header  (12 zero bytes, BE u16 0x013C 0x0051 0x00BC …)
    0x00020  block 0  0x3C000 bytes   -> glyph sheets 0 and 1
    0x3C020  0x20-byte header  (identical)
    0x3C040  block 1  0x3C000 bytes   -> glyph sheets 2 and 3
    0x78040  metrics: (left, right) byte pairs, one per proportional slot
             USA: 256 pairs (file = 492,096 bytes); JP: 96 pairs (491,776)

Each block is a PS2 GS upload of a 320x192 PSMCT32 image which the engine
re-reads as a 640x768 PSMT4 texture: 4 bits per pixel, the low two bits
forming one 4-level sheet and the high two bits another. A sheet is a
32x32 grid of 20x24 cells, so one file carries 4 x 1,024 = 4,096 cells.

The GS swizzle walk (PSMCT32 write, PSMT4 read) is the one worked out by
poketony for XS1KOR, the Korean localisation of the Japanese disc
(https://github.com/poketony/XS1KOR, ``xeno1font_ex.py``); it is
re-implemented here from that description. The cell-addressing and
metrics rules below were recovered from the unstripped USA executable
(``xglFontGetKanjiClutUV``, ``xglFontCheckProportional``,
``xglFontGetProportionalSize``, ``xglFontAscii2Euc``, ``xglFontGetStringWidth2``).

Addressing (EUC-JP codes, two bytes hi/lo, each 0xA1..0xFE):

* ``cell_of_code(code)`` — the "kanji path": JIS rows 1-8 linear
  (94 cells per row from cell 0), rows 13-15 folded in right after row 8
  (row 13 starts at cell 658), rows 16-47 kanji from cell 752. Unassigned
  JIS rows 9-12 alias kanji cells — they are *not* free space.
* ``prop_index(code)`` — the proportional slot (0..255) or -1. Digits are
  slots 0-9, A-Z 11-36, space 37, a-z 38-63; everything the USA build
  added (punctuation, Latin-1) lives in codes 0xD0C1..0xD3xx, slots
  permuted as the engine does it.
* ``cell_of_prop(slot)`` — proportional glyphs are drawn from a dedicated
  256-cell block, cells 3776..4031 (the last eight rows of sheet 3),
  *not* from the full-width JIS row-3 Latin cells. Proportional mode is
  what the dialogue printer uses; a slot's drawn width is
  ``right - left + 3`` pixels, fixed-width glyphs are 20.
* ``ascii_to_code(ch)`` — the engine's own ASCII -> EUC table.

Round trip: ``Font.from_bytes(b).to_bytes() == b`` for both retail files.

Usage:
    python font.py extract font0.tex --out DIR     # 4 PNG sheets + metrics.csv
    python font.py rebuild --ref font0.tex --sheets DIR --out new.tex
    python font.py info font0.tex
"""
from __future__ import annotations

import argparse
import csv
import struct
import sys
import zlib
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

HEADER = 0x20
BLOCK = 0x3C000
SHEET_W, SHEET_H = 640, 768
CELL_W, CELL_H = 20, 24
CELLS_PER_ROW = SHEET_W // CELL_W          # 32
CELLS_PER_SHEET = CELLS_PER_ROW * (SHEET_H // CELL_H)   # 1024
METRICS_OFF = 2 * (HEADER + BLOCK)         # 0x78040
PROP_CELL_BASE = 3744                      # cell = PROP_CELL_BASE + n, n = 32..287
PROP_SLOTS = 256
FULLWIDTH = (0, 19)                        # metrics pair meaning "not proportional"

# --- GS swizzle tables (PS2 GS PSMCT32 / PSMT4 page/block/column layout) ---
_BLOCK32 = [0, 1, 4, 5, 16, 17, 20, 21, 2, 3, 6, 7, 18, 19, 22, 23,
            8, 9, 12, 13, 24, 25, 28, 29, 10, 11, 14, 15, 26, 27, 30, 31]
_COLWORD32 = [0, 1, 4, 5, 8, 9, 12, 13, 2, 3, 6, 7, 10, 11, 14, 15]
_BLOCK4 = [0, 2, 8, 10, 1, 3, 9, 11, 4, 6, 12, 14, 5, 7, 13, 15,
           16, 18, 24, 26, 17, 19, 25, 27, 20, 22, 28, 30, 21, 23, 29, 31]
_COLWORD4 = [
    [0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13,
     0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13,
     2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15,
     2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15,
     8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5,
     8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5,
     10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7,
     10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7],
    [8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5,
     8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5,
     10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7,
     10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7,
     0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13,
     0, 1, 4, 5, 8, 9, 12, 13, 0, 1, 4, 5, 8, 9, 12, 13,
     2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15,
     2, 3, 6, 7, 10, 11, 14, 15, 2, 3, 6, 7, 10, 11, 14, 15],
]
_COLBYTE4 = ([0] * 8 + [2] * 8 + [4] * 8 + [6] * 8) * 2 + \
            ([1] * 8 + [3] * 8 + [5] * 8 + [7] * 8) * 2


def _ct32_word(x: int, y: int, dbw: int = 0x140) -> int:
    page = ((y >> 5) * (dbw >> 6)) + (x >> 6)
    px, py = x & 63, y & 31
    block = _BLOCK32[(px >> 3) + (py >> 3) * 8]
    bx, by = px & 7, py & 7
    return page * 2048 + block * 64 + (by >> 1) * 16 + _COLWORD32[bx + (by & 1) * 8]


def _t4_word(x: int, y: int, dbp: int, dbw: int = 64) -> Tuple[int, int]:
    dbw2 = dbw >> 1
    page = ((y >> 7) * ((dbw2 + 127) >> 7)) + (x >> 7)
    px, py = x & 127, y & 127
    block = _BLOCK4[(px >> 5) + (py >> 4) * 4]
    bx, by = px & 31, py & 15
    col = by >> 2
    cx, cy = bx, by & 3
    word = dbp * 64 + page * 2048 + block * 64 + col * 16 + _COLWORD4[col & 1][cx + cy * 32]
    return word, _COLBYTE4[cx + cy * 32]


_MAP: Optional[Tuple[list, list]] = None


def _layout() -> Tuple[list, list]:
    """Per sheet pixel (row-major 640x768): (byte offset in block, nibble shift).

    Same for both blocks. Built once (~1 s, pure Python) and cached.
    """
    global _MAP
    if _MAP is not None:
        return _MAP
    # GS word address -> block byte offset of that word, for the CT32 upload
    gs_to_byte = {}
    for y in range(192):
        for x in range(320):
            gs_to_byte[_ct32_word(x, y)] = (y * 320 + x) * 4
    offs = [0] * (SHEET_W * SHEET_H)
    shifts = [0] * (SHEET_W * SHEET_H)
    i = 0
    for tile in range(SHEET_H // 128):
        dbp = (tile * SHEET_W) // 4
        for row in range(128):
            for col in range(SHEET_W):
                word, cb = _t4_word(col, row, dbp)
                offs[i] = gs_to_byte[word] + (cb >> 1)
                shifts[i] = 4 if (cb & 1) else 0
                i += 1
    _MAP = (offs, shifts)
    return _MAP


def decode_block(block: bytes) -> Tuple[bytearray, bytearray]:
    """One 0x3C000 block -> two 640x768 sheets of values 0..3 (row-major)."""
    offs, shifts = _layout()
    lo = bytearray(SHEET_W * SHEET_H)
    hi = bytearray(SHEET_W * SHEET_H)
    for i in range(SHEET_W * SHEET_H):
        nib = (block[offs[i]] >> shifts[i]) & 0xF
        lo[i] = nib & 3
        hi[i] = nib >> 2
    return lo, hi


def encode_block(lo: Sequence[int], hi: Sequence[int]) -> bytes:
    offs, shifts = _layout()
    out = bytearray(BLOCK)
    for i in range(SHEET_W * SHEET_H):
        nib = (lo[i] & 3) | ((hi[i] & 3) << 2)
        out[offs[i]] |= nib << shifts[i]
    return bytes(out)


# --- engine addressing (SLUS_204.69) -----------------------------------------

# xglFontAscii2Euc's table for 0x20..0x7F (0x7F -> space). Punctuation was
# moved into the USA-only rows 0xD1/0xD2; '\\' lands on the yen glyph as in
# every JIS font.
ASCII2EUC = [
    0xA1A1, 0xD1B5, 0xD1C2, 0xD1E5, 0xD2A9, 0xD1E6, 0xD2A8, 0xD1C0,
    0xD1CB, 0xD1CC, 0xD1B4, 0xD1FE, 0xD1BA, 0xD1FD, 0xD1B9, 0xD1D4,
    0xA3B0, 0xA3B1, 0xA3B2, 0xA3B3, 0xA3B4, 0xA3B5, 0xA3B6, 0xA3B7,
    0xA3B8, 0xA3B9, 0xD1BC, 0xD1BB, 0xD1C7, 0xD1FC, 0xD1C8, 0xD1B7,
    0xD1AD, 0xA3C1, 0xA3C2, 0xA3C3, 0xA3C4, 0xA3C5, 0xA3C6, 0xA3C7,
    0xA3C8, 0xA3C9, 0xA3CA, 0xA3CB, 0xA3CC, 0xA3CD, 0xA3CE, 0xA3CF,
    0xA3D0, 0xA3D1, 0xA3D2, 0xA3D3, 0xA3D4, 0xA3D5, 0xA3D6, 0xA3D7,
    0xA3D8, 0xA3D9, 0xA3DA, 0xD1CD, 0xD1A5, 0xD1CE, 0xD1D8, 0xD1D3,
    0xD1DA, 0xA3E1, 0xA3E2, 0xA3E3, 0xA3E4, 0xA3E5, 0xA3E6, 0xA3E7,
    0xA3E8, 0xA3E9, 0xA3EA, 0xA3EB, 0xA3EC, 0xA3ED, 0xA3EE, 0xA3EF,
    0xA3F0, 0xA3F1, 0xA3F2, 0xA3F3, 0xA3F4, 0xA3F5, 0xA3F6, 0xA3F7,
    0xA3F8, 0xA3F9, 0xA3FA, 0xD1CF, 0xD1D1, 0xD1D0, 0xD1E8, 0xA1A1,
]
_ROW3_BASE = (-16, 10, 37, 0)   # xglFontCheckProportional's table for row 3

# Operand byte count per control byte 0x00..0x1F (xglFontGetSPcodeSize).
# None = variable: 0x08 takes a flag byte whose set bits add 2,2,3,2,2 bytes
# (bits 0-4); 0x15 takes a length byte L and L more bytes.
CONTROL_OPERANDS = [0, 1, 1, 1, 1, 1, 3, 0, None, 0, 0, 0, 3, 1, 5, 3,
                    5, 6, 2, 2, 0, None, 0, 7, 1, 1, 0, 1, 0, 0, 0, 0]
_CTRL08_BITS = (2, 2, 3, 2, 2, 0, 0, 0)


def control_length(data: bytes, i: int) -> int:
    """Total byte length of the control sequence starting at data[i] (< 0x20)."""
    c = data[i]
    n = CONTROL_OPERANDS[c]
    if n is not None:
        return 1 + n
    if c == 0x08:
        flags = data[i + 1] if i + 1 < len(data) else 0
        return 2 + sum(_CTRL08_BITS[b] for b in range(8) if flags >> b & 1)
    return 2 + (data[i + 1] if i + 1 < len(data) else 0)      # 0x15


def ascii_to_code(ch: int) -> int:
    """ASCII byte (0x20..0x7F) -> EUC code as the engine maps it."""
    return ASCII2EUC[ch - 0x20]


def cell_of_code(code: int) -> int:
    """Kanji-path cell for an EUC code (xglFontGetKanjiClutUV)."""
    hi, lo = code >> 8, code & 0xFF
    v = (lo + 94 * hi - 0xA1) & 0xFFFF
    v += 0xC4E2 if hi < 0xAD else (0xC30C if hi < 0xB0 else 0xC250)
    return v & 0xFFFF


def prop_index(code: int) -> int:
    """Proportional slot for an EUC code, or -1 (xglFontCheckProportional)."""
    hi, lo = code >> 8, code & 0xFF
    if hi == 0xA7:
        return {0xA1: 0x40, 0xA3: 0x41, 0xA6: 0x42}.get(lo, -1)
    if hi == 0xA1:
        return {0xA1: 37, 0xA5: 10}.get(lo, -1)
    if hi == 0xA3:
        return _ROW3_BASE[(lo - 0xA0) >> 5] + (lo & 0x1F)
    if hi < 0xD0:
        return -1
    v = (94 * hi + lo - 0x4D21) & 0xFFFF
    r = (~(v >> 5)) & 7
    if r < 2:
        r ^= 1
    return r * 32 + (v & 0x1F)


def prop_offset(slot: int) -> int:
    """Linear offset n (32..287) from code 0xD0A1 of the code owning ``slot``."""
    r, k = slot >> 5, slot & 0x1F
    r0 = r ^ 1 if r < 2 else r
    return (7 - r0) * 32 + k + 32


def code_of_prop(slot: int) -> int:
    """The USA-row EUC code that selects proportional ``slot`` (inverse of
    prop_index for the 0xD0.. rows)."""
    n = prop_offset(slot)
    return ((0xD0 + n // 94) << 8) | (0xA1 + n % 94)


def cell_of_prop(slot: int) -> int:
    """Cell drawn for a proportional slot (the 256-cell block, 3776..4031)."""
    return PROP_CELL_BASE + prop_offset(slot)


def glyph_cell(code: int) -> int:
    """Cell the dialogue printer draws for ``code`` in proportional mode."""
    p = prop_index(code)
    return cell_of_prop(p) if p >= 0 else cell_of_code(code)


def advance(pair: Tuple[int, int]) -> int:
    """Pixels the printer advances for a proportional slot's (left, right)."""
    left, right = pair
    if left == 0:
        return 20   # engine substitutes its default width; retail uses this
    return right - left + 3        # xglFontGetProportionalSize, mode 0


# --- the file --------------------------------------------------------------

class FontError(ValueError):
    pass


class Font:
    def __init__(self, headers: List[bytes], sheets: List[bytearray],
                 metrics: List[Tuple[int, int]], tail: bytes = b""):
        self.headers = headers        # two 0x20-byte headers
        self.sheets = sheets          # four 640*768 bytearrays, values 0..3
        self.metrics = metrics        # (left, right) per slot
        self.tail = tail              # bytes after the metrics, if any

    @classmethod
    def from_bytes(cls, data: bytes) -> "Font":
        if len(data) < METRICS_OFF:
            raise FontError(f"font file too short ({len(data)} bytes)")
        headers, sheets = [], []
        for m in range(2):
            base = m * (HEADER + BLOCK)
            headers.append(bytes(data[base:base + HEADER]))
            lo, hi = decode_block(data[base + HEADER:base + HEADER + BLOCK])
            sheets += [lo, hi]
        rest = data[METRICS_OFF:]
        npairs = len(rest) // 2
        metrics = [(rest[2 * i], rest[2 * i + 1]) for i in range(npairs)]
        return cls(headers, sheets, metrics, bytes(rest[npairs * 2:]))

    @classmethod
    def load(cls, path) -> "Font":
        return cls.from_bytes(Path(path).read_bytes())

    def to_bytes(self) -> bytes:
        out = bytearray()
        for m in range(2):
            out += self.headers[m]
            out += encode_block(self.sheets[2 * m], self.sheets[2 * m + 1])
        for left, right in self.metrics:
            out += bytes((left & 0xFF, right & 0xFF))
        out += self.tail
        return bytes(out)

    # cells
    @property
    def cell_count(self) -> int:
        return CELLS_PER_SHEET * len(self.sheets)

    def cell(self, index: int) -> List[List[int]]:
        """24 rows of 20 values (0..3) for cell ``index``."""
        sheet = self.sheets[index // CELLS_PER_SHEET]
        i = index % CELLS_PER_SHEET
        x0, y0 = (i % CELLS_PER_ROW) * CELL_W, (i // CELLS_PER_ROW) * CELL_H
        return [list(sheet[(y0 + r) * SHEET_W + x0:(y0 + r) * SHEET_W + x0 + CELL_W])
                for r in range(CELL_H)]

    def set_cell(self, index: int, rows: Sequence[Sequence[int]]) -> None:
        if len(rows) != CELL_H or any(len(r) != CELL_W for r in rows):
            raise FontError("cell must be 24 rows of 20 values")
        sheet = self.sheets[index // CELLS_PER_SHEET]
        i = index % CELLS_PER_SHEET
        x0, y0 = (i % CELLS_PER_ROW) * CELL_W, (i // CELLS_PER_ROW) * CELL_H
        for r in range(CELL_H):
            sheet[(y0 + r) * SHEET_W + x0:(y0 + r) * SHEET_W + x0 + CELL_W] = \
                bytes(v & 3 for v in rows[r])

    def ink_bounds(self, index: int) -> Optional[Tuple[int, int]]:
        """(leftmost, rightmost) inked columns of a cell, or None if blank."""
        cols = [c for c in range(CELL_W)
                if any(row[c] for row in self.cell(index))]
        return (cols[0], cols[-1]) if cols else None

    def set_metrics(self, slot: int, left: int, right: int) -> None:
        while len(self.metrics) <= slot:
            self.metrics.append(FULLWIDTH)
        self.metrics[slot] = (left, right)

    def width_of_code(self, code: int) -> int:
        p = prop_index(code)
        if p < 0 or p >= len(self.metrics):
            return 20
        return advance(self.metrics[p])

    def width_of_bytes(self, text: bytes) -> int:
        """Pixel width of a rendered line (proportional mode on, no wrap),
        mirroring xglFontGetStringWidth2: control codes cost nothing,
        ASCII goes through the engine's table, bytes >= 0x80 pair up."""
        w, i = 0, 0
        while i < len(text):
            b = text[i]
            if b == 0 or b == 0x0A:
                break
            if b < 0x20:
                i += control_length(text, i)
                continue
            if b < 0x80:
                w += self.width_of_code(ascii_to_code(b))
                i += 1
            else:
                code = (b << 8) | (text[i + 1] if i + 1 < len(text) else 0xA1)
                w += self.width_of_code(code)
                i += 2
        return w


# --- PNG (8-bit grayscale, stdlib only) -------------------------------------

def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def sheet_to_png(sheet: Sequence[int], width: int = SHEET_W,
                 height: int = SHEET_H) -> bytes:
    levels = bytes((0, 85, 170, 255))
    raw = bytearray()
    for y in range(height):
        raw.append(0)
        raw += bytes(levels[v & 3] for v in sheet[y * width:(y + 1) * width])
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + _png_chunk(b"IEND", b""))


def png_to_gray(data: bytes) -> Tuple[int, int, bytearray]:
    """Decode a non-interlaced 8-bit PNG (gray, gray+alpha, RGB, RGBA, or
    8-bit palette) to (width, height, gray bytes). Enough for edited sheets."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise FontError("not a PNG file")
    pos, idat, plte = 8, bytearray(), b""
    width = height = depth = ctype = 0
    while pos < len(data):
        (ln,) = struct.unpack_from(">I", data, pos)
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if tag == b"IHDR":
            width, height, depth, ctype, _, _, inter = struct.unpack(">IIBBBBB", body)
            if depth != 8 or inter:
                raise FontError("PNG must be 8-bit, non-interlaced")
        elif tag == b"PLTE":
            plte = body
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + ln
    bpp = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    raw = zlib.decompress(bytes(idat))
    stride = width * bpp
    prev = bytearray(stride)
    gray = bytearray(width * height)
    p = 0
    for y in range(height):
        f = raw[p]
        line = bytearray(raw[p + 1:p + 1 + stride])
        p += 1 + stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 0xFF
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        elif f != 0:
            raise FontError(f"bad PNG filter {f}")
        row = y * width
        if ctype == 3:
            for x in range(width):
                i = line[x] * 3
                gray[row + x] = (plte[i] * 299 + plte[i + 1] * 587 + plte[i + 2] * 114) // 1000
        elif bpp == 1:
            gray[row:row + width] = line
        else:
            for x in range(width):
                i = x * bpp
                if bpp == 2:
                    gray[row + x] = line[i]
                else:
                    gray[row + x] = (line[i] * 299 + line[i + 1] * 587 + line[i + 2] * 114) // 1000
        prev = line
    return width, height, gray


def quantize(gray: Sequence[int]) -> bytearray:
    """8-bit gray -> 4 levels (thresholds between the 0/85/170/255 steps)."""
    return bytearray(0 if g <= 42 else 1 if g <= 127 else 2 if g <= 212 else 3
                     for g in gray)


# --- CLI -------------------------------------------------------------------

def cmd_extract(tex: str, out: str) -> int:
    font = Font.load(tex)
    outp = Path(out)
    outp.mkdir(parents=True, exist_ok=True)
    stem = Path(tex).stem
    for i, sheet in enumerate(font.sheets):
        (outp / f"{stem}_sheet{i}.png").write_bytes(sheet_to_png(sheet))
    with open(outp / f"{stem}_metrics.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["slot", "code", "cell", "left", "right", "advance"])
        for slot, (l, r) in enumerate(font.metrics):
            code = code_of_prop(slot) if slot < PROP_SLOTS else 0
            w.writerow([slot, f"{code:04X}", cell_of_prop(slot), l, r, advance((l, r))])
    print(f"{tex}: 4 sheets + {len(font.metrics)} metrics -> {outp}")
    return 0


def cmd_rebuild(ref: str, sheets_dir: str, out: str, metrics_csv: Optional[str]) -> int:
    font = Font.load(ref)
    stem = Path(ref).stem
    for i in range(4):
        p = Path(sheets_dir) / f"{stem}_sheet{i}.png"
        if not p.exists():
            continue
        w, h, gray = png_to_gray(p.read_bytes())
        if (w, h) != (SHEET_W, SHEET_H):
            raise FontError(f"{p}: sheet must be {SHEET_W}x{SHEET_H}, got {w}x{h}")
        font.sheets[i] = quantize(gray)
    if metrics_csv:
        with open(metrics_csv, newline="") as fh:
            for row in csv.DictReader(fh):
                font.set_metrics(int(row["slot"]), int(row["left"]), int(row["right"]))
    Path(out).write_bytes(font.to_bytes())
    print(f"wrote {out}")
    return 0


def cmd_info(tex: str) -> int:
    data = Path(tex).read_bytes()
    font = Font.from_bytes(data)
    ok = font.to_bytes() == data
    used = sum(1 for m in font.metrics if m != FULLWIDTH)
    print(f"{tex}: {len(data)} bytes, {len(font.metrics)} metric slots "
          f"({used} proportional), round-trip {'OK' if ok else 'MISMATCH'}")
    for ch in "AWi0!.":
        code = ascii_to_code(ord(ch))
        p = prop_index(code)
        print(f"  {ch!r}: code {code:04X} slot {p:3d} cell {glyph_cell(code)} "
              f"metrics {font.metrics[p] if 0 <= p < len(font.metrics) else None} "
              f"width {font.width_of_code(code)}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("extract")
    pe.add_argument("tex")
    pe.add_argument("--out", required=True)
    pr = sub.add_parser("rebuild")
    pr.add_argument("--ref", required=True, help="retail .tex to start from")
    pr.add_argument("--sheets", required=True, help="directory with edited sheet PNGs")
    pr.add_argument("--metrics", help="edited metrics CSV (optional)")
    pr.add_argument("--out", required=True)
    pi = sub.add_parser("info")
    pi.add_argument("tex")
    a = ap.parse_args(argv)
    if a.cmd == "extract":
        return cmd_extract(a.tex, a.out)
    if a.cmd == "rebuild":
        return cmd_rebuild(a.ref, a.sheets, a.out, a.metrics)
    return cmd_info(a.tex)


if __name__ == "__main__":
    sys.exit(main())
