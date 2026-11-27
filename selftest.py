#!/usr/bin/env python3
"""selftest.py — the kit's own checks, run by ``python cli.py self-test``.

Two layers, the same shape as the Episode II and III kits:

* **Without a disc** every module must import (a missing requirement is a
  failure, never a skip), the codecs must round-trip made-up data, and the
  translation editor's server must answer every route on a real socket with
  its built-in fake catalog.
* **With a disc** (``--iso``, or the ``XS1_ISO`` environment variable) the
  claims the kit's write path rests on are re-proved on that image, read-only:
  the tables of contents parse, the ARX compressor reproduces the disc's own
  compressed objects byte for byte, the fonts and the dialogue containers
  rebuild to their retail bytes, and the editor's catalog loads from it.
  ``--full`` adds the checks that build patched copy-on-write clones of the
  image in a scratch folder (minutes, and the clones need a volume that can
  clone files: APFS, or enough free space for full copies).

Nothing here writes to the image it is given.
"""
from __future__ import annotations

import importlib
import os
import random
import struct
import sys
import time
import traceback
from typing import Callable, List, Optional, Tuple

# Every module of the kit. All of them must import: the app bundle carries the
# same list, so a module that cannot import here cannot run there either.
MODULES = (
    "iso9660", "chains", "toc", "arx", "carve", "evt", "evt_unpack", "class_map",
    "repack", "textpack", "evttext", "ovltext", "font", "glyphs", "langpack",
    "statfont", "subs", "ssd", "ssd_render", "lex", "browse", "pinkhair",
    "pseudoloc", "dump_symbols", "region", "fcv",
    "txcodec", "txlangs", "txtarget", "txsource", "txspeakers", "txbackup",
    "txproject", "txcatalog", "txfam_umn", "txfam_evt", "txfam_ovl", "txfam_fix",
    "txbuild", "txeditor_page", "txserver", "gui",
)

# The packages in requirements.txt, by import name. All required.
REQUIREMENTS = (("numpy", "music rendering (ssd_render.py)"),
                ("fontTools", "glyph outlines for new scripts (glyphs.py)"),
                ("uharfbuzz", "text shaping for new scripts (glyphs.py, langpack.py)"))

_REQ_HINT = "install the requirements: python -m pip install -r requirements.txt"


class _Run:
    def __init__(self) -> None:
        self.failed: List[str] = []
        self.count = 0

    def check(self, cond, what: str) -> bool:
        self.count += 1
        print(("ok    " if cond else "FAIL  ") + what, flush=True)
        if not cond:
            self.failed.append(what)
        return bool(cond)

    def step(self, what: str, fn: Callable[[], object]) -> None:
        """Run ``fn``; it passes when it returns a falsy value (the modules'
        own self-tests return 0) and fails on a truthy one or an exception."""
        self.count += 1
        t0 = time.time()
        try:
            rc = fn()
        except SystemExit as e:                     # argparse or sys.exit inside a test
            rc = e.code
        except Exception as e:                      # a crash is a failure, not a skip
            traceback.print_exc()
            rc = f"{type(e).__name__}: {e}"
        dt = time.time() - t0
        if rc:
            print(f"FAIL  {what} ({rc}; {dt:.1f} s)", flush=True)
            self.failed.append(what)
        else:
            print(f"ok    {what} ({dt:.1f} s)", flush=True)


def _quiet(fn: Callable[[], object]) -> Callable[[], object]:
    """Run a module's chatty self-test with its output held back unless it
    fails, so the summary stays one line per check."""
    def run():
        import contextlib
        import io
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                rc = fn()
        except BaseException:
            sys.stdout.write(buf.getvalue())
            raise
        if rc:
            sys.stdout.write(buf.getvalue())
        return rc
    return run


# ---------------------------------------------------------------------------
# without a disc
# ---------------------------------------------------------------------------

