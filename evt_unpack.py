"""evt_unpack.py — unpack Xenosaga Episode I FL00 event containers.

XS1's `.evt` files are FL00 containers of JDK 1.1 Java class files (format
45.3) — the game's cutscene/field scripting. See out/TOOLCHAIN.md and
out/DECOMP-PLAN.md §1 in the disc extraction directory for the format notes.

FL00 layout (little-endian):
    0x00  "FL00"
    0x04  u16 ?, u16 ?
    0x08  u32 file size
    0x0c  u32 offset of directory-name string (package path, e.g. "xeno/plan")
    0x10  u16 directory-name length
    0x12  u16 entry count
    0x14  entry table: (u32 name_off, u32 name_len, u32 data_off, u32 data_size)
    ...   class payloads (some untabled ones may follow the tabled ones)
    tail  NUL-separated name string table

Quirks handled here (verified against base.evt / system.evt / scene/ST*.evt):
  * Tiny 24-byte "stub" entries: constant-holder classes (ChrNo, MapNo, …)
    whose bodies the VM resolves elsewhere. Extracted raw, flagged in the
    manifest, skipped for normalization.
  * Untabled classes: base.evt appends 4 full classes (xeno/Enepc, MAPUnit,
    Monitor, Uwamono) after the tabled payloads with no entry-table row.
    Found by CAFEBABE scan of gaps, named from their this_class.
  * NUL-terminated UTF8: every CONSTANT_Utf8 in shipped classes has a
    trailing NUL included in its length — invalid modified-UTF-8 that stock
    JVM tools reject. `--normalize` rewrites the constant pool stripping one
    trailing NUL per entry (the rest of the file is pool-index-based and is
    copied verbatim).

Usage:
    python3 evt_unpack.py --dump <...>/out/dump --out <...>/out/java [--no-normalize]

Outputs:
    out/java/classes/<container>/<this_class>.class      raw payloads
    out/java/classes_norm/<container>/<this_class>.class normalized (loadable)
    out/java/manifest.csv                                 one row per payload
    out/java/anomalies.txt                                parse problems
"""
from __future__ import annotations

import argparse
import csv
import struct
from pathlib import Path

MAGIC_FL00 = b"FL00"
MAGIC_CLASS = b"\xca\xfe\xba\xbe"
STUB_MAX = 100  # payloads smaller than this can't be real classes


# ---------------------------------------------------------------- classfile

def scan_pool(b: bytes):
    """Walk the constant pool. Returns (pool_end_off, utf8_spans, pool)
    where utf8_spans = [(len_field_off, str_off, str_len)], pool = {idx: bytes|int-ref}."""
    cpc = struct.unpack_from(">H", b, 8)[0]
    pool: dict[int, object] = {}
    spans = []
    i, idx = 10, 1
    while idx < cpc:
        tag = b[i]
        if tag == 1:  # Utf8
            ln = struct.unpack_from(">H", b, i + 1)[0]
            spans.append((i + 1, i + 3, ln))
            pool[idx] = b[i + 3 : i + 3 + ln]
            i += 3 + ln
        elif tag in (7, 8):  # Class, String -> utf8 index
            pool[idx] = struct.unpack_from(">H", b, i + 1)[0]
            i += 3
        elif tag in (3, 4, 9, 10, 11, 12):  # int/float/refs/nameandtype
            i += 5
        elif tag in (5, 6):  # long/double take two slots
            i += 9
            idx += 1
        else:
            raise ValueError(f"bad constant tag {tag} at 0x{i:x}")
        idx += 1
    return i, spans, pool


def this_class_name(b: bytes) -> str:
    pool_end, _spans, pool = scan_pool(b)
    this_idx = struct.unpack_from(">H", b, pool_end + 2)[0]
    utf8 = pool[pool[this_idx]]
    assert isinstance(utf8, bytes)
    return utf8.rstrip(b"\0").decode()


def class_length(b: bytes) -> int:
    """Exact byte length of the class file at the start of b (untabled
    payloads carry alignment padding that strict parsers reject)."""
    i, _spans, _pool = scan_pool(b)
    i += 6  # access_flags, this_class, super_class

    def attrs(j: int) -> int:
        (n,) = struct.unpack_from(">H", b, j)
        j += 2
        for _ in range(n):
            (ln,) = struct.unpack_from(">I", b, j + 2)
            j += 6 + ln
        return j

    (n_if,) = struct.unpack_from(">H", b, i)
    i += 2 + 2 * n_if
    for _section in range(2):  # fields, then methods
        (n,) = struct.unpack_from(">H", b, i)
        i += 2
        for _ in range(n):
            i = attrs(i + 6)
    return attrs(i)


