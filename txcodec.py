"""txcodec.py — what a translator sees and types, and the bytes the game draws.

The translation editor shows every string as Unicode ("display form") and
turns the translator's Unicode back into the exact bytes the engine's
printers expect. This module is that codec, plus the checker the editor
runs on every keystroke and the one font repair a French build needs.

Why a codec at all: the kit's exporters decode with Python's ``euc_jp``,
which is right for ASCII and the JIS X 0208 symbols but wrong for every
accented letter. Python spells ``é`` as the three-byte JIS X 0212 sequence
``8F AB B1``; the engine has no such code and silently draws garbage. The
USA build reaches Latin-1 through its own proportional block instead.

**Field printer** (dialogue, menus, U.M.N.; ``xglFontAscii2Euc`` and
friends in SLUS_204.69). ASCII goes through the engine's table
(``font.ASCII2EUC``). ``#`` is an escape: ``#`` plus two characters picks
code 0xD0A1 + 0x20 + xx (xx from the two hex digits; a non-hex digit
counts as 0), drawn from proportional cell 3776 + xx; ``##`` draws ``#``.
A ``#`` therefore never prints itself: a literal ``#`` must be written as
its raw code (``D1E5``), which is what ``from_display`` does. Writing the
raw two-byte code of a block cell is the same as its ``#xx`` escape and
one byte shorter, so ``from_display`` always writes raw codes.

**Two faces, two layouts.** ``font1.tex`` is the face ``xglFontInitial``
loads and the field, menus and U.M.N. keep; ``InitCfSystem`` swaps in
``font0.tex`` only for "ev" scenes (event id above 0x0FFFFFFF, which
``RES_getScenePath`` names ``ev####`` instead of ``cf####``). The two
faces do NOT agree on the block: font0 puts grave before acute for E, I,
O, U (cells 08/0B, 0C/0F, 13/16, 19/1C and lowercase 28/2B, 2E/31, 34/37,
3A/3D, plus the spacing accents 74/77), font1 the other way round; A/a
agree. Retail text is consistent with that split: field script ST2040
writes ``fianc#28`` (font1 #28 = é), cutscene SCE02001A writes
``resum#2b`` (font0 #2B = é), the U.M.N. database writes ``p#28ch#28``
(péché, font1). So the map is per face: ``LATIN_MAP`` is font1's (the
default), ``LATIN_MAP_FONT0`` font0's. Both faces also carry é and ê at
A2/A1, the same in both, and those are the codes written for é/ê, so the
two most common French letters are right whichever face draws them.
font0's È É Ê Ë cells (08-0B) are a plain E with no accent;
``fix_capital_accents`` draws the accents from font0's own À Á Â Ä.

**Battle printer** (OV01 ``menuFontPut``; ``statfont.py``): its own
10x18 grid in ``yamamoto\\stat\\stat.xtx``. Bytes go through a 230-byte
table that knows only space, digits, letters and ``. ' / -``; ``#XX``
(two hex digits, ``sscanf("%X")``) draws grid cell XX directly. The grid
holds accented Latin and punctuation in cells 63-228, so ``BATTLE_MAP``
writes those as ``#XX``. The grid has no ``à``, ``â`` or ``)`` (checked
cell by cell); those fold to ``a`` / are left as a byte that draws
nothing, with a warning. Labels are centred by byte count
(``x + (16 - len) * 5``), so each escape shifts a label 10 px left; the
checker warns.

**Tag font** (``PrintTagFont``, font ``"ascii"``: window titles and labels):
A-Z, a-z, digits, ``: / . -`` and space only, no accents (its uv table has
those 52 letters and nothing else). It has its own control bytes, NOT the
field printer's: a leading 0x01 right-aligns (no operand), 0x03 takes one
byte of extra advance, and a digit ends the label. So ``"\\x01Current"``
shows as ``⟦01⟧Current`` and every letter is free to change; accents fold
to ASCII with a warning; anything else the font cannot draw is an error.

Display form: Unicode text, real ``\\n``; control bytes (``< 0x20`` except
LF, with their operands per ``font.control_length``) and any byte that is
not a clean character are ``⟦XX⟧`` tokens; evt ``/[...]`` tags and chat
backslash escapes stay literal; ``#XX`` escapes show as the letter. The
round trip ``from_display(to_display(raw)) == raw`` holds byte for byte,
except where retail itself used ``#XX`` / ``##`` escapes (they come back
as the equivalent raw code; the selftest counts them).

Usage:
    python txcodec.py selftest --iso GAME.iso
    python txcodec.py show --iso GAME.iso --out DIR     # accent block / grid PNGs, before/after fix
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import font as fontmod
from font import Font, ascii_to_code, control_length

FIELD, BATTLE, ASCII = "field", "battle", "ascii"
FONT0, FONT1 = "font0", "font1"
CTLS = ("c0", "evt", "chat")

BLOCK_CELL = 3776                 # cell of #00 (font.cell_of_prop of code 0xD0C1)


def block_code(xx: int) -> int:
    """EUC code that ``#xx`` selects (xglFontAscii2Euc's formula)."""
    n = 0x20 + xx
    return ((0xD0 + n // 94) << 8) | (0xA1 + n % 94)


def block_index(code: int) -> int:
    """Inverse of block_code, or -1 for codes outside the block."""
    hi, lo = code >> 8, code & 0xFF
    if not (0xD0 <= hi <= 0xD3 and 0xA1 <= lo <= 0xFE):
        return -1
    xx = (hi - 0xD0) * 94 + (lo - 0xA1) - 0x20
    return xx if 0 <= xx < 0x100 else -1


# -- the proportional block, as drawn ------------------------------------------
# Transcribed from enlarged renders of every cell #00-#BF of the retail
# font1.tex and font0.tex (``python txcodec.py show``); None = blank or a
# glyph not given a character here. #B0-#BF are blank in both faces.
_ROWS_FONT1 = {
    0x00: "ÆÀÂÄÁÅÃÇÉÊËÈÍÎÏÌ",
    0x10: "ÐÑŒÓÔÖÒÕØÚÛÜÙÝŸ\0",
    0x20: "æàâäáåãçéêëèﬁﬂíî",
    0x30: "ïìñœóôöòõøúûüùýÿ",
    0x40: "|£¥ƒ$¢€™©®@ªº†‡§",
    0x50: "¶*!¡?¿.,;:'\"‘’“”",       # 5A/5B straight quotes; ASCII ' and " draw 5D/5F
    0x60: "‚„…‥<>«»()[]{}|‾",
    0x70: "_/\\·´^¨`˚˜¯˘˙ˇ˝\0",
    0x80: "¸˛#%‰~—〔〕【】○\0△□Þ",      # 8C is a large saltire, left unnamed
    0x90: "þß^Šš±¹²³=−+×÷\0\0",
    0xA0: "–êé&$ðµ¼½¾¤¦\0\0\0\0",      # AC a slashed c variant, AD a stray I-beam
}
# font0 (the "ev" scene face) differs only in these cells
_FONT0_DIFF = {
    0x08: "È", 0x0B: "É", 0x0C: "Ì", 0x0F: "Í", 0x13: "Ò", 0x16: "Ó",
    0x19: "Ù", 0x1C: "Ú", 0x28: "è", 0x2B: "é", 0x2E: "ì", 0x31: "í",
    0x34: "ò", 0x37: "ó", 0x3A: "ù", 0x3D: "ú", 0x74: "`", 0x77: "´",
}


def _block_glyphs(face: str) -> Dict[int, str]:
    g = {}
    for base, row in _ROWS_FONT1.items():
        for i, ch in enumerate(row):
            if ch != "\0":
                g[base + i] = ch
    if face == FONT0:
        g.update(_FONT0_DIFF)
    return g


BLOCK_GLYPHS = {FONT0: _block_glyphs(FONT0), FONT1: _block_glyphs(FONT1)}

# Characters JIS X 0208 also has and retail writes as JIS (full-width
# symbols): keep the JIS code so retail text round-trips.
_KEEP_JIS = set("〔〕【】○△□×÷±−")
# The ASCII byte draws exactly this glyph (ASCII2EUC lands on the cell).
ALIASES = {"’": "'", "”": '"'}
# Same glyph in both faces: written for these letters whatever the face.
_SHARED = {"é": 0xA2, "ê": 0xA1}


def _latin_map(face: str) -> Dict[str, int]:
    m: Dict[str, int] = {}
    for xx in sorted(BLOCK_GLYPHS[face]):
        ch = BLOCK_GLYPHS[face][xx]
        if ch == "#":
            m[ch] = block_code(xx)
            continue
        if (" " <= ch <= "~") or ch in _KEEP_JIS or ch in ALIASES or ch in m:
            continue
        m[ch] = block_code(xx)
    for ch, xx in _SHARED.items():
        m[ch] = block_code(xx)
    return m


LATIN_MAP_FONT0: Dict[str, int] = _latin_map(FONT0)
LATIN_MAP: Dict[str, int] = _latin_map(FONT1)      # font1: field, menus, U.M.N.
FACE_MAPS = {FONT0: LATIN_MAP_FONT0, FONT1: LATIN_MAP}
# letters whose code depends on the face (grave/acute order)
FACE_DEPENDENT = sorted(c for c in LATIN_MAP if LATIN_MAP_FONT0.get(c) != LATIN_MAP[c])
FONT0_UNACCENTED = "ÈÉÊË"        # retail font0 cells 08-0B: plain E until fixed


# -- the battle grid, as drawn ---------------------------------------------------
# Cell index -> character, transcribed from enlarged renders of the retail
# stat.xtx grid (cells 0-62 are space, digits, letters in table order).
_BATTLE_CELLS = {}
for _i, _ch in enumerate("ÆÁÂÄÀÅÃÇÉÊËÈÍÎÏÌÐÑŒÓÔÖÒÕØÚÛÜÙÝŸæäáåãçéêëèﬁﬂíîïìñœóôöòõøúûüùýÿ"):
    _BATTLE_CELLS[63 + _i] = _ch
for _i, _ch in enumerate("|£¥ƒ$¢€™©®@ªº†‡§¶*!¡?¿.,"):
    _BATTLE_CELLS[125 + _i] = _ch
for _i, _ch in enumerate(";:'\"‘’“”‚„…‥<>«»([]{}|‾_"):
    _BATTLE_CELLS[150 + _i] = _ch
for _i, _ch in enumerate("/\\·´^¨`˚˜¯˘˙ˇ˝¸˛#%‰~-〔〕【】○×△□Þþß^Šš±¹²³=−+×÷&$ðµ¼½¾¤¦"):
    _BATTLE_CELLS[175 + _i] = _ch
BATTLE_CELLS: Dict[int, str] = dict(_BATTLE_CELLS)
_BATTLE_TABLE = (" 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                 + " " * 84 + "." + " " * 4 + "'" + " " * 22 + "/" + " " * 19 + "-")
_BATTLE_NATIVE = set(_BATTLE_TABLE)
# two-byte table menuJpText4: ／ 　 ・ ＾ (first match wins)
_BATTLE_JIS = {"／": 175, "　": 176, "・": 177, "＾": 179}


def _battle_map() -> Dict[str, bytes]:
    m: Dict[str, bytes] = {}
    for cell in sorted(BATTLE_CELLS):
        ch = BATTLE_CELLS[cell]
        if ch in _BATTLE_NATIVE or ch in _BATTLE_JIS or ch in m:
            continue
        m[ch] = b"#%02X" % cell
    for ch, a in ALIASES.items():          # the grid has real curly quotes
        m.setdefault(ch, m.get(a, a.encode()))
    return m


BATTLE_MAP: Dict[str, bytes] = _battle_map()
BATTLE_MISSING = "àâ)"                     # no cell draws these


# -- folding (battle fallbacks, ASCII-only ribbons) ------------------------------
_FOLD_SPECIAL = {"œ": "oe", "Œ": "OE", "æ": "ae", "Æ": "AE", "ß": "ss", "ﬁ": "fi", "ﬂ": "fl",
                 "«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "‘": "'", "’": "'",
                 "‚": ",", "…": "...", "—": "-", "–": "-", "−": "-", "¿": "?", "¡": "!",
                 "€": "EUR", "º": "o", "ª": "a", "·": ".", "×": "x", "Ø": "O", "ø": "o",
                 "Ð": "D", "ð": "d", "Þ": "Th", "þ": "th", "　": " "}


def fold_ascii(ch: str) -> Optional[str]:
    """Nearest plain-ASCII spelling of ``ch``, or None."""
    if " " <= ch <= "~":
        return ch
    if ch in _FOLD_SPECIAL:
        return _FOLD_SPECIAL[ch]
    base = "".join(c for c in unicodedata.normalize("NFKD", ch)
                   if not unicodedata.combining(c))
    return base if base and all(" " <= c <= "~" for c in base) else None


# -- typing artefacts ---------------------------------------------------------------
# DeepL, Word and French keyboards put no-break / thin spaces before ? ! : ; »
# and after «; macOS copy sources can hand over decomposed accents (e + U+0301).
# No printer has a glyph for these, but each has an obvious equivalent: the
# text is NFC-normalised first and these are written as the plain character
# (or dropped) with a warning, the same in every font.
SPACE_LIKE = {"\u00a0": " ", "\u202f": " ", "\u2009": " ", "\u2007": " ",
              "\u2011": "-", "\u02bc": "\u2019"}
INVISIBLE = frozenset("\u00ad\u200b\ufeff")


def char_name(ch: str) -> str:
    """'U+00A0 NO-BREAK SPACE' for characters a textarea does not show."""
    return f"U+{ord(ch):04X} {unicodedata.name(ch, 'unnamed')}"


def _shown(ch: str) -> str:
    return f"{ch!r}" if ch.isprintable() and not (ch.isspace() and ch != " ") \
        else f"{ch!r} ({char_name(ch)})"


# -- errors ----------------------------------------------------------------------

class TxEncodeError(ValueError):
    def __init__(self, problems: List[Tuple[int, str, str]]):
        self.problems = problems
        super().__init__("; ".join(f"pos {p}: {c!r}: {m}" for p, c, m in problems[:5])
                         + (f" (+{len(problems) - 5} more)" if len(problems) > 5 else ""))


class TxBuildError(RuntimeError):
    """Raised by the family modules when a build must not write anything."""


# -- tokens ------------------------------------------------------------------------
_TOK = "⟦{:02X}⟧"
_TOK_RE = re.compile("⟦([0-9A-Fa-f]{2})⟧")
_TOKRUN_RE = re.compile("(?:⟦[0-9A-Fa-f]{2}⟧)+")
_TAG_RE = re.compile(r"/\[[^\]\n]*\]")
_TAG_RAW_RE = re.compile(rb"/\[[^\]\n]*\]")
_CHAT_RE = re.compile(r"\\(?:[0-7]{1,3}|.)")
_PRINTF_RE = re.compile(r"%[-+0#]*\d*(?:\.\d+)?[lh]?[diouxXcsfeg]")
_HEX = b"0123456789abcdefABCDEF"


def _hexval(b: int) -> int:
    return int(chr(b), 16)


# -- new-script glyph codes (build only) -----------------------------------------
# A translation into a script the retail fonts never had (Cyrillic, Arabic,
# level-2 kanji, ...) is encoded by ``txtarget`` at build time: new letters
# get cells in font0/font1 (or the battle grid) and each string is rewritten
# with their codes. Those codes travel through the family modules as
# characters of Supplementary Private Use Area-A, one character per code,
# so ``check`` / ``validate`` / byte limits see ordinary text (no ``⟦XX⟧``
# token mismatch) and ``from_display`` writes the exact bytes:
#   U+F0000 + code, code >= 0x100: the two bytes (code >> 8, code & 0xFF)
#                                  (field printer: a proportional slot or a
#                                  kanji cell the build redrew);
#   U+F0000 + b,    b < 0x100:     the single byte b (battle printer: a byte
#                                  the build wrote into menuFontIdxGet's table,
#                                  or one byte of a #XX cell escape).
# They are accepted only inside ``with glyph_codes():`` (txbuild turns it on
# for a build whose target needs new glyphs); anywhere else they are an
# error, so a stray private-use character typed into the editor never
# reaches a disc.
GLYPH_BASE = 0xF0000
_GLYPH_CODES_ON = [0]


def glyph_char(code: int) -> str:
    """The private-use character that carries ``code`` (see above)."""
    if not (0 < code <= 0xFFFF):
        raise ValueError(f"glyph code {code:#x} out of range")
    return chr(GLYPH_BASE + code)


def glyph_code(ch: str) -> Optional[int]:
    """The code a glyph character carries, or None for any other character."""
    o = ord(ch) - GLYPH_BASE
    return o if 0 < o <= 0xFFFF else None


class glyph_codes:
    """Context manager: accept glyph characters in from_display/check (nests)."""

    def __enter__(self):
        _GLYPH_CODES_ON[0] += 1
        return self

    def __exit__(self, *exc):
        _GLYPH_CODES_ON[0] -= 1
        return False


def _glyph_bytes(ch: str, font: str) -> Tuple[Optional[bytes], Optional[str]]:
    code = glyph_code(ch)
    if not _GLYPH_CODES_ON[0]:
        return None, "private-use character (new-script glyph codes exist only inside a build)"
    if code < 0x100:
        if font != BATTLE:
            return None, "single-byte glyph code outside the battle font"
        return bytes((code,)), None
    if font != FIELD or not (0xA1 <= code >> 8 <= 0xFE and 0xA1 <= code & 0xFF <= 0xFE):
        return None, f"glyph code {code:04X} is not a two-byte font code"
    return bytes((code >> 8, code & 0xFF)), None


def draws_natively(ch: str, font: str = FIELD, face: str = FONT1) -> bool:
    """Can the RETAIL font of ``font`` draw ``ch`` exactly (no fold, no new
    glyph)? Field: ASCII, the face's accent block (LATIN_MAP), the aliases
    and every JIS X 0208 character of rows A1-CF (symbols, kana, Greek,
    Cyrillic, level-1 kanji). Battle: the table letters, the grid's accent
    and punctuation cells (BATTLE_MAP) and its four JIS symbols. Tag font
    ("ascii"): TAG_GLYPHS. Space-like and invisible characters count as
    native in every font (they are written as a space / dropped)."""
    if ch in SPACE_LIKE or ch in INVISIBLE or ch == "\n":
        return True
    if font == ASCII:
        return ch in TAG_GLYPHS
    if font == BATTLE:
        return ch in _BATTLE_NATIVE or ch in BATTLE_MAP or ch in _BATTLE_JIS
    if " " <= ch <= "~" or ch in ALIASES or ch in FACE_MAPS[face]:
        return True
    return _encode_jis(ch)[1] is None


# -- encoding one character ----------------------------------------------------

def _jis_ok(enc: bytes) -> bool:
    """A two-byte JIS X 0208 code the kanji path draws (rows A1..CF; codes
    from D0 up are the USA proportional block, not the kanji they name)."""
    return len(enc) == 2 and 0xA1 <= enc[0] <= 0xCF and 0xA1 <= enc[1] <= 0xFE


def _encode_char(ch: str, font: str, face: str, literal_hash: bool,
                 warns: List[str]) -> Tuple[Optional[bytes], Optional[str]]:
    """-> (bytes, None) or (None, error message)."""
    if ch == "\n":
        return b"\n", None
    if glyph_code(ch) is not None:
        return _glyph_bytes(ch, font)
    if ch in INVISIBLE:
        warns.append(f"{char_name(ch)} dropped (it draws nothing)")
        return b"", None
    if ch in SPACE_LIKE:
        rep = SPACE_LIKE[ch]
        b, err = _encode_char(rep, font, face, literal_hash, warns)
        if err is None:
            warns.append(f"{char_name(ch)} written as {rep!r}")
        return b, err
    if font == FIELD:
        if ch in ALIASES:
            return ALIASES[ch].encode(), None
        if ch == "#":
            return (b"#", None) if literal_hash else (_code_bytes(LATIN_MAP["#"]), None)
        if " " <= ch <= "~":
            return ch.encode(), None
        m = FACE_MAPS[face]
        if ch in m:
            return _code_bytes(m[ch]), None
        return _encode_jis(ch)
    if font == BATTLE:
        if ch in _BATTLE_NATIVE:
            return ch.encode(), None
        if ch == "#" and literal_hash:
            return b"#", None
        if ch in BATTLE_MAP:
            return BATTLE_MAP[ch], None
        if ch in _BATTLE_JIS:
            return ch.encode("euc_jp"), None
        if " " <= ch <= "~":
            warns.append(f"{ch!r} has no cell in the battle font (draws nothing)")
            return ch.encode(), None
        f = fold_ascii(ch)
        if f is not None:
            out = b""
            for c in f:
                b, err = _encode_char(c, BATTLE, face, literal_hash, [])
                if err:
                    return None, err
                out += b
            warns.append(f"{ch!r} is not in the battle font; written as {f!r}")
            return out, None
        enc, err = _encode_jis(ch)
        if err:
            return None, err
        warns.append(f"{ch!r} draws nothing in the battle font")
        return enc, None
    # tag font (PrintTagFont): see _tag_from_display for the whole-string rules
    if ch in TAG_GLYPHS:
        return ch.encode(), None
    f = fold_ascii(ch)
    if f is None or any(c not in TAG_GLYPHS for c in f):
        return None, _TAG_NO_GLYPH
    warns.append(f"{ch!r} written as {f!r} (the tag font has no accents)")
    return f.encode(), None


# -- the tag font (PrintTagFont) ---------------------------------------------------
# Window titles and labels (eTagFontSet, window tag_id, END_PRINT_CMD_TAG_FONT).
# From the matching decomp, src/main/end_print.c PrintTagFont with tag_uv[52]
# and num_uv[10]:
#   * 'A'-'Z' and 'a'-'z' are tag_uv cells 0-25 / 26-51 -- the whole letter set;
#     tag_uv has no other entry, so no accented letter is reachable;
#   * ':' '/' '.' '-' come from the number strip (num_uv 4 / 3 / 9 / 6);
#   * a digit is drawn from the number strip and then the function RETURNS:
#     nothing after a digit is drawn;
#   * ' ' advances 8 px (TAG_TEXT_SPACE_WIDTH);
#   * byte 3 (TAG_TEXT_ADVANCE_CODE) is followed by one byte: extra advance in px;
#   * byte 1 (TAG_TEXT_RIGHT_ALIGN) right-aligns the label, only as the FIRST
#     byte, and takes no operand (the next byte is an ordinary letter);
#   * any other byte leaves the previous cell selected and draws it again.
TAG_RIGHT_ALIGN, TAG_ADVANCE, TAG_SPACE_PX = 0x01, 0x03, 8
TAG_GLYPHS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789:/.- ")
_TAG_NO_GLYPH = ("the tag font draws only A-Z a-z 0-9 : / . - and space "
                 "(any other byte draws the previous letter again)")
# glyph widths in px (tag_uv / num_uv widths)
_TAG_WIDTH = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                      (10, 10, 10, 10, 9, 9, 10, 10, 6, 8, 10, 9, 13, 12, 10, 9, 10, 10, 10, 10,
                       10, 10, 13, 10, 10, 11)))
_TAG_WIDTH.update(zip("abcdefghijklmnopqrstuvwxyz",
                      (8, 8, 8, 8, 7, 7, 8, 8, 5, 6, 8, 7, 10, 9, 8, 8, 8, 8, 8, 7, 8, 9, 11, 9,
                       8, 8)))
_TAG_WIDTH.update({d: 11 for d in "0123456789"})
_TAG_WIDTH.update({":": 10, "/": 7, ".": 4, "-": 11})


def tag_width(raw: bytes) -> int:
    """Pixel width PrintTagFont gives ``raw`` (its draw loop, byte for byte)."""
    i = 1 if raw[:1] == bytes((TAG_RIGHT_ALIGN,)) else 0
    x, last = 0, _TAG_WIDTH["A"]
    while i < len(raw) and raw[i]:
        b = raw[i]
        if b == TAG_ADVANCE:
            x += raw[i + 1] if i + 1 < len(raw) else 0
            i += 2
            continue
        if b == 0x20:
            x += TAG_SPACE_PX
            i += 1
            continue
        w = _TAG_WIDTH.get(chr(b), last)
        x += w
        if 0x30 <= b <= 0x39:
            break
        last = w
        i += 1
    return x


def tag_render(raw: bytes) -> str:
    """What PrintTagFont draws for ``raw``, as text (repeats and truncation shown)."""
    out: List[str] = []
    i = 1 if raw[:1] == bytes((TAG_RIGHT_ALIGN,)) else 0
    last = "A"
    while i < len(raw) and raw[i]:
        b = raw[i]
        if b == TAG_ADVANCE:
            out.append(" ")
            i += 2
            continue
        ch = chr(b) if b < 0x80 and chr(b) in TAG_GLYPHS else last
        out.append(ch)
        if "0" <= ch <= "9":
            break
        if ch != " ":
            last = ch
        i += 1
    return "".join(out)


def _tag_to_display(raw: bytes) -> str:
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        b = raw[i]
        if b == TAG_ADVANCE:
            out.extend(_TOK.format(x) for x in raw[i:i + 2])
            i += 2
        elif b < 0x80 and chr(b) in TAG_GLYPHS:
            out.append(chr(b))
            i += 1
        else:                       # 0x01 marker, or a byte that repeats the last cell
            out.append(_TOK.format(b))
            i += 1
    return "".join(out)


def _tag_from_display(text: str, ctl: str) -> Tuple[bytes, List[str]]:
    out = bytearray()
    pos: List[int] = []
    warns: List[str] = []
    problems: List[Tuple[int, str, str]] = []
    for kind, start, s in _segments(text, ctl):
        if kind == "tok":
            out.append(int(s[1:3], 16))
            pos.append(start)
            continue
        for k, ch in enumerate(s):
            if ch == "\n":
                problems.append((start + k, ch, "the tag font has no line break"))
                continue
            b, err = _encode_char(ch, ASCII, FONT1, False, warns)
            if err:
                problems.append((start + k, ch, err))
            else:
                out += b
                pos.extend([start + k] * len(b))
    i = 1 if out[:1] == bytes((TAG_RIGHT_ALIGN,)) else 0
    while i < len(out):
        b = out[i]
        if b == TAG_ADVANCE:
            i += 2
            continue
        if 0x30 <= b <= 0x39 and i + 1 < len(out):
            problems.append((pos[i], chr(b), "PrintTagFont stops at a digit: the text after "
                             "it is not drawn (put the digit last)"))
            break
        if b == TAG_RIGHT_ALIGN:
            warns.append("⟦01⟧ right-aligns only as the first byte; anywhere else it draws "
                         "the previous letter again")
        i += 1
    if problems:
        raise TxEncodeError(problems)
    return bytes(out), warns


def _code_bytes(code: int) -> bytes:
    return bytes((code >> 8, code & 0xFF))


def _encode_jis(ch: str) -> Tuple[Optional[bytes], Optional[str]]:
    if ord(ch) < 0x20 or ch == "\x7f":
        return None, "raw control character (write it as ⟦XX⟧)"
    try:
        enc = ch.encode("euc_jp")
    except UnicodeEncodeError:
        return None, "not in the game's font"
    if b"\x8f" in enc:
        return None, "JIS X 0212 character: the engine has no such code"
    if enc[:1] == b"\x8e":
        return None, "half-width katakana: not drawn by the engine"
    if len(enc) == 2 and enc[0] >= 0xD0:
        return None, "kanji outside the font (its code is the USA proportional block)"
    if not _jis_ok(enc):
        return None, "not in the game's font"
    return enc, None


# -- display -> bytes ---------------------------------------------------------------

def _segments(text: str, ctl: str):
    """Yield (kind, start, string) with kind tok/tag/chat/text."""
    pats = [("tok", _TOK_RE)]
    if ctl == "evt":
        pats.append(("tag", _TAG_RE))
    if ctl == "chat":
        pats.append(("chat", _CHAT_RE))
    pos = 0
    while pos < len(text):
        best = None
        for kind, rx in pats:
            m = rx.search(text, pos)
            if m and (best is None or m.start() < best[1].start()):
                best = (kind, m)
        if best is None:
            yield "text", pos, text[pos:]
            return
        kind, m = best
        if m.start() > pos:
            yield "text", pos, text[pos:m.start()]
        yield kind, m.start(), m.group(0)
        pos = m.end()


def from_display(text: str, font: str = FIELD, ctl: str = "c0",
                 face: str = FONT1) -> Tuple[bytes, List[str]]:
    """Display text -> engine bytes. Raises TxEncodeError (with every problem
    position) when something cannot be written; returns (bytes, warnings).
    The text is NFC-normalised first (positions refer to the normalised text)."""
    text = unicodedata.normalize("NFC", text)
    if font == ASCII:
        return _tag_from_display(text, ctl)
    out = bytearray()
    warns: List[str] = []
    problems: List[Tuple[int, str, str]] = []
    for kind, start, s in _segments(text, ctl):
        if kind == "tok":
            out.append(int(s[1:3], 16))
            continue
        literal = kind in ("tag", "chat")
        for i, ch in enumerate(s):
            b, err = _encode_char(ch, font, face, literal, warns)
            if err:
                problems.append((start + i, ch, err))
            else:
                out += b
    if problems:
        raise TxEncodeError(problems)
    return bytes(out), warns


# -- bytes -> display ---------------------------------------------------------------

def _canon(ch: str, font: str, face: str) -> Optional[bytes]:
    try:
        b, err = _encode_char(ch, font, face, False, [])
    except Exception:
        return None
    return None if err else b


def _same_glyph(ch: str, xx: int, font: str, face: str) -> bool:
    """Does writing ``ch`` the normal way draw what escape ``#xx`` draws?
    (Straight-quote cells #5A/#5B, for instance, have no typed spelling:
    ASCII ' and " draw the curly cells.)"""
    b = _canon(ch, font, face)
    if b is None:
        return False
    if font == BATTLE:
        return b == b"#%02X" % xx or (len(b) == 1 and _BATTLE_TABLE.find(chr(b[0])) == xx)
    if len(b) == 1:
        code = ascii_to_code(b[0])
    elif len(b) == 2:
        code = (b[0] << 8) | b[1]
    else:
        return False
    k = block_index(code)
    return BLOCK_GLYPHS[face].get(k) == ch if k >= 0 else False


def to_display(raw: bytes, ctl: str = "c0", font: str = FIELD, face: str = FONT1) -> str:
    """Engine bytes -> display text (see the module docstring)."""
    if font == ASCII:
        return _tag_to_display(raw)
    out: List[str] = []
    n = len(raw)
    i = 0
    tag_end = -1                       # evt /[...] tag: '#' is literal inside
    glyphs = BLOCK_GLYPHS[face]
    while i < n:
        b = raw[i]
        if b == 0x0A:
            out.append("\n")
            i += 1
            continue
        if b < 0x20:
            L = min(control_length(raw, i), n - i)
            out.extend(_TOK.format(x) for x in raw[i:i + L])
            i += L
            continue
        if b == 0x7F:
            out.append(_TOK.format(b))
            i += 1
            continue
        if b < 0x80:
            if ctl == "evt" and b == 0x2F and i >= tag_end:
                m = _TAG_RAW_RE.match(raw, i)
                if m:
                    tag_end = m.end()
            in_tag = i < tag_end
            if b == 0x23 and not in_tag and font in (FIELD, BATTLE):
                ch, step = None, 1
                if i + 2 < n and raw[i + 1] in _HEX and raw[i + 2] in _HEX:
                    xx = _hexval(raw[i + 1]) * 16 + _hexval(raw[i + 2])
                    ch = glyphs.get(xx) if font == FIELD else BATTLE_CELLS.get(xx)
                    step = 3
                elif font == FIELD and i + 1 < n and raw[i + 1] == 0x23:
                    ch, step = "#", 2
                if ch is not None and _same_glyph(ch, xx if step == 3 else 0x82, font, face):
                    out.append(ch)
                    i += step
                    continue
                out.append(_TOK.format(b))
                i += 1
                continue
            ch = chr(b)
            # a decoded character must encode back to exactly this byte
            if _canon(ch, font, face) == bytes((b,)) or (in_tag and ch == "#"):
                out.append(ch)
            else:
                out.append(_TOK.format(b))
            i += 1
            continue
        # >= 0x80: the engine always pairs
        if i + 1 >= n:
            out.append(_TOK.format(b))
            i += 1
            continue
        pair = raw[i:i + 2]
        ch = None
        if 0xA1 <= pair[0] <= 0xFE and 0xA1 <= pair[1] <= 0xFE:
            if pair[0] >= 0xD0:
                xx = block_index((pair[0] << 8) | pair[1])
                if font == FIELD and xx >= 0:
                    ch = glyphs.get(xx)
            else:
                try:
                    ch = pair.decode("euc_jp")
                except UnicodeDecodeError:
                    ch = None
        if ch is not None and len(ch) == 1 and _canon(ch, font, face) == pair:
            out.append(ch)
        else:
            out.extend(_TOK.format(x) for x in pair)
        i += 2
    return "".join(out)


# -- protected tokens / checker ----------------------------------------------------

def _protected(text: str, ctl: str) -> List[Tuple[str, str]]:
    found: List[Tuple[int, str, str]] = []
    for m in _TOKRUN_RE.finditer(text):
        found.append((m.start(), "tok", m.group(0)))
    if ctl == "evt":
        for m in _TAG_RE.finditer(text):
            found.append((m.start(), "tag", m.group(0)))
    if ctl == "chat":
        for m in _CHAT_RE.finditer(text):
            found.append((m.start(), "chat", m.group(0)))
    tagspans = [(m.start(), m.end()) for m in _TAG_RE.finditer(text)] if ctl == "evt" else []
    for m in _PRINTF_RE.finditer(text):
        if not any(a <= m.start() < b for a, b in tagspans):
            found.append((m.start(), "printf", m.group(0)))
    found.sort()
    return [(k, s) for _, k, s in found]


def protected_tokens(text: str, ctl: str = "c0") -> List[str]:
    """Tokens a translation must keep: ⟦XX⟧ runs, evt /[..] tags, chat
    backslash escapes, printf conversions — in order of appearance."""
    return [s for _, s in _protected(text, ctl)]


def _multiset_diff(a: List[str], b: List[str]) -> Tuple[List[str], List[str]]:
    from collections import Counter
    ca, cb = Counter(a), Counter(b)
    return sorted((ca - cb).elements()), sorted((cb - ca).elements())


def check(text: str, source: str, font: str = FIELD, ctl: str = "c0",
          limit_bytes: Optional[int] = None, measure: Optional[Font] = None,
          width_px: Optional[int] = None, face: str = FONT1) -> dict:
    """Validate one translation against its source. ``ok`` is False when
    there is any error; warnings never block a build."""
    errors: List[str] = []
    warnings: List[str] = []
    data = b""
    try:
        data, w = from_display(text, font, ctl, face)
        warnings += list(dict.fromkeys(w))
    except TxEncodeError as e:
        for pos, ch, msg in e.problems:
            errors.append(f"position {pos}: {_shown(ch)}: {msg}")
    src = _protected(source, ctl)
    dst = _protected(text, ctl)
    for kind in ("tok", "tag", "chat", "printf"):
        missing, extra = _multiset_diff([s for k, s in src if k == kind],
                                        [s for k, s in dst if k == kind])
        if missing or extra:
            msg = (f"{kind} tokens differ from the source:"
                   + (f" missing {missing}" if missing else "")
                   + (f" extra {extra}" if extra else ""))
            (errors if kind in ("tok", "tag") else warnings).append(msg)
    if font == FIELD:
        if ctl != "chat" and "\\" in "".join(t for k, _, t in _segments(text, ctl) if k == "text"):
            warnings.append("'\\' is drawn as ¥ by the field font")
        if face == FONT0 and any(c in text for c in FONT0_UNACCENTED):
            warnings.append("È É Ê Ë need the font0 accent fix (the build applies it)")
    if font == BATTLE:
        esc = len(re.findall(rb"#[0-9A-F]{2}", data))
        if esc:
            warnings.append(f"battle labels are centred by byte count: {esc} escape(s) "
                            f"shift this label {esc * 10} px left")
    nbytes = len(data)
    if limit_bytes is not None and nbytes > limit_bytes:
        errors.append(f"{nbytes} bytes, over the {limit_bytes}-byte limit")
    px = None
    px_lines: Optional[List[int]] = None
    if data and font == FIELD and measure is not None:
        # evt /[..] tags are control language, not glyphs: measure without them
        drawn = _TAG_RAW_RE.sub(b"", data) if ctl == "evt" else data
        px_lines = [measure.width_of_bytes(line) for line in drawn.split(b"\n")]
        px = max(px_lines)
    elif data and font == BATTLE:
        px = max(len(re.sub(rb"#..|[\x80-\xff].", b"x", line)) * 10 for line in data.split(b"\n"))
    if px is not None and width_px is not None and px > width_px:
        warnings.append(f"line is {px} px wide (guide {width_px} px)")
    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "nbytes": nbytes, "px": px, "px_lines": px_lines}


