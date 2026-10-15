"""evttext.py — export/import scene dialogue with **length-changing** edits.

Scene and cutscene dialogue is compiled into the ``.evt`` Java classes as
``CONSTANT_String`` literals (see docs/JAVA.md). Until now only same-length
swaps were possible. This module re-serializes the class files and rebuilds
the FL00 container around them, so a translated line may be any length;
the only ceiling is the object's sector allocation on the disc — and
``repack.patch_iso(..., grow=True)`` lifts even that by relocating the
object (docs/MODDING.md § growth).

The container shift rules (which header fields and table columns move
when a class changes size) follow poketony's ``xeno_evt.py`` from XS1KOR
(https://github.com/poketony/XS1KOR), the Korean localisation of the
Japanese disc, which rebuilt every scene this way; the implementation here
is this kit's own, on top of ``evt.py``'s class walker.

Export format — one UTF-8 text file per container, one line per string:

    # evttext 1  chain=0  path=scene\\ST0210.evt  classes=4
    ## 3 ST0210.class
    @3.17z\\t      "Virtual Tutorial"\\n

``@<class>.<pool-index>`` identifies the string; a trailing ``z`` means the
original ends in the packer's NUL (restored on import — leave the key
alone). The text part uses ``\\n`` ``\\t`` ``\\\\`` escapes and ``⟦XX⟧`` for
any byte EUC-JP cannot show (control codes, raw glyph codes). Translate the
text after the TAB; keep the key; the ``#`` lines are comments.

Usage:
    python evttext.py export --iso GAME.iso --out DIALOGUE/
    python evttext.py import --iso GAME.iso --text DIALOGUE/ --out MODDED.iso [--grow]
    python evttext.py selftest --iso GAME.iso      # identity rebuild == retail, all containers
"""
from __future__ import annotations

import argparse
import shutil
import struct
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from chains import CHAINS, ChainReader
from iso9660 import IsoImage
from repack import patch_iso
from textpack import decode_text, encode_text
from toc import parse_toc

FL00 = b"FL00"
CAFEBABE = b"\xca\xfe\xba\xbe"
TABLE = 0x14
_FIXED = {3: 4, 4: 4, 5: 8, 6: 8, 7: 2, 8: 2, 9: 4, 10: 4, 11: 4, 12: 4}


class EvtTextError(ValueError):
    pass


# --- class files -----------------------------------------------------------

def parse_pool(cls: bytes):
    """-> (entries, rest_offset). entries[i] = (tag, payload bytes) for pool
    index i (1-based; long/double occupy i and i+1, the second is None)."""
    if cls[:4] != CAFEBABE:
        raise EvtTextError("not a class file")
    (count,) = struct.unpack_from(">H", cls, 8)
    p = 10
    entries: List[Optional[Tuple[int, bytes]]] = [None] * count
    i = 1
    while i < count:
        tag = cls[p]
        if tag == 1:
            (n,) = struct.unpack_from(">H", cls, p + 1)
            entries[i] = (1, cls[p + 3:p + 3 + n])
            p += 3 + n
        elif tag in _FIXED:
            n = _FIXED[tag]
            entries[i] = (tag, cls[p + 1:p + 1 + n])
            p += 1 + n
        else:
            raise EvtTextError(f"unknown constant tag {tag} at {p:#x}")
        i += 2 if tag in (5, 6) else 1
    return entries, p


def serialize(cls: bytes, entries, rest_offset: int) -> bytes:
    out = bytearray(cls[:10])
    for e in entries[1:]:
        if e is None:
            continue
        tag, body = e
        out.append(tag)
        if tag == 1:
            out += struct.pack(">H", len(body))
        out += body
    out += cls[rest_offset:]
    return bytes(out)


def literal_indexes(entries) -> List[int]:
    """Pool indexes of Utf8 entries referenced by CONSTANT_String, in pool order."""
    seen, out = set(), []
    for e in entries[1:]:
        if e and e[0] == 8:
            (idx,) = struct.unpack_from(">H", e[1])
            if idx not in seen and 0 < idx < len(entries) and entries[idx] and entries[idx][0] == 1:
                seen.add(idx)
                out.append(idx)
    return sorted(out)


# --- FL00 containers ---------------------------------------------------------

def fl00_entries(data: bytes) -> List[Tuple[int, int, int, int]]:
    """(name_off, name_len, data_off, data_size) per table row."""
    if data[:4] != FL00:
        raise EvtTextError("not an FL00 container")
    (count,) = struct.unpack_from("<H", data, 0x12)
    return [struct.unpack_from("<4I", data, TABLE + i * 16) for i in range(count)]


