"""repack.py — write modified files back into an Episode I ISO.

The disc is unusually mod-friendly: plain ISO9660, all game data inside a
handful of contiguous bigfiles (chain 0 = XENOSAGA.00+.01+.02, chain 1 =
XENOSAGA.10-.13, chain 2 = XENOSAGA.20-.24 on the second layer, see
``chains.py``), every object addressed by a binary TOC whose grammar
``toc.py`` parses to the byte. So repacking never rebuilds the image — it
overwrites the object's bytes at their computed offsets and, when a stored
size changes, patches the TOC entry in place.

Constraint: an object cannot outgrow its allocation (the gap to the next
entry's sector, both TOC-relative) — **unless** ``grow=True``. The engine
reads objects by raw sector (``xglCdReadFile`` -> ``sceCdRead``, LBA = TOC
file LBA + entry sector), so an object may live anywhere on the image: a
grown object is appended after the last sector of the disc and its TOC
sector repointed there. The second layer's volume-space count is bumped
so the ISO9660 volume still covers every sector. The idea of relocating
rather than rebuilding comes from poketony's XS1KOR workspace for the
Japanese disc (https://github.com/poketony/XS1KOR); the append strategy
here is this kit's own, chosen because every chain on the USA disc is
packed to the sector with no free gaps.

Usage (always work on a copy — patching is in-place):

    from repack import patch_iso
    patch_iso("copy.iso", {(0, "char\\\\pc\\\\kosmos.xtx"): new_payload_bytes})
    patch_iso("copy.iso", {(0, "scene\\\\ST0210.evt"): bigger}, grow=True)

Payloads are given *uncompressed*; entries the TOC marks compressed are
ARX-compressed automatically.
"""
from __future__ import annotations

import mmap
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import arx
from chains import CHAINS
from iso9660 import IsoImage
from toc import SECTOR, TocEntry, parse_toc

DVD9_SECTORS = 4_173_824          # 8,543,666,176 bytes, the single-sided DL ceiling


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
    relocated_lba: Optional[int] = None   # absolute sector, when grown


