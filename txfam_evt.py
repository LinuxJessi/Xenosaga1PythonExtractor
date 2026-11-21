"""txfam_evt.py — scene and cutscene dialogue for the translation editor.

Every line spoken on a field map or in a real-time cutscene is a
``CONSTANT_String`` literal in one of the ``.evt`` Java class files
(docs/JAVA.md); ``evttext.py`` already rebuilds those containers with
literals of any length. What it cannot tell is *which* of the 44,605
literals a player ever sees: 62% are debug ``System.println`` text and
Japanese developer notes, and some 6,000 are names the scripts look up at
run time (``Chr.start("act18_shion")``, ``talkto("Talk_npc1")``, XEVE*
story flags) that break the game when translated. The pool order is
alphabetical, so it says nothing about who speaks or in what order.

So this module reads the bytecode. For each method it records every
``ldc`` of a string and the call that consumes it (``uses``), then:

* **drawn or not.** ``Window.print`` and ``Menu.addItem/addQuery`` draw
  their argument. A script method draws a String parameter when it hands
  that parameter (``aload n``) on to a drawing call; this is followed to a
  fixpoint over every class in the container. That is how
  ``SCE01001.msg_print(String speaker, String text)`` is known to draw
  ``text`` and ignore ``speaker`` (its body is just ``msg.print(text)``):
  the 【シオン】 arguments are never shown in the USA build, so they are
  listed locked, never offered for translation.
* **Field maps** build their messages in ``<init>`` as String arrays,
  ``[ "/[label(Allen)]", "<text>", "/[waitkey(64)]/[close()]" ]``; the
  ``ldc`` order is the script order and the label is the speaker.
* **Cutscenes** call ``MSG(int, Chr face, ..., String)`` with the face of
  ``this.allen.face`` (class ``SCE01007A1$Allen``): the owner's class name
  is the speaker. Calls that pass a speaker string (``msg_print``,
  ``_MSG``) give the string itself; it is Japanese and the disc holds no
  table that maps it to an English name, so the editor shows it as is.
  ``Window.print`` continuation pages inherit the last speaker.
* **Locks**: identifiers (``Chr.start``/``Thread.create``/``talkto``
  names, file names, XEVE* flags), control-only strings, the speaker
  arguments above, and any Utf8 entry that is *also* a class, member name
  or descriptor (``"F"``, ``"NPC1_TALK1"``): editing it would rename a
  member or corrupt a type.
* **Hidden** (still editable): debug prints, debug banners, Japanese
  developer text.

Speaker labels get one row per ``/[label(X)]`` pool entry whose source is
just ``X``; apply wraps the translation back into ``/[label(...)]``.

Text goes through ``txcodec`` with the face that draws the container
(``txcodec.evt_face(chain, path, iso)``): font0 and font1 disagree on the
grave/acute cells. The face comes from the disc's scene planner files: a
container run by an ``ev####.txt`` scene is drawn with font0 (all 181 SCE*
cutscenes, chain 1 and chain 2), one run by ``cf####.txt`` with font1 (the
386 ST* maps); see ``txcodec.scene_faces`` for the decomp trace.

Speaker names (``/[label(X)]``) are printed by ``xglFontPrint`` like the
dialogue (same face, same ``#XX`` handling), but ``MSG_convert`` tokenises
the tag, so ``label_problems`` refuses names with ``, ( ) ; " [ ]``, a
leading digit / ``$``, or over 248 bytes. Field-map rows carry no width
guide (``TWIN_popScene`` wraps at the window width); cutscene rows get the
scene window's 480 px (``TWIN_SCENE_WIDTH``) as a guide. Choice rows
(``Menu.addItem``) must keep their number of lines: one line is one choice.
Every edit is rebuilt from the RETAIL container (``evttext.rebuild``) and
all containers go into the output ISO with one
``repack.patch_iso(..., grow=True)``.

    python3 txfam_evt.py selftest --iso RETAIL
    python3 txfam_evt.py rows --iso RETAIL [--out rows.json]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

import evttext as et
import txcodec as tc
from iso9660 import IsoImage

FAM = "evt"
CAT_FIELD = "Dialogue — field maps"
CAT_CUT = "Dialogue — cutscenes"
CAT_SPK = "Speaker names"
CAT_MENU = "Choices & menus (scripts)"
# cutscene width guide: WIN_initScene's window width (TWIN_SCENE_WIDTH 0x1E0,
# src/main/twsys_init.c); 6% of retail cutscene lines are wider (the window
# wraps them), against 36% for the old 370 px Hindi tuning value
PX_GUIDE = 480

# -- bytecode walk ------------------------------------------------------------------

# JVM 45.3 instruction lengths; tableswitch/lookupswitch/wide are handled in the walk
_OPLEN = [1] * 256
for _op in (0x10, 0x12, 0x15, 0x16, 0x17, 0x18, 0x19, 0x36, 0x37, 0x38, 0x39, 0x3A, 0xA9, 0xBC):
    _OPLEN[_op] = 2
for _op in (0x11, 0x13, 0x14, 0x84, 0x99, 0x9A, 0x9B, 0x9C, 0x9D, 0x9E, 0x9F, 0xA0, 0xA1,
            0xA2, 0xA3, 0xA4, 0xA5, 0xA6, 0xA7, 0xA8, 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7,
            0xB8, 0xBB, 0xBD, 0xC0, 0xC1, 0xC6, 0xC7):
    _OPLEN[_op] = 3
_OPLEN[0xC5] = 4
for _op in (0xB9, 0xC8, 0xC9):
    _OPLEN[_op] = 5
_INVOKES = (0xB6, 0xB7, 0xB8, 0xB9)
_ASTORES = {0x3A, 0x4B, 0x4C, 0x4D, 0x4E}
_STRING = "Ljava/lang/String;"

MethodKey = Tuple[str, str, str]           # (class, name, descriptor)


def _params(desc: str) -> List[str]:
    """Parameter type descriptors of a method descriptor."""
    out, i, s = [], 1, desc
    while i < len(s) and s[i] != ")":
        j = i
        while s[j] == "[":
            j += 1
        if s[j] == "L":
            j = s.index(";", j)
        out.append(s[i:j + 1])
        i = j + 1
    return out


def _local_to_param(desc: str, static: bool) -> Dict[int, int]:
    """Local variable slot -> parameter position."""
    out, slot = {}, 0 if static else 1
    for k, p in enumerate(_params(desc)):
        out[slot] = k
        slot += 2 if p in ("J", "D") else 1
    return out


@dataclass
class Call:
    caller: MethodKey
    mindex: int
    pc: int
    callee: MethodKey
    ldcs: Tuple[Tuple[int, int], ...]      # (pool, pc) strings loaded for this call
    aloads: Tuple[int, ...]                # local slots pushed with aload since the last call
    face_owner: Tuple[str, str] = ("", "")   # (descriptor, field) of the object whose Chr was passed
    speaker: str = ""


@dataclass
class Use:
    method: str
    mindex: int
    pc: int
    pool: int
    sink: str              # "Cls.name(desc)", "aastore", "putfield X", "areturn", "astore", "(none)"
    final: str             # first real consumer past StringBuffer plumbing
    call: Optional[Call] = None
    pos: int = -1          # parameter position in ``call.callee`` (-1 = unknown / not a call)


@dataclass
class ClassInfo:
    row: int
    name: str              # e.g. "SCE01007A1$Allen" (no .class)
    entries: list
    uses: List[Use] = field(default_factory=list)
    calls: List[Call] = field(default_factory=list)
    shared: Set[int] = field(default_factory=set)       # Utf8 indexes used as names/descriptors
    methods: List[Tuple[str, str, bool]] = field(default_factory=list)   # (name, desc, static)
    fields: List[Tuple[str, str]] = field(default_factory=list)          # (name, desc)


def _utf(entries, i: int) -> str:
    e = entries[i] if 0 < i < len(entries) else None
    if not e or e[0] != 1:
        return ""
    return e[1].rstrip(b"\0").decode("euc_jp", "replace")


def _member(entries, ref: int) -> Tuple[str, str, str]:
    cidx, nt = struct.unpack(">HH", entries[ref][1])
    cname = _utf(entries, struct.unpack(">H", entries[cidx][1])[0])
    ni, di = struct.unpack(">HH", entries[nt][1])
    return cname, _utf(entries, ni), _utf(entries, di)


def _short(cls_name: str) -> str:
    return cls_name.rsplit("/", 1)[-1]


def walk_class(row: int, name: str, cls: bytes) -> ClassInfo:
    entries, p = et.parse_pool(cls)
    info = ClassInfo(row, name, entries)

    def u16(o):
        return struct.unpack_from(">H", cls, o)[0]

    def u32(o):
        return struct.unpack_from(">I", cls, o)[0]

    shared = info.shared
    for e in entries[1:]:
        if not e:
            continue
        if e[0] == 7:
            shared.add(struct.unpack(">H", e[1])[0])
        elif e[0] == 12:
            shared.update(struct.unpack(">HH", e[1]))

    def attrs(q: int, on_code=None) -> int:
        n = u16(q)
        q += 2
        for _ in range(n):
            ani, alen = u16(q), u32(q + 2)
            shared.add(ani)
            aname = _utf(entries, ani)
            body = q + 6
            if aname == "SourceFile":
                shared.add(u16(body))
            elif aname == "Code":
                clen = u32(body + 4)
                if on_code:
                    on_code(cls[body + 8:body + 8 + clen])
                ex = body + 8 + clen
                attrs(ex + 2 + 8 * u16(ex))
            elif aname == "LocalVariableTable":
                for k in range(u16(body)):
                    shared.add(u16(body + 2 + 10 * k + 4))
                    shared.add(u16(body + 2 + 10 * k + 6))
            q += 6 + alen
        return q

    p += 6
    p += 2 + 2 * u16(p)                    # interfaces
    n = u16(p)
    p += 2
    for _ in range(n):                     # fields
        info.fields.append((_utf(entries, u16(p + 2)), _utf(entries, u16(p + 4))))
        shared.add(u16(p + 2))
        shared.add(u16(p + 4))
        p = attrs(p + 6)
    n = u16(p)
    p += 2
    for mi in range(n):                    # methods
        acc, ni, di = u16(p), u16(p + 2), u16(p + 4)
        shared.update((ni, di))
        mname, mdesc = _utf(entries, ni), _utf(entries, di)
        info.methods.append((mname, mdesc, bool(acc & 0x0008)))
        key = (name, mname, mdesc)
        p = attrs(p + 6, lambda code, k=key, i=mi: _walk_code(info, k, i, code))
    attrs(p)
    return info


def _walk_code(info: ClassInfo, key: MethodKey, mindex: int, code: bytes) -> None:
    entries = info.entries
    mname = key[1]
    pending: List[Tuple[int, int]] = []        # (pool, pc) awaiting a consumer
    carry: List[Tuple[int, int, str]] = []     # went through StringBuffer plumbing
    aloads: List[int] = []
    face_owner = ("", "")
    prev = ("", "")                            # (descriptor, name) of the previous getfield
    pc, n = 0, len(code)

    def settle(sink: str, call: Optional[Call] = None):
        nonlocal pending
        for si, spc in pending:
            info.uses.append(Use(mname, mindex, spc, si, sink, sink, call))
        pending = []

    while pc < n:
        op = code[pc]
        if op in (0x12, 0x13):
            idx = code[pc + 1] if op == 0x12 else struct.unpack_from(">H", code, pc + 1)[0]
            e = entries[idx] if idx < len(entries) else None
            if e and e[0] == 8:
                pending.append((struct.unpack_from(">H", e[1])[0], pc))
        elif op == 0x19:
            aloads.append(code[pc + 1])
        elif 0x2A <= op <= 0x2D:
            aloads.append(op - 0x2A)
        elif op in (0xB2, 0xB4):
            ref = struct.unpack_from(">H", code, pc + 1)[0]
            _, fname, fdesc = _member(entries, ref)
            if fdesc == "Lxeno/Chr;" and prev[0].startswith("L"):
                face_owner = prev
            prev = (fdesc, fname)
        elif op in _INVOKES:
            ref = struct.unpack_from(">H", code, pc + 1)[0]
            cname, mn, desc = _member(entries, ref)
            callee = (_short(cname), mn, desc)
            label = f"{callee[0]}.{mn}{desc}"
            if cname == "java/lang/StringBuffer" or (cname == "java/lang/String" and mn == "valueOf"):
                carry += [(si, spc, label) for si, spc in pending]
                pending = []
            else:
                call = Call(key, mindex, pc, callee, tuple(pending), tuple(aloads), face_owner)
                info.calls.append(call)
                args = desc.split(")")[0]
                if carry and (_STRING in args or "Ljava/lang/Object;" in args):
                    for si, spc, first in carry:
                        info.uses.append(Use(mname, mindex, spc, si, first, label))
                    carry = []
                settle(label, call)
            aloads, face_owner, prev = [], ("", ""), ("", "")
        elif op == 0x53:
            settle("aastore")
        elif op in (0xB3, 0xB5):
            ref = struct.unpack_from(">H", code, pc + 1)[0]
            settle("putfield " + _member(entries, ref)[1])
        elif op == 0xB0:
            settle("areturn")
        elif op in _ASTORES:
            settle("astore")
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
    settle("(none)")
    for si, spc, first in carry:
        info.uses.append(Use(mname, mindex, spc, si, first, "(none)"))


def _trailing_strings(desc: str) -> List[int]:
    """Positions of the String parameters at the end of the list (stops at
    the first non-String from the right)."""
    ps = _params(desc)
    out = []
    for k in range(len(ps) - 1, -1, -1):
        if ps[k] != _STRING:
            break
        out.append(k)
    return out[::-1]


def _is_drawing_api(callee: MethodKey) -> bool:
    c, m, d = callee
    return _STRING in d.split(")")[0] and (
        (c == "Window" and m == "print") or (c == "Menu" and m in ("addItem", "addQuery")))


def drawn_params(infos: List[ClassInfo]) -> Dict[MethodKey, Set[int]]:
    """Which String parameters each method draws, to a fixpoint over the
    container's own methods (a method draws a parameter it passes on, via
    ``aload``, to a call that draws it)."""
    statics = {(ci.name, m, d): s for ci in infos for m, d, s in ci.methods}
    drawn: Dict[MethodKey, Set[int]] = defaultdict(set)
    calls = [c for ci in infos for c in ci.calls]
    changed = True
    while changed:
        changed = False
        for c in calls:
            if c.caller not in statics:
                continue
            target = {k for k in range(len(_params(c.callee[2])))} if _is_drawing_api(c.callee) \
                else drawn.get(c.callee, set())
            if not target:
                continue
            tail = _trailing_strings(c.callee[2])
            slots = list(c.aloads[-len(tail):]) if tail else []
            l2p = _local_to_param(c.caller[2], statics[c.caller])
            for pos, slot in zip(tail[len(tail) - len(slots):], slots):
                if pos in target and slot in l2p and _params(c.caller[2])[l2p[slot]] == _STRING:
                    if l2p[slot] not in drawn[c.caller]:
                        drawn[c.caller].add(l2p[slot])
                        changed = True
    return drawn


def walk_container(data: bytes) -> List[ClassInfo]:
    out = []
    for ri, (no, nl, do, ds) in enumerate(et.fl00_entries(data)):
        cls = data[do:do + ds]
        if ds <= 24 or cls[:4] != et.CAFEBABE:
            continue
        name = data[no:no + nl].rstrip(b"\x00").decode("ascii", "replace")
        out.append(walk_class(ri, name[:-6] if name.endswith(".class") else name, cls))
    # speaker of a face: the owner object's class (SCE01007A1$Allen -> "Allen") when
    # only one field has that class, else the owner field's name (shion, kosmos)
    per_desc: Dict[str, Set[str]] = defaultdict(set)
    for ci in out:
        for fname, fdesc in ci.fields:
            per_desc[fdesc].add(fname)
    for ci in out:
        for c in ci.calls:
            desc, fname = c.face_owner
            if desc:
                cls_name = desc[1:-1].rsplit("/", 1)[-1].rsplit("$", 1)[-1]
                c.speaker = cls_name if len(per_desc.get(desc, ())) <= 1 else fname
    # parameter position of each ldc in its call (right-aligned to the trailing Strings)
    for ci in out:
        for u in ci.uses:
            c = u.call
            if c is None:
                continue
            tail = _trailing_strings(c.callee[2])
            pools = [si for si, _ in c.ldcs]
            k = len(pools) - 1 - pools[::-1].index(u.pool) if u.pool in pools else -1
            off = len(tail) - len(pools)
            if k >= 0 and 0 <= off + k < len(tail):
                u.pos = tail[off + k]
    return out


# -- classification ----------------------------------------------------------------

_ID_RE = re.compile(r"^[A-Za-z0-9_$./\\:\-]+$")
_TAG_RE = re.compile(r"/\[[^\]]*\]")
_LABEL_RE = re.compile(r"^/\[label\((.*?)(\)?\])$", re.S)
_JP_RE = re.compile(r"[぀-ヿ一-鿿＀-￯]")
_SHARED_REASON = "also a class / member name or descriptor in this class (P2)"


def _label_parts(raw: bytes) -> Optional[Tuple[bytes, bytes, bytes]]:
    """``/[label(X)]`` -> (prefix, X, suffix); retail also has ``/[label(Bunnie]``."""
    body = raw[:-1] if raw.endswith(b"\0") else raw
    if not body.startswith(b"/[label(") or not body.endswith(b"]") or b"]" in body[8:-1]:
        return None
    inner = body[8:-1]
    if inner.endswith(b")"):
        return b"/[label(", inner[:-1], b")]"
    return b"/[label(", inner, b"]"


def _kind(path: str) -> str:
    stem = path.rsplit("\\", 1)[-1]
    if stem.upper().startswith("SCE"):
        return "cut"
    if stem.upper().startswith("ST"):
        return "field"
    return "other"


def _stem(path: str) -> str:
    s = path.rsplit("\\", 1)[-1]
    return s[:-4] if s.lower().endswith(".evt") else s


def _row_id(chain: int, path: str, ri: int, pi: int) -> str:
    return f"evt:{chain}|{path}|{ri}|{pi}"


def parse_id(rid: str) -> Tuple[int, str, int, int]:
    if not rid.startswith("evt:"):
        raise ValueError(f"not an evt id: {rid!r}")
    chain, path, ri, pi = rid[4:].split("|")
    return int(chain), path, int(ri), int(pi)


@dataclass
class _Lit:
    """One literal with everything rows() and apply() need."""
    chain: int
    path: str
    row: int
    pool: int
    raw: bytes             # retail bytes, NUL included when present
    face: str
    label: Optional[Tuple[bytes, bytes, bytes]]


def _container_rows(chain: int, path: str, data: bytes, face: str) -> List[dict]:
    kind = _kind(path)
    stem = _stem(path)
    infos = walk_container(data)
    drawn = drawn_params(infos)
    rows: List[dict] = []
    for ci in infos:
        ent = ci.entries
        text = {}
        for si in et.literal_indexes(ent):
            raw = ent[si][1]
            text[si] = raw[:-1] if raw.endswith(b"\0") else raw
        dec = {si: tc.to_display(b, "evt", tc.FIELD, face) for si, b in text.items()}
        plain = {si: b.decode("euc_jp", "replace") for si, b in text.items()}

        by_pool: Dict[int, List[Use]] = defaultdict(list)
        for u in ci.uses:
            by_pool[u.pool].append(u)

        # per-call roles: drawn text, speaker argument
        drawn_use: Set[int] = set()
        spk_arg: Set[int] = set()
        for u in ci.uses:
            c = u.call
            if c is None or u.pos < 0:
                continue
            target = set(range(len(_params(c.callee[2])))) if _is_drawing_api(c.callee) \
                else drawn.get(c.callee, set())
            if u.pos in target:
                drawn_use.add(id(u))
            elif target:
                spk_arg.add(u.pool)            # the call draws another String, not this one

        # script order and speakers, method by method in pc order
        speaker: Dict[int, str] = {}
        first: Dict[int, Tuple[int, int, int, str]] = {}
        per_method: Dict[int, List[Use]] = defaultdict(list)
        for u in ci.uses:
            per_method[u.mindex].append(u)
        for mi, us in per_method.items():
            us.sort(key=lambda u: u.pc)
            cur = ""
            for u in us:
                s = plain[u.pool]
                lab = _label_parts(ent[u.pool][1])
                if u.pool in spk_arg:
                    cur = s
                elif lab is not None:
                    cur = lab[1].decode("euc_jp", "replace")
                elif u.call is not None and u.call.speaker and id(u) in drawn_use:
                    cur = u.call.speaker
                if u.pool not in speaker and lab is None and u.pool not in spk_arg:
                    speaker[u.pool] = cur
                if "/[close()]" in s and not _TAG_RE.sub("", s).strip():
                    cur = ""
                k = (0 if u.method == "<init>" else 1, ci.row, u.mindex, u.pc)
                if u.pool not in first or k < first[u.pool][:4]:
                    first[u.pool] = k + (u.method,)

        # literal neighbours in pc order (field message triples)
        in_msg: Set[int] = set()
        for us in per_method.values():
            for a, b in zip(us, us[1:]):
                if _label_parts(ent[a.pool][1]) is not None:
                    in_msg.add(b.pool)
                if "/[waitkey(" in plain[b.pool] or "/[close()]" in plain[b.pool]:
                    in_msg.add(a.pool)

        for si in et.literal_indexes(ent):
            raw = ent[si][1]
            s = plain[si]
            src = dec[si]
            us = by_pool.get(si, [])
            finals = {u.final.split("(")[0] for u in us}
            menu = any(f in ("Menu.addItem", "Menu.addQuery") for f in finals)
            is_drawn = any(id(u) in drawn_use for u in us) or "aastore" in finals
            bare = _TAG_RE.sub("", s).strip()
            lab = _label_parts(raw)
            cat = CAT_CUT if kind == "cut" else CAT_FIELD
            hide = lock = None
            ctx = ""
            method = first.get(si, (0, 0, 0, 0, ""))[4]
            if lab is not None:
                cat = CAT_SPK
                src = tc.to_display(lab[1], "evt", tc.FIELD, face)
                ctx = "field speaker label /[label(...)]"
                if not lab[1]:
                    lock = hide = "empty label (no name box text)"
            elif si in spk_arg:
                cat = CAT_SPK
                ctx = "cutscene speaker argument"
                lock = hide = "speaker argument the script never draws"
            elif not bare:
                lock = hide = "control codes / whitespace only"
            elif "XEVE" in s:                     # story graph, even when printed
                lock = "story flag / scene jump name (XEVE*)"
                hide = lock
            elif finals and all(f.endswith("println") for f in finals):
                hide = "debug print (System.println)"
            elif _ID_RE.match(s) and re.search(r"[0-9_]", s):
                lock = "identifier (looked up by name at run time)"
                hide = lock
            elif _ID_RE.match(s) and not (menu or (is_drawn and "aastore" not in finals)
                                         or (si in in_msg and "aastore" in finals)):
                lock = "identifier (looked up by name at run time)"
                hide = lock
            elif _JP_RE.search(s) and not re.search(r"[a-z]{3}", s):
                hide = "Japanese developer text"
            elif s.lstrip().startswith(("***", "---", "===")):
                hide = "debug banner"
            elif not is_drawn and not menu:
                hide = "not drawn by any script call found"
            if lock is None and si in ci.shared:
                lock = _SHARED_REASON
                hide = hide or lock
            if cat not in (CAT_SPK,) and menu:
                cat = CAT_MENU
            if cat == CAT_CUT or cat == CAT_MENU:
                grp = f"{stem} / {method}" if method else stem
            else:
                grp = stem
            if not ctx:
                ctx = speaker.get(si, "")
                if cat == CAT_MENU and not ctx:
                    ctx = method
            rows.append({
                "id": _row_id(chain, path, ci.row, si), "fam": FAM, "cat": cat, "grp": grp,
                "ord": 0, "src": src, "ctx": ctx, "font": tc.FIELD, "ctl": "evt",
                "measure": face, "face": face,
                # field maps: TWIN_popScene wraps at the window width, so a guide
                # is noise; cutscenes: the scene window's width (PX_GUIDE)
                "px": PX_GUIDE if cat == CAT_CUT else None,
                "lim": None, "lock": lock, "hide": hide, "dup": 0,
                "_k": first.get(si, (2, ci.row, 0, 0, ""))[:4] + (si,),
                # Menu.addItem: TMENU_addItem adds one choice per line
                "_items": cat == CAT_MENU and "Menu.addItem" in finals,
            })
    return rows


# -- public API --------------------------------------------------------------------
_ROWS: Dict[str, Dict[str, dict]] = {}       # resolved retail path -> id -> row
_LAST: List[str] = []


def _key(iso_path: str) -> str:
    return str(Path(iso_path).resolve())


def rows(iso_path: str) -> List[dict]:
    """Every .evt literal as a catalog row (read-only)."""
    iso = IsoImage(iso_path)
    out: List[dict] = []
    try:
        for chain, path, data, _alloc in et.iter_containers(iso):
            try:
                if not et.strings_of(data):
                    continue
                et.fl00_rebuild(data, {})          # refuses base/system-style containers
            except et.EvtTextError:
                continue
            out += _container_rows(chain, path, data, tc.evt_face(chain, path, iso_path))
    finally:
        iso.close()
    # order within each group
    groups: Dict[Tuple[str, str], List[dict]] = defaultdict(list)
    for r in out:
        groups[(r["cat"], r["grp"])].append(r)
    for g in groups.values():
        g.sort(key=lambda r: r["_k"])
        for i, r in enumerate(g):
            r["ord"] = i
    for r in out:
        del r["_k"]
    dup = Counter(r["src"] for r in out)
    for r in out:
        r["dup"] = dup[r["src"]] - 1
    _ROWS[_key(iso_path)] = {r["id"]: r for r in out}
    _LAST[:] = [_key(iso_path)]
    return out


def _rows_by_id(iso_path: Optional[str]) -> Optional[Dict[str, dict]]:
    if iso_path:
        k = _key(iso_path)
        if k not in _ROWS:
            rows(iso_path)
        return _ROWS[k]
    return _ROWS[_LAST[0]] if _LAST else None


def _encode(row: dict, text: str) -> bytes:
    """Display text -> pool bytes WITHOUT the trailing NUL (speaker rows:
    just the name; the caller wraps it)."""
    data, _ = tc.from_display(text, tc.FIELD, "evt", row.get("face") or tc.FONT1)
    return data


# MSG_print2 (src/main/msg_init.c) converts Window.print text into
# ``unsigned char message[0x400]`` (two bytes of it a font escape in render
# mode 4) and MSG_convert ignores its capacity argument; Menu.addItem/addQuery
# use 0x400-byte buffers the same way (src/main/twsys_init.c). Tags never
# grow when converted, so the encoded length bounds the converted length.
MSG_TEXT_LIMIT = 0x400 - 3
# MSG_convert copies a tag body into ``char bracket[256]`` (``label(`` + X + ``)``)
LABEL_LIMIT = 256 - len("label()") - 1


def label_problems(name: bytes) -> List[str]:
    """Why an encoded speaker name would not reach the name box intact.

    ``/[label(X)]`` goes through MSG_convert (src/main/msg_init.c): the tag
    body is copied into ``bracket[256]`` (matchBracket nests ``[``/``]``), cut
    into tokens at ``, ( ) ;`` by STR_tokenGetNext (src/main/js_init.c), which
    also swallows a ``"..."`` run whole; MSG_setArgs turns a token that starts
    with a digit or ``-digit`` into a number and skips one that starts with
    ``$``. MIF2_label then copies X behind byte 0x85 and TWIN_pop* strcpy it to
    ``textOutput``, which TWIN_draw2 prints with ``xglFontPrint`` -- the same
    printer, face and ``#XX`` handling as the dialogue text. So raw accent
    codes and the raw ``#`` code (bytes >= 0xA1) are fine; these are not."""
    out: List[str] = []
    if not name:
        out.append("a speaker name may not be empty (MSG_convert would pass no argument)")
        return out
    bad = sorted({chr(b) for b in name if b < 0x80 and chr(b) in ',;()"[]'})
    if bad:
        out.append("a speaker name may not contain " + " ".join(repr(c) for c in bad)
                   + " (MSG_convert splits /[label(...)] at , ( ) ; and swallows \"...\"; the "
                     "name box would show only the part before it -- retail 'Sailor (E)' shows "
                     "'Sailor ' -- and text after a ')' lands in the dialogue)")
    if any(b < 0x20 for b in name):
        out.append("a speaker name may not contain control bytes")
    c0, c1 = name[0], name[1] if len(name) > 1 else 0
    if 0x30 <= c0 <= 0x39 or (c0 == 0x2D and 0x30 <= c1 <= 0x39):
        out.append("a speaker name may not start with a digit or '-digit' "
                   "(MSG_setArgs would read it as a number)")
    if c0 == 0x24:
        out.append("a speaker name may not start with '$' (MSG_setArgs skips it)")
    if len(name) > LABEL_LIMIT:
        out.append(f"{len(name)} bytes: over the {LABEL_LIMIT}-byte limit of /[label(...)]")
    return out


def validate(edits: Dict[str, str], iso_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Errors per id (empty dict = OK). Uses the rows of the last ``rows()``
    call, or of ``iso_path``; never writes anything."""
    by_id = _rows_by_id(iso_path)
    errors: Dict[str, List[str]] = {}
    for rid, text in edits.items():
        errs: List[str] = []
        try:
            chain, path, ri, pi = parse_id(rid)
        except ValueError as ex:
            errors[rid] = [str(ex)]
            continue
        row = by_id.get(rid) if by_id is not None else None
        if by_id is not None and row is None:
            errors[rid] = ["unknown id (not an .evt literal of this disc)"]
            continue
        if row is None:
            row = {"src": "", "face": tc.evt_face(chain, path), "cat": "", "lock": None}
        if text == row["src"]:
            continue
        if row.get("lock"):
            errs.append(f"locked: {row['lock']}")
        if row.get("_items") and text.count("\n") != row["src"].count("\n"):
            n_src, n_new = row["src"].count("\n") + 1, text.count("\n") + 1
            errs.append(f"{n_new} line(s) for {n_src} choice(s): Menu.addItem makes one choice "
                        f"per line (TMENU_addItem), and the script handles exactly {n_src}")
        if row["cat"] == CAT_SPK:
            if any(c in text for c in "]\n") or "/[" in text or "⟦" in text:
                errs.append("a speaker name may not contain ']', '/[', ⟦XX⟧ or a line break")
        res = tc.check(text, row["src"], tc.FIELD, "evt", face=row["face"])
        errs += res["errors"]
        if not errs:
            try:
                body = _encode(row, text)
                n = len(body) + 1 + (17 if row["cat"] == CAT_SPK else 0)
                if n > 0xFFFF:
                    errs.append(f"{n} bytes: over the class-file limit of 65535")
                if row["cat"] == CAT_SPK:
                    errs += label_problems(body)
                elif len(body) > MSG_TEXT_LIMIT:
                    errs.append(f"{len(body)} bytes: over the {MSG_TEXT_LIMIT}-byte message buffer "
                                f"(MSG_print2 converts into a 0x400-byte stack buffer)")
            except tc.TxEncodeError as ex:
                errs.append(str(ex))
        if errs:
            errors[rid] = errs
    return errors


def build_containers(retail_iso: str, edits: Dict[str, str]) -> Dict[Tuple[int, str], bytes]:
    """(chain, path) -> rebuilt container for every container an edit really
    changes, always from the retail bytes. Unchanged texts keep the retail
    bytes (retail ``#XX`` escapes survive)."""
    by_id = _rows_by_id(retail_iso)
    want: Dict[Tuple[int, str], Dict[Tuple[int, int], str]] = defaultdict(dict)
    for rid, text in edits.items():
        row = by_id[rid]
        if text == row["src"]:
            continue
        chain, path, ri, pi = parse_id(rid)
        want[(chain, path)][(ri, pi)] = text
    out: Dict[Tuple[int, str], bytes] = {}
    if not want:
        return out
    iso = IsoImage(retail_iso)
    try:
        for chain, path, data, _alloc in et.iter_containers(iso):
            ch = want.get((chain, path))
            if not ch:
                continue
            cur = {(ri, pi): raw for ri, pi, raw, _ in et.strings_of(data)}
            new: Dict[Tuple[int, int], bytes] = {}
            for (ri, pi), text in ch.items():
                row = by_id[_row_id(chain, path, ri, pi)]
                raw = cur[(ri, pi)]
                nul = b"\0" if raw.endswith(b"\0") else b""
                body = _encode(row, text)
                if row["cat"] == CAT_SPK and not row.get("lock"):
                    lab = _label_parts(raw)
                    body = lab[0] + body + lab[2]
                new[(ri, pi)] = body + nul
            out[(chain, path)] = et.rebuild(data, new)
    finally:
        iso.close()
    missing = set(want) - set(out)
    if missing:
        raise tc.TxBuildError(f"containers not found on the retail disc: {sorted(missing)[:3]}")
    return out


def apply(out_iso: str, retail_iso: str, edits: Dict[str, str], log=print) -> List[str]:
    """Write every evt edit into ``out_iso`` in place (a fresh clone of
    retail). Validates first; on any error raises TxBuildError and writes
    nothing."""
    msgs: List[str] = []

    def say(m: str):
        msgs.append(m)
        if log:
            log(m)

    if os.path.exists(out_iso) and os.path.samefile(out_iso, retail_iso):
        raise tc.TxBuildError("refusing to write: the output ISO is the retail ISO")
    errs = validate(edits, retail_iso)
    if errs:
        first = "; ".join(f"{k}: {v[0]}" for k, v in list(errs.items())[:5])
        raise tc.TxBuildError(f"{len(errs)} evt edit(s) invalid: {first}")
    try:
        blobs = build_containers(retail_iso, edits)
    except et.EvtTextError as ex:
        raise tc.TxBuildError(f"evt rebuild failed: {ex}")
    if not blobs:
        say("evt: no changed lines")
        return msgs
    from repack import patch_iso
    reports = patch_iso(out_iso, blobs, verbose=False, grow=True)
    moved = 0
    for r in reports:
        if r.relocated_lba is not None:
            moved += 1
        say(f"evt: chain{r.chain} {r.path}: {r.old_size} -> {r.new_size} bytes"
            + (f" (relocated to sector {r.relocated_lba})" if r.relocated_lba is not None else ""))
    say(f"evt: {len(reports)} container(s) written, {moved} relocated")
    return msgs


def read_back(iso_path: str, ids: Iterable[str]) -> Dict[str, str]:
    """Display text of the given ids as read from ``iso_path`` (speaker rows:
    the name only)."""
    want: Dict[Tuple[int, str], Set[Tuple[int, int]]] = defaultdict(set)
    for rid in ids:
        c, p, ri, pi = parse_id(rid)
        want[(c, p)].add((ri, pi))
    out = {}
    iso = IsoImage(iso_path)
    try:
        for chain, path, data, _ in et.iter_containers(iso):
            keys = want.get((chain, path))
            if not keys:
                continue
            face = tc.evt_face(chain, path, iso_path)
            for ri, pi, raw, _n in et.strings_of(data):
                if (ri, pi) in keys:
                    lab = _label_parts(raw)
                    body = lab[1] if lab else (raw[:-1] if raw.endswith(b"\0") else raw)
                    out[_row_id(chain, path, ri, pi)] = tc.to_display(body, "evt", tc.FIELD, face)
    finally:
        iso.close()
    return out


# -- selftest ----------------------------------------------------------------------
FR_ES = [
    "Élève, où es-tu ? « Ça va très bien ! » dit Zoë.",
    "Noël : l'œuvre est sûre, le maïs mûr, l'île à côté.",
    "Être prêt à goûter ? Là-bas, près du château, c'est déjà ça.",
    "À bientôt, Ève ! Ça marche ; ôte ton manteau.",
    "¿Dónde está el niño? ¡Allí, junto al pingüino!",
    "Señor Muñoz, ¿qué tal? Así es la canción del índice único.",
    "É À Ç : é è ê ë à â ç ô û ù ï î œ « » ñ ¿ ¡ á í ó ú ü",
]
NAMES = ["Éloïse", "Señor Núñez", "Chloé", "Ángel", "Françoise", "Zoë", "Hélène", "Íñigo"]
REQUIRED = "éèêëàâçôûùïîœ«»ÉÀÇñ¿¡áíóúü"


def _translate(row: dict, i: int, long: bool = False) -> str:
    if row["cat"] == CAT_SPK:
        return NAMES[i % len(NAMES)]
    toks = tc.protected_tokens(row["src"], "evt")
    lines = row["src"].split("\n")
    n = len(lines)
    if row["cat"] == CAT_MENU:                       # one choice per line, keep the count
        parts = [FR_ES[(i + k) % len(FR_ES)][:24].rstrip() for k in range(n)]
    else:
        parts = [FR_ES[(i + k) % len(FR_ES)] for k in range(n)]
        if long:                                      # < 1021 bytes: under MSG_TEXT_LIMIT
            parts[0] = " ".join([parts[0]] * 40)[:600].rstrip()
    parts[-1] = parts[-1] + "".join(toks)
    # keep leading spaces (manual centring) of the first line
    lead = len(lines[0]) - len(lines[0].lstrip(" "))
    return " " * lead + "\n".join(parts)


def _clone(retail: str, dest: Path) -> None:
    if dest.exists():
        dest.unlink()
    subprocess.run(["cp", "-c", retail, str(dest)], check=True)


def selftest(iso_path: str, scratch: Optional[str] = None) -> int:
    bad: List[str] = []

    def expect(cond: bool, what: str):
        if not cond:
            bad.append(what)
            print(f"  FAIL {what}")

    t0 = time.time()
    rs = rows(iso_path)
    t_rows = time.time() - t0
    print(f"rows: {len(rs)} in {t_rows:.2f} s")
    expect(len({r['id'] for r in rs}) == len(rs), "row ids are unique")
    cats = Counter(r["cat"] for r in rs)
    for cat in (CAT_FIELD, CAT_CUT, CAT_SPK, CAT_MENU):
        sub = [r for r in rs if r["cat"] == cat]
        vis = sum(1 for r in sub if not r["hide"])
        print(f"  {cat:28} {len(sub):6} rows  visible {vis:6}  hidden {len(sub) - vis:6}  "
              f"locked {sum(1 for r in sub if r['lock']):6}")
    print(f"  total visible {sum(1 for r in rs if not r['hide'])}, "
          f"editable visible {sum(1 for r in rs if not r['hide'] and not r['lock'])}")
    hide_why = Counter(r["hide"] for r in rs if r["hide"])
    lock_why = Counter(r["lock"] for r in rs if r["lock"])
    print("  hidden by reason: " + "; ".join(f"{v} {k}" for k, v in hide_why.most_common()))
    print("  locked by reason: " + "; ".join(f"{v} {k}" for k, v in lock_why.most_common()))
    by_id = {r["id"]: r for r in rs}

    # spot checks from the research notes (ST0210 / SCE01001)
    def find(path_end, src_start, cat=None):
        for r in rs:
            if r["id"].split("|")[1].endswith(path_end) and r["src"].startswith(src_start) \
                    and (cat is None or r["cat"] == cat):
                return r
        return None
    r = find("ST0210.evt", "Chief, which program shall I boot up?")
    expect(r is not None and r["cat"] == CAT_FIELD and r["ctx"] == "Allen" and not r["lock"]
           and not r["hide"], f"ST0210 Allen line: {r and (r['cat'], r['ctx'], r['lock'], r['hide'])}")
    r = find("ST0210.evt", "Yes\nNo")
    expect(r is not None and r["cat"] == CAT_MENU, "ST0210 Yes/No is a choice row")
    r = find("ST0210.evt", "Allen", CAT_SPK)
    expect(r is not None and not r["lock"], "ST0210 /[label(Allen)] is an editable speaker row")
    r = find("ST0210.evt", "Talk_npc1")
    expect(r is not None and r["lock"], "Talk_npc1 is locked")
    r = find("SCE01001.evt", "Morning, KOS-MOS.")
    expect(r is not None and r["cat"] == CAT_CUT and r["grp"] == "SCE01001 / playSCENE2"
           and r["ctx"] == "【シオン】", f"SCE01001 Shion line: {r and (r['grp'], r['ctx'])}")
    r = find("SCE01001.evt", "【シオン】")
    expect(r is not None and r["lock"] and r["cat"] == CAT_SPK, "【シオン】 is a locked speaker argument")
    r = find("SCE01007A1.evt", "See?")
    expect(r is not None and r["ctx"] == "Shion", f"SCE01007A1 'See?' speaker from face owner: "
                                                    f"{r and r['ctx']}")
    r = find("SCE01001.evt", "XEVEJNAME:")
    expect(r is not None and r["lock"], "XEVEJNAME string is locked")
    # which face draws each container: scene planner data vs the name rule
    sf = tc.scene_faces(iso_path)
    face_n: Counter = Counter()
    by_kind: Counter = Counter()
    disagree: List[str] = []
    mixed = sorted(k for k, v in sf.items() if len(v) > 1)
    unref: List[str] = []
    conts: List[Tuple[int, str]] = []
    iso = IsoImage(iso_path)
    try:
        for chain, path, _data, _a in et.iter_containers(iso):
            conts.append((chain, path))
    finally:
        iso.close()
    for chain, path in conts:
        f = tc.evt_face(chain, path, iso_path)
        face_n[f] += 1
        stem = tc._evt_stem(path)
        by_kind[(f, chain, "SCE" if stem.startswith("SCE") else stem[:2])] += 1
        if stem not in sf:
            unref.append(path)
        if f != tc.evt_face(chain, path):
            disagree.append(path)
    print(f"faces: {dict(sorted(face_n.items()))} over {len(conts)} containers "
          f"({len(sf)} run by a scene planner file; not run by any: {unref})")
    print("  by face/chain/kind: " + ", ".join(f"{f} chain{c} {k}: {n}"
                                                for (f, c, k), n in sorted(by_kind.items())))
    expect(not mixed, f"no container is run by both cf and ev scenes: {mixed[:5]}")
    expect(not disagree, f"name rule agrees with the planner data: {disagree[:5]}")
    expect(face_n[tc.FONT0] > 0 and all(r["face"] == tc.evt_face(*parse_id(r["id"])[:2], iso_path)
                                        for r in rs[::97]), "rows carry the planner face")
    # every retail #XX escape must read right under the face assigned
    # (only these three strings use a cell where the faces differ)
    want_word = {"ST2040": "fiancé", "ST2041": "fiancé", "SCE02001A": "resumé"}
    n_esc = n_sens = 0
    iso = IsoImage(iso_path)
    try:
        for chain, path, data, _ in et.iter_containers(iso):
            face = tc.evt_face(chain, path, iso_path)
            stem = tc._evt_stem(path)
            for ri, pi, raw, _n in et.strings_of(data):
                body = tc._TAG_RAW_RE.sub(b"", raw)
                cells = [int(m.group(1), 16) for m in re.finditer(rb"#([0-9A-Fa-f]{2})", body)]
                if not cells:
                    continue
                n_esc += 1
                shown = tc.to_display(raw.rstrip(b"\0"), "evt", tc.FIELD, face)
                sens = [c for c in cells if c in tc._FONT0_DIFF]
                if sens:
                    n_sens += 1
                    expect(stem in want_word, f"face-sensitive escape not checked: {path} {shown[:60]!r}")
                    print(f"  {path} ({face}): ...{shown[max(0, shown.find(want_word.get(stem, '#')) - 20):][:44]!r}")
                if sens and stem in want_word:
                    expect(want_word[stem] in shown,
                           f"{path}: escape reads {shown[:80]!r} under {face}, want {want_word[stem]!r}")
    finally:
        iso.close()
    print(f"retail #XX escapes: {n_esc} strings, {n_sens} use a cell where font0 and font1 differ; "
          f"all read as the intended letter under the assigned face")
    # speaker names: retail passes the label rules; bad names are refused
    spk = [r for r in rs if r["cat"] == CAT_SPK and not r["lock"]]
    lp = [r["src"] for r in spk if label_problems(_encode(r, r["src"]))]
    expect(all("(" in x and not re.match(r"[0-9$]|-[0-9]", x) for x in lp),
           f"retail speaker names break only the '(' rule: {lp}")
    print(f"speaker names: {len(spk) - len(lp)} of {len(spk)} editable retail labels pass the "
          f"/[label(...)] rules; retail itself truncates {lp} at '('")
    # px guides: none for field maps (engine wraps), the scene window for cutscenes
    expect(all(r["px"] is None for r in rs if r["cat"] == CAT_FIELD), "field rows have px=None")
    expect(all(r["px"] == PX_GUIDE for r in rs if r["cat"] == CAT_CUT),
           f"cutscene rows have px={PX_GUIDE}")
    fonts_ = tc.load_fonts(iso_path)
    cuts = [r for r in rs if r["cat"] == CAT_CUT and not r["lock"] and not r["hide"]]
    wide_cut = sum(1 for r in cuts
                   if (tc.check(r["src"], r["src"], tc.FIELD, "evt", measure=fonts_[r["measure"]],
                                face=r["face"])["px"] or 0) > PX_GUIDE)
    print(f"retail cutscene lines over the {PX_GUIDE} px guide: {wide_cut} of {len(cuts)}")
    expect(wide_cut < len(cuts) // 10, "under 10% of retail cutscene lines exceed the guide")
    # choice lists: one line per choice
    item = next((r for r in rs if r.get("_items") and "\n" in r["src"] and not r["lock"]), None)
    expect(item is not None, "a Menu.addItem row with several choices")
    if item is not None:
        n = item["src"].count("\n") + 1
        expect(validate({item["id"]: "Allons-y"}) != {}, "fewer choices refused")
        expect(validate({item["id"]: "\n".join(["Oui"] * (n + 1))}) != {}, "extra choice refused")
        expect(validate({item["id"]: "\n".join(["Oui"] * n)}) == {}, "same count accepted")
    f1 = tc.load_fonts(iso_path)[tc.FONT1]
    r_t = tc.check("/[color(0x329bbe)]Bonjour/[waitkey(64)]", "/[color(0x329bbe)]Hello/[waitkey(64)]",
                   tc.FIELD, "evt", measure=f1)
    expect(r_t["px"] == f1.width_of_bytes(b"Bonjour"),
           f"check() measures without /[..] tags: {r_t['px']} vs {f1.width_of_bytes(b'Bonjour')}")

    iso = IsoImage(iso_path)
    try:
        for chain, path, data, _ in et.iter_containers(iso):
            if path.endswith("SCE01001.evt"):
                dp = drawn_params(walk_container(data))
                got = dp.get(("SCE01001", "msg_print", "(Ljava/lang/String;Ljava/lang/String;)V"))
                expect(got == {1}, f"SCE01001.msg_print draws only its text parameter: {got}")
                print(f"  SCE01001.msg_print(speaker, text) draws parameter(s) {sorted(got or ())}")
                break
    finally:
        iso.close()

    # the codec reproduces retail bytes for every row (else unchanged rows must keep retail)
    t1 = time.time()
    fonts = tc.load_fonts(iso_path)
    same = esc = 0
    iso = IsoImage(iso_path)
    retail_raw: Dict[str, bytes] = {}
    retail_data: Dict[Tuple[int, str], bytes] = {}
    try:
        for chain, path, data, _ in et.iter_containers(iso):
            retail_data[(chain, path)] = data
            for ri, pi, raw, _n in et.strings_of(data):
                rid = _row_id(chain, path, ri, pi)
                retail_raw[rid] = raw
                row = by_id.get(rid)
                if row is None:
                    continue
                body = raw[:-1] if raw.endswith(b"\0") else raw
                lab = _label_parts(raw)
                if lab and row["cat"] == CAT_SPK:
                    body = lab[1]
                try:
                    enc = _encode(row, row["src"])
                except tc.TxEncodeError:
                    enc = None
                if enc == body:
                    same += 1
                elif b"#" in body:
                    esc += 1
                else:
                    expect(False, f"{rid}: display form does not encode back to retail")
    finally:
        iso.close()
    print(f"codec round trip: {same} byte-exact, {esc} differ only by retail #XX escapes "
          f"({time.time() - t1:.2f} s)")
    wide = [r for r in rs if r["cat"] == CAT_FIELD and not r["hide"]]
    over = 0
    for r in wide:
        try:
            enc = _encode(r, r["src"])
        except tc.TxEncodeError:
            continue
        f = fonts[r["face"]]
        if max(f.width_of_bytes(_TAG_RE.sub("", l.decode("latin-1")).encode("latin-1"))
               for l in enc.split(b"\n")) > PX_GUIDE:
            over += 1
    print(f"retail field lines wider than {PX_GUIDE} px (tags stripped): {over} of {len(wide)}")

    # sample: French + Spanish translations
    cand = sorted((r for r in rs if not r["hide"] and not r["lock"]), key=lambda r: r["id"])
    sample: List[dict] = []
    for cat, n in ((CAT_FIELD, 40), (CAT_CUT, 30), (CAT_SPK, 10), (CAT_MENU, 12)):
        sub = [r for r in cand if r["cat"] == cat]
        step = max(1, len(sub) // n)
        sample += sub[::step][:n]
    edits = {r["id"]: _translate(r, i) for i, r in enumerate(sample)}
    # one long cutscene line so its container must grow and relocate
    big = next(r for r in sample if r["cat"] == CAT_CUT)
    big_cont = big["id"].rsplit("|", 2)[0]
    for k, r in enumerate([r for r in cand if r["id"].startswith(big_cont + "|")
                           and r["cat"] == CAT_CUT][:12]):
        edits[r["id"]] = _translate(r, k, long=True)
    # a speaker name with '#' + hex digits and accents: written as raw codes
    spk_row = next(r for r in sample if r["cat"] == CAT_SPK)
    edits[spk_row["id"]] = "Chloé #2b"
    used = set("".join(edits.values()))
    expect(all(c in used for c in REQUIRED), f"sample covers {REQUIRED}")
    verr = validate(edits)
    expect(not verr, f"sample validates: {list(verr.items())[:3]}")
    # validation catches problems
    tag_row = next(r for r in sample if "/[" in r["src"] and r["cat"] != CAT_SPK)
    bad_edits = {tag_row["id"]: "sans balises", find("ST0210.evt", "Talk_npc1")["id"]: "Parler",
                 sample[0]["id"]: "Erdős"}
    verr = validate(bad_edits)
    expect(len(verr) == 3, f"validate flags missing tag, locked row, JIS X 0212 char: {verr}")
    spk2 = [r for r in sample if r["cat"] == CAT_SPK][1:6]
    cut_row = next(r for r in sample if r["cat"] == CAT_CUT and not tc.protected_tokens(r["src"], "evt"))
    bad_names = dict(zip([r["id"] for r in spk2],
                         ["Dr. (Mme)", "2e garde", "Roi, le", "$Zoë", "« Il » \"dit\""]))
    bad_names[cut_row["id"]] = "é" * 600
    verr = validate(bad_names)
    expect(len(verr) == len(bad_names), f"validate refuses bad speaker names and over-long lines: "
                                        f"{sorted(set(bad_names) - set(verr))}")

    scratch_dir = Path(scratch or tempfile.gettempdir())
    clone = scratch_dir / "txfam_evt_selftest.iso"
    try:
        # refuse to write while invalid
        _clone(iso_path, clone)
        try:
            apply(str(clone), iso_path, bad_edits, log=None)
            expect(False, "apply raises on invalid edits")
        except tc.TxBuildError:
            pass
        expect(os.path.getsize(clone) == os.path.getsize(iso_path), "nothing written on refusal")

        t2 = time.time()
        msgs = apply(str(clone), iso_path, edits, log=None)
        t_apply = time.time() - t2
        print(f"apply: {len(edits)} edits in {t_apply:.2f} s; {msgs[-1]}")
        expect(any("relocated to sector" in m for m in msgs), "long line container relocated")
        back = read_back(str(clone), edits)
        mism = [k for k, v in edits.items() if back.get(k) != v]
        expect(not mism, f"edited rows read back exactly ({len(mism)} differ: {mism[:2]})")
        # untouched literals byte-identical; edited containers consistent
        iso2 = IsoImage(str(clone))
        untouched = changed_cont = 0
        try:
            for chain, path, data, _ in et.iter_containers(iso2):
                ret = retail_data[(chain, path)]
                strs = et.strings_of(data)
                touched = False
                for ri, pi, raw, _n in strs:
                    rid = _row_id(chain, path, ri, pi)
                    if rid in edits:
                        touched = True
                        continue
                    untouched += 1
                    if raw != retail_raw[rid]:
                        expect(False, f"untouched {rid} changed")
                if touched:
                    changed_cont += 1
                    rebuilt = et.rebuild(ret, {(ri, pi): raw for ri, pi, raw, _n in strs})
                    expect(rebuilt == data, f"{path}: container = retail rebuilt with new strings")
                else:
                    expect(data == ret, f"{path}: untouched container byte-identical")
        finally:
            iso2.close()
        print(f"read back: {len(edits)} edited rows exact, {untouched} untouched literals "
              f"identical, {changed_cont} containers rebuilt")

        # identity: every sampled row set to its own source -> clone == retail
        _clone(iso_path, clone)
        ident = {r["id"]: r["src"] for r in sample}
        msgs = apply(str(clone), iso_path, ident, log=None)
        expect(msgs == ["evt: no changed lines"], f"identity apply writes nothing: {msgs}")
        iso3 = IsoImage(str(clone))
        try:
            diff = sum(1 for c, p, d, _ in et.iter_containers(iso3) if d != retail_data[(c, p)])
        finally:
            iso3.close()
        expect(diff == 0, f"identity: {diff} containers differ")
        # identity through the encoder: force-encode each sampled source
        forced = {}
        for r in sample:
            c, p, ri, pi = parse_id(r["id"])
            body = _encode(r, r["src"])
            raw = retail_raw[r["id"]]
            lab = _label_parts(raw)
            if r["cat"] == CAT_SPK:
                body = lab[0] + body + lab[2]
            forced.setdefault((c, p), {})[(ri, pi)] = body + (b"\0" if raw.endswith(b"\0") else b"")
        blobs = {k: et.rebuild(retail_data[k], v) for k, v in forced.items()}
        from repack import patch_iso
        patch_iso(str(clone), blobs, verbose=False, grow=True)
        iso4 = IsoImage(str(clone))
        try:
            diff = sum(1 for c, p, d, _ in et.iter_containers(iso4) if d != retail_data[(c, p)])
        finally:
            iso4.close()
        expect(diff == 0 and os.path.getsize(clone) == os.path.getsize(iso_path),
               f"identity through the encoder: {diff} containers differ")
        print(f"identity: {len(sample)} rows set to their source (skip path and forced "
              f"re-encode of {len(blobs)} containers) leave every container byte-identical")
    finally:
        if clone.exists():
            clone.unlink()
    print(f"timings: rows {t_rows:.2f} s, whole selftest {time.time() - t0:.2f} s")
    if bad:
        print(f"selftest FAILED ({len(bad)})")
        return 1
    print("selftest OK")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("selftest")
    ps.add_argument("--iso", required=True)
    ps.add_argument("--scratch", help="folder for the APFS clone (default: temp dir)")
    pr = sub.add_parser("rows")
    pr.add_argument("--iso", required=True)
    pr.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest(a.iso, a.scratch)
    rs = rows(a.iso)
    if a.out:
        Path(a.out).write_text(json.dumps(rs, ensure_ascii=False, indent=0), encoding="utf-8")
        print(f"{len(rs)} rows -> {a.out}")
    else:
        for r in rs[:50]:
            print(json.dumps(r, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
