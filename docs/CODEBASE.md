# How the code is organized

A tour of the codebase for someone who has never seen it — what each module
does, how they stack, and where to start reading when you want to change
something. Byte-level format details live in [FORMATS.md](FORMATS.md); the
mechanics of modding in [MODDING.md](MODDING.md). This file is about the
*code*.

## The design rules

Two rules shape everything here:

1. **Standard library, plus three required packages.** Extracting,
   converting, patching and the text pipeline are the standard library:
   this is why the GUI is a hand-rolled `http.server` page and PNG/WAV
   writers are written from scratch. Three packages are needed on top, and
   all three are *required*, never optional with a worse fallback
   (`requirements.txt`): numpy for the music renderer, fontTools and
   uharfbuzz for drawing and shaping letters the game's fonts never had.
   The launchers install them, `build.py` refuses to build without them,
   the app bundles carry them, and `cli.py self-test` fails when one cannot
   be imported. (Until 2026-11-21 this rule read "standard library only"
   and `music-export` quietly wrote MIDI and SoundFont without numpy; that
   fallback is gone.) The only external program the kit uses is `ffmpeg`,
   for movies and FLAC (release bundles ship one).
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
                   lex.py
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
| translate the game | [TRANSLATION-EDITOR.md](TRANSLATION-EDITOR.md) and `txbuild.py`; the text trees: [MODDING.md](MODDING.md) §5, then `textpack.py` and `evttext.py` |
| translate into another alphabet | [SCRIPTS.md](SCRIPTS.md), then `font.py` → `glyphs.py` → `langpack.py` |
| translate menu and battle words | `ovltext.py` (the overlays), then [SCRIPTS.md](SCRIPTS.md) § 2b; battle bars: `statfont.py`, § 2c |
| subtitle a movie in any script | [SUBTITLES.md](SUBTITLES.md), then `subs.py` (`CueRenderer`, `encode_es_overlay`) |
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

**`python cli.py self-test`** (`selftest.py`, November 2026) runs those
checks in one go, the way the Episode II and III kits do. Without a disc:
every module and required package imports, the ARX, text and font codecs
round-trip made-up data, `region.py` identifies builds, and the translation
editor answers every route on a real socket with its fake catalog. With
`--iso` (or `XS1_ISO`), read-only: the three tables of contents parse, a
sample of the disc's ARX objects recompress to the disc's own bytes, both
fonts and every dialogue container rebuild identically, and on the USA disc
the editor's codec, catalog, project and server checks run against it.
`--full` recompresses every ARX object and runs the editor checks that
build patched copy-on-write clones in `--scratch`. `build.py` runs the
no-disc part from inside the finished bundle.

**`python cli.py region --iso GAME.iso`** (`region.py`) names the build
(USA retail, the Japanese *Reloaded* disc, the 2002 Japanese original by
serial only), prints the executable's sha256 and whether it still has its
symbol table, counts each chain, and says what the kit has been checked
with on that build.


## Added in the translation update (October 2026)

**`evttext.py`** — scene dialogue in and out of the `.evt` Java scripts
at any length: `parse_pool`/`serialize` for class files, `fl00_rebuild`
for the container, `strings_of`/`rebuild` as the API, a text format of
`@class.index<TAB>text` lines, and a `selftest` that must reproduce every
retail container byte-for-byte.

**`font.py`** — the two bitmap fonts: `Font.from_bytes`/`to_bytes` (GS
unswizzle and back, exact), four 640x768 sheets of 20x24 cells, the
metrics table, and the engine's own addressing rules (`cell_of_code`,
`prop_index`, `cell_of_prop`, `ascii_to_code`, `width_of_bytes`). PNG in
and out with stdlib `zlib`.

**`glyphs.py`** — give a new script cells in those fonts: `GlyphSet`
(keys → slots, proportional first, kanji cells for overflow, JSON on
disk), `from_png` for hand-drawn cells, and `TTFRenderer` (HarfBuzz
shaping + fontTools outlines + a stdlib scanline rasteriser) for the
automatic path. Those two packages and numpy (music rendering) are the
kit's only non-stdlib dependencies; all three are required.

**`langpack.py`** — the text side: tokenise (control codes kept in
logical order), shape per word, measure with the new metrics, wrap to a
pixel width, order runs for right-to-left paragraphs (UBA levels for
runs, mirrored brackets), emit `⟦XX⟧` byte markers, and `render_line` for
previews without the game.

`repack.py` grew `grow=True` (append + repoint), `iso9660.py` the second
volume, `chains.py` chain 2, `textpack.py` the `.uml` growth rules and
EUC-JP.

