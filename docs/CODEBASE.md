# How the code is organized

A tour of the codebase for someone who has never seen it — what each module
does, how they stack, and where to start reading when you want to change
something. Byte-level format details live in [FORMATS.md](FORMATS.md); the
mechanics of modding in [MODDING.md](MODDING.md). This file is about the
*code*.

## The design rules

Two rules shape everything here:

1. **Standard library only.** No pip installs, ever, for any feature. This
   is why the GUI is a hand-rolled `http.server` page and PNG/WAV writers
   are written from scratch. The only external program the kit will use is
   `ffmpeg`, and only for movie conversion (release bundles ship one).
2. **Every layer is importable on its own.** `toc.py`, `arx.py`,
   `iso9660.py` etc. have no dependencies on the CLI or each other beyond
   what's genuinely needed, so you can `from arx import decompress` in a
   throwaway script and it just works. Several modules double as
   standalone scripts (`python evt_unpack.py …`).

## The big picture

```
                 you
                  │
        ┌─────────┴──────────┐
      gui.py              cli.py ◄──────────── the one entry point that matters
   (browser GUI,             │
    shells out ──────────────┤
    to cli.py)               │
          ┌──────────┬───────┴──────┬───────────────┐
          │          │              │               │
      EXTRACTING   CONVERTING     MODDING        STANDALONE
          │          │              │            evt_unpack.py
      cli.py       browse.py     repack.py       class_map.py
      carve.py     evt.py        pinkhair.py     dump_symbols.py
                                 textpack.py
          │          │              │
          └──────────┴──────┬───────┘
                            │
                   THE READING STACK        ── everything sits on these four
                   iso9660.py   find files inside the ISO image
                   chains.py    read the bigfiles as one byte space
                   toc.py       parse the binary index of each chain
                   arx.py       (de)compress Monolith's ARX format
```

Data flows one way: the reading stack turns a disc image into named
objects; the converters turn objects into desktop formats; the modding
layer runs the reading stack *in reverse* (encode, then write back at the
same offsets).

## The reading stack, bottom up

These four small modules (~460 lines total) are the foundation. Read them
first, in this order — each one is self-explanatory once you've read the
one below it.

### `iso9660.py` (89 lines)
A minimal pure-Python ISO9660 reader: parse the primary volume
descriptor, walk the directory tree, and expose each file as
`IsoFile(name, lba, size)` with `mmap`-backed reads. This is the only
module that knows the disc is an ISO. Key fact used everywhere else: in
ISO9660 a file is one **contiguous** run of 2048-byte sectors, so
`byte N of file F` = `F.lba * 2048 + N` in the image — which is what
makes in-place patching possible later.

### `chains.py` (73 lines)
The seven `XENOSAGA.*` bigfiles form two **chains** (0 = `.00+.01+.02`,
1 = `.10+.11+.12+.13`). `ChainReader` concatenates a chain into one
virtual byte space and serves reads that may span a bigfile boundary.
After this module, "which bigfile is it in" stops being a question
anyone asks; everything above addresses `(chain, offset)`.

### `toc.py` (125 lines)
Parses the binary table of contents at the head of each chain into
`TocEntry(path, sector, size, csize, usize, fields_off, …)` records —
8,922 of them across both chains. The full grammar is in the module
docstring (and [FORMATS.md](FORMATS.md)); it's a serialized pre-order
walk of a directory tree, with compressed entries carrying two sizes.
Two fields matter beyond extraction: `fields_off` is the byte offset of
the entry's size fields inside the TOC file — the hook `repack.py` uses
to patch sizes — and an entry's *allocation* (the gap to the next
entry's sector) is the hard budget any replacement must fit.

### `arx.py` (167 lines)
Monolith's compression, both directions. `decompress()` handles the
2,095 `ARX\0` blobs on the disc (word-oriented dictionary coder, ported
from xenotool). `compress()` is a byte-perfect clone of Monolith's own
2002 packer — 2,094 of 2,095 disc objects recompress byte-identically
(the tie-break rule that achieves this is documented in
[FORMATS.md](FORMATS.md)). The clone is what makes modded-ISO
verification trivial: recompressing an untouched object is a no-op, so
`cmp` against retail shows only your edits.

