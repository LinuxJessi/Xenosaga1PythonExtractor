"""txfam_ovl.py — menu, item, skill and battle words for the translation editor.

The third text family: C string literals inside the boot ELF and the five
code overlays (``ovltext.py`` reads and rewrites them). This module turns
``ovltext.Overlay`` into editor rows and writes a translation back.

Why a family module and not just ``ovl-export``: the executables hold
9,335 referenced strings, and most are not words a player sees (asset
ids, file names, printf formats, asserts, JNI symbols, Japanese debug
pages). Changing an id or a path breaks loading, so those rows are
hidden and locked. What is left is grouped by the symbol that holds the
reference (every executable keeps its ``.symtab``): ``OV01 accNameTbl``,
``SLUS_204.69 msg00.1`` and so on.

Which printer draws a string decides how it is encoded:

* **battle** — OV01 ``menuFontPut`` (see ``statfont.py``): the command
  bars and unit names. From the matching decomp (``src/ov01/menu.c``):
  ``monsNameDisp`` draws ``enemyNameTbl`` / ``playerNameTbl`` entries,
  ``menuKeyGuideHelp`` draws the name field of ``nrmNameTbl``,
  ``spcNameTbl`` and ``wepNameTbl`` records, the ``atr1Name`` /
  ``atr2Name`` / ``atr3Name`` words, and its own literals ("End",
  "Menu"). Enemy and weapon names are also drawn by the field printer
  (enemy: the Analyze window, ``specAnalyzeProc`` -> ``eBattleWinOpen3``;
  weapon: the "Changed to ..." / "Stole ..." battle messages,
  ``itemNameGet`` -> ``battleMsg``). No byte encoding draws an accented
  letter right in both: the grid's ``#XX`` escapes are proportional-block
  cells to the field printer, and the field font's two-byte codes are
  blank cells in the grid (``menuDFontIdxGet`` knows only ／ 　 ・ ＾).
  Plain A-Z a-z 0-9 space . ' / - draw the same in both. The row ctx says
  where each printer draws it and ``warnings()`` shows both renderings.
* **ascii** — ``PrintTagFont`` ribbons and tags (``eTagFontSet``, window
  ``tag_id`` titles): an ASCII-only sprite font (SCRIPTS.md, "Known limit:
  window title ribbons"). The texts are the ones the decomp passes there,
  plus ``pseudoloc.EXCLUDE``. Its control bytes are its own (``txcodec``
  font "ascii"): a leading 0x01 right-aligns and has no operand, 0x03 takes
  one byte of extra advance, a digit ends the label.
* **field** — everything else (``xglFont``, font1).

Battle-font text goes through ``txcodec`` font "battle": letters the
byte table lacks are written as ``#XX`` grid-cell escapes, so the
table at ``@a46af8`` is never changed (that row is locked).

Safety rules the writer adds on top of ``Overlay.apply``:

* ``Str.slot`` runs to the next non-zero byte, and sometimes that zero run
  holds data something points at ("Survival Wear" @a4a390: +16 is an empty
  string referenced 393 times). Each slot is clamped to the next address
  any pointer word, ``lui`` pair or ``$gp`` offset forms, so a long text
  moves instead of overwriting it. A string something points *into* is
  never rewritten in place.
* A string whose ``lui`` also forms another address, or that is
  addressed off ``$gp``, cannot move; its ``lim`` is the clamped slot and
  ``validate`` gives the shorten-to-N advice.
* An overlay may not grow past 0xA80000, where the game's own allocator
  starts (``InitializeSystem``, FORMATS.md).
* The boot ELF's third load segment is a byte copy of OV02 at the same
  addresses: those strings are one row (keyed by OV02) and written to both.

Everything is read from the retail ISO and keyed by retail address; a
build always starts from retail bytes, one ``Overlay.apply`` per
executable with every edit for it.

    python3 txfam_ovl.py selftest --iso RETAIL
    python3 txfam_ovl.py dump --iso RETAIL --out rows.json
"""
from __future__ import annotations

import argparse
import bisect
import copy
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from array import array
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import ovltext
import txcodec as tc
from iso9660 import IsoImage
from textpack import decode_text, encode_text

FAM = "ovl"
BOOT = "SLUS_204.69"
DUP_OVERLAY = "OV02.OVL"          # the boot ELF's third PT_LOAD is a copy of it
ALLOC_START = 0xA80000            # the game's allocator; overlays end below it
BATTLE_TABLE_VA = 0xA46AF8        # statfont.TABLE_VA: menuFontIdxGet's byte table

CAT_ITEMS = "Items & equipment"
CAT_SKILLS = "Skills & ether"
CAT_ENEMIES = "Enemies"
CAT_HUD = "Battle HUD"
CAT_FIELD = "Field menu & system"
CAT_OV02 = "Title / install / U.M.N. UI"
CAT_CARD = "Card game"
CAT_CASINO = "Casino"
CAT_ROBOT = "Robot battles"
FILE_CAT = {BOOT: CAT_FIELD, "OV02.OVL": CAT_OV02, "OV10.OVL": CAT_CARD,
            "OV11.OVL": CAT_CASINO, "OV12.OVL": CAT_ROBOT}

# OV01 symbol -> category (symbols from the overlay's .symtab)
OV01_ITEMS = {"accNameTbl", "wepNameTbl", "itmNameTbl", "bltNameTbl", "engNameTbl", "frmNameTbl"}
OV01_SKILLS = {"sklNameTbl", "ethNameTbl", "spcNameTbl", "nrmNameTbl"}
OV01_ENEMIES = {"enemyNameTbl"}
OV01_HUD = {"playerNameTbl", "ziggyName", "statNameTbl", "aa1", "aa2", "ab", "ba", "bb", "ma", "mb",
            "atr1Name", "atr2Name", "atr3Name", "menuKeyGuideHelp", "dmgValPut",
            "menuOpenEth", "menuOpenItm"}
OV01_UI = OV01_ITEMS | OV01_SKILLS | OV01_ENEMIES | OV01_HUD
# EthName records (name, help, initial): 12 bytes, decomp data_eth_name_get.c
RECORD_TABLES = {n: 12 for n in OV01_ITEMS | OV01_SKILLS}
RECORD_FIELDS = {0: "name", 4: "description", 8: "initial"}
# asset / motion / sound ids the code looks up by name
ID_TABLES = {"motFile", "nameTbl", "wpnTbl", "enemyBaseSeName", "thinkStrTbl", "cmdStrTbl"}

# menuFontPut (battle grid) callers, decomp src/ov01/menu.c
BATTLE_PTR_TABLES = {"enemyNameTbl", "playerNameTbl", "ziggyName", "atr1Name", "atr2Name", "atr3Name"}
BATTLE_NAME_TABLES = {"nrmNameTbl", "spcNameTbl", "wepNameTbl"}     # name field only
BATTLE_FUNCS = {"menuKeyGuideHelp"}
# which menuFontPut caller draws each of them
BATTLE_CALLER = {"enemyNameTbl": "monsNameDisp", "playerNameTbl": "monsNameDisp",
                 "ziggyName": "monsNameDisp"}