**`ovltext.py`** — the third text tree: the C strings inside the code
overlays `OV01`-`OV12` and the boot ELF (tech names, "End", "Menu",
item and skill descriptions, the field menu's labels). `Overlay` parses
the ELF, finds every NUL-terminated string that something points at
(32-bit words outside functions; `lui`/`addiu` pairs inside them, using
the `.symtab` function ranges), and `apply` rewrites them: in place when
they fit, else appended to the load segment with every reference
repointed; `export_strings`/`import_strings` are the tree API. On the
ISO side `repack.patch_fs_file` writes a root file back in place (and
updates its directory record) and `repack.relocate_fs_file` appends it
to the image when the next extent leaves no room; `iso9660.IsoFile`
carries `record_off` for that.

**`statfont.py`** — the battle HUD's font: the 25×10 grid of 10×18
cells in `yamamoto\stat\stat.xtx` (XTX canvas in and out, the PSMT8
swizzle both ways), the 230-byte lookup table, `CellRenderer` (HarfBuzz
shaping + the outline filler at cell size, coverage mapped to the
sheet's index ramp), `scan` (free cells + spare bytes per glyph),
`encode` (strings as those bytes plus the rewritten table line for
`ovl-import`).

**`subs.py`** additions — `CueRenderer` draws an SRT cue as an outlined
RGBA PNG through `glyphs.TTFRenderer` (HarfBuzz shaping, any script),
`encode_es_overlay` composites the cues with ffmpeg's `overlay` filter
and blurs the original subtitle band while a cue is up, and
`detect_burned_in` finds when the original subtitle is on screen so
`subs-template --detect` can write a timed skeleton. `burn` picks the
overlay path whenever a font or a directory of cue PNGs is given, and
needs no libass for it.


## The translation editor (November 2026)

One page (`/editor`) over every string of the disc, documented for
users in [TRANSLATION-EDITOR.md](TRANSLATION-EDITOR.md). Reads run
in-process in the GUI because they are read-only; the build shells out
to `cli.py tx-build` like every other card.

**`txcodec.py`** — display text ↔ engine bytes for the three printers
(field font, battle grid, ASCII ribbons): `to_display`/`from_display`,
`check` (protected tokens, byte limit, width), one Latin map per font
face (`LATIN_MAP` for font1, `LATIN_MAP_FONT0`; they swap grave and
acute), `evt_face`, and `fix_capital_accents` for font0's È É Ê Ë.

**`txfam_umn.py`, `txfam_evt.py`, `txfam_ovl.py`, `txfam_fix.py`** — the
four string families, each with `rows(iso)` (catalog rows), `validate`
and `apply(out, retail, edits)` (writes from the retail bytes, refuses
the retail image, validates before writing) and a `selftest` that
builds a French/Spanish sample into a clone and reads it back.
`umn`: mails, database, chats and the two header lists; `evt`: every
string literal of the `.evt` scripts, with drawn/never-drawn analysis of
the bytecode; `ovl`: the executables' C strings on top of `ovltext`;
`fix`: fixed tables (key items, save-screen names, plug-ins, junk
parts, cards, casino, enemy battle quotes).

**`txcatalog.py`** — merges the families into one catalog (`rows`,
`cats` tree), cached per retail id. **`txproject.py`** — the project
JSON, retail id, TSV and numbered-paste formats. **`txserver.py`** /
**`txeditor_page.py`** — the `/editor` page and its `/tx/*` routes
(per-launch token, loopback only), delegated to from `gui.Handler`.
**`txbuild.py`** — the build: retail-id check, validate all, clone to
`OUT.partial`, apply, font fix, read back, rename.

## Motion (November 2026)

**`fcv.py`** — the motion packs ([MOTION.md](MOTION.md)). `parse_fpk` /
`parse_clip` / `read_pack` read a pack without its skeleton (the value
mask says how many curves follow). The key stream ends where the game's
does, at a 0xFFFF record or a record that is not type 4; with `strict`
(the sweep and the disc self-test) a clip that does not end on its last
byte is refused. `parse_rig` reads a `.jnt` the way the motion code sees
it (block counts, `JNT_hairID`, the attribute key, limits, the bend
sign), `_ranges` splits its blocks into the four ranges
`JNT_computeMatrix` walks, `fits` says whether a clip's body records
line up with it, and `eval_curve` is `FCV2_getPackVal`. `pose` gives
world matrices for every block at a frame: body blocks read and built
the way `JNT_getVal` and `JNT_constructMatrix` do (chain solver
included), the next additional_primary_count blocks the way
`JNT_getVal2` does, the hair and cloth blocks at rest. `simulate` runs
those hair and cloth chains through the port of `EXM_CalcExMotion`:
`exm_preset` is `EXM_PresetHair`, `exm_chains` lists the chains it
solves, `exm_step` is one tick and `_exm_collide` is
`EXM_CheckExCollision`. `bake` samples a clip into per-block
translation, rotation and scale (through `simulate` when the skeleton
has hair or cloth chains), and `add_to_glb` appends those as glTF
animations to a `.glb` that `lex.to_glb` wrote (it finds the armature
nodes by position, so `lex.py` knows nothing about motion). When a clip
scales a block unevenly under a child turned against it, `add_to_glb`
moves the blocks under it to the skeleton node (`_hang_off_root`) and
bakes them in skeleton space, listed in the animation's
`extras.skeleton_space_blocks`. `MotionIndex` is the `motion\` folder
read once for `browse`, pairing clips and skeletons by attribute key;
`sweep` is `motion --sweep`; `cli_model` and `cli_motion` are the two
commands. `selftest()` runs hand-made packs, and with a disc the six
party field packs.

**`browse.ModelBuilder`** is the model pass of `build_browse` lifted into
a class so that `cli.py model` and `browse` build the same `.glb` from one
`.lex` (atlas, companions, skeleton).

**`lex.to_glb`** now hangs a merged face on body block 48
(`FACE_PARENT_BLOCK`, the game's `ACT_JOINT_HEAD`): one small node per
face weight group under that block, so the face follows the head.
Before, face vertices were bound to the armature's root and stayed put.

**`docs/gallery/render_motion.py`** (run inside Blender) and
**`docs/gallery/make_gif.py`** (Pillow) make the front-page GIFs; the
commands are at the end of MOTION.md. They are not part of the kit's
requirements.