def fl00_rebuild(data: bytes, patches: Dict[int, bytes]) -> bytes:
    """Replace tabled class payloads (by row index) and shift everything
    that follows: later rows' data/name offsets, the directory-name offset
    (0x0C) and the file size (0x08)."""
    rows = [list(r) for r in fl00_entries(data)]
    first_data = min(r[2] for r in rows) if rows else len(data)
    if any(data[TABLE + len(rows) * 16:first_data]):
        raise EvtTextError("container has data between its table and first "
                           "class (base.evt/system.evt style); not rebuilt")
    out = bytearray(data)
    for ri in sorted(patches, key=lambda i: rows[i][2], reverse=True):
        off, size = rows[ri][2], rows[ri][3]
        new = patches[ri]
        delta = len(new) - size
        out[off:off + size] = new
        rows[ri][3] = len(new)
        if delta:
            for r in rows:
                if r[2] > off:
                    r[2] += delta
                if r[0] > off:
                    r[0] += delta
            (dname,) = struct.unpack_from("<I", out, 0x0C)
            if dname > off:
                struct.pack_into("<I", out, 0x0C, dname + delta)
    for i, r in enumerate(rows):
        struct.pack_into("<4I", out, TABLE + i * 16, *r)
    struct.pack_into("<I", out, 0x08, len(out))
    return bytes(out)


def strings_of(data: bytes) -> List[Tuple[int, int, bytes, str]]:
    """(row index, pool index, bytes, class name) for every literal in a container."""
    out = []
    for ri, (no, nl, do, ds) in enumerate(fl00_entries(data)):
        cls = data[do:do + ds]
        if ds <= 24 or cls[:4] != CAFEBABE:
            continue
        try:
            entries, _ = parse_pool(cls)
        except EvtTextError:
            continue
        name = data[no:no + nl].rstrip(b"\x00").decode("ascii", "replace")
        for pi in literal_indexes(entries):
            out.append((ri, pi, entries[pi][1], name))
    return out


def rebuild(data: bytes, new_strings: Dict[Tuple[int, int], bytes]) -> bytes:
    """Container with literals replaced; keys are (row index, pool index)."""
    by_row: Dict[int, Dict[int, bytes]] = {}
    for (ri, pi), b in new_strings.items():
        by_row.setdefault(ri, {})[pi] = b
    rows = fl00_entries(data)
    patches = {}
    for ri, repl in by_row.items():
        no, nl, do, ds = rows[ri]
        cls = data[do:do + ds]
        entries, rest = parse_pool(cls)
        for pi, b in repl.items():
            if not (0 < pi < len(entries)) or not entries[pi] or entries[pi][0] != 1:
                raise EvtTextError(f"row {ri} pool {pi} is not a Utf8 entry")
            if len(b) > 0xFFFF:
                raise EvtTextError(f"row {ri} pool {pi}: string longer than 65535 bytes")
            entries[pi] = (1, b)
        patches[ri] = serialize(cls, entries, rest)
    return fl00_rebuild(data, patches)


# --- text files --------------------------------------------------------------

def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}.get(n, "\\" + n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def container_to_text(chain: int, path: str, data: bytes) -> str:
    lines = [f"# evttext 1  chain={chain}  path={path}  size={len(data)}"]
    last = None
    for ri, pi, raw, name in strings_of(data):
        if ri != last:
            lines.append(f"## {ri} {name}")
            last = ri
        z = raw.endswith(b"\x00")
        body = raw[:-1] if z else raw
        lines.append(f"@{ri}.{pi}{'z' if z else ''}\t{_escape(decode_text(body))}")
    return "\n".join(lines) + "\n"


def text_to_strings(text: str) -> Dict[Tuple[int, int], bytes]:
    out = {}
    for ln, line in enumerate(text.splitlines(), 1):
        if not line.startswith("@"):
            continue
        key, _, body = line.partition("\t")
        key = key[1:]
        z = key.endswith("z")
        if z:
            key = key[:-1]
        try:
            ri, pi = (int(x) for x in key.split("."))
        except ValueError:
            raise EvtTextError(f"line {ln}: bad key {line[:20]!r}")
        raw = encode_text(_unescape(body))
        out[(ri, pi)] = raw + (b"\x00" if z else b"")
    return out


# --- disc walk ---------------------------------------------------------------

def _fs(path: str) -> Path:
    return Path(*path.split("\\"))


def iter_containers(iso: IsoImage) -> Iterable[Tuple[int, str, bytes, int]]:
    """(chain, path, bytes, allocation) for every .evt on the disc."""
    import bisect
    for chain in sorted(CHAINS):
        try:
            reader = ChainReader(iso, chain)
        except FileNotFoundError:
            continue
        entries = parse_toc(reader.toc_bytes(), CHAINS[chain][0])
        starts = sorted(e.byte_offset for e in entries)
        for e in entries:
            if e.ext != "evt" or e.compressed:
                continue
            i = bisect.bisect_right(starts, e.byte_offset)
            alloc = (starts[i] if i < len(starts) else reader.total) - e.byte_offset
            yield chain, e.path, reader.read(e.byte_offset, e.size), alloc


def export_dialogue(iso_path: str, out_dir: str) -> int:
    iso = IsoImage(iso_path)
    out = Path(out_dir)
    n_files = n_strings = 0
    try:
        for chain, path, data, alloc in iter_containers(iso):
            text = container_to_text(chain, path, data)
            count = sum(1 for l in text.splitlines() if l.startswith("@"))
            if not count:
                continue
            dest = out / f"chain{chain}" / _fs(path + ".txt")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text.replace(f"size={len(data)}",
                                         f"size={len(data)} budget={alloc}"),
                            encoding="utf-8")
            n_files += 1
            n_strings += count
    finally:
        iso.close()
    print(f"exported {n_strings} string literals from {n_files} containers -> {out}")
    print("edit the text after each TAB; keys and # lines stay; then run evt-import")
    return 0


