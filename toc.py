"""toc.py — Parse Xenosaga Episode I binary TOC files (XENOSAGA.00 / XENOSAGA.10).

Unlike Episode III's plain-text ``Lba*.txt`` tables, Episode I ships binary
tables. Grammar (reverse-engineered 2026-06-10, validated against both TOCs —
8,573 + 349 entries parse to the byte with zero desyncs, and every recovered
path matches the engine's own ``data\\...`` string literals in SLUS_204.69):

::

    toc      = [u8 data_base_sector] entry* [0x00] filler
    entry    = file | cfile | dir
    file     = [b]        [name: b-1 bytes]        [u24le sector] [u32le size]
    cfile    = [b | 0x40] [name: (b&0x3f)-1 bytes] [u24le sector] [u32le csize] [u24le usize]
    dir      = [b | 0x80] [u8 pop_count]           [name: (b&0x7f)-2 bytes]
    filler   = "MONOLITHSOFT Xenosaga Episode.1\\0" repeated, phase-locked to
               (absolute file offset % 32)

* The leading byte equals the TOC's own size in sectors — i.e. entry sector
  numbers are relative to the **start of the TOC file itself**, with the data
  continuing seamlessly into the sibling bigfiles (see ``chains.py``).
* ``dir`` pushes a directory onto the path stack after popping ``pop_count``
  levels. A bare ``0x00`` byte pops one level (in practice it appears once,
  closing the final directory before the filler).
* ``cfile`` entries are ARX-compressed: the payload on disc begins with an
  ``ARX\\0`` header (u32le usize, u32le csize, u32le pad) whose fields match
  the TOC's csize/usize.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

SECTOR = 2048
FILLER = b"MONOLITHSOFT Xenosaga Episode.1\x00"


@dataclass(frozen=True)
class TocEntry:
    source: str         # TOC filename the entry came from
    path: str           # in-game path, backslash-separated (no "data\" prefix)
    sector: int         # sector offset relative to chain start (TOC = sector 0)
    size: int           # stored byte length (compressed length for cfiles)
    usize: int | None   # uncompressed length for cfiles, else None
    compressed: bool

    @property
    def byte_offset(self) -> int:
        return self.sector * SECTOR

    @property
    def ext(self) -> str:
        name = self.path.rsplit("\\", 1)[-1]
        return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class TocError(ValueError):
    pass


def _is_filler(data: bytes, pos: int) -> bool:
    rest = data[pos:]
    if not rest:
        return True
    phase = pos % len(FILLER)
    want = (FILLER * (len(rest) // len(FILLER) + 2))[phase : phase + len(rest)]
    return rest == want


def parse_toc(data: bytes, source: str) -> List[TocEntry]:
    """Parse a full TOC file. Raises TocError on any grammar violation."""
    if not data:
        raise TocError(f"{source}: empty")
    base = data[0]
    if base != len(data) // SECTOR:
        raise TocError(
            f"{source}: header byte {base} != file sector count {len(data) // SECTOR}"
        )
    pos = 1
    stack: List[str] = []
    entries: List[TocEntry] = []
    while pos < len(data):
        if _is_filler(data, pos):
            break
        b = data[pos]
        if b == 0:
            if stack:
                stack.pop()
            pos += 1
            continue
        if b & 0x80:
            ln = b & 0x7F
            pop = data[pos + 1]
            name = data[pos + 2 : pos + ln]
            if not name or not all(32 <= c < 127 for c in name):
                raise TocError(f"{source}: bad dir name at 0x{pos:06x}")
            for _ in range(pop):
                if stack:
                    stack.pop()
            stack.append(name.decode("ascii"))
            pos += ln
            continue
        comp = bool(b & 0x40)
        nlen = (b & 0x3F) - 1
        name = data[pos + 1 : pos + 1 + nlen]
        if not name or not all(32 <= c < 127 for c in name):
            raise TocError(f"{source}: bad file name at 0x{pos:06x}")
        off = pos + 1 + nlen
        sector = int.from_bytes(data[off : off + 3], "little")
        size = int.from_bytes(data[off + 3 : off + 7], "little")
        usize = int.from_bytes(data[off + 7 : off + 10], "little") if comp else None
        entries.append(
            TocEntry(
                source=source,
                path="\\".join(stack + [name.decode("ascii")]),
                sector=sector,
                size=size,
                usize=usize,
                compressed=comp,
            )
        )
        pos = off + (10 if comp else 7)
    return entries
