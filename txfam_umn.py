"""txfam_umn.py — the U.M.N. strings for the translation editor.

The U.M.N. terminal (overlay OV02) draws three kinds of text, all of them
uncompressed chain-0 TOC objects under ``umn\\``:

  mails     ``umn\\0000.uml`` - ``0080.uml``. A UML page: a 0x60-byte header
            (32 bytes of fields + an 8-entry picture table), the text, then
            16-byte aligned JPEGs. The text opens with two lines,
            ``Subject: `` and ``Sender: ``; the body follows a blank line.
            Rows: subject, sender, body.
  database  ``umn\\9000.uml`` - ``9244.uml``, same page format: the title
            between ``0D 02`` and ``0D 00``, a ruler line of ``―``, the body.
            Rows: title, body. Grouped by the letter sections of
            ``umn\\db_fileno.txt`` (the list order the game uses).
  chats     ``umn\\event00.txt`` - ``event18.txt``, the manzai dialogues:
            ``<speech>`` blocks with a ``<char>`` speaker and a ``<msg>``
            body in which ``\\NNN`` octal escapes carry the font codes.
            Rows: one per ``<msg>`` body; nothing else is editable.

Why this module re-parses the pages instead of using textpack: textpack
cuts a page's text at the first NUL byte, but the engine
(``tyaUmlDispLoad``) finds the end by stepping over whole control codes
(``font.control_length``), and ``0D`` takes one operand byte. A database
title ends with ``0D 00`` (shadow off), so textpack's cut loses the whole
encyclopedia body, and every mail ends with ``0D 00 15 02 04 00 0A`` that
the same cut hides. Here the page text runs to the real terminator; each
row is a span of it, and a rebuild splices the new spans into the
untouched frame (labels, leading/trailing control codes, the ruler, the
reset codes), so nothing after a row is ever dropped.

Coupled copies (``umn\\header.lst``, ``umn\\dbheader.lst``): the mail folder
and the database list are built from these, not from the pages. Each is a
chain of records ``u32 size`` (bytes after this word), ``u32 id``, a copy of
the page's 96-byte header, the page's first two text lines each ended by a
NUL instead of 0x0A, zero padding to 4; a zero size word ends the list. The
selftest rebuilds both lists from the retail pages and gets them back byte
for byte, so ``apply`` regenerates the record of every page it changes
(the lists may grow; the game parses them in the U.M.N. work arena, not in
a fixed slot). ``UmnMailHeaderCreate`` copies line 1 from byte 9 and line 2
from byte 8 (past ``Subject: `` / ``Sender: ``) into 64-byte fields with
``eMessageCpy``, so a subject or sender may hold 63 bytes with whatever
control codes sit in front of it; that is the ``lim`` of those rows. The
labels themselves stay fixed for the same reason.

Faces and widths: every U.M.N. screen draws with font1 (the face
``xglFontInitial`` loads; font0 is swapped in only for "ev" scenes), so the
codec runs with ``face="font1"``. Subjects, senders, database titles and
bodies and the chats are proportional (``measure="font1"``); a mail body
switches to fixed 10-pixel boxes (``15 02 04 07``) to keep its box-drawing
frames aligned, so its width is not a font1 measure (``measure=None``).

Engine rules the checker adds on top of txcodec:
  * page text: ``@`` plus the next byte is a loader code (``@*`` scroll mark,
    ``@( @) @< @>`` plug-in hiding); the loader eats the byte after any
    ``@``. The translation must carry exactly the source's ``@`` codes; any
    other ``@`` the translator types is written as the escape ``#4a`` (the
    same glyph; retail spells ``bunnie#4anamco.com`` that way).
  * subject, sender and title stay on one line (the lists copy one line).
  * a page or chat byte 0x00 ends the text; ``⟦00⟧`` is refused.
  * chats: ``<`` ends a message; ``\\`` must start an octal escape (the parser
    reads every following 0-7 digit, so a digit right after an escape
    changes it; only the source's escapes and ``\\37`` (page end) are
    accepted); the whole file must stay within the 0x2000-byte buffer
    ``UmnEventTextMake`` loads it into.

Not rows: ``db_fileno.txt`` (list order), the planner ``scene\\*.txt`` files
(never drawn), ``endou\\umn\\umntxt.bin`` (fixed-record family).

Usage:
    python3 txfam_umn.py selftest --iso RETAIL
    python3 txfam_umn.py dump --iso RETAIL --out rows.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from font import control_length
from iso9660 import IsoImage
from repack import patch_iso
from textpack import _entries_with_alloc
import txcodec
from txcodec import FIELD, FONT1, TxBuildError, TxEncodeError

FAM = "umn"
CHAIN = 0
UML_HEADER = 0x60
UML_ALIGN = 16
PIC_TABLE = range(0x20, 0x60, 8)          # 8 x {u32 offset, u16 x, u16 y}
LIST_FIELD = 64                           # UmnMailHeader.subject / .sender
SUBJECT_SKIP, SENDER_SKIP = 9, 8          # bytes UmnMailHeaderCreate skips
EVENT_BUFFER = 0x2000                     # UMN_EVENT_TEXT_BUFFER_SIZE

CAT_MAIL, CAT_CHAT, CAT_DB = "U.M.N. mail", "U.M.N. chat", "U.M.N. database"
MAIL_LIST, DB_LIST, DB_INDEX = "umn\\header.lst", "umn\\dbheader.lst", "umn\\db_fileno.txt"

_MAIL_RE = re.compile(r"umn\\(00\d\d)\.uml$", re.I)
_DB_RE = re.compile(r"umn\\(9\d\d\d)\.uml$", re.I)
_CHAT_RE = re.compile(r"umn\\(event\d\d)\.txt$", re.I)
_TAG_RE = re.compile(rb"<(speech|char|msg|mot|/speech|memo|/memo)>")
_ESC_RE = re.compile(rb"\\([0-7]*)")
_LITERAL_AT = re.compile(r"(@(?![*()<>]))")    # an at-sign that is not a loader code
AT_ESCAPE = b"#4a"                            # how retail spells one (bunnie@namco.com)


# -- byte walking ------------------------------------------------------------------

def _units(t: bytes, start: int = 0, end: Optional[int] = None) -> List[Tuple[int, int, str]]:
    """(start, end, kind) per drawable unit: 'nl', 'nul', 'ctl' (with
    operands) or 'ch' (ASCII byte or two-byte code), the way the engine steps."""
    end = len(t) if end is None else end
    out = []
    i = start
    while i < end:
        c = t[i]
        if c == 0x0A:
            out.append((i, i + 1, "nl"))
            i += 1
        elif c == 0x00:
            out.append((i, i + 1, "nul"))
            i += 1
        elif c < 0x20:
            n = min(control_length(t, i), end - i)
            out.append((i, i + n, "ctl"))
            i += n
        elif c >= 0x80 and i + 1 < end:
            out.append((i, i + 2, "ch"))
            i += 2
        else:
            out.append((i, i + 1, "ch"))
            i += 1
    return out


def page_text_end(data: bytes) -> int:
    """Offset of the page's terminating NUL (control operands skipped)."""
    i = UML_HEADER
    while i < len(data) and data[i] != 0:
        i += control_length(data, i) if data[i] < 0x20 else 1
    return i


