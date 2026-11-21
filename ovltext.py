"""ovltext.py — the strings inside the code overlays (OV01.OVL …) and the boot ELF.

Battle and menu words — tech names ("NEEDLE", "BLASTER"), "End", "Menu",
item and skill descriptions, the field menu's "Skills / Ether / Items" —
are C string literals in the game's code, not in any text object. The
code lives in five small ELF overlays at the root of the disc, which the
engine loads with ``sceSifLoadElf`` into one fixed region (USA disc:
OV01 at 0xA00000-0xA5B8F8, the others inside that span), plus the boot
executable itself. None of it is reachable through ``text-export`` or
``evt-export``; this module is the third tree.

Export writes one text file per executable::

    # ovltext 1  file=OV01.OVL  load=0xa00000  size=0x5b8f8  grow=yes
    @a4c4b8<TAB>NEEDLE

Every string listed is one the code actually points at (a 32-bit pointer
word in data, or a ``lui``/``addiu`` pair in code, the MIPS way to form
an address); unreferenced byte runs are left out. The text part uses the
same escapes as the other trees (``\\n``, ``⟦XX⟧`` byte markers), so a
tree encoded by ``lang encode`` drops in unchanged.

Import writes each changed string back in place when it fits its slot
(up to the next non-zero byte). When it does not, and the file is an
overlay with no bss (its load segment's file size equals its memory
size), the string is appended to the end of the load segment and every
reference is repointed: pointer words are rewritten, ``addiu`` low
halves are rewritten, and the ``lui`` high half is rewritten unless that
``lui`` also serves another address — then the string is reported as
not relocatable and nothing is written. The grown overlay is written
back to the ISO in place if the next file leaves room, or appended to
the image and its directory record repointed (``grow``).

Memory past the overlay region is unused on the USA disc (the boot ELF's
bss ends at 0x99D910; 0xA5B8F8-0xA80000 read as zero on the running
game), which is what makes the segment-append safe.

The boot ELF has a bss after its data, so nothing can be appended to its
data segment: the bytes would land on variables the startup code zeroes.
Its long strings go to a load segment of their own instead (``grow``
again), placed in memory the program never touches: above the end of
the C heap (``_end`` + ``_heap_size``, both read from the ELF's symbols:
0x99D910 + 0x10000 on the USA disc, rounded up to 0x9B0000, plus 32K =
0x9B8000) and below the lowest address any overlay loads at (0x9FF000). The segment gets a
fifth program header in the zero padding after the four the file has;
its bytes sit in the rest of that padding (0xE0-0x1000, the file keeps
its size) or, when they outgrow it, at the end of the file. A boot ELF
without those two symbols, with an unbounded heap, or with a stack
inside that span is left in place only. Strings addressed off ``$gp``
(``addiu rX, gp, n``) cannot be moved out of gp's reach and are
reported.

Moving overlay strings instead of squeezing them is how poketony's
XS1KOR (the Korean localisation of the Japanese disc) handled this
tier, using the overlay's ELF symbols to find the referencing code; the
scanner and the segment-append placement here are this kit's own.
"""
from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from evttext import _escape, _unescape
from iso9660 import IsoImage
from textpack import decode_text, encode_text

PT_LOAD = 1
STT_FUNC = 2
ALIGN = 4
ZONE_ALIGN = 0x10000      # the boot ELF's string segment: next 64K boundary after the heap,
ZONE_PHASE = 0x8000       # plus 32K, so the low halves of its addresses start at 0x8000
PF_RW = 6


@dataclass
class Segment:
    vaddr: int
    off: int
    filesz: int
    memsz: int
    ph_off: int           # where the program header sits in the file


@dataclass
class Ref:
    kind: str             # "ptr" | "hilo"
    off: int              # file offset of the word (ptr) or of the addiu/ori/load (hilo)
    lui_off: int = -1     # file offset of the lui (hilo)
    signed: bool = True   # addiu / memory ops sign-extend the low half; ori does not


@dataclass
class Str:
    va: int
    off: int
    raw: bytes
    slot: int             # bytes from the string start to the next non-zero byte
    refs: List[Ref] = field(default_factory=list)

    @property
    def text(self) -> str:
        return decode_text(self.raw)


