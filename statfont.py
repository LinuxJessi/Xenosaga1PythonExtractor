"""statfont.py — the battle screen's own font: the command bars and labels.

The battle HUD ("Knuckle", "Drone M", "L1 CHANGE BOOST R1", the Ether and
Items lists) is not drawn with font0/font1. The battle overlay (OV01) has
its own printer, ``menuFontPut``, which looks each byte up in a 230-byte
table (``menuFontIdxGet``: ' ', '0'-'9', 'A'-'Z', 'a'-'z', then mostly
unused slots with '.', ''', '/', '-' at 147, 152, 175, 195) and draws cell
*index* of a 25-column grid of 10x18-pixel glyphs that lives in the status
sheet ``yamamoto\\stat\\stat.xtx`` (PSMT8 indices; canvas origin x=512, y=0
when the sheet is read as a 1024-wide 8-bit image). Bytes with the high
bit set go through a second, two-byte table (indices 175+); and a ``#``
followed by two hex digits selects a cell by its index directly (the code
runs ``sscanf("%X")`` on them).

So a new alphabet in battle is: draw its letters into the grid's unused
cells (110 of them: 63-174 minus the four punctuation marks; the retail
sheet keeps unreachable accented Latin there), give each a byte the
table does not use yet (the printable punctuation the table lacks, then
the control range — 56 bytes), write those bytes into the table at the
cells' indices, and write each letter in the battle strings as its byte.
The table is itself one of the overlay strings ``ovl-export`` lists
(``@a46af8`` on the USA disc), so ``encode`` emits the new table as a
line of the same tree and ``ovl-import`` writes everything. One byte per
letter matters: the HUD centres a label by its byte count, so the
``#XX`` escape (three bytes a letter; ``--escape``) draws right but
starts a third of the label too far left. The strings themselves are the
overlay strings (tech names, "End", "Menu", item and skill
descriptions); ``ovl-import --grow`` moves the longer ones.

    python cli.py statfont extract --iso GAME.iso --out tr/statfont      # the grid as PNG, to look at
    python cli.py statfont scan    --iso GAME.iso --text tr/ka --font NotoSansGeorgian.ttf \\
                                   --script Geor --language ka --out tr/statfont
    python cli.py statfont encode  --text tr/ka --map tr/statfont/statfont.json --out tr/ka-btl
    python cli.py ovl-import --iso OUT.iso --retail GAME.iso --text tr/ka-btl --out OUT.iso --grow
    python cli.py patch --iso OUT.iso --out OUT.iso --set 'chain0:yamamoto\\stat\\stat.xtx=tr/statfont/stat.xtx'

Glyphs are rendered from a TrueType font with the same HarfBuzz shaping as
``glyphs.py`` (one cell per shaped glyph; marks that attach to a base are
drawn with it), at ``--px`` (default 13) on a baseline ``--baseline`` rows
down (default 13), into the sheet's own 16-level coverage ramp (index 4 is
solid, 5-15 the retail anti-aliasing shades, 0 transparent). Scripts the
cells cannot hold legibly at 10 px (Devanagari conjunct stacks, long
Arabic ligatures) will look cramped here; the dialogue font is the place
for those, the battle bars are short labels.

Verified on PCSX2 (2026-10-15): Georgian (Mkhedruli) tech names drawn by
the battle bars from cells painted here, through both the byte table and
the ``#XX`` escape.
"""
from __future__ import annotations

import json
import re
import struct
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SHEET = "yamamoto\\stat\\stat.xtx"
GRID_X, GRID_Y = 512, 0          # PSMT8 canvas coordinates of cell 0
COLS, CELL_W, CELL_H = 25, 10, 18
ROWS = 10
TABLE = (" 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
         + " " * 84 + "." + " " * 4 + "'" + " " * 22 + "/" + " " * 19 + "-" + " " * 33)
FREE = [i for i in range(63, 175) if TABLE[i] == " "]
TABLE_VA = 0xA46AF8              # the byte table's address in OV01 (USA disc)
# bytes the table does not use, in the order new letters take them
POOL = [ord(c) for c in "!\"$%&()*+,:;<=>?@[\\]^_`{|}~"] + \
       [b for b in range(0x01, 0x20) if b not in (0x09, 0x0A, 0x0D)]