def _lines(t: bytes) -> List[Tuple[int, int]]:
    spans, s = [], 0
    for a, b, k in _units(t):
        if k == "nl":
            spans.append((s, a))
            s = b
    spans.append((s, len(t)))
    return spans


def _strip(t: bytes, a: int, b: int, lead_nl: bool = True) -> Tuple[int, int]:
    """Narrow [a, b) past leading/trailing control codes (and newlines)."""
    u = _units(t, a, b)
    skip = ("nl", "ctl") if lead_nl else ("ctl",)
    i, j = 0, len(u)
    while i < j and u[i][2] in skip:
        i += 1
    while j > i and u[j - 1][2] in skip:
        j -= 1
    if i >= j:
        return (u[i][0] if i < len(u) else b), (u[i][0] if i < len(u) else b)
    return u[i][0], u[j - 1][1]


def at_codes(t: bytes) -> List[bytes]:
    """``@`` loader codes of page text (outside control operands)."""
    out, i = [], 0
    while i < len(t):
        c = t[i]
        if c < 0x20 and c != 0x0A:
            i += control_length(t, i)
        elif c == 0x40:
            out.append(t[i:i + 2])
            i += 2
        elif c >= 0x80:
            i += 2
        else:
            i += 1
    return out


# -- UML pages ---------------------------------------------------------------------

def rebuild_page(data: bytes, text: bytes) -> bytes:
    """Page with its text (header..terminator, exclusive) replaced. The
    pictures stay where they are while the text fits in front of them, and
    move to the next 16-byte boundary (picture table fixed up) when not."""
    end = page_text_end(data)
    img = struct.unpack_from("<I", data, 0x20)[0]
    if not (end < img <= len(data)):
        img = len(data)
    need = UML_HEADER + len(text) + 1
    if need <= img:
        return data[:UML_HEADER] + text + bytes(img - need + 1) + data[img:]
    new_img = (need + UML_ALIGN - 1) // UML_ALIGN * UML_ALIGN
    delta = new_img - img
    hdr = bytearray(data[:UML_HEADER])
    for off in PIC_TABLE:
        ptr = struct.unpack_from("<I", hdr, off)[0]
        if ptr and img <= ptr < len(data):
            struct.pack_into("<I", hdr, off, ptr + delta)
    return bytes(hdr) + text + bytes(new_img - UML_HEADER - len(text)) + data[img:]


def page_lines(data: bytes) -> Tuple[bytes, bytes]:
    t = data[UML_HEADER:page_text_end(data)]
    ls = _lines(t)
    l1 = t[ls[0][0]:ls[0][1]]
    l2 = t[ls[1][0]:ls[1][1]] if len(ls) > 1 else b""
    return l1, l2


# -- header lists ------------------------------------------------------------------