def import_dialogue(iso_path: str, text_dir: str, out_iso: str, grow: bool = False) -> int:
    iso = IsoImage(iso_path)
    tdir = Path(text_dir)
    replacements: Dict[Tuple[int, str], bytes] = {}
    problems: List[str] = []
    try:
        for chain, path, data, alloc in iter_containers(iso):
            src = tdir / f"chain{chain}" / _fs(path + ".txt")
            if not src.exists():
                continue
            try:
                wanted = text_to_strings(src.read_text(encoding="utf-8-sig"))
                current = {(ri, pi): raw for ri, pi, raw, _ in strings_of(data)}
                changed = {k: v for k, v in wanted.items() if current.get(k) != v}
                missing = [k for k in wanted if k not in current]
                if missing:
                    problems.append(f"{src}: unknown keys {missing[:3]}")
                    continue
                if not changed:
                    continue
                new = rebuild(data, changed)
            except (EvtTextError, UnicodeEncodeError) as ex:
                problems.append(f"{src}: {ex}")
                continue
            if len(new) > alloc and not grow:
                problems.append(f"{path}: {len(new)} bytes exceeds allocation {alloc} "
                                f"(over by {len(new) - alloc}; use --grow to relocate)")
                continue
            replacements[(chain, path)] = new
    finally:
        iso.close()
    if problems:
        print(f"{len(problems)} container(s) NOT importable:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("fix these and re-run; nothing was written.", file=sys.stderr)
        return 1
    if not replacements:
        print("no changes detected — nothing to write")
        return 0
    src, dst = Path(iso_path), Path(out_iso)
    if src.resolve() != dst.resolve():
        print(f"copying {src.name} -> {dst} ({src.stat().st_size / 1e9:.1f} GB) ...", flush=True)
        shutil.copyfile(src, dst)
    patch_iso(dst, replacements, grow=grow)
    print(f"done: {dst} ({len(replacements)} containers updated)")
    return 0


def selftest(iso_path: str) -> int:
    """Identity rebuild of every container must reproduce the retail bytes."""
    iso = IsoImage(iso_path)
    ok = bad = skipped = 0
    try:
        for chain, path, data, _ in iter_containers(iso):
            strs = strings_of(data)
            if not strs:
                skipped += 1
                continue
            try:
                out = rebuild(data, {(ri, pi): raw for ri, pi, raw, _ in strs})
                # and through the text format
                out2 = rebuild(data, text_to_strings(container_to_text(chain, path, data)))
            except EvtTextError as ex:
                print(f"  skipped {path}: {ex}")
                skipped += 1
                continue
            if out == data and out2 == data:
                ok += 1
            else:
                bad += 1
                print(f"  MISMATCH {path} ({len(out)} vs {len(data)})")
    finally:
        iso.close()
    print(f"identity rebuild: {ok} identical, {bad} mismatched, {skipped} skipped")
    return 0 if not bad else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("export")
    pe.add_argument("--iso", required=True)
    pe.add_argument("--out", required=True)
    pi = sub.add_parser("import")
    pi.add_argument("--iso", required=True)
    pi.add_argument("--text", required=True)
    pi.add_argument("--out", required=True)
    pi.add_argument("--grow", action="store_true",
                    help="relocate containers that outgrow their allocation")
    ps = sub.add_parser("selftest")
    ps.add_argument("--iso", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "export":
        return export_dialogue(a.iso, a.out)
    if a.cmd == "import":
        return import_dialogue(a.iso, a.text, a.out, a.grow)
    return selftest(a.iso)


if __name__ == "__main__":
    sys.exit(main())