class Overlay:
    """One ELF file (overlay or boot executable) and its referenced strings."""

    def __init__(self, name: str, data: bytes):
        self.name = name
        self.data = bytearray(data)
        if data[:4] != b"\x7fELF":
            raise ValueError(f"{name}: not an ELF")
        (e_phoff, e_shoff) = struct.unpack_from("<II", data, 28)
        e_phentsize, e_phnum, e_shentsize, e_shnum, e_shstrndx = struct.unpack_from("<HHHHH", data, 42)
        self.e_phoff = e_phoff
        self.e_phentsize = e_phentsize
        self.e_phnum = e_phnum
        self.e_shoff = e_shoff
        self.e_shnum = e_shnum
        self.segments: List[Segment] = []
        self._contents: List[Tuple[int, int]] = []   # (file offset, load address or -1) of everything in the file
        if e_shoff:
            self._contents.append((e_shoff, -1))
        for i in range(e_phnum):
            p = e_phoff + i * e_phentsize
            p_type, p_off, p_vaddr, _, p_filesz, p_memsz = struct.unpack_from("<6I", data, p)
            if p_type == PT_LOAD:
                self.segments.append(Segment(p_vaddr, p_off, p_filesz, p_memsz, p))
            if p_filesz:
                self._contents.append((p_off, p_vaddr if p_type == PT_LOAD else -1))
        # (base, limit) of memory a boot ELF's relocated strings may use; see free_zone
        self.zone: Optional[Tuple[int, int]] = None
        self.sections: List[Tuple[int, int, int, int, int]] = []  # (sh_off_in_file, type, addr, off, size)
        for i in range(e_shnum):
            p = e_shoff + i * e_shentsize
            _, sh_type, _, sh_addr, sh_off, sh_size = struct.unpack_from("<6I", data, p)
            self.sections.append((p, sh_type, sh_addr, sh_off, sh_size))
            if sh_type not in (0, 8) and sh_size:
                self._contents.append((sh_off, -1))
        self.funcs = self._functions()
        self._code = bytearray(len(data))            # 1 = inside a sized FUNC symbol
        for f_off, size in self.funcs:
            self._code[f_off:f_off + size] = b"\x01" * size
        self.strings: Dict[int, Str] = {}
        self._scan()

    # -- layout -------------------------------------------------------------

    def va_to_off(self, va: int) -> Optional[int]:
        for s in self.segments:
            if s.vaddr <= va < s.vaddr + s.filesz:
                return s.off + (va - s.vaddr)
        return None

    def off_to_va(self, off: int) -> Optional[int]:
        for s in self.segments:
            if s.off <= off < s.off + s.filesz:
                return s.vaddr + (off - s.off)
        return None

    @property
    def _plain(self) -> bool:
        """One load segment and no bss: strings can be appended to it."""
        return len(self.segments) == 1 and self.segments[0].filesz == self.segments[0].memsz

    @property
    def growable(self) -> bool:
        return self._plain or self.zone is not None

    def symbol(self, name: str) -> Optional[int]:
        """Value of a symbol-table entry, by its name in the file."""
        symtab = next((s for s in self.sections if s[1] == 2), None)
        if symtab is None:
            return None
        link = struct.unpack_from("<I", self.data, symtab[0] + 24)[0]
        if link >= len(self.sections):
            return None
        names = self.sections[link][3]
        want = name.encode() + b"\0"
        for i in range(symtab[4] // 16):
            st_name, value = struct.unpack_from("<II", self.data, symtab[3] + i * 16)
            if self.data[names + st_name:names + st_name + len(want)] == want:
                return value
        return None

    def load_ranges(self) -> List[Tuple[int, int]]:
        return [(s.vaddr, s.vaddr + s.memsz) for s in self.segments]

    def free_zone(self, others: List[Tuple[int, int]]) -> Optional[Tuple[int, int]]:
        """(base, limit) of the memory a segment of relocated strings may
        occupy in an executable with a bss, or None when that cannot be
        established from the file. ``others`` are the (start, end) load
        ranges of every other executable the game loads (the overlays).

        base is the first 64K boundary after the C heap, which the startup
        code hands to the kernel as ``SetupHeap(_end, _heap_size)``, plus
        32K; limit is the lowest load address above it. Both ends are
        needed: without a bounded heap or without something above, nothing
        says the span is unused.

        The 32K is for the scanner's sake, on a later run over the grown
        file: the first 32K of strings then have ``addiu`` low halves of
        -0x8000 and up, which no structure-field access resembles. A
        string at xxxx0000 + a small number would collect false
        references from every ``lw rX, 8(rY)`` whose rY last held that
        high half."""
        if self._plain:
            return None
        end, heap = self.symbol("_end"), self.symbol("_heap_size")
        if end is None or heap is None or heap >= 0x80000000:
            return None
        bss_end = max((s.vaddr + s.memsz for s in self.segments if s.memsz > s.filesz), default=None)
        if bss_end != end:
            return None
        base = ((end + heap + ZONE_ALIGN - 1) & ~(ZONE_ALIGN - 1)) + ZONE_PHASE
        mine = [(s.vaddr, s.vaddr + s.memsz) for s in self.segments if s.vaddr != base]
        above = [lo for lo, hi in mine + list(others) if lo >= base]
        if not above or any(lo < base < hi for lo, hi in mine + list(others)):
            return None
        limit = min(above)
        stack, stack_size = self.symbol("_stack"), self.symbol("_stack_size") or 0
        if stack is not None and stack < 0x80000000 and stack < limit and stack + stack_size > base:
            return None
        if self._zone_segment(base) is None:
            # room for one more program header, in bytes nothing uses
            slot = self.e_phoff + self.e_phnum * self.e_phentsize
            if (self.e_phentsize != 32 or slot + 32 > self._first_content(base)
                    or any(self.data[slot:slot + 32])):
                return None
        return (base, limit) if limit - base >= ALIGN else None

    def _first_content(self, base: int) -> int:
        """Lowest file offset anything but the string segment at ``base`` occupies."""
        return min((off for off, va in self._contents if va != base), default=len(self.data))

    def _zone_segment(self, base: int) -> Optional[Segment]:
        return next((s for s in self.segments if s.vaddr == base), None)

    def _gp_targets(self) -> Dict[int, int]:
        """address -> file offset of an ``addiu rX, gp, n`` that forms it."""
        gp = self.symbol("_gp")
        out: Dict[int, int] = {}
        if gp is None:
            return out
        for f_off, size in self.funcs:
            for off in range(f_off, f_off + size - 3, 4):
                w = struct.unpack_from("<I", self.data, off)[0]
                if w >> 26 == 0x09 and (w >> 21) & 31 == 28:
                    imm = w & 0xFFFF
                    out.setdefault(gp + (imm - 0x10000 if imm & 0x8000 else imm), off)
        return out

    def _functions(self) -> List[Tuple[int, int]]:
        """(file offset, size) of every FUNC symbol with a size; code is only
        looked for there, pointer words only outside."""
        symtab = next((s for s in self.sections if s[1] == 2), None)
        out = []
        if symtab is None:
            return out
        for i in range(symtab[4] // 16):
            _, value, size, info, _, _ = struct.unpack_from("<IIIBBH", self.data, symtab[3] + i * 16)
            if info & 0xF == STT_FUNC and size:
                off = self.va_to_off(value)
                if off is not None:
                    out.append((off, size))
        out.sort()
        return out

    def _in_code(self, off: int) -> bool:
        return bool(self._code[off])

    # -- strings ------------------------------------------------------------

    @staticmethod
    def _textlike(raw: bytes) -> bool:
        if len(raw) < 2:
            return False
        letters = 0
        i = 0
        while i < len(raw):
            b = raw[i]
            if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D):
                letters += b >= 0x30
                i += 1
            elif 0x01 <= b < 0x20:
                i += 1                              # printer control codes
            elif 0xA1 <= b <= 0xFE and i + 1 < len(raw) and 0xA1 <= raw[i + 1] <= 0xFE:
                letters += 1
                i += 2
            elif b == 0x8E and i + 1 < len(raw):
                letters += 1
                i += 2
            else:
                return False
        return letters >= 1

    def _candidates(self) -> Dict[int, Str]:
        out: Dict[int, Str] = {}
        d = self.data
        for seg in self.segments:
            lo, hi = seg.off, seg.off + seg.filesz
            i = lo
            while i < hi:
                if d[i] == 0 or (i > lo and d[i - 1] != 0) or self._in_code(i):
                    i += 1
                    continue
                j = d.find(b"\0", i, hi)
                if j < 0:
                    break
                raw = bytes(d[i:j])
                if self._textlike(raw):
                    k = j
                    while k < hi and d[k] == 0:
                        k += 1
                    out[self.off_to_va(i)] = Str(self.off_to_va(i), i, raw, k - i)
                i = j + 1
        return out

    def _scan(self) -> None:
        cands = self._candidates()
        d = self.data
        # pointer words outside code
        for seg in self.segments:
            for off in range(seg.off, seg.off + seg.filesz - 3, 4):
                if self._in_code(off):
                    continue
                v = struct.unpack_from("<I", d, off)[0]
                s = cands.get(v)
                if s is not None:
                    s.refs.append(Ref("ptr", off))
        # lui / addiu pairs inside functions
        self.lui_uses: Dict[int, List[Tuple[int, int]]] = {}   # lui off -> [(use off, addr)]
        for f_off, size in self.funcs:
            pending: Dict[int, Tuple[int, int]] = {}            # reg -> (hi16, lui off), linear
            last_lui: Dict[int, Tuple[int, int]] = {}           # reg -> same, never cleared:
            # the fallback for an addiu reached by a branch from a block whose
            # lui came earlier in the function (switch arms share one lui)
            for off in range(f_off, f_off + size - 3, 4):
                w = struct.unpack_from("<I", d, off)[0]
                op, rs, rt = w >> 26, (w >> 21) & 31, (w >> 16) & 31
                imm = w & 0xFFFF
                if op == 0x0F:                                   # lui
                    pending[rt] = last_lui[rt] = (imm, off)
                    continue
                if op in (0x02, 0x03):                           # j / jal: temps die
                    for r in list(pending):
                        if r in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31):
                            del pending[r]
                    continue
                if op == 0:                                      # R-type writes rd
                    rd = (w >> 11) & 31
                    pending.pop(rd, None)
                    if w & 0x3F == 0x09:                         # jalr
                        for r in list(pending):
                            if r in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 24, 25, 31):
                                del pending[r]
                    continue
                uses_base = op == 0x09 or op == 0x0D or op >= 0x20   # addiu, ori, loads/stores
                if uses_base and (rs in pending or rs in last_lui):
                    hi, lui_off = pending.get(rs) or last_lui[rs]
                    if op == 0x0D:
                        addr, signed = (hi << 16) | imm, False
                    else:
                        addr, signed = (hi << 16) + (imm - 0x10000 if imm & 0x8000 else imm), True
                    s = cands.get(addr)
                    if rs in pending or s is not None:
                        self.lui_uses.setdefault(lui_off, []).append((off, addr))
                    if s is not None:
                        s.refs.append(Ref("hilo", off, lui_off, signed))
                if op in (0x08, 0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x0E) or (0x20 <= op <= 0x27) or op in (0x30, 0x31, 0x33, 0x35, 0x37, 0x1E, 0x1A, 0x1B):
                    pending.pop(rt, None)                        # rt written
        self.strings = {va: s for va, s in cands.items() if s.refs}

    # -- text files ---------------------------------------------------------

    def to_text(self) -> str:
        seg = self.segments[0]
        lines = [f"# ovltext 1  file={self.name}  load={seg.vaddr:#x}  size={seg.filesz:#x}  "
                 f"grow={'yes' if self.growable else 'no'}",
                 "# every line is one string the code points at: @address<TAB>text; "
                 "translate the text, keep the key"]
        for va in sorted(self.strings):
            lines.append(f"@{va:x}\t{_escape(self.strings[va].text)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def parse_text(text: str) -> Tuple[str, Dict[int, str]]:
        head = text.split("\n", 1)[0]
        if not head.startswith("# ovltext"):
            raise ValueError("not an ovltext file")
        name = next(tok.split("=", 1)[1] for tok in head.split() if tok.startswith("file="))
        out: Dict[int, str] = {}
        for line in text.split("\n"):
            if line.startswith("@") and "\t" in line:
                key, _, body = line.partition("\t")
                out[int(key[1:], 16)] = _unescape(body)
        return name, out

    # -- rewriting ----------------------------------------------------------

    def apply(self, new_text: Dict[int, str], grow: bool, skip_unfit: bool = False) -> List[str]:
        """Write the changed strings. Returns a log; raises ValueError with
        every problem listed when something cannot be done. With
        ``skip_unfit`` the strings that cannot be written (slot too small,
        shared lui, no room) are left as they were and listed in the log as
        ``skipped @addr: why`` while the rest is written."""
        log, problems = [], []
        seg = self.segments[0]
        appended = bytearray()
        if self._plain:
            start_va, limit = seg.vaddr + seg.filesz, None
        elif self.zone is not None:
            start_va, limit = self.zone
            old = self._zone_segment(start_va)
            if old is not None:
                start_va += (old.filesz + ALIGN - 1) & ~(ALIGN - 1)
        gp_targets: Optional[Dict[int, int]] = None
        patches: List[Tuple[int, bytes]] = []          # (file off, bytes) applied at the end
        for va, text in new_text.items():
            s = self.strings.get(va)
            if s is None:
                problems.append(f"@{va:x}: no such string in {self.name}")
                continue
            if text == s.text:
                continue
            try:
                raw = encode_text(text)
            except UnicodeEncodeError as e:
                problems.append(f"@{va:x}: {e.object[e.start:e.end]!r} is not encodable; "
                                f"run lang encode first")
                continue
            if len(raw) + 1 <= s.slot:
                patches.append((s.off, raw + bytes(s.slot - len(raw))))
                log.append(f"@{va:x}: {len(s.raw)} -> {len(raw)} bytes in place")
                continue
            if not grow or not self.growable:
                why = ("not growable (has bss, and no free memory could be "
                       "established for it)") if not self.growable else "pass --grow"
                problems.append(f"@{va:x}: {len(raw) + 1} bytes need a {s.slot}-byte slot; {why}")
                continue
            if not self._plain:
                if gp_targets is None:
                    gp_targets = self._gp_targets()
                if va in gp_targets:
                    problems.append(f"@{va:x}: also addressed off gp at "
                                    f"{self.off_to_va(gp_targets[va]):#x}, which cannot reach "
                                    f"a moved string; shorten the text to {s.slot - 1} bytes")
                    continue
            # relocate: append to the load segment (or, with a bss, to the
            # string segment in the free zone), repoint every reference
            while len(appended) % ALIGN:
                appended.append(0)
            new_va = start_va + len(appended)
            if limit is not None and new_va + len(raw) + 1 > limit:
                problems.append(f"@{va:x}: no room left below {limit:#x} for {len(raw) + 1} bytes")
                continue
            appended += raw + b"\0"
            lo = new_va & 0xFFFF
            hi_signed = (new_va >> 16) + (1 if lo & 0x8000 else 0)
            hi_plain = new_va >> 16
            blocked = False
            ref_patches: List[Tuple[int, bytes]] = []
            for r in s.refs:
                if r.kind == "ptr":
                    ref_patches.append((r.off, struct.pack("<I", new_va)))
                    continue
                hi = hi_signed if r.signed else hi_plain
                lui = struct.unpack_from("<I", self.data, r.lui_off)[0]
                if (lui & 0xFFFF) != hi:
                    others = {a for _, a in self.lui_uses.get(r.lui_off, []) if a != va}
                    if others:
                        blocked = True
                        problems.append(f"@{va:x}: its lui at {self.off_to_va(r.lui_off):#x} also "
                                        f"forms {', '.join(f'{a:#x}' for a in sorted(others))}; "
                                        f"shorten the text to {s.slot - 1} bytes")
                        break
                    ref_patches.append((r.lui_off, struct.pack("<I", (lui & 0xFFFF0000) | hi)))
                use = struct.unpack_from("<I", self.data, r.off)[0]
                ref_patches.append((r.off, struct.pack("<I", (use & 0xFFFF0000) | lo)))
            if blocked:
                del appended[len(appended) - len(raw) - 1:]
                continue
            patches += ref_patches
            log.append(f"@{va:x}: {len(s.raw)} -> {len(raw)} bytes, moved to {new_va:#x} "
                       f"({len(s.refs)} reference{'s' if len(s.refs) != 1 else ''})")
        if problems and skip_unfit:
            log.extend("skipped " + q for q in problems)
        elif problems:
            raise ValueError("\n".join(problems))
        for off, b in patches:
            self.data[off:off + len(b)] = b
        if appended and self._plain:
            self._grow_segment(bytes(appended))
        elif appended:
            self._grow_zone(bytes(appended))
        return log

    def _grow_zone(self, extra: bytes) -> None:
        """Add ``extra`` to the string segment at the zone base, creating the
        segment (one more program header) the first time. The bytes go in
        the zero padding between the program headers and the first thing in
        the file when they fit there, else at the end of the file; nothing
        else in the file moves either way."""
        base, _ = self.zone
        old = self._zone_segment(base)
        if old is not None:
            blob = bytes(self.data[old.off:old.off + old.filesz])
            blob += bytes(-len(blob) % ALIGN) + extra
            if old.off + old.filesz == len(self.data):
                del self.data[old.off:]
            else:
                self.data[old.off:old.off + old.filesz] = bytes(old.filesz)
            ph = old.ph_off
        else:
            blob = extra
            ph = self.e_phoff + self.e_phnum * self.e_phentsize
            self.e_phnum += 1
            struct.pack_into("<H", self.data, 44, self.e_phnum)
        blob += bytes(-len(blob) % ALIGN)
        table_end = self.e_phoff + self.e_phnum * self.e_phentsize
        off = (table_end + 15) & ~15
        floor = self._first_content(base)
        if off + len(blob) > floor or any(self.data[off:floor]):
            self.data += bytes(-len(self.data) % 16)
            off = len(self.data)
            self.data += blob
        else:
            self.data[off:off + len(blob)] = blob
        struct.pack_into("<8I", self.data, ph, PT_LOAD, off, base, base,
                         len(blob), len(blob), PF_RW, 16)
        seg = Segment(base, off, len(blob), len(blob), ph)
        if old is not None:
            self.segments[self.segments.index(old)] = seg
        else:
            self.segments.append(seg)

    def _grow_segment(self, extra: bytes) -> None:
        seg = self.segments[0]
        end = seg.off + seg.filesz
        self.data[end:end] = extra
        n = len(extra)
        seg.filesz += n
        seg.memsz += n
        struct.pack_into("<II", self.data, seg.ph_off + 16, seg.filesz, seg.memsz)
        # everything after the segment moves down: section table and sections
        if self.e_shoff >= end:
            self.e_shoff += n
            struct.pack_into("<I", self.data, 32, self.e_shoff)
        new_sections = []
        for i, (p, sh_type, addr, off, size) in enumerate(self.sections):
            p2 = p + n if p >= end else p
            if addr == seg.vaddr and size == seg.filesz - n:
                size += n
                struct.pack_into("<I", self.data, p2 + 20, size)
            if off >= end and sh_type != 8:
                off += n
                struct.pack_into("<I", self.data, p2 + 16, off)
            new_sections.append((p2, sh_type, addr, off, size))
        self.sections = new_sections


# -- disc-level ------------------------------------------------------------

def executables(iso: IsoImage) -> List[str]:
    boot = iso.boot_elf_name()
    names = sorted(n for n, f in iso.files.items()
                   if not f.is_dir and "\\" not in n and n.upper().endswith(".OVL"))
    return ([boot] if boot else []) + names


def _segments_of(data: bytes) -> List[Tuple[int, int]]:
    """(start, end) in memory of an ELF's load segments, headers only."""
    if data[:4] != b"\x7fELF":
        return []
    e_phoff = struct.unpack_from("<I", data, 28)[0]
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 42)
    out = []
    for i in range(e_phnum):
        p_type, _, p_vaddr, _, _, p_memsz = struct.unpack_from("<6I", data, e_phoff + i * e_phentsize)
        if p_type == PT_LOAD:
            out.append((p_vaddr, p_vaddr + p_memsz))
    return out