def needs_font0_fix(text: str) -> bool:
    return any(c in text for c in FONT0_UNACCENTED)


_SCENE_FACES: Dict[str, Dict[str, Tuple[str, ...]]] = {}
_SCRIPT_LINE_RE = re.compile(rb"^[ \t]*[0-9]*[ \t]*script[ \t]+([^ \t\r\n;]+)", re.M)


def _evt_stem(path: str) -> str:
    s = path.replace("/", "\\").rsplit("\\", 1)[-1].upper()
    return s[:-4] if s.endswith(".EVT") else s


def scene_faces(iso_path: str) -> Dict[str, Tuple[str, ...]]:
    """Container stem (upper case, no ``.evt``) -> faces of every scene that
    runs it, read from the scene planner files ``scene\\cf####.txt`` /
    ``scene\\ev####.txt`` on the disc.

    Traced in the decomp (KOS-MOS ver. 3): ``Runtime.jumpEvent(n)`` jumps to
    ``n + 0x10000000`` and ``Runtime.jumpCF(n)`` to ``n``
    (src/main/runtime.c ``Java_xeno_util_Runtime_jumpEvent__I`` /
    ``jumpCF__II``); ``Java_xeno_util_Runtime_jump_sub`` stores it in
    ``current_event_id`` and calls ``InitCfSystem``, which loads font0 when
    ``current_event_id & 0xF0000000`` and font1 otherwise
    (src/main/game.c ``InitCfSystem``); ``GameResourceLoad`` reads
    ``RES_getScenePath``, which names ids above 0x0FFFFFFF ``ev####.txt`` and
    the others ``cf####.txt`` (src/main/res_get_path.c), and the planner's
    ``script NAME`` line loads ``scene\\NAME.evt`` (``command_script``).
    So an ev planner file means font0 and a cf planner file font1."""
    key = str(Path(iso_path).resolve())
    if key in _SCENE_FACES:
        return _SCENE_FACES[key]
    from chains import CHAINS, ChainReader
    from iso9660 import IsoImage
    from toc import parse_toc
    faces: Dict[str, set] = {}
    iso = IsoImage(iso_path)
    try:
        for chain in sorted(CHAINS):
            try:
                reader = ChainReader(iso, chain)
            except FileNotFoundError:
                continue
            for e in parse_toc(reader.toc_bytes(), CHAINS[chain][0]):
                p = e.path.replace("/", "\\").lower()
                if e.ext != "txt" or e.compressed or not p.startswith("scene\\"):
                    continue
                name = p.rsplit("\\", 1)[-1]
                if not re.fullmatch(r"(cf|ev)\d{4}\.txt", name):
                    continue
                face = FONT0 if name.startswith("ev") else FONT1
                data = b"".join(reader.read_iter(e.byte_offset, e.size))
                body = data.split(b"\n#end", 1)[0]
                for m in _SCRIPT_LINE_RE.finditer(body):
                    stem = m.group(1).decode("ascii", "replace").upper()
                    faces.setdefault(stem, set()).add(face)
    finally:
        iso.close()
    out = {k: tuple(sorted(v)) for k, v in faces.items()}
    _SCENE_FACES[key] = out
    return out