## Extracting

**`cli.py` → `cmd_extract`** walks both TOCs and writes every object to
`out/dump/chain0/` and `chain1/` exactly as stored (compressed entries
stay compressed — extraction is lossless and `verify` re-checks the
stored bytes). It also writes `manifest.csv` — one row per object with
`area, path, sector, size, compressed, usize, bigfile, bigfile_offset` —
which is the join key for everything downstream: the sweep scripts, the
verifier, and your own greps all navigate by manifest.

**`carve.py`** (76 lines) recovers the 58 movies on DVD layer 1, which no
filesystem or TOC describes. `scan_layer1` scans the raw image beyond the
ISO9660 volume for sector-aligned MPEG-2 pack headers and accepts one as
a *movie start* only if its embedded clock (SCR) reads below one second —
the clock resets at the front of each movie, which cleanly separates true
starts from the repeated mid-stream headers. Output names encode the
absolute sector (`layer1_047_lba3551180.pss`) so the mapping to real
titles can be recovered later from the game's playback code.

**`evt.py`** (123 lines) carves Java class files out of `.evt` FL00
containers. The container's own entry table is untrustworthy (it omits
classes and lists 24-byte stubs), so `carve_classes` ignores it and walks
the JVM class-file structure itself — constant pool, fields, methods,
attributes — at every `CAFEBABE` magic, yielding exact byte ranges and
true fully-qualified names. `cli.py cmd_classes` uses this to lift ~2,200
unique classes into a javap-ready tree.

## Converting (`browse.py`, 784 lines)

`build_browse` maps over `dump/` and writes the human-readable mirror
`browse/`. It's a collection of independent decoders, one per format,
each a direct implementation of the corresponding
[FORMATS.md](FORMATS.md) section:

| Decoder | In → out | Notes |
|---|---|---|
| `decode_xtx` + `lex_materials` | `.xtx` (+ sibling `.lex`) → PNG | composes GS-memory sub-images onto a canvas, unswizzles PSMT8, resolves 256-colour palettes via the model's material table, falling back to an embedded palette, then a corner-scan, then grayscale |
| `decode_voice_stream` + `decode_spu_adpcm` | `.vds`/`.vdm`/`.vda` → WAV | PS2 SPU ADPCM, stereo interleaved every 0x400 bytes, 48 kHz; mono streams auto-detected |
| `parse_swd` / `smd_info` | `.swd`/`.smd` → per-instrument WAVs + catalogue | music is sequenced (Procyon Studio); this extracts samples and metadata, not a rendition |
| `extract_pss_audio` + `convert_movie` | `.pss`/`.ipu` → MP4 | demuxes the Sony-ADS audio ffmpeg misparses, decodes it, muxes AAC; movies with audio also emit `.video.mp4` + `.audio.wav` |
| (inline) | Shift-JIS `.txt` → UTF-8 | transcode so editors show text, not mojibake |

`write_png` and `write_wav` are tiny stdlib-only writers (zlib + struct)
— remember, no pip installs.

## Modding

**`repack.py`** (188 lines) is the whole write path, kept deliberately
small:

* `read_entry(iso, chain, path)` — fetch one object straight from an
  ISO, transparently ARX-decompressed. The read half of every mod.
* `patch_iso(iso, {(chain, path): payload})` — the write half: re-ARX
  compressed entries, write **in place** at the offset computed from
  `lba + chain offset + sector*2048`, patch the TOC's size fields at
  `fields_off`, refuse anything over the entry's allocation, and verify
  every write by reading it back. Always run it on a copy.

Nothing else in the kit writes to an ISO. If you're building a new kind of
mod, you write code that produces `(chain, path) → new bytes` and hand it
to `patch_iso`.

