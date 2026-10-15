"""glyphs.py — put a new script's glyphs into the game's fonts.

The dialogue font has two kinds of room for a translation that needs
letters the disc never had (Arabic, Hebrew, Devanagari, Cyrillic, …):

* **proportional slots** — the 256-entry block the printer uses for Latin
  text (``font.py``: cells 3776..4031, metrics at 0x78040). Each slot has
  its own (left, right) ink bounds, so glyphs get real proportional
  spacing. Slots the English game needs (digits, A-Z, a-z, space, basic
  punctuation) stay reserved by default; the Latin-1 accents and the
  symbol rows (~160 slots) are free.
* **wide cells** — the 2,992 kanji cells (codes 0xB0A1..0xCFEE), drawn at
  a fixed 20 px. Overflow goes here; fine for scripts whose letters are
  near-uniform in width, or for rare conjuncts.

A ``GlyphSet`` maps *glyph keys* to slots and bitmaps and is saved as
``glyphset.json`` so ``langpack.py`` can encode text with it and so a
later run can keep the same codes. Keys are the strings ``langpack``
produces: ``u:<text>`` for a unit of text rendered as a whole (a letter, a
presentation form, a syllable), ``g:<font>:<gid>`` for one glyph id and
``c:<font>:<gid>@<dx>,<dy>;…`` for a base+mark cluster drawn as one cell.

Bitmaps come from one of two sources:

1. A TrueType/OpenType font, rasterised here (``TTFRenderer``). Needs the
   optional ``fonttools`` (outlines) and ``uharfbuzz`` (shaping) packages:
   ``pip install fonttools uharfbuzz``. Everything else — the scanline
   rasteriser, quantisation, cell packing — is standard library, like the
   rest of the kit.
2. A PNG sheet of hand-drawn 20x24 cells plus a key list (``from_png``),
   for a translator working in an image editor; no extra packages.

XS1KOR (https://github.com/poketony/XS1KOR), poketony's Korean
localisation of the Japanese disc, is the proof this works: 2,048 Hangul
cells rendered from a TTF into the kanji area, in exactly these sheets.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import font as fontmod
from font import (ASCII2EUC, CELL_H, CELL_W, Font, cell_of_code, cell_of_prop,
                  code_of_prop, prop_index, PROP_SLOTS)

WIDE_FIRST, WIDE_LAST = 752, 3743       # kanji cells, rows 16..47
KANJI_ROW_BASE = 752


class GlyphError(ValueError):
    pass


def code_of_wide_cell(cell: int) -> int:
    """EUC code that selects a kanji-area cell through the kanji path."""
    n = cell - KANJI_ROW_BASE
    if not (0 <= n <= WIDE_LAST - WIDE_FIRST):
        raise GlyphError(f"cell {cell} is not in the kanji area")
    return ((0xB0 + n // 94) << 8) | (0xA1 + n % 94)


def reserved_prop_slots(keep_latin: bool = True) -> set:
    """Slots the retail English text draws from (via the engine's ASCII table)."""
    keep = set()
    for ch in range(0x20, 0x7F):
        if not keep_latin and (0x41 <= ch <= 0x5A or 0x61 <= ch <= 0x7A):
            continue
        p = prop_index(ASCII2EUC[ch - 0x20])
        if p >= 0:
            keep.add(p)
    return keep


# --- bitmaps ---------------------------------------------------------------

Bitmap = List[List[int]]        # CELL_H rows of CELL_W values 0..3


def blank() -> Bitmap:
    return [[0] * CELL_W for _ in range(CELL_H)]


def ink_bounds(bm: Bitmap) -> Optional[Tuple[int, int]]:
    cols = [x for x in range(CELL_W) if any(row[x] for row in bm)]
    return (cols[0], cols[-1]) if cols else None


def shift_x(bm: Bitmap, dx: int) -> Bitmap:
    out = blank()
    for y in range(CELL_H):
        for x in range(CELL_W):
            if 0 <= x - dx < CELL_W:
                out[y][x] = bm[y][x - dx]
    return out


class Glyph:
    def __init__(self, key: str, bitmap: Bitmap, advance_hint: Optional[int] = None):
        self.key = key
        self.bitmap = bitmap
        self.bounds = ink_bounds(bitmap)
        self.advance_hint = advance_hint    # for blank glyphs (spaces)

    @property
    def ink_width(self) -> int:
        return self.bounds[1] - self.bounds[0] + 1 if self.bounds else 0


# --- allocation --------------------------------------------------------------

class GlyphSet:
    def __init__(self, source: str = ""):
        self.source = source
        self.glyphs: Dict[str, Glyph] = {}
        self.slots: Dict[str, dict] = {}     # key -> {tier, slot|cell, code, left, right}

    def add(self, g: Glyph) -> None:
        self.glyphs[g.key] = g

    def allocate(self, order: Sequence[str], keep_latin: bool = True,
                 prop_only: bool = False, wide_only: bool = False) -> None:
        """Give every key in ``order`` (most frequent first) a slot: free
        proportional slots first, then kanji cells. Keys already placed keep
        their place."""
        used_prop = reserved_prop_slots(keep_latin) | {
            v["slot"] for v in self.slots.values() if v["tier"] == "prop"}
        used_wide = {v["cell"] for v in self.slots.values() if v["tier"] == "wide"}
        free_prop = [p for p in range(PROP_SLOTS) if p not in used_prop]
        free_wide = [c for c in range(WIDE_FIRST, WIDE_LAST + 1) if c not in used_wide]
        for key in order:
            if key in self.slots:
                continue
            g = self.glyphs.get(key)
            if g is None:
                raise GlyphError(f"no bitmap for key {key!r}")
            if not wide_only and free_prop and (g.ink_width <= 18 or prop_only):
                p = free_prop.pop(0)
                left, right = self._prop_metrics(g)
                self.slots[key] = {"tier": "prop", "slot": p, "cell": cell_of_prop(p),
                                   "code": code_of_prop(p), "left": left, "right": right}
            elif not prop_only and free_wide:
                c = free_wide.pop(0)
                self.slots[key] = {"tier": "wide", "cell": c, "code": code_of_wide_cell(c),
                                   "left": 0, "right": 19}
            else:
                raise GlyphError(f"out of font cells at key {key!r} "
                                 f"({len(self.slots)} placed)")

    @staticmethod
    def _prop_metrics(g: Glyph) -> Tuple[int, int]:
        """(left, right) for a proportional slot. The engine reads left == 0
        as "no metrics, use the default width", so ink always starts at
        column 1 (``apply`` shifts the bitmap to match) and ends by 19."""
        if g.bounds is None:
            w = max(2, min(18, (g.advance_hint or 6) - 2))
            return 1, w                       # blank slot with a usable advance
        left, right = g.bounds
        shift = 1 - left if left < 1 else 0
        return 1 if shift else left, min(19, right + shift)

    def width(self, key: str) -> int:
        s = self.slots[key]
        return fontmod.advance((s["left"], s["right"])) if s["tier"] == "prop" else 20

    # applying
    def apply(self, font: Font) -> None:
        """Draw every placed glyph into ``font`` (cells + metrics)."""
        for key, s in self.slots.items():
            g = self.glyphs[key]
            bm = g.bitmap
            if s["tier"] == "prop":
                # the slot's metrics are the ink bounds; move the ink to them
                if g.bounds and g.bounds[0] != s["left"]:
                    bm = shift_x(bm, s["left"] - g.bounds[0])
                font.set_metrics(s["slot"], s["left"], s["right"])
            else:
                if g.bounds:   # centre in the 20 px cell
                    w = g.ink_width
                    bm = shift_x(bm, (CELL_W - w) // 2 - g.bounds[0])
            font.set_cell(s["cell"], bm)

    # persistence
    def to_json(self) -> str:
        return json.dumps({
            "source": self.source,
            "slots": {k: dict(v, code=f"{v['code']:04X}") for k, v in self.slots.items()},
            "bitmaps": {k: ["".join(str(v) for v in row) for row in g.bitmap]
                        for k, g in self.glyphs.items()},
        }, ensure_ascii=False, indent=0)

    @classmethod
    def from_json(cls, text: str) -> "GlyphSet":
        d = json.loads(text)
        gs = cls(d.get("source", ""))
        for k, rows in d.get("bitmaps", {}).items():
            gs.add(Glyph(k, [[int(ch) for ch in row] for row in rows]))
        for k, v in d.get("slots", {}).items():
            gs.slots[k] = dict(v, code=int(v["code"], 16))
        return gs

    def save(self, path) -> None:
        Path(path).write_text(self.to_json(), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "GlyphSet":
        return cls.from_json(Path(path).read_text(encoding="utf-8"))

    def code_bytes(self, key: str) -> bytes:
        c = self.slots[key]["code"]
        return bytes((c >> 8, c & 0xFF))


# --- PNG cells (no dependencies) -----------------------------------------------

def from_png(png_path, keys: Iterable[str], source: str = "") -> GlyphSet:
    """Cells read left-to-right, top-to-bottom from a 20x24-grid PNG."""
    w, h, gray = fontmod.png_to_gray(Path(png_path).read_bytes())
    if w % CELL_W or h % CELL_H:
        raise GlyphError(f"sheet {w}x{h} is not a multiple of {CELL_W}x{CELL_H}")
    q = fontmod.quantize(gray)
    per_row = w // CELL_W
    gs = GlyphSet(source or str(png_path))
    for i, key in enumerate(keys):
        cx, cy = (i % per_row) * CELL_W, (i // per_row) * CELL_H
        if cy + CELL_H > h:
            raise GlyphError("more keys than cells in the sheet")
        bm = [list(q[(cy + r) * w + cx:(cy + r) * w + cx + CELL_W]) for r in range(CELL_H)]
        gs.add(Glyph(key, bm))
    return gs


# --- TrueType rendering (optional fonttools + uharfbuzz) ---------------------------

class TTFRenderer:
    """Shape text with HarfBuzz and rasterise glyph outlines into cells.

    ``px`` is the em size in pixels (20 fits most scripts in a 24 px cell;
    Devanagari with stacked marks may want 16-18), ``baseline`` the cell
    row the baseline sits on (default 17: six rows for descenders).
    """

    def __init__(self, path, px: float = 20.0, baseline: int = 17,
                 face_index: int = 0, features: Optional[dict] = None,
                 supersample: int = 4):
        try:
            from fontTools.ttLib import TTFont, TTCollection
            import uharfbuzz as hb
        except ImportError as ex:   # pragma: no cover
            raise GlyphError("TrueType rendering needs the optional packages: "
                             "pip install fonttools uharfbuzz") from ex
        self.hb = hb
        p = Path(path)
        data = p.read_bytes()
        if p.suffix.lower() == ".ttc":
            self.tt = TTCollection(str(p)).fonts[face_index]
        else:
            self.tt = TTFont(str(p))
        self.upem = self.tt["head"].unitsPerEm
        self.glyph_set = self.tt.getGlyphSet()
        self.glyph_order = self.tt.getGlyphOrder()
        face = hb.Face(data, face_index)
        self.hbfont = hb.Font(face)
        self.hbfont.scale = (self.upem, self.upem)
        self.px = px
        self.baseline = baseline
        # one cell per glyph: keep required ligatures (lam-alef, conjuncts)
        # but not the optional wide ones a 20 px cell cannot hold
        self.features = {"liga": False, "dlig": False, "calt": False} if features is None else features
        self.ss = supersample
        self.name = p.stem
        self.scale = px / self.upem

    # shaping
    def shape(self, text: str, direction: str = "ltr", script: Optional[str] = None,
              language: Optional[str] = None):
        """-> list of (gid, cluster, x_advance, x_offset, y_offset) in font units."""
        hb = self.hb
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        buf.direction = direction
        if script:
            buf.script = script
        if language:
            buf.language = language
        hb.shape(self.hbfont, buf, self.features)
        return [(i.codepoint, i.cluster, p.x_advance, p.x_offset, p.y_offset)
                for i, p in zip(buf.glyph_infos, buf.glyph_positions)]

    def gid_name(self, gid: int) -> str:
        return self.glyph_order[gid]

    def advance_px(self, gid: int) -> float:
        return self.hbfont.get_glyph_h_advance(gid) * self.scale

    # outlines
    def contours(self, gid: int) -> List[List[Tuple[float, float]]]:
        """Flattened closed contours in font units."""
        from fontTools.pens.recordingPen import DecomposingRecordingPen
        pen = DecomposingRecordingPen(self.glyph_set)   # flattens TrueType components
        self.glyph_set[self.gid_name(gid)].draw(pen)
        out: List[List[Tuple[float, float]]] = []
        cur: List[Tuple[float, float]] = []
        last = (0.0, 0.0)
        for op, args in pen.value:
            if op == "moveTo":
                if cur:
                    out.append(cur)
                cur = [args[0]]
                last = args[0]
            elif op == "lineTo":
                cur.append(args[0])
                last = args[0]
            elif op == "qCurveTo":
                pts = list(args)
                if pts[-1] is None:          # TrueType implied on-curve start
                    pts = pts[:-1]
                # expand implied on-curve points between consecutive off-curves
                ctrls = pts[:-1]
                end = pts[-1]
                p0 = last
                for i, c in enumerate(ctrls):
                    nxt = ctrls[i + 1] if i + 1 < len(ctrls) else end
                    p2 = nxt if i + 1 == len(ctrls) else ((c[0] + nxt[0]) / 2, (c[1] + nxt[1]) / 2)
                    for t in range(1, 9):
                        u = t / 8
                        x = (1 - u) ** 2 * p0[0] + 2 * (1 - u) * u * c[0] + u * u * p2[0]
                        y = (1 - u) ** 2 * p0[1] + 2 * (1 - u) * u * c[1] + u * u * p2[1]
                        cur.append((x, y))
                    p0 = p2
                last = end
            elif op == "curveTo":
                c1, c2, end = args[-3], args[-2], args[-1]
                p0 = last
                for t in range(1, 9):
                    u = t / 8
                    x = ((1 - u) ** 3 * p0[0] + 3 * (1 - u) ** 2 * u * c1[0]
                         + 3 * (1 - u) * u * u * c2[0] + u ** 3 * end[0])
                    y = ((1 - u) ** 3 * p0[1] + 3 * (1 - u) ** 2 * u * c1[1]
                         + 3 * (1 - u) * u * u * c2[1] + u ** 3 * end[1])
                    cur.append((x, y))
                last = end
            elif op in ("closePath", "endPath"):
                if cur:
                    out.append(cur)
                cur = []
        if cur:
            out.append(cur)
        return out

    # rasterising
    def raster(self, placements: Sequence[Tuple[int, float, float]],
               x_origin: float = 1.0) -> Bitmap:
        """Draw glyphs (gid, x_px, y_px offsets from the pen) into one cell.
        Even-odd scanline fill at ``supersample`` x resolution, then averaged
        to the four levels the font supports."""
        ss = self.ss
        W, H = CELL_W * ss, CELL_H * ss
        cov = [[0] * W for _ in range(H)]
        edges = []
        for gid, dx, dy in placements:
            for contour in self.contours(gid):
                pts = [((x_origin + dx + x * self.scale) * ss,
                        (self.baseline - dy - y * self.scale) * ss) for x, y in contour]
                for i in range(len(pts)):
                    x0, y0 = pts[i]
                    x1, y1 = pts[(i + 1) % len(pts)]
                    if y0 != y1:
                        edges.append((x0, y0, x1, y1))
        for sy in range(H):
            yc = sy + 0.5
            xs = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yc < y1) or (y1 <= yc < y0):
                    xs.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
            xs.sort()
            for i in range(0, len(xs) - 1, 2):
                a, b = int(math.floor(xs[i] + 0.5)), int(math.floor(xs[i + 1] + 0.5))
                for sx in range(max(a, 0), min(b, W)):
                    cov[sy][sx] = 1
        bm = blank()
        n = ss * ss
        for y in range(CELL_H):
            for x in range(CELL_W):
                s = sum(cov[y * ss + j][x * ss + i] for j in range(ss) for i in range(ss))
                bm[y][x] = min(3, (s * 4 + n // 2) // n) if s else 0
        return bm

    def render_gid(self, gid: int) -> Glyph:
        bm = self.raster([(gid, 0.0, 0.0)])
        return Glyph(f"g:{self.name}:{gid}", bm, round(self.advance_px(gid)))

    def render_cluster(self, parts: Sequence[Tuple[int, float, float]], key: str) -> Glyph:
        return Glyph(key, self.raster(parts))

    def render_text(self, text: str, key: str, direction: str = "ltr",
                    script: Optional[str] = None) -> Glyph:
        """Shape ``text`` as one unit and draw it into one cell (left-aligned)."""
        pen = 0.0
        parts = []
        for gid, _, adv, xo, yo in self.shape(text, direction, script):
            parts.append((gid, (pen + xo) * self.scale, yo * self.scale))
            pen += adv
        return Glyph(key, self.raster(parts), round(pen * self.scale))
