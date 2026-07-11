#!/usr/bin/env python3
"""pinkhair.py — make KOS-MOS's hair pink. A worked example for repack.py.

Her textures are PSMT8 indexed images; hair color lives in a handful of
256-entry CLUT tiles inside each .xtx (see docs/FORMATS.md). So the mod is
a palette edit — no pixels move, and recompressed sizes come out identical.

It works in two phases:

1. **Curated recolor.** For each character .xtx, hue-rotate the
   blue-dominant entries of hand-curated hair CLUT tiles (canvas coords
   chosen from the .lex material bindings, so armor/visor blues stay blue).

2. **Disc-wide row sweep.** Each recolored CLUT tile row is a distinctive
   64-byte string, and the disc embeds copies of the character textures
   inside other containers (yamamoto battle bundles, per-scene char
   bundles scene\\cf*.a — 12 carriers total, found by sweeping every
   object). Rather than parse each container, every chain-0 and chain-1
   object is byte-swept with the old-row -> new-row map.

3. **Entry-level pass for re-framed carriers.** The scene\\cf*.a bundles
   store the canvas re-framed with 4-byte inserts, so whole 64-byte rows
   only partially survive (the row sweep alone left the opening-sim
   KOS-MOS 75% blue — caught live via PINE RAM forensics). Where >= 4
   quarter-row (16-byte) anchors land in a file, every aligned 4-byte
   word in the anchored span equal to a recolored entry's original value
   is replaced. Safe because the hair ramp shares no exact RGBA value
   with any other tile in the same canvas (verified: 0 overlap).

kosmos2 (special costume) has silver hair — no blue entries — and is left
alone. Pre-rendered movies keep blue hair; re-encoding FMVs is out of scope.

Usage:
    python pinkhair.py --iso GAME.iso --out PINK.iso [--hue 0.92]
                       [--preview DIR] [--dry-run]
"""
from __future__ import annotations

import argparse
import colorsys
import shutil
import struct
import sys
from pathlib import Path

import arx
from browse import decode_xtx, write_png
from chains import CHAINS, ChainReader
from iso9660 import IsoImage
from repack import patch_iso, read_entry
from toc import parse_toc

# hue 0.92 ~ 330 degrees: rose pink. Saturation is nudged up slightly so
# the desaturated highlight entries still read as pink, not gray.
DEFAULT_HUE = 0.92


def _is_hair_blue(r: int, g: int, b: int) -> bool:
    light = b > 140 and b > r + 40 and g > r     # strand sheet / highlights
    deep = b > 90 and b > r + 30 and b >= g      # CLUT ramp / shadows
    return light or deep


def _recolor_rgb(r: int, g: int, b: int, hue: float):
    h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    nr, ng, nb = colorsys.hls_to_rgb(hue, l, min(1.0, s * 1.1))
    return int(nr * 255), int(ng * 255), int(nb * 255)

# (xtx path, paired lex path, hair CLUT tiles as canvas coords)
TARGETS = [
    ("char\\pc\\kosmos.xtx",    "char\\pc\\kosmos.lex",    [(448, 96)]),
    ("char\\pc\\kosmos1.xtx",   "char\\pc\\kosmos1.lex",   [(448, 96)]),
    ("char\\pc\\kosmos_h.xtx",  "char\\pc\\kosmos_h.lex",  [(224, 96), (384, 144)]),
    ("char\\pc\\kosmos_h1.xtx", "char\\pc\\kosmos_h1.lex", [(224, 96), (384, 144)]),
    ("char\\pc\\kosmos_h3.xtx", "char\\pc\\kosmos_h3.lex", [(224, 96), (224, 112), (384, 144)]),
    ("char\\pc\\kosmos_h5.xtx", "char\\pc\\kosmos_h5.lex", [(224, 96), (384, 144)]),
]