def evt_face(chain: int, path: str, iso_path: Optional[str] = None) -> str:
    """Face (font0/font1) that draws an .evt container's text.

    With ``iso_path`` it is read from the disc's scene planner files (see
    ``scene_faces``): a container run by ``ev####`` scenes is drawn with
    font0, one run by ``cf####`` scenes (or by no scene: ``base``,
    ``system``) with font1. On the USA disc that gives font0 for all 181
    SCE* containers (chain 1 AND chain 2) and font1 for the 386 ST* maps.
    Without ``iso_path`` the same split is applied by name (SCE* -> font0),
    which agrees with the planner data for every retail container. Retail's
    own face-sensitive escapes agree too: SCE02001A ``resum#2b`` (font0 #2B
    = é) and ST2040/ST2041 ``fianc#28`` (font1 #28 = é)."""
    stem = _evt_stem(path)
    if iso_path:
        got = scene_faces(iso_path).get(stem)
        if got:
            return got[0] if len(got) == 1 else FONT1
        return FONT1
    return FONT0 if stem.startswith("SCE") else FONT1


# -- fonts -------------------------------------------------------------------------
_FONT_CACHE: Dict[str, Dict[str, Font]] = {}


def load_fonts(iso_path: str) -> Dict[str, Font]:
    key = str(Path(iso_path).resolve())
    if key not in _FONT_CACHE:
        from repack import read_entry
        _FONT_CACHE[key] = {n: Font.from_bytes(read_entry(iso_path, 0, f"{n}.tex"))
                            for n in (FONT0, FONT1)}
    return _FONT_CACHE[key]


