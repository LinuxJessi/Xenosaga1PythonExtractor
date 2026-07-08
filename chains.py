"""chains.py — Episode I's two bigfile chains.

A TOC's sector numbers address a virtual byte space formed by concatenating
the TOC file itself with its sibling bigfiles, in order:

    chain 0:  XENOSAGA.00 + XENOSAGA.01 + XENOSAGA.02      (system / field data)
    chain 1:  XENOSAGA.10 + XENOSAGA.11 + XENOSAGA.12 + XENOSAGA.13
              (streaming data: voice .vds/.vdm, scene .fpk/.arc/.evt, movie .pss)

Verified: the maximum extent of every entry in each TOC lands exactly at (or
within 384 bytes of) the end of its chain, and reads at computed offsets
produce the right magics (e.g. ``movie\\mpeg2\\2034.pss`` → MPEG-2 pack header).
"""
from __future__ import annotations

from typing import Iterator, List, Tuple

from iso9660 import IsoImage

CHAINS: dict[int, List[str]] = {
    0: ["XENOSAGA.00", "XENOSAGA.01", "XENOSAGA.02"],
    1: ["XENOSAGA.10", "XENOSAGA.11", "XENOSAGA.12", "XENOSAGA.13"],
}

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

    def toc_bytes(self) -> bytes:
        return self.iso.read_file(self.names[0])

    def read_iter(self, offset: int, length: int) -> Iterator[bytes]:
        """Yield the byte range [offset, offset+length) of the chain in chunks,
        transparently spanning bigfile boundaries."""
        if offset + length > self.total:
            raise ValueError(
                f"read past chain end: {offset}+{length} > {self.total}"
            )
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