def _imports(r: _Run) -> None:
    for mod, why in REQUIREMENTS:
        try:
            m = importlib.import_module(mod)
            r.check(True, f"requirement {mod} {getattr(m, '__version__', getattr(m, 'version', ''))}".rstrip())
        except ImportError as e:
            r.check(False, f"requirement {mod} ({why}) does not import: {e}; {_REQ_HINT}")
    bad = []
    # in the app bundle the window is its own program: xeno-cli does not carry gui
    names = [n for n in MODULES if not (n == "gui" and getattr(sys, "frozen", False))]
    for name in names:
        try:
            importlib.import_module(name)
        except Exception as e:                      # ImportError, or an error at import time
            bad.append(f"{name} ({type(e).__name__}: {e})")
    r.check(not bad, f"all {len(names)} modules import" + (": " + "; ".join(bad) if bad else ""))


def _arx_synthetic(r: _Run) -> None:
    import arx
    rng = random.Random(1)
    cases = {
        "empty": b"",
        "one byte": b"\x7f",
        "a run": b"\x00" * 5000,
        "text": (b"KOS-MOS, Shion, chaos, Jr., MOMO, Ziggy. " * 200),
        "noise": bytes(rng.randrange(256) for _ in range(4096)),
        "mixed": b"".join(bytes([rng.randrange(4)]) * rng.randrange(1, 40) for _ in range(600)),
    }
    bad = []
    for name, payload in cases.items():
        try:
            packed = arx.compress(payload)
            if not arx.is_arx(packed) or arx.decompress(packed) != payload:
                bad.append(name)
        except Exception as e:
            bad.append(f"{name} ({type(e).__name__}: {e})")
    r.check(not bad, f"arx: compress then decompress returns the input ({len(cases)} payloads)"
            + (": " + ", ".join(bad) if bad else ""))


def _text_synthetic(r: _Run) -> None:
    import textpack
    samples = ["Shion Uzuki", "U.M.N. Mail\nline two", "グノーシス", "KOS-MOS『相転移砲』", "100% ok?"]
    bad = []
    for s in samples:
        try:
            if textpack.decode_text(textpack.encode_text(s)) != s:
                bad.append(repr(s))
        except Exception as e:
            bad.append(f"{s!r} ({type(e).__name__}: {e})")
    r.check(not bad, f"textpack: text encodes and decodes back ({len(samples)} samples)"
            + (": " + ", ".join(bad) if bad else ""))


def _font_synthetic(r: _Run) -> None:
    import font
    rng = random.Random(2)
    # the sheet codec: a PNG written by the kit reads back to the same grey levels
    w, h = 64, 48
    sheet = bytearray(rng.randrange(4) * 85 for _ in range(w * h))
    try:
        png = font.sheet_to_png(sheet, w, h)
        rw, rh, grey = font.png_to_gray(png)
        r.check((rw, rh) == (w, h) and bytes(grey) == bytes(sheet),
                "font: a sheet written as PNG reads back to the same pixels")
    except Exception as e:
        r.check(False, f"font: PNG sheet round trip ({type(e).__name__}: {e})")


def _music_synthetic(r: _Run) -> None:
    import ssd
    ok = (ssd._vlq(0) == b"\x00" and ssd._vlq(0x7F) == b"\x7f" and ssd._vlq(0x80) == b"\x81\x00"
          and ssd._vlq(0x3FFF) == b"\xff\x7f" and ssd._vlq(0x200000) == b"\x81\x80\x80\x00")
    r.check(ok, "ssd: MIDI variable-length numbers")
    r.check(ssd.parse_bank(b"", "x") is None and ssd.parse_sequence(b"", "x") is None,
            "ssd: an empty file is not a bank or a sequence")


def _region_synthetic(r: _Run) -> None:
    import region
    r.step("region: build identification", _quiet(lambda: region.self_test(None)))


def _motion_synthetic(r: _Run) -> None:
    import fcv
    r.step("motion: hand-made pack, curves, a solved chain, glTF animation (fcv)",
           _quiet(lambda: fcv.selftest(None)))