def set_zone(ov: Overlay, iso: IsoImage) -> None:
    """Give an executable with a bss (the boot ELF) its free zone, bounded by
    where the other executables on ``iso`` load."""
    if ov._plain:
        return
    others: List[Tuple[int, int]] = []
    for name in executables(iso):
        if name != ov.name:
            others += _segments_of(bytes(iso.read_file(name)))
    ov.zone = ov.free_zone(others)


def export_strings(iso_path: str, out_dir: str) -> int:
    iso = IsoImage(iso_path)
    out = Path(out_dir) / "fs"
    out.mkdir(parents=True, exist_ok=True)
    try:
        total = 0
        for name in executables(iso):
            ov = Overlay(name, bytes(iso.read_file(name)))
            set_zone(ov, iso)
            with open(out / f"{name}.txt", "w", encoding="utf-8", newline="") as fh:
                fh.write(ov.to_text())
            print(f"{name}: {len(ov.strings)} strings"
                  f"{'' if ov.growable else ' (in place only)'}")
            total += len(ov.strings)
        print(f"wrote {total} strings -> {out}")
    finally:
        iso.close()
    return 0


def import_strings(iso_path: str, text_dir: str, out_iso: str, grow: bool = False,
                   retail_iso: Optional[str] = None, skip_unfit: bool = False) -> int:
    """``retail_iso``: take the executables from this (unmodified) disc
    instead of ``iso_path``, so a tree keyed by retail addresses can be
    imported again into a disc whose overlays were already rewritten."""
    import shutil

    from repack import RepackError, fs_allocation, patch_fs_file, relocate_fs_file

    files = sorted(p for p in Path(text_dir).rglob("*.txt")
                   if p.read_text(encoding="utf-8-sig").startswith("# ovltext"))
    if not files:
        print(f"no ovltext files under {text_dir}", file=sys.stderr)
        return 2
    iso = IsoImage(iso_path)
    source = IsoImage(retail_iso) if retail_iso else iso
    staged: List[Tuple[str, bytes, bool]] = []
    failed = False
    try:
        for p in files:
            name, new_text = Overlay.parse_text(p.read_text(encoding="utf-8-sig"))
            if name not in iso.files or name not in source.files:
                print(f"{p}: {name} is not on this disc", file=sys.stderr)
                failed = True
                continue
            ov = Overlay(name, bytes(source.read_file(name)))
            set_zone(ov, source)
            try:
                log = ov.apply(new_text, grow, skip_unfit)
            except ValueError as e:
                print(f"{name}:\n  " + str(e).replace("\n", "\n  "), file=sys.stderr)
                failed = True
                continue
            if not log:
                continue
            for line in log:
                print(f"{name} {line}", file=sys.stderr if line.startswith("skipped") else sys.stdout)
            fits = len(ov.data) <= fs_allocation(iso, name)
            if not fits and not grow:
                print(f"{name}: grown to {len(ov.data)} bytes, more than the "
                      f"{fs_allocation(iso, name)} before the next file; pass --grow",
                      file=sys.stderr)
                failed = True
                continue
            staged.append((name, bytes(ov.data), fits))
    finally:
        iso.close()
        if source is not iso:
            source.close()
    if failed:
        print("nothing written", file=sys.stderr)
        return 1
    if not staged:
        print("no changes")
        return 0
    dst = Path(out_iso)
    if dst.resolve() != Path(iso_path).resolve():
        print(f"copying {iso_path} -> {dst} ...", file=sys.stderr)
        shutil.copyfile(iso_path, dst)
    try:
        for name, data, fits in staged:
            if fits:
                patch_fs_file(dst, name, data)
            else:
                relocate_fs_file(dst, name, data)
    except RepackError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("export")
    p.add_argument("--iso", required=True)
    p.add_argument("--out", required=True)
    p = sub.add_parser("import")
    p.add_argument("--iso", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--grow", action="store_true")
    p.add_argument("--retail", help="unmodified ISO to take the executables from")
    a = ap.parse_args(argv)
    if a.cmd == "export":
        return export_strings(a.iso, a.out)
    return import_strings(a.iso, a.text, a.out, a.grow, a.retail)


if __name__ == "__main__":
    sys.exit(main())