def _copy_font(f: Font) -> Font:
    return Font(list(f.headers), [bytearray(s) for s in f.sheets], list(f.metrics), f.tail)


def _plain_cell(letter: str) -> int:
    return fontmod.glyph_cell(ascii_to_code(ord(letter)))


def _top_row(rows, min_level: int = 1) -> int:
    for r, row in enumerate(rows):
        if any(v >= min_level for v in row):
            return r
    return len(rows)


def _centre(rows, r0: int) -> float:
    cols = [c for c in range(fontmod.CELL_W) if any(rows[r][c] >= 2 for r in range(r0, len(rows)))]
    return (cols[0] + cols[-1]) / 2 if cols else fontmod.CELL_W / 2


_ACCENT_OF = {"̀": "grave", "́": "acute", "̂": "circ", "̈": "diaer",
              "̃": "tilde", "̊": "ring"}
_DONOR = {"grave": "À", "acute": "Á", "circ": "Â", "diaer": "Ä", "tilde": "Ã", "ring": "Å"}


def _decompose(ch: str) -> Tuple[str, Optional[str]]:
    d = unicodedata.normalize("NFD", ch)
    if len(d) == 2 and d[1] in _ACCENT_OF:
        return d[0], _ACCENT_OF[d[1]]
    return ch, None


