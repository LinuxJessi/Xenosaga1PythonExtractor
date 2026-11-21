"""txfam_fix.py — the small fixed-record text tables, for the translation editor.

Seven data files hold player-visible text that none of the three big readers
(textpack, evttext, ovltext) touch: event-item names, save-point names,
U.M.N. plug-in texts, the Segment Address texts, the Xeno Card cards, the
casino prize list and the enemy battle messages. Each has its own little
layout, and each layout was checked against the code of the matching
decompilation (``~/Desktop/KOS-MOS ver. 3``, real SLUS_204.69 / overlay
symbols) before it was made editable, because a wrong guess here does not
show a wrong letter, it breaks a record the game indexes blindly.

Every file is rebuilt from retail by its own builder, and the selftest
proves that rebuilding every record from its own retail bytes gives back the
retail file byte for byte. What can be written, and how:

``karakama\\evtitem.dat`` (Key items). 255 records of 0x80 bytes
(``EVT_ITEM_COUNT``); ``dataEvtItmNameGet(n)`` returns record n-1; the
menu reads the description after the first NUL (``menu_1.c``). Name and
description share the 128 bytes: name + NUL + description + NUL <= 128.

``endou\\savemap.bin`` (Save-screen locations). ``SaveMapNameEntry`` x 37
read (``SAVEMAP_ENTRY_COUNT``; the 38th record is leftover bytes and is not
listed). ``names[0]`` and ``names[1]`` are char[33]; the save panel prints
both (``file.c``), and ``xglMcSetMapName`` copies them into the memory-card
title by reading byte PAIRS, so a 32-byte name would run on into the next
field. The editor therefore allows 31 bytes + NUL per name (lim 32).

``endou\\umn\\umntxt.bin`` (U.M.N. plug-ins). Six 0x80 records
(``UmnPluginTextGet``): name char[32], description char[96].

``endou\\mapex.bin`` (Junk parts & extras). Three blocks of NUL-terminated
strings at fixed offsets 0x000 / 0x3C0 / 0x540 inside a 0x2000-byte buffer
(``MAPEX_BUFFER_SIZE``); ``MenuSegmentInfoTextGet``,
``MenuSegmentItemNameGet`` and ``MenuSegmentMapNameGet`` find string n by
skipping NULs. Strings may grow and shrink freely as long as each block fits
before the next block's offset (the last block: before 0x2000).

``carddata\\card.dat`` (Card game). Header, 148 ``CardDefinition`` records
of 0x24 bytes with u32 name/caption offsets, then the texts in card order
with no gaps. The builder repoints every offset, so texts grow freely; the
file must stay below 0xC000 bytes because ``CARD_GRAPHIC_BUFFER`` starts at
0x0100C000, right after ``CARD_DATA_BUFFER`` (0x01000000). The card
printers (``CardNameConvert`` / ``CardDispTxtSub``) rewrite ``@`` to a
comma, ``#`` to an apostrophe, ``$`` to a line break and the two backslash
forms to ``#73`` / ``#23`` before printing, so the display form shows the
converted text and the encoder writes those characters back; ``@ # $ \\``
themselves cannot be typed in card text.

``tanaka\\CASINO.res`` (Casino). ``CasinoPrize[51]`` (name[40],
description[80]) and ``CasinoCoinOffer[4]`` (description[28]), printed with
``xglFontPrint`` (``mini_g.c``). Retail fields keep leftover bytes after the
NUL; an untouched field keeps them, an edited one is zero-filled.

``yamamoto\\think\\think*.bin`` (Enemy battle quotes). Bytecode of the
battle think machine (``src/ov01/cmd.c``): a string operand sits inline,
NUL-terminated and padded to an even length, and the program counter steps
over it by its own length. A string can therefore be replaced only in place,
keeping its padded size: a shorter translation is padded with trailing
spaces (shown in the re-read text). Strings are found by a recursive
disassembly from every monster set / spawn entry point (99 opcodes, operand
shapes from ``thinkStrTbl``); a file with any unknown opcode on a reachable
path is listed locked. ``MSG`` text goes through ``battleMsgPut`` /
``battleMsgPut2`` -> ``eBattleWinOpen2/4`` -> ``eMessage`` -> ``xglFontPrint``,
the field printer; ``PRINT`` text is printf debug output (listed hidden).

Font: every table here is drawn by the field printer (``xglFontPrint`` /
``eMessage``) outside "ev" scenes, so face font1 (txcodec's default map).
For the think messages this is inferred (a battle runs from a field map,
and font0 is loaded only for ev scenes), not traced per battle.

Usage:
    python3 txfam_fix.py selftest --iso RETAIL
    python3 txfam_fix.py dump --iso RETAIL [--out rows.json]
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
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import arx
import repack
from iso9660 import IsoImage
from txcodec import (FIELD, FONT1, TxBuildError, TxEncodeError, check,
                     from_display, to_display)

FAM = "fix"
CHAIN = 0

P_EVTITEM = "karakama\\evtitem.dat"
P_SAVEMAP = "endou\\savemap.bin"
P_UMNTXT = "endou\\umn\\umntxt.bin"
P_MAPEX = "endou\\mapex.bin"
P_CARD = "carddata\\card.dat"
P_CASINO = "tanaka\\CASINO.res"
THINK_DIR = "yamamoto\\think\\"

CAT_EVTITEM = "Key items"
CAT_SAVEMAP = "Save-screen locations"
CAT_UMNTXT = "U.M.N. plug-ins"
CAT_MAPEX = "Junk parts & extras"
CAT_CARD = "Card game"
CAT_CASINO = "Casino"
CAT_THINK = "Enemy battle quotes"

LOCK_UNKNOWN = "format not yet understood: listed for reference"


# -- one string slot ---------------------------------------------------------------

@dataclass
class Field:
    id: str
    cat: str
    grp: str
    ord: int
    ctx: str
    path: str                      # TOC path of the object holding it
    raw: bytes                     # retail bytes, no terminating NUL
    src: str = ""                  # display form
    lim: Optional[int] = None      # bytes INCLUDING the NUL, when fixed
    lock: Optional[str] = None
    hide: Optional[str] = None
    kind: str = "plain"            # "plain" | "card" | "think"
    px: Optional[int] = None
    key: tuple = ()                # builder address of the slot


# -- reading objects ---------------------------------------------------------------

def _read_objects(iso_path: str, want: Callable[[str], bool]) -> Dict[str, bytes]:
    """Every chain-0 object whose path satisfies ``want``, in one ISO open."""
    iso = IsoImage(iso_path)
    try:
        writer, entries, _ = repack._load_chain(iso.mm, iso, CHAIN)
        out = {}
        for e in entries:
            if want(e.path):
                raw = writer.read(e.byte_offset, e.size)
                out[e.path] = arx.decompress(raw) if e.compressed else raw
        return out
    finally:
        iso.close()


def _is_ours(path: str) -> bool:
    p = path.lower()
    return p in {x.lower() for x in (P_EVTITEM, P_SAVEMAP, P_UMNTXT, P_MAPEX, P_CARD, P_CASINO)} \
        or (p.startswith(THINK_DIR) and p.endswith(".bin"))


def _cstr(buf: bytes, off: int, end: Optional[int] = None) -> bytes:
    stop = buf.index(b"\0", off) if end is None else buf.find(b"\0", off, end)
    if stop < 0:
        raise TxBuildError(f"unterminated string at 0x{off:X}")
    return buf[off:stop]


# -- card text escapes (CardNameConvert / CardDispTxtSub) ----------------------------

def card_to_print(raw: bytes) -> bytes:
    """What the card printers hand to xglFontPrint after their rewrite."""
    b = bytearray(raw)
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x40:
            b[i] = 0x2C
        elif c == 0x23:
            b[i] = 0x27
        elif c == 0x24:
            b[i] = 0x0A
        elif c == 0x5C and i + 2 < len(b) and b[i + 1] == 0x5C:
            if b[i + 2] == 0x5C:
                b[i:i + 3] = b"#73"
            elif b[i + 2] == 0x25:
                b[i:i + 3] = b"#23"
        i += 1
    return bytes(b)


def print_to_card(data: bytes) -> bytes:
    """Inverse for freshly encoded text: comma -> '@', apostrophe -> '#',
    line break -> '$'. Raises TxEncodeError for bytes the rewrite would
    change or that would stop it early (control codes)."""
    problems = []
    for i, c in enumerate(data):
        if c in (0x40, 0x23, 0x24, 0x5C):
            problems.append((i, chr(c), f"{chr(c)!r} cannot be written in card text "
                                        f"(the card printer rewrites it)"))
        elif c < 0x20 and c != 0x0A:
            problems.append((i, f"\\x{c:02x}", "control codes are not allowed in card text"))
    if problems:
        raise TxEncodeError(problems)
    return data.replace(b",", b"@").replace(b"'", b"#").replace(b"\n", b"$")


# -- think bytecode ----------------------------------------------------------------

# operand shapes per opcode (src/ov01/cmd.c, thinkStrTbl): n number, r register,
# J jump target, s inline string, T jump table (length = previous operand)
_THINK_OPS: Dict[int, str] = {
    0: "", 1: "J", 2: "J", 3: "", 4: "nnnJ", 5: "nnT", 6: "nnT", 7: "rn", 8: "r", 9: "r",
    10: "rn", 11: "rn", 12: "rn", 13: "rn", 14: "rn", 15: "rn", 16: "rn", 17: "rn",
    18: "r", 19: "r", 20: "rn", 21: "n", 22: "r", 23: "rnn", 24: "rnn", 25: "r",
    26: "s", 27: "nn", 28: "", 29: "", 30: "", 31: "", 32: "nn", 33: "sn", 34: "",
    35: "", 36: "", 37: "rnn", 38: "rn", 39: "rnn", 40: "rnn", 41: "rnn", 42: "rnnn",
    43: "rn", 44: "rnn", 45: "nn", 46: "nn", 47: "nn", 48: "rnnJ", 49: "rnn", 50: "r",
    51: "rn", 80: "rJ", 81: "n", 82: "n", 83: "", 84: "", 85: "", 86: "", 87: "n",
    88: "nnnn", 89: "rn", 90: "r", 91: "n", 92: "r", 93: "r", 94: "r", 95: "rnnn",
    96: "rnnn", 97: "nnn", 98: "nn",
}
_THINK_END = {0, 1, 3, 5}           # EXIT, GO, RETURN, ONGO: no fall-through
OP_PRINT, OP_MSG = 26, 33


class ThinkError(ValueError):
    pass


@dataclass
class ThinkString:
    op: int
    off: int          # file offset of the first byte
    slot: int         # padded size including the NUL (even)
    raw: bytes        # without NUL
    frames: Optional[int]   # MSG: the number operand (0 = wait for the player), None if a register


def think_strings(data: bytes) -> List[ThinkString]:
    """Recursive disassembly of a think file from every entry point; returns
    the PRINT / MSG string operands reached. Raises ThinkError on anything
    the disassembler does not know (unknown opcode, computed jump table,
    a path running off the file). Jumps to a register-held offset are skipped:
    the code they reach is not listed, and nothing unreached is ever written."""
    n = len(data)

    def h(o: int) -> int:
        if o < 0 or o + 2 > n:
            raise ThinkError(f"read past the end at 0x{o:X}")
        return struct.unpack_from("<H", data, o)[0]

    def target(v: int, where: int) -> int:
        if v & 0xC000:
            raise ThinkError(f"computed or negative jump target 0x{v:04X} at 0x{where:X}")
        if v >= n:
            raise ThinkError(f"jump target 0x{v:X} outside the file at 0x{where:X}")
        return v & ~1

    entries = []
    t = h(0) & ~1
    while True:
        spawn = h(t)
        if spawn == 0:
            break
        for k in (4, 6, 8):
            s = h(t + k)
            if s:
                entries.append(target(s, t + k))
        q = target(spawn, t)
        while h(q) != 0:
            for k in (4, 6):
                s = h(q + k)
                if s:
                    entries.append(target(s, q + k))
            q += 8
        t += 10

    seen = set()
    computed: List[int] = []
    found: Dict[int, ThinkString] = {}
    work = list(entries)
    while work:
        pc = work.pop()
        while pc not in seen:
            seen.add(pc)
            op = h(pc)
            shape = _THINK_OPS.get(op)
            if shape is None:
                raise ThinkError(f"unknown opcode {op} at 0x{pc:X}")
            q = pc + 2
            prev = None
            frames = None
            for kind in shape:
                if kind == "s":
                    end = data.find(b"\0", q)
                    if end < 0:
                        raise ThinkError(f"unterminated string at 0x{q:X}")
                    ln = end - q + 1
                    ln += ln & 1
                    found[q] = ThinkString(op, q, ln, data[q:end], None)
                    sq = q
                    q += ln
                    continue
                v = h(q)
                if kind == "J":
                    if v & 0x8000:
                        # a register-held script (ATKTBL_SEARCH in think057/087):
                        # not followable statically; only fewer strings are found
                        computed.append(q)
                    else:
                        work.append(target(v, q))
                elif kind == "T":
                    if prev is None or prev & 0x8000:
                        raise ThinkError(f"jump table with computed length at 0x{pc:X}")
                    for j in range(prev):
                        work.append(target(h(q + 2 * j), q + 2 * j))
                    q += 2 * prev
                    prev = None
                    continue
                elif kind == "n" and op == OP_MSG:
                    frames = None if v & 0x8000 else v
                prev = v
                q += 2
            if op == OP_MSG:
                found[sq].frames = frames
            if op in _THINK_END:
                break
            pc = q
    return [found[k] for k in sorted(found)]


# -- readers (one per file) --------------------------------------------------------

def _fields_evtitem(data: bytes) -> List[Field]:
    out = []
    for i in range(len(data) // 0x80):
        rec = data[i * 0x80:(i + 1) * 0x80]
        if not any(rec):
            continue
        name = _cstr(rec, 0)
        desc = _cstr(rec, len(name) + 1)
        n = i + 1
        for part, raw, other in (("name", name, desc), ("desc", desc, name)):
            out.append(Field(f"fix:evtitem:{n}:{part}", CAT_EVTITEM, P_EVTITEM, n * 2 + (part == "desc"),
                             f"event item {n} {'name' if part == 'name' else 'description'} "
                             f"(name + description share 128 bytes)",
                             P_EVTITEM, raw, lim=0x80 - 1 - len(other), key=(i, part)))
    return out


def _build_evtitem(data: bytes, new: Dict[tuple, bytes]) -> bytes:
    out = bytearray(data)
    recs = {}
    for (i, part), b in new.items():
        recs.setdefault(i, {})[part] = b
    for i, parts in recs.items():
        rec = data[i * 0x80:(i + 1) * 0x80]
        name = parts.get("name", _cstr(rec, 0))
        desc = parts.get("desc", _cstr(rec, len(_cstr(rec, 0)) + 1))
        body = name + b"\0" + desc + b"\0"
        if len(body) > 0x80:
            raise TxBuildError(f"evtitem {i + 1}: name + description need {len(body)} of 128 bytes")
        out[i * 0x80:(i + 1) * 0x80] = body + bytes(0x80 - len(body))
    return bytes(out)


def _fixed_slots_build(data: bytes, new: Dict[tuple, bytes]) -> bytes:
    """Builder for tables of fixed char[size] slots: key = (offset, size)."""
    out = bytearray(data)
    for (off, size), b in new.items():
        if len(b) + 1 > size:
            raise TxBuildError(f"0x{off:X}: {len(b) + 1} bytes in a {size}-byte field")
        if b == _cstr(data, off, off + size):
            continue                      # untouched: keep retail tail bytes
        out[off:off + size] = b + bytes(size - len(b))
    return bytes(out)


def _fields_savemap(data: bytes) -> List[Field]:
    out = []
    for i in range(37):                    # SAVEMAP_ENTRY_COUNT
        base = i * 70
        map_id = struct.unpack_from("<H", data, base + 0x42)[0]
        for k in (0, 1):
            off = base + k * 0x21
            out.append(Field(f"fix:savemap:{i}:{k}", CAT_SAVEMAP, P_SAVEMAP, i * 2 + k,
                             f"map {map_id} names[{k}]", P_SAVEMAP, _cstr(data, off, off + 0x21),
                             lim=32, key=(off, 32)))
    return out


def _fields_umntxt(data: bytes) -> List[Field]:
    out = []
    for i in range(len(data) // 0x80):
        base = i * 0x80
        for part, off, size in (("name", base, 0x20), ("desc", base + 0x20, 0x60)):
            out.append(Field(f"fix:umntxt:{i}:{part}", CAT_UMNTXT, P_UMNTXT, i * 2 + (part == "desc"),
                             f"plug-in {i} {'name' if part == 'name' else 'description'}",
                             P_UMNTXT, _cstr(data, off, off + size), lim=size, key=(off, size)))
    return out


MAPEX_BLOCKS = ((0x000, 0x3C0, "MenuSegmentInfoTextGet"),
                (0x3C0, 0x540, "MenuSegmentItemNameGet"),
                (0x540, 0x2000, "MenuSegmentMapNameGet"))   # MAPEX_BUFFER_SIZE


def _mapex_split(data: bytes) -> List[List[bytes]]:
    blocks = []
    for start, end, _ in MAPEX_BLOCKS:
        used = data[start:min(end, len(data))].rstrip(b"\0")
        blocks.append(used.split(b"\0") if used else [])
    return blocks


def _fields_mapex(data: bytes) -> List[Field]:
    out = []
    for bi, strings in enumerate(_mapex_split(data)):
        start, end, getter = MAPEX_BLOCKS[bi]
        for si, raw in enumerate(strings):
            hide = None
            if bi < 2 and si == 22:
                hide = "never requested by the code (string 22, the Erde Kaiser pair)"
            out.append(Field(f"fix:mapex:{bi}:{si}", CAT_MAPEX, f"{P_MAPEX} {getter}", si,
                             f"{getter} string {si}; block shares {end - start} bytes",
                             P_MAPEX, raw, hide=hide, key=(bi, si)))
    return out


def _build_mapex(data: bytes, new: Dict[tuple, bytes]) -> bytes:
    blocks = _mapex_split(data)
    for (bi, si), b in new.items():
        blocks[bi][si] = b
    out = bytearray()
    for bi, strings in enumerate(blocks):
        start, end, getter = MAPEX_BLOCKS[bi]
        body = b"".join(s + b"\0" for s in strings)
        if len(body) > end - start:
            raise TxBuildError(f"mapex {getter}: {len(body)} bytes, block holds {end - start}")
        if bi < 2:
            body += bytes(end - start - len(body))
        out += body
    if len(out) < len(data):
        out += bytes(len(data) - len(out))
    return bytes(out)


CARD_HEADER, CARD_REC = 0x10, 0x24
CARD_MAX = 0xC000            # CARD_GRAPHIC_BUFFER 0x0100C000 - CARD_DATA_BUFFER 0x01000000


def _card_texts(data: bytes) -> Tuple[int, int, List[Tuple[bytes, bytes]]]:
    count = struct.unpack_from("<H", data, 0)[0]
    text0 = CARD_HEADER + count * CARD_REC
    pos = text0
    texts = []
    for i in range(count):
        no, co, do = struct.unpack_from("<III", data, CARD_HEADER + i * CARD_REC + 0x18)
        if no != pos or do != 0:
            raise TxBuildError(f"card {i}: name not where the packed layout expects it")
        name = _cstr(data, no)
        pos = no + len(name) + 1
        if co != pos:
            raise TxBuildError(f"card {i}: caption not where the packed layout expects it")
        cap = _cstr(data, co)
        pos = co + len(cap) + 1
        texts.append((name, cap))
    if pos != len(data):
        raise TxBuildError("card.dat: bytes after the last caption")
    return count, text0, texts


# CardDeckDisp and the owned-card list (src/ov10/card_fread.c) copy exactly
# CARD_LIST_TEXT_LENGTH * CARD_TEXT_CHAR_BYTES = 16 * 2 bytes of a name into
# nameText[32] and NUL it there: a longer name is cut, and a two-byte code
# starting at byte 31 would make xglFontPrint step over that NUL. The hand
# list (cgp.c, CGP_HAND_LIST_TEXT_LENGTH 14) shows 28 bytes.
CARD_NAME_BYTES = 32
CARD_HAND_BYTES = 28


def _fields_card(data: bytes) -> List[Field]:
    count, _, texts = _card_texts(data)
    out = []
    for i, (name, cap) in enumerate(texts):
        kind = data[CARD_HEADER + i * CARD_REC]
        for part, raw in (("name", name), ("caption", cap)):
            ctx = f"card 0x{i:02X} (kind {kind}) {part}"
            if part == "name":
                ctx += (f"; the deck lists show {CARD_NAME_BYTES} bytes, the hand list "
                        f"{CARD_HAND_BYTES} (an accent is 2 bytes)")
            out.append(Field(f"fix:card:{i}:{part}", CAT_CARD, P_CARD, i * 2 + (part == "caption"),
                             ctx, P_CARD, raw, kind="card", key=(i, part),
                             lim=CARD_NAME_BYTES + 1 if part == "name" else None))
    return out


def _build_card(data: bytes, new: Dict[tuple, bytes]) -> bytes:
    count, text0, texts = _card_texts(data)
    texts = [list(t) for t in texts]
    for (i, part), b in new.items():
        texts[i][0 if part == "name" else 1] = b
    head = bytearray(data[:text0])
    area = bytearray()
    for i, (name, cap) in enumerate(texts):
        r = CARD_HEADER + i * CARD_REC
        struct.pack_into("<II", head, r + 0x18, text0 + len(area), text0 + len(area) + len(name) + 1)
        area += name + b"\0" + cap + b"\0"
    out = bytes(head + area)
    if len(out) > CARD_MAX:
        raise TxBuildError(f"card.dat would be {len(out)} bytes; the load buffer holds {CARD_MAX}")
    return out


CASINO_PRIZES, CASINO_OFFERS = 0x0008, 0x18BC


def _fields_casino(data: bytes) -> List[Field]:
    out = []
    for i in range(51):
        base = CASINO_PRIZES + i * 0x7C
        cost = struct.unpack_from("<i", data, base)[0]
        for part, off, size in (("name", base + 4, 40), ("description", base + 44, 80)):
            out.append(Field(f"fix:casino:prize{i}:{part}", CAT_CASINO, f"{P_CASINO} prizes",
                             i * 2 + (part == "description"), f"prize {i} {part} ({cost} coins)",
                             P_CASINO, _cstr(data, off, off + size), lim=size, key=(off, size)))
    for i in range(4):
        base = CASINO_OFFERS + i * 0x20
        cost = struct.unpack_from("<i", data, base)[0]
        out.append(Field(f"fix:casino:offer{i}", CAT_CASINO, f"{P_CASINO} coin_offers", i,
                         f"coin offer {i} description ({cost} G)", P_CASINO,
                         _cstr(data, base + 4, base + 32), lim=28, key=(base + 4, 28)))
    return out


def _fields_think(path: str, data: bytes) -> List[Field]:
    name = path.rsplit("\\", 1)[-1]
    stem = name.rsplit(".", 1)[0]
    try:
        strings = think_strings(data)
        lock = None
    except ThinkError as e:
        return [Field(f"fix:{stem}:unparsed", CAT_THINK, path, 0, f"disassembly stopped: {e}",
                      path, b"", lock=LOCK_UNKNOWN, kind="think")]
    out = []
    for s in strings:
        if s.op == OP_MSG:
            how = ("waits for the player" if s.frames == 0 else
                   f"shown {s.frames} frames" if s.frames else "timed by a register")
            ctx = f"MSG @0x{s.off:X}, {how}; shorter text is padded with spaces"
            hide = None
            px = 462 if s.frames == 0 else 306      # eBattleWinOpen4 468 / Open2 312, minus margins
        else:
            ctx = f"PRINT @0x{s.off:X} (debug console)"
            hide = "PRINT debug text (printf only, not drawn)"
            px = None
        out.append(Field(f"fix:{stem}:{s.off:x}", CAT_THINK, path, s.off, ctx, path, s.raw,
                         lim=s.slot, lock=lock, hide=hide, kind="think", px=px, key=(s.off, s.slot)))
    return out


def _build_think(data: bytes, new: Dict[tuple, bytes]) -> bytes:
    out = bytearray(data)
    for (off, slot), b in new.items():
        body = think_pad(b, slot)
        if data[off + slot - 1] != 0 or (slot > 1 and data[off:off + slot].find(b"\0") < 0):
            raise TxBuildError(f"think 0x{off:X}: slot not where expected")
        out[off:off + slot] = body
    return bytes(out)


def think_pad(b: bytes, slot: int) -> bytes:
    """Bytes of a think string slot: text, trailing spaces so the padded
    length stays ``slot``, NUL, and the retail-style NUL pad."""
    if len(b) + 1 > slot:
        raise TxBuildError(f"{len(b) + 1} bytes in a {slot}-byte think slot")
    if len(b) + 1 < slot - 1:
        b = b + b" " * (slot - 2 - len(b))
    return b + bytes(slot - len(b))


TABLES = (
    (P_EVTITEM, _fields_evtitem, _build_evtitem),
    (P_SAVEMAP, _fields_savemap, _fixed_slots_build),
    (P_UMNTXT, _fields_umntxt, _fixed_slots_build),
    (P_MAPEX, _fields_mapex, _build_mapex),
    (P_CARD, _fields_card, _build_card),
    (P_CASINO, _fields_casino, _fixed_slots_build),
)


def _builder(path: str):
    for p, _, b in TABLES:
        if p.lower() == path.lower():
            return b
    return _build_think


# -- scan / codec ------------------------------------------------------------------

def _decode(f: Field) -> str:
    raw = card_to_print(f.raw) if f.kind == "card" else f.raw
    return to_display(raw, "c0", FIELD, FONT1)


def encode(f: Field, text: str) -> Tuple[bytes, List[str]]:
    """Display text -> the bytes stored in the file (no NUL). Raises
    TxEncodeError / TxBuildError."""
    if text == f.src:
        return f.raw, []
    data, warns = from_display(text, FIELD, "c0", FONT1)
    if b"\0" in data:
        raise TxEncodeError([(text.find("⟦00⟧"), "⟦00⟧", "a NUL byte would end the string")])
    if f.kind == "card":
        data = print_to_card(data)
    return data, warns


_CACHE: Dict[tuple, Tuple[List[Field], Dict[str, bytes]]] = {}
_LAST: List[tuple] = []


def scan(iso_path: str) -> Tuple[List[Field], Dict[str, bytes]]:
    """All fields of the family plus the retail bytes of every object read."""
    st = os.stat(iso_path)
    key = (str(Path(iso_path).resolve()), st.st_size, st.st_mtime_ns)
    if key in _CACHE:
        _LAST[:] = [key]
        return _CACHE[key]
    objs = _read_objects(iso_path, _is_ours)
    fields: List[Field] = []
    lower = {p.lower(): p for p in objs}
    for p, reader, _ in TABLES:
        real = lower.get(p.lower())
        if real is None:
            continue
        try:
            fields += reader(objs[real])
        except (TxBuildError, ValueError, struct.error) as e:
            fields.append(Field(f"fix:{p.rsplit(chr(92), 1)[-1]}:unparsed", "Key items", p, 0,
                                f"reader failed: {e}", p, b"", lock=LOCK_UNKNOWN))
    for p in sorted(x for x in objs if x.lower().startswith(THINK_DIR)):
        fields += _fields_think(p, objs[p])
    for f in fields:
        f.src = _decode(f)
    _CACHE[key] = (fields, objs)
    _LAST[:] = [key]
    return fields, objs


def _row(f: Field) -> dict:
    return {"id": f.id, "fam": FAM, "cat": f.cat, "grp": f.grp, "ord": f.ord, "src": f.src,
            "ctx": f.ctx, "font": FIELD, "ctl": "c0", "measure": FONT1, "px": f.px,
            "lim": f.lim, "lock": f.lock, "hide": f.hide, "dup": 0}


def rows(iso_path: str) -> List[dict]:
    fields, _ = scan(iso_path)
    return [_row(f) for f in fields]


# -- validate / apply --------------------------------------------------------------

_ESC_UNITS = re.compile("⟦1E⟧(?:⟦[0-9A-Fa-f]{2}⟧|.)|⟦23⟧(?:⟦[0-9A-Fa-f]{2}⟧|.){2}", re.S)


def escape_units(text: str) -> List[str]:
    """Operands that belong to a token but display as plain characters:
    the icon id after 0x1E (``eMessage``: "30 is followed by an icon id")
    and the two digits of a ``#xx`` escape whose glyph has no Unicode
    letter (``⟦23⟧8B``, a button icon). They must survive translation
    unchanged, like the token itself."""
    return sorted(_ESC_UNITS.findall(text))


def _fields_for(iso_path: Optional[str]) -> Tuple[List[Field], Dict[str, bytes]]:
    if iso_path:
        return scan(iso_path)
    if _LAST and _LAST[0] in _CACHE:
        return _CACHE[_LAST[0]]
    raise TxBuildError("txfam_fix.validate: call rows(iso) first or pass iso_path")


def _plan(fields: List[Field], objs: Dict[str, bytes], edits: Dict[str, str]):
    """(errors per id, {path: {key: bytes}})"""
    by_id = {f.id: f for f in fields}
    errs: Dict[str, List[str]] = {}
    new: Dict[str, Dict[tuple, bytes]] = {}
    for eid, text in edits.items():
        f = by_id.get(eid)
        if f is None:
            errs[eid] = ["unknown txfam_fix id"]
            continue
        if f.lock:
            if text != f.src:
                errs[eid] = [f"locked: {f.lock}"]
            continue
        e: List[str] = []
        # evtitem: name and description share one record; the builder checks
        # the pair (the row's lim is the room left by the retail partner)
        lim = None if f.path == P_EVTITEM else f.lim
        res = check(text, f.src, FIELD, "c0", limit_bytes=None if lim is None else lim - 1)
        e += res["errors"]
        if escape_units(text) != escape_units(f.src):
            e.append(f"icon / escape operands differ from the source: keep "
                     f"{escape_units(f.src)}")
        if not e:
            try:
                data, _ = encode(f, text)
            except TxEncodeError as x:
                e += [f"position {p}: {c!r}: {m}" for p, c, m in x.problems]
            else:
                if lim is not None and len(data) + 1 > lim:
                    e.append(f"{len(data)} bytes + NUL, over the {lim}-byte field")
                else:
                    new.setdefault(f.path, {})[f.key] = data
        if e:
            errs[eid] = e
    # shared budgets: build every touched object once
    for path, slots in new.items():
        try:
            _builder(path)(objs[path], slots)
        except TxBuildError as x:
            for f in fields:
                if f.path == path and f.key in slots and f.id in edits:
                    errs.setdefault(f.id, []).append(str(x))
    return errs, new


def validate(edits: Dict[str, str], iso_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Errors per id (empty dict = every edit can be built). Uses the fields of
    the last ``rows()`` call unless ``iso_path`` is given."""
    fields, objs = _fields_for(iso_path)
    errs, _ = _plan(fields, objs, {k: v for k, v in edits.items() if k.startswith("fix:")})
    return errs


