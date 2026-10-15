"""chains.py — Episode I's two bigfile chains.

A TOC's sector numbers address a virtual byte space formed by concatenating
the TOC file itself with its sibling bigfiles, in order:

    chain 0:  XENOSAGA.00 + XENOSAGA.01 + XENOSAGA.02      (system / field data)
    chain 1:  XENOSAGA.10 + XENOSAGA.11 + XENOSAGA.12 + XENOSAGA.13
              (streaming data: voice .vds/.vdm, scene .fpk/.arc/.evt, movie .pss)
    chain 2:  XENOSAGA.20 + XENOSAGA.21 .. XENOSAGA.24   (DVD-9 layer 1: the
              second half of the cutscenes, voices and movies — 266 objects)

The engine reads by raw sector (``xglCdReadFile`` -> ``sceCdRead``): LBA =
TOC file LBA + entry sector. That is why the bigfiles must be contiguous on
disc, and why a relocated object (``repack.patch_iso(grow=True)``) may live
anywhere on the image: ``read`` falls back to absolute addressing for
offsets past the chain's own extents.

Verified: the maximum extent of every entry in each TOC lands exactly at (or
within 384 bytes of) the end of its chain, and reads at computed offsets
produce the right magics (e.g. ``movie\\mpeg2\\2034.pss`` → MPEG-2 pack header).
"""
from __future__ import annotations

from typing import Iterator, List, Tuple

from iso9660 import IsoImage, SECTOR

CHAINS: dict[int, List[str]] = {
    0: ["XENOSAGA.00", "XENOSAGA.01", "XENOSAGA.02"],
    1: ["XENOSAGA.10", "XENOSAGA.11", "XENOSAGA.12", "XENOSAGA.13"],
    2: ["XENOSAGA.20", "XENOSAGA.21", "XENOSAGA.22", "XENOSAGA.23", "XENOSAGA.24"],
}


def available_chains(iso: IsoImage) -> List[int]:
    """Chains whose bigfiles all exist on this image (chain 2 needs layer 1)."""
    return [c for c, names in CHAINS.items() if all(n in iso.files for n in names)]

CHUNK = 8 * 1024 * 1024


class ChainReader:
    def __init__(self, iso: IsoImage, chain: int):
        self.iso = iso
        self.names = CHAINS[chain]
        for n in self.names:
            if n not in iso.files:
                raise FileNotFoundError(f"{n} missing from ISO filesystem")
        self.sizes = [iso.files[n].size for n in self.names]
        self.total = sum(self.sizes)
        self.toc_lba = iso.files[self.names[0]].lba

    def toc_bytes(self) -> bytes:
        return self.iso.read_file(self.names[0])

    def read_iter(self, offset: int, length: int) -> Iterator[bytes]:
        """Yield the byte range [offset, offset+length) of the chain in chunks,
        transparently spanning bigfile boundaries."""
        if offset + length > self.total:
            # relocated object: the engine addresses by absolute sector
            start = self.toc_lba * SECTOR + offset
            if start + length > self.iso.image_size:
                raise ValueError(
                    f"read past image end: chain offset {offset}+{length}")
            yield self.iso.mm[start:start + length]
            return
        remaining = length
        for name, size in zip(self.names, self.sizes):
            if offset >= size:
                offset -= size
                continue
            take = min(remaining, size - offset)
            pos = 0
            while pos < take:
                n = min(CHUNK, take - pos)
                yield self.iso.read_file(name, offset + pos, n)
                pos += n
            remaining -= take
            offset = 0
            if remaining == 0:
                return

    def read(self, offset: int, length: int) -> bytes:
        return b"".join(self.read_iter(offset, length))

    def locate(self, offset: int) -> Tuple[str, int]:
        """Map a chain byte offset to (bigfile name, offset within it)."""
        for name, size in zip(self.names, self.sizes):
            if offset < size:
                return name, offset
            offset -= size
        raise ValueError(f"offset {offset} beyond chain end")
