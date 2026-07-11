"""textpack.py — export/import the disc's text for translation projects.

Round-trip: ``export`` pulls every text object off the ISO into an editable
UTF-8 tree plus a manifest; a translator edits the files in any editor;
``import`` re-encodes to Shift-JIS, validates every file against its byte
budget, and writes a patched ISO (via repack.py — in place, no rebuild).

What counts as text on this disc (all uncompressed TOC objects):

  *.txt (588)  whole-file Shift-JIS. Scene scripts with the developers'
               comments, U.M.N. event dialogue, misc. Budget = the object's
               sector allocation (its size may grow up to that).
  *.uml (326)  U.M.N. mail: 0x60-byte binary header, then the Shift-JIS
               mail text (space-padded, running to the first NUL byte),
               then a binary tail (a small record + the mail's attached
               JPEG, with Photoshop 8BIM blocks — the u32 at header +0x20
               points into those resources). Only the text region is
               exported; header and tail ride along untouched. Budget =
               the text region's length, which is fixed (imports are
               space-padded back to exactly that length).

Bytes Shift-JIS cannot decode (a handful of mails embed raw JIS symbol
codes the game's font interprets directly — the ★/●/○ family) are exported
as ``⟦XX⟧`` hex markers. The bracket characters are not encodable in
Shift-JIS, so they can never collide with real text; leave the markers in
place when translating and import restores the original bytes. Every
exported file is round-trip self-checked (decode → re-encode must equal
the disc bytes); anything unstable falls back to a verbatim ``.raw`` copy.

The in-game dialogue rendered during events lives in the ``.evt`` Java class
constant pools, NOT in these files — see docs/MODDING.md for the state of
that route before promising a full dialogue translation.

Usage:
    python textpack.py export --iso GAME.iso --out TEXTDIR
    python textpack.py import --iso GAME.iso --text TEXTDIR --out MODDED.iso
    (or: python cli.py text-export / text-import, or the GUI cards)
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path

from chains import CHAINS, ChainReader
from iso9660 import IsoImage
from repack import patch_iso
from toc import SECTOR, parse_toc

MANIFEST = "textpack_manifest.csv"
UML_HEADER = 0x60
ENCODING = "cp932"          # Shift-JIS as the game (and Windows) understand it

# lossless escapes for bytes cp932 can't decode: U+27E6/27E7 are themselves
# unencodable in cp932, so the markers can never be confused with game text
_ESC = "⟦{:02X}⟧"
_ESC_RE = re.compile("⟦([0-9A-Fa-f]{2})⟧")


def decode_sjis(b: bytes) -> str:
    out = []
    pos = 0
    while pos < len(b):
        try:
            out.append(b[pos:].decode(ENCODING))
            break
        except UnicodeDecodeError as e:
            out.append(b[pos:pos + e.start].decode(ENCODING))
            out.append(_ESC.format(b[pos + e.start]))
            pos += e.start + 1
    return "".join(out)


def encode_sjis(s: str) -> bytes:
    parts = _ESC_RE.split(s)     # text, hexbyte, text, hexbyte, ...
    out = bytearray()
    for i, p in enumerate(parts):
        if i % 2:
            out.append(int(p, 16))
        else:
            out += p.encode(ENCODING)
    return bytes(out)


def _entries_with_alloc(iso: IsoImage, chain: int):
    import bisect
    reader = ChainReader(iso, chain)
    entries = parse_toc(reader.toc_bytes(), CHAINS[chain][0])
    starts = sorted(e.byte_offset for e in entries)
    for e in entries:
        i = bisect.bisect_right(starts, e.byte_offset)
        alloc = (starts[i] if i < len(starts) else reader.total) - e.byte_offset
        yield reader, e, alloc


def _fs_path(path: str) -> Path:
    return Path(*path.split("\\"))


def export_text(iso_path: str, out_dir: str) -> int:
    iso = IsoImage(iso_path)
    out = Path(out_dir)
    rows = []
    stats = {"txt": 0, "uml": 0, "raw": 0}
    try:
        for chain in (0, 1):
            for reader, e, alloc in _entries_with_alloc(iso, chain):
                if e.ext not in ("txt", "uml") or e.compressed:
                    continue
                data = b"".join(reader.read_iter(e.byte_offset, e.size))
                if e.ext == "uml":
                    end = data.find(b"\x00", UML_HEADER)
                    end = end if end >= 0 else len(data)
                    body = data[UML_HEADER:end]
                    budget = end - UML_HEADER
                else:
                    body = data
                    budget = alloc
                rel = Path(f"chain{chain}") / _fs_path(e.path)
                text = decode_sjis(body)
                if encode_sjis(text) == body:
                    kind = e.ext
                    rel = rel.with_suffix(rel.suffix + ".utf8.txt")
                    # .uml slots are space-padded to their fixed length;
                    # strip that for editing (import pads back). .txt files
                    # are exported byte-faithful.
                    if e.ext == "uml":
                        text = text.rstrip(" ")
                    payload = text.encode("utf-8")
                else:       # round-trip-unstable mapping: keep bytes verbatim
                    kind = "raw"
                    rel = rel.with_suffix(rel.suffix + ".raw")
                    payload = body
                dest = out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(payload)
                stats[kind] += 1
                rows.append({
                    "chain": chain, "path": e.path, "kind": kind,
                    "budget_bytes": budget, "orig_bytes": len(body),
                    "exported": str(rel),
                })
    finally:
        iso.close()
    out.mkdir(parents=True, exist_ok=True)
    with open(out / MANIFEST, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "chain", "path", "kind", "budget_bytes", "orig_bytes", "exported"])
        w.writeheader()
        w.writerows(rows)
    print(f"exported {stats['txt']} .txt + {stats['uml']} .uml text slots "
          f"({stats['raw']} undecodable, copied raw) -> {out}")
    print(f"budgets per file in {out / MANIFEST}; edit the .utf8.txt files, "
          f"then run text-import")
    return 0


def import_text(iso_path: str, text_dir: str, out_iso: str) -> int:
    iso_path_p, out = Path(iso_path), Path(out_iso)
    tdir = Path(text_dir)
    manifest = tdir / MANIFEST
    if not manifest.exists():
        print(f"error: {manifest} not found — run text-export first",
              file=sys.stderr)
        return 2

    # original objects, for headers/attachments and change detection
    iso = IsoImage(iso_path_p)
    originals: dict[tuple[int, str], bytes] = {}
    try:
        for chain in (0, 1):
            for reader, e, _ in _entries_with_alloc(iso, chain):
                if e.ext in ("txt", "uml") and not e.compressed:
                    originals[(chain, e.path)] = b"".join(
                        reader.read_iter(e.byte_offset, e.size))
    finally:
        iso.close()

    replacements: dict[tuple[int, str], bytes] = {}
    problems = []
    with open(manifest, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            chain, path = int(row["chain"]), row["path"]
            src = tdir / row["exported"]
            if not src.exists():
                continue
            orig = originals.get((chain, path))
            if orig is None:
                problems.append(f"{path}: not on this ISO")
                continue
            budget = int(row["budget_bytes"])
            if row["kind"] == "raw":
                body = src.read_bytes()
            else:
                # newline='' keeps CRLF/LF exactly as saved — the game's line
                # endings must survive editors on any OS
                with open(src, encoding="utf-8-sig", newline="") as sf:
                    text = sf.read()
                try:
                    body = encode_sjis(text)
                except UnicodeEncodeError as ex:
                    problems.append(
                        f"{path}: not representable in Shift-JIS at char "
                        f"{ex.start}: {text[ex.start:ex.start+8]!r}")
                    continue
            # .uml objects are ALWAYS reconstructed around the fixed text
            # region — including 'raw' ones — so header and attached image
            # survive regardless of how the text was exported
            if path.lower().endswith(".uml"):
                end = orig.find(b"\x00", UML_HEADER)
                end = end if end >= 0 else len(orig)
                slot = end - UML_HEADER
                if len(body) > slot:
                    problems.append(
                        f"{path}: text is {len(body)} bytes, slot holds "
                        f"{slot} (over by {len(body) - slot})")
                    continue
                new = (orig[:UML_HEADER] + body.ljust(slot, b" ")
                       + orig[end:])
            else:
                if len(body) > budget:
                    problems.append(
                        f"{path}: {len(body)} bytes exceeds allocation "
                        f"{budget} (over by {len(body) - budget})")
                    continue
                new = body
            if new != orig:
                replacements[(chain, path)] = new

    if problems:
        print(f"{len(problems)} file(s) NOT importable:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("fix these and re-run; nothing was written.", file=sys.stderr)
        return 1
    if not replacements:
        print("no changes detected — nothing to write")
        return 0

    if iso_path_p.resolve() != out.resolve():
        print(f"copying {iso_path_p.name} -> {out} "
              f"({iso_path_p.stat().st_size / 1e9:.1f} GB) ...", flush=True)
        shutil.copyfile(iso_path_p, out)
    patch_iso(out, replacements)
    print(f"done: {out} ({len(replacements)} text objects updated)")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("export")
    pe.add_argument("--iso", required=True)
    pe.add_argument("--out", required=True, help="directory for the text tree")
    pi = sub.add_parser("import")
    pi.add_argument("--iso", required=True, help="retail ISO (read only)")
    pi.add_argument("--text", required=True, help="edited text tree")
    pi.add_argument("--out", required=True, help="patched ISO to write")
    args = ap.parse_args()
    if args.cmd == "export":
        return export_text(args.iso, args.out)
    return import_text(args.iso, args.text, args.out)


if __name__ == "__main__":
    sys.exit(main())