def apply(out_iso: str, retail_iso: str, edits: Dict[str, str], log=print) -> List[str]:
    """Write every fix: edit into ``out_iso`` in place (a fresh clone of
    ``retail_iso``), building each object from the retail bytes."""
    if Path(out_iso).resolve() == Path(retail_iso).resolve():
        raise TxBuildError("refusing to write the retail ISO")
    fields, objs = scan(retail_iso)
    mine = {k: v for k, v in edits.items() if k.startswith("fix:")}
    errs, new = _plan(fields, objs, mine)
    if errs:
        raise TxBuildError("txfam_fix: %d invalid edit(s): %s" % (
            len(errs), "; ".join(f"{k}: {v[0]}" for k, v in list(errs.items())[:5])))
    payloads = {}
    for path, slots in new.items():
        payloads[(CHAIN, path)] = _builder(path)(objs[path], slots)
    if not payloads:
        return []
    reports = repack.patch_iso(out_iso, payloads, verbose=False, grow=True)
    done = []
    for r in reports:
        where = f", relocated to sector {r.relocated_lba}" if r.relocated_lba is not None else ""
        log(f"fix: {r.path}: {r.old_size} -> {r.new_size} bytes{where}")
        done.append(r.path)
    return done


# -- selftest ----------------------------------------------------------------------