**`pinkhair.py`** (368 lines) is the worked example of exactly that, and
the reference for any texture mod. It knows two color mechanisms
(`recolor_tile_rows` edits 256-entry palettes; `strand_segments`
hue-rotates raw true-colour pixels) and — the actual hard part — sweeps
the whole disc for **embedded copies** of the texture (`build_maps` +
`sweep`), because the disc carries 12 of them inside battle bundles and
re-framed cutscene containers. The three-granularity sweep and the
`patch_reframed_row` walker are explained in [MODDING.md](MODDING.md) §4.

**`textpack.py`** (269 lines) is the translator pipeline: `export_text`
pulls all 914 text objects into an editable UTF-8 tree annotated with
byte budgets; `import_text` re-encodes to Shift-JIS, validates everything
(budgets, round-trips, the fixed-length `.uml` mail regions), and calls
`patch_iso` only when every file passes. The encoding safety nets are in
[MODDING.md](MODDING.md) §5.

## The user interfaces

**`cli.py`** (364 lines) — argparse with one `cmd_<name>` function per
subcommand: `list`, `extract`, `classes`, `browse`, `verify`, `patch`,
`pinkhair`, `text-export`, `text-import`. Engine modules are imported
*lazily inside* each command function so `python cli.py --help` starts
instantly; this laziness is load-bearing for packaging (see below).

**`gui.py`** (1,600 lines) — a local web GUI with zero dependencies: one
self-contained HTML string served by stdlib `http.server`, live logs over
server-sent events. The crucial design decision: the GUI **shells out to
`cli.py`** rather than importing the engine, so the two can never drift —
a GUI card is just a form that builds a CLI argv (`build_<name>`
functions, `BUILDERS` table). Test it headless with
`PORT=8931 python3 gui.py --no-browser` and
`curl -X POST 127.0.0.1:8931/preview/<name>`.

**Launchers** — `launch.bat` / `launch.command` / `launch.sh` just `cd`
next to themselves, find a Python, and run `gui.py`.

**Packaging** — `build.py` wraps PyInstaller with
`packaging/xenosaga1-extractor.spec` to produce the self-contained
bundles (GUI + `xeno-cli` side by side, embedded Python, optional bundled
ffmpeg in `tools/`). Because CLI imports are lazy, PyInstaller can't see
them statically — every lazily-imported module must be listed in the
spec's `HIDDEN` list or the release build breaks while your checkout
works. The full four-touch-point checklist for adding a command
(engine module → `cli.py` → `gui.py` → spec) is in
[MODDING.md](MODDING.md) §7.

## Standalone research tools

Not wired into the GUI; run directly when digging:

* `evt_unpack.py` — FL00 containers from an extracted `dump/` → raw and
  normalized (JVM-loadable) class trees.
* `class_map.py` — parse a class tree into a machine-readable JSON map
  (packages, methods, constant-pool strings).
* `dump_symbols.py` — dump the ELF symbol tables of SLUS + overlays to
  `symbols.csv` (they're unstripped; 8,115 named functions).

## Where to start reading, by goal

| You want to… | Start at |
|---|---|
| understand the disc format | `toc.py` docstring, then [FORMATS.md](FORMATS.md) |
| add a new asset decoder | a sibling in `browse.py` (e.g. `decode_voice_stream`), wire into `build_browse` |
| build a new mod | `repack.py` (it's 188 lines), then `pinkhair.py` as the worked example |
| translate the game | [MODDING.md](MODDING.md) §5, then `textpack.py` |
| decompile cutscene logic | [JAVA.md](JAVA.md), then `evt.py` |
| add a command end-to-end | [MODDING.md](MODDING.md) §7 (the four touch points) |
| trust but verify | `cli.py cmd_verify`, plus the checklist in [MODDING.md](MODDING.md) §8 |

## Verification culture

Every claim in these docs was established against the retail USA disc,
and the code keeps re-proving it: `extract` is lossless and `verify`
re-checks every manifest row; `patch_iso` read-backs every write; the ARX
clone makes whole-disc diffs meaningful; text exports round-trip
(decode → re-encode == disc bytes) before they're written; and for
anything visual, [MODDING.md](MODDING.md) §6 shows how to dump the
running game's RAM over PCSX2's PINE socket and check what the engine
*actually* loaded. When you change the code, keep that property: prefer
adding a check to trusting a formula.
