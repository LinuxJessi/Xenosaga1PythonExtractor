"""repack.py — write modified files back into an Episode I ISO.

The disc is unusually mod-friendly: plain ISO9660, all game data inside a
handful of contiguous bigfiles (chain 0 = XENOSAGA.00+.01+.02, chain 1 =
XENOSAGA.10-.13, see ``chains.py``), every object addressed by a binary TOC
whose grammar ``toc.py`` parses to the byte. So repacking never rebuilds the
image — it overwrites the object's bytes at their computed offsets and, when
a compressed object's stored size changes, patches the TOC entry in place.

Constraint: an object cannot outgrow its allocation (the gap to the next
entry's sector, both TOC-relative). In practice palette/texture edits
recompress to within a few bytes of retail — ``arx.compress`` reproduces
retail blobs byte-identically on the whole disc corpus, so an untouched
region costs nothing and a touched one only what the edit itself changes.

Usage (always work on a copy — patching is in-place):

    from repack import patch_iso
    patch_iso("copy.iso", {(0, "char\\pc\\kosmos.xtx"): new_payload_bytes})

Payloads are given *uncompressed*; entries the TOC marks compressed are
ARX-compressed automatically.
"""
from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import arx
from chains import CHAINS
from iso9660 import IsoImage
from toc import SECTOR, TocEntry, parse_toc


class RepackError(ValueError):
    pass


@dataclass
class PatchReport:
    chain: int
    path: str
    old_size: int
    new_size: int
    allocation: int
    compressed: bool


class ChainWriter:
    """Byte-addressed read/write view of one chain inside a writable ISO."""

    def __init__(self, mm: mmap.mmap, iso: IsoImage, chain: int):
        self.mm = mm
        self.names = CHAINS[chain]
        self.extents: List[Tuple[int, int]] = []  # (abs iso offset, size)
        for n in self.names:
            f = iso.files.get(n)
            if f is None:
                raise RepackError(f"{n} missing from ISO filesystem")
            self.extents.append((f.lba * SECTOR, f.size))
        self.total = sum(size for _, size in self.extents)

    def _spans(self, offset: int, length: int) -> List[Tuple[int, int, int]]:
        """(abs_iso_offset, src_start, nbytes) spans covering the range,
        crossing bigfile boundaries as needed."""
        if offset < 0 or offset + length > self.total:
            raise RepackError(
                f"chain range {offset}+{length} outside chain ({self.total})")
        spans, src = [], 0
        for base, size in self.extents:
            if offset >= size:
                offset -= size
                continue
            take = min(length - src, size - offset)
            spans.append((base + offset, src, take))
            src += take
            offset = 0
            if src == length:
                break
        return spans

    def read(self, offset: int, length: int) -> bytes:
        return b"".join(self.mm[a : a + n] for a, _, n in self._spans(offset, length))

    def write(self, offset: int, data: bytes) -> None:
        for abs_off, src, n in self._spans(offset, len(data)):
            self.mm[abs_off : abs_off + n] = data[src : src + n]


def _load_chain(mm, iso, chain):
    writer = ChainWriter(mm, iso, chain)
    toc_name = CHAINS[chain][0]
    entries = parse_toc(bytes(iso.read_file(toc_name)), toc_name)
    toc_base = iso.files[toc_name].lba * SECTOR  # TOC fields are patched here
    return writer, entries, toc_base


def _allocation(entry: TocEntry, entries: List[TocEntry], total: int) -> int:
    start = entry.byte_offset
    following = [e.byte_offset for e in entries if e.byte_offset > start]
    return (min(following) if following else total) - start


def read_entry(iso_path: str | Path, chain: int, path: str) -> bytes:
    """Read one object straight from the ISO, ARX-decompressed."""
    iso = IsoImage(iso_path)
    try:
        writer, entries, _ = _load_chain(iso.mm, iso, chain)
        entry = _find(entries, path)
        raw = writer.read(entry.byte_offset, entry.size)
        return arx.decompress(raw) if entry.compressed else raw
    finally:
        iso.close()


def _find(entries: List[TocEntry], path: str) -> TocEntry:
    want = path.lower()
    for e in entries:
        if e.path.lower() == want:
            return e
    raise RepackError(f"no TOC entry for {path!r}")


def patch_iso(iso_path: str | Path,
              replacements: Dict[Tuple[int, str], bytes],
              verbose: bool = True) -> List[PatchReport]:
    """Patch objects into ``iso_path`` **in place** (work on a copy!).

    ``replacements`` maps (chain, toc_path) -> uncompressed payload. Every
    write is verified by reading back and (for compressed entries) round-trip
    decompressing before the function returns.
    """
    iso = IsoImage(iso_path)
    reports: List[PatchReport] = []
    try:
        with open(iso.path, "r+b") as fh:
            mm = mmap.mmap(fh.fileno(), 0)
            chains_needed = sorted({c for c, _ in replacements})
            loaded = {c: _load_chain(mm, iso, c) for c in chains_needed}

            for (chain, path), payload in replacements.items():
                writer, entries, toc_base = loaded[chain]
                entry = _find(entries, path)
                alloc = _allocation(entry, entries, writer.total)

                blob = arx.compress(payload) if entry.compressed else payload
                if entry.compressed and arx.decompress(blob) != payload:
                    raise RepackError(f"{path}: ARX self-check failed")
                if len(blob) > alloc:
                    raise RepackError(
                        f"{path}: new size {len(blob)} exceeds allocation "
                        f"{alloc} (old size {entry.size})")

                writer.write(entry.byte_offset, blob)
                if len(blob) < entry.size:  # clear the stale tail
                    writer.write(entry.byte_offset + len(blob),
                                 bytes(entry.size - len(blob)))

                if entry.compressed:
                    if len(payload) >= 1 << 24:
                        raise RepackError(f"{path}: usize exceeds u24")
                    mm[toc_base + entry.fields_off + 3:
                       toc_base + entry.fields_off + 7] = struct.pack(
                           "<I", len(blob))
                    mm[toc_base + entry.fields_off + 7:
                       toc_base + entry.fields_off + 10] = struct.pack(
                           "<I", len(payload))[:3]
                elif len(blob) != entry.size:
                    mm[toc_base + entry.fields_off + 3:
                       toc_base + entry.fields_off + 7] = struct.pack(
                           "<I", len(blob))

                if writer.read(entry.byte_offset, len(blob)) != blob:
                    raise RepackError(f"{path}: read-back mismatch")
                reports.append(PatchReport(chain, path, entry.size, len(blob),
                                           alloc, entry.compressed))
                if verbose:
                    print(f"patched chain{chain} {path}: "
                          f"{entry.size} -> {len(blob)} bytes "
                          f"(allocation {alloc})")
            mm.flush()
            mm.close()
    finally:
        iso.close()
    return reports