def _editor_synthetic(r: _Run) -> None:
    import txbackup
    import txcatalog
    import txproject
    import txserver
    r.step("translation editor: backups (txbackup)", _quiet(txbackup.selftest))
    r.step("translation editor: project files (txproject)", _quiet(lambda: txproject.selftest(None)))
    r.step("translation editor: catalog shape (txcatalog)", _quiet(lambda: txcatalog.selftest(None)))
    r.step("translation editor: every server route on a socket, fake catalog (txserver)",
           _quiet(lambda: txserver.selftest(None)))


# ---------------------------------------------------------------------------
# with a disc (read-only)
# ---------------------------------------------------------------------------

def _disc(r: _Run, iso_path: str, full: bool, scratch: Optional[str]) -> None:
    import arx
    import evttext
    import font
    from chains import CHAINS, ChainReader, available_chains
    from iso9660 import IsoImage
    from toc import parse_toc

    import region
    iso = IsoImage(iso_path)
    boot = iso.boot_elf_name()
    r.check(bool(boot), f"disc: boot executable {boot} ({region.info(iso_path).label})")
    chains = available_chains(iso)
    r.check(0 in chains, f"disc: data chains present {chains}")
    packed: List[Tuple[int, object]] = []
    total = 0
    for chain in chains:
        reader = ChainReader(iso, chain)
        try:
            entries = parse_toc(reader.toc_bytes(), CHAINS[chain][0])
        except Exception as e:
            r.check(False, f"disc: chain {chain} table of contents ({type(e).__name__}: {e})")
            continue
        total += len(entries)
        seen = len({e.path.lower() for e in entries})
        r.check(len(entries) > 0 and seen == len(entries),
                f"disc: chain {chain} table of contents, {len(entries)} objects, no path twice")
        packed += [(chain, e) for e in entries if e.compressed]

    # the compressor is a clone of the game's: recompressing what the disc
    # stores must give the disc's bytes back
    rng = random.Random(3)
    sample = packed if full else rng.sample(packed, min(60, len(packed)))
    readers = {c: ChainReader(iso, c) for c in chains}
    bad: List[str] = []
    ties: List[str] = []
    nbytes = 0
    for chain, e in sample:
        raw = readers[chain].read(e.byte_offset, e.size)
        nbytes += len(raw)
        try:
            plain = arx.decompress(raw)
            again = arx.compress(plain)
        except Exception as ex:
            bad.append(f"chain{chain} {e.path} ({type(ex).__name__})")
            continue
        if e.usize and len(plain) != e.usize:
            bad.append(f"chain{chain} {e.path} (unpacks to {len(plain)}, the TOC says {e.usize})")
        elif again == raw[:len(again)]:             # the stored object may carry padding after the stream
            pass
        elif len(again) == struct.unpack_from("<I", raw, 8)[0] and arx.decompress(again) == plain:
            # same length, same content, another order among equally common
            # words in the header table: docs/FORMATS.md, "Compressor"
            ties.append(f"chain{chain} {e.path}")
        else:
            bad.append(f"chain{chain} {e.path} (recompressed to {len(again)} B)")
    r.check(sample and not bad,
            f"arx: {len(sample) - len(ties) - len(bad)} of {len(sample)} compressed objects "
            f"(the disc has {len(packed)}) recompress to the disc's own bytes ({nbytes:,} B)"
            + (f"; {len(ties)} more are the same size and content with tied table words in another "
               f"order ({', '.join(ties)})" if ties else "")
            + (f"; WRONG: {'; '.join(bad[:5])}" if bad else ""))

    # fonts: parse and write back
    from repack import read_entry
    for name in ("font0.tex", "font1.tex"):
        try:
            data = read_entry(iso_path, 0, name)
            r.check(font.Font.from_bytes(data).to_bytes() == data,
                    f"font: {name} parses and writes back byte-identically ({len(data):,} B)")
        except Exception as e:
            r.check(False, f"font: {name} ({type(e).__name__}: {e})")

    r.step("evttext: every dialogue container rebuilds to its retail bytes",
           _quiet(lambda: evttext.selftest(iso_path)))

    import fcv
    r.step("motion: the party's field packs read to their last byte, line up with their "
           "skeletons, solved chains land on their targets, KOS-MOS's hair and Jr.'s coat "
           "stay clear of the body", _quiet(lambda: fcv.selftest(iso_path)))

    if region.info(iso_path).build != "usa":
        # the editor's rows are keyed to the USA disc (txsource.py); on another
        # build these checks would test the wrong thing
        print("skip  translation editor disc checks (they are keyed to the USA disc, SLUS_204.69)", flush=True)
        return
    import txcodec
    import txfam_umn
    import txcatalog
    import txproject
    import txserver
    r.step("translation editor: codec against the disc's fonts (txcodec)",
           _quiet(lambda: txcodec.selftest(iso_path)))
    r.step("translation editor: menus and mails read and rebuild (txfam_umn)",
           _quiet(lambda: txfam_umn.selftest(iso_path)))
    r.step("translation editor: catalog from the disc (txcatalog)",
           _quiet(lambda: txcatalog.selftest(iso_path)))
    r.step("translation editor: project against the disc (txproject)",
           _quiet(lambda: txproject.selftest(iso_path)))
    r.step("translation editor: every server route on a socket, real catalog (txserver)",
           _quiet(lambda: txserver.selftest(iso_path)))

    if full:
        import txbuild
        import txfam_evt
        import txfam_fix
        import txfam_ovl
        r.step("translation editor: cutscene lines on a clone (txfam_evt)",
               _quiet(lambda: txfam_evt.selftest(iso_path, scratch)))
        r.step("translation editor: executable strings on a clone (txfam_ovl)",
               _quiet(lambda: txfam_ovl.selftest(iso_path, scratch)))
        r.step("translation editor: fixed-size tables on a clone (txfam_fix)",
               _quiet(lambda: txfam_fix.selftest(iso_path, scratch)))
        r.step("translation editor: a whole project built and read back (txbuild)",
               _quiet(lambda: txbuild.selftest(iso_path, scratch)))


