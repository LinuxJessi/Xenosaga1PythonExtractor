"""iso9660.py — Minimal pure-Python ISO9660 reader.

Episode I's disc is plain ISO9660 (no UDF complications), so unlike the
Episode III kit there is **no 7-Zip dependency** — the standard library is
enough. Only what the extractor needs is implemented: the primary volume
descriptor, a recursive directory walk, and extent reads via mmap.

The image is 8.47 GB but the ISO9660 volume only describes the first
~4.25 GB (layer 0). The rest of the disc — all of layer 1 — is raw movie
data outside the filesystem; see ``carve.py``.
"""
from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator

SECTOR = 2048


@dataclass(frozen=True)
class IsoFile:
    path: str       # backslash-joined, e.g. "IOP\\PADMAN.IRX"
    lba: int
    size: int
    is_dir: bool


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

    # -- filesystem ---------------------------------------------------------

    def _walk(self, lba: int, length: int, prefix: str) -> Iterator[IsoFile]:
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
            elba = struct.unpack_from("<I", d, i + 2)[0]
            elen = struct.unpack_from("<I", d, i + 10)[0]
            is_dir = bool(d[i + 25] & 2)
            name = d[i + 33 : i + 33 + nl].decode("ascii", "replace").split(";")[0]
            f = IsoFile(prefix + name, elba, elen, is_dir)
            yield f
            if is_dir:
                yield from self._walk(elba, elen, prefix + name + "\\")
            i += rl

    # -- reads --------------------------------------------------------------

    def read_file(self, name: str, offset: int = 0, length: int | None = None) -> bytes:
        f = self.files[name]
        length = f.size - offset if length is None else min(length, f.size - offset)
        start = f.lba * SECTOR + offset
        return self.mm[start : start + length]

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
