"""dump_symbols.py — export ELF symbol tables from XS1 code binaries to CSV.

The USA release shipped unstripped: the boot ELF (SLUS_204.69) and all five
OVL overlays carry full .symtab/.strtab sections. Binaries are discovered by
scanning --code-dir for ELFs with a .symtab, so other regions' serials (e.g.
JP SLPS_251.60) work without changes; stripped ELFs are skipped. The CSV
feeds the rest of the decomp tooling (splat symbol_addrs, Ghidra import, JNI
stub generation, progress tracking).

Usage:
    python3 dump_symbols.py --code-dir <...>/out/browse/code --out <...>/out/symbols.csv

Columns: binary,name,va,size,type,bind,shndx
  type is FUNC/OBJECT/SECTION/NOTYPE/FILE/other-int; va is hex.
"""
from __future__ import annotations

import argparse
import csv
import struct
from pathlib import Path

SYM_TYPES = {0: "NOTYPE", 1: "OBJECT", 2: "FUNC", 3: "SECTION", 4: "FILE"}
SYM_BINDS = {0: "LOCAL", 1: "GLOBAL", 2: "WEAK"}

def discover_binaries(code_dir: Path) -> list[Path]:
    """Top-level files in code_dir that are 32-bit ELFs carrying a .symtab.

    Top-level only: the EE-side binaries (boot ELF + OVL overlays) live at
    the root of browse/code, while IOP/*.IRX are R3000 modules whose symbols
    would pollute the EE-focused CSV.
    """
    found = []
    for path in sorted(code_dir.glob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if data[:4] != b"\x7fELF":
            continue
        try:
            secs = read_sections(data)
        except (struct.error, ValueError, IndexError):
            continue
        if ".symtab" in secs and ".strtab" in secs:
            found.append(path)
    return found


def read_sections(data: bytes) -> dict[str, tuple[int, int]]:
    """Return {section_name: (file_offset, size)} for a 32-bit LE ELF."""
    (shoff,) = struct.unpack_from("<I", data, 0x20)
    (shentsize,) = struct.unpack_from("<H", data, 0x2E)
    (shnum,) = struct.unpack_from("<H", data, 0x30)
    (shstrndx,) = struct.unpack_from("<H", data, 0x32)
    (str_off,) = struct.unpack_from("<I", data, shoff + shstrndx * shentsize + 16)
    secs: dict[str, tuple[int, int]] = {}
    for i in range(shnum):
        base = shoff + i * shentsize
        name_off, _typ, _flags, _addr, offset, size = struct.unpack_from(
            "<IIIIII", data, base
        )
        name = data[str_off + name_off : data.index(b"\0", str_off + name_off)]
        secs[name.decode()] = (offset, size)
    return secs


def dump_binary(path: Path):
    data = path.read_bytes()
    if data[:4] != b"\x7fELF":
        raise ValueError(f"{path}: not an ELF")
    secs = read_sections(data)
    sym_off, sym_size = secs[".symtab"]
    str_off, _ = secs[".strtab"]
    for i in range(sym_size // 16):
        name_off, val, size, info, _other, shndx = struct.unpack_from(
            "<IIIBBH", data, sym_off + i * 16
        )
        name = data[str_off + name_off : data.index(b"\0", str_off + name_off)]
        yield (
            name.decode(errors="replace"),
            val,
            size,
            SYM_TYPES.get(info & 0xF, str(info & 0xF)),
            SYM_BINDS.get(info >> 4, str(info >> 4)),
            shndx,
        )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code-dir", required=True, type=Path)
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    rows = 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["binary", "name", "va", "size", "type", "bind", "shndx"])
        binaries = discover_binaries(args.code_dir)
        for path in binaries:
            binary = path.name
            for name, va, size, typ, bind, shndx in dump_binary(path):
                if not name:
                    continue
                w.writerow([binary, name, f"0x{va:x}", size, typ, bind, shndx])
                rows += 1
    print(f"{args.out}: {rows} symbols from {len(binaries)} binaries")


if __name__ == "__main__":
    main()
