"""langpack.py — translate into scripts the disc never had (Arabic, Hebrew,
Devanagari, …): shape, wrap, order, and encode text for the game's font.

The engine draws left-to-right, one cell per two-byte code, with no idea of
joining, reordering or bidi. So all of that happens here, at build time,
and the game only ever sees a stream of glyph codes — the approach every
console fan translation of a right-to-left language has used. Pipeline:

    text-export / evt-export       -> logical UTF-8 tree (translate it)
    langpack scan   TREE           -> glyphset.json + font0.tex + font1.tex
    langpack encode TREE -> TREE2  -> same tree, target script as ⟦XX⟧ codes
    text-import / evt-import TREE2 -> patched ISO (plus the two fonts)

``scan`` shapes every string with HarfBuzz (via ``glyphs.TTFRenderer``),
collects the glyphs the translation actually uses, gives each a slot in the
font (proportional slots first, kanji cells for the overflow — see
``glyphs.py``) and rasterises them from a TrueType font into both faces.
``encode`` shapes each word again, measures it with the new metrics,
wraps to a pixel width, puts the words of each line in visual order (the
Unicode bidi rule for runs: right-to-left paragraphs reversed, embedded
Latin/digit runs kept forward, brackets mirrored), optionally right-aligns,
and writes the glyph codes as the ``⟦XX⟧`` byte markers the importers
already understand. Control codes (``⟦15⟧⟦02⟧…``) ride along in logical
order and are emitted ahead of the word they precede.

Without ``uharfbuzz``/``fonttools`` a simpler path still works: letters
become ``u:<char>`` keys (Arabic through the built-in presentation-form
table), and the cells come from a hand-drawn PNG sheet (``--cells`` +
``--keys``) instead of a TrueType font.

Verify without the game: ``langpack preview`` renders an encoded line with
the rebuilt font cells to a PNG.

Usage:
    python langpack.py scan   --iso GAME.iso --text AR/ --font Amiri.ttf --script Arab --rtl --out ARFONT/
    python langpack.py encode --text AR/ --glyphset ARFONT/glyphset.json --out AR-enc/ --width 400 --align right
    python langpack.py preview --glyphset ARFONT/glyphset.json --font0 ARFONT/font0.tex --text "…" --out line.png
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import font as fontmod
from font import Font, ascii_to_code, glyph_cell, prop_index
from glyphs import Glyph, GlyphSet, GlyphError, from_png
from textpack import _ESC_RE

MARK = re.compile(r"(?:⟦[0-9A-Fa-f]{2}⟧)+")
MIRROR = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<"}
DEFAULT_SPACE_PX = 8          # engine default advance for the blank slot


# --- Arabic presentation forms (fallback shaper, no dependencies) ----------------

# letter -> (isolated, final, initial, medial); two entries = right-joining only
_ARABIC = {
    0x0621: (0xFE80,), 0x0622: (0xFE81, 0xFE82), 0x0623: (0xFE83, 0xFE84),
    0x0624: (0xFE85, 0xFE86), 0x0625: (0xFE87, 0xFE88),
    0x0626: (0xFE89, 0xFE8A, 0xFE8B, 0xFE8C), 0x0627: (0xFE8D, 0xFE8E),
    0x0628: (0xFE8F, 0xFE90, 0xFE91, 0xFE92), 0x0629: (0xFE93, 0xFE94),
    0x062A: (0xFE95, 0xFE96, 0xFE97, 0xFE98), 0x062B: (0xFE99, 0xFE9A, 0xFE9B, 0xFE9C),
    0x062C: (0xFE9D, 0xFE9E, 0xFE9F, 0xFEA0), 0x062D: (0xFEA1, 0xFEA2, 0xFEA3, 0xFEA4),
    0x062E: (0xFEA5, 0xFEA6, 0xFEA7, 0xFEA8), 0x062F: (0xFEA9, 0xFEAA),
    0x0630: (0xFEAB, 0xFEAC), 0x0631: (0xFEAD, 0xFEAE), 0x0632: (0xFEAF, 0xFEB0),
    0x0633: (0xFEB1, 0xFEB2, 0xFEB3, 0xFEB4), 0x0634: (0xFEB5, 0xFEB6, 0xFEB7, 0xFEB8),
    0x0635: (0xFEB9, 0xFEBA, 0xFEBB, 0xFEBC), 0x0636: (0xFEBD, 0xFEBE, 0xFEBF, 0xFEC0),
    0x0637: (0xFEC1, 0xFEC2, 0xFEC3, 0xFEC4), 0x0638: (0xFEC5, 0xFEC6, 0xFEC7, 0xFEC8),
    0x0639: (0xFEC9, 0xFECA, 0xFECB, 0xFECC), 0x063A: (0xFECD, 0xFECE, 0xFECF, 0xFED0),
    0x0640: (0x0640, 0x0640, 0x0640, 0x0640),
    0x0641: (0xFED1, 0xFED2, 0xFED3, 0xFED4), 0x0642: (0xFED5, 0xFED6, 0xFED7, 0xFED8),
    0x0643: (0xFED9, 0xFEDA, 0xFEDB, 0xFEDC), 0x0644: (0xFEDD, 0xFEDE, 0xFEDF, 0xFEE0),
    0x0645: (0xFEE1, 0xFEE2, 0xFEE3, 0xFEE4), 0x0646: (0xFEE5, 0xFEE6, 0xFEE7, 0xFEE8),
    0x0647: (0xFEE9, 0xFEEA, 0xFEEB, 0xFEEC), 0x0648: (0xFEED, 0xFEEE),
    0x0649: (0xFEEF, 0xFEF0), 0x064A: (0xFEF1, 0xFEF2, 0xFEF3, 0xFEF4),
}
_LAM_ALEF = {0x0622: (0xFEF5, 0xFEF6), 0x0623: (0xFEF7, 0xFEF8),
             0x0625: (0xFEF9, 0xFEFA), 0x0627: (0xFEFB, 0xFEFC)}


def arabic_forms(word: str) -> List[str]:
    """Contextual presentation forms for one word, logical order; marks
    (harakat) are dropped. Lam-alef becomes one ligature form."""
    letters = [c for c in word if not (0x064B <= ord(c) <= 0x0652 or ord(c) == 0x0670)]
    out: List[str] = []
    i = 0
    prev_joins = False            # does the previous emitted letter join forward?
    while i < len(letters):
        c = letters[i]
        o = ord(c)
        forms = _ARABIC.get(o)
        if forms is None:
            out.append(c)
            prev_joins = False
            i += 1
            continue
        nxt = ord(letters[i + 1]) if i + 1 < len(letters) else None
        if o == 0x0644 and nxt in _LAM_ALEF:
            iso, fin = _LAM_ALEF[nxt]
            out.append(chr(fin if prev_joins else iso))
            prev_joins = False
            i += 2
            continue
        dual = len(forms) == 4
        next_joins = nxt in _ARABIC
        if prev_joins and dual and next_joins:
            f = forms[3]
        elif prev_joins:
            f = forms[1] if len(forms) > 1 else forms[0]
        elif dual and next_joins:
            f = forms[2]
        else:
            f = forms[0]
        out.append(chr(f))
        prev_joins = dual and next_joins
        i += 1
    return out


# --- shaping back-ends -----------------------------------------------------------

class SimpleShaper:
    """No HarfBuzz: one key per character (presentation forms for Arabic)."""
    name = "simple"

    def __init__(self, rtl: bool):
        self.rtl = rtl

    def keys(self, word: str, rtl: bool) -> List[str]:
        if any(0x0600 <= ord(c) <= 0x06FF for c in word):
            seq = [f"u:{f}" for f in arabic_forms(word)]
        else:
            seq = [f"u:{c}" for c in word if unicodedata.category(c) != "Mn"]
        return seq[::-1] if rtl else seq      # visual order, like HarfBuzz

    def glyph(self, key: str):
        return None


class HBShaper:
    """HarfBuzz shaping through glyphs.TTFRenderer; keys name glyph ids."""
    name = "harfbuzz"

    def __init__(self, renderer, script: Optional[str], language: Optional[str],
                 max_cluster_px: int = 18):
        self.r = renderer
        self.script = script
        self.language = language
        self.max_cluster = max_cluster_px
        self._parts: Dict[str, list] = {}
        self.notdef: set = set()        # words the font had no glyph for

    def keys(self, word: str, rtl: bool) -> List[str]:
        r = self.r
        glyphs = r.shape(word, "rtl" if rtl else "ltr", self.script, self.language)
        if any(g[0] == 0 for g in glyphs):
            self.notdef.add(word)
        # group consecutive glyphs by cluster
        groups: List[list] = []
        for g in glyphs:
            if groups and groups[-1][0][1] == g[1]:
                groups[-1].append(g)
            else:
                groups.append([g])
        out = []
        for grp in groups:
            if len(grp) == 1:
                out.append(f"g:{r.name}:{grp[0][0]}")
                continue
            pen = 0.0
            parts = []
            for gid, _, adv, xo, yo in grp:
                parts.append((gid, (pen + xo) * r.scale, yo * r.scale))
                pen += adv
            if pen * r.scale <= self.max_cluster:
                key = "c:%s:%s" % (r.name, ";".join(
                    f"{gid}@{dx:.1f},{dy:.1f}" for gid, dx, dy in parts))
                self._parts[key] = parts
                out.append(key)
            else:
                out += [f"g:{r.name}:{gid}" for gid, *_ in grp]
        return out

    def glyph(self, key: str) -> Glyph:
        kind, _, rest = key.partition(":")
        if kind == "g":
            return self.r.render_gid(int(rest.split(":")[1]))
        if kind == "c":
            parts = self._parts.get(key)
            if parts is None:
                parts = []
                for p in rest.split(":", 1)[1].split(";"):
                    gid, _, off = p.partition("@")
                    dx, dy = off.split(",")
                    parts.append((int(gid), float(dx), float(dy)))
            return self.r.render_cluster(parts, key)
        return self.r.render_text(rest, key)


# --- the encoder ---------------------------------------------------------------

def _char_class(c: str, keep_latin: bool) -> str:
    o = ord(c)
    if o < 0x80:
        if not keep_latin:
            return "x"
        return "a" if c.isalnum() else "p"
    return "n" if _is_native(c) else "s"


def _runs(tok: str, keep_latin: bool) -> List[str]:
    out, cur, cls = [], "", None
    for c in tok:
        k = _char_class(c, keep_latin)
        if k != cls and cur:
            out.append(cur)
            cur = ""
        cur += c
        cls = k
    if cur:
        out.append(cur)
    return out


def _direction(word: str) -> str:
    """R (right-to-left strong), L (left-to-right strong), E (numbers), N."""
    classes = {unicodedata.bidirectional(c) for c in word}
    if classes & {"R", "AL"}:
        return "R"
    if "L" in classes:
        return "L"
    if classes & {"EN", "AN"}:
        return "E"
    return "N"


def _is_ascii(s: str) -> bool:
    return all(0x20 <= ord(c) < 0x7F for c in s)


def _is_native(s: str) -> bool:
    """The disc's own Japanese (kana, kanji, full-width forms): left alone,
    the importers re-encode it as EUC-JP. Debug prints in the scripts are
    full of it and must not be shaped as the target script."""
    return any(0x3000 <= ord(c) <= 0x30FF or 0x3400 <= ord(c) <= 0x9FFF
               or 0xFF00 <= ord(c) <= 0xFFEF or 0x2500 <= ord(c) <= 0x25FF
               for c in s)


class Encoder:
    def __init__(self, glyphset: GlyphSet, shaper, font0: Font, rtl: bool,
                 keep_latin: bool = True, width: int = 0, align: str = "left",
                 space_px: int = DEFAULT_SPACE_PX, collect: bool = False):
        self.gs = glyphset
        self.shaper = shaper
        self.font = font0
        self.rtl = rtl
        self.keep_latin = keep_latin
        self.width = width
        self.align = align
        self.space_px = space_px
        self.collect = collect          # scan mode: gather keys, emit nothing
        self.counts: Counter = Counter()
        self.missing: Counter = Counter()

    # measuring
    def _ascii_width(self, s: str) -> int:
        return self.font.width_of_bytes(s.encode("ascii"))

    def _key_width(self, key: str) -> int:
        if key in self.gs.slots:
            return self.gs.width(key)
        return 20

    # one paragraph (no newlines) -> list of encoded lines
    def paragraph(self, text: str) -> List[str]:
        items = []          # (kind, payload, dir, width, ctrl_prefix)
        pending_ctrl = ""
        pos = 0
        # control codes come as ⟦XX⟧ markers or as raw C0 characters (a
        # mail's "\r\x02" subject marker); both pass through untouched
        for m in re.finditer(r"(?:⟦[0-9A-Fa-f]{2}⟧|[\x00-\x08\x0b-\x1f])+|[ \u3000]+|[^ \u3000⟦\x00-\x1f]+", text):
            tok = m.group(0)
            if tok.startswith("⟦") or ord(tok[0]) < 0x20:
                pending_ctrl += tok
                continue
            if tok[0] in " \u3000":
                items.append(("space", tok, "N", self.space_px * len(tok), pending_ctrl))
            else:
                # a token is split into runs: ASCII letters/digits, ASCII
                # punctuation, the disc's own Japanese symbols, and target
                # script — so a Hebrew prefix glued to a Latin name, brackets
                # around a number, or a ■ bullet before a word each get the
                # right treatment
                for part in _runs(tok, self.keep_latin):
                    items.append(self._word(part, pending_ctrl))
                    pending_ctrl = ""
            pending_ctrl = ""
        if pending_ctrl:
            items.append(("ctrl", "", "N", 0, pending_ctrl))
        if self.collect:
            return []
        # a paragraph with no right-to-left letters (an untranslated English
        # line, a bare number) stays left-to-right even in an RTL project
        has_r = any(it[2] == "R" for it in items)
        base_rtl = has_r if self.rtl is None or self.rtl else False
        lines = self._wrap(items) if self.width else [items]
        return [self._emit(line, base_rtl) for line in lines]

    def _word(self, tok: str, ctrl: str):
        d = _direction(tok)
        if _is_ascii(tok) and self.keep_latin:
            return ("ascii", tok, d, self._ascii_width(tok), ctrl)
        if _is_native(tok):
            return ("ascii", tok, "L", 20 * len(tok), ctrl)
        keys = self.shaper.keys(tok, d == "R")
        if self.collect:
            self.counts.update(keys)
            return ("keys", keys, d, 0, ctrl)
        w = 0
        for k in keys:
            if k not in self.gs.slots:
                self.missing[k] += 1
            w += self._key_width(k)
        return ("keys", keys, d, w, ctrl)

    def _wrap(self, items):
        """Greedy fill; breaks only at spaces, so a word and the punctuation
        or control codes glued to it always travel together."""
        groups, grp = [], []           # runs of non-space items
        for it in items:
            if it[0] == "space":
                if grp:
                    groups.append(grp)
                groups.append([it])
                grp = []
            else:
                grp.append(it)
        if grp:
            groups.append(grp)
        lines, cur, cur_w = [], [], 0
        for g in groups:
            w = sum(it[3] for it in g)
            if g[0][0] == "space":
                if cur:
                    cur += g
                    cur_w += w
                continue
            if cur and cur_w + w > self.width:
                while cur and cur[-1][0] == "space":
                    cur.pop()
                lines.append(cur)
                cur, cur_w = [], 0
            cur += g
            cur_w += w
        while cur and cur[-1][0] == "space":
            cur.pop()
        if cur or not lines:
            lines.append(cur)
        return lines

    def _emit(self, items, base_rtl: bool) -> str:
        # resolve embedding levels: paragraph level 1 if RTL else 0
        base = 1 if base_rtl else 0
        levels = []
        for kind, _, d, _, _ in items:
            if d == "R":
                levels.append(1)
            elif d in ("L", "E"):
                levels.append(2 if base_rtl else 0)
            else:
                levels.append(None)
        # neutrals take the level of equal neighbours, else the base level
        for i, lv in enumerate(levels):
            if lv is None:
                prev = next((levels[j] for j in range(i - 1, -1, -1) if levels[j] is not None), base)
                nxt = next((levels[j] for j in range(i + 1, len(levels)) if levels[j] is not None), base)
                levels[i] = prev if prev == nxt else base
        order = list(range(len(items)))
        # UBA L2: reverse runs at each level from the highest down to 1
        for lv in (2, 1):
            i = 0
            while i < len(order):
                if levels[order[i]] >= lv:
                    j = i
                    while j < len(order) and levels[order[j]] >= lv:
                        j += 1
                    order[i:j] = order[i:j][::-1]
                    i = j
                else:
                    i += 1
        out = []
        line_w = 0
        lead = items[0][4] if items else ""   # control codes before the first word
        if lead:
            out.append(lead)
        for idx in order:
            kind, payload, d, w, ctrl = items[idx]
            if idx != 0:
                out.append(ctrl)
            line_w += w
            if kind == "space":
                out.append(payload)
            elif kind == "ascii":
                s = payload
                if levels[idx] == 1 and base_rtl:        # neutral ASCII inside RTL
                    s = "".join(MIRROR.get(c, c) for c in s)
                    if d == "N":
                        s = s[::-1]
                out.append(s)
            else:
                out.append("".join(self._code_text(k) for k in payload))
        text = "".join(out)
        if self.width and self.align == "right" and base_rtl and line_w < self.width:
            text = " " * ((self.width - line_w) // self.space_px) + text
        return text

    def _code_text(self, key: str) -> str:
        if key not in self.gs.slots:
            return "⟦A1⟧⟦A2⟧"          # visible tofu: JIS row-1 cell 2 (、)
        b = self.gs.code_bytes(key)
        return f"⟦{b[0]:02X}⟧⟦{b[1]:02X}⟧"

    def _para(self, p: str) -> str:
        if not p.strip():
            return p
        # leading/trailing spaces are the scripts' manual centering: keep them
        # where they are instead of letting the bidi pass flip them
        head = p[:len(p) - len(p.lstrip(" "))]
        tail = p[len(p.rstrip(" ")):]
        body = p.strip(" ")
        return head + "\n".join(self.paragraph(body) or [""]) + tail

    def string(self, text: str) -> str:
        """Whole logical string (may contain newlines) -> encoded string."""
        return "\n".join(self._para(p) for p in text.split("\n"))


# --- trees ---------------------------------------------------------------------

def _iter_tree(tree: Path):
    for p in sorted(tree.rglob("*")):
        if p.is_file() and p.suffix == ".txt":
            yield p


def _transform_file(path: Path, fn) -> str:
    with open(path, encoding="utf-8-sig", newline="") as fh:   # keep \r as a control byte
        text = fh.read()
    if text.startswith("# evttext"):
        from evttext import _escape, _unescape
        out = []
        for line in text.split("\n"):
            if line.startswith("@") and "\t" in line:
                key, _, body = line.partition("\t")
                out.append(key + "\t" + _escape(fn(_unescape(body))))
            else:
                out.append(line)
        return "\n".join(out)
    return fn(text)


def _load_disc_fonts(iso: Optional[str], font0: Optional[str], font1: Optional[str]):
    if iso:
        from repack import read_entry
        return (Font.from_bytes(read_entry(iso, 0, "font0.tex")),
                Font.from_bytes(read_entry(iso, 0, "font1.tex")))
    if not font0:
        raise GlyphError("give --iso, or --font0 (and --font1)")
    f0 = Font.load(font0)
    return f0, (Font.load(font1) if font1 else Font.from_bytes(f0.to_bytes()))


def _make_shaper(cfg: dict):
    if cfg.get("ttf"):
        from glyphs import TTFRenderer
        r = TTFRenderer(cfg["ttf"], cfg.get("px", 20.0), cfg.get("baseline", 17),
                        cfg.get("face", 0))
        return HBShaper(r, cfg.get("script"), cfg.get("language"))
    return SimpleShaper(bool(cfg.get("rtl")))


def cmd_scan(a) -> int:
    f0, f1 = _load_disc_fonts(a.iso, a.font0, a.font1)
    cfg = {"ttf": a.font, "px": a.px, "baseline": a.baseline, "face": a.face,
           "script": a.script, "language": a.language, "rtl": a.rtl,
           "keep_latin": not a.reclaim_latin}
    shaper = _make_shaper(cfg)
    gs = GlyphSet(a.font or a.cells or "")
    if a.extend:
        # a second script into the same fonts: keep every slot already
        # given out (pass the first run's font0/font1 as --font0/--font1)
        gs = GlyphSet.load(a.extend)
        gs.source += f" + {a.font or a.cells}"
    if a.cells:
        keys = Path(a.keys).read_text(encoding="utf-8").split("\n")
        keys = [k if k.startswith(("u:", "g:", "c:")) else f"u:{k}" for k in keys if k]
        drawn = from_png(a.cells, keys, a.cells)
        for g in drawn.glyphs.values():
            gs.add(g)
    enc = Encoder(gs, shaper, f0, a.rtl, keep_latin=not a.reclaim_latin, collect=True)
    tree = Path(a.text)
    n = 0
    for p in _iter_tree(tree):
        _transform_file(p, enc.string)
        n += 1
    order = [k for k, _ in enc.counts.most_common()]
    missing = [k for k in order if k not in gs.glyphs]
    if missing and not a.cells:
        for k in missing:
            gs.add(shaper.glyph(k))
    elif missing:
        raise GlyphError(f"{len(missing)} keys have no cell in {a.cells}: {missing[:8]}")
    gs.allocate(order, keep_latin=not a.reclaim_latin, prop_only=a.prop_only,
                wide_only=a.wide_only)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for f, name in ((f0, "font0.tex"), (f1, "font1.tex")):
        gs.apply(f)
        (out / name).write_bytes(f.to_bytes())
        (out / f"{name[:-4]}_preview.png").write_bytes(fontmod.sheet_to_png(f.sheets[3]))
    gs.save(out / "glyphset.json")
    (out / "langpack.json").write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    if getattr(shaper, "notdef", None):
        print(f"warning: {len(shaper.notdef)} word(s) use characters this font "
              f"lacks (drawn as the font's .notdef): {sorted(shaper.notdef)[:6]}")
    tiers = Counter(v["tier"] for v in gs.slots.values())
    print(f"scanned {n} files: {sum(enc.counts.values())} glyph uses, "
          f"{len(order)} distinct glyphs -> {tiers.get('prop', 0)} proportional slots, "
          f"{tiers.get('wide', 0)} wide cells")
    print(f"wrote {out / 'glyphset.json'}, font0.tex, font1.tex (+ sheet previews)")
    print("patch the fonts with: cli.py patch --set chain0:font0.tex=... --set chain0:font1.tex=...")
    return 0


def cmd_encode(a) -> int:
    gs = GlyphSet.load(a.glyphset)
    cfgp = Path(a.glyphset).with_name("langpack.json")
    cfg = json.loads(cfgp.read_text(encoding="utf-8")) if cfgp.exists() else {}
    f0 = Font.load(a.font0) if a.font0 else Font.load(Path(a.glyphset).with_name("font0.tex"))
    shaper = _make_shaper(cfg)
    rtl = cfg.get("rtl") if a.rtl is None else a.rtl
    enc = Encoder(gs, shaper, f0, rtl, keep_latin=cfg.get("keep_latin", True),
                  width=a.width, align=a.align, space_px=a.space_px)
    src, dst = Path(a.text), Path(a.out)
    n = 0
    for p in _iter_tree(src):
        rel = p.relative_to(src)
        out = dst / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="") as fh:
            fh.write(_transform_file(p, enc.string))
        n += 1
    for p in src.rglob("*"):
        if p.is_file() and p.suffix != ".txt":
            d = dst / p.relative_to(src)
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_bytes(p.read_bytes())
    print(f"encoded {n} files -> {dst}")
    if enc.missing:
        print(f"warning: {sum(enc.missing.values())} glyph uses had no slot "
              f"(re-run scan): {list(enc.missing)[:6]}")
        return 1
    return 0


def render_line(font: Font, data: bytes, scale: int = 2) -> bytes:
    """PNG of one text line drawn from the font's cells the way the printer
    would (proportional advance = right - left + 3, cells at their ink)."""
    pen, cells = 0, []
    i = 0
    while i < len(data):
        b = data[i]
        if b < 0x20:
            i += fontmod.control_length(data, i)
            continue
        if b < 0x80:
            code = ascii_to_code(b)
            i += 1
        else:
            code = (b << 8) | data[i + 1]
            i += 2
        p = prop_index(code)
        cell = glyph_cell(code)
        if 0 <= p < len(font.metrics) and font.metrics[p][0] != 0:
            left, right = font.metrics[p]
            cells.append((cell, pen + 1 - left))
            pen += right - left + 3
        elif p >= 0:          # blank/space slot
            pen += DEFAULT_SPACE_PX
        else:
            cells.append((cell, pen))
            pen += 20
    W = max(pen + 2, 1)
    img = [[0] * W for _ in range(fontmod.CELL_H)]
    for cell, x0 in cells:
        bm = font.cell(cell)
        for y in range(fontmod.CELL_H):
            for x in range(fontmod.CELL_W):
                if bm[y][x] and 0 <= x0 + x < W:
                    img[y][x0 + x] = max(img[y][x0 + x], bm[y][x])
    flat = bytearray()
    for row in img:
        for _ in range(scale):
            for v in row:
                flat += bytes([v]) * scale
    return fontmod.sheet_to_png(flat, W * scale, fontmod.CELL_H * scale)


def cmd_preview(a) -> int:
    gs = GlyphSet.load(a.glyphset)
    cfgp = Path(a.glyphset).with_name("langpack.json")
    cfg = json.loads(cfgp.read_text(encoding="utf-8")) if cfgp.exists() else {}
    f0 = Font.load(a.font0 or Path(a.glyphset).with_name("font0.tex"))
    enc = Encoder(gs, _make_shaper(cfg), f0, cfg.get("rtl") if a.rtl is None else a.rtl,
                  keep_latin=cfg.get("keep_latin", True), width=a.width, align=a.align)
    from textpack import encode_text
    encoded = enc.string(a.text)
    print(encoded)
    lines = [encode_text(l) for l in encoded.split("\n")]
    pngs = [render_line(f0, l, a.scale) for l in lines]
    if len(pngs) == 1:
        Path(a.out).write_bytes(pngs[0])
    else:
        for i, p in enumerate(pngs):
            Path(a.out).with_name(f"{Path(a.out).stem}_{i}{Path(a.out).suffix}").write_bytes(p)
    print(f"preview -> {a.out} ({len(lines)} line(s))")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("scan")
    ps.add_argument("--iso", help="retail ISO (source of the two fonts)")
    ps.add_argument("--font0"); ps.add_argument("--font1")
    ps.add_argument("--text", required=True, help="translated tree (logical order)")
    ps.add_argument("--font", help="TrueType/OpenType font to rasterise from")
    ps.add_argument("--face", type=int, default=0, help="face index inside a .ttc")
    ps.add_argument("--px", type=float, default=20.0, help="em size in pixels")
    ps.add_argument("--baseline", type=int, default=17, help="baseline row in the 24 px cell")
    ps.add_argument("--script", help="HarfBuzz script tag: Arab, Hebr, Deva, Cyrl, …")
    ps.add_argument("--language", help="BCP-47 language for shaping (ar, he, hi, …)")
    ps.add_argument("--rtl", action="store_true", help="right-to-left paragraphs")
    ps.add_argument("--cells", help="PNG sheet of hand-drawn cells instead of --font")
    ps.add_argument("--keys", help="text file, one key per line, matching --cells order")
    ps.add_argument("--reclaim-latin", action="store_true",
                    help="free the A-Z/a-z slots too (translation uses no Latin)")
    ps.add_argument("--extend", help="glyphset.json from an earlier scan (another "
                    "script/font) to add to instead of starting fresh")
    ps.add_argument("--prop-only", action="store_true")
    ps.add_argument("--wide-only", action="store_true")
    ps.add_argument("--out", required=True)
    pe = sub.add_parser("encode")
    pe.add_argument("--text", required=True)
    pe.add_argument("--glyphset", required=True)
    pe.add_argument("--font0")
    pe.add_argument("--out", required=True)
    pe.add_argument("--width", type=int, default=0, help="wrap width in pixels (0 = none)")
    pe.add_argument("--align", choices=("left", "right"), default="left")
    pe.add_argument("--space-px", type=int, default=DEFAULT_SPACE_PX)
    pe.add_argument("--rtl", dest="rtl", action="store_true", default=None)
    pe.add_argument("--ltr", dest="rtl", action="store_false")
    pp = sub.add_parser("preview")
    pp.add_argument("--glyphset", required=True)
    pp.add_argument("--font0")
    pp.add_argument("--text", required=True)
    pp.add_argument("--out", required=True)
    pp.add_argument("--width", type=int, default=0)
    pp.add_argument("--align", choices=("left", "right"), default="left")
    pp.add_argument("--scale", type=int, default=2)
    pp.add_argument("--rtl", dest="rtl", action="store_true", default=None)
    pp.add_argument("--ltr", dest="rtl", action="store_false")
    a = ap.parse_args(argv)
    try:
        return {"scan": cmd_scan, "encode": cmd_encode, "preview": cmd_preview}[a.cmd](a)
    except GlyphError as ex:
        print(f"error: {ex}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