# where the field printer draws the same bytes (decomp src/ov01/battle_init.c)
ALSO_FIELD = {"enemyNameTbl": "the Analyze window (specAnalyzeProc -> eBattleWinOpen3)",
              "wepNameTbl": "battle messages 'Changed to ...' / 'Stole ...' "
                            "(itemNameGet -> battleMsg)"}

# PrintTagFont texts: what the decomp passes to eTagFontSet / window tag_id /
# title (src/main/*.c, src/ov01/menu.c, src/ov02/*.c), as raw bytes
TAG_RAW = {
    b"Menu", b"Select", b"Set\x03,Lv\x03\x12T.Pts", b"\x01Time", b"\x01Money", b"Combatant",
    b"Configuration", b"\x01Num", b"No.", b"List\x03\x9cE.Pts", b"List\x03\x9cEp",
    b"List\x03\x9aE.Pts", b"List\x03\x9dWt", b"Result", b"\x01FHP", b"\x01Hp", b"Information",
    b"\x01T.Pts", b"List\x03\x99Num\x03\x99\x033Num", b"List\x03\x8dNum", b"List",
    b"Ether List", b"Items List", b"Bullet", b"Pay", b"List                   Price",
    b"Target", b"Party", b"\x01Clear Time", b"\x01E.Pts", b"\x01S.Pts", b"Level up",
    b"\x01Wt", b"\x01S.Lv", b"List\x03\x8d\x03\x18Price", b"List\x03\x8d\x03\x0cStock",
    b"\x01Stock", b"\x01Target", b"\x01Equip", b"\x01Price", b"Buy", b"Sell", b"\x01Tech",
    b"\x01Speed", b"\x01Wait", b"\x01Exp", b"\x01Total", b"\x01Result", b"\x01DPOW",
    b"\x01PARM", b"\x01EARM", b"\x01AGL", b"\x01WAGL", b"\x01Weight", b"\x01Reserve",
    b"\x01Str", b"\x01Vit", b"\x01Eatk", b"\x01Edef", b"\x01Dex", b"\x01Eva", b"\x01Agl",
    b"\x01Current", b"Menu         Party", b"Selection", b"SetList/HI", b"SetList/LW",
    b"Shion:Lv", b"List\x03\x90S.Lv\x03\x07S.Pts", b"Set Skills", b"Set", b"T.Pts",
    b"Location", b"Rare item", b"List\x03P\x03>Eqp", b"Sort", b"Reserve",
    b"Registered Reserve", b"Registered Combatant",
}
try:
    from pseudoloc import EXCLUDE as _PSEUDO_EXCLUDE
    TAG_RAW |= {t.encode("ascii") for t in _PSEUDO_EXCLUDE}
except Exception:                      # pseudoloc is optional for this module
    pass
TAG_FILES = {BOOT, "OV01.OVL", "OV02.OVL"}

_DEBUG_OWNER = re.compile(r"Test|test|Debug|debug|Dump|dump|^_?sce|^Init|^init|^JNI_|"
                          r"^default_native|^instructionCodeName|Heap|FileSys|Error|Assert|"
                          r"Memdump|^think|^MCam|^MMv|^MEf|^dataUnitFile|^Xrg|^Rg.*(DB|Load)|"
                          r"^PauseMenuPageEnemy$|^cmd[A-Z]|printf|^_alalc|^Ssd|^_send_to_iop|"
                          r"^(Pad|Req)StateStr$|^DB_|^Drill|^updateWind$|^PrintDisp$|^ACT_info$")
_ASSET_OWNER = re.compile(r"^(AID|CID|MID|WID|EID|maptbl|_loadComboData|tiSymbol)")
# symbols whose strings are keywords the code compares against or names it loads
# (spec-file keys, motion/sound names, picture tables, a magic string)
_KEYWORD_OWNER = re.compile(r"^(regName|szName|plChaBaseSeName|sndMuNameExt\d|MUSICLIST\.\d+|"
                            r"RgActorMotIDGetFromName|s_aszVer3TimerName|_LoadVersion3|"
                            r"RgRobotSpecReadFromText|RgBgBuilderBuildFromText|_FindInPrepare|"
                            r"_AnaAutoHoming|_Read[A-Z]\w*|LoadRgBxx_sub|_LoadSeq\w*|"
                            r"s_szMagicString|s_aWepPicTbl\.\d+|s_ainPicNameTbl\.\d+|"
                            r"s_apszFileTbl\.\d+|xglCd\w+|GameCFSoundReload|nmlModelLexDataCheck|"
                            r"On_Ladder|Call_JavaMethod|sresLoadCommonMemory)$")
_PRINTF = re.compile(r"%[-+0# ]*\d*(?:\.\d+)?[lh]?[diouxXcsfegp]")


# -- text classification ------------------------------------------------------

def classify(text: str, file: str = "") -> str:
    """Rough kind of a decoded retail string (heuristics from the ovltext survey,
    tuned by reading the visible rows of the USA disc)."""
    plain = re.sub(r"[\x00-\x1f]", "", text)
    if re.match(r"^Java_|^xeno/|^java/|^\(.*\)[A-Z\[]|^<init>$|^<clinit>$", text):
        return "java"
    if (re.search(r"[\\/]", text) and not re.search(r"[A-Za-z]{3,} [A-Za-z]{3,}", text)) \
            or re.fullmatch(r"[\w\-]*\.([a-z0-9]{2,5}|[A-Z]{3})|\w+\d:.*|[a-z]+\d*:", plain):
        return "path"
    if _PRINTF.search(text) or text.endswith("\\n") or re.search(r"\(\);|^\s*(\*\*|>>>|---)", text) \
            or re.match(r"^\n?[A-Za-z_]\w*: ", text) \
            or (text.endswith("\n") and (re.search(r"[:!]|err|fail|faild|over ?flow", text, re.I)
                                         or re.fullmatch(r"\w+\n", text))):
        return "debug"
    if re.search(r" != NIL|->|==|!=|&&|<=|>=|\bRG_[A-Z_]+|assert", text) \
            or re.fullmatch(r"\w+ [<>] \w+|_?\w+\(.*\).*", text):
        return "assert"
    if re.search(r"(Build:)?\b[A-Z][a-z]{2} +\d{1,2} +(19|20)\d\d\b", text):
        return "debug"
    if sum(1 for c in text if ord(c) < 0x20 and c != "\n") * 3 > len(text) and len(text) > 8:
        return "binary"
    if file == "OV12.OVL" and re.match(r"[a-z]", text) and not re.match(r"(left|right|back) ", text):
        return "debug"
    if re.search(r"[぀-ヿ一-鿿＀-￯]", text):
        return "japanese"
    if re.fullmatch(r"[A-Za-z]{0,6}_?[A-Za-z0-9_]*\d+[A-Za-z0-9_ ]*", text) \
            or re.fullmatch(r"[a-z0-9_]+", text) or re.fullmatch(r"[A-Za-z0-9]*_[A-Za-z0-9_]+", text) \
            or re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)+", text) \
            or re.fullmatch(r"[a-z]+[A-Z]\w*|[A-Z][a-z]+[A-Z][A-Za-z]*", text) \
            or (file == "OV12.OVL" and re.fullmatch(r"[A-Z0-9]+(-[A-Z0-9]+)+", text)
                and re.search(r"\d", text)):
        return "id"
    if len(plain) < 2:
        return "junk"
    if " " in plain.strip() or re.search(r"[a-z]{3,}|[A-Z]{3,}|[A-Z][a-z]", plain):
        return "ui"
    return "other"