def self_test(iso: Optional[str] = None, full: bool = False, scratch: Optional[str] = None) -> int:
    r = _Run()
    iso = iso or os.environ.get("XS1_ISO") or None
    print("--- without a disc", flush=True)
    _imports(r)
    for part in (_arx_synthetic, _text_synthetic, _font_synthetic, _music_synthetic, _region_synthetic,
                 _motion_synthetic, _editor_synthetic):
        try:
            part(r)
        except Exception as e:
            traceback.print_exc()
            r.check(False, f"{part.__name__} stopped with {type(e).__name__}: {e}")
    if iso:
        print(f"--- with the disc {iso}" + (" (full)" if full else ""), flush=True)
        try:
            _disc(r, iso, full, scratch)
        except Exception as e:
            traceback.print_exc()
            r.check(False, f"disc checks stopped with {type(e).__name__}: {e}")
    else:
        print("skip  disc checks (no --iso and XS1_ISO is not set)", flush=True)
    n = r.count
    print(f"=== self-test: {n - len(r.failed)}/{n} checks passed"
          + (f"; FAILED: {'; '.join(r.failed)}" if r.failed else ""), flush=True)
    return 1 if r.failed else 0


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--iso", help="disc image for the read-only disc checks (default: $XS1_ISO)")
    ap.add_argument("--full", action="store_true",
                    help="every compressed object, and the checks that build patched clones")
    ap.add_argument("--scratch", help="folder for the clones --full builds (same volume as the image)")
    a = ap.parse_args(argv)
    return self_test(a.iso, a.full, a.scratch)


if __name__ == "__main__":
    raise SystemExit(main())
