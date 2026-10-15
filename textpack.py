"""textpack.py — export/import the disc's text for translation projects.

Round-trip: ``export`` pulls every text object off the ISO into an editable
UTF-8 tree plus a manifest; a translator edits the files in any editor;
``import`` re-encodes to EUC-JP, validates every file against its byte
budget, and writes a patched ISO (via repack.py — in place, no rebuild).

What counts as text on this disc (all uncompressed TOC objects):

  *.txt (588)  whole-file EUC-JP. Scene scripts with the developers'
               comments, U.M.N. event dialogue, misc. Budget = the object's
               sector allocation (its size may grow up to that).
  *.uml (326)  U.M.N. mail: 0x60-byte binary header, then the EUC-JP mail
               text running to the first NUL byte, then the attached JPEG
               (16-byte aligned; the u32 at header +0x20 points at it and
               the (pointer, position) pairs from +0x28 locate further
               images). Only the text is exported; the header and the
               images ride along. The text may now **grow**: the images are
               shifted and every pointer fixed up (the layout rule is the
               one poketony established for XS1KOR's ``uml_tool.py``,
               https://github.com/poketony/XS1KOR). Budget = what fits in
               the object's allocation with the images in tow.

The game's text is EUC-JP (the font is EUC-indexed and the mails' bullet
glyphs are EUC codes); earlier kit versions decoded as Shift-JIS, which
is byte-transparent for ASCII but showed the wrong symbols for the few
Japanese codes. Bytes EUC-JP cannot decode (control codes with binary
operands, raw glyph codes such as the ones a non-Latin translation emits)
are exported as ``⟦XX⟧`` hex markers. The bracket characters are not
encodable in EUC-JP, so they can never collide with real text; leave the
markers in place when translating and import restores the original bytes.
Every exported file is round-trip self-checked (decode → re-encode must
equal the disc bytes); anything unstable falls back to a verbatim ``.raw``
copy.

The in-game dialogue rendered during events lives in the ``.evt`` Java class
constant pools, NOT in these files — ``evttext.py`` handles those.

Usage:
    python textpack.py export --iso GAME.iso --out TEXTDIR
    python textpack.py import --iso GAME.iso --text TEXTDIR --out MODDED.iso [--grow]
    (or: python cli.py text-export / text-import, or the GUI cards)
"""
from __future__ import annotations

import argparse
import csv
import re
import shutil
import struct
import sys
from pathlib import Path

from chains import CHAINS, ChainReader
from iso9660 import IsoImage
from repack import patch_iso
from toc import SECTOR, parse_toc

MANIFEST = "textpack_manifest.csv"
UML_HEADER = 0x60
UML_ALIGN = 16
ENCODING = "euc_jp"         # what the engine's font indexing expects

# lossless escapes for bytes the codec can't decode: U+27E6/27E7 are
# themselves unencodable in EUC-JP, so the markers can never be confused
# with game text
_ESC = "⟦{:02X}⟧"
_ESC_RE = re.compile("⟦([0-9A-Fa-f]{2})⟧")


def decode_text(b: bytes) -> str:
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


def encode_text(s: str) -> bytes:
    parts = _ESC_RE.split(s)     # text, hexbyte, text, hexbyte, ...
    out = bytearray()
    for i, p in enumerate(parts):
        if i % 2:
            out.append(int(p, 16))
        else:
            out += p.encode(ENCODING)
    return bytes(out)


decode_sjis = decode_text     # names kept for scripts written against v1.1
encode_sjis = encode_text


def _entries_with_alloc(iso: IsoImage, chain: int):
    import bisect
    reader = ChainReader(iso, chain)
    entries = parse_toc(reader.toc_bytes(), CHAINS[chain][0])
    starts = sorted(e.byte_offset for e in entries)
    for e in entries:
        i = bisect.bisect_right(starts, e.byte_offset)
        alloc = (starts[i] if i < len(starts) else reader.total) - e.byte_offset
        yield reader, e, alloc


def _chains(iso: IsoImage):
    for chain, names in CHAINS.items():
        if all(n in iso.files for n in names):
            yield chain


def _fs_path(path: str) -> Path:
    return Path(*path.split("\\"))


# --- .uml layout -------------------------------------------------------------

def uml_split(data: bytes):
    """-> (text bytes, image block start). The text ends at the first NUL."""
    end = data.find(b"\x00", UML_HEADER)
    end = end if end >= 0 else len(data)
    img = struct.unpack_from("<I", data, 0x20)[0]
    if not (end < img <= len(data)):
        img = len(data)
    return data[UML_HEADER:end], img


def uml_text_budget(data: bytes, alloc: int) -> int:
    """Longest text (bytes, without NUL) that keeps the object within ``alloc``."""
    _, img = uml_split(data)
    tail = len(data) - img
    room = (alloc - tail) // UML_ALIGN * UML_ALIGN
    return max(0, room - UML_HEADER - 1)


