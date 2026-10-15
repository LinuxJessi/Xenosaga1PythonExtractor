#!/usr/bin/env python3
"""cli.py — Xenosaga Episode I (USA) disc extractor.

Pure-Python, stdlib-only (no 7-Zip needed — the disc is plain ISO9660 and
this kit parses it directly).

Usage:
    python cli.py list    --iso GAME.iso [--chain 0|1]
    python cli.py extract --iso GAME.iso --out OUTDIR
                          [--chain 0|1] [--glob 'movie\\*'] [--no-carve] [--code]
    python cli.py classes --iso GAME.iso --out OUTDIR
    python cli.py browse  --out OUTDIR [--kinds textures,models,audio,banks,battle_audio,images,text,movies]
                          [--rate 48000]
    python cli.py verify  --out OUTDIR
    python cli.py patch   --iso GAME.iso --out MODDED.iso
                          --set 'chain0:char\\pc\\kosmos.xtx=my_kosmos.xtx' [...]
    python cli.py pinkhair --iso GAME.iso --out PINK.iso [--hue 0.92] [--dry-run]
    python cli.py text-export --iso GAME.iso --out TEXTDIR
    python cli.py text-import --iso GAME.iso --text TEXTDIR --out MODDED.iso
    python cli.py subs-template --src MOVIE.pss --out MOVIE.srt [--cue-seconds 5]
    python cli.py subs-burn --src MOVIE.pss --srt MOVIE.srt --out MOVIE.dub.pss
                            [--max-bytes N]
    python cli.py layer1-list  --iso GAME.iso
    python cli.py layer1-patch --iso GAME.iso --out MODDED.iso
                               (--index N | --name layer1_045_lba....pss) --file NEW.pss

``extract`` writes:
    OUTDIR/dump/chain0/<in-game path>   chain-0 files (system / field data)
    OUTDIR/dump/chain1/<in-game path>   chain-1 files (voice / scene / movies)
    OUTDIR/dump/layer1/*.pss            the 58 movies carved from layer 1
    OUTDIR/browse/code/                 SLUS + overlays + IOP modules (--code)
    OUTDIR/manifest.csv                 one row per extracted object

``browse`` converts the extracted dump into viewable/playable formats under
``OUTDIR/browse/``: .xtx textures -> PNG (pure Python) plus a disc-wide
sweep for XTX blobs embedded in effect/scene/UI containers (~3,000 more,
ARX-packed scene members decompressed first; written to
textures_png/_embedded + embedded_textures.csv), .vds/.vdm
voice -> 48 kHz WAV (pure Python), battle voice/SE banks
(yamamoto/snd/sed/*.bin) -> per-sample WAVs, .jpg copied + PS2ICON3D .res
unpacked (icon.sys / .ico), .txt recoded to UTF-8 + string tables sniffed
out of binary-extension files, and .pss/.ipu movies -> MP4 when ffmpeg is
available (skipped with a note otherwise; raw .pss plays in VLC).

``classes`` reads every ``.evt`` event container straight from the disc
image (no prior extract needed) and lifts the embedded Java class files out
of the FL00 wrapper (see evt.py):
    OUTDIR/browse/classes/<package>/<Class>.class    javap-ready layout
    OUTDIR/browse/classes_manifest.csv               one row per class found

ARX-compressed entries (TOC flag 0x40; payload begins with "ARX\\0") are
written as stored — compressed — and flagged in the manifest. ``browse``
decompresses them transparently (arx.py) when converting.
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import signal
import sys
from pathlib import Path

# behave like a normal Unix filter when piped into head etc.
# SIGPIPE doesn't exist on Windows (and matters only for the packaged exe there).
if hasattr(signal, "SIGPIPE"):
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)

from carve import scan_layer1
from chains import CHAINS, ChainReader
from evt import carve_classes
from chains import available_chains
from iso9660 import BOOT_RE, IsoImage
from toc import parse_toc

MAGICS = {
    "pss": b"\x00\x00\x01\xba",
    "jpg": b"\xff\xd8",
    "ipu": b"ipum",
}


def _entries(iso: IsoImage, chain: int):
    reader = ChainReader(iso, chain)
    return reader, parse_toc(reader.toc_bytes(), CHAINS[chain][0])


def cmd_list(args) -> int:
    iso = IsoImage(args.iso)
    for chain in [args.chain] if args.chain is not None else available_chains(iso):
        _, entries = _entries(iso, chain)
        for e in entries:
            flag = "C" if e.compressed else " "
            print(f"chain{chain} {flag} {e.byte_offset:>12} {e.size:>11} {e.path}")
    return 0


def cmd_extract(args) -> int:
    iso = IsoImage(args.iso)
    out = Path(args.out)
    rows = []
    chains = [args.chain] if args.chain is not None else available_chains(iso)
    for chain in chains:
        reader, entries = _entries(iso, chain)
        if args.glob:
            entries = [e for e in entries if fnmatch.fnmatch(e.path.lower(), args.glob.lower())]
        print(f"chain {chain}: extracting {len(entries)} entries", file=sys.stderr)
        for i, e in enumerate(entries):
            dest = out / "dump" / f"chain{chain}" / Path(*e.path.split("\\"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as fh:
                for block in reader.read_iter(e.byte_offset, e.size):
                    fh.write(block)
            bigfile, local = reader.locate(e.byte_offset)
            rows.append({
                "area": f"chain{chain}", "path": e.path, "sector": e.sector,
                "size": e.size, "compressed": int(e.compressed),
                "usize": e.usize or "", "bigfile": bigfile, "bigfile_offset": local,
            })
            if (i + 1) % 1000 == 0:
                print(f"  {i + 1}/{len(entries)}", file=sys.stderr)
    if 2 in chains and not args.no_carve and args.glob is None:
        print("layer 1: chain 2 holds the second-layer movies; raw carve skipped", file=sys.stderr)
    if not args.no_carve and args.glob is None and args.chain is None and 2 not in chains:
        streams = scan_layer1(iso)
        print(f"layer 1: carving {len(streams)} movie streams", file=sys.stderr)
        ldir = out / "dump" / "layer1"
        ldir.mkdir(parents=True, exist_ok=True)
        for s in streams:
            with open(ldir / s.name, "wb") as fh:
                pos = s.start
                while pos < s.start + s.size:
                    n = min(8 * 1024 * 1024, s.start + s.size - pos)
                    fh.write(iso.mm[pos : pos + n])
                    pos += n
            rows.append({
                "area": "layer1", "path": s.name, "sector": s.sector,
                "size": s.size, "compressed": 0, "usize": "",
                "bigfile": "(raw image)", "bigfile_offset": s.start,
            })
    if args.code:
        cdir = out / "browse" / "code"
        cdir.mkdir(parents=True, exist_ok=True)
        boot = iso.boot_elf_name()
        for name, f in iso.files.items():
            if f.is_dir or not (name.endswith((".OVL", ".IRX", ".IMG"))
                                or name == boot or BOOT_RE.match(name)):
                continue
            dest = cdir / name.replace("\\", "/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(iso.read_file(name))
            rows.append({
                "area": "code", "path": name, "sector": f.lba, "size": f.size,
                "compressed": 0, "usize": "", "bigfile": "(iso fs)", "bigfile_offset": f.lba * 2048,
            })
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "manifest.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "area", "path", "sector", "size", "compressed", "usize", "bigfile", "bigfile_offset",
        ])
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {len(rows)} rows to {out / 'manifest.csv'}", file=sys.stderr)
    return 0


def cmd_classes(args) -> int:
    import hashlib

    iso = IsoImage(args.iso)
    out = Path(args.out)
    cdir = out / "browse" / "classes"
    rows = []
    first_sha: dict[str, str] = {}      # fqcn -> sha1 of first-written content
    written_variants: set[str] = set()  # sha1s already on disk under any name
    n_evt = n_stub = n_dup = n_arx = 0
    for chain in available_chains(iso):
        reader, entries = _entries(iso, chain)
        for e in entries:
            if not e.path.lower().endswith(".evt"):
                continue
            if e.compressed:
                n_arx += 1
                continue
            data = b"".join(reader.read_iter(e.byte_offset, e.size))
            classes, stubs = carve_classes(data)
            n_evt += 1
            n_stub += stubs
            for c in classes:
                blob = data[c.offset : c.offset + c.size]
                sha = hashlib.sha1(blob).hexdigest()
                dest = ""
                if sha in written_variants:
                    n_dup += 1
                else:
                    rel = c.name if first_sha.get(c.name) in (None, sha) \
                        else f"{c.name}__{sha[:8]}"
                    first_sha.setdefault(c.name, sha)
                    written_variants.add(sha)
                    p = cdir / (rel + ".class")
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_bytes(blob)
                    dest = str(p.relative_to(out))
                rows.append({
                    "area": f"chain{chain}", "evt": e.path, "class": c.name,
                    "offset": c.offset, "size": c.size, "sha1": sha[:12],
                    "written": dest,
                })
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "browse" / "classes_manifest.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "area", "evt", "class", "offset", "size", "sha1", "written",
        ])
        w.writeheader()
        w.writerows(rows)
    uniq = len(written_variants)
    print(
        f"scanned {n_evt} .evt containers: {len(rows)} classes "
        f"({uniq} unique written to {cdir}), {n_dup} duplicates, "
        f"{n_stub} stub records skipped"
        + (f", {n_arx} ARX-compressed .evt skipped" if n_arx else ""),
        file=sys.stderr,
    )
    print(
        "inspect with:  javap -c -p -classpath "
        f"{cdir} xeno.plan.Base   (or any class from the manifest)",
        file=sys.stderr,
    )
    return 0


def cmd_browse(args) -> int:
    from browse import ALL_KINDS, VOICE_RATE, build_browse

    kinds = [k.strip() for k in (args.kinds or ",".join(ALL_KINDS)).split(",") if k.strip()]
    unknown = [k for k in kinds if k not in ALL_KINDS]
    if unknown:
        print(f"unknown kind(s): {', '.join(unknown)} "
              f"(choose from {', '.join(ALL_KINDS)})", file=sys.stderr)
        return 2
    build_browse(Path(args.out), kinds, rate=args.rate or VOICE_RATE)
    print(f"browse bundle ready under {Path(args.out) / 'browse'}", file=sys.stderr)
    return 0


def cmd_verify(args) -> int:
    out = Path(args.out)
    bad = checked = 0
    with open(out / "manifest.csv") as fh:
        for row in csv.DictReader(fh):
            if row["area"] == "code":
                continue
            sub = "dump" if row["area"].startswith(("chain", "layer")) else "."
            p = out / sub / (row["area"] if row["area"] != "layer1" else "layer1") / Path(*row["path"].split("\\"))
            if not p.exists():
                print(f"MISSING {p}")
                bad += 1
                continue
            if p.stat().st_size != int(row["size"]):
                print(f"SIZE MISMATCH {p}: {p.stat().st_size} != {row['size']}")
                bad += 1
                continue
            head = open(p, "rb").read(8)
            checked += 1
            if int(row["compressed"]):
                if not head.startswith(b"ARX\x00"):
                    print(f"BAD ARX MAGIC {p}: {head.hex()}")
                    bad += 1
                continue
            ext = row["path"].rsplit(".", 1)[-1].lower()
            magic = MAGICS.get(ext)
            if magic and not head.startswith(magic):
                print(f"BAD {ext.upper()} MAGIC {p}: {head.hex()}")
                bad += 1
    print(f"verified {checked} files, {bad} problems")
    return 1 if bad else 0


def cmd_patch(args) -> int:
    import shutil

    from repack import patch_iso

    replacements = {}
    for spec in args.set:
        try:
            target, src = spec.split("=", 1)
            chain_s, path = target.split(":", 1)
            chain = int(chain_s.removeprefix("chain"))
        except ValueError:
            print(f"bad --set {spec!r} (want 'chain0:some\\path=file')",
                  file=sys.stderr)
            return 2
        replacements[(chain, path)] = Path(src).read_bytes()
    dst = Path(args.out)
    if dst.resolve() != Path(args.iso).resolve():
        print(f"copying {args.iso} -> {dst} ...", file=sys.stderr)
        shutil.copyfile(args.iso, dst)
    patch_iso(dst, replacements)
    return 0


def cmd_pinkhair(args) -> int:
    from pinkhair import DEFAULT_HUE, run

    try:
        return run(args.iso, args.out, hue=(args.hue if args.hue is not None
                                            else DEFAULT_HUE),
                   preview=args.preview, dry_run=args.dry_run)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


def cmd_text_export(args) -> int:
    from textpack import export_text

    return export_text(args.iso, args.out)


def cmd_text_import(args) -> int:
    from textpack import import_text

    return import_text(args.iso, args.text, args.out, args.grow)


def cmd_evt_export(args) -> int:
    from evttext import export_dialogue

    return export_dialogue(args.iso, args.out)


def cmd_evt_import(args) -> int:
    from evttext import import_dialogue

    return import_dialogue(args.iso, args.text, args.out, args.grow)


def cmd_ovl_export(args) -> int:
    from ovltext import export_strings

    return export_strings(args.iso, args.out)


def cmd_ovl_import(args) -> int:
    from ovltext import import_strings

    return import_strings(args.iso, args.text, args.out, args.grow, args.retail)


def cmd_statfont(args) -> int:
    from statfont import main as statfont_main

    return statfont_main([args.statfont_cmd, *args.rest])


def cmd_font_extract(args) -> int:
    import font
    from repack import read_entry

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for name in ("font0.tex", "font1.tex"):
        (out / name).write_bytes(read_entry(args.iso, 0, name))
        font.cmd_extract(str(out / name), str(out))
    return 0


def cmd_font_rebuild(args) -> int:
    import font

    return font.cmd_rebuild(args.ref, args.sheets, args.out, args.metrics)


def cmd_lang(args) -> int:
    import langpack

    argv = [args.lang_cmd] + args.rest
    return langpack.main(argv)


def cmd_subs_template(args) -> int:
    import subs
    from browse import detect_ffmpeg

    ffprobe = subs.detect_ffprobe(detect_ffmpeg())
    if not ffprobe:
        print("error: ffprobe not found (need ffmpeg installed)", file=sys.stderr)
        return 2
    if args.detect:
        n = subs.write_srt_detected(args.src, args.out, ffmpeg=detect_ffmpeg())
        print(f"wrote {n} cues timed from the burned-in subtitle to {args.out} "
              "— fill in each line, then run subs-burn", file=sys.stderr)
        return 0
    n = subs.write_srt_template(args.src, args.out,
                                cue_seconds=args.cue_seconds or 5.0,
                                ffprobe=ffprobe)
    print(f"wrote {n} blank cues to {args.out} — time and translate them in "
          "any subtitle editor (Aegisub, Subtitle Edit) against the movie's "
          "already-extracted MP4, then run subs-burn", file=sys.stderr)
    return 0


def cmd_subs_burn(args) -> int:
    import subs
    from browse import detect_ffmpeg

    ffmpeg = detect_ffmpeg()
    if not ffmpeg:
        print("error: ffmpeg not found", file=sys.stderr)
        return 2
    ffprobe = subs.detect_ffprobe(ffmpeg)
    renderer = None
    if args.font:
        try:
            renderer = subs.CueRenderer(args.font, px=args.px, face_index=args.face,
                                        script=args.script, language=args.language,
                                        rtl=args.rtl)
        except Exception as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    mask = None
    if args.mask != "none":
        try:
            mask = tuple(int(v) for v in args.mask.split(","))
            assert len(mask) == 4
        except (ValueError, AssertionError):
            print("error: --mask wants x,y,w,h or none", file=sys.stderr)
            return 2
    try:
        report = subs.burn(args.src, args.srt, args.out, ffmpeg=ffmpeg,
                           ffprobe=ffprobe, max_bytes=args.max_bytes,
                           renderer=renderer, cue_dir=args.cues, mask=mask,
                           bottom_margin=args.bottom, keep_cues=args.keep_cues,
                           band=args.band, band_pad=args.band_pad)
    except (RuntimeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"wrote {args.out}: {report['new_size']} bytes "
          f"(source was {report['orig_size']}, {report['video_packets']} "
          f"video packets, encoded at {report['bitrate_kbps']} kbps in "
          f"{report['attempts']} attempt(s))", file=sys.stderr)
    print("NOT verified against the PS2 IPU decoder or a real disc — boot "
          "in PCSX2 (via layer1-patch/patch + the movie's own chain/index) "
          "before trusting a batch of these. See docs/SUBTITLES.md.",
          file=sys.stderr)
    return 0


def cmd_layer1_list(args) -> int:
    import subs

    for s in subs.list_layer1(args.iso):
        print(f"{s.index:>3}  {s.name}  sector={s.sector:<10} size={s.size}")
    return 0


def cmd_layer1_patch(args) -> int:
    import shutil

    import subs

    if args.index is None and args.name is None:
        print("error: need --index or --name", file=sys.stderr)
        return 2
    dst = Path(args.out)
    if dst.resolve() != Path(args.iso).resolve():
        print(f"copying {args.iso} -> {dst} ...", file=sys.stderr)
        shutil.copyfile(args.iso, dst)
    new_bytes = Path(args.file).read_bytes()
    try:
        subs.patch_layer1(dst, args.index if args.index is not None else args.name,
                          new_bytes)
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_music_export(args) -> int:
    """Sequenced BGM -> .mid + .sf2 (+ .wav/.flac when numpy is present)."""
    import ssd

    dump, out = Path(args.dump), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        import ssd_render
    except ImportError:
        ssd_render = None
        print("numpy not available: writing MIDI + SoundFont only", file=sys.stderr)
    if ssd_render is not None:
        from browse import detect_ffmpeg

        ssd_render.export_all(dump, out, detect_ffmpeg())
        return 0
    banks = []
    for p in sorted(dump.rglob("*.SWD")) + sorted(dump.rglob("*.SED")):
        b = ssd.parse_bank(p.read_bytes(), p.stem)
        if b and b.programs:
            banks.append((p, b))
    def match_bank(p, seq):
        used = seq.used_programs() or {0}
        for bp, b in banks:                       # exact stem match wins
            if bp.stem.lower() == p.stem.lower():
                return b
        best, best_cov = None, 0.0
        for bp, b in banks:                       # else best program coverage
            cov = len(used & set(b.programs)) / len(used)
            if cov > best_cov:
                best, best_cov = b, cov
        return best
    for p in sorted(dump.rglob("*.SMD")) + sorted(dump.rglob("*.smd")):
        data = p.read_bytes()
        if len(data) < 5000:
            continue
        seq = ssd.parse_sequence(data, p.stem)
        if not seq:
            continue
        bank = match_bank(p, seq)
        if bank is None:
            print(f"  {p.stem}: no matching bank, skipped", file=sys.stderr)
            continue
        (out / f"{p.stem}.mid").write_bytes(ssd.sequence_to_midi(seq))
        (out / f"{p.stem}.sf2").write_bytes(ssd.bank_to_sf2(bank, p.stem))
        print(f"  {p.stem} ({seq.title!r})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("list", cmd_list), ("extract", cmd_extract),
                     ("classes", cmd_classes), ("browse", cmd_browse),
                     ("verify", cmd_verify), ("patch", cmd_patch),
                     ("pinkhair", cmd_pinkhair),
                     ("text-export", cmd_text_export),
                     ("text-import", cmd_text_import),
                     ("evt-export", cmd_evt_export),
                     ("evt-import", cmd_evt_import),
                     ("ovl-export", cmd_ovl_export),
                     ("ovl-import", cmd_ovl_import),
                     ("font-extract", cmd_font_extract),
                     ("font-rebuild", cmd_font_rebuild),
                     ("lang", cmd_lang),
                     ("statfont", cmd_statfont),
                     ("subs-template", cmd_subs_template),
                     ("subs-burn", cmd_subs_burn),
                     ("layer1-list", cmd_layer1_list),
                     ("layer1-patch", cmd_layer1_patch),
                     ("music-export", cmd_music_export)):
        p = sub.add_parser(name)
        if name == "music-export":
            p.add_argument("--dump", required=True,
                           help="extracted dump directory (cli.py extract output)")
            p.add_argument("--out", required=True)
            p.set_defaults(fn=cmd_music_export)
            continue
        if name == "statfont":
            p.add_argument("statfont_cmd", choices=("extract", "scan", "encode"),
                           help="battle-font step; remaining options go to it "
                                "(python statfont.py <step> -h)")
            p.add_argument("rest", nargs=argparse.REMAINDER)
            p.set_defaults(fn=fn)
            continue
        if name == "lang":
            p.add_argument("lang_cmd", choices=("scan", "encode", "preview"),
                           help="langpack step; remaining options go to it "
                                "(python langpack.py <step> -h)")
            p.add_argument("rest", nargs=argparse.REMAINDER)
            p.set_defaults(fn=fn)
            continue
        if name not in ("browse", "subs-template", "subs-burn", "font-rebuild"):
            p.add_argument("--iso", required=name not in ("verify",))
        p.set_defaults(fn=fn)
        if name in ("list", "extract"):
            p.add_argument("--chain", type=int, choices=(0, 1, 2))
        if name == "extract":
            p.add_argument("--out", required=True)
            p.add_argument("--glob")
            p.add_argument("--no-carve", action="store_true")
            p.add_argument("--code", action="store_true")
        if name in ("classes", "browse", "verify"):
            p.add_argument("--out", required=True)
        if name == "browse":
            p.add_argument("--kinds", help="comma list: textures,models,audio,"
                           "banks,battle_audio,images,text,movies")
            p.add_argument("--rate", type=int, help="voice sample rate (default 48000)")
        if name == "patch":
            p.add_argument("--out", required=True,
                           help="patched ISO to write (copied from --iso first)")
            p.add_argument("--set", action="append", required=True, metavar
                           ="chainN:toc\\path=localfile",
                           help="replace a TOC object with a local file "
                                "(uncompressed content; repeatable)")
        if name == "pinkhair":
            p.add_argument("--out",
                           help="recolored ISO to write (required unless --dry-run)")
            p.add_argument("--hue", type=float,
                           help="target hue 0..1 (default 0.92 = pink)")
            p.add_argument("--preview",
                           help="directory for before/after PNG renders")
            p.add_argument("--dry-run", action="store_true",
                           help="recolor + sweep only, write no ISO")
        if name == "text-export":
            p.add_argument("--out", required=True,
                           help="directory for the editable UTF-8 text tree")
        if name == "text-import":
            p.add_argument("--text", required=True,
                           help="edited text tree (from text-export)")
            p.add_argument("--out", required=True,
                           help="patched ISO to write")
            p.add_argument("--grow", action="store_true",
                           help="relocate objects that outgrow their allocation")
        if name == "evt-export":
            p.add_argument("--out", required=True,
                           help="directory for the dialogue text tree")
        if name == "evt-import":
            p.add_argument("--text", required=True,
                           help="edited dialogue tree (from evt-export)")
            p.add_argument("--out", required=True, help="patched ISO to write")
            p.add_argument("--grow", action="store_true",
                           help="relocate containers that outgrow their allocation")
        if name == "ovl-export":
            p.add_argument("--out", required=True,
                           help="directory for the executable-strings tree (menus, battle)")
        if name == "ovl-import":
            p.add_argument("--text", required=True,
                           help="edited executable-strings tree (from ovl-export)")
            p.add_argument("--out", required=True, help="patched ISO to write")
            p.add_argument("--grow", action="store_true",
                           help="move strings that outgrow their slot to the end of "
                                "the overlay, and the overlay to the end of the image "
                                "if the next file leaves no room")
            p.add_argument("--retail", help="unmodified ISO to take the executables from "
                                            "(import again into an already-patched disc)")
        if name == "font-extract":
            p.add_argument("--out", required=True,
                           help="directory for font0/font1 .tex, sheet PNGs, metrics CSV")
        if name == "font-rebuild":
            p.add_argument("--ref", required=True, help="retail .tex to start from")
            p.add_argument("--sheets", required=True, help="directory with edited sheet PNGs")
            p.add_argument("--metrics", help="edited metrics CSV")
            p.add_argument("--out", required=True, help=".tex to write")
        if name == "subs-template":
            p.add_argument("--src", required=True, help="source .pss movie")
            p.add_argument("--out", required=True, help="SRT skeleton to write")
            p.add_argument("--cue-seconds", type=float,
                           help="uniform cue length (default 5s) — a rough "
                                "starting point, retime by hand")
            p.add_argument("--detect", action="store_true",
                           help="time the cues from the burned-in subtitle "
                                "instead (one cue per interval it is on screen)")
        if name == "subs-burn":
            p.add_argument("--src", required=True, help="source .pss movie")
            p.add_argument("--srt", required=True, help="translated, timed SRT")
            p.add_argument("--out", required=True,
                           help="subtitled .pss to write (original audio kept)")
            p.add_argument("--max-bytes", type=int,
                           help="size ceiling (default: source file's own "
                                "size — the allocation for a TOC movie or "
                                "layer-1 slot is never larger than that)")
            p.add_argument("--font", help="TrueType/OpenType font: render the cues with "
                                          "the kit (HarfBuzz shaping, any script) instead "
                                          "of libass")
            p.add_argument("--face", type=int, default=0, help="face index in a .ttc")
            p.add_argument("--px", type=float, default=22.0, help="letter size in pixels")
            p.add_argument("--script", help="HarfBuzz script tag (Deva, Arab, …)")
            p.add_argument("--language", help="BCP-47 language (hi, ar, …)")
            p.add_argument("--rtl", action="store_true", help="right-to-left text")
            p.add_argument("--cues", help="directory of hand-made cue PNGs "
                                          "(cue0001.png … one per SRT cue)")
            p.add_argument("--mask", default="0,330,512,118",
                           help="band to cover while a cue is up, x,y,w,h "
                                "(the original subtitle's place); 'none' to skip")
            p.add_argument("--band", default="black", choices=("black", "blur"),
                           help="how the band is covered: a solid black bar "
                                "(default; hides the English outright and costs "
                                "the encoder nothing) or a box blur")
            p.add_argument("--band-pad", type=float, default=0.25,
                           help="seconds the cover opens before each cue and "
                                "closes after it (the original line's own fade "
                                "is rarely inside the SRT times)")
            p.add_argument("--bottom", type=int, default=28,
                           help="pixels between the cue and the bottom edge")
            p.add_argument("--keep-cues", help="also write the rendered cue PNGs here")
        if name == "layer1-patch":
            p.add_argument("--out", required=True, help="patched ISO to write")
            p.add_argument("--index", type=int, help="movie index (see layer1-list)")
            p.add_argument("--name", help="movie filename (see layer1-list)")
            p.add_argument("--file", required=True, help="replacement .pss")
    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