@dataclass
class ListRecord:
    off: int
    size: int
    id: int
    raw: bytes            # the whole record including its size word


def parse_list(data: bytes) -> Tuple[List[ListRecord], bytes]:
    """-> (records, trailing bytes after the zero size word)."""
    recs, off = [], 0
    while off + 4 <= len(data):
        size = struct.unpack_from("<i", data, off)[0]
        if size == 0:
            return recs, data[off + 4:]
        rid = struct.unpack_from("<i", data, off + 4)[0]
        recs.append(ListRecord(off, size, rid, data[off:off + 4 + size]))
        off += 4 + size
    raise ValueError("header list without its end word")


def list_record(rid: int, page: bytes) -> bytes:
    l1, l2 = page_lines(page)
    body = struct.pack("<i", rid) + page[:UML_HEADER] + l1 + b"\x00" + l2 + b"\x00"
    body += bytes(-len(body) % 4)
    return struct.pack("<i", len(body)) + body


def rebuild_list(data: bytes, pages: Dict[int, bytes]) -> bytes:
    """Header list with the records of ``pages`` (id -> page bytes) rebuilt;
    every other record is copied verbatim, in the original order."""
    recs, trail = parse_list(data)
    out = bytearray()
    for r in recs:
        out += list_record(r.id, pages[r.id]) if r.id in pages else r.raw
    return bytes(out) + bytes(4) + trail


# -- db_fileno.txt -----------------------------------------------------------------

def db_sections(text: bytes) -> Dict[int, Tuple[str, int]]:
    """id -> (section letter from its '//<TAB>X' line, position in section)."""
    out: Dict[int, Tuple[str, int]] = {}
    letter, pos = "", 0
    for line in text.split(b"\n"):
        s = line.strip()
        if s.startswith(b"//"):
            letter, pos = s[2:].strip().decode("ascii", "replace"), 0
        elif s[:1].isdigit():
            m = re.match(rb"\d+", s)
            out[int(m.group())] = (letter, pos)
            pos += 1
    return out


# -- slots -------------------------------------------------------------------------

@dataclass
class Slot:
    row: dict
    path: str             # TOC path of the object the span lives in
    start: int            # span inside the object's text (page text or chat file)
    end: int
    raw: bytes


@dataclass
class State:
    iso: str
    objs: Dict[str, bytes] = field(default_factory=dict)      # TOC path -> payload
    slots: Dict[str, Slot] = field(default_factory=dict)      # row id -> slot
    rows: List[dict] = field(default_factory=list)


_STATES: Dict[str, State] = {}
_LAST: Optional[State] = None


def _row(rid, cat, grp, ord_, src, ctx, ctl, measure, lim=None, hide=None, lock=None):
    return {"id": rid, "fam": FAM, "cat": cat, "grp": grp, "ord": ord_, "src": src,
            "ctx": ctx, "font": FIELD, "ctl": ctl, "measure": measure, "px": None,
            "lim": lim, "lock": lock, "hide": hide, "dup": 0}


def _text_of(path: str, data: bytes) -> bytes:
    """The editable text an object's spans index into."""
    return data[UML_HEADER:page_text_end(data)] if path.lower().endswith(".uml") else data


def _mail_slots(path: str, num: str, data: bytes) -> List[Slot]:
    t = _text_of(path, data)
    ls = _lines(t)
    grp = f"umn/{num}"
    out = []
    dummy = t[ls[0][0]:ls[0][1]].strip() == b"Subject: Dummy"
    hide = "placeholder page ('Dummy')" if dummy else None
    if len(ls) < 3 or b"Subject: " not in t[ls[0][0]:ls[0][1]] \
            or b"Sender: " not in t[ls[1][0]:ls[1][1]]:
        a, b = _strip(t, 0, len(t))
        out.append(Slot(_row(f"umn:mail:{num}:body", CAT_MAIL, grp, 2,
                             txcodec.to_display(t[a:b], "c0", FIELD, FONT1), "body", "c0",
                             None, lock="page without Subject/Sender lines"), path, a, b, t[a:b]))
        return out
    for k, (label, skip, ctx) in enumerate(((b"Subject: ", SUBJECT_SKIP, "subject"),
                                             (b"Sender: ", SENDER_SKIP, "sender"))):
        la, lb = ls[k]
        p = t.index(label, la, lb) + len(label)
        a, b = _strip(t, p, lb, lead_nl=False)
        lim = LIST_FIELD - 1 - ((lb - la - skip) - (b - a))
        out.append(Slot(_row(f"umn:mail:{num}:{ctx}", CAT_MAIL, grp, k,
                             txcodec.to_display(t[a:b], "c0", FIELD, FONT1), ctx, "c0",
                             "font1", lim=lim, hide=hide), path, a, b, t[a:b]))
    a, b = _strip(t, ls[1][1] + 1, len(t))
    out.append(Slot(_row(f"umn:mail:{num}:body", CAT_MAIL, grp, 2,
                         txcodec.to_display(t[a:b], "c0", FIELD, FONT1), "body", "c0",
                         None, hide=hide), path, a, b, t[a:b]))
    return out