# coverage -> palette index; the retail glyphs use 4 for the solid core and
# 5..15 as edge shades (lighter with distance from the stroke)
RAMP = [(0.78, 4), (0.55, 8), (0.35, 11), (0.12, 13)]


# -- XTX in and out ---------------------------------------------------------

def xtx_subs(data: bytes):
    _t, count, hdr = struct.unpack_from("<III", data, 4)
    subs = []
    for i in range(count):
        b = hdr + 20 * i
        w, bufw, h = struct.unpack_from("<HHH", data, b)
        gs, _s, addr = struct.unpack_from("<III", data, b + 8)
        subs.append((w, bufw, h, gs, addr))
    return subs


def xtx_canvas(data: bytes):
    """CT32 canvas (clen x clen) with every sub-image placed, as browse.decode_xtx."""
    subs = xtx_subs(data)
    clen = {4: 256, 8: 512}[subs[0][1] or 8]
    canvas = bytearray(clen * clen * 4)
    for w, _, h, gs, addr in subs:
        px = data[addr + 32:addr + 32 + w * h * 4]
        y0 = (gs // (clen * 32)) * 32
        x0 = (gs % (clen * 32)) // 32
        for y in range(h):
            dst = ((y0 + y) * clen + x0) * 4
            canvas[dst:dst + w * 4] = px[y * w * 4:(y + 1) * w * 4]
    return canvas, clen, subs


def xtx_store(data: bytes, canvas: bytearray, subs) -> bytes:
    """Write the canvas back into every sub-image's bytes."""
    out = bytearray(data)
    clen = {4: 256, 8: 512}[subs[0][1] or 8]
    for w, _, h, gs, addr in subs:
        y0 = (gs // (clen * 32)) * 32
        x0 = (gs % (clen * 32)) // 32
        for y in range(h):
            src = ((y0 + y) * clen + x0) * 4
            out[addr + 32 + y * w * 4:addr + 32 + (y + 1) * w * 4] = canvas[src:src + w * 4]
    return bytes(out)


def _t8_offset(x: int, y: int, tw: int) -> int:
    block_row = (y & ~0xF) * tw
    swap = (((y + 2) >> 2) & 1) * 4
    col_row = ((((y & ~3) >> 1) + (y & 1)) & 7) * tw * 2
    return block_row + (x & ~0xF) * 2 + col_row + ((x + swap) & 7) * 4 + ((y >> 1) & 1) + ((x >> 2) & 2)


def get_index(canvas: bytearray, clen: int, x: int, y: int) -> int:
    return canvas[_t8_offset(x, y, clen * 2)]


def set_index(canvas: bytearray, clen: int, x: int, y: int, v: int) -> None:
    canvas[_t8_offset(x, y, clen * 2)] = v


def cell_origin(index: int) -> Tuple[int, int]:
    r, c = divmod(index, COLS)
    return GRID_X + c * CELL_W, GRID_Y + r * CELL_H


def read_cell(canvas, clen, index: int) -> List[List[int]]:
    x0, y0 = cell_origin(index)
    return [[get_index(canvas, clen, x0 + x, y0 + y) for x in range(CELL_W)] for y in range(CELL_H)]


def write_cell(canvas, clen, index: int, rows: List[List[int]]) -> None:
    x0, y0 = cell_origin(index)
    for y in range(CELL_H):
        for x in range(CELL_W):
            set_index(canvas, clen, x0 + x, y0 + y, rows[y][x])


# -- rendering ----------------------------------------------------------------

SHADE = {0: 0, 4: 255, 5: 230, 6: 215, 7: 200, 8: 180, 9: 160, 10: 140, 11: 110,
         12: 90, 13: 70, 14: 50, 15: 35}


def grid_png(canvas, clen, path, mark: Optional[Dict[int, str]] = None) -> None:
    """The whole grid as a grey PNG (cell index values mapped to brightness)."""
    from font import sheet_to_png
    W, H = COLS * CELL_W, ROWS * CELL_H
    g = bytearray(W * H)
    for y in range(H):
        for x in range(W):
            g[y * W + x] = SHADE.get(get_index(canvas, clen, GRID_X + x, GRID_Y + y), 128)
    Path(path).write_bytes(sheet_to_png(g, W, H))


class CellRenderer:
    def __init__(self, font, px: float = 13.0, baseline: int = 13, face_index: int = 0,
                 script: Optional[str] = None, language: Optional[str] = None,
                 rtl: bool = False, supersample: int = 4):
        from glyphs import TTFRenderer
        self.r = TTFRenderer(font, px=px, face_index=face_index, features=None,
                             supersample=supersample)
        self.px, self.baseline, self.ss = px, baseline, supersample
        self.script, self.language, self.rtl = script, language, rtl

    def shape(self, word: str):
        return self.r.shape(word, "rtl" if self.rtl else "ltr", self.script, self.language)

    def raster(self, parts) -> List[List[int]]:
        """parts: [(gid, dx_px, dy_px)] drawn into one 10x18 cell -> index rows."""
        ss, r = self.ss, self.r
        W, H = CELL_W * ss, CELL_H * ss
        edges = []
        for gid, dx, dy in parts:
            for contour in r.contours(gid):
                pts = [((1.0 + dx + x * r.scale) * ss, (self.baseline - dy - y * r.scale) * ss)
                       for x, y in contour]
                for i in range(len(pts)):
                    x0, y0 = pts[i]
                    x1, y1 = pts[(i + 1) % len(pts)]
                    if y0 != y1:
                        edges.append((x0, y0, x1, y1))
        cov = [[0] * W for _ in range(H)]
        for sy in range(H):
            yc = sy + 0.5
            xs = sorted(x0 + (yc - y0) * (x1 - x0) / (y1 - y0)
                        for x0, y0, x1, y1 in edges if (y0 <= yc < y1) or (y1 <= yc < y0))
            for i in range(0, len(xs) - 1, 2):
                a, b = int(xs[i] + 0.5), int(xs[i + 1] + 0.5)
                for sx in range(max(a, 0), min(b, W)):
                    cov[sy][sx] = 1
        n = ss * ss
        rows = []
        for y in range(CELL_H):
            row = []
            for x in range(CELL_W):
                s = sum(cov[y * ss + j][x * ss + i] for j in range(ss) for i in range(ss)) / n
                v = 0
                for thr, idx in RAMP:
                    if s >= thr:
                        v = idx
                        break
                row.append(v)
            rows.append(row)
        return rows


def _is_target(ch: str) -> bool:
    o = ord(ch)
    return o >= 0x80 and not (0x3000 <= o <= 0x30FF or 0x4E00 <= o <= 0x9FFF or 0xFF00 <= o <= 0xFFEF)


def _words(text: str) -> List[str]:
    return [w for w in re.split(r"(\s+)", text) if w and not w.isspace()]


def collect(tree: Path, cr: CellRenderer) -> Dict[str, Tuple[List[Tuple[int, float, float]], int]]:
    """key -> (parts, advance_px) for every shaped glyph cluster in the tree's
    target-script words. A key is the glyph id (plus attached marks)."""
    from ovltext import Overlay
    keys: Dict[str, Tuple[list, int]] = {}
    for p in sorted(tree.rglob("*.txt")):
        text = p.read_text(encoding="utf-8-sig")
        strings = []
        if text.startswith("# ovltext"):
            strings = list(Overlay.parse_text(text)[1].values())
        elif text.startswith("# evttext"):
            from evttext import _unescape
            strings = [_unescape(l.partition("\t")[2]) for l in text.split("\n") if l.startswith("@")]
        else:
            strings = [text]
        for s in strings:
            for w in _words(s):
                if not any(_is_target(c) for c in w):
                    continue
                run = "".join(c for c in w if _is_target(c))
                glyphs = cr.shape(run)
                cluster: list = []
                base = None
                for gid, cl, adv, xo, yo in glyphs:
                    if adv == 0 and cluster:                 # mark: attach to the base cell
                        cluster.append((gid, xo * cr.r.scale, yo * cr.r.scale))
                        continue
                    if cluster:
                        key = "g:" + ":".join(str(g) for g, _, _ in cluster)
                        keys.setdefault(key, (cluster, base))
                    cluster = [(gid, xo * cr.r.scale, yo * cr.r.scale)]
                    base = adv
                if cluster:
                    key = "g:" + ":".join(str(g) for g, _, _ in cluster)
                    keys.setdefault(key, (cluster, base))
    return keys


def cmd_extract(a) -> int:
    from repack import read_entry
    data = read_entry(a.iso, 0, SHEET) if a.iso else Path(a.xtx).read_bytes()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    canvas, clen, subs = xtx_canvas(data)
    (out / "stat.xtx").write_bytes(data)
    grid_png(canvas, clen, out / "grid.png")
    assert xtx_store(data, canvas, subs) == data
    print(f"{SHEET}: {len(data)} bytes, grid {COLS}x{ROWS} cells of {CELL_W}x{CELL_H} at "
          f"({GRID_X},{GRID_Y}); {len(FREE)} free cells -> {out / 'grid.png'}")
    return 0


def cmd_scan(a) -> int:
    from repack import read_entry
    data = read_entry(a.iso, 0, SHEET) if a.iso else Path(a.xtx).read_bytes()
    canvas, clen, subs = xtx_canvas(data)
    cr = CellRenderer(a.font, px=a.px, baseline=a.baseline, face_index=a.face,
                      script=a.script, language=a.language, rtl=a.rtl)
    keys = collect(Path(a.text), cr)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    mapping: Dict[str, int] = {}
    codes: Dict[str, int] = {}
    if a.extend:
        prev = json.loads(Path(a.extend).read_text(encoding="utf-8"))
        mapping, codes = prev["cells"], prev.get("codes", {})
    used = set(mapping.values())
    free = [i for i in FREE if i not in used]
    pool = [b for b in POOL if b not in set(codes.values())]
    wide = 0
    for key, (parts, adv) in keys.items():
        if key in mapping:
            continue
        if not free:
            print(f"error: the grid's {len(FREE)} free cells are used up at {key}", file=sys.stderr)
            return 1
        if not pool:
            print(f"error: the {len(POOL)} spare bytes of the lookup table are used up at {key}; "
                  "encode with --escape for the rest", file=sys.stderr)
            return 1
        idx = free.pop(0)
        mapping[key] = idx
        codes[key] = pool.pop(0)
        rows = cr.raster(parts)
        if adv and adv * cr.r.scale > CELL_W:
            wide += 1
        write_cell(canvas, clen, idx, rows)
    new = xtx_store(data, canvas, subs)
    (out / "stat.xtx").write_bytes(new)
    grid_png(canvas, clen, out / "grid.png")
    cfg = {"font": str(Path(a.font).resolve()), "face": a.face, "px": a.px, "baseline": a.baseline,
           "script": a.script, "language": a.language, "rtl": a.rtl}
    (out / "statfont.json").write_text(json.dumps({"config": cfg, "cells": mapping, "codes": codes},
                                                  indent=1, ensure_ascii=False), encoding="utf-8")
    print(f"{len(keys)} glyphs -> cells; {len(free)} free cells left"
          f"{f'; {wide} glyphs wider than the 10 px cell (clipped)' if wide else ''}")
    print(f"wrote {out / 'stat.xtx'}, statfont.json, grid.png")
    return 0


def _byte_text(b: int) -> str:
    return chr(b) if 0x20 <= b < 0x7F else f"⟦{b:02X}⟧"


def table_line(mapping: Dict[str, int], codes: Dict[str, int]) -> str:
    """The 230-byte lookup table with the new letters' bytes at their cells."""
    tbl = list(TABLE)
    for key, idx in mapping.items():
        if key in codes:
            tbl[idx] = chr(codes[key])
    return "".join(_byte_text(ord(c)) for c in tbl)


class Encoder:
    def __init__(self, mapping: Dict[str, int], cr: CellRenderer, codes: Optional[Dict[str, int]] = None,
                 escape: bool = False):
        self.m, self.cr = mapping, cr
        self.codes = codes or {}
        self.escape = escape
        self.missing: List[str] = []

    def word(self, w: str) -> str:
        out = []
        i = 0
        while i < len(w):
            if not _is_target(w[i]):
                out.append(w[i])
                i += 1
                continue
            j = i
            while j < len(w) and _is_target(w[j]):
                j += 1
            run = w[i:j]
            glyphs = self.cr.shape(run)
            cluster: list = []
            codes = []
            def flush():
                if cluster:
                    key = "g:" + ":".join(str(g) for g in cluster)
                    idx = self.m.get(key)
                    if idx is None:
                        self.missing.append(run)
                        codes.append("?")
                    elif not self.escape and key in self.codes:
                        codes.append(_byte_text(self.codes[key]))
                    else:
                        codes.append(f"#{idx:02X}")
            for gid, cl, adv, xo, yo in glyphs:
                if adv == 0 and cluster:
                    cluster.append(gid)
                    continue
                flush()
                cluster = [gid]
            flush()
            if self.cr.rtl:
                codes.reverse()
            out.append("".join(codes))
            i = j
        return "".join(out)

    def string(self, s: str) -> str:
        return "".join(self.word(p) if not p.isspace() else p for p in re.split(r"(\s+)", s))


def cmd_encode(a) -> int:
    spec = json.loads(Path(a.map).read_text(encoding="utf-8"))
    cfg = spec["config"]
    cr = CellRenderer(cfg["font"], px=cfg["px"], baseline=cfg["baseline"], face_index=cfg["face"],
                      script=cfg["script"], language=cfg["language"], rtl=cfg["rtl"])
    enc = Encoder(spec["cells"], cr, spec.get("codes"), escape=a.escape)
    from langpack import _iter_tree, _transform_file
    src, dst = Path(a.text), Path(a.out)
    n = 0
    tbl = None if a.escape else table_line(spec["cells"], spec.get("codes", {}))
    for p in _iter_tree(src):
        rel = p.relative_to(src)
        o = dst / rel
        o.parent.mkdir(parents=True, exist_ok=True)
        text = _transform_file(p, enc.string)
        if tbl is not None and text.startswith("# ovltext") and "file=OV01.OVL" in text.split("\n", 1)[0]:
            key = f"@{TABLE_VA:x}\t"
            lines = [l for l in text.split("\n") if not l.startswith(key)]
            while lines and lines[-1] == "":
                lines.pop()
            lines.append(key + tbl)
            text = "\n".join(lines) + "\n"
        with open(o, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        n += 1
    print(f"encoded {n} files -> {dst}" + ("" if tbl is None else " (lookup table line included)"))
    if enc.missing:
        print(f"warning: no cell for {sorted(set(enc.missing))[:8]} (re-run scan)", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("extract", "scan"):
        p = sub.add_parser(name)
        p.add_argument("--iso", help="retail ISO (source of the status sheet)")
        p.add_argument("--xtx", help="a stat.xtx instead of --iso (e.g. an earlier scan's)")
        p.add_argument("--out", required=True)
        if name == "scan":
            p.add_argument("--text", required=True, help="translated tree (ovl-export files, or any .txt)")
            p.add_argument("--font", required=True, help="TrueType/OpenType font")
            p.add_argument("--face", type=int, default=0)
            p.add_argument("--px", type=float, default=13.0)
            p.add_argument("--baseline", type=int, default=13)
            p.add_argument("--script")
            p.add_argument("--language")
            p.add_argument("--rtl", action="store_true")
            p.add_argument("--extend", help="statfont.json of an earlier scan to add to")
    p = sub.add_parser("encode")
    p.add_argument("--text", required=True)
    p.add_argument("--map", required=True, help="statfont.json from scan")
    p.add_argument("--out", required=True)
    p.add_argument("--escape", action="store_true",
                   help="write letters as #XX cell escapes instead of table bytes "
                        "(no table change, but centred labels shift left)")
    a = ap.parse_args(argv)
    if a.cmd in ("extract", "scan") and not (a.iso or a.xtx):
        ap.error("--iso or --xtx is required")
    return {"extract": cmd_extract, "scan": cmd_scan, "encode": cmd_encode}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