def accent_rows(font: Font, ch: str, face: str):
    """(rows above the plain base letter's top, top row) for an accented
    letter's block cell, or None if ``ch`` is not an accented letter."""
    base, acc = _decompose(ch)
    xx = _xx_of(ch, face)
    if acc is None or xx is None or not ("A" <= base.upper() <= "Z"):
        return None
    # the dotted i would hide the accent rows: measure from n's x-height
    top = _top_row(font.cell(_plain_cell("n" if base == "i" else base)))
    rows = font.cell(BLOCK_CELL + xx)
    return rows[:top], top


def _xx_of(ch: str, face: str) -> Optional[int]:
    for xx, g in BLOCK_GLYPHS[face].items():
        if g == ch:
            return xx
    return None


def missing_capital_accents(font: Font, face: str) -> List[str]:
    """Accented capitals whose cell has no ink above the plain letter."""
    out = []
    for ch in "ÀÁÂÄÃÅÈÉÊËÌÍÎÏÒÓÔÖÕÙÚÛÜÑÝŸ":
        ar = accent_rows(font, ch, face)
        if ar and not any(v for row in ar[0] for v in row):
            out.append(ch)
    return out


def fix_capital_accents(font0: Font, face: str = FONT0) -> Font:
    """Return a copy of the font with every accented capital that lacks its
    accent (retail font0: È É Ê Ë) given the accent of the same face's
    À / Á / Â / Ä (Ã, Å for tilde/ring), re-centred over the letter."""
    f = _copy_font(font0)
    plain_a_top = _top_row(f.cell(_plain_cell("A")))
    for ch in missing_capital_accents(font0, face):
        base, acc = _decompose(ch)
        donor = f.cell(BLOCK_CELL + _xx_of(_DONOR[acc], face))
        dst_xx = _xx_of(ch, face)
        dst = f.cell(BLOCK_CELL + dst_xx)
        top = _top_row(f.cell(_plain_cell(base)))
        # half-pixel ties go right: font1's own Ê sits one column right of its Â
        dx = int(_centre(dst, top) - _centre(donor, plain_a_top) + 0.5 + 64) - 64
        # the accent sits the same distance above the cap line as on À
        dy = top - plain_a_top
        for r in range(plain_a_top):
            for c in range(fontmod.CELL_W):
                v = donor[r][c]
                if v and 0 <= c + dx < fontmod.CELL_W and 0 <= r + dy < fontmod.CELL_H:
                    dst[r + dy][c + dx] = max(dst[r + dy][c + dx], v)
        f.set_cell(BLOCK_CELL + dst_xx, dst)
        ink = f.ink_bounds(BLOCK_CELL + dst_xx)
        slot = fontmod.prop_index(block_code(dst_xx))
        left, right = f.metrics[slot]
        if ink and left and (ink[0] < left or ink[1] > right):
            f.set_metrics(slot, max(1, min(left, ink[0])), max(right, ink[1]))
    return f