def xtx_subs(data: bytes, base: int = 0):
    """(w, bufw, h, gs_off, file_addr) per sub-image of the XTX at ``base``."""
    if data[base : base + 4] != b"XTX\x00":
        raise ValueError("not an XTX")
    count, hdr = struct.unpack_from("<II", data, base + 8)
    subs = []
    for i in range(count):
        w, bufw, h = struct.unpack_from("<HHH", data, base + hdr + 20 * i)
        gs_off, _size, addr = struct.unpack_from("<III", data, base + hdr + 20 * i + 8)
        subs.append((w, bufw, h, gs_off, addr))
    return subs


def _canvas_to_file(subs, bufw: int, px: int, py: int) -> int:
    """File offset (relative to the XTX start) of canvas pixel (px, py)."""
    for w, _, h, gs_off, addr in subs:
        block = gs_off // 4096
        x0 = (block % (bufw // 2)) * 64
        y0 = (block // (bufw // 2)) * 32
        if x0 <= px < x0 + w and y0 <= py < y0 + h:
            return addr + 32 + ((py - y0) * w + (px - x0)) * 4
    raise ValueError(f"canvas ({px},{py}) not covered by any sub-image")


def recolor_tile_rows(xtx: bytes, palx: int, paly: int, hue: float):
    """Yield (row_offset, old_row, new_row) for each changed 64-byte row of
    the 16x16 CLUT tile at canvas (palx, paly). Storage is raw CT32, so
    per-entry recoloring needs no CSM1 or swizzle awareness."""
    subs = xtx_subs(xtx)
    bufw = subs[0][1] or 8
    for ey in range(16):
        off = _canvas_to_file(subs, bufw, palx, paly + ey)
        old = xtx[off : off + 64]
        new = bytearray(old)
        for ex in range(16):
            r, g, b = new[ex * 4], new[ex * 4 + 1], new[ex * 4 + 2]
            if b > 90 and b > r + 30 and b >= g:
                new[ex * 4], new[ex * 4 + 1], new[ex * 4 + 2] = (
                    _recolor_rgb(r, g, b, hue))
        if bytes(new) != old:
            yield off, old, bytes(new)


def strand_segments(xtx: bytes, hue: float):
    """(old, new) canvas-row segments for the raw-CT32 hair strand sheets.

    The long hair is NOT paletted: it lives in the canvas as true-colour
    CT32 pixel regions (~250 distinct colours per 16x16 tile — which is
    also why those regions decode to noise as PSMT8). Detect 16px tiles
    that are overwhelmingly light-blue, group contiguous tiles per tile
    row, and hue-rotate every hair-blue pixel. Position-independent, so
    GS swizzle never enters into it."""
    subs = xtx_subs(xtx)
    bufw = subs[0][1] or 8
    tiles = set()
    for ty in range(0, 512, 16):
        for tx in range(0, 512, 16):
            light = 0
            try:
                for ey in range(16):
                    off = _canvas_to_file(subs, bufw, tx, ty + ey)
                    for ex in range(16):
                        r, g, b, a = xtx[off + ex * 4: off + ex * 4 + 4]
                        if a > 0x90:
                            raise ValueError
                        if b > 140 and b > r + 40 and g > r:
                            light += 1
            except ValueError:
                continue
            if light >= 200:
                tiles.add((tx, ty))
    pairs = []
    for ty in sorted({t[1] for t in tiles}):
        xs = sorted(t[0] for t in tiles if t[1] == ty)
        groups, cur = [], [xs[0]]
        for x in xs[1:]:
            if x == cur[-1] + 16:
                cur.append(x)
            else:
                groups.append(cur)
                cur = [x]
        groups.append(cur)
        for grp in groups:
            for ey in range(16):
                off = _canvas_to_file(subs, bufw, grp[0], ty + ey)
                old = xtx[off: off + (grp[-1] + 16 - grp[0]) * 4]
                new = bytearray(old)
                for i in range(0, len(new), 4):
                    r, g, b = new[i], new[i + 1], new[i + 2]
                    if _is_hair_blue(r, g, b):
                        new[i], new[i + 1], new[i + 2] = _recolor_rgb(r, g, b, hue)
                if bytes(new) != old:
                    pairs.append((old, bytes(new)))
    return pairs


def patch_reframed_row(buf: bytearray, old: bytes, new: bytes) -> int:
    """Patch one strand row inside a scene\\cf*.a bundle, which stores
    canvas data with sporadic 4-byte zero words inserted or elided but
    non-zero pixels in order. Anchor on a findable 16-byte window, then
    walk both directions replacing old pixels with new, skipping zero
    insertions/elisions. Returns pixels recolored (0 if no anchor)."""
    ZERO = b"\x00\x00\x00\x00"
    anchor_k = -1
    for k in range(0, len(old) - 16, 4):
        pos = buf.find(old[k:k + 16])
        if pos >= 0:
            anchor_k = k
            break
    if anchor_k < 0:
        return 0
    n = 0
    for step, ci0, bi0 in ((4, anchor_k, pos), (-4, anchor_k - 4, pos - 4)):
        ci, bi = ci0, bi0
        while 0 <= ci < len(old) and 0 <= bi < len(buf) - 3:
            w = bytes(buf[bi:bi + 4])
            o = old[ci:ci + 4]
            if w == o or w == new[ci:ci + 4]:
                if w == o and o != new[ci:ci + 4]:
                    buf[bi:bi + 4] = new[ci:ci + 4]
                    n += 1
                ci += step
                bi += step
            elif w == ZERO:
                bi += step          # zero word inserted in the bundle
            elif o == ZERO:
                ci += step          # canvas zero elided in the bundle
            else:
                break               # lost alignment: stop, stay safe
    return n


def build_maps(iso_path: str, hue: float):
    """(row_map, entry_map, quarters): 64-byte row and 4-byte entry
    replacements plus 16-byte quarter-row anchors, over all curated tiles."""
    row_map: dict[bytes, bytes] = {}
    entry_map: dict[bytes, bytes] = {}
    quarters: set[bytes] = set()
    strand_pairs: list[tuple[bytes, bytes]] = []
    seen_strands: set[bytes] = set()
    for xtx_path, _, tiles in TARGETS:
        xtx = read_entry(iso_path, 0, xtx_path)
        for palx, paly in tiles:
            for _, old, new in recolor_tile_rows(xtx, palx, paly, hue):
                if old in row_map and row_map[old] != new:
                    raise RuntimeError("inconsistent row mapping")
                row_map[old] = new
                for i in range(0, 64, 4):
                    if old[i:i+4] != new[i:i+4]:
                        entry_map[old[i:i+4]] = new[i:i+4]
                for i in range(0, 64, 16):
                    if len(set(old[i:i+16])) > 4:  # skip flat quarters
                        quarters.add(old[i:i+16])
        for old, new in strand_segments(xtx, hue):
            if old in seen_strands:
                continue
            seen_strands.add(old)
            row_map[old] = new           # verbatim carriers via .replace
            strand_pairs.append((old, new))  # re-framed carriers via walker
    return row_map, entry_map, quarters, strand_pairs


def sweep(iso_path: str, row_map, entry_map, quarters, strand_pairs=()):
    """Apply row-level, strand-walker, then anchored entry-level
    replacement to every chain object; yield (chain, path, new payload,
    rows, entries, strand_px) per change."""
    iso = IsoImage(iso_path)
    try:
        for chain in (0, 1):
            reader = ChainReader(iso, chain)
            entries = parse_toc(reader.toc_bytes(), CHAINS[chain][0])
            for e in entries:
                raw = b"".join(reader.read_iter(e.byte_offset, e.size))
                payload = arx.decompress(raw) if e.compressed else raw

                anchors = []
                for q in quarters:
                    i = payload.find(q)
                    while i >= 0:
                        anchors.append(i)
                        i = payload.find(q, i + 1)
                if not anchors:
                    continue    # every carrier holds CLUT quarter anchors

                n_rows = 0
                for old, new in row_map.items():
                    if old in payload:
                        payload = payload.replace(old, new)
                        n_rows += 1

                strand_px = 0
                if len(anchors) >= 4 and strand_pairs:
                    buf = bytearray(payload)
                    for old, new in strand_pairs:
                        if new in buf:      # already handled by .replace
                            continue
                        strand_px += patch_reframed_row(buf, old, new)
                    if strand_px:
                        payload = bytes(buf)

                n_entries = 0
                if len(anchors) >= 4:
                    phases = {a % 4 for a in anchors}
                    if len(phases) != 1:
                        raise RuntimeError(
                            f"{e.path}: mixed palette alignment {phases}")
                    phase = phases.pop()
                    lo = max(phase, min(anchors) - 0x2000)
                    hi = min(len(payload), max(anchors) + 0x2000)
                    buf = bytearray(payload)
                    for o in range(lo + (phase - lo) % 4, hi - 3, 4):
                        w = bytes(buf[o:o+4])
                        if w in entry_map:
                            buf[o:o+4] = entry_map[w]
                            n_entries += 1
                    if n_entries:
                        payload = bytes(buf)

                if n_rows or n_entries or strand_px:
                    yield chain, e.path, payload, n_rows, n_entries, strand_px
    finally:
        iso.close()


def run(iso: str, out: str | None, hue: float = DEFAULT_HUE,
        preview: str | None = None, dry_run: bool = False) -> int:
    """The whole recipe; callable from cli.py / the GUI as well as main()."""
    if not dry_run and not out:
        raise ValueError("out is required unless dry_run")

    row_map, entry_map, quarters, strand_pairs = build_maps(iso, hue)
    print(f"maps: {len(row_map)} rows/segments ({len(strand_pairs)} strand), "
          f"{len(entry_map)} entry values, {len(quarters)} anchors "
          f"(hue {hue})", flush=True)

    print("sweeping every disc object for embedded copies ...", flush=True)
    replacements = {}
    for chain, path, payload, n_rows, n_entries, strand_px in sweep(
            iso, row_map, entry_map, quarters, strand_pairs):
        print(f"  hit: chain{chain} {path} ({n_rows} rows, "
              f"{n_entries} loose entries, {strand_px} walked strand px)",
              flush=True)
        replacements[(chain, path)] = payload

    if preview:
        pdir = Path(preview)
        pdir.mkdir(parents=True, exist_ok=True)
        for xtx_path, lex_path, _ in TARGETS:
            stem = xtx_path.split("\\")[-1].removesuffix(".xtx")
            lex = read_entry(iso, 0, lex_path)
            for tag, blob in (("before", read_entry(iso, 0, xtx_path)),
                              ("after", replacements[(0, xtx_path)])):
                res = decode_xtx(bytes(blob), lex)
                if res:
                    write_png(pdir / f"{stem}.{tag}.png", *res)
        print(f"previews under {pdir}", flush=True)

    if dry_run:
        print(f"dry run: {len(replacements)} objects would be patched")
        return 0

    src, dst = Path(iso), Path(out)
    if src.resolve() != dst.resolve():
        print(f"copying {src.name} -> {dst} ({src.stat().st_size / 1e9:.1f} GB) ...",
              flush=True)
        shutil.copyfile(src, dst)
    patch_iso(dst, replacements)
    print(f"done: {dst} ({len(replacements)} objects patched)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--iso", required=True, help="retail ISO (read only)")
    ap.add_argument("--out", help="patched ISO to write (required unless --dry-run)")
    ap.add_argument("--hue", type=float, default=DEFAULT_HUE,
                    help="target hue 0..1 (default 0.92 = pink; try 0.33 green)")
    ap.add_argument("--preview", help="directory for before/after PNG renders")
    ap.add_argument("--dry-run", action="store_true",
                    help="recolor + sweep + preview only, write no ISO")
    args = ap.parse_args()
    if not args.dry_run and not args.out:
        ap.error("--out is required unless --dry-run")
    return run(args.iso, args.out, hue=args.hue, preview=args.preview,
               dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