def uml_rebuild(data: bytes, text: bytes) -> bytes:
    """Mail object with its text replaced; images shifted and pointers fixed
    when the text grows past the original image offset."""
    old_text, img = uml_split(data)
    slot = img - UML_HEADER
    end = UML_HEADER + len(old_text)          # retail NUL position
    if len(text) <= len(old_text):
        # retail layout untouched: the slot is space-padded, and whatever
        # follows the NUL (a stray control code, zero padding) is kept
        return data[:UML_HEADER] + text.ljust(len(old_text), b" ") + data[end:]
    if len(text) < slot:
        return (data[:UML_HEADER] + text + b"\x00" + bytes(slot - len(text) - 1)
                + data[img:])
    new_img = (UML_HEADER + len(text) + 1 + UML_ALIGN - 1) // UML_ALIGN * UML_ALIGN
    delta = new_img - img
    hdr = bytearray(data[:UML_HEADER])
    struct.pack_into("<I", hdr, 0x20, new_img)
    for off in range(0x28, UML_HEADER, 8):
        ptr = struct.unpack_from("<I", hdr, off)[0]
        if img <= ptr < len(data):
            struct.pack_into("<I", hdr, off, ptr + delta)
    region = text + bytes(new_img - UML_HEADER - len(text))
    return bytes(hdr) + region + data[img:]


# --- export / import -----------------------------------------------------------

def export_text(iso_path: str, out_dir: str) -> int:
    iso = IsoImage(iso_path)
    out = Path(out_dir)
    rows = []
    stats = {"txt": 0, "uml": 0, "raw": 0}
    try:
        for chain in _chains(iso):
            for reader, e, alloc in _entries_with_alloc(iso, chain):
                if e.ext not in ("txt", "uml") or e.compressed:
                    continue
                data = b"".join(reader.read_iter(e.byte_offset, e.size))
                if e.ext == "uml":
                    body, _ = uml_split(data)
                    budget = uml_text_budget(data, alloc)
                else:
                    body = data
                    budget = alloc
                rel = Path(f"chain{chain}") / _fs_path(e.path)
                text = decode_text(body)
                if encode_text(text) == body:
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


def import_text(iso_path: str, text_dir: str, out_iso: str, grow: bool = False) -> int:
    iso_path_p, out = Path(iso_path), Path(out_iso)
    tdir = Path(text_dir)
    manifest = tdir / MANIFEST
    if not manifest.exists():
        print(f"error: {manifest} not found — run text-export first",
              file=sys.stderr)
        return 2

    # original objects, for headers/attachments and change detection
    iso = IsoImage(iso_path_p)
    originals: dict[tuple[int, str], tuple[bytes, int]] = {}
    try:
        for chain in _chains(iso):
            for reader, e, alloc in _entries_with_alloc(iso, chain):
                if e.ext in ("txt", "uml") and not e.compressed:
                    originals[(chain, e.path)] = (
                        b"".join(reader.read_iter(e.byte_offset, e.size)), alloc)
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
            got = originals.get((chain, path))
            if got is None:
                problems.append(f"{path}: not on this ISO")
                continue
            orig, alloc = got
            if row["kind"] == "raw":
                body = src.read_bytes()
            else:
                # newline='' keeps CRLF/LF exactly as saved — the game's line
                # endings must survive editors on any OS
                with open(src, encoding="utf-8-sig", newline="") as sf:
                    text = sf.read()
                try:
                    body = encode_text(text)
                except UnicodeEncodeError as ex:
                    bad = ex.object[ex.start:ex.start + 1]
                    problems.append(
                        f"{path}: {bad!r} (U+{ord(bad):04X}) is not representable "
                        f"in EUC-JP, near {ex.object[max(0, ex.start-6):ex.start+6]!r} "
                        f"(non-Latin scripts go through langpack first)")
                    continue
            # .uml objects are ALWAYS reconstructed around the text region —
            # including 'raw' ones — so header and attached images survive
            # regardless of how the text was exported
            if path.lower().endswith(".uml"):
                if b"\x00" in body:
                    problems.append(f"{path}: text contains a NUL byte")
                    continue
                new = uml_rebuild(orig, body)
                if len(new) > alloc and not grow:
                    problems.append(
                        f"{path}: text is {len(body)} bytes, {uml_text_budget(orig, alloc)} "
                        f"fit (over by {len(body) - uml_text_budget(orig, alloc)}; "
                        f"use --grow to relocate)")
                    continue
            else:
                if len(body) > alloc and not grow:
                    problems.append(
                        f"{path}: {len(body)} bytes exceeds allocation "
                        f"{alloc} (over by {len(body) - alloc}; use --grow to relocate)")
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
    patch_iso(out, replacements, grow=grow)
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
    pi.add_argument("--grow", action="store_true",
                    help="relocate objects that outgrow their allocation")
    args = ap.parse_args()
    if args.cmd == "export":
        return export_text(args.iso, args.out)
    return import_text(args.iso, args.text, args.out, args.grow)


if __name__ == "__main__":
    sys.exit(main())