# -- per-executable analysis -------------------------------------------------

@dataclass
class Item:
    """One referenced string of one executable, with what the editor needs."""
    file: str
    va: int
    raw: bytes
    slot: int                 # ovltext's slot (to the next non-zero byte)
    safe_slot: int            # clamped to the next referenced address
    interior: bool            # something points inside the string or at its NUL
    owners: List[str]
    fields: List[str]         # record field names where known ("name", "description")
    order: int
    blocked: Optional[str]    # why it cannot move, or None
    battle: str = ""          # menuFontPut caller that draws it (battle grid), or ""
    also_field: str = ""
    ptrword: bool = False     # the "string" is the low bytes of an address word


@dataclass
class Exe:
    name: str
    retail: bytes
    ov: ovltext.Overlay
    items: Dict[int, Item] = field(default_factory=dict)


def _symbols(ov: ovltext.Overlay) -> List[Tuple[int, int, str]]:
    """(start, end, name) of every sized FUNC/OBJECT symbol, sorted."""
    symtab = next((s for s in ov.sections if s[1] == 2), None)
    if symtab is None:
        return []
    link = struct.unpack_from("<I", ov.data, symtab[0] + 24)[0]
    names = ov.sections[link][3]
    out = []
    for i in range(symtab[4] // 16):
        st_name, value, size, info, _, _ = struct.unpack_from("<IIIBBH", ov.data, symtab[3] + i * 16)
        if size and (info & 0xF) in (1, 2):
            e = ov.data.index(b"\0", names + st_name)
            out.append((value, value + size, ov.data[names + st_name:e].decode("latin-1")))
    out.sort()
    return out


def _targets(ov: ovltext.Overlay) -> List[int]:
    """Every address something points at: non-code pointer words, lui pairs, gp offsets."""
    t = set()
    for seg in ov.segments:
        n = (seg.filesz // 4) * 4
        words = array("I", bytes(ov.data[seg.off:seg.off + n]))
        if sys.byteorder != "little":
            words.byteswap()
        code = ov._code
        base = seg.off
        for i, w in enumerate(words):
            if w and not code[base + 4 * i]:
                t.add(w)
    for uses in ov.lui_uses.values():
        for _, a in uses:
            t.add(a)
    if not ov._plain:
        t.update(ov._gp_targets())
    return sorted(t)


def _append_start(ov: ovltext.Overlay) -> Optional[int]:
    if ov._plain:
        seg = ov.segments[0]
        return seg.vaddr + seg.filesz
    if ov.zone is not None:
        start = ov.zone[0]
        old = ov._zone_segment(start)
        if old is not None:
            start += (old.filesz + 3) & ~3
        return start
    return None


def _blocked(ov: ovltext.Overlay, s: ovltext.Str, start: Optional[int],
             gp: Dict[int, int]) -> Optional[str]:
    """Would moving this string be refused? (same tests as Overlay.apply, at
    the first append address; the real build appends later, within 64K)."""
    if start is None:
        return "this executable cannot grow"
    if s.va in gp:
        return "addressed off $gp"
    lo = start & 0xFFFF
    for r in s.refs:
        if r.kind != "hilo":
            continue
        hi = (start >> 16) + (1 if (r.signed and lo & 0x8000) else 0)
        lui = struct.unpack_from("<I", ov.data, r.lui_off)[0]
        if (lui & 0xFFFF) != hi and any(a != s.va for _, a in ov.lui_uses.get(r.lui_off, [])):
            return f"its lui at {ov.off_to_va(r.lui_off):#x} also forms other addresses"
    return None


def _analyse(name: str, data: bytes, iso: IsoImage) -> Exe:
    ov = ovltext.Overlay(name, data)
    ovltext.set_zone(ov, iso)
    ex = Exe(name, data, ov)
    syms = _symbols(ov)
    starts = [a for a, _, _ in syms]
    targets = _targets(ov)
    gp = ov._gp_targets() if not ov._plain else {}
    start = _append_start(ov)
    target_set = set(targets)

    def owner(va):
        i = bisect.bisect_right(starts, va) - 1
        return syms[i] if i >= 0 and syms[i][0] <= va < syms[i][1] else None

    for va, s in sorted(ov.strings.items()):
        owners: Counter = Counter()
        fields: List[str] = []
        order = None
        battle = ""
        also = ""
        for r in s.refs:
            rva = ov.off_to_va(r.off)
            o = owner(rva) if rva is not None else None
            oname = o[2] if o else "(none)"
            owners[oname] += 1
            if o and r.kind == "ptr":
                rec = RECORD_TABLES.get(oname)
                foff = (rva - o[0]) % rec if rec else None
                if rec:
                    fields.append(RECORD_FIELDS.get(foff, f"+{foff}"))
                pos = (rva - o[0]) // (rec or 4)
                order = pos if order is None else min(order, pos)
                if name == "OV01.OVL" and (oname in BATTLE_PTR_TABLES
                                           or (oname in BATTLE_NAME_TABLES and foff == 0)):
                    battle = battle or BATTLE_CALLER.get(oname, "menuKeyGuideHelp")
                    also = also or ALSO_FIELD.get(oname, "")
            if name == "OV01.OVL" and r.kind == "hilo" and oname in BATTLE_FUNCS:
                battle = battle or oname
        i = bisect.bisect_right(targets, va)
        nxt = targets[i] if i < len(targets) else None
        safe = s.slot
        interior = False
        if nxt is not None and nxt < va + s.slot:
            if nxt <= va + len(s.raw):
                interior = True
                safe = 0
            else:
                safe = nxt - va
        ptrword = False
        if va % 4 == 0 and len(s.raw) <= 3:
            # the low bytes of an address word: a pointer to a known string, or an
            # aligned word pointing at code (a jump table) or at referenced data
            w = struct.unpack_from("<I", ov.data, s.off)[0]
            woff = ov.va_to_off(w)
            ptrword = w in ov.strings or (w % 4 == 0 and woff is not None
                                          and (ov._code[woff] or w in target_set))
        ex.items[va] = Item(name, va, s.raw, s.slot, safe, interior,
                            [o for o, _ in owners.most_common()], sorted(set(fields)),
                            order if order is not None else 0,
                            _blocked(ov, s, start, gp), battle, also, ptrword)
    return ex


# -- rows ---------------------------------------------------------------------

@dataclass
class State:
    retail_iso: str
    exes: Dict[str, Exe]
    rows: List[dict]
    by_id: Dict[str, dict]
    target: Dict[str, List[Tuple[str, int]]]     # id -> [(file, va)] it writes
    seconds: float = 0.0


_STATE: Dict[str, State] = {}
_LAST: Optional[State] = None


def row_id(file: str, va: int) -> str:
    return f"{FAM}:{file}:{va:x}"


def _primary_owner(file: str, owners: List[str]) -> str:
    if file == "OV01.OVL":
        for o in owners:
            if o in OV01_UI:
                return o
    named = [o for o in owners if o != "(none)"]
    return (named or owners or ["(none)"])[0]


def _category(file: str, owner: str) -> str:
    if file == "OV01.OVL":
        if owner in OV01_ITEMS:
            return CAT_ITEMS
        if owner in OV01_SKILLS:
            return CAT_SKILLS
        if owner in OV01_ENEMIES:
            return CAT_ENEMIES
        return CAT_HUD
    return FILE_CAT.get(file, CAT_FIELD)


def _hide_lock(it: Item, text: str, owner: str) -> Tuple[Optional[str], Optional[str]]:
    """(hide, lock) reasons for one string."""
    if it.file == "OV01.OVL" and it.va == BATTLE_TABLE_VA:
        return "battle font byte table", "battle font byte table (menuFontIdxGet)"
    if it.ptrword:
        return "pointer word, not text", "an address in the code's data that only looks like text"
    tag = it.file in TAG_FILES and (it.raw in TAG_RAW or bool(_TAG_PREFIX.fullmatch(it.raw)))
    kind = "ui" if tag else classify(text, it.file)
    if "⟦" in text or kind == "binary":
        return "binary data, not text", "binary data that only looks like a string"
    ui_table = it.file == "OV01.OVL" and owner in OV01_UI
    if owner in ID_TABLES or _ASSET_OWNER.match(owner):
        return f"asset id ({owner})", f"identifier the game looks up by name ({owner})"
    if _KEYWORD_OWNER.match(owner) and not ui_table:
        return f"keyword or name the code uses ({owner})", f"keyword or file name {owner} compares or loads"
    if kind == "path":
        return "file path", "file name the game loads"
    if kind == "java":
        return "Java native name", "Java/JNI symbol name"
    if kind == "id" and not ui_table:
        return "identifier", "identifier, not text"
    lock = None
    if it.interior and it.blocked:
        lock = "other data points into this string and it cannot move"
    if kind == "id" and ui_table and re.fullmatch(r"Reserve\d*", text):
        return "placeholder entry", lock
    if kind == "debug":
        return "debug print / format string", lock
    if kind == "assert":
        return "assertion text", lock
    if kind == "japanese":
        return "Japanese developer text", lock
    if kind == "junk":
        return "fragment", lock
    if owner == "s_aszCommandName":
        return "four-letter command codes (robot-battle debug display)", lock
    if all(_DEBUG_OWNER.search(o) for o in it.owners if o != "(none)") \
            and any(o != "(none)" for o in it.owners) and not ui_table:
        return f"only used by {owner} (debug/system code)", lock
    if kind == "other" and not ui_table:
        return "not word-like", lock
    return None, lock


_TAG_PREFIX = re.compile(rb"\x01[\x20-\x7e]{1,15}")


def _font_of(it: Item) -> str:
    if it.battle:
        return tc.BATTLE
    if it.file in TAG_FILES and (it.raw in TAG_RAW or _TAG_PREFIX.fullmatch(it.raw)):
        # every \x01-prefixed label the decomp passes to eTagFontSet is a tag;
        # MenuStatusDisp's "\x01Hp"/"\x01Ep"/"\x01Lv"/"\x01Nextlv" go to
        # END_PRINT_CMD_TAG_FONT directly (src/main/menu_top_command_check.c);
        # the remaining \x01 labels are taken to be tags too (inferred)
        return tc.ASCII
    return tc.FIELD


def _build_rows(exes: Dict[str, Exe], fonts) -> Tuple[List[dict], Dict[str, List[Tuple[str, int]]]]:
    rows: List[dict] = []
    target: Dict[str, List[Tuple[str, int]]] = {}
    boot = exes.get(BOOT)
    dup = exes.get(DUP_OVERLAY)
    dup_lo = dup_hi = None
    if boot is not None and dup is not None:
        seg = dup.ov.segments[0]
        for bs in boot.ov.segments:
            if seg.vaddr <= bs.vaddr < seg.vaddr + seg.filesz:
                dup_lo, dup_hi = bs.vaddr, bs.vaddr + bs.filesz
    font1 = fonts.get(tc.FONT1) if fonts else None
    # name tables (items, skills, enemies...) are drawn in fixed menu columns;
    # with no column width on record, the widest English name of each table
    # (per field: name, description) is its width guide, so a translation
    # wider than every retail entry in that column warns
    name_w: Dict[Tuple[str, str, str], int] = {}
    name_rows: List[Tuple[dict, Tuple[str, str, str]]] = []
    for name, ex in exes.items():
        for va in sorted(ex.items):
            it = ex.items[va]
            writes = [(name, va)]
            lim_items = [it]
            if name == BOOT and dup_lo is not None and dup_lo <= va < dup_hi:
                d = dup.items.get(va)
                if d is not None and d.raw == it.raw:
                    continue                         # shown once, as the OV02 row
            if name == DUP_OVERLAY and boot is not None and dup_lo is not None \
                    and dup_lo <= va < dup_hi:
                b = boot.items.get(va)
                if b is not None and b.raw == it.raw:
                    writes.append((BOOT, va))
                    lim_items.append(b)
            font = _font_of(it)
            src = tc.to_display(it.raw, "c0", font=font)
            owner = _primary_owner(name, it.owners)
            hide, lock = _hide_lock(it, decode_text(it.raw), owner)
            blocked = [x for x in lim_items if x.blocked]
            lim = None
            if blocked:
                lim = max(0, min(x.safe_slot for x in lim_items) - 1)
                if lim < len(it.raw) and lock is None:
                    lock = "other data shares these bytes and the string cannot move"
            ctxs = [owner]
            if it.fields:
                ctxs.append("/".join(it.fields))
            others = [o for o in it.owners if o != owner]
            if others:
                ctxs.append("also " + ", ".join(others[:3]) + ("…" if len(others) > 3 else ""))
            if len(writes) > 1:
                ctxs.append(f"also in {BOOT}")
            if font == tc.BATTLE:
                ctxs.append(f"drawn by the battle grid ({it.battle} -> menuFontPut)"
                            + (f" AND by the field font in {it.also_field}: accented letters "
                               "cannot be right in both, plain A-Z a-z 0-9 . ' / - are"
                               if it.also_field else ""))
            elif font == tc.ASCII:
                ctxs.append("drawn by the tag font (PrintTagFont): A-Z a-z 0-9 : / . - space, "
                            "no accents; a digit ends the label")
            if blocked:
                ctxs.append(f"cannot move: {blocked[0].blocked}")
            measure = px = None
            if font == tc.FIELD and font1 is not None:
                measure = tc.FONT1
            rid = row_id(name, va)
            target[rid] = writes
            nkey = None
            if measure and owner.endswith("NameTbl") and not hide and not lock:
                nkey = (name, owner, "/".join(it.fields))   # name and description columns apart
                try:
                    w = max(font1.width_of_bytes(line) for line in it.raw.split(b"\n"))
                except Exception:
                    w = 0
                name_w[nkey] = max(name_w.get(nkey, 0), w)
            rows.append({"id": rid, "fam": FAM, "cat": _category(name, owner),
                         "grp": f"{name[:-4] if name.upper().endswith('.OVL') else name} {owner}", "ord": it.order * 0x100000 + (va & 0xFFFFF),
                         "src": src, "ctx": "; ".join(ctxs), "font": font, "ctl": "c0",
                         "measure": measure, "px": px, "lim": lim, "lock": lock, "hide": hide,
                         "dup": 0})
            if nkey is not None:
                name_rows.append((rows[-1], nkey))
    for r, nkey in name_rows:
        if name_w.get(nkey):
            r["px"] = name_w[nkey]
    n = Counter(r["src"] for r in rows)
    for r in rows:
        r["dup"] = n[r["src"]] - 1
    return rows, target


def _load(iso_path: str) -> State:
    global _LAST
    key = str(Path(iso_path).resolve())
    st = _STATE.get(key)
    if st is None:
        t = time.time()
        iso = IsoImage(iso_path)
        try:
            exes = {n: _analyse(n, bytes(iso.read_file(n)), iso) for n in ovltext.executables(iso)}
        finally:
            iso.close()
        try:
            fonts = tc.load_fonts(iso_path)
        except Exception:
            fonts = None
        rows, target = _build_rows(exes, fonts)
        st = State(iso_path, exes, rows, {r["id"]: r for r in rows}, target, time.time() - t)
        _STATE[key] = st
    _LAST = st
    return st


def rows(iso_path: str) -> List[dict]:
    """Every referenced string of the executables as editor rows (read-only)."""
    return [dict(r) for r in _load(iso_path).rows]


# -- validate / build ------------------------------------------------------------

def _encode(row: dict, text: str) -> Tuple[bytes, List[str]]:
    return tc.from_display(text, row["font"], "c0")


def _as_ovltext(raw: bytes) -> str:
    """A string Overlay.apply encodes back to exactly ``raw``."""
    s = decode_text(raw)
    if encode_text(s) != raw:
        s = "".join(chr(b) if 0x20 <= b < 0x7F else f"⟦{b:02X}⟧" for b in raw)
    return s


def _fresh(ex: Exe) -> ovltext.Overlay:
    """A private copy of the retail Overlay with every slot clamped to its
    safe size (Overlay.apply mutates the object it is given)."""
    ov = copy.deepcopy(ex.ov)
    for va, s in ov.strings.items():
        s.slot = ex.items[va].safe_slot
    return ov


def _plan(st: State, edits: Dict[str, str]):
    """-> (errors per id, {file: {va: raw}}). No ISO access."""
    errors: Dict[str, List[str]] = {}
    per_file: Dict[str, Dict[int, bytes]] = {}
    owner_of: Dict[Tuple[str, int], str] = {}
    for rid, text in edits.items():
        row = st.by_id.get(rid)
        if row is None:
            errors.setdefault(rid, []).append("no such ovl row")
            continue
        if text == row["src"]:
            continue
        if row["lock"]:
            errors.setdefault(rid, []).append(f"locked: {row['lock']}")
            continue
        res = tc.check(text, row["src"], row["font"], "c0", limit_bytes=row["lim"])
        if not res["ok"]:
            errors.setdefault(rid, []).extend(res["errors"])
            if row["lim"] is not None and res["nbytes"] > row["lim"]:
                why = row["ctx"].split("cannot move: ", 1)[-1]
                errors[rid].append(f"shorten the text to {row['lim']} bytes: it cannot move ({why})")
            continue
        raw, _ = _encode(row, text)
        for file, va in st.target[rid]:
            if raw == st.exes[file].items[va].raw:
                continue
            per_file.setdefault(file, {})[va] = raw
            owner_of[(file, va)] = rid
    return errors, per_file, owner_of


def _apply_exes(st: State, per_file: Dict[str, Dict[int, bytes]], owner_of,
                errors: Dict[str, List[str]]) -> Dict[str, Tuple[bytes, List[str]]]:
    out: Dict[str, Tuple[bytes, List[str]]] = {}
    for file in sorted(per_file):
        ex = st.exes[file]
        ov = _fresh(ex)
        new = {va: _as_ovltext(raw) for va, raw in sorted(per_file[file].items())}
        try:
            log = ov.apply(new, grow=True)
        except ValueError as e:
            for line in str(e).split("\n"):
                m = re.match(r"@([0-9a-f]+): (.*)", line)
                rid = owner_of.get((file, int(m.group(1), 16))) if m else None
                msg = m.group(2) if m else line
                errors.setdefault(rid or f"{FAM}:{file}", []).append(f"{file}: {msg}")
            continue
        if ov._plain:
            seg = ov.segments[0]
            if seg.vaddr + seg.memsz > ALLOC_START:
                errors.setdefault(f"{FAM}:{file}", []).append(
                    f"{file}: grown to {seg.vaddr + seg.memsz:#x}, past {ALLOC_START:#x} where "
                    f"the game's allocator starts; shorten some {file} texts")
                continue
        out[file] = (bytes(ov.data), [f"{file} {x}" for x in log])
    return out


def validate(edits: Dict[str, str], iso_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Errors per row id (empty dict = OK). Uses the retail ISO of the last
    ``rows()`` call unless ``iso_path`` is given. Writes nothing."""
    st = _load(iso_path) if iso_path else _LAST
    if st is None:
        return {f"{FAM}:": ["txfam_ovl.rows(retail) must be called before validate()"]}
    errors, per_file, owner_of = _plan(st, edits)
    _apply_exes(st, per_file, owner_of, errors)
    return errors


_NOTHING = "▯"          # a byte the printer draws nothing for (the pen still moves)


def battle_render(raw: bytes) -> str:
    """What OV01 menuFontPut draws for ``raw`` (decomp src/ov01/menu.c): ``#`` + two
    characters is a grid cell (sscanf "%X"), a byte with bit 7 set starts a pair looked
    up in menuJpText4, any other byte is looked up in the 230-byte table."""
    out: List[str] = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x23:
            m = re.match(rb"[0-9A-Fa-f]{1,2}", raw[i + 1:i + 3])
            cell = int(m.group(0), 16) if m else -1
            out.append(tc.BATTLE_CELLS.get(cell) or (tc._BATTLE_TABLE[cell] if 0 <= cell < 63
                                                     else "?"))
            i += 3
            continue
        if b & 0x80:
            pair = raw[i:i + 2]
            try:
                ch = pair.decode("euc_jp")
            except UnicodeDecodeError:
                ch = ""
            cell = tc._BATTLE_JIS.get(ch, -1)
            out.append(tc.BATTLE_CELLS.get(cell, _NOTHING) if cell >= 0 else _NOTHING)
            i += 2
            continue
        k = tc._BATTLE_TABLE.find(chr(b))
        out.append(_NOTHING if k < 0 else (tc._BATTLE_TABLE[k] if k < 63
                                           else tc.BATTLE_CELLS.get(k, tc._BATTLE_TABLE[k])))
        i += 1
    return "".join(out)


def field_render(raw: bytes, face: str = tc.FONT1) -> str:
    """What the field printer (xglFontPrint / xglFontAscii2Euc) draws for ``raw``:
    ``##`` is '#', ``#`` + two characters is proportional-block cell xx, a pair from
    0xD0 up is a block cell, other pairs are JIS; control bytes are skipped."""
    glyphs = tc.BLOCK_GLYPHS[face]
    out: List[str] = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b < 0x20 and b != 0x0A:
            i += max(1, tc.control_length(raw, i))
            continue
        if b == 0x23:
            if raw[i + 1:i + 2] == b"#":
                out.append("#")
                i += 2
                continue
            hx = raw[i + 1:i + 3]
            xx = sum((int(chr(c), 16) if chr(c) in "0123456789abcdefABCDEF" else 0) << (4 * (1 - k))
                     for k, c in enumerate(hx))
            out.append(glyphs.get(xx, "?"))
            i += 3
            continue
        if b < 0x80:
            out.append({"'": "’", '"': "”"}.get(chr(b), chr(b)))
            i += 1
            continue
        pair = raw[i:i + 2]
        ch = "?"
        if len(pair) == 2 and pair[0] >= 0xD0:
            ch = glyphs.get(tc.block_index((pair[0] << 8) | pair[1]), "?")
        elif len(pair) == 2:
            try:
                ch = pair.decode("euc_jp")
            except UnicodeDecodeError:
                pass
        out.append(ch)
        i += 2
    return "".join(out)


def _safe_both(text: str) -> str:
    """``text`` spelled with what draws the same in the battle grid and the field font."""
    native = set(tc._BATTLE_TABLE)
    out = []
    for c in text:
        f = c if c in native else (tc.fold_ascii(c) or "")
        out.append("".join(x for x in f if x in native))
    return "".join(out)


def renderings(rid: str, text: str, iso_path: Optional[str] = None) -> Dict[str, str]:
    """{printer: what it draws} for a translation of row ``rid``, for every printer
    that draws that row's bytes (battle grid, field font, tag font)."""
    st = _load(iso_path) if iso_path else _LAST
    row = st.by_id.get(rid) if st else None
    if row is None:
        return {}
    raw, _ = _encode(row, text)
    file, va = rid.split(":")[1], int(rid.split(":")[2], 16)
    it = st.exes[file].items[va]
    out: Dict[str, str] = {}
    if row["font"] == tc.BATTLE:
        out[f"battle grid ({it.battle})"] = battle_render(raw)
        if it.also_field:
            out[f"field font, {it.also_field}"] = field_render(raw)
    elif row["font"] == tc.ASCII:
        out["tag font (PrintTagFont)"] = tc.tag_render(raw)
    else:
        out["field font"] = field_render(raw)
    return out


def warnings(edits: Dict[str, str], iso_path: Optional[str] = None) -> Dict[str, List[str]]:
    """Non-blocking notes per id: codec warnings, and for strings two printers draw
    (enemy names: battle grid + Analyze window; weapon names: battle grid + battle
    messages) what EACH printer will show. Only A-Z a-z 0-9 space . ' / - draw the
    same in both: the grid's accent escapes (#XX) are proportional-block cells to the
    field printer, and the field font's two-byte accent codes are blank cells
    (menuDFontIdxGet finds no match) in the grid. No byte encoding does both."""
    st = _load(iso_path) if iso_path else _LAST
    out: Dict[str, List[str]] = {}
    if st is None:
        return out
    for rid, text in edits.items():
        row = st.by_id.get(rid)
        if row is None or text == row["src"]:
            continue
        res = tc.check(text, row["src"], row["font"], "c0", limit_bytes=row["lim"])
        w = list(res["warnings"])
        if res["ok"]:
            r = renderings(rid, text)
            if len(r) > 1 and len(set(r.values())) > 1:
                shown = "; ".join(f"{k} shows {v!r}" for k, v in r.items())
                w.append(f"drawn by two printers that disagree: {shown}. No byte encoding "
                         f"draws accented letters right in both; {_safe_both(text)!r} would "
                         f"(only A-Z a-z 0-9 space . ' / - are shared)")
            elif row["font"] == tc.ASCII:
                raw, _ = _encode(row, text)
                src_raw, _ = _encode(row, row["src"])
                wn, ws = tc.tag_width(raw), tc.tag_width(src_raw)
                if wn > ws:
                    w.append(f"tag label is {wn} px wide, the retail one {ws} px "
                             f"(the window layout is fixed)")
        if w:
            out[rid] = w
    return out


def _merge_raw(per_file: Dict[str, Dict[int, bytes]], owner_of,
               raw_overrides: Optional[Dict[Tuple[str, int], bytes]]) -> None:
    for (file, va), raw in (raw_overrides or {}).items():
        per_file.setdefault(file, {})[va] = raw
        owner_of[(file, va)] = row_id(file, va)


def apply(out_iso: str, retail_iso: str, edits: Dict[str, str],
          log: Callable[[str], None] = print,
          raw_overrides: Optional[Dict[Tuple[str, int], bytes]] = None) -> List[str]:
    """Write every ovl edit into ``out_iso`` (a fresh clone of retail), in place.
    Raises TxBuildError, writing nothing, when any edit is invalid.

    ``raw_overrides`` ({(file, retail va): raw bytes}) are written in the same
    ``Overlay.apply`` as the edits, bypassing the row lock: txbuild uses it for
    the battle font's byte table (``@a46af8``, locked for translators) when a
    new-script build gives the table new letters (txtarget)."""
    from repack import RepackError, fs_allocation, patch_fs_file, relocate_fs_file

    if Path(out_iso).resolve() == Path(retail_iso).resolve():
        raise tc.TxBuildError("out_iso is the retail ISO; refusing to write it")
    st = _load(retail_iso)
    errors, per_file, owner_of = _plan(st, edits)
    _merge_raw(per_file, owner_of, raw_overrides)
    staged = _apply_exes(st, per_file, owner_of, errors)
    if errors:
        lines = [f"{rid}: {m}" for rid, ms in sorted(errors.items()) for m in ms]
        raise tc.TxBuildError("ovl: nothing written:\n  " + "\n  ".join(lines))
    lines: List[str] = []
    try:
        for file, (data, flog) in staged.items():
            if data == st.exes[file].retail:
                continue
            iso = IsoImage(out_iso)
            try:
                fits = len(data) <= fs_allocation(iso, file)
            finally:
                iso.close()
            if fits:
                patch_fs_file(out_iso, file, data, verbose=False)
                flog.append(f"{file}: written in place ({len(data)} bytes)")
            else:
                lba = relocate_fs_file(out_iso, file, data, verbose=False)
                flog.append(f"{file}: grown to {len(data)} bytes, relocated to sector {lba}")
            for x in flog:
                log(x)
            lines += flog
    except RepackError as e:
        raise tc.TxBuildError(f"ovl: write failed: {e}")
    return lines


# -- read back ---------------------------------------------------------------------

def _phdrs(data: bytes) -> List[Tuple[int, int, int]]:
    e_phoff = struct.unpack_from("<I", data, 28)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 42)
    out = []
    for i in range(e_phnum):
        p_type, p_off, p_vaddr, _, p_filesz, _ = struct.unpack_from("<6I", data, e_phoff + i * e_phentsize)
        if p_type == ovltext.PT_LOAD:
            out.append((p_vaddr, p_off, p_filesz))
    return out


def _cstr(data: bytes, phdrs, va: int) -> Optional[bytes]:
    for v, o, n in phdrs:
        if v <= va < v + n:
            off = o + va - v
            end = data.index(b"\0", off)
            return bytes(data[off:end])
    return None


def _ref_target(data: bytes, r: ovltext.Ref) -> int:
    if r.kind == "ptr":
        return struct.unpack_from("<I", data, r.off)[0]
    hi = struct.unpack_from("<I", data, r.lui_off)[0] & 0xFFFF
    lo = struct.unpack_from("<I", data, r.off)[0] & 0xFFFF
    if r.signed:
        return ((hi << 16) + (lo - 0x10000 if lo & 0x8000 else lo)) & 0xFFFFFFFF
    return (hi << 16) | lo


def read_back(iso_path: str, retail_iso: str) -> Dict[str, Tuple[bytes, int, bool]]:
    """Follow every row's retail references in a patched ISO.
    -> id -> (raw bytes now drawn, va now pointed at, all refs agree), checked
    for every executable the row writes."""
    st = _load(retail_iso)
    iso = IsoImage(iso_path)
    try:
        data = {n: bytes(iso.read_file(n)) for n in st.exes}
    finally:
        iso.close()
    ph = {n: _phdrs(d) for n, d in data.items()}
    out: Dict[str, Tuple[bytes, int, bool]] = {}
    for rid, writes in st.target.items():
        res = []
        for file, va in writes:
            s = st.exes[file].ov.strings[va]
            vas = {_ref_target(data[file], r) for r in s.refs}
            nva = min(vas)
            res.append((_cstr(data[file], ph[file], nva), nva, len(vas) == 1))
        raw, nva, agree = res[0]
        agree = agree and all(x[0] == raw and x[2] for x in res)
        out[rid] = (raw, nva, agree)
    return out


# -- selftest ------------------------------------------------------------------------

SAMPLE_FIELD = ["Épée légère « à â ç »", "Bouclier ô û ù ï î œ", "¿Señal? ¡Sí! á í ó ú ü ñ",
                "Être Ça Àlors É È", "Énergie: très élevée, maïs, cœur",
                "Pointe d'aiguille très très longue — ¿Qué pasa aquí, señor?",
                "Récupération complète des PV et des PE de toute l'équipe, même hors combat"]
SAMPLE_BATTLE = ["Épée", "AIGUILLE ÉLECTRIQUE", "Coup ç ô û", "Señal ¿Sí? ¡ñ!",
                 "Œil É ï", "Lanza óú"]
SAMPLE_ASCII = ["Equipe", "Reserve FR", "Menu ES"]


def _pick(rows_, pred, n):
    return [r for r in rows_ if pred(r)][:n]


def selftest(iso_path: str, scratch: Optional[str] = None) -> int:
    ok = True
    t0 = time.time()
    rs = rows(iso_path)
    t_rows = time.time() - t0
    st = _load(iso_path)
    print(f"rows: {len(rs)} in {t_rows:.2f} s")
    cats = Counter(r["cat"] for r in rs)
    for c in sorted(cats):
        sub = [r for r in rs if r["cat"] == c]
        print(f"  {c:30s} {len(sub):5d}  visible {sum(1 for r in sub if not r['hide']):5d}"
              f"  hidden {sum(1 for r in sub if r['hide']):5d}"
              f"  locked {sum(1 for r in sub if r['lock']):5d}")
    vis = [r for r in rs if not r["hide"]]
    print(f"  total visible {len(vis)}, hidden {len(rs) - len(vis)}, "
          f"locked {sum(1 for r in rs if r['lock'])}, with lim {sum(1 for r in rs if r['lim'] is not None)}")
    print(f"  fonts: {dict(Counter(r['font'] for r in rs))}; visible: {dict(Counter(r['font'] for r in vis))}")
    print(f"  hide reasons: {dict(Counter(r['hide'].split(' (')[0] for r in rs if r['hide']).most_common(12))}")
    interior = sum(1 for ex in st.exes.values() for it in ex.items.values() if it.interior)
    clamped = sum(1 for ex in st.exes.values() for it in ex.items.values()
                  if not it.interior and it.safe_slot < it.slot)
    blocked = sum(1 for ex in st.exes.values() for it in ex.items.values() if it.blocked)
    print(f"  slots clamped {clamped}, pointed-into {interior}, cannot move {blocked}")
    ids = [r["id"] for r in rs]
    if len(set(ids)) != len(ids):
        print("FAIL: duplicate ids")
        ok = False
    dups = [r for r in rs if "also in" in r["ctx"]]
    print(f"  OV02 strings also written to {BOOT}: {len(dups)}")
    # round trip of every src through the codec
    bad = 0
    for r in rs:
        raw = st.exes[r["id"].split(":")[1]].items[int(r["id"].split(":")[2], 16)].raw
        try:
            if tc.from_display(r["src"], r["font"], "c0")[0] != raw:
                bad += 1
        except tc.TxEncodeError:
            bad += 1
    print(f"  src that do not encode back to retail bytes: {bad}")
    known = {"ovl:OV01.OVL:a4c4b8": "battle", "ovl:OV01.OVL:a46970": "battle",
             "ovl:OV01.OVL:a46978": "battle"}
    for rid, f in known.items():
        r = st.by_id.get(rid)
        print(f"  {rid}: {r and r['src']!r} font={r and r['font']} cat={r and r['cat']} grp={r and r['grp']}")
        if r is None or r["font"] != f:
            ok = False
            print("FAIL: expected font", f)

    # validate: locked, over the limit, unknown
    lockr = next(r for r in rs if r["lock"])
    limr = next((r for r in rs if r["lim"] is not None and not r["lock"] and "⟦" not in r["src"]), None)
    v = {lockr["id"]: lockr["src"] + "x", "ovl:NOPE:0": "x"}
    if limr:
        v[limr["id"]] = "É" * (limr["lim"] + 5)
    t = time.time()
    errs = validate(v)
    print(f"validate (bad edits) {time.time() - t:.2f} s:")
    for k, m in errs.items():
        print(f"  {k}: {m[0][:110]}")
    if len(errs) != len(v):
        print("FAIL: every bad edit must be reported")
        ok = False
    if limr:
        fit = (limr["src"][:limr["lim"]] if len(limr["src"]) > limr["lim"] else limr["src"] + "é")
        fit = fit if len(tc.from_display(fit, limr["font"], "c0")[0]) <= limr["lim"] else limr["src"]
        e2 = validate({limr["id"]: fit})
        print(f"  {limr['id']} (lim {limr['lim']}, cannot move) {fit!r}: "
              f"{'accepted' if not e2 else e2}")
        ok &= not e2
    bat = next(r for r in rs if r["font"] == "battle" and not r["lock"] and "field font" in r["ctx"])
    w = warnings({bat["id"]: "Épée à"})
    print(f"  warnings {bat['id']} 'Épée à': {w.get(bat['id'])}")
    if not any("two printers" in x for x in w.get(bat["id"], [])):
        print("FAIL: both-printers warning missing")
        ok = False
    # tag font: 0x01 is a bare right-align marker, the first letter is free
    tag01 = [r for r in rs if r["font"] == "ascii" and r["src"].startswith("⟦01⟧")]
    bad01 = [r["src"] for r in tag01 if r["src"][4:5] == "⟦"]
    print(f"  tag labels with the 0x01 marker: {len(tag01)}, first letter still a token: {len(bad01)}")
    ok &= not bad01

    # sample: French + Spanish over field, battle and ascii rows, short and long
    edit = lambda r: not r["hide"] and not r["lock"]
    picks: Dict[str, str] = {}
    fld = _pick(rs, lambda r: edit(r) and r["font"] == "field" and r["lim"] is None
                and "⟦" not in r["src"] and "%" not in r["src"], 10 ** 9)
    by_file: Dict[str, List[dict]] = {}
    for r in fld:
        by_file.setdefault(r["id"].split(":")[1], []).append(r)
    for f, lst in sorted(by_file.items()):
        step = max(1, len(lst) // 6)
        for k, r in enumerate(lst[::step][:6]):
            picks[r["id"]] = SAMPLE_FIELD[(k + len(picks)) % len(SAMPLE_FIELD)]
    for k, r in enumerate(_pick(rs, lambda r: edit(r) and r["font"] == "battle"
                                 and r["lim"] is None and "⟦" not in r["src"], 10 ** 9)[::25][:6]):
        picks[r["id"]] = SAMPLE_BATTLE[k % len(SAMPLE_BATTLE)]
    for k, r in enumerate(_pick(rs, lambda r: edit(r) and r["font"] == "ascii"
                                 and r["lim"] is None and "⟦" not in r["src"], 3)):
        picks[r["id"]] = SAMPLE_ASCII[k % len(SAMPLE_ASCII)]
    for k, r in enumerate(_pick(rs, lambda r: edit(r) and r["font"] == "ascii"
                                 and r["lim"] is None and r["src"].startswith("⟦01⟧"), 2)):
        picks[r["id"]] = ["⟦01⟧Argent", "⟦01⟧Équipé"][k]
    # the "Survival Wear" case: a text longer than the padding before shared data
    sw = st.by_id.get("ovl:OV01.OVL:a4a390")
    if sw and edit(sw):
        picks[sw["id"]] = "Tenue de survie très résistante"
    dup_row = next((r for r in dups if edit(r) and r["lim"] is None and "⟦" not in r["src"]), None)
    if dup_row:
        picks[dup_row["id"]] = "Installation sur le disque dur ¿Sí?"
    errs = validate(picks)
    if errs:
        print("FAIL: sample does not validate:", errs)
        return 1
    print(f"sample: {len(picks)} rows ({dict(Counter(st.by_id[i]['font'] for i in picks))})")

    clone = Path(scratch or tempfile.gettempdir()) / "txfam_ovl_selftest.iso"
    if clone.exists():
        clone.unlink()
    try:
        r = subprocess.run(["cp", "-c", iso_path, str(clone)], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"FAIL: cannot clone with cp -c (APFS needed): {r.stderr.strip()}")
            return 1
        retail = {n: ex.retail for n, ex in st.exes.items()}

        def exe_bytes():
            iso = IsoImage(str(clone))
            try:
                return {n: bytes(iso.read_file(n)) for n in st.exes}
            finally:
                iso.close()

        # identity: every row set to its own source
        t = time.time()
        lines = apply(str(clone), iso_path, {r["id"]: r["src"] for r in rs if not r["lock"]},
                      log=lambda s: None)
        same = exe_bytes() == retail
        print(f"identity apply ({len(rs)} rows): {len(lines)} log lines, executables "
              f"{'byte-identical' if same else 'CHANGED'} ({time.time() - t:.2f} s)")
        ok &= same and not lines

        t = time.time()
        lines = apply(str(clone), iso_path, picks, log=lambda s: None)
        t_apply = time.time() - t
        moved = sum(1 for x in lines if "moved to" in x)
        print(f"sample apply: {t_apply:.2f} s, {moved} strings moved; " +
              "; ".join(x for x in lines if ": written" in x or "relocated" in x))
        t = time.time()
        back = read_back(str(clone), iso_path)
        print(f"read back: {time.time() - t:.2f} s")
        bad_edit = bad_keep = refword = 0
        patched: Dict[str, set] = {}
        for rid in picks:
            for file, va in st.target[rid]:
                for r in st.exes[file].ov.strings[va].refs:
                    for o in ((r.off, r.lui_off) if r.kind == "hilo" else (r.off,)):
                        patched.setdefault(file, set()).update(range(o, o + 4))
        for rid, (raw, nva, agree) in back.items():
            row = st.by_id[rid]
            file, va = rid.split(":")[1], int(rid.split(":")[2], 16)
            if rid in picks:
                got = tc.to_display(raw or b"", "c0", font=row["font"])
                want = picks[rid]
                if row["font"] == "ascii":
                    want = "".join(tc.fold_ascii(c) or c for c in want)
                if got != want or not agree:
                    bad_edit += 1
                    print(f"  FAIL edited {rid}: {got!r} != {want!r} (refs agree: {agree})")
            else:
                it = st.exes[file].items[va]
                off = st.exes[file].ov.strings[va].off
                if raw != it.raw and any(o in patched.get(file, ()) for o in range(off, off + len(it.raw) + 1)):
                    refword += 1                 # its bytes are a reference word we repointed
                    continue
                if raw != it.raw or nva != va or not agree:
                    bad_keep += 1
                    if bad_keep <= 5:
                        print(f"  FAIL untouched {rid} changed: {raw!r} @ {nva:#x}")
        print(f"edited rows decoded back: {len(picks) - bad_edit}/{len(picks)}; "
              f"untouched rows changed: {bad_keep} (plus {refword} false-positive 'strings' that are "
              f"reference words the move repointed)")
        ok &= bad_edit == 0 and bad_keep == 0
        for rid in list(picks)[:4] + ([dup_row["id"]] if dup_row else []):
            print(f"  {rid} [{st.by_id[rid]['font']}] {st.by_id[rid]['src']!r} -> {picks[rid]!r}")
        # the clone still parses with ovltext
        iso = IsoImage(str(clone))
        try:
            for n in st.exes:
                ovltext.Overlay(n, bytes(iso.read_file(n)))
        finally:
            iso.close()
        print("clone executables re-parse with ovltext: OK")
    finally:
        if clone.exists():
            clone.unlink()
    print(f"total {time.time() - t0:.1f} s")
    print("selftest OK" if ok else "selftest FAILED")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("selftest")
    p.add_argument("--iso", required=True)
    p.add_argument("--scratch", help="folder for the APFS clone (same volume as the ISO); "
                   "default: the system temp folder")
    p = sub.add_parser("dump")
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest(a.iso, a.scratch)
    rs = rows(a.iso)
    Path(a.out).write_text(json.dumps(rs, ensure_ascii=False, indent=0), encoding="utf-8")
    print(f"{len(rs)} rows -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