FR_ES = [
    "Clé du maître", "Élément très rare", "À bientôt, Noël !", "Ça coûte cher",
    "« Où êtes-vous ? »", "Œuvre d'art", "Forêt", "Côte", "Mûre", "Maïs", "Île",
    "¿Dónde está la llave?", "¡Ánimo!", "Niño pequeño", "Pingüino", "Información útil",
    "Canción", "Salón", "Château", "Leçon", "Sœur", "Señal débil", "Sí, señor", "Être ou ne pas être",
    "Après la guerre, la paix règne à jamais sur l'île.",
    "Le cœur du vaisseau bat encore ; où es-tu, Shion ?",
    "¿Quién está ahí? ¡Responde, por favor! Áñ íóú ü",
]
ALL_LETTERS = "é è ê ë à â ç ô û ù ï î œ « » É À Ç ñ ¿ ¡ á í ó ú ü"


def _object_bytes(iso_path: str, paths) -> Dict[str, bytes]:
    want = {p.lower() for p in paths}
    return _read_objects(iso_path, lambda p: p.lower() in want)


def _toc_bytes(iso_path: str) -> bytes:
    from chains import CHAINS
    iso = IsoImage(iso_path)
    try:
        return bytes(iso.read_file(CHAINS[CHAIN][0]))
    finally:
        iso.close()