# -- cells with no metrics -------------------------------------------------------------
# xglFontGetProportionalSize reads a slot's (left, right) as
# ``(u16)(entry + 0xff)``: a stored left of 0 comes out as low byte 0xff and
# is replaced by ``default_width << 8``, and xglFontPrint then draws only
# default_width + 1 columns of the cell (default_width is 4 after
# xglFontInitial and every 0x0B reset). Retail stores (0, 19) for — ‾ and _,
# so a typed em dash shows as a 5 px stub. A stored (l, r) with l >= 1 draws
# columns l-1 .. r+1 and advances r - l + 3 (font.advance).

def _drawn_slot(ch: str, face: str) -> int:
    """Proportional slot the field printer draws ``ch`` from, or -1."""
    b = _canon(ch, FIELD, face)
    if not b:
        return -1
    code = ascii_to_code(b[0]) if len(b) == 1 else (b[0] << 8) | b[1] if len(b) == 2 else 0
    return fontmod.prop_index(code) if code else -1


def unmeasured_chars(font: Font, face: str) -> Dict[str, int]:
    """{char: slot} for typed characters whose cell has ink but whose slot
    has the no-metrics left of 0 (retail: — ‾ _)."""
    out: Dict[str, int] = {}
    chars = set(BLOCK_GLYPHS[face].values()) | {chr(c) for c in range(0x21, 0x7f)}
    for ch in sorted(chars):
        slot = _drawn_slot(ch, face)
        if 0 <= slot < len(font.metrics) and font.metrics[slot][0] == 0 \
                and font.ink_bounds(fontmod.cell_of_prop(slot)) is not None:
            out[ch] = slot
    return out