def _db_slots(path: str, num: str, data: bytes, sections) -> List[Slot]:
    t = _text_of(path, data)
    ls = _lines(t)
    letter, pos = sections.get(int(num), ("", 999))
    grp = letter or "umn"
    out = []
    a, b = _strip(t, ls[0][0], ls[0][1])
    out.append(Slot(_row(f"umn:db:{num}:title", CAT_DB, grp, pos * 2,
                         txcodec.to_display(t[a:b], "c0", FIELD, FONT1), f"{num}.uml title",
                         "c0", "font1"), path, a, b, t[a:b]))
    if len(ls) >= 3:
        a, b = _strip(t, ls[1][1] + 1, len(t))
        out.append(Slot(_row(f"umn:db:{num}:body", CAT_DB, grp, pos * 2 + 1,
                             txcodec.to_display(t[a:b], "c0", FIELD, FONT1), f"{num}.uml body",
                             "c0", "font1"), path, a, b, t[a:b]))
    return out


def _chat_slots(path: str, name: str, data: bytes) -> List[Slot]:
    out = []
    in_memo = False
    speaker = ""
    k = 0
    room = EVENT_BUFFER - len(data)
    tags = list(_TAG_RE.finditer(data))
    for m in tags:
        tag = m.group(1)
        if tag == b"memo":
            in_memo = True
        elif tag == b"/memo":
            in_memo = False
        elif tag == b"speech":
            speaker = ""
        elif tag == b"char":
            e = data.find(b"\n", m.end())
            speaker = data[m.end():e if e >= 0 else len(data)].strip().decode("ascii", "replace")
        elif tag == b"msg":
            a = m.end()
            while a < len(data) and data[a] == 0x0A:
                a += 1
            b = data.find(b"<", a)
            b = len(data) if b < 0 else b
            while b > a and data[b - 1] == 0x0A:
                b -= 1
            hide = None
            if name.lower() == "event00":
                hide = "developer test (loaded only as a fallback)"
            elif in_memo:
                hide = "inside <memo> (cut from the game)"
            raw = data[a:b]
            out.append(Slot(_row(f"umn:chat:{name}:{k}", CAT_CHAT, f"umn/{name}", k,
                                 txcodec.to_display(raw, "chat", FIELD, FONT1), speaker,
                                 "chat", "font1", lim=len(raw) + room, hide=hide),
                            path, a, b, raw))
            k += 1
    return out


def _load(iso_path: str) -> State:
    st = State(str(iso_path))
    iso = IsoImage(iso_path)
    try:
        for reader, e, alloc in _entries_with_alloc(iso, CHAIN):
            if e.path.lower().startswith("umn\\") and not e.compressed:
                st.objs[e.path] = b"".join(reader.read_iter(e.byte_offset, e.size))
    finally:
        iso.close()
    sections = db_sections(st.objs.get(DB_INDEX, b""))
    slots: List[Slot] = []
    for path in sorted(st.objs):
        data = st.objs[path]
        m = _MAIL_RE.search(path)
        if m:
            slots += _mail_slots(path, m.group(1), data)
            continue
        m = _DB_RE.search(path)
        if m:
            slots += _db_slots(path, m.group(1), data, sections)
            continue
        m = _CHAT_RE.search(path)
        if m:
            slots += _chat_slots(path, m.group(1).lower(), data)
    counts = Counter(s.row["src"] for s in slots)
    for s in slots:
        s.row["dup"] = counts[s.row["src"]] - 1
        st.slots[s.row["id"]] = s
        st.rows.append(s.row)
    return st


def _state(iso_path: str) -> State:
    global _LAST
    key = str(Path(iso_path).resolve())
    if key not in _STATES:
        _STATES[key] = _load(iso_path)
    _LAST = _STATES[key]
    return _LAST


# -- public interface ----------------------------------------------------------------

def rows(iso_path: str) -> List[dict]:
    """Every U.M.N. row of the disc (read-only)."""
    return [dict(r) for r in _state(iso_path).rows]


def _encode(slot: Slot, text: str) -> bytes:
    """Row text -> bytes; the source text gives back the retail bytes verbatim
    (retail spells a few letters as #XX escapes that display identically)."""
    if text == slot.row["src"]:
        return slot.raw
    if slot.row["ctl"] != "c0":
        return txcodec.from_display(text, FIELD, slot.row["ctl"], FONT1)[0]
    out = bytearray()
    for part in _LITERAL_AT.split(text):
        out += AT_ESCAPE if part == "@" else txcodec.from_display(part, FIELD, "c0", FONT1)[0]
    return bytes(out)


def _chat_escape_errors(new: bytes, old: bytes) -> List[str]:
    errs = []
    allowed = {m.group(0) for m in _ESC_RE.finditer(old)} | {b"\\37"}
    for m in _ESC_RE.finditer(new):
        if not m.group(1):
            errs.append("'\\' must start an octal escape (the chat parser writes byte 0 "
                        "and the message ends there)")
        elif m.group(0) not in allowed:
            errs.append(f"escape {m.group(0).decode()!r} is not in the source (a digit "
                        f"right after an escape is read as part of it)")
    return errs


