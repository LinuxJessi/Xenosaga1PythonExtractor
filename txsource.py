"""txsource.py — show the Japanese original next to each USA string.

The translation editor's rows are keyed to the USA disc (SLUS_204.69). A
translator who reads Japanese is better served by the line the writers
wrote, so this module finds, for every USA catalog row, the matching string
on the one Japanese Episode I disc that is around: **Xenosaga Episode I
Reloaded** (SLPS_290.05, 2003), a stripped rebuild of the USA code whose
text was put back into Japanese (EUC-JP). Nothing is ever written to either
disc; the result is a ``{row id: Japanese text}`` map the editor shows as the
"original".

Public API (all read-only; callable from txserver / txcatalog)::

    sources(iso_path_usa) -> [{"id": "en-usa", "label": "English (USA disc)"},
                              {"id": "ja-reloaded",
                               "label": "Japanese (Xenosaga I Reloaded disc)",
                               "needs": "path to the Reloaded ISO"}]
    identify_disc(path) -> "usa" | "reloaded" | "jp-original" | None
    align(usa_iso, source_iso, rows=None, log=print, use_cache=True) -> {
        "source": "ja-reloaded",               # or "en-usa" when source_iso is
                                               # the USA disc (texts = row src)
        "texts": {row_id: display text},       # only rows that align; never
                                               # blank or whitespace-only
        "partial": [row_id, ...],              # text is the whole Japanese message
                                               # the row is one piece of
        "stats": {family: {"rows": n, "aligned": m, "unaligned": k,
                           "visible": v, "visible_aligned": w}},
        "by_cat": {category: same five counts},
        "detail": {family: {aligner counter: n}},
        "notes": [str, ...]}
    display_ja(raw, ctl="c0", font="field") -> str   # EUC-JP bytes -> display text
    disc_id(path) -> sha1 hex                        # identity used in the cache key
    class SourceError(ValueError)

``family`` is the catalog's ``fam`` (evt / umn / fix / ovl); "visible" counts
rows without a ``hide`` reason. ``align`` raises ``SourceError`` with a
readable message when a disc is not supported: the USA argument is not
SLUS_204.69, the source is another game or unreadable, or the source is the
2002 original Japanese disc ("recognised but not supported yet"). ``rows`` is
the USA catalog row list (``txcatalog.build_catalog(usa)["rows"]``); when
omitted it is built (cached). The display text follows the catalog's display
form (txcodec): Japanese characters as Unicode, control codes as ⟦XX⟧, evt
``/[...]`` tags literal, speaker rows hold just the name. The result is cached
in ``~/.cache/xs1-txeditor/source-<hash>.json``; the hash covers both discs'
identity (``disc_id``), the code of this module and of every reader it uses,
and the row list. A fresh run takes about 12-40 s (both discs read once),
a cached one well under a second plus ~0.2 s per disc for ``disc_id``.

How each family is aligned (and why that is safe):

* **evt** (field maps, cutscenes, speaker names, script menus). Reloaded has
  the same 569 containers with the same classes (paired by FL00 row name;
  one container gained a stub row) and the same methods in the same order
  (checked per class, not assumed). The constant pool order differs (it is
  alphabetical by content), so strings are matched by *code position*: each
  method is turned into a token stream (opcodes with their member / class
  operands, every ``ldc`` String as a placeholder), and the n-th String load
  of a USA method is paired with the n-th of the Reloaded method when the
  streams are identical (26,116 of 26,190 methods). Field messages are
  ``String[]`` arrays (``[label, text, waitkey]``) and Reloaded often splits
  one USA text element into one element per line, so arrays are compared as
  units (``_array_pairs``): elements are classed as speaker label,
  control-only (tags) or text, consecutive text elements form a group, and
  groups pair in order when both arrays have the same shape. A USA text that
  is one element gets the concatenation of its Japanese group. Where the
  sizes differ, where the two scripts break the message into pages
  differently, or where the shapes agree but the pages' English/Japanese
  length ratios differ more than 6x (the text sits on different pages:
  ST1060 "It says," | quote against quote | "...that's what it says"),
  every USA text element of the message gets the whole Japanese
  message and is listed in ``partial``. Methods whose streams differ are
  aligned with a longest-common-subsequence match of the token streams; only
  String loads inside identical runs are paired.
* **umn**. Mail pages (same numbers on both discs): Reloaded writes
  ``件名：`` / ``差出人：`` instead of ``Subject:`` / ``Sender:``; parsed the
  same way. Chats: the ``<msg>`` blocks of the same ``eventNN.txt`` pair by
  index when the sequence of ``<char>`` speakers agrees (all 19 files do).
  Database pages are numbered in each language's alphabetical order, so file
  numbers do NOT line up; ``_db_pairs`` pairs them inside the header's group
  key by evidence only (numbers in the text, capital-letter acronyms, last
  page of a group) and leaves the rest without Japanese.
* **fix**: same files, but four of the seven tables have another layout on
  Reloaded (checked file by file in ``fix_map``): key items and plug-ins pad
  the name to a fixed column, the junk-part blocks are stored in another
  order with space padding, card texts are not packed. Enemy battle quotes
  pair by index inside the same think file when the PRINT / MSG sequence
  agrees (all 100 files). ``endou\savemap.bin`` is not on Reloaded.
* **ovl** (SLUS / OV*.OVL). Reloaded's executables have no symbols. Each
  USA function (USA symbol table) is found in the Reloaded file by its
  masked instruction words (immediates and jump targets masked) at a likely
  function start; every ``lui``/``addiu`` pair that forms a USA string
  address is then read at the same offset in the Reloaded function. Data
  tables (``itmNameTbl`` ...): the Reloaded address of each USA object that
  holds string pointers is its USA address plus a displacement. Candidate
  displacements come from the code addressing the object and from the
  displacements already found in the file (or, when nothing places any
  table, a scan of +-32 KiB for the largest objects); a candidate must keep
  the object's layout (every non-pointer word equal, every string pointer
  facing a pointer to a string start) and be clearly the best at pairing
  strings with the same line breaks, the same numbers and correlated
  lengths, against the other candidates and their shifts by up to four
  words (``_pick_delta``). Small objects are placed only between two
  anchors with the same displacement. A row is aligned only when every
  reference that resolves agrees.

Japanese text is decoded by ``display_ja``: JIS X 0208 pairs pass through
as Unicode (``txcodec.to_display`` would read rows 0xD0+ as the USA accent
block, but on Reloaded they are level-2 kanji), everything else (control
codes, ``/[...]`` tags, ASCII) goes through ``txcodec.to_display``.

Quality checks the selftest runs on the result: the control tags that drive
the message window (``waitkey``, ``close``, ``clear``, ``label``, ``$n``,
``#n``) must agree between USA and Japanese, and a Japanese line must not be
paired with a USA debug print and vice versa; counts are reported, and the
selftest fails above a small threshold.

The 2002 original Japanese disc (SLPS_250.xx) is recognised by
``identify_disc`` but ``align`` refuses it with "not supported yet". Design
for adding it: its code predates the USA build (Reloaded is the USA code
rebuilt), so the same steps apply with lower coverage expected: evt by code
position (class / method lists must be checked per container), ovl by masked
function match against the USA symbols (the original ELF is also stripped),
umn / fix by file + record. It becomes a third entry of ``sources()`` with id
``ja-original`` once a disc has been checked.

    python3 txsource.py selftest --usa USA.iso --source RELOADED.iso
    python3 txsource.py align --usa USA.iso --source RELOADED.iso --out ja.json
    python3 txsource.py identify DISC.iso
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import txcodec as tc                                   # noqa: E402
from iso9660 import IsoImage                            # noqa: E402

VERSION = 2
CACHE_DIR = Path.home() / ".cache" / "xs1-txeditor"

SRC_EN = "en-usa"
SRC_JA = "ja-reloaded"
SRC_JA_ORIGINAL = "ja-original"          # recognised, not supported yet

BOOT_USA = "SLUS_204.69"
BOOT_RELOADED = "SLPS_290.05"
_BOOT_JP_ORIGINAL = re.compile(r"^SLPS_250\.\d\d$", re.I)

FAMILIES = ("evt", "umn", "fix", "ovl")


class SourceError(ValueError):
    """The source disc cannot be used (wrong disc, unsupported, unreadable)."""


def _log_null(msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# discs
# ---------------------------------------------------------------------------

def identify_disc(path: str) -> Optional[str]:
    """"usa" | "reloaded" | "jp-original" | None, from the boot executable
    named in SYSTEM.CNF (plus XENOSAGA.00 and OV01.OVL, so another game with
    a similar serial is not taken for Episode I). Never raises for a bad
    path; returns None."""
    try:
        iso = IsoImage(path)
    except Exception:
        return None
    try:
        boot = (iso.boot_elf_name() or "").upper()
        files = {n.upper() for n in iso.files}
    except Exception:
        return None
    finally:
        try:
            iso.close()
        except Exception:
            pass
    if "XENOSAGA.00" not in files or "OV01.OVL" not in files:
        return None
    if boot == BOOT_USA:
        return "usa"
    if boot == BOOT_RELOADED:
        return "reloaded"
    if _BOOT_JP_ORIGINAL.match(boot):
        return "jp-original"
    return None


def sources(iso_path_usa: Optional[str] = None) -> List[dict]:
    """Source-language options for the editor. ``iso_path_usa`` is accepted
    for symmetry (the English source is the USA disc itself) and not read."""
    return [{"id": SRC_EN, "label": "English (USA disc)"},
            {"id": SRC_JA, "label": "Japanese (Xenosaga I Reloaded disc)",
             "needs": "path to the Reloaded ISO"}]


def disc_id(path: str) -> str:
    """sha1 of the boot ELF, every OV*.OVL and the three TOC files (the
    same object set as ``txproject.retail_id``, for any Episode I disc)."""
    iso = IsoImage(path)
    try:
        boot = iso.boot_elf_name()
        if not boot or boot not in iso.files:
            raise SourceError(f"{path}: no boot executable on the disc")
        names = [boot] + sorted(n for n in iso.files if n.upper().endswith(".OVL")) + \
            [n for n in ("XENOSAGA.00", "XENOSAGA.10", "XENOSAGA.20") if n in iso.files]
        h = hashlib.sha1()
        for n in names:
            h.update(n.encode() + b"\0")
            h.update(bytes(iso.read_file(n)))
        return h.hexdigest()
    finally:
        iso.close()


# ---------------------------------------------------------------------------
# Japanese display form
# ---------------------------------------------------------------------------

def display_ja(raw: bytes, ctl: str = "c0", font: str = "field") -> str:
    """EUC-JP engine bytes -> display text. Two-byte JIS X 0208 characters
    (and 0x8E half-width kana) are decoded as Unicode; runs of everything
    else go through ``txcodec.to_display`` with the row's ``ctl`` and
    ``font`` (face font1), so control codes come out as the usual ⟦XX⟧
    tokens and ``/[...]`` tags stay."""
    out: List[str] = []
    run = bytearray()
    i, n = 0, len(raw)

    def flush():
        if run:
            out.append(tc.to_display(bytes(run), ctl, font, tc.FONT1))
            run.clear()

    while i < n:
        b = raw[i]
        if 0xA1 <= b <= 0xFE and i + 1 < n and 0xA1 <= raw[i + 1] <= 0xFE:
            try:
                ch = raw[i:i + 2].decode("euc_jp")
            except UnicodeDecodeError:
                ch = None
            if ch is not None:
                flush()
                out.append(ch)
                i += 2
                continue
        if b == 0x8E and i + 1 < n and 0xA1 <= raw[i + 1] <= 0xDF:
            flush()
            out.append(raw[i:i + 2].decode("euc_jp", "replace"))
            i += 2
            continue
        if b < 0x80:
            run.append(b)
            i += 1
            continue
        # a lone high byte: let the codec show it as ⟦XX⟧
        run.append(b)
        i += 1
    flush()
    return "".join(out)


# ---------------------------------------------------------------------------
# evt: bytecode position
# ---------------------------------------------------------------------------

def _u16(b: bytes, o: int) -> int:
    return struct.unpack_from(">H", b, o)[0]


def _u32(b: bytes, o: int) -> int:
    return struct.unpack_from(">I", b, o)[0]


def _utf(ent, i: int) -> bytes:
    e = ent[i] if 0 < i < len(ent) else None
    return e[1].rstrip(b"\0") if e and e[0] == 1 else b""


def _cref(ent, i: int) -> tuple:
    """Constant-pool operand in a disc-independent form."""
    e = ent[i] if 0 < i < len(ent) else None
    if not e:
        return ("?",)
    tag = e[0]
    if tag == 8:
        return ("S",)
    if tag in (3, 4, 5, 6):
        return ("K", tag, e[1])
    if tag == 7:
        return ("C", _utf(ent, _u16(e[1], 0)))
    if tag in (9, 10, 11):
        ci, nt = struct.unpack(">HH", e[1])
        cn = _utf(ent, _u16(ent[ci][1], 0))
        ni, di = struct.unpack(">HH", ent[nt][1])
        return ("M", cn, _utf(ent, ni), _utf(ent, di))
    return ("?", tag)


_CPOOL_OPS = {0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xB9, 0xBB, 0xBD, 0xC0, 0xC1, 0xC5}
_ONE_BYTE_OPERAND = {0x10, 0x15, 0x16, 0x17, 0x18, 0x19, 0x36, 0x37, 0x38, 0x39, 0x3A, 0xBC}
_INT_PUSH = set(range(0x02, 0x09)) | {0x10, 0x11}       # iconst_m1..5, bipush, sipush
_DUP, _AASTORE, _ANEWARRAY = 0x59, 0x53, 0xBD
_STR_CLASS = ("C", b"java/lang/String")


def _class_methods(cls: bytes):
    """[(name, desc, tokens)] per method. A token is a tuple; a String load
    is ("str", utf8 pool index)."""
    import evttext as et
    from txfam_evt import _OPLEN
    ent, p = et.parse_pool(cls)
    p += 6
    p += 2 + 2 * _u16(cls, p)

    def attrs(q, on=None):
        n = _u16(cls, q)
        q += 2
        for _ in range(n):
            an = _utf(ent, _u16(cls, q))
            al = _u32(cls, q + 2)
            if an == b"Code" and on is not None:
                body = q + 6
                on(cls[body + 8:body + 8 + _u32(cls, body + 4)])
            q += 6 + al
        return q

    nf = _u16(cls, p)
    p += 2
    for _ in range(nf):
        p = attrs(p + 6)
    nm = _u16(cls, p)
    p += 2
    out = []
    for _ in range(nm):
        name, desc = _utf(ent, _u16(cls, p + 2)), _utf(ent, _u16(cls, p + 4))
        got: List[bytes] = []
        p = attrs(p + 6, got.append)
        toks: List[tuple] = []
        if got:
            code = got[0]
            pc, n = 0, len(code)
            while pc < n:
                op = code[pc]
                if op in (0x12, 0x13, 0x14):
                    idx = code[pc + 1] if op == 0x12 else _u16(code, pc + 1)
                    r = _cref(ent, idx)
                    if r[0] == "S":
                        toks.append(("str", _u16(ent[idx][1], 0)))
                    else:
                        toks.append(("ldc",) + r)
                elif op in _CPOOL_OPS:
                    toks.append((op,) + _cref(ent, _u16(code, pc + 1)))
                elif op in _ONE_BYTE_OPERAND:
                    toks.append((op, code[pc + 1]))
                elif op == 0x11:
                    toks.append((op, _u16(code, pc + 1)))
                elif op == 0x84:
                    toks.append((op, code[pc + 1], code[pc + 2]))
                else:
                    toks.append((op,))
                if op == 0xAA:
                    q = (pc + 4) & ~3
                    lo, hi = struct.unpack_from(">ii", code, q + 4)
                    pc = q + 12 + 4 * (hi - lo + 1)
                    continue
                if op == 0xAB:
                    q = (pc + 4) & ~3
                    pc = q + 8 + 8 * struct.unpack_from(">i", code, q + 4)[0]
                    continue
                if op == 0xC4:
                    pc += 6 if code[pc + 1] == 0x84 else 4
                    continue
                pc += _OPLEN[op]
        out.append((name, desc, toks))
    return ent, out


def _fold_arrays(toks: List[tuple]) -> List[tuple]:
    """Replace each ``new String[n] {ldc, ldc, ...}`` construction by one
    ("arr", (elem, ...)) item; elem = utf8 pool index, or None for an element
    that is not a String literal. The size push is dropped (Reloaded's arrays
    often have more elements). Anything unusual leaves the tokens as they are."""
    out: List[tuple] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t[0] == _ANEWARRAY and t[1:] == _STR_CLASS and out and out[-1][0] in _INT_PUSH:
            j = i + 1
            elems: List[Optional[int]] = []
            ok = True
            while j + 1 < n and toks[j][0] == _DUP and toks[j + 1][0] in _INT_PUSH:
                k = j + 2
                val = []
                while k < n and toks[k][0] != _AASTORE:
                    if toks[k][0] in (_ANEWARRAY, _DUP) or len(val) > 6:
                        ok = False
                        break
                    val.append(toks[k])
                    k += 1
                if not ok or k >= n:
                    ok = False
                    break
                elems.append(val[0][1] if len(val) == 1 and val[0][0] == "str" else None)
                j = k + 1
            if ok and elems:
                out.pop()                         # the size push
                out.append(("arr", tuple(elems)))
                i = j
                continue
        out.append(t)
        i += 1
    return out


def _key(t: tuple) -> tuple:
    if t[0] == "str":
        return ("str",)
    if t[0] == "arr":
        return ("arr",)
    return t


_TAG_RE = re.compile(rb"/\[[^\]]*\]")


def _elem_kind(raw: bytes) -> str:
    body = raw.rstrip(b"\0")
    if body.startswith(b"/[label("):
        return "label"
    if not body.strip():
        return "text"                     # whitespace: part of the text around it
    if not _TAG_RE.sub(b"", body).strip():
        return "ctl"
    return "text"


def _array_pairs(ue: Sequence[Optional[int]], re_: Sequence[Optional[int]],
                 uent, rent) -> Optional[List[Tuple[int, List[int], bool]]]:
    """Pair the elements of a USA and a Reloaded String[]: [(usa pool,
    [reloaded pools], partial)], or None when they cannot be paired.

    Elements are speaker labels, control-only elements (tags) and text;
    whitespace-only elements count as text. Consecutive text elements form a
    group. Same shape, when _pagewise_ok finds the page split plausible:
    groups pair in order (one-to-one when the sizes agree, a single USA
    element takes the whole Japanese group). Otherwise, or with a different
    shape but the same
    speaker labels (the two scripts break the message into pages differently,
    ``/[waitkey(1)]/[clear()]``): labels pair in order and every USA text
    element gets the whole Japanese message (its text groups joined by a line
    break), ``partial``."""
    def shape(elems, ent):
        segs: List[Tuple[str, List[int]]] = []
        for e in elems:
            if e is None:
                segs.append(("expr", []))
                continue
            k = _elem_kind(_utf_raw(ent, e))
            if k == "text":
                if segs and segs[-1][0] == "text":
                    segs[-1][1].append(e)
                else:
                    segs.append(("text", [e]))
            else:
                segs.append((k, [e]))
        return segs

    su, sr = shape(ue, uent), shape(re_, rent)
    out: List[Tuple[int, List[int], bool]] = []
    if [k for k, _ in su] == [k for k, _ in sr] and _pagewise_ok(su, sr, uent, rent):
        for (k, a), (_, b) in zip(su, sr):
            if k == "expr":
                continue
            if k != "text" or len(a) == len(b):
                out += [(x, [y], False) for x, y in zip(a, b)]
            else:                               # len(a) == 1 (_pagewise_ok)
                out.append((a[0], b, False))
        return out
    lu = [a[0] for k, a in su if k == "label"]
    lr = [b[0] for k, b in sr if k == "label"]
    if len(lu) != len(lr) or len(lu) > 1 or any(k == "expr" for k, _ in su + sr):
        return None
    tu = [x for k, a in su if k == "text" for x in a]
    groups = [b for k, b in sr if k == "text"]
    if not tu or not groups:
        return None
    whole: List[int] = []
    for g in groups:
        if whole:
            whole.append(-1)                    # line break between pages
        whole += g
    out += [(x, [y], False) for x, y in zip(lu, lr)]
    out += [(x, whole, True) for x in tu]
    return out


# two pages whose English/Japanese length ratios differ by more than this
# are taken as the scripts putting the text on different pages
_PAGE_RATIO_MAX = 6.0


def _text_len(ent, i: int) -> int:
    return len(_TAG_RE.sub(b"", _utf_raw(ent, i).rstrip(b"\0")).strip())


def _pagewise_ok(su, sr, uent, rent) -> bool:
    """Same shape: is pairing the text groups page by page plausible? Not
    when a USA group cannot map onto its Japanese group (several USA
    elements against a different number of Japanese ones), nor when the
    groups' English/Japanese length ratios disagree strongly (ST1060: USA
    "It says," | quote, Japanese quote | "...that's what it says"): the two
    scripts split the message at different points. The caller then gives
    every USA element the whole message, ``partial``."""
    ratios = []
    for (k, a), (_, b) in zip(su, sr):
        if k != "text":
            continue
        if len(a) > 1 and len(b) != len(a):
            return False
        la = sum(_text_len(uent, x) for x in a)
        lb = sum(_text_len(rent, y) for y in b)
        if la and lb:
            ratios.append(lb / la)
    return len(ratios) < 2 or max(ratios) / min(ratios) <= _PAGE_RATIO_MAX


def _utf_raw(ent, i: int) -> bytes:
    e = ent[i] if 0 < i < len(ent) else None
    return e[1] if e and e[0] == 1 else b""


def _method_pairs(ut: List[tuple], rt: List[tuple], uent, rent, st: Counter):
    """Yield (usa pool, [reloaded pools], partial) for one method pair."""
    uf, rf = _fold_arrays(ut), _fold_arrays(rt)
    uk, rk = [_key(t) for t in uf], [_key(t) for t in rf]
    if uk == rk:
        blocks = [(0, 0, len(uk))]
        st["methods_same"] += 1
    else:
        sm = difflib.SequenceMatcher(None, uk, rk, autojunk=False)
        blocks = [b for b in sm.get_matching_blocks() if b[2]]
        st["methods_lcs"] += 1
    for a, b, size in blocks:
        for k in range(size):
            x, y = uf[a + k], rf[b + k]
            if x[0] == "str":
                yield x[1], [y[1]], False
            elif x[0] == "arr":
                pr = _array_pairs(x[1], y[1], uent, rent)
                if pr is None:
                    st["arrays_shape_differs"] += 1
                    continue
                st["arrays_paired"] += 1
                yield from pr


def evt_map(usa_iso: str, src_iso: str, log=_log_null) -> Tuple[Dict[str, Tuple[bytes, bool]], dict]:
    """{evt row id: (Japanese raw bytes, partial)} for every USA literal
    that has a code-position partner, plus counters. Ids use txfam_evt's
    scheme (``evt:chain|path|class row|utf8 pool index``); speaker labels
    map to the whole ``/[label(...)]`` literal (the caller unwraps)."""
    import evttext as et
    st: Counter = Counter()

    def load(p):
        iso = IsoImage(p)
        try:
            return {(ch, path): data for ch, path, data, _ in et.iter_containers(iso)}
        finally:
            iso.close()

    u, r = load(usa_iso), load(src_iso)
    out: Dict[str, Tuple[bytes, bool]] = {}
    conflicts = 0
    for key in sorted(u):
        if key not in r:
            st["containers_missing"] += 1
            continue
        ud, rd = u[key], r[key]
        try:
            ue, re_ = et.fl00_entries(ud), et.fl00_entries(rd)
        except et.EvtTextError:
            continue
        # classes pair by FL00 row name (one container gained a stub row on Reloaded)
        rname = {rd[no:no + nl]: k for k, (no, nl, _, _) in enumerate(re_)}
        if len(rname) != len(re_):
            st["containers_duplicate_names"] += 1
            continue
        if len(ue) != len(re_):
            st["containers_row_count_differs"] += 1
        st["containers"] += 1
        for ri, (no, nl, do, ds) in enumerate(ue):
            rj = rname.get(ud[no:no + nl])
            if rj is None:
                st["classes_missing"] += 1
                continue
            _, _, do2, ds2 = re_[rj]
            ucls, rcls = ud[do:do + ds], rd[do2:do2 + ds2]
            if ds <= 24 or ucls[:4] != et.CAFEBABE or rcls[:4] != et.CAFEBABE:
                continue
            try:
                uent, um = _class_methods(ucls)
                rent, rm = _class_methods(rcls)
            except Exception:
                st["classes_unparsed"] += 1
                continue
            if [(a, b) for a, b, _ in um] != [(a, b) for a, b, _ in rm]:
                st["classes_methods_differ"] += 1
                continue
            st["classes"] += 1
            chain, path = key
            got: Dict[int, List[Tuple[bytes, bool]]] = defaultdict(list)
            for (_, _, ut), (_, _, rt) in zip(um, rm):
                for upool, rpools, partial in _method_pairs(ut, rt, uent, rent, st):
                    raw = b"".join(b"\n" if p < 0 else _utf_raw(rent, p).rstrip(b"\0")
                                   for p in rpools)
                    got[upool].append((raw, partial))
            for upool, cands in got.items():
                first = cands[0]
                if any(c[0] != first[0] for c in cands[1:]):
                    conflicts += 1
                    # several code positions share this USA literal and the Japanese
                    # differs between them; show the most common (first on a tie)
                    cnt = Counter(c[0] for c in cands)
                    best = max(cnt.values())
                    first = next(c for c in cands if cnt[c[0]] == best)
                out[f"evt:{chain}|{path}|{ri}|{upool}"] = first
    st["literals_conflicting"] = conflicts
    log(f"evt: {st['containers']} containers, {st['classes']} classes, "
        f"{st['methods_same']} methods identical + {st['methods_lcs']} matched by LCS, "
        f"{st['arrays_paired']} arrays paired, {st['arrays_shape_differs']} shape differs; "
        f"{len(out)} literals mapped ({conflicts} with differing Japanese per use)")
    return out, dict(st)


_LABEL_RAW = re.compile(rb"^/\[label\((.*?)\)?\]$", re.S)


def _evt_texts(rows: List[dict], m: Dict[str, Tuple[bytes, bool]]):
    texts, partial = {}, []
    for r in rows:
        hit = m.get(r["id"])
        if hit is None:
            continue
        raw, part = hit
        if r["cat"] == "Speaker names" and r.get("ctx", "").startswith("field speaker label"):
            # the USA's empty label literal is one pool entry shared by many
            # positions; a name taken from one of them is not this row's
            if not r["src"].strip():
                continue
            mm = _LABEL_RAW.match(raw)
            if not mm:
                continue
            raw = mm.group(1)
        texts[r["id"]] = display_ja(raw, "evt", r.get("font") or tc.FIELD)
        if part:
            partial.append(r["id"])
    return texts, partial


# ---------------------------------------------------------------------------
# umn: same files; mail / chat / database
# ---------------------------------------------------------------------------

_JA_SUBJECT = "件名：".encode("euc_jp")
_JA_SENDER = "差出人：".encode("euc_jp")


def _ja_mail_slots(t: bytes) -> Optional[Dict[str, bytes]]:
    """Reloaded mail page -> {"subject", "sender", "body"} raw bytes, cut
    the way txfam_umn cuts a USA page (``_strip`` past control codes)."""
    import txfam_umn as fu
    ls = fu._lines(t)
    if len(ls) < 3:
        return None
    out: Dict[str, bytes] = {}
    for k, (label, part) in enumerate(((_JA_SUBJECT, "subject"), (_JA_SENDER, "sender"))):
        la, lb = ls[k]
        p = t.find(label, la, lb)
        if p != la:
            return None
        a, b = fu._strip(t, p + len(label), lb, lead_nl=False)
        out[part] = t[a:b]
    a, b = fu._strip(t, ls[1][1] + 1, len(t))
    out["body"] = t[a:b]
    return out


def umn_map(usa_iso: str, src_iso: str, log=_log_null) -> Tuple[Dict[str, bytes], dict]:
    """{umn row id: Japanese raw bytes}. Mails by file (subject / sender /
    body), chats by ``<msg>`` index when the speaker sequences agree,
    database pages through ``_db_pairs`` (the files are numbered in each
    language's own alphabetical order, so file numbers do not line up)."""
    import txfam_umn as fu
    su, sr = fu._load(usa_iso), fu._load(src_iso)
    out: Dict[str, bytes] = {}
    st: Counter = Counter()
    # mails
    for path, data in su.objs.items():
        m = fu._MAIL_RE.search(path)
        if not m or path not in sr.objs:
            continue
        ja = _ja_mail_slots(fu._text_of(path, sr.objs[path]))
        if ja is None:
            st["mail_pages_unparsed"] += 1
            continue
        st["mail_pages"] += 1
        for part, raw in ja.items():
            rid = f"umn:mail:{m.group(1)}:{part}"
            if rid in su.slots:
                out[rid] = raw
    # chats
    chats: Dict[str, List[str]] = defaultdict(list)
    for rid in su.slots:
        if rid.startswith("umn:chat:"):
            chats[rid.rsplit(":", 1)[0]].append(rid)
    for base, ids in chats.items():
        rids = [rid for rid in sr.slots if rid.rsplit(":", 1)[0] == base]
        if len(rids) != len(ids) or [su.slots[i].row["ctx"] for i in ids] != \
                [sr.slots[i].row["ctx"] for i in rids]:
            st["chat_files_differ"] += 1
            continue
        st["chat_files"] += 1
        for a, b in zip(ids, rids):
            out[a] = sr.slots[b].raw
    # database
    pairs, dst = _db_pairs(su, sr)
    st.update(dst)
    for unum, rnum in pairs.items():
        for part in ("title", "body"):
            a, b = f"umn:db:{unum}:{part}", f"umn:db:{rnum}:{part}"
            if a in su.slots and b in sr.slots:
                out[a] = sr.slots[b].raw
    log(f"umn: {st['mail_pages']} mail pages, {st['chat_files']} chat files "
        f"({st['chat_files_differ']} differ), {len(pairs)} database pages paired")
    return out, dict(st)


def _db_pairs(su, sr) -> Tuple[Dict[str, str], Counter]:
    """USA database file number -> Reloaded file number.

    The two discs number their pages in their own alphabetical order
    (``db_fileno.txt``: A, B, C ... against ア行, カ行 ...), so 9000.uml is
    "AAA class encryption" on one and "アーキタイプ" on the other. The one
    language-independent key in a page is the u32 at 0x0C of its header,
    which groups pages (the pages of a group share it; the 41 groups have
    the same sizes on both discs, 245 pages). Inside a group, pages are
    paired only by evidence that cannot pair two different articles:

    1. the set of numbers in title + body (full-width digits folded; at
       least two numbers or one of 3+ digits: years like T.C. 4743, "Plan
       401"), when exactly one page on each side has that set;
    2. then the run of capital Latin letters of the title ("ECM",
       "DME addiction" / "ＤＭＥ中毒"), at least two, mutually unique;
    3. then, when one page is left on each side of a group, those two.

    Everything else stays unpaired (no guessing between look-alikes)."""
    import unicodedata
    import txfam_umn as fu
    st: Counter = Counter()

    def groups(state):
        g: Dict[int, Dict[str, Tuple[frozenset, str]]] = defaultdict(dict)
        for path, data in state.objs.items():
            m = fu._DB_RE.search(path)
            if not m or len(data) < 0x10:
                continue
            n = m.group(1)
            title = state.slots.get(f"umn:db:{n}:title")
            body = state.slots.get(f"umn:db:{n}:body")
            tt = unicodedata.normalize("NFKC", (title.raw if title else b"").decode("euc_jp", "replace"))
            bt = unicodedata.normalize("NFKC", (body.raw if body else b"").decode("euc_jp", "replace"))
            nums = frozenset(re.findall(r"\d+", tt + "\n" + bt))
            caps = "".join(re.findall(r"[A-Z]", tt))
            g[struct.unpack_from("<I", data, 0x0C)[0]][n] = (nums, caps)
        return g

    def strong_nums(ns) -> bool:
        return len(ns) >= 2 or any(len(x) >= 3 for x in ns)

    gu, gr = groups(su), groups(sr)
    out: Dict[str, str] = {}
    for key, a in gu.items():
        b = dict(gr.get(key, {}))
        a = dict(a)
        if len(a) != len(b):
            st["db_groups_size_differs"] += 1
            continue
        for field, strong, why in ((0, strong_nums, "db_pairs_numbers"),
                                   (1, lambda c: len(c) >= 2, "db_pairs_capitals")):
            changed = True
            while changed:
                changed = False
                for un, fu_ in list(a.items()):
                    if not strong(fu_[field]):
                        continue
                    hits = [rn for rn, fr in b.items() if fr[field] == fu_[field]]
                    twins = [x for x, fx in a.items() if fx[field] == fu_[field]]
                    if len(hits) == 1 and len(twins) == 1:
                        out[un] = hits[0]
                        del a[un], b[hits[0]]
                        st[why] += 1
                        changed = True
        if len(a) == 1 and len(b) == 1:
            out[next(iter(a))] = next(iter(b))
            st["db_pairs_last_in_group"] += 1
        st["db_pages_unpaired"] += len(a) - (1 if len(a) == 1 and len(b) == 1 else 0)
    return out, st


# ---------------------------------------------------------------------------
# fix: same files, per-file record layouts
# ---------------------------------------------------------------------------

def _cstr(buf: bytes, off: int, end: Optional[int] = None) -> bytes:
    end = len(buf) if end is None else end
    j = buf.find(b"\0", off, end)
    return bytes(buf[off:end if j < 0 else j])


def _cstr_ctl(buf: bytes, off: int, end: Optional[int] = None) -> bytes:
    """A NUL-terminated engine string that may contain control codes whose
    operand is 0 (Reloaded writes ``\x19\x00`` / ``\x19\x01`` around
    half-width punctuation, e.g. ``Ｕ\x19\x01．\x19\x00Ｍ``): operands are
    stepped over with ``font.control_length``, as the printer does."""
    from font import control_length
    end = len(buf) if end is None else end
    i = off
    while i < end and buf[i] != 0:
        if buf[i] < 0x20 and buf[i] != 0x0A:
            try:
                i += max(1, control_length(buf, i))
            except (IndexError, TypeError):
                i += 1
        else:
            i += 1
    return bytes(buf[off:min(i, end)])


def _name_desc(rec: bytes, desc_end: int) -> Tuple[bytes, bytes]:
    """Reloaded's name + description records: the name at 0, the
    description at the first non-NUL byte after it (Reloaded pads the name
    with NULs to a fixed column instead of packing the two)."""
    name = _cstr_ctl(rec, 0, desc_end)
    k = len(name) + 1
    while k < desc_end and rec[k] == 0:
        k += 1
    return name, (_cstr_ctl(rec, k, desc_end) if k < desc_end else b"")


def fix_map(usa_iso: str, src_iso: str, log=_log_null) -> Tuple[Dict[str, bytes], dict]:
    """{fix row id: Japanese raw bytes}. Layouts checked against the
    Reloaded files (they differ from USA's in four of the seven tables)."""
    import txfam_fix as ff
    fu_, ou = ff.scan(usa_iso)
    fr, orr = ff.scan(src_iso)
    ids = {f.id: f for f in fu_}
    out: Dict[str, bytes] = {}
    st: Counter = Counter()
    lower = {p.lower(): p for p in orr}

    def obj(p):
        k = lower.get(p.lower())
        return orr.get(k) if k else None

    # key items: 255 x 0x80 records, same item numbers; the last 4 bytes of a
    # Reloaded record hold a separate two-byte code, not text
    d = obj(ff.P_EVTITEM)
    if d is not None and len(d) == len(ou.get(ff.P_EVTITEM, b"")):
        for i in range(len(d) // 0x80):
            rec = d[i * 0x80:(i + 1) * 0x80]
            if not any(rec[:0x7C]):
                continue
            name, desc = _name_desc(rec, 0x7C)
            for part, raw in (("name", name), ("desc", desc)):
                rid = f"fix:evtitem:{i + 1}:{part}"
                if rid in ids and raw:
                    out[rid] = raw
        st["evtitem"] = 1
    # U.M.N. plug-ins: 6 records of 127 bytes on Reloaded (128 on USA)
    d = obj(ff.P_UMNTXT)
    nu = len(ou.get(ff.P_UMNTXT, b"")) // 0x80
    if d is not None and nu and len(d) % nu == 0:
        size = len(d) // nu
        for i in range(nu):
            name, desc = _name_desc(d[i * size:(i + 1) * size], size)
            for part, raw in (("name", name), ("desc", desc)):
                rid = f"fix:umntxt:{i}:{part}"
                if rid in ids and raw:
                    out[rid] = raw
        st["umntxt"] = 1
    # junk parts: the same 64 strings, but Reloaded stores the blocks in the
    # order info (23), map names (18), item names (23) with space padding
    d = obj(ff.P_MAPEX)
    if d is not None:
        ss = [s.rstrip(b" ") for s in d.split(b"\0") if s.strip(b" ")]
        blocks_u = ff._mapex_split(ou[ff.P_MAPEX])
        n0, n1, n2 = (len(b) for b in blocks_u)
        if len(ss) == n0 + n1 + n2:
            jb = {0: ss[:n0], 2: ss[n0:n0 + n2], 1: ss[n0 + n2:]}
            # check: every map name has one line break on both discs, no item name has one
            if all(s.count(b"\n") == 1 for s in jb[2]) and all(b"\n" not in s for s in jb[1]) \
                    and all(s.count(b"\n") == 1 for s in blocks_u[2]):
                for bi, strings in jb.items():
                    for si, raw in enumerate(strings):
                        rid = f"fix:mapex:{bi}:{si}"
                        if rid in ids:
                            out[rid] = raw
                st["mapex"] = 1
    # card game: same 148 records; Reloaded's texts are not packed (a third
    # offset is used), so read name / caption through their offsets
    d = obj(ff.P_CARD)
    if d is not None and len(d) > 0x10:
        cnt = struct.unpack_from("<H", d, 0)[0]
        if cnt == struct.unpack_from("<H", ou[ff.P_CARD], 0)[0]:
            for i in range(cnt):
                no, co, _ = struct.unpack_from("<III", d, ff.CARD_HEADER + i * ff.CARD_REC + 0x18)
                for part, off in (("name", no), ("caption", co)):
                    rid = f"fix:card:{i}:{part}"
                    if rid in ids and 0 < off < len(d):
                        out[rid] = ff.card_to_print(_cstr_ctl(d, off))
            st["card"] = 1
    # casino: identical layout
    d = obj(ff.P_CASINO)
    if d is not None and len(d) == len(ou.get(ff.P_CASINO, b"")):
        for f in ff._fields_casino(d):
            if f.id in ids and f.raw:
                out[f.id] = f.raw
        st["casino"] = 1
    # save-screen locations: endou\savemap.bin is not on the Reloaded disc
    # enemy battle quotes: the n-th string of the same think file, when the
    # PRINT / MSG sequence agrees
    for p in sorted(x for x in ou if x.lower().startswith(ff.THINK_DIR)):
        d = obj(p)
        if d is None:
            continue
        try:
            a, b = ff.think_strings(ou[p]), ff.think_strings(d)
        except ff.ThinkError:
            st["think_unparsed"] += 1
            continue
        if [s.op for s in a] != [s.op for s in b]:
            st["think_differs"] += 1
            continue
        st["think_files"] += 1
        stem = p.rsplit("\\", 1)[-1].rsplit(".", 1)[0]
        for x, y in zip(a, b):
            rid = f"fix:{stem}:{x.off:x}"
            if rid in ids:
                out[rid] = y.raw
    log(f"fix: tables {sorted(k for k in st if st[k] == 1 and not k.startswith('think'))}, "
        f"{st['think_files']} think files ({st['think_differs']} differ)")
    return out, dict(st)


# ---------------------------------------------------------------------------
# ovl: masked function match against the USA symbols
# ---------------------------------------------------------------------------

_JR_RA = 0x03E00008
_MASK_IMM = {0x09, 0x0D} | set(range(0x20, 0x40))      # addiu, ori, loads / stores


def _mask(w: int) -> int:
    op = w >> 26
    if op in (0x02, 0x03):
        return op << 26                                   # j / jal target
    if op == 0x0F:
        return w & 0xFFFF0000                             # lui immediate
    if op in _MASK_IMM:
        return w & 0xFFFF0000                             # address / offset low half
    return w


class _Elf:
    """Load segments of an executable and its words, for the Reloaded side
    (no symbols needed)."""

    def __init__(self, data: bytes):
        from txfam_ovl import _phdrs
        self.data = data
        self.segs = _phdrs(data)                          # (vaddr, off, filesz)
        n = len(data) // 4
        self.words = struct.unpack_from(f"<{n}I", data, 0)

    def va_to_off(self, va: int) -> Optional[int]:
        for v, o, sz in self.segs:
            if v <= va < v + sz:
                return o + va - v
        return None

    def off_to_va(self, off: int) -> Optional[int]:
        for v, o, sz in self.segs:
            if o <= off < o + sz:
                return v + off - o
        return None

    def word(self, off: int) -> int:
        return self.words[off // 4]

    def starts(self) -> List[int]:
        """File offsets where a function may start: after a ``jr ra`` and its
        delay slot, past nop padding, and every segment start."""
        out = set()
        for v, o, sz in self.segs:
            out.add(o)
            for off in range(o, o + sz - 8, 4):
                if self.words[off // 4] == _JR_RA:
                    q = off + 8
                    out.add(q)
                    while q < o + sz and self.words[q // 4] == 0:
                        q += 4
                        out.add(q)
        return sorted(out)


def _func_map(uov, rel: _Elf) -> Dict[int, int]:
    """USA function file offset -> Reloaded file offset, for every USA FUNC
    symbol whose masked instruction words occur at exactly one Reloaded
    function start (or, for a body that occurs several times, at the one
    with the same displacement as the nearest uniquely matched neighbour)."""
    K = 4
    index: Dict[tuple, List[int]] = defaultdict(list)
    for s in rel.starts():
        if s + 4 * K <= len(rel.data):
            index[tuple(_mask(rel.word(s + 4 * k)) for k in range(K))].append(s)
    ud = bytes(uov.data)
    uw = struct.unpack_from(f"<{len(ud) // 4}I", ud, 0)
    out: Dict[int, int] = {}
    ambiguous: List[Tuple[int, List[int]]] = []
    for off, size in uov.funcs:
        if size < 4 * K or off % 4:
            continue
        body = [_mask(w) for w in uw[off // 4:(off + size) // 4]]
        hits = [s for s in index.get(tuple(body[:K]), ())
                if s + size <= len(rel.data)
                and all(_mask(rel.word(s + 4 * k)) == body[k] for k in range(K, len(body)))]
        if len(hits) == 1:
            out[off] = hits[0]
        elif hits:
            ambiguous.append((off, hits))
    if ambiguous and out:
        keys = sorted(out)
        import bisect
        for off, hits in ambiguous:
            i = bisect.bisect_left(keys, off)
            near = [keys[j] for j in (i - 1, i) if 0 <= j < len(keys)]
            deltas = {out[k] - k for k in near}
            pick = [s for s in hits if s - off in deltas]
            if len(pick) == 1:
                out[off] = pick[0]
    return out


def _hilo(w_lui: int, w_use: int) -> int:
    hi, lo = w_lui & 0xFFFF, w_use & 0xFFFF
    if w_use >> 26 == 0x0D:
        return (hi << 16) | lo
    return ((hi << 16) + (lo - 0x10000 if lo & 0x8000 else lo)) & 0xFFFFFFFF


def _table_layout_same(uov, rel: _Elf, start: int, end: int, d: int) -> bool:
    """Does the Reloaded object at ``start + d`` have the USA object's layout?
    Every word that is not a pointer into the USA executable must be equal
    (record numbers, prices, stats are the same in both builds), and every
    USA pointer to a string must face a Reloaded pointer to a string."""
    uo, ro = uov.va_to_off(start), rel.va_to_off(start + d)
    n = (end - start) // 4
    if uo is None or ro is None or ro % 4 or uo % 4 or rel.va_to_off(start + d + 4 * n - 4) is None:
        return False
    ud = bytes(uov.data)
    same = differ = ptrs = 0
    for k in range(n):
        uw = struct.unpack_from("<I", ud, uo + 4 * k)[0]
        rw = rel.word(ro + 4 * k)
        if uw in uov.strings:
            ptrs += 1
            roff = rel.va_to_off(rw)
            if roff is None or roff == 0 or rel.data[roff - 1] != 0:
                return False
        elif uov.va_to_off(uw) is not None and uw >= 0x100000:
            continue                                   # pointer to other data / code
        elif uw == rw:
            same += 1
        else:
            differ += 1
    return ptrs > 0 and differ <= max(0, (same + differ) // 50)


def _table_strings(uov, rel: _Elf, start: int, end: int, d: int) -> List[Tuple[bytes, bytes]]:
    uo, ro = uov.va_to_off(start), rel.va_to_off(start + d)
    out = []
    ud = bytes(uov.data)
    for k in range((end - start) // 4):
        uw = struct.unpack_from("<I", ud, uo + 4 * k)[0]
        if uw in uov.strings:
            roff = rel.va_to_off(rel.word(ro + 4 * k))
            out.append((uov.strings[uw].raw, _cstr_ctl(rel.data, roff) if roff is not None else b""))
    return out


def _len_corr(pairs: List[Tuple[bytes, bytes]]) -> Optional[float]:
    """Pearson correlation of USA and Japanese string lengths (None when
    there is too little to measure)."""
    if len(pairs) < 4:
        return None
    xs = [float(len(a)) for a, _ in pairs]
    ys = [float(len(b)) for _, b in pairs]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        # every string the same length on one side: equal-length tables (codes,
        # initials) say nothing; accept only exact byte equality of the sides
        return 1.0 if all(a == b for a, b in pairs) else None
    return sxy / (sxx * syy) ** 0.5


def _feat(raw: bytes) -> Tuple[int, tuple]:
    import unicodedata
    t = unicodedata.normalize("NFKC", raw.decode("euc_jp", "replace"))
    return t.count("\n"), tuple(re.findall(r"\d+", t))


def _pair_score(pairs: List[Tuple[bytes, bytes]]) -> float:
    """How well USA and Japanese strings of a table correspond, 0..3: the
    share with the same number of line breaks, the share with the same
    numbers in them (full-width digits folded), and the length correlation."""
    if not pairs:
        return 0.0
    s = 0
    for x, y in pairs:
        fx, fy = _feat(x), _feat(y)
        s += (fx[0] == fy[0]) + (fx[1] == fy[1])
    return s / len(pairs) + (_len_corr(pairs) or 0.0)


ANCHOR_MIN_STRINGS = 8
SEED_RANGE = 0x8000        # displacements tried when no code reference places any table
ANCHOR_MIN_SCORE = 2.3
ANCHOR_MARGIN = 0.15


def _pick_delta(uov, rel: _Elf, start: int, end: int, cands) -> Optional[int]:
    """The displacement of a USA object when it is clearly the best among
    the candidates and their shifts by up to four words, else None. Needs
    ANCHOR_MIN_STRINGS strings to measure; smaller objects are placed only
    between anchors (see the sandwich rule in ``_ovl_exe_map``)."""
    scored: Dict[int, float] = {}
    for d in cands:
        for sh in (0, -16, -12, -8, -4, 4, 8, 12, 16):
            e = d + sh
            if e in scored or not _table_layout_same(uov, rel, start, end, e):
                continue
            pairs = _table_strings(uov, rel, start, end, e)
            if len(pairs) < ANCHOR_MIN_STRINGS:
                return None
            scored[e] = _pair_score(pairs)
    if not scored:
        return None
    best = max(scored, key=lambda d: scored[d])
    rest = [c for d, c in scored.items() if d != best]
    if scored[best] < ANCHOR_MIN_SCORE or (rest and max(rest) > scored[best] - ANCHOR_MARGIN):
        return None
    return best


def _ovl_exe_map(name: str, uov, rdata: bytes, st: Counter) -> Dict[int, bytes]:
    """USA string va -> Reloaded raw bytes for one executable."""
    import bisect
    rel = _Elf(rdata)
    fmap = _func_map(uov, rel)
    st["functions"] += len(uov.funcs)
    st["functions_matched"] += len(fmap)
    funcs = sorted(uov.funcs)
    fstarts = [f for f, _ in funcs]
    ud = bytes(uov.data)

    def func_of(off: int) -> Optional[Tuple[int, int]]:
        i = bisect.bisect_right(fstarts, off) - 1
        if i >= 0 and funcs[i][0] <= off < funcs[i][0] + funcs[i][1]:
            return funcs[i]
        return None

    def rel_pair(use_off: int, lui_off: int) -> Optional[int]:
        """Reloaded address formed by the instruction pair at the same place."""
        f = func_of(use_off)
        if f is None or f[0] not in fmap or not (f[0] <= lui_off < f[0] + f[1]):
            return None
        r0 = fmap[f[0]]
        ru, rl = r0 + use_off - f[0], r0 + lui_off - f[0]
        uu, ul = struct.unpack_from("<I", ud, use_off)[0], struct.unpack_from("<I", ud, lui_off)[0]
        wu, wl = rel.word(ru), rel.word(rl)
        if _mask(wu) != _mask(uu) or _mask(wl) != _mask(ul):
            return None
        return _hilo(wl, wu)

    def rel_string(va: int) -> Optional[bytes]:
        off = rel.va_to_off(va)
        if off is None or off == 0 or rdata[off - 1] != 0 or rdata[off] == 0:
            return None
        return _cstr_ctl(rdata, off)

    # data tables: the Reloaded base of each USA OBJECT symbol that holds
    # string pointers. Candidate displacements come from the code that forms
    # an address inside the object (lui + addiu / ori; a load or store off a
    # lui base forms field addresses that merely fall inside some table) and
    # from the neighbouring objects already placed (data keeps its order, so
    # neighbours usually share a displacement). A candidate must keep the
    # object's layout (``_table_layout_same``) and, among the candidates and
    # their shifts by one to four words, be the clear best at pairing long
    # USA strings with long Japanese ones (``_len_corr``).
    from txfam_ovl import _symbols
    objs = [(a, b, n) for a, b, n in _symbols(uov) if uov.va_to_off(a) is not None
            and not uov._code[uov.va_to_off(a)]]
    ostarts = [a for a, _, _ in objs]

    def obj_of(va: int):
        i = bisect.bisect_right(ostarts, va) - 1
        return objs[i] if i >= 0 and objs[i][0] <= va < objs[i][1] else None

    holders = {}
    for va, s in uov.strings.items():
        for r in s.refs:
            if r.kind == "ptr":
                wva = uov.off_to_va(r.off)
                o = obj_of(wva) if wva is not None else None
                if o is not None:
                    holders[o[0]] = o
    votes: Dict[int, Counter] = defaultdict(Counter)
    for lui_off, uses in uov.lui_uses.items():
        for use_off, addr in uses:
            o = obj_of(addr)
            if o is None or o[0] not in holders:
                continue
            ra = rel_pair(use_off, lui_off)
            if ra is not None:
                votes[o[0]][ra - addr] += 1
    # anchors: objects whose best displacement is clearly best
    # every displacement already found in this file is a candidate for the
    # others too (data moves in a few large steps), until nothing new appears
    delta: Dict[int, int] = {}
    tried: Dict[int, set] = defaultdict(set)
    seeded = False
    while True:
        found = set(delta.values())
        new = 0
        for base, (start, end, _) in holders.items():
            if base in delta:
                continue
            cands = set(votes.get(base, ())) | found
            if cands <= tried[base]:
                continue
            tried[base] |= cands
            pick = _pick_delta(uov, rel, start, end, cands)
            if pick is not None:
                delta[base] = pick
                new += 1
        if not new and not delta and not seeded and holders:
            # no code reference reached any table: scan displacements for the
            # three objects with the most strings and let the clear winner seed
            seeded = True
            npt = {b: sum(1 for k in range((e - a) // 4)
                          if struct.unpack_from("<I", ud, uov.va_to_off(a) + 4 * k)[0] in uov.strings)
                   for b, (a, e, _) in holders.items()}
            for base in sorted(holders, key=lambda b: -npt[b])[:3]:
                start, end, _ = holders[base]
                cands = {d for d in range(-SEED_RANGE, SEED_RANGE, 4)
                         if _table_layout_same(uov, rel, start, end, d)}
                pick = _pick_delta(uov, rel, start, end, cands)
                if pick is not None:
                    delta[base] = pick
                    st["tables_seeded"] += 1
            if delta:
                continue
        if not new:
            break
    st["tables_anchored"] += len(delta)
    # sandwich: an object between two anchors of the same segment that share a
    # displacement has that displacement too (everything between them kept
    # its size); still checked with _table_layout_same
    order = sorted(holders)
    anchors = [b for b in order if b in delta]
    for lo, hi in zip(anchors, anchors[1:]):
        d = delta[lo]
        if delta[hi] != d or uov.va_to_off(hi) - uov.va_to_off(lo) != hi - lo:
            continue
        for base in order:
            if lo < base < hi and base not in delta:
                start, end, _ = holders[base]
                if _table_layout_same(uov, rel, start, end, d):
                    delta[base] = d
    st["tables"] += len(holders)
    st["tables_located"] += len(delta)

    out: Dict[int, bytes] = {}
    for va, s in uov.strings.items():
        got: List[bytes] = []
        bad = False
        for r in s.refs:
            if r.kind == "hilo":
                ra = rel_pair(r.off, r.lui_off)
                if ra is None:
                    continue
                b = rel_string(ra)
            else:
                wva = uov.off_to_va(r.off)
                o = obj_of(wva) if wva is not None else None
                if o is None or o[0] not in delta:
                    continue
                woff = rel.va_to_off(wva + delta[o[0]])
                if woff is None or woff % 4:
                    continue
                b = rel_string(rel.word(woff))
            if b is None:
                bad = True                     # the place exists but holds no string
                continue
            got.append(b)
        if got and not bad and all(g == got[0] for g in got):
            out[va] = got[0]
        elif got:
            st["strings_disagree"] += 1
    return out


def ovl_map(usa_iso: str, src_iso: str, log=_log_null) -> Tuple[Dict[str, bytes], dict]:
    """{ovl row id: Japanese raw bytes} (ids ``ovl:FILE:va``, FILE being
    the USA name; the boot ELF pairs with the source disc's boot ELF)."""
    import txfam_ovl as fo
    ust = fo._load(usa_iso)
    iso = IsoImage(src_iso)
    try:
        rboot = iso.boot_elf_name()
        rfiles = {n.upper(): n for n in iso.files}
        rdata = {}
        for name in ust.exes:
            rn = rboot if name == fo.BOOT else rfiles.get(name.upper())
            if rn:
                rdata[name] = bytes(iso.read_file(rn))
    finally:
        iso.close()
    out: Dict[str, bytes] = {}
    st: Counter = Counter()
    for name, ex in ust.exes.items():
        if name not in rdata:
            continue
        c: Counter = Counter()
        m = _ovl_exe_map(name, ex.ov, rdata[name], c)
        log(f"ovl {name}: {c['functions_matched']}/{c['functions']} functions matched, "
            f"{c['tables_located']}/{c['tables']} data objects located, {len(m)} strings")
        st.update(c)
        for va, raw in m.items():
            out[fo.row_id(name, va)] = raw
    return out, dict(st)


# ---------------------------------------------------------------------------
# align: the public entry point
# ---------------------------------------------------------------------------

_STAMPED = ("txsource", "txfam_evt", "txfam_umn", "txfam_fix", "txfam_ovl", "txcodec",
            "evttext", "ovltext", "textpack", "font", "iso9660", "toc", "chains")


def _code_digest(code) -> bytes:
    """Deterministic digest of a code object: bytecode, names and constants,
    recursively, with set constants sorted. (``marshal.dumps`` is not stable:
    its output depends on refcounts and, for frozenset constants, on the
    per-process string hash seed.)"""
    h = hashlib.sha1()

    def walk(c):
        h.update(c.co_code)
        h.update(repr((c.co_names, c.co_varnames, c.co_argcount)).encode())
        for k in c.co_consts:
            if hasattr(k, "co_code"):
                walk(k)
            elif isinstance(k, frozenset):
                h.update(repr(sorted(repr(x) for x in k)).encode())
            else:
                h.update(repr(k).encode())

    walk(code)
    return h.digest()


def _code_stamp() -> str:
    """Hash of this module and every reader it uses: the source file when
    there is one, else (a frozen build that ships only bytecode) a
    deterministic digest of the code object."""
    import importlib.util
    h = hashlib.sha1()
    for name in _STAMPED:
        digest = b""
        try:
            spec = importlib.util.find_spec(name)
            origin = getattr(spec, "origin", None) if spec is not None else None
            if origin and origin.endswith(".py") and os.path.isfile(origin):
                digest = hashlib.sha1(Path(origin).read_bytes()).digest()
            elif spec is not None and spec.loader is not None:
                code = spec.loader.get_code(name)
                if code is not None:
                    digest = _code_digest(code)
        except Exception:
            digest = b""
        h.update(name.encode() + b"\0" + (digest or b"-"))
    return h.hexdigest()


def _rows_key(rows: List[dict]) -> str:
    h = hashlib.sha1()
    for r in rows:
        h.update(f"{r['id']}\0{r.get('cat')}\0{r.get('font')}\0{r.get('ctl')}\0"
                 f"{(r.get('ctx') or '')[:24]}\n".encode("utf-8", "replace"))
    return h.hexdigest()


def _stats(rows: List[dict], texts: Dict[str, str]) -> Tuple[dict, dict]:
    fam: Dict[str, Counter] = defaultdict(Counter)
    cat: Dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        hit = r["id"] in texts
        vis = not r.get("hide")
        for c in (fam[r["fam"]], cat[r["cat"]]):
            c["rows"] += 1
            c["aligned"] += hit
            c["unaligned"] += not hit
            c["visible"] += vis
            c["visible_aligned"] += vis and hit
    keys = ("rows", "aligned", "unaligned", "visible", "visible_aligned")
    return ({f: {k: c[k] for k in keys} for f, c in fam.items()},
            {f: {k: c[k] for k in keys} for f, c in cat.items()})


def align(usa_iso: str, source_iso: str, rows: Optional[List[dict]] = None,
          log: Callable[[str], None] = print, use_cache: bool = True) -> dict:
    """Source-language text for every USA catalog row that can be aligned.

    ``usa_iso``: the retail USA image the catalog was read from.
    ``source_iso``: the source-language disc (Reloaded for Japanese; the USA
    disc itself gives English = each row's own ``src``).
    ``rows``: the catalog rows (``txcatalog.build_catalog(usa_iso)["rows"]``);
    built (cached) when omitted.

    Returns ``{"source": id, "texts": {row_id: display text},
    "partial": [row_id], "stats": {family: {"rows", "aligned", "unaligned",
    "visible", "visible_aligned"}}, "by_cat": {category: same},
    "detail": {family: counters of the aligner}, "notes": [str]}``.
    ``partial`` lists rows whose text is the whole Japanese message the USA
    row is one piece of (the two scripts cut that message differently).
    Raises ``SourceError`` for a disc that is not supported. Read-only; the
    result is cached under ``~/.cache/xs1-txeditor/source-<hash>.json``."""
    kind_u = identify_disc(usa_iso)
    if kind_u != "usa":
        raise SourceError(f"{usa_iso}: not the Xenosaga Episode I (USA) disc "
                          f"(boot executable {BOOT_USA} expected)")
    kind_s = identify_disc(source_iso)
    if kind_s is None:
        raise SourceError(f"{source_iso}: not a Xenosaga Episode I disc this editor knows "
                          f"(expected Xenosaga Episode I Reloaded, {BOOT_RELOADED})")
    if kind_s == "jp-original":
        raise SourceError(f"{source_iso}: the 2002 original Japanese disc is recognised but "
                          f"not supported yet as a source; use Xenosaga Episode I Reloaded "
                          f"({BOOT_RELOADED})")
    if rows is None:
        import txcatalog
        rows = txcatalog.build_catalog(usa_iso, log=log)["rows"]
    if kind_s == "usa":
        texts = {r["id"]: r["src"] for r in rows if r["src"].strip()}
        stats, by_cat = _stats(rows, texts)
        return {"source": SRC_EN, "texts": texts, "partial": [], "stats": stats,
                "by_cat": by_cat, "detail": {}, "notes": ["English: the rows' own source text"]}

    key = hashlib.sha1("\0".join((str(VERSION), disc_id(usa_iso), disc_id(source_iso),
                                  _code_stamp(), _rows_key(rows))).encode()).hexdigest()
    cache = CACHE_DIR / f"source-{key}.json"
    if use_cache:
        try:
            with open(cache, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("version") == VERSION and data.get("source") == SRC_JA:
                data["notes"] = data.get("notes", []) + ["(from cache)"]
                data.pop("version", None)
                return data
        except (OSError, ValueError):
            pass

    t0 = time.time()
    by_fam: Dict[str, List[dict]] = defaultdict(list)
    for r in rows:
        by_fam[r["fam"]].append(r)
    texts: Dict[str, str] = {}
    partial: List[str] = []
    detail: Dict[str, dict] = {}
    notes: List[str] = []
    complete = True
    for fam, fn in (("evt", evt_map), ("umn", umn_map), ("fix", fix_map), ("ovl", ovl_map)):
        if fam not in by_fam:
            continue
        t = time.time()
        try:
            m, st = fn(usa_iso, source_iso, log=log)
        except Exception as exc:          # one family failing must not lose the others
            notes.append(f"{fam}: alignment failed ({type(exc).__name__}: {exc})")
            log(notes[-1])
            complete = False
            continue
        detail[fam] = st
        if fam == "evt":
            tx, part = _evt_texts(by_fam[fam], m)
            texts.update(tx)
            partial += part
        else:
            for r in by_fam[fam]:
                raw = m.get(r["id"])
                if raw is not None:
                    texts[r["id"]] = display_ja(raw, r.get("ctl") or "c0", r.get("font") or tc.FIELD)
        log(f"{fam}: aligned in {time.time() - t:.1f} s")
    # a Japanese counterpart that is blank where the English row is not
    # (no speaker name box, a page whose text is all in its picture) is
    # left out: it would read as "nothing to translate from"
    # (whatever the English is: a blank or whitespace-only text is never
    # returned, so a key in "texts" is always something to translate from)
    blank = [rid for rid, tx in texts.items() if not tx.strip()]
    for rid in blank:
        del texts[rid]
    blank_set = set(blank)
    partial = [rid for rid in partial if rid not in blank_set]
    if blank:
        notes.append(f"{len(blank)} rows whose Japanese counterpart is blank are left out")
    stats, by_cat = _stats(rows, texts)
    notes.append(f"aligned in {time.time() - t0:.1f} s")
    res = {"source": SRC_JA, "texts": texts, "partial": partial, "stats": stats,
           "by_cat": by_cat, "detail": detail, "notes": notes}
    if complete:
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(cache.parent), suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(dict(res, version=VERSION), fh, ensure_ascii=False)
            os.replace(tmp, cache)
        except OSError as exc:
            log(f"could not write cache {cache}: {exc}")
    return res


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

_CTL_TAG = re.compile(r"/\[(waitkey|close|clear|label|\$\d+|#\d+)")
_JA_CHAR = re.compile(r"[぀-ヿ一-鿿！-～]")


def _tag_sig(s: str) -> Counter:
    return Counter(m.group(1) for m in _CTL_TAG.finditer(s))


def selftest(usa: str, source: Optional[str]) -> int:
    ok = True

    def check(cond, what):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + what)
        ok = ok and bool(cond)

    # offline pieces
    check(display_ja("主任、どう？".encode("euc_jp") + b"\n/[waitkey(64)]", "evt")
          == "主任、どう？\n/[waitkey(64)]", "display_ja: kanji pass through, tags stay")
    check(display_ja("鬱".encode("euc_jp")) == "鬱", "display_ja: level-2 kanji (row 0xD0+) is kanji, not the USA accent block")
    check(display_ja(b"\x0cTTH" + "あ".encode("euc_jp")) == "⟦0C⟧⟦54⟧⟦54⟧⟦48⟧あ", "display_ja: control codes as ⟦XX⟧")
    check(_cstr_ctl(b"\xa3\xd5\x19\x01\xa1\xa5\x19\x00\xa3\xcd\x00rest", 0) == b"\xa3\xd5\x19\x01\xa1\xa5\x19\x00\xa3\xcd",
          "_cstr_ctl steps over a control operand of 0")
    check(_mask(0x3C0200A4) == 0x3C020000 and _mask(0x0C123456) == 0x0C000000, "_mask hides lui / jal operands")
    ue = [[(1, b"/[label(A)]\0"), (2, b"Hello there.\0"), (3, b"/[waitkey(64)]/[close()]\0")]]
    re_ = [(11, "/[label(ア)]\0"), (12, "こん\n\0"), (13, "にちは\0"), (14, "/[waitkey(64)]/[close()]\0")]
    uent = [None] * 20
    rent = [None] * 20
    for i, b in ue[0]:
        uent[i] = (1, b)
    for i, t in re_:
        rent[i] = (1, t.encode("euc_jp"))
    pr = _array_pairs([1, 2, 3], [11, 12, 13, 14], uent, rent)
    check(pr == [(1, [11], False), (2, [12, 13], False), (3, [14], False)],
          "array pairing: one USA text element takes the whole Japanese group")
    # same shape, but the pages hold different parts of the message (ST1060)
    ue2 = [(1, b"/[label(Jr.)]\0"), (2, b"It says,\0"), (3, b"/[waitkey(1)]/[clear()]\0"),
           (4, b"'Emergency!\n\0"), (5, b"Emergency!\n\0"),
           (6, b"Prepare for close-quarter combat.'\0"), (7, b"/[waitkey(64)]/[close()]\0")]
    re2 = [(11, "/[label(Jr.)]\0"), (12, "緊急事態発生！\n緊急事態発生！\nただちに臨戦態勢を整えてください。\0"),
           (13, "/[waitkey(1)]/[clear()]\0"), (14, "…だってさ。\0"), (15, "/[waitkey(64)]/[close()]\0")]
    for i, b in ue2:
        uent[i] = (1, b)
    for i, t in re2:
        rent[i] = (1, t.encode("euc_jp"))
    pr = _array_pairs([1, 2, 3, 4, 5, 6, 7], [11, 12, 13, 14, 15], uent, rent)
    got = {u: (r, p) for u, r, p in (pr or [])}
    check(pr is not None and got[2] == ([12, -1, 14], True) and got[4] == ([12, -1, 14], True),
          "array pairing: same shape, text on different pages -> whole message, partial")
    check([s["id"] for s in sources()] == [SRC_EN, SRC_JA] and sources()[1]["needs"], "sources()")
    check(identify_disc("/nonexistent.iso") is None, "identify_disc(missing) is None")
    check(_BOOT_JP_ORIGINAL.match("SLPS_250.50") is not None and not _BOOT_JP_ORIGINAL.match(BOOT_RELOADED),
          "the 2002 serial pattern is not Reloaded's")
    if not usa:
        print("selftest", "passed" if ok else "FAILED", "(no discs given)")
        return 0 if ok else 1
    check(identify_disc(usa) == "usa", "identify_disc(USA) == usa")
    if not source:
        print("selftest", "passed" if ok else "FAILED", "(no source disc given)")
        return 0 if ok else 1
    check(identify_disc(source) == "reloaded", "identify_disc(Reloaded) == reloaded")
    import txcatalog
    rows = txcatalog.build_catalog(usa, log=lambda m: None)["rows"]
    t = time.time()
    res = align(usa, source, rows, log=lambda m: print("     " + m), use_cache=False)
    print(f"     aligned {len(res['texts'])} of {len(rows)} rows in {time.time() - t:.1f} s")
    texts, partial = res["texts"], set(res["partial"])
    print("\n     coverage per family (visible = shown without 'show hidden'):")
    for f, c in res["stats"].items():
        print(f"     {f:4} all {c['aligned']:6}/{c['rows']:6} ({100 * c['aligned'] / max(1, c['rows']):5.1f}%)"
              f"   visible {c['visible_aligned']:5}/{c['visible']:5} "
              f"({100 * c['visible_aligned'] / max(1, c['visible']):5.1f}%)")
    print("\n     coverage per category (visible rows):")
    for cat, c in res["by_cat"].items():
        print(f"     {cat:32} {c['visible_aligned']:5}/{c['visible']:5} "
              f"({100 * c['visible_aligned'] / max(1, c['visible']):5.1f}%)   all {c['aligned']}/{c['rows']}")
    print(f"     partial (whole-message) rows: {len(partial)}")
    # quality: control tags, line breaks, script
    by_id = {r["id"]: r for r in rows}
    tag_bad, nl_bad, nl_n, no_ja = [], Counter(), Counter(), []
    for rid, ja in texts.items():
        r = by_id[rid]
        if r.get("hide") or rid in partial:
            continue
        if r["fam"] == "evt" and r["cat"] != "Speaker names":
            # Reloaded sometimes keeps the closing tags inside the text element
            su, sj = _tag_sig(r["src"]), _tag_sig(ja)
            if any(su[k] > sj[k] for k in su):
                tag_bad.append(rid)
        if r["fam"] in ("ovl", "fix") and "\n" in r["src"] + ja:
            nl_n[r["cat"]] += 1
            if r["src"].count("\n") != ja.count("\n"):
                nl_bad[r["cat"]] += 1
        if re.search(r"[A-Za-z]{3}", r["src"]) and not _JA_CHAR.search(ja) and ja != r["src"]:
            no_ja.append(rid)
    n_evt = sum(1 for rid in texts if by_id[rid]["fam"] == "evt" and not by_id[rid].get("hide"))
    print(f"\n     evt rows whose Japanese lacks a window tag the English has: {len(tag_bad)} of {n_evt}")
    for rid in tag_bad[:5]:
        print(f"       {rid}: {by_id[rid]['src'][:50]!r} <> {texts[rid][:50]!r}")
    check(len(tag_bad) <= max(5, n_evt // 200), "window tags agree (<= 0.5% of visible evt rows differ)")
    print("     table rows with line breaks: same count of breaks per category:")
    for c in nl_n:
        print(f"       {c:30} {nl_n[c] - nl_bad[c]}/{nl_n[c]}")
    print(f"     visible rows with English letters whose source text has no Japanese and differs: {len(no_ja)}")
    for rid in no_ja[:8]:
        print(f"       {rid}: {by_id[rid]['src'][:40]!r} => {texts[rid][:40]!r}")

    # known pairs (looked at by hand on both discs)
    def find(cat, src, grp=None):
        for r in rows:
            if r["cat"] == cat and r["src"] == src and (grp is None or r["grp"] == grp):
                return r["id"]
        return None

    known = [
        ("Dialogue — field maps", "Chief, which program shall I boot up?", "ST0210", "主任、どのプログラムを起動しますか？\n"),
        ("Speaker names", "Allen", "ST0210", "アレン"),
        ("U.M.N. mail", "New Connection Gear Models Announced", None, "新型コネクションギア発表"),
        ("Key items", "U-TIC Card", None, "Ｕ−ＴＩＣカード"),
        ("Skills & ether", "Poison Guard", None, "ポイズンガード"),
        ("Enemies", "Golem", None, "ゴーレム"),
        ("Items & equipment", "Med Kit S", None, "アクアジストＳ"),
    ]
    for cat, src, grp, want in known:
        rid = find(cat, src, grp)
        check(rid is not None and texts.get(rid) == want, f"known pair {src!r} -> {want!r} (got {texts.get(rid)!r})")
    # negative: ST1060 "It says," is on the other page from the Japanese quote
    says = [r["id"] for r in rows if r["src"] == "It says," and r["grp"].startswith("ST1060")]
    check(says and all(rid in partial or "緊急事態発生" not in texts.get(rid, "") for rid in says),
          f"ST1060 'It says,' is not paired page-by-page with the quote "
          f"({[(texts.get(rid), rid in partial) for rid in says]})")
    blanks = [rid for rid, tx in texts.items() if not tx.strip()]
    check(not blanks, f"no blank texts ({len(blanks)}: {blanks[:3]})")
    empty_lbl = [rid for rid in texts if by_id[rid]["cat"] == "Speaker names"
                 and not by_id[rid]["src"].strip()]
    check(not empty_lbl, f"no Japanese name on an empty USA speaker label ({empty_lbl[:3]})")

    # samples across categories, incl. the ST0210 opening, SCE01001, a mail ...
    print("\n     samples (USA English  ⇄  Japanese from Reloaded):")
    picks: List[str] = []
    want_grps = [("Dialogue — field maps", "ST0210", 4), ("Dialogue — cutscenes", "SCE01001", 5),
                 ("Speaker names", None, 1), ("Choices & menus (scripts)", None, 2),
                 ("U.M.N. mail", None, 2), ("U.M.N. chat", None, 2), ("Items & equipment", None, 2),
                 ("Skills & ether", None, 2), ("Enemies", None, 2), ("Field menu & system", None, 2),
                 ("Battle HUD", None, 1), ("Key items", None, 1), ("Enemy battle quotes", None, 1),
                 ("Card game", None, 1), ("Junk parts & extras", None, 1), ("Casino", None, 1)]
    for cat, grp, n in want_grps:
        cand = [r for r in rows if r["cat"] == cat and not r.get("hide") and r["id"] in texts
                and (grp is None or r["grp"].startswith(grp)) and len(r["src"]) > 3]
        if grp is None and len(cand) > n:
            step = max(1, len(cand) // (n + 1))
            cand = cand[step::step]
        picks += [r["id"] for r in cand[:n]]
    for rid in picks:
        r = by_id[rid]
        print(f"     [{r['cat']} | {r['grp']}]{' (whole message)' if rid in partial else ''}")
        print(f"        EN {r['src'][:110]!r}")
        print(f"        JA {texts[rid][:110]!r}")
    print("selftest", "passed" if ok else "FAILED")
    return 0 if ok else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("selftest")
    s.add_argument("--usa")
    s.add_argument("--source")
    a_ = sub.add_parser("align")
    a_.add_argument("--usa", required=True)
    a_.add_argument("--source", required=True)
    a_.add_argument("--out", required=True)
    a_.add_argument("--no-cache", action="store_true")
    i_ = sub.add_parser("identify")
    i_.add_argument("iso")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest(a.usa, a.source)
    if a.cmd == "identify":
        print(identify_disc(a.iso))
        return 0
    try:
        res = align(a.usa, a.source, use_cache=not a.no_cache)
    except SourceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    Path(a.out).write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(res['texts'])} rows -> {a.out}")
    for f, c in res["stats"].items():
        print(f"  {f}: {c}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
