"""carve.py — Recover the layer-1 movies that live outside the filesystem.

The ISO9660 volume covers only layer 0 (~4.25 GB) of the 8.47 GB disc. The
remaining ~4.2 GB is wall-to-wall MPEG-2 program-stream data: 58 distinct
movies that no TOC indexes (the game reaches them by absolute sector — the
45 TOC-indexed ``movie\\mpeg2\\*.pss`` files in chain 1 are a separate,
smaller set).

A stream start is a sector-aligned MPEG-2 pack header (``00 00 01 BA``)
immediately followed by a system header (``00 00 01 BB``) whose SCR is near
zero — the system clock resets at the front of each movie, so an SCR under
one second distinguishes a true start from a mid-stream repeated system
header. On this disc all 58 candidate sites pass the SCR test. Each stream
runs to the next start (or end of image).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from iso9660 import IsoImage, SECTOR

PACK = b"\x00\x00\x01\xba"
SYSTEM = b"\x00\x00\x01\xbb"


@dataclass(frozen=True)
class CarvedStream:
    index: int
    start: int   # absolute byte offset in the image
    size: int

    @property
    def sector(self) -> int:
        return self.start // SECTOR

    @property
    def name(self) -> str:
        return f"layer1_{self.index:03d}_lba{self.sector}.pss"


def _scr_seconds(mm, pos: int) -> float | None:
    """Decode the pack header's 33-bit SCR base to seconds (90 kHz units)."""
    b = mm[pos + 4 : pos + 10]
    if len(b) < 6:
        return None
    base = (
        ((b[0] >> 3) & 7) << 30
        | (b[0] & 3) << 28
        | b[1] << 20
        | (b[2] >> 3) << 15
        | (b[2] & 3) << 13
        | b[3] << 5
        | b[4] >> 3
    )
    return base / 90000.0


def scan_layer1(iso: IsoImage) -> List[CarvedStream]:
    mm = iso.mm
    first = mm.find(PACK, iso.volume_end - 1024 * 1024)
    if first < 0:
        return []
    starts = []
    pos = (first // SECTOR) * SECTOR
    end = iso.image_size
    while pos < end:
        if mm[pos : pos + 4] == PACK and mm[pos + 14 : pos + 18] == SYSTEM:
            scr = _scr_seconds(mm, pos)
            if scr is not None and scr < 1.0:
                starts.append(pos)
        pos += SECTOR
    return [
        CarvedStream(i, s, (starts[i + 1] if i + 1 < len(starts) else end) - s)
        for i, s in enumerate(starts)
    ]