def fix_unmeasured(font: Font, face: str, chars=None) -> Font:
    """Copy of the font where the slots of ``chars`` (default: all of
    unmeasured_chars) get metrics that draw their whole ink and advance by
    its width (retail — : (1, 18), drawn columns 0-19, advance 20)."""
    f = _copy_font(font)
    for ch, slot in unmeasured_chars(font, face).items():
        if chars is not None and ch not in chars:
            continue
        lo, hi = f.ink_bounds(fontmod.cell_of_prop(slot))
        f.set_metrics(slot, lo + 1, max(lo + 1, hi - 1))
    return f


def needs_metric_fix(text: str, font: Font, face: str) -> List[str]:
    """Characters of ``text`` that need fix_unmeasured to draw in full."""
    um = unmeasured_chars(font, face)
    return sorted({c for c in text if c in um})


# -- previews ------------------------------------------------------------------------

def _strip_png(font: Font, cells: Sequence[int], scale: int = 4) -> bytes:
    gap = 2
    W = len(cells) * (fontmod.CELL_W + gap) + gap
    H = fontmod.CELL_H + 2 * gap
    img = bytearray(W * H)
    for x in range(W):
        img[x] = img[(H - 1) * W + x] = 1
    for k, c in enumerate(cells):
        x0 = gap + k * (fontmod.CELL_W + gap)
        for y in range(H):
            img[y * W + x0 - 1] = 1
        bm = font.cell(c)
        for y in range(fontmod.CELL_H):
            img[(gap + y) * W + x0:(gap + y) * W + x0 + fontmod.CELL_W] = bytes(bm[y])
    big = bytearray()
    for y in range(H):
        row = b"".join(bytes([v]) * scale for v in img[y * W:(y + 1) * W])
        big += row * scale
    return fontmod.sheet_to_png(big, W * scale, H * scale)


def write_previews(iso_path: str, out_dir: str) -> List[str]:
    """Accent-block rows of both faces, font0 E row before/after the fix."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    fonts = load_fonts(iso_path)
    written = []
    for name, f in fonts.items():
        for r in range(0, 0xC0, 0x10):
            p = out / f"{name}_block_{r:02X}.png"
            p.write_bytes(_strip_png(f, [BLOCK_CELL + x for x in range(r, r + 16)]))
            written.append(str(p))
    for name in (FONT0, FONT1):
        before = fonts[name]
        after = fix_capital_accents(before, name)
        cells = [BLOCK_CELL + x for x in range(0x00, 0x20)]
        for tag, f in (("before", before), ("after", after)):
            p = out / f"{name}_capitals_{tag}.png"
            p.write_bytes(_strip_png(f, cells))
            written.append(str(p))
    return written


# -- selftest ------------------------------------------------------------------------

def _slant(rows) -> Optional[int]:
    """+1 if an accent's ink runs up to the right (acute), -1 up-left (grave)."""
    pts = [(r, c) for r, row in enumerate(rows) for c, v in enumerate(row) if v >= 2]
    if len(pts) < 2:
        return None
    rt, rb = min(r for r, _ in pts), max(r for r, _ in pts)
    if rt == rb:
        return None
    ct = sum(c for r, c in pts if r == rt) / sum(1 for r, _ in pts if r == rt)
    cb = sum(c for r, c in pts if r == rb) / sum(1 for r, _ in pts if r == rb)
    return 1 if ct > cb else -1 if ct < cb else None


def _unalias(text: str) -> str:
    for a, b in ALIASES.items():
        text = text.replace(a, b)
    return text


def _roundtrip(raw: bytes, ctl: str, face: str, stats: Dict[str, int], bad: List[str], label: str):
    disp = to_display(raw, ctl, FIELD, face)
    try:
        back, _ = from_display(disp, FIELD, ctl, face)
    except TxEncodeError as e:
        stats["fail"] += 1
        bad.append(f"{label}: encode error {e}")
        return
    stats["n"] += 1
    if back == raw:
        stats["exact"] += 1
    elif b"#" in raw and to_display(back, ctl, FIELD, face) == _unalias(disp):
        stats["escape"] += 1
    else:
        stats["fail"] += 1
        bad.append(f"{label}: {raw[:60]!r} -> {back[:60]!r}")
    stats["tokens"] += disp.count("⟦")