def _row_errors(slot: Slot, text: str) -> List[str]:
    row = slot.row
    if row["lock"]:
        return [f"locked: {row['lock']}"]
    if text == row["src"]:
        return []
    res = txcodec.check(text, row["src"], FIELD, row["ctl"], limit_bytes=row["lim"]
                        if row["ctl"] != "chat" else None, face=FONT1)
    errs = list(res["errors"])
    if not res["ok"]:
        return errs
    new = _encode(slot, text)
    if row["ctl"] == "c0":
        if any(k == "nul" for _, _, k in _units(new)):
            errs.append("byte 00 would end the page here")
        if row["ctx"] in ("subject", "sender") or row["ctx"].endswith("title"):
            if b"\n" in new:
                errs.append("must stay on one line (the list copies one line)")
        a_new, a_old = Counter(at_codes(new)), Counter(at_codes(slot.raw))
        if a_new != a_old:
            errs.append(f"'@' codes differ from the source ({sorted(c.decode('latin-1') for c in a_old.elements())} "
                        f"expected)")
    else:
        if b"<" in new:
            errs.append("'<' ends a chat message")
        if 0 in new:
            errs.append("byte 00 ends the message")
        errs += _chat_escape_errors(new, slot.raw)
    return errs


def _new_objects(st: State, edits: Dict[str, str]) -> Dict[str, bytes]:
    """TOC path -> rebuilt payload for every object an edit touches (pages,
    chat files and the header lists), without validation."""
    by_obj: Dict[str, List[Tuple[Slot, bytes]]] = {}
    for rid, text in edits.items():
        slot = st.slots.get(rid)
        if slot is None:
            continue
        by_obj.setdefault(slot.path, []).append((slot, _encode(slot, text)))
    out: Dict[str, bytes] = {}
    mail_pages: Dict[int, bytes] = {}
    db_pages: Dict[int, bytes] = {}
    for path, items in by_obj.items():
        data = st.objs[path]
        t = _text_of(path, data)
        items.sort(key=lambda x: x[0].start)
        buf, pos = bytearray(), 0
        for slot, new in items:
            buf += t[pos:slot.start] + new
            pos = slot.end
        buf += t[pos:]
        if path.lower().endswith(".uml"):
            new_obj = data if bytes(buf) == t else rebuild_page(data, bytes(buf))
            num = int(re.search(r"(\d{4})\.uml$", path, re.I).group(1))
            if new_obj != data:
                (db_pages if num >= 9000 else mail_pages)[num] = new_obj
        else:
            new_obj = bytes(buf)
        if new_obj != data:
            out[path] = new_obj
    for lst, pages in ((MAIL_LIST, mail_pages), (DB_LIST, db_pages)):
        if pages and lst in st.objs:
            new = rebuild_list(st.objs[lst], pages)
            if new != st.objs[lst]:
                out[lst] = new
    return out


def _validate(st: State, edits: Dict[str, str]) -> Dict[str, List[str]]:
    errors: Dict[str, List[str]] = {}
    mine = {k: v for k, v in edits.items() if k.startswith("umn:")}
    for rid, text in mine.items():
        slot = st.slots.get(rid)
        if slot is None:
            errors[rid] = ["unknown U.M.N. row id"]
            continue
        e = _row_errors(slot, text)
        if e:
            errors[rid] = e
    if errors:
        return errors
    # file-level limits, with every edit in place
    for path, new in _new_objects(st, mine).items():
        if _CHAT_RE.search(path) and len(new) > EVENT_BUFFER:
            msg = (f"{path} would be {len(new)} bytes; the chat buffer holds "
                   f"{EVENT_BUFFER} (over by {len(new) - EVENT_BUFFER})")
            for rid in mine:
                if st.slots[rid].path == path:
                    errors.setdefault(rid, []).append(msg)
    return errors