class ChainWriter:
    """Byte-addressed read/write view of one chain inside a writable ISO.

    Offsets are chain-relative (TOC file = offset 0). Inside the bigfiles
    they map through the ISO9660 extents; beyond them (relocated objects)
    through the engine's own rule, absolute = TOC LBA * 2048 + offset.
    """

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
        self.toc_lba = iso.files[self.names[0]].lba

    def _spans(self, offset: int, length: int) -> List[Tuple[int, int, int]]:
        """(abs_iso_offset, src_start, nbytes) spans covering the range,
        crossing bigfile boundaries as needed."""
        if offset < 0:
            raise RepackError(f"negative chain offset {offset}")
        if offset + length > self.total:
            start = self.toc_lba * SECTOR + offset
            if start + length > len(self.mm):
                raise RepackError(
                    f"chain range {offset}+{length} outside the image")
            return [(start, 0, length)]
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
    end = min(following) if following else total
    if start >= total:                     # already relocated past the chain
        return max(end - start, (entry.size + SECTOR - 1) // SECTOR * SECTOR)
    return end - start


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


def _plan(iso: IsoImage, replacements) -> Tuple[Dict, int]:
    """Compress/validate every payload up front; return per-key blobs and the
    number of sectors that must be appended for the ones that don't fit."""
    plan = {}
    extra = 0
    loaded = {}
    for (chain, path), payload in replacements.items():
        if chain not in loaded:
            loaded[chain] = _load_chain(iso.mm, iso, chain)
        writer, entries, toc_base = loaded[chain]
        entry = _find(entries, path)
        alloc = _allocation(entry, entries, writer.total)
        blob = arx.compress(payload) if entry.compressed else payload
        if entry.compressed and arx.decompress(blob) != payload:
            raise RepackError(f"{path}: ARX self-check failed")
        if entry.compressed and len(payload) >= 1 << 24:
            raise RepackError(f"{path}: usize exceeds u24")
        fits = len(blob) <= alloc
        if not fits:
            extra += (len(blob) + SECTOR - 1) // SECTOR
        plan[(chain, path)] = (blob, len(payload), entry, alloc, fits, toc_base)
    return plan, extra


def patch_iso(iso_path: str | Path,
              replacements: Dict[Tuple[int, str], bytes],
              verbose: bool = True,
              grow: bool = False) -> List[PatchReport]:
    """Patch objects into ``iso_path`` **in place** (work on a copy!).

    ``replacements`` maps (chain, toc_path) -> uncompressed payload. Every
    write is verified by reading back and (for compressed entries) round-trip
    decompressing before the function returns.

    With ``grow=True`` an object larger than its allocation is appended to
    the end of the image (sector-aligned) and its TOC entry repointed;
    without it the same case raises ``RepackError`` and nothing is written.
    """
    iso = IsoImage(iso_path)
    reports: List[PatchReport] = []
    try:
        plan, extra_sectors = _plan(iso, replacements)
        too_big = [p for (c, p), v in plan.items() if not v[4]]
        if too_big and not grow:
            c, p = next(k for k, v in plan.items() if not v[4])
            blob, _, entry, alloc, _, _ = plan[(c, p)]
            raise RepackError(
                f"{p}: new size {len(blob)} exceeds allocation {alloc} "
                f"(old size {entry.size}); pass grow=True to relocate it")
        image_size = iso.image_size
        if image_size % SECTOR:
            raise RepackError("image size is not a multiple of 2048")
        if extra_sectors:
            with open(iso.path, "r+b") as fh:
                fh.truncate(image_size + extra_sectors * SECTOR)
            new_total = image_size // SECTOR + extra_sectors
            if new_total > DVD9_SECTORS and verbose:
                print(f"warning: image now {new_total} sectors, beyond the "
                      f"DVD-9 ceiling of {DVD9_SECTORS}; fine for emulators, "
                      f"not burnable")
        with open(iso.path, "r+b") as fh:
            mm = mmap.mmap(fh.fileno(), 0)
            writers = {c: ChainWriter(mm, iso, c) for c in {c for c, _ in plan}}
            append_sector = image_size // SECTOR
            for (chain, path), (blob, usize, entry, alloc, fits, toc_base) in plan.items():
                writer = writers[chain]
                if fits:
                    offset = entry.byte_offset
                    sector_field = None
                    relocated = None
                else:
                    relocated = append_sector
                    append_sector += (len(blob) + SECTOR - 1) // SECTOR
                    sector_field = relocated - writer.toc_lba
                    if sector_field >= 1 << 24:
                        raise RepackError(f"{path}: relocated sector exceeds u24")
                    offset = sector_field * SECTOR
                writer.write(offset, blob)
                if fits and len(blob) < entry.size:  # clear the stale tail
                    writer.write(offset + len(blob), bytes(entry.size - len(blob)))

                f = toc_base + entry.fields_off
                if sector_field is not None:
                    mm[f:f + 3] = struct.pack("<I", sector_field)[:3]
                if entry.compressed:
                    mm[f + 3:f + 7] = struct.pack("<I", len(blob))
                    mm[f + 7:f + 10] = struct.pack("<I", usize)[:3]
                elif len(blob) != entry.size:
                    mm[f + 3:f + 7] = struct.pack("<I", len(blob))

                if writer.read(offset, len(blob)) != blob:
                    raise RepackError(f"{path}: read-back mismatch")
                reports.append(PatchReport(chain, path, entry.size, len(blob),
                                           alloc, entry.compressed, relocated))
                if verbose:
                    where = f" -> relocated to sector {relocated}" if relocated is not None else ""
                    print(f"patched chain{chain} {path}: "
                          f"{entry.size} -> {len(blob)} bytes "
                          f"(allocation {alloc}){where}")
            if extra_sectors:
                _bump_volume(mm, iso, extra_sectors)
            mm.flush()
            mm.close()
    finally:
        iso.close()
    return reports


def fs_allocation(iso: IsoImage, name: str) -> int:
    """Bytes available to an ISO9660 file before the next extent starts."""
    f = iso.files[name]
    later = [g.lba for g in iso.files.values()
             if g.layer == f.layer and g.lba > f.lba]
    end = min(later) * SECTOR if later else iso.volume_end
    return end - f.lba * SECTOR


def patch_fs_file(iso_path: str | Path, name: str, data: bytes,
                  verbose: bool = True) -> None:
    """Replace a plain ISO9660 file (the boot ELF, an ``.OVL`` overlay, an
    IOP module) **in place**. The new content must fit before the next
    extent; the directory record's length is updated so the console's
    filesystem reads the new size. Work on a copy."""
    iso = IsoImage(iso_path)
    try:
        f = iso.files[name]
        alloc = fs_allocation(iso, name)
        if len(data) > alloc:
            raise RepackError(f"{name}: new size {len(data)} exceeds the "
                              f"{alloc} bytes before the next file")
        with open(iso.path, "r+b") as fh:
            mm = mmap.mmap(fh.fileno(), 0)
            start = f.lba * SECTOR
            mm[start:start + len(data)] = data
            if len(data) < f.size:          # clear the stale tail
                mm[start + len(data):start + f.size] = bytes(f.size - len(data))
            r = f.record_off
            mm[r + 10:r + 14] = struct.pack("<I", len(data))
            mm[r + 14:r + 18] = struct.pack(">I", len(data))
            if bytes(mm[start:start + len(data)]) != data:
                raise RepackError(f"{name}: read-back mismatch")
            mm.flush()
            mm.close()
        if verbose:
            print(f"patched {name}: {f.size} -> {len(data)} bytes (allocation {alloc})")
    finally:
        iso.close()


def relocate_fs_file(iso_path: str | Path, name: str, data: bytes,
                     verbose: bool = True) -> int:
    """Append a plain ISO9660 file's new content after the last sector of the
    image and repoint its directory record there (extent + length). The
    console's filesystem follows the record, so the place does not matter;
    the volume-space count is bumped as for grown TOC objects. Returns the
    new LBA. Work on a copy."""
    iso = IsoImage(iso_path)
    try:
        f = iso.files[name]
        image_size = iso.image_size
        if image_size % SECTOR:
            raise RepackError("image size is not a multiple of 2048")
        sectors = (len(data) + SECTOR - 1) // SECTOR
        new_lba = image_size // SECTOR
        if new_lba + sectors > DVD9_SECTORS and verbose:
            print(f"warning: image now {new_lba + sectors} sectors, beyond the "
                  f"DVD-9 ceiling of {DVD9_SECTORS}; fine for emulators, not burnable")
        with open(iso.path, "r+b") as fh:
            fh.truncate(image_size + sectors * SECTOR)
            mm = mmap.mmap(fh.fileno(), 0)
            start = new_lba * SECTOR
            mm[start:start + len(data)] = data
            r = f.record_off
            rec_lba = new_lba - (iso.layer2_base if f.layer else 0)
            mm[r + 2:r + 6] = struct.pack("<I", rec_lba)
            mm[r + 6:r + 10] = struct.pack(">I", rec_lba)
            mm[r + 10:r + 14] = struct.pack("<I", len(data))
            mm[r + 14:r + 18] = struct.pack(">I", len(data))
            _bump_volume(mm, iso, sectors)
            if bytes(mm[start:start + len(data)]) != data:
                raise RepackError(f"{name}: read-back mismatch")
            mm.flush()
            mm.close()
        if verbose:
            print(f"patched {name}: {f.size} -> {len(data)} bytes, relocated to sector {new_lba}")
        return new_lba
    finally:
        iso.close()


def _bump_volume(mm: mmap.mmap, iso: IsoImage, sectors: int) -> None:
    """Grow the volume-space count of the volume that ends the image."""
    pvd_sector = iso.layer2_pvd_sector if iso.layer2_pvd_sector is not None else 16
    base = pvd_sector * SECTOR
    old = struct.unpack_from("<I", mm, base + 80)[0]
    new = old + sectors
    mm[base + 80:base + 84] = struct.pack("<I", new)
    mm[base + 84:base + 88] = struct.pack(">I", new)