def selftest(iso_path: str) -> int:
    fails = 0

    def expect(cond: bool, what: str):
        nonlocal fails
        if not cond:
            fails += 1
            print(f"  FAIL {what}")

    fonts = load_fonts(iso_path)
    # 1. formula and the cells the maps point at
    expect(block_code(0x2B) == 0xD0EC and fontmod.glyph_cell(0xD0EC) == BLOCK_CELL + 0x2B,
           "#2B = D0EC = cell 3819")
    fixed_pitch: List[str] = []
    for face, m in FACE_MAPS.items():
        f = fonts[face]
        for ch, code in m.items():
            cell = fontmod.glyph_cell(code)
            expect(f.ink_bounds(cell) is not None, f"{face} {ch!r} cell {cell} has ink")
            if f.metrics[fontmod.prop_index(code)][0] == 0:
                fixed_pitch.append(f"{face}:{ch}")
    print(f"mapped cells with no metrics (a 5 px stub until fix_unmeasured): {' '.join(fixed_pitch) or 'none'}")
    # 2. accents: presence, and grave/acute slant read from the pixels
    for face in (FONT0, FONT1):
        f = fonts[face]
        miss = missing_capital_accents(f, face)
        print(f"{face}: capitals missing their accent: {''.join(miss) or 'none'}")
        expect(miss == (list(FONT0_UNACCENTED) if face == FONT0 else []),
               f"{face} missing accents as transcribed")
        fixed = fix_capital_accents(f, face)
        expect(missing_capital_accents(fixed, face) == [], f"{face} fixed has all accents")
        checked = 0
        for ch in "ÀÁÈÉÌÍÒÓÙÚàáèéìíòóùú":
            ar = accent_rows(fixed, ch, face)
            if not ar:
                continue
            want = 1 if _decompose(ch)[1] == "acute" else -1
            expect(_slant(ar[0]) == want, f"{face} {ch!r} accent slant")
            checked += 1
        for ch, xx in _SHARED.items():
            ar = fixed.cell(BLOCK_CELL + xx)[:_top_row(fixed.cell(_plain_cell("e")))]
            if ch == "é":
                expect(_slant(ar) == 1, f"{face} shared é slant")
        print(f"{face}: grave/acute slant verified on {checked} letters")
    # 3. French + Spanish
    sample = ("àâäçéèêëîïôöùûüÿœæ ÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŸŒÆ ñÑ áíóúÁÍÓÚ ¿¡ « » ‘ ’ “ ” … — – € º ª")
    for face in (FONT0, FONT1):
        data, w = from_display(sample, FIELD, "evt", face)
        expect(b"\x8f" not in data, f"{face} sample has no 0x8F")
        expect(to_display(data, "evt", FIELD, face) == sample.replace("’", "'").replace("”", '"'),
               f"{face} sample round trip")
        print(f"{face}: sample {len(sample)} chars -> {len(data)} bytes, "
              f"{fonts[face].width_of_bytes(data)} px")
    try:
        from_display("été 😀", FIELD, "c0")
        expect(False, "emoji rejected")
    except TxEncodeError as e:
        expect([p for p, _, _ in e.problems] == [4], "emoji error position")
    try:
        from_display("ā", FIELD, "c0")          # ā: JIS X 0212 only
        expect(False, "0x8F guard")
    except TxEncodeError as e:
        expect("0212" in e.problems[0][2], "0x8F guard message")
    d, _ = from_display("#12 /[#0]", FIELD, "evt")
    expect(d == b"\xd1\xe512 /[#0]", "literal # becomes D1E5, tag # stays")
    expect(to_display(b"D#23mmerung", "evt") == "Dämmerung", "retail #23 shows ä")
    expect(to_display(b"Card Pack #821", "evt") == "Card Pack #1", "retail #82 shows #")
    expect(from_display("café", FIELD, "c0", FONT0)[0] == from_display("café", FIELD, "c0", FONT1)[0],
           "é is face independent")
    f1 = fonts[FONT1]
    w_esc = f1.width_of_bytes(b"D#23mmerung")
    w_raw = f1.width_of_bytes(from_display("Dämmerung")[0])
    print(f"width font1: 'D#23mmerung' {w_esc} px as escape text, {w_raw} px as raw code")
    # battle / ascii
    b, w = from_display("Épée à feu!", BATTLE, "c0")
    expect(b == b"#47p#64e a feu#8F", "battle encoding")
    expect(any("'à'" in x for x in w), "battle à warning")
    expect(to_display(b, "c0", BATTLE) == "Épée a feu!", "battle round trip")
    b, w = from_display("Œuvre çà", ASCII, "c0")
    expect(b == b"OEuvre ca" and len(w) == 3, "tag font fold")
    expect(to_display(b"\x01Current", "c0", ASCII) == "⟦01⟧Current", "tag 0x01 has no operand")
    expect(to_display(b"List\x03\x99\x033Num", "c0", ASCII) == "List⟦03⟧⟦99⟧⟦03⟧⟦33⟧Num",
           "tag 0x03 takes one operand")
    expect(from_display("⟦01⟧Argent", ASCII, "c0")[0] == b"\x01Argent", "tag first letter free")
    for bad_text, bad_pos in (("Pts «x»", 4), ("Niv 2 max", 4), ("a,b", 1), ("a\nb", 1)):
        try:
            from_display(bad_text, ASCII, "c0")
            expect(False, f"tag font rejects {bad_text!r}")
        except TxEncodeError as e:
            expect(e.problems[0][0] == bad_pos, f"tag font error position {bad_text!r}")
    expect(tag_render(b"\x01Lv,x") == "Lvvx" and tag_width(b"\x01Ab 9z") == 10 + 8 + 8 + 11,
           "tag render/width follow PrintTagFont")
    expect(check("⟦01⟧Argent", "⟦01⟧Money", ASCII, "c0")["ok"], "tag check: letters free")
    r = check("Ça va ⟦0C⟧", "Hello ⟦0C⟧⟦54⟧", FIELD, "c0")
    expect(not r["ok"] and any("tok" in e for e in r["errors"]), "checker token mismatch")
    r = check("Bonjour /[$0] !", "Hello /[$0]!", FIELD, "evt", measure=f1, width_px=40)
    expect(r["ok"] and r["px"] and any("px wide" in x for x in r["warnings"]), "checker width")
    # DeepL / French-keyboard spacing and decomposed accents
    typed = "Pourquoi\u202f? C\u2019est \u00ab\u00a0incroyable\u00a0\u00bb\u00a0! Ce\u0301cile l\u00e0\u00ad"
    plain = "Pourquoi ? C\u2019est \u00ab incroyable \u00bb ! C\u00e9cile l\u00e0"
    for fnt in (FIELD, BATTLE):
        r = check(typed, "x", fnt, "evt", face=FONT1)
        expect(r["ok"] and any("NO-BREAK SPACE" in x for x in r["warnings"])
               and any("SOFT HYPHEN" in x for x in r["warnings"]), f"{fnt} folds NBSP/NNBSP/SHY")
        expect(from_display(typed, fnt, "evt")[0] == from_display(plain, fnt, "evt")[0],
               f"{fnt} typed == plain bytes")
    r = check("a\u2028b", "x", FIELD, "c0")
    expect(not r["ok"] and "U+2028" in r["errors"][0], "invisible error names the character")
    # new-script glyph codes (txtarget): refused outside a build, exact bytes inside
    g2, g1 = glyph_char(0xD2A5), glyph_char(0x21)
    try:
        from_display("a" + g2, FIELD, "c0")
        expect(False, "glyph char refused outside glyph_codes()")
    except TxEncodeError as e:
        expect(e.problems[0][0] == 1, "glyph char error position")
    with glyph_codes():
        expect(from_display("a" + g2 + "é", FIELD, "evt")[0] == b"a\xd2\xa5" +
               from_display("é", FIELD, "evt")[0], "field glyph char -> two bytes")
        expect(from_display("K" + g1, BATTLE, "c0")[0] == b"K!", "battle glyph char -> one byte")
        expect(not check(g2 + " x", "Hello", FIELD, "c0")["errors"], "check accepts glyph chars")
        try:
            from_display(g1, FIELD, "c0")
            expect(False, "single-byte glyph code refused in the field font")
        except TxEncodeError:
            pass
    expect(draws_natively("é") and draws_natively("漢") and not draws_natively("鬱")
           and not draws_natively("ł") and draws_natively("ó", BATTLE) and not draws_natively("é", ASCII),
           "draws_natively")
    # cells whose retail metrics are the no-metrics (0, 19): — draws in full once fixed
    for face in (FONT0, FONT1):
        um = unmeasured_chars(fonts[face], face)
        expect(set(um) == set("—‾_"), f"{face} unmeasured — ‾ _ ({''.join(sorted(um))})")
        fx = fix_unmeasured(fonts[face], face, needs_metric_fix("a — b", fonts[face], face))
        slot = um["—"]
        lo, hi = fx.metrics[slot]
        expect(lo >= 1 and (lo - 1, hi + 1) == fonts[face].ink_bounds(fontmod.cell_of_prop(slot)),
               f"{face} — drawn over its ink")
        expect(fx.metrics[um["_"]] == fonts[face].metrics[um["_"]], f"{face} _ untouched")
        expect(len(fx.to_bytes()) == len(fonts[face].to_bytes()), f"{face} size kept")
    # 4. the whole disc
    import evttext
    import ovltext
    import textpack
    from iso9660 import IsoImage
    iso = IsoImage(iso_path)
    report = []
    try:
        st = {"n": 0, "exact": 0, "escape": 0, "fail": 0, "tokens": 0}
        bad: List[str] = []
        for chain, path, data, _ in evttext.iter_containers(iso):
            face = evt_face(chain, path)
            for ri, pi, raw, _name in evttext.strings_of(data):
                body = raw[:-1] if raw.endswith(b"\x00") else raw
                _roundtrip(body, "evt", face, st, bad, f"{path} @{ri}.{pi}")
        report.append(("evt", st, bad))
        st = {"n": 0, "exact": 0, "escape": 0, "fail": 0, "tokens": 0}
        bad = []
        for name in ovltext.executables(iso):
            ov = ovltext.Overlay(name, bytes(iso.read_file(name)))
            for va, s in ov.strings.items():
                _roundtrip(s.raw, "c0", FONT1, st, bad, f"{name} @{va:x}")
        report.append(("ovl", st, bad))
        st = {"n": 0, "exact": 0, "escape": 0, "fail": 0, "tokens": 0}
        bad = []
        for chain in textpack._chains(iso):
            for reader, e, _alloc in textpack._entries_with_alloc(iso, chain):
                if e.ext not in ("txt", "uml") or e.compressed:
                    continue
                data = b"".join(reader.read_iter(e.byte_offset, e.size))
                body = textpack.uml_split(data)[0] if e.ext == "uml" else data
                low = e.path.lower().replace("/", "\\")
                ctl = "chat" if low.startswith("umn\\event") and e.ext == "txt" else "c0"
                _roundtrip(body, ctl, FONT1, st, bad, e.path)
        report.append(("umn/txt", st, bad))
    finally:
        iso.close()
    for fam, st, bad in report:
        print(f"round trip {fam}: {st['n']} strings, {st['exact']} byte-exact, "
              f"{st['escape']} differ only by retail #XX escapes, {st['fail']} failed, "
              f"{st['tokens']} ⟦XX⟧ tokens shown")
        for x in bad[:8]:
            print(f"    {x}")
        fails += st["fail"]
    print("selftest " + ("OK" if not fails else f"FAILED ({fails})"))
    return 0 if not fails else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("selftest")
    ps.add_argument("--iso", required=True)
    pv = sub.add_parser("show")
    pv.add_argument("--iso", required=True)
    pv.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "show":
        for p in write_previews(a.iso, a.out):
            print(p)
        return 0
    return selftest(a.iso)


if __name__ == "__main__":
    sys.exit(main())
