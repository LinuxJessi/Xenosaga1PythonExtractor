"""iso9660.py — Minimal pure-Python ISO9660 reader.

Episode I's disc is plain ISO9660 (no UDF complications), so unlike the
Episode III kit there is **no 7-Zip dependency** — the standard library is
enough. Only what the extractor needs is implemented: the primary volume
descriptor, a recursive directory walk, and extent reads via mmap.

The image is a dual-layer DVD-9 (8.47 GB). Layer 0's ISO9660 volume
describes the first ~4.25 GB; layer 1 carries a **second** volume (its own
PVD at the sector where layer 0's volume-space count ends) holding
``XENOSAGA.20``-``.24``, the third bigfile chain (chain 2 in ``chains.py``:
the second half of the cutscenes, voices and movies). Both volumes are
walked here; layer-1 files get absolute LBAs so reads need no special
casing. ``carve.py`` predates this and still scans layer 1 raw; both views
agree on the bytes.
"""
from __future__ import annotations

import mmap
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, Optional

SECTOR = 2048

# PS2 boot executables are named <region>_<num>.<num>: SLUS_204.69 (USA),
# SLPS_251.60 (JP), SLES_*, SCPS_*, etc.
BOOT_RE = re.compile(r"^S[A-Z]{3}_\d{3}\.\d{2}$")


@dataclass(frozen=True)
class IsoFile:
    path: str       # backslash-joined, e.g. "IOP\\PADMAN.IRX"
    lba: int
    size: int
    is_dir: bool
    layer: int = 0  # 0 = first volume, 1 = second (dual-layer) volume


class IsoImage:
    def __init__(self, iso_path: str | Path):
        self.path = Path(iso_path)
        self._fh = open(self.path, "rb")
        self.mm = mmap.mmap(self._fh.fileno(), 0, access=mmap.ACCESS_READ)
        pvd = self.mm[16 * SECTOR : 17 * SECTOR]
        if pvd[1:6] != b"CD001":
            raise ValueError(f"{iso_path}: no ISO9660 PVD at sector 16")
        self.volume_sectors = struct.unpack_from("<I", pvd, 80)[0]
        root_lba = struct.unpack_from("<I", pvd, 158)[0]
        root_len = struct.unpack_from("<I", pvd, 166)[0]
        self.files: Dict[str, IsoFile] = {}
        for f in self._walk(root_lba, root_len, ""):
            self.files[f.path] = f
        # second volume (DVD-9 layer 1): PVD sits at layer 0's volume end,
        # its LBAs are relative to 16 sectors before that PVD
        self.layer2_base: Optional[int] = None
        self.layer2_pvd_sector: Optional[int] = None
        l2 = self.volume_sectors
        if (l2 + 1) * SECTOR <= len(self.mm) and self.mm[l2 * SECTOR + 1:l2 * SECTOR + 6] == b"CD001":
            pvd2 = self.mm[l2 * SECTOR:(l2 + 1) * SECTOR]
            base = l2 - 16
            self.layer2_base = base
            self.layer2_pvd_sector = l2
            self.layer2_volume_sectors = struct.unpack_from("<I", pvd2, 80)[0]
            r_lba = struct.unpack_from("<I", pvd2, 158)[0]
            r_len = struct.unpack_from("<I", pvd2, 166)[0]
            for f in self._walk(base + r_lba, r_len, "", base=base, layer=1):
                self.files.setdefault(f.path, f)

    # -- filesystem ---------------------------------------------------------

    def _walk(self, lba: int, length: int, prefix: str,
              base: int = 0, layer: int = 0) -> Iterator[IsoFile]:
        d = self.mm[lba * SECTOR : lba * SECTOR + length]
        i = 0
        while i < len(d):
            rl = d[i]
            if rl == 0:  # records never span sectors; skip to next
                i = (i // SECTOR + 1) * SECTOR
                continue
            nl = d[i + 32]
            if nl == 1 and d[i + 33] in (0, 1):  # "." / ".."
                i += rl
                continue
            elba = struct.unpack_from("<I", d, i + 2)[0] + base
            elen = struct.unpack_from("<I", d, i + 10)[0]
            is_dir = bool(d[i + 25] & 2)
            name = d[i + 33 : i + 33 + nl].decode("ascii", "replace").split(";")[0]
            f = IsoFile(prefix + name, elba, elen, is_dir, layer)
            yield f
            if is_dir:
                yield from self._walk(elba, elen, prefix + name + "\\", base, layer)
            i += rl

    # -- reads --------------------------------------------------------------

    def read_file(self, name: str, offset: int = 0, length: int | None = None) -> bytes:
        f = self.files[name]
        length = f.size - offset if length is None else min(length, f.size - offset)
        start = f.lba * SECTOR + offset
        return self.mm[start : start + length]

    def boot_elf_name(self) -> Optional[str]:
        """Name of the boot executable (e.g. "SLUS_204.69", "SLPS_251.60").

        Read from SYSTEM.CNF's BOOT2 line (``BOOT2 = cdrom0:\\SLUS_204.69;1``),
        falling back to the first root file matching the serial pattern.
        """
        if "SYSTEM.CNF" in self.files:
            cnf = self.read_file("SYSTEM.CNF").decode("ascii", "replace")
            m = re.search(r"BOOT2\s*=\s*cdrom0?:\\?([^;\r\n]+)", cnf)
            if m:
                name = m.group(1).strip().split("\\")[-1]
                if name in self.files:
                    return name
        for name, f in self.files.items():
            if not f.is_dir and BOOT_RE.match(name):
                return name
        return None

    @property
    def volume_end(self) -> int:
        """Byte offset where the ISO9660 volume claims to end (start of layer-1 data)."""
        return self.volume_sectors * SECTOR

    @property
    def image_size(self) -> int:
        return len(self.mm)

    def close(self) -> None:
        self.mm.close()
        self._fh.close()