def _clone(retail: str, dst: str) -> None:
    if os.path.exists(dst):
        os.remove(dst)
    if sys.platform == "darwin":
        subprocess.run(["cp", "-c", retail, dst], check=True)
    else:
        subprocess.run(["cp", "--reflink=always", retail, dst], check=True)


def selftest(iso_path: str, scratch: Optional[str] = None) -> int:
    t0 = time.time()
    ok = True

    def bad(msg):
        nonlocal ok
        ok = False
        print("FAIL:", msg)

    fields, objs = scan(iso_path)
    t_rows = time.time() - t0
    r = rows(iso_path)
    ids = [x["id"] for x in r]
    if len(ids) != len(set(ids)):
        bad("duplicate ids")
    print(f"rows: {len(r)} in {t_rows:.2f} s")
    cats: Dict[str, List[int]] = {}
    for x in r:
        c = cats.setdefault(x["cat"], [0, 0, 0, 0])
        c[0] += 1
        c[1] += x["hide"] is None
        c[2] += x["hide"] is not None
        c[3] += x["lock"] is not None
    for c, (n, vis, hid, lck) in cats.items():
        print(f"  {c:24s} {n:5d} rows  visible {vis:5d}  hidden {hid:4d}  locked {lck:4d}")

    # think disassembly agrees with the decomp documentation (826 MSG, 87 PRINT)
    msg = sum(1 for f in fields if f.kind == "think" and f.ctx.startswith("MSG"))
    prt = sum(1 for f in fields if f.kind == "think" and f.ctx.startswith("PRINT"))
    print(f"think: {msg} MSG + {prt} PRINT strings reached")
    if (msg, prt) != (826, 87):
        bad("think string counts differ from 826 MSG / 87 PRINT")

    # 1. identity: every builder, every slot set to its own retail bytes
    by_path: Dict[str, Dict[tuple, bytes]] = {}
    for f in fields:
        if not f.lock:
            by_path.setdefault(f.path, {})[f.key] = f.raw
    for path, slots in by_path.items():
        if _builder(path)(objs[path], slots) != objs[path]:
            bad(f"identity rebuild differs: {path}")
    print(f"identity rebuild: {len(by_path)} objects byte-exact" if ok else "identity rebuild FAILED")
    # display round trip of every source
    for f in fields:
        if f.lock:
            continue
        try:
            data, _ = from_display(f.src, FIELD, "c0", FONT1)
            if f.kind == "card":
                data = print_to_card(data)
        except (TxEncodeError, TxBuildError) as e:
            continue      # retail-only spellings (e.g. card backslash forms) keep their bytes
        if f.kind == "card":
            same = card_to_print(data) == card_to_print(f.raw) or _decode_bytes(data, "card") == f.src
        else:
            same = to_display(data, "c0", FIELD, FONT1) == f.src
        if not same:
            bad(f"display round trip: {f.id}")

    scratch = scratch or tempfile.gettempdir()
    Path(scratch).mkdir(parents=True, exist_ok=True)
    clone = os.path.join(scratch, f"txfam_fix_selftest_{os.getpid()}.iso")
    try:
        # 2. identity through apply on a clone
        t1 = time.time()
        _clone(iso_path, clone)
        sample = [f for f in fields if not f.lock]
        ident = {f.id: f.src for f in sample}
        apply(clone, iso_path, ident, log=lambda m: None)
        paths = sorted({f.path for f in fields})
        after = _object_bytes(clone, paths)
        same = all(after[p] == objs[p] for p in paths)
        if not same or _toc_bytes(clone) != _toc_bytes(iso_path):
            bad("identity apply changed the clone")
        print(f"identity apply: {len(ident)} rows, {len(paths)} objects byte-identical "
              f"({time.time() - t1:.2f} s)")
        os.remove(clone)

        # 3. French + Spanish sample
        t2 = time.time()
        _clone(iso_path, clone)
        edits: Dict[str, str] = {}
        expect: Dict[str, str] = {}
        per_cat: Dict[str, int] = {}
        k = 0
        for f in sample:
            if f.hide or "⟦" in f.src or "/[" in f.src or "%" in f.src:
                continue
            if per_cat.get(f.cat, 0) >= 12:
                continue
            lim = (f.lim - 1) if f.lim else 120
            cands = FR_ES[k % len(FR_ES):] + FR_ES[:k % len(FR_ES)]
            if f.cat == CAT_CARD and not f.lim and ALL_LETTERS not in edits.values():
                cands = [ALL_LETTERS] + cands          # every sample letter, through the card escapes
            text = None
            for t in cands:
                try:
                    data, _ = encode(Field(**{**f.__dict__, "src": "\0"}), t)
                except (TxEncodeError, TxBuildError):
                    continue
                if len(data) <= lim and (f.kind != "think" or len(data) + 1 >= 2):
                    text = t
                    break
            k += 1
            if text is None:
                continue
            edits[f.id] = text
            per_cat[f.cat] = per_cat.get(f.cat, 0) + 1
            if f.kind == "think":
                data, _ = encode(Field(**{**f.__dict__, "src": "\0"}), text)
                padded = think_pad(data, f.lim).rstrip(b"\0")
                expect[f.id] = to_display(padded, "c0", FIELD, FONT1)
            else:
                expect[f.id] = text
        errs = validate(edits, iso_path)
        if errs:
            bad(f"sample does not validate: {list(errs.items())[:3]}")
        # an over-long text must be refused without writing
        long_id = next(f.id for f in sample if f.cat == CAT_SAVEMAP)
        if not validate({long_id: "é" * 40}, iso_path):
            bad("over-long save-point name was accepted")
        # card names: the deck lists copy 32 bytes (lead byte at 31 would skip the NUL)
        name_id = next(f.id for f in sample if f.id.endswith(":name") and f.cat == CAT_CARD)
        if not validate({name_id: "Croiseur lourd de la Confédéré"}, iso_path):
            bad("33-byte card name was accepted")
        if validate({name_id: "Croiseur lourd de la Confédér"}, iso_path):
            bad("30-byte card name was refused")
        apply(clone, iso_path, edits, log=lambda m: None)
        _CACHE.clear()
        fields2, objs2 = scan(clone)
        by2 = {f.id: f for f in fields2}
        nchk = 0
        for f in fields:
            g = by2.get(f.id)
            if g is None:
                bad(f"row vanished: {f.id}")
                continue
            if f.id in edits:
                nchk += 1
                if g.src != expect[f.id]:
                    bad(f"{f.id}: re-read {g.src!r} != {expect[f.id]!r}")
            elif g.raw != f.raw or g.src != f.src:
                bad(f"untouched row changed: {f.id}")
        for p in objs:
            if not any(f.path == p and f.id in edits for f in fields) and objs2.get(p) != objs[p]:
                bad(f"untouched object changed: {p}")
        print(f"French/Spanish sample: {len(edits)} rows edited "
              f"({', '.join(f'{c} {n}' for c, n in per_cat.items())}), "
              f"{nchk} re-read exactly, untouched rows byte-identical ({time.time() - t2:.2f} s)")
        used = set("".join(edits.values()))
        missing = [c for c in ALL_LETTERS.split() if c not in used]
        if missing:
            bad(f"sample did not exercise {missing}")
    finally:
        if os.path.exists(clone):
            os.remove(clone)
        _CACHE.clear()
    print(f"total {time.time() - t0:.2f} s")
    print("selftest OK" if ok else "selftest FAILED")
    return 0 if ok else 1


def _decode_bytes(data: bytes, kind: str) -> str:
    return to_display(card_to_print(data) if kind == "card" else data, "c0", FIELD, FONT1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("selftest")
    ps.add_argument("--iso", required=True)
    ps.add_argument("--scratch", help="folder for the copy-on-write clone (default: temp dir)")
    pd = sub.add_parser("dump")
    pd.add_argument("--iso", required=True)
    pd.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest(a.iso, a.scratch)
    out = json.dumps(rows(a.iso), ensure_ascii=False, indent=1)
    if a.out:
        Path(a.out).write_text(out, encoding="utf-8")
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