def normalize(b: bytes) -> bytes:
    """Strip one trailing NUL from every Utf8 entry (fixing its length).
    Everything outside the constant pool is index-based -> copied verbatim."""
    pool_end, spans, _pool = scan_pool(b)
    out = bytearray(b[:10])
    prev = 10
    for len_off, str_off, ln in spans:
        out += b[prev:len_off]
        s = b[str_off : str_off + ln]
        if s.endswith(b"\0"):
            s = s[:-1]
        out += struct.pack(">H", len(s)) + s
        prev = str_off + ln
    out += b[prev:]
    return bytes(out)


# --------------------------------------------------------------------- FL00

def parse_fl00(data: bytes):
    """Returns (dirname, tabled=[(name, off, size)], untabled=[off, ...])."""
    if data[:4] != MAGIC_FL00:
        raise ValueError("no FL00 magic")
    dir_off = struct.unpack_from("<I", data, 0x0C)[0]
    dir_len, count = struct.unpack_from("<HH", data, 0x10)
    dirname = data[dir_off : dir_off + dir_len].decode()
    tabled = []
    p = 0x14
    for _ in range(count):
        name_off, name_len, data_off, data_size = struct.unpack_from("<4I", data, p)
        tabled.append((data[name_off : name_off + name_len].decode(), data_off, data_size))
        p += 16
    covered = {off for _, off, _ in tabled}
    untabled = []
    j = -1
    while True:
        j = data.find(MAGIC_CLASS, j + 1)
        if j < 0:
            break
        if j not in covered and not any(o < j < o + s for _, o, s in tabled):
            untabled.append(j)
    return dirname, tabled, untabled


def untabled_end(data: bytes, offs: list[int], dir_off: int) -> list[tuple[int, int]]:
    """Untabled classes run back-to-back; each ends where the next begins
    (or at the name table)."""
    bounds = []
    for k, off in enumerate(offs):
        end = offs[k + 1] if k + 1 < len(offs) else dir_off
        bounds.append((off, end))
    return bounds


# --------------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump", required=True, type=Path, help="out/dump directory")
    ap.add_argument("--out", required=True, type=Path, help="output dir (out/java)")
    ap.add_argument("--no-normalize", action="store_true")
    args = ap.parse_args()

    evts = sorted(args.dump.rglob("*.evt"))
    raw_root = args.out / "classes"
    norm_root = args.out / "classes_norm"
    anomalies = []
    rows = []
    n_classes = n_stubs = n_untabled = 0

    for evt in evts:
        data = evt.read_bytes()
        container = evt.relative_to(args.dump).as_posix()
        stem = container.replace("/", "_").removesuffix(".evt")
        try:
            dirname, tabled, untabled = parse_fl00(data)
        except ValueError as e:
            anomalies.append(f"{container}: {e}")
            continue

        payloads = []  # (suggested_name, bytes, tabled?, stub?)
        for name, off, size in tabled:
            payloads.append((name, data[off : off + size], True))
        dir_off = struct.unpack_from("<I", data, 0x0C)[0]
        for off, end in untabled_end(data, untabled, dir_off):
            blob = data[off:end]
            if len(blob) >= STUB_MAX:
                try:
                    blob = blob[: class_length(blob)]
                except Exception as e:
                    anomalies.append(f"{container}: untabled@0x{off:x} length parse: {e}")
            payloads.append((None, blob, False))
            n_untabled += 1

        for name, blob, is_tabled in payloads:
            stub = len(blob) < STUB_MAX
            cls = None
            if not stub:
                try:
                    cls = this_class_name(blob)
                except Exception as e:
                    anomalies.append(f"{container}:{name}: classfile parse: {e}")
            # prefer real class name for the path; fall back to table name
            if cls:
                rel = cls + ".class"
            elif name:
                rel = (dirname + "/" if dirname not in (".", "") else "") + name
            else:
                anomalies.append(f"{container}: unnamed unparseable payload")
                continue
            dest = raw_root / stem / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            ok_norm = ""
            if not stub and not args.no_normalize:
                try:
                    nb = normalize(blob)
                    this_class_name(nb)  # re-parse as sanity check
                    nd = norm_root / stem / rel
                    nd.parent.mkdir(parents=True, exist_ok=True)
                    nd.write_bytes(nb)
                    ok_norm = "yes"
                except Exception as e:
                    anomalies.append(f"{container}:{rel}: normalize: {e}")
                    ok_norm = "FAIL"
            rows.append([container, rel, len(blob),
                         "tabled" if is_tabled else "untabled",
                         "stub" if stub else "class", ok_norm])
            n_stubs += stub
            n_classes += not stub

    args.out.mkdir(parents=True, exist_ok=True)
    with open(args.out / "manifest.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["container", "class_path", "size", "table", "kind", "normalized"])
        w.writerows(rows)
    (args.out / "anomalies.txt").write_text("\n".join(anomalies) + "\n" if anomalies else "")
    print(f"{len(evts)} containers -> {n_classes} classes ({n_untabled} untabled) "
          f"+ {n_stubs} stubs; {len(anomalies)} anomalies")


if __name__ == "__main__":
    main()