def validate(edits: Dict[str, str], iso_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Errors per row id (empty dict = everything buildable). Uses the rows of
    the last ``rows()`` call unless ``iso_path`` is given."""
    st = _state(iso_path) if iso_path else _LAST
    if st is None:
        return {k: ["U.M.N. rows not loaded (call rows(iso) first)"]
                for k in edits if k.startswith("umn:")}
    return _validate(st, edits)


def apply(out_iso: str, retail_iso: str, edits: Dict[str, str],
          log: Callable[[str], None] = print) -> List[str]:
    """Write every U.M.N. edit into ``out_iso`` (a fresh clone of retail) in
    place. Originals always come from ``retail_iso``. Raises TxBuildError and
    writes nothing when any edit is invalid."""
    if Path(out_iso).resolve() == Path(retail_iso).resolve():
        raise TxBuildError("refusing to write the retail ISO itself")
    st = _state(retail_iso)
    mine = {k: v for k, v in edits.items() if k.startswith("umn:")}
    errors = _validate(st, mine)
    if errors:
        lines = [f"{rid}: {e}" for rid, es in sorted(errors.items()) for e in es]
        raise TxBuildError(f"{len(errors)} U.M.N. row(s) not buildable:\n  " + "\n  ".join(lines[:40]))
    new = _new_objects(st, mine)
    msgs: List[str] = []
    if not new:
        msgs.append(f"U.M.N.: {len(mine)} edit(s), no object changed")
        log(msgs[-1])
        return msgs
    reports = patch_iso(out_iso, {(CHAIN, p): b for p, b in sorted(new.items())},
                        verbose=False, grow=True)
    for r in reports:
        where = f", relocated to sector {r.relocated_lba}" if r.relocated_lba is not None else ""
        msgs.append(f"U.M.N.: {r.path} {r.old_size} -> {r.new_size} bytes{where}")
        log(msgs[-1])
    msgs.append(f"U.M.N.: {len(mine)} edit(s) -> {len(reports)} object(s) written")
    log(msgs[-1])
    return msgs


# -- selftest ----------------------------------------------------------------------

FR = ("« Élève, à la fenêtre de l'hôtel, ça goûte où Noël a bu ; cœur naïf, "
      "île Ève. » À Ç ô û ù ï î ë è ê â ")
ES = "¡Señor! ¿Qué día es? Último camión, pingüino, mamá. "
SAMPLE_CHARS = "éèêëàâçôûùïîœ«»ÉÀÇñ¿¡áíóúü"
_PROT_RE = {"c0": re.compile(r"(?:⟦[0-9A-Fa-f]{2}⟧)+|@.|\n"),
            "chat": re.compile(r"(?:⟦[0-9A-Fa-f]{2}⟧)+|\\[0-7]*|\n")}


def _sample(src: str, ctl: str, k: int, scale: float, cap: Optional[int] = None) -> str:
    """French/Spanish text with the source's protected tokens kept in place."""
    phrase = (FR + ES) if k % 2 == 0 else (ES + FR)
    phrase = phrase[k % 17:] + phrase[:k % 17]
    out, pos, used = [], 0, 0
    parts = []
    for m in _PROT_RE[ctl].finditer(src):
        parts.append(("t", src[pos:m.start()]))
        parts.append(("p", m.group(0)))
        pos = m.end()
    parts.append(("t", src[pos:]))
    for kind, s in parts:
        if kind == "p" or not s.strip():
            out.append(s)
            continue
        n = max(4, int(len(s) * scale))
        if cap is not None:
            n = min(n, cap)
        rep = (phrase * (n // len(phrase) + 2))[used % len(phrase):][:n]
        used += n
        rep = rep.replace("\n", " ").strip() or "é"
        out.append(rep)
    return "".join(out)


def _sample_edits(st: State) -> Dict[str, str]:
    edits: Dict[str, str] = {}
    vis = [r for r in st.rows if not r["lock"]]
    for i, r in enumerate(vis):
        rid, ctx = r["id"], r["ctx"]
        pick = (i % 5 == 0 or rid in ("umn:mail:0000:subject", "umn:mail:0078:body",
                                      "umn:db:9000:title", "umn:db:9000:body",
                                      "umn:mail:0013:subject", "umn:chat:event01:0"))
        if not pick:
            continue
        if ctx in ("subject", "sender"):
            text = _sample(r["src"], "c0", i, 2.0, cap=max(4, r["lim"] // 2 - 1))
        elif ctx.endswith("title"):
            text = _sample(r["src"], "c0", i, 1.6)
        elif r["ctl"] == "chat":
            text = _sample(r["src"], "chat", i, 0.9)
        else:
            text = _sample(r["src"], "c0", i, 1.3)
        edits[rid] = text
    # every sample letter at least once, in one database title
    edits["umn:db:9001:title"] = "Coordonnées " + SAMPLE_CHARS
    return edits


def _clone(retail: str, dest: Path) -> None:
    if dest.exists():
        dest.unlink()
    if sys.platform != "darwin":
        raise RuntimeError("selftest clones with cp -c (APFS clonefile); macOS only")
    subprocess.run(["cp", "-c", retail, str(dest)], check=True)


def _scratch() -> Path:
    base = os.environ.get("TXFAM_SCRATCH") or os.environ.get("TMPDIR") or "/tmp"
    return Path(base)


def selftest(iso_path: str) -> int:
    ok = True

    def fail(msg):
        nonlocal ok
        ok = False
        print("FAIL:", msg)

    t0 = time.time()
    rs = rows(iso_path)
    t_rows = time.time() - t0
    st = _state(iso_path)
    by_cat = Counter(r["cat"] for r in rs)
    print(f"rows: {len(rs)} in {t_rows:.2f} s")
    for cat in (CAT_MAIL, CAT_DB, CAT_CHAT):
        sub = [r for r in rs if r["cat"] == cat]
        print(f"  {cat}: {len(sub)} rows, {sum(1 for r in sub if not r['hide'])} visible, "
              f"{sum(1 for r in sub if r['hide'])} hidden, {sum(1 for r in sub if r['lock'])} locked, "
              f"{len({r['grp'] for r in sub})} groups")
    ctxs = Counter(r["ctx"] if r["cat"] == CAT_MAIL else r["ctx"].split()[-1] if r["cat"] == CAT_DB
                   else "msg" for r in rs)
    print("  by kind:", dict(sorted(ctxs.items())))
    if len({r["id"] for r in rs}) != len(rs):
        fail("duplicate row ids")
    if by_cat[CAT_MAIL] != 81 * 3 or by_cat[CAT_DB] != 245 * 2:
        fail(f"expected 243 mail and 490 database rows, got {by_cat}")

    # 1. the header lists are exactly what the pages say
    for lst, rng in ((MAIL_LIST, range(0, 81)), (DB_LIST, range(9000, 9245))):
        data = st.objs[lst]
        recs, trail = parse_list(data)
        pages = {r.id: st.objs[f"umn\\{r.id:04d}.uml"] for r in recs}
        rebuilt = b"".join(list_record(r.id, pages[r.id]) for r in recs) + bytes(4) + trail
        same = rebuilt == data
        print(f"{lst}: {len(recs)} records, ids {'match' if sorted(pages) == list(rng) else 'DIFFER'}, "
              f"rebuilt from the pages {'byte-identical' if same else 'DIFFERENT'} "
              f"({len(data)} bytes, trailing {len(trail)})")
        if not same or sorted(pages) != list(rng):
            fail(f"{lst} layout")

    # 2. every row re-encodes to its retail bytes (or only differs by retail #XX)
    exact = esc = bad = 0
    for s in st.slots.values():
        try:
            b = txcodec.from_display(s.row["src"], FIELD, s.row["ctl"], FONT1)[0]
        except TxEncodeError as e:
            bad += 1
            fail(f"{s.row['id']}: source does not encode: {e}")
            continue
        if b == s.raw:
            exact += 1
        elif txcodec.to_display(b, s.row["ctl"], FIELD, FONT1) == s.row["src"]:
            esc += 1
        else:
            bad += 1
            fail(f"{s.row['id']}: source re-encodes differently")
    print(f"source round trip: {exact} byte-exact, {esc} differ only by retail #XX escapes, {bad} failed")

    # 3. splicing every span back (forced through the encoder) rebuilds every object
    forced = {}
    for s in st.slots.values():
        forced.setdefault(s.path, []).append(s)
    same = drift = 0
    for path, slots in forced.items():
        data = st.objs[path]
        t = _text_of(path, data)
        slots.sort(key=lambda x: x.start)
        buf, pos = bytearray(), 0
        for s in slots:
            buf += t[pos:s.start] + s.raw
            pos = s.end
        buf += t[pos:]
        new = rebuild_page(data, bytes(buf)) if path.endswith(".uml") else bytes(buf)
        if new == data:
            same += 1
        else:
            drift += 1
            fail(f"{path}: frame + spans do not rebuild the object")
    print(f"frame + spans rebuild: {same} objects identical, {drift} different")

    # 4. the textpack corruption case: a longer database title keeps the body
    import textpack
    d9000 = st.objs["umn\\9000.uml"]
    t_old, _ = textpack.uml_split(d9000)
    grown = textpack.uml_rebuild(d9000, t_old[:-1] + b" (longer)" + t_old[-1:])
    body_kept = b"Triple A" in grown and page_text_end(grown) - UML_HEADER == \
        page_text_end(d9000) - UML_HEADER + 9
    print(f"textpack.uml_rebuild, longer 9000 title: body {'kept' if body_kept else 'LOST'}")
    if not body_kept:
        fail("textpack.uml_rebuild still drops the tail")

    # 5. checker rules
    probes = [("umn:mail:0000:subject", "Objet\nde test", "one line"),
              ("umn:mail:0000:subject", "x" * 80, "limit"),
              ("umn:mail:0000:body", st.slots["umn:mail:0000:body"].row["src"].replace("@*", "", 1)
               if "@*" in st.slots["umn:mail:0000:body"].row["src"] else "a@b", "@"),
              ("umn:chat:event01:0", "Bonjour <b>", "'<'"),
              ("umn:chat:event01:0", st.slots["umn:chat:event01:0"].row["src"] + "\\q", "octal"),
              ("umn:db:9000:title", "naïve ō", "position")]
    for rid, text, needle in probes:
        e = validate({rid: text}, iso_path).get(rid, [])
        if not any(needle in x for x in e):
            fail(f"checker missed {needle!r} on {rid}: {e}")
    s10 = st.slots["umn:mail:0010:sender"]
    if validate({s10.row["id"]: "bunnie@namco.fr"}, iso_path) or \
            _encode(s10, "bunnie@namco.fr") != b"bunnie#4anamco.fr":
        fail("literal '@' in page text not written as #4a")
    big = {f"umn:chat:event01:{k}": "é" * 2000 for k in range(2)}
    if not any("chat buffer" in x for es in validate(big, iso_path).values() for x in es):
        fail("chat file limit not enforced")
    print(f"checker probes: {len(probes) + 2} rule(s) exercised")

    # 6. identity + French/Spanish build on an APFS clone
    clone = _scratch() / f"txfam_umn_selftest_{os.getpid()}.iso"
    try:
        _clone(iso_path, clone)
        ident = {r["id"]: r["src"] for r in rs if not r["lock"]}
        t1 = time.time()
        apply(str(clone), iso_path, ident, log=lambda m: None)
        st_id = _load(str(clone))
        diff = [p for p in st.objs if st_id.objs.get(p) != st.objs[p]]
        print(f"identity apply ({len(ident)} rows set to their own source): "
              f"{len(diff)} object(s) differ from retail ({time.time() - t1:.2f} s)")
        if diff:
            fail(f"identity apply changed {diff[:5]}")

        edits = _sample_edits(st)
        errs = validate(edits, iso_path)
        if errs:
            for rid, es in list(errs.items())[:10]:
                fail(f"sample edit {rid}: {es}")
        else:
            t1 = time.time()
            msgs = apply(str(clone), iso_path, edits, log=lambda m: None)
            t_apply = time.time() - t1
            reloc = [m for m in msgs if "relocated" in m]
            t1 = time.time()
            st2 = _load(str(clone))
            t_reread = time.time() - t1
            got = {r["id"]: r for r in st2.rows}
            ed_ok = sum(1 for rid, text in edits.items() if got[rid]["src"] == text)
            for rid, text in edits.items():
                if got[rid]["src"] != text:
                    fail(f"{rid}: re-read {got[rid]['src'][:60]!r} != {text[:60]!r}")
                    break
            untouched = [rid for rid in st.slots if rid not in edits]
            un_ok = sum(1 for rid in untouched if st2.slots[rid].raw == st.slots[rid].raw)
            if un_ok != len(untouched):
                fail(f"{len(untouched) - un_ok} untouched rows changed")
            # pictures and frames survive, lists stay in sync with the pages
            pics = 0
            for path, data in st.objs.items():
                if not path.endswith(".uml"):
                    continue
                new = st2.objs[path]
                for off in PIC_TABLE:
                    p_old = struct.unpack_from("<I", data, off)[0]
                    p_new = struct.unpack_from("<I", new, off)[0]
                    if p_old:
                        if data[p_old:p_old + 64] != new[p_new:p_new + 64] or \
                                len(data) - p_old != len(new) - p_new:
                            fail(f"{path}: picture at {p_old:#x} lost")
                        pics += 1
            for lst in (MAIL_LIST, DB_LIST):
                recs, trail = parse_list(st2.objs[lst])
                rebuilt = b"".join(list_record(r.id, st2.objs[f"umn\\{r.id:04d}.uml"])
                                   for r in recs) + bytes(4) + trail
                if rebuilt != st2.objs[lst]:
                    fail(f"{lst} out of sync with the pages after the build")
            sub = st2.objs[MAIL_LIST]
            want = edits["umn:mail:0000:subject"]
            l1 = parse_list(sub)[0][0].raw[8 + UML_HEADER:].split(b"\x00")[0]
            in_list = txcodec.to_display(l1[SUBJECT_SKIP + 2:], "c0", FIELD, FONT1) == want
            if not in_list:
                fail("header.lst subject of mail 0000 not translated")
            changed = sorted({st.slots[r].path for r in edits} | {MAIL_LIST, DB_LIST})
            stray = [p for p in st.objs if p not in changed and st2.objs[p] != st.objs[p]]
            if stray:
                fail(f"objects changed that no edit touches: {stray[:5]}")
            used = set("".join(edits.values()))
            print(f"French/Spanish build: {len(edits)} edits "
                  f"({Counter(st.slots[r].row['cat'] for r in edits)}), "
                  f"{len(msgs) - 1} objects written, {len(reloc)} relocated, "
                  f"apply {t_apply:.2f} s, re-read {t_reread:.2f} s")
            for m in msgs:
                if ".lst" in m:
                    print("  " + m)
            print(f"  edited rows decode back exactly: {ed_ok}/{len(edits)}; untouched rows "
                  f"byte-identical: {un_ok}/{len(untouched)}; pictures intact: {pics}; "
                  f"lists in sync; header.lst subject translated: {in_list}")
            print(f"  sample letters present: "
                  f"{''.join(c for c in SAMPLE_CHARS if c in used)} "
                  f"(missing: {''.join(c for c in SAMPLE_CHARS if c not in used) or 'none'})")
    finally:
        if clone.exists():
            clone.unlink()
    print(f"clone deleted: {not clone.exists()}; total {time.time() - t0:.1f} s")
    print("selftest OK" if ok else "selftest FAILED")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("selftest")
    ps.add_argument("--iso", required=True)
    pd = sub.add_parser("dump")
    pd.add_argument("--iso", required=True)
    pd.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "dump":
        Path(a.out).write_text(json.dumps(rows(a.iso), ensure_ascii=False, indent=1),
                               encoding="utf-8")
        print(f"wrote {a.out}")
        return 0
    return selftest(a.iso)


if __name__ == "__main__":
    sys.exit(main())
