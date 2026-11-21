#!/usr/bin/env python3
"""region.py — which Episode I build a disc image carries.

    python cli.py region --iso GAME.iso

The build is read from the disc itself: the boot executable named in
SYSTEM.CNF (``txsource.identify_disc``, the same test the translation editor
uses), its sha256, whether it still has its symbol table, and what the data
chains hold. The kit was written against the USA disc; this command says
what has been checked on the build you have, so a report about another disc
starts from facts.

    build                 boot executable   what the kit has been run on
    USA retail (2003)     SLUS_204.69       everything; the write path was
                                            played in PCSX2 (docs/MODDING.md)
    Reloaded (JP, 2003)   SLPS_290.05       list / extract and the text
                                            exports read it (2026-11-21);
                                            it is the translation editor's
                                            Japanese source. No disc written
                                            from it has been booted.
    Japan retail (2002)   SLPS_250.xx       recognised by serial only: no
                                            image of it has been run through
                                            the kit.

Nothing in the reading stack is tied to a serial: an unknown build is read
the same way and reported as unknown. The fixed addresses the write tools
use (overlay strings, the battle font's tables) were read from the USA
executables.
"""
from __future__ import annotations

import hashlib
import struct
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from chains import CHAINS, ChainReader, available_chains
from iso9660 import IsoImage
from toc import parse_toc
import txsource

BUILDS = {
    "usa": ("USA retail", "everything in the kit; the write path has been played in PCSX2"),
    "reloaded": ("Xenosaga Episode I Reloaded (Japan)",
                 "list / extract and the text exports read it; the translation editor's "
                 "Japanese source; no disc written from it has been booted"),
    "jp-original": ("Japan retail (2002)", "recognised by its serial only; not run through the kit"),
}


@dataclass
class Info:
    path: str
    boot: Optional[str]
    build: Optional[str]                 # "usa" | "reloaded" | "jp-original" | None
    exe_sha256: Optional[str]
    exe_size: int
    symbols: Optional[int]               # entries in the boot ELF's .symtab, 0 = stripped
    chains: Dict[int, int] = field(default_factory=dict)        # chain -> objects
    compressed: Dict[int, int] = field(default_factory=dict)    # chain -> ARX objects
    overlays: List[str] = field(default_factory=list)
    volume_end: int = 0
    image_size: int = 0

    @property
    def label(self) -> str:
        return BUILDS[self.build][0] if self.build in BUILDS else "unknown build"

    @property
    def checked(self) -> str:
        return BUILDS[self.build][1] if self.build in BUILDS else \
            "not a build the kit knows; the reading commands will try it as it is"


def elf_symbol_count(elf: bytes) -> Optional[int]:
    """Entries in a 32-bit little-endian ELF's ``.symtab``; 0 when the file
    has none (stripped); None when the bytes are not such an ELF."""
    if len(elf) < 0x34 or elf[:4] != b"\x7fELF" or elf[4] != 1 or elf[5] != 1:
        return None
    shoff, = struct.unpack_from("<I", elf, 0x20)
    shentsize, shnum = struct.unpack_from("<HH", elf, 0x2E)
    if not shoff or shentsize < 0x28 or shoff + shnum * shentsize > len(elf):
        return 0
    for i in range(shnum):
        _, stype, _, _, _, size, _, _, _, entsize = struct.unpack_from("<10I", elf, shoff + i * shentsize)
        if stype == 2:                                  # SHT_SYMTAB
            return size // entsize if entsize else 0
    return 0


def info(iso_path: str) -> Info:
    build = txsource.identify_disc(iso_path)
    iso = IsoImage(iso_path)
    try:
        boot = iso.boot_elf_name()
        exe = bytes(iso.read_file(boot)) if boot and boot in iso.files else b""
        out = Info(path=str(iso_path), boot=boot, build=build,
                   exe_sha256=hashlib.sha256(exe).hexdigest() if exe else None,
                   exe_size=len(exe), symbols=elf_symbol_count(exe) if exe else None,
                   overlays=sorted(n for n in iso.files if n.upper().endswith(".OVL")),
                   volume_end=iso.volume_end, image_size=iso.image_size)
        for chain in available_chains(iso):
            entries = parse_toc(ChainReader(iso, chain).toc_bytes(), CHAINS[chain][0])
            out.chains[chain] = len(entries)
            out.compressed[chain] = sum(1 for e in entries if e.compressed)
        return out
    finally:
        iso.close()


def report(i: Info) -> str:
    lines = [f"image            {i.path}",
             f"build            {i.label}",
             f"boot executable  {i.boot or '(none found)'}"
             + (f", {i.exe_size:,} bytes" if i.exe_size else "")]
    if i.exe_sha256:
        lines.append(f"  sha256         {i.exe_sha256}")
    if i.symbols is not None:
        lines.append("  symbol table   " + (f"{i.symbols:,} entries (function names intact)" if i.symbols
                                            else "none (stripped)"))
    lines.append(f"overlays         {len(i.overlays)}" + (f" ({i.overlays[0]} .. {i.overlays[-1]})" if i.overlays else ""))
    for chain in sorted(i.chains):
        lines.append(f"chain {chain}          {i.chains[chain]:,} objects, {i.compressed[chain]:,} ARX-compressed")
    beyond = i.image_size - i.volume_end
    lines.append(f"second layer     {beyond:,} bytes after the ISO9660 volume" if beyond > 0
                 else "second layer     nothing after the ISO9660 volume")
    lines.append(f"checked with     {i.checked}")
    return "\n".join(lines)


def cmd_region(iso_path: str) -> int:
    print(report(info(iso_path)))
    return 0


def self_test(iso: Optional[str] = None) -> int:
    ok = True

    def check(cond, what):
        nonlocal ok
        print(("ok    " if cond else "FAIL  ") + what)
        ok = ok and bool(cond)

    check(elf_symbol_count(b"not an elf") is None, "a file that is not an ELF is not counted")
    hdr = bytearray(0x34)
    hdr[:6] = b"\x7fELF\x01\x01"
    check(elf_symbol_count(bytes(hdr)) == 0, "an ELF with no section table counts as stripped")
    # one SHT_SYMTAB section of 3 entries
    sh = struct.pack("<10I", 0, 2, 0, 0, 0, 48, 0, 0, 4, 16)
    struct.pack_into("<I", hdr, 0x20, 0x34)
    struct.pack_into("<HH", hdr, 0x2E, 0x28, 1)
    check(elf_symbol_count(bytes(hdr) + sh) == 3, "a .symtab of 48 bytes / 16 is 3 symbols")
    unknown = Info("x.iso", "SLES_000.00", None, None, 0, None)
    check(unknown.label == "unknown build" and "try it" in unknown.checked, "an unknown build says so")
    if iso:
        i = info(iso)
        check(i.boot and i.chains.get(0, 0) > 0, f"{i.label}: {i.boot}, chain 0 has {i.chains.get(0, 0):,} objects")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--iso":
        raise SystemExit(cmd_region(sys.argv[2]))
    raise SystemExit(self_test(sys.argv[1] if len(sys.argv) > 1 else None))
