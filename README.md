# Xenosaga Episode I Python Extractor

Pure-Python asset extractor for **Xenosaga Episode I — Der Wille zur Macht
(USA, SLUS-20469)**. Companion to
[Xenosaga3PythonExtractor](https://github.com/LinuxJessi/Xenosaga3PythonExtractor) —
same goals, same spirit, different disc internals (Episode I predates the
Episode III tooling by four years and shares none of its container formats).

**Zero dependencies.** Episode I's disc is plain ISO9660, so unlike the
Episode III kit there is no 7-Zip requirement — the standard library does
everything, including reading the raw image past the filesystem (see
"Layer 1" below).

Use your own legally obtained disc dump. This tool ships no game data.

## Documentation

| Doc | What's in it |
|---|---|
| [docs/BROWSING.md](docs/BROWSING.md) | guided tour of the extraction output — what's where, what to open first, grep recipes |
| [docs/JAVA.md](docs/JAVA.md) | the headline find: the game's cutscenes are compiled **JDK 1.1 Java**, and how to decompile them |
| [docs/FINDS.md](docs/FINDS.md) | easter eggs and dev leftovers — staff work folders on retail disc, debug tools inside shipped cutscenes, Gamera |
| [docs/HISTORY.md](docs/HISTORY.md) | Monolith Soft historical context: what this disc records about the studio in 2002 |
| [docs/FORMATS.md](docs/FORMATS.md) | byte-level format reference (ARX, XTX, LEX, FL00, SPU streams, SMD/SWD, PSS) with verification evidence |
| [docs/MODDING.md](docs/MODDING.md) | **how the repack layer works** — in-place ISO patching, the ARX compressor clone, texture recolors (CLUT + raw-CT32), the 12-carrier sweep, the translator text pipeline, PINE debugging, and how to add a command to CLI/GUI/packaged builds |

## Quick start

```sh
python cli.py list    --iso "Xenosaga Episode I (USA).iso"
python cli.py extract --iso "Xenosaga Episode I (USA).iso" --out out/ --code
python cli.py classes --iso "Xenosaga Episode I (USA).iso" --out out/
python cli.py browse  --out out/          # after extract: textures->PNG, voice->WAV, ...
python cli.py verify  --out out/
```

`extract` produces:

```
out/
  dump/chain0/        8,573 files — system + field data (in-game "data\" tree)
  dump/chain1/          349 files — streaming voice (.vds/.vdm), scenes
                        (.fpk/.arc/.evt), TOC-indexed movies (movie\mpeg2\*.pss)
  dump/layer1/           58 movies carved from outside the filesystem
  browse/code/        SLUS_204.69 + 5 overlays + IOP modules   (--code)
  manifest.csv        one row per object: area, path, sector, size,
                      compressed flag, bigfile + local offset
```

`verify` re-checks every manifest row on disk: size, ARX magic on compressed
entries, and content magic for `.pss` / `.jpg` / `.ipu`.

## Repacking (modding)

The kit writes mods back too. The disc is unusually friendly to this: plain
ISO9660, all data inside contiguous bigfiles, one binary TOC per chain — so
patching is in-place at computed offsets, never an image rebuild.

```sh
# replace any TOC object (content given uncompressed; ARX applied as needed)
python cli.py patch --iso GAME.iso --out MODDED.iso \
    --set 'chain0:char\pc\kosmos.xtx=my_kosmos.xtx'
```

* `arx.compress` (in `arx.py`) is a byte-perfect clone of Monolith's
  original packer — 2,094 of the 2,095 compressed objects on the USA disc
  recompress **byte-identically** (the last differs only in a frequency
  tie and round-trips exactly).
* `repack.py` patches objects in place, updates the TOC's csize/usize
  fields, refuses writes past an object's sector allocation, and verifies
  every write by read-back.
* `pinkhair.py` is a worked example: it recolors KOS-MOS's hair — both the
  CLUT palettes and the raw-CT32 strand sheets — and byte-sweeps the whole
  disc for embedded texture copies (battle bundles `yamamoto\pc\*.bin`,
  re-framed scene bundles `scene\cf*.a`).
* `textpack.py` is the translator pipeline: `text-export` pulls all 914
  text objects (588 `.txt`, 326 U.M.N. mail `.uml` text slots) into an
  editable UTF-8 tree with per-file byte budgets; `text-import` re-encodes
  to Shift-JIS, validates every file, and writes a patched ISO.

How all of it works — down to the byte — is written up in
[docs/MODDING.md](docs/MODDING.md), deliberately so the tools can be
modified or reimplemented without archaeology.

### Screenshots — the pinkhair mod, in-game

Not a mockup — this is `python cli.py pinkhair` output booted for real
(PCSX2), proving the repack layer round-trips through every carrier at
once: model textures, the battle HUD, and field cutscenes together.

| | |
|---|---|
| ![KOS-MOS model close-up, recolored hair](docs/images/pinkhair-model-closeup.png) | ![Battle screen, recolored KOS-MOS landing a HI-CRITICAL](docs/images/pinkhair-battle.png) |
| Field/party model — the raw-CT32 strand sheets patched | Battle bundle (`yamamoto\pc\*.bin`) — same recolor, independent carrier |

![Field scene with the recolored party](docs/images/pinkhair-field-party.png)
*Vector Industries scene — the field model and battle model both carry the
edit, which is the point: `pinkhair.py` sweeps and patches all 12 embedded
copies of the character texture in one pass, not just the obvious one.*

### The translator kit

`textpack.py` is the piece built for full translation projects, not just
cosmetic mods: `text-export` pulls **all 914 in-game text objects** — 588
`.txt` scene/menu scripts plus 326 U.M.N. mail `.uml` slots — into a plain
UTF-8 tree, one file per object, each annotated with its Shift-JIS byte
budget so a translator knows exactly how much room they have before
`text-import` will reject a line. Re-encoding, budget validation, and the
patched-ISO write are all handled by the same command; nothing is written
until every file passes. (Cutscene dialogue is a separate case — it's
compiled into the Java `.evt` scripts, not plain text objects; see
[docs/MODDING.md](docs/MODDING.md) for that path.) Together with
`repack.py`'s in-place patching, this is a complete loop for a fan
translation: export text, translate, import, verify with `arx.compress`'s
byte-identical round-trip that nothing else on the disc moved.

## GUI

Prefer clicking to typing? `python gui.py` starts a local web GUI (stdlib
only — no Flask, no install), opens your browser, auto-detects the disc image,
and gives you all nine commands (list / extract / classes / browse / verify /
recolor KOS-MOS / export & import text for translation / patch)
as cards with a built-in file picker and a live log that streams the
underlying `cli.py` output. It shells out to the same CLI, so the two never
drift.

### Double-click launchers (source checkout)

Have Python 3.9+ installed? Just double-click the launcher for your OS — no
build step, no dependencies (the kit is stdlib-only). Same scheme as the
Episode III kit:

| OS | Double-click |
|---|---|
| Windows | `launch.bat` |
| macOS | `launch.command` (first time: right-click → Open) |
| Linux | `launch.sh` |

Each one `cd`s next to itself, finds your Python, and runs `gui.py`.

### Packaged double-click bundle (no Python needed)

`python build.py` (a thin wrapper over `packaging/xenosaga1-extractor.spec`)
freezes the kit into a **self-contained bundle** — the same shape as the
Episode III kit. The bundle embeds its own **Python 3.12** runtime and (in
release builds) a portable **ffmpeg** under `tools/`, so end users install
nothing at all — the "Python 3.9+" note above applies only to running from
a source checkout. Output names carry the platform, so builds coexist in
`dist/`:

```
dist/Xenosaga-I-Extractor-windows/    (built on Windows — zip and ship)
  Xenosaga-I-Extractor.exe            the GUI — double-click to launch
  xeno-cli.exe                        the engine the GUI drives
  _internal/, python3X.dll            shared runtime

dist/Xenosaga-I-Extractor.app         (built on macOS — double-click this)
dist/Xenosaga-I-Extractor-macos/      (same build, bare folder form)
```

Both executables live **side by side** (inside the `.app` they share
`Contents/MacOS/`), so the GUI always finds its engine beside itself. It's
deliberately one-folder, not one-file (one-file unpacks to `%TEMP%` at launch,
which antivirus heuristically flags), with `upx=False` and a windowed GUI /
console CLI, mirroring the III kit's AV-friendliness choices.

The macOS app is ad-hoc signed by PyInstaller. Built locally it opens with a
plain double-click; downloaded from the internet, Gatekeeper quarantines it —
right-click → **Open** once (or `xattr -dr com.apple.quarantine
Xenosaga-I-Extractor.app`), after which it double-clicks normally. Zip it
with `ditto -c -k --keepParent` so symlinks and exec bits survive.

PyInstaller does **not** cross-compile: run the build under a Windows Python
for `.exe` files, on a Mac for the `.app`, or under Linux for ELF binaries
(`.github/workflows/release.yml` builds the Windows and macOS zips in CI).
`python build.py --clean` wipes `build/` and this platform's `dist/` output
first. Requires `pip install pyinstaller`.

## How the disc is organized

The visible ISO9660 filesystem (layer 0, ~4.25 GB) holds the executables and
seven opaque bigfiles. Everything else the game reads lives inside those
bigfiles, addressed through two **binary TOC files**:

| TOC | Chain | Contents |
|---|---|---|
| `XENOSAGA.00` | `.00 + .01 + .02` (1.43 GB) | the whole `data\` tree: textures, models, scripts, menus, small `.ipu` movies |
| `XENOSAGA.10` | `.10 + .11 + .12 + .13` (2.82 GB) | streamed voice audio, scene packs, 45 video-only `.pss` movies |

A TOC's sector numbers count from the **start of the TOC file itself**, and
the data region runs seamlessly through its sibling bigfiles in order — the
same "region chain" model Episode III later used with its `X3.*` files and
text `Lba*.txt` tables, just with a binary table and no per-disc split.

### TOC binary format

Reverse-engineered from scratch; validated by parsing both TOCs to the byte
(zero desyncs across 8,922 entries) and by matching every recovered path
against the engine's own `data\...` string literals in the unstripped ELF.

```
toc      = [u8 data_base_sector] entry* [0x00] filler
file     = [b]        [name: b-1 bytes]        [u24le sector] [u32le size]
cfile    = [b | 0x40] [name: (b&0x3f)-1 bytes] [u24le sector] [u32le csize] [u24le usize]
dir      = [b | 0x80] [u8 pop_count]           [name: (b&0x7f)-2 bytes]
filler   = "MONOLITHSOFT Xenosaga Episode.1\0" repeated, phase-locked to offset % 32
```

* `dir` pops `pop_count` path levels, then pushes its name — a serialized
  pre-order walk of the directory tree.
* `cfile` entries (2,095 of them) are compressed; the stored payload begins
  with an `ARX\0` header carrying uncompressed + compressed sizes that match
  the TOC fields. **ARX is fully reverse-engineered both ways** — `arx.py`
  decompresses (applied transparently by `browse`) and compresses (a
  byte-perfect clone of Monolith's own packer, used by the repack layer;
  see [docs/FORMATS.md](docs/FORMATS.md)). `extract` still writes
  compressed entries as stored and `verify` still checks the stored form.
* The unused tail of each TOC is filled with a repeating, offset-phase-locked
  `MONOLITHSOFT Xenosaga Episode.1` string. Charming, and a clean
  end-of-entries sentinel.

### Layer 1: the movies outside the filesystem

The disc is a 8.47 GB dual-layer DVD but the ISO9660 volume only describes
layer 0. The other ~4.2 GB — all of layer 1 — is wall-to-wall MPEG-2 program
stream: **58 full movies with muxed MP2 audio**, reached by the game via raw
sector addressing. (The 45 TOC-indexed `movie\mpeg2\*.pss` files in chain 1
are a separate, video-only set.)

`carve.py` recovers them without any index: a movie start is a
sector-aligned pack header + system header whose SCR (system clock) is below
one second — the clock resets at the front of each movie, which separates
true starts from mid-stream repeated system headers. Output names encode the
absolute LBA (`layer1_000_lba2077586.pss`) so the mapping to real titles can
be recovered later from the game's playback code.

These play directly in VLC / ffplay and convert with plain ffmpeg.

## Notable disc facts

* **Every ELF on the disc is unstripped** — SLUS_204.69 and all five
  overlays, 8,115 named functions total. (Episode III shipped fully
  stripped; Episode I is a decompiler's gift.)
* **The engine embeds a JVM**: 803 `Java_xeno_*` JNI bridge symbols
  (`Java_xeno_Camera_getFov__`, `Java_xeno_Movie_start__I`, ...). Event
  scripting is **actual Java**: every `.evt` file is an `FL00` container of
  real `0xCAFEBABE` class files — format 45.3 (JDK 1.1), compiled with a
  stock Sun javac, `SourceFile`/`LineNumberTable` debug attributes intact.
  `system.evt` even ships the runtime (`java/lang/Object`, `String`,
  `StringBuffer`). The `classes` command lifts them all out (~2,200 unique
  classes across 480 containers) into a javap-ready package tree — decompile
  with `javap -c -p` or Krakatau/CFR. The FL00 table mixes real classes with
  24-byte stub records, so `evt.py` walks the class-file structure itself to
  carve exact byte ranges and recover true fully-qualified names.
* The IOP module set includes `DEV9.IRX`, `HDD.IRX`, `PFS.IRX`, `SMAP.IRX` —
  PS2 HDD (and network adapter!) support, in a 2002 USA release.
* Mastering date 2002-12-02; the TOC filler string and the developer-named
  directories (`endou\`, `yajima\`, `matumoto\`, `karakama\`, `simajiri\`,
  `tanaka\`, `nisimori\`, `yamamoto\`) survive in retail.

## Module map

| File | Role |
|---|---|
| `toc.py` | binary TOC parser (grammar above) |
| `iso9660.py` | minimal pure-Python ISO9660 reader (PVD, dir walk, mmap reads) |
| `chains.py` | the two bigfile chains; spanning reads across file boundaries |
| `carve.py` | layer-1 movie scanner/carver (SCR-validated boundaries) |
| `evt.py` | Java class-file carver for `.evt` FL00 event containers |
| `arx.py` | ARX codec (word dictionary coder; decompressor ported from xenotool, compressor is a byte-perfect clone of Monolith's packer) |
| `repack.py` | in-place ISO patcher: TOC field updates, allocation checks, read-back verification |
| `pinkhair.py` | worked modding example: KOS-MOS hair recolor (CLUT edits + raw-CT32 strand pixels) with disc-wide carrier sweep |
| `textpack.py` | translator pipeline: export all 914 text objects to editable UTF-8 (+ byte budgets), re-encode/validate/import to a patched ISO |
| `browse.py` | asset converters: `.xtx`→PNG (lex-material colours), voice→WAV, text→UTF-8, movies→MP4 |
| `cli.py` | `list` / `extract` / `classes` / `browse` / `verify` / `patch` / `pinkhair` / `text-export` / `text-import` |
| `gui.py` | local web GUI over the CLI (stdlib `http.server` + SSE) |
| `evt_unpack.py` | standalone: FL00 → raw + normalized (JVM-loadable) class trees from an extracted `dump/` |
| `class_map.py` | standalone: parse a class tree into a machine-readable JSON class map |
| `dump_symbols.py` | standalone: ELF symtab dump of SLUS + overlays → `symbols.csv` |
| `launch.bat` / `launch.command` / `launch.sh` | double-click launchers for the source checkout (Windows / macOS / Linux) |
| `build.py` | PyInstaller build wrapper → self-contained `dist/` bundle |
| `packaging/xenosaga1-extractor.spec` | one-folder COLLECT of GUI + engine (+ macOS `.app`) |
| `.github/workflows/release.yml` | CI: Windows + macOS release zips on tag push |

## Asset formats decoded so far

* **ARX decompression** (`arx.py`, applied transparently by `browse`).
  Word-oriented dictionary coder, ported from
  [Lakuwu's xenotool](https://github.com/Lakuwu/xenotool) — this unlocks
  the 810 compressed `.xtx`, the `mtnpack/*.arc` packs and every other
  `ARX\0` blob on the disc. (`extract` still writes them as stored, and
  `verify` still checks the stored form.)
* **`.xtx` textures → PNG** (`browse`). The file is a raw GS memory image:
  sub-images composed onto a CT32 canvas, unswizzled as PSMT8, 256-colour
  CSM1 palettes in 16x16 tiles (format understanding owed to xenotool).
  When a sibling `.lex` model exists, its material headers say which
  palette tile colours which UV region — character/NPC/object textures come
  out in true colour. Lex-less menu/backdrop textures (casino, dev-folder
  art) park their CLUT in an unused corner of the same canvas, addressed by
  GS block pointers in the consuming overlay's texture descriptors; the
  decoder finds these by scanning block-aligned 16x16 tiles (alpha-legal,
  colour-rich, conventional corners first). Remaining grayscale cases:
  multi-palette sprite atlases whose per-region mapping lives in overlay
  data / model VIF streams.
* **`.vds`/`.vdm` streamed audio → WAV**. Headerless PS2 SPU ADPCM
  (decoder verified against ffmpeg's `adpcm_psx` to ~1 LSB), **stereo,
  block-interleaved every 0x400 bytes**, 48 kHz (the constant the scene
  scripts pass to `xeno.Sound.streamPlay`). Decoded as mono these sound
  slow/echoey/choppy — the fix was the interleave, not the rate. Genuinely
  mono streams are auto-detected per file. Cutscene music+voice mixes are
  streamed here too (`s29xxxx` and friends).
* **BGM: sequenced, not streamed** — Procyon Studio's format: `.smd`
  sequences (`smdm`, embedded metadata literally credits
  `Yasunori Mitsuda / PROCYON STUDIO`) driving `.swd` wave banks (`swdm`).
  Field areas mostly play ambience stubs (the game's famous silence);
  battle/menu themes live in `chain0/yamamoto/snd/smd/` and
  `chain0/sound/smd/`. The `banks` kind carves every bank instrument to
  WAV (`browse/soundbanks/…`, audition rate 32 kHz — true pitch is
  per-note in the sequence) and writes `smd_catalog.csv` naming every
  sequence. Faithful playback needs an SMD synth (open thread); the
  full-production track versions are in the movies.
* **Game text → UTF-8** (`browse`). The `.txt` files are Shift-JIS with
  inline `\NN` engine control codes; they're transcoded so editors show
  the Japanese/English instead of mojibake.
* **`.evt` event scripts → `.class`** (`classes` command; see above).
* **`.pss`/`.ipu` movies → MP4 with sound** via ffmpeg. Movie audio hides
  in MPEG private stream 1 as a Sony ADS payload (`SShd`: SPU ADPCM,
  48 kHz stereo, 0x400 interleave — the game's own header confirming the
  `.vds` layout) which ffmpeg misparses; the kit demuxes and decodes it
  itself, then muxes AAC into the MP4. Each movie with audio yields
  **three files** for fan-work (undub/redub) convenience: `name.mp4`
  (muxed), `name.video.mp4` (same encode, no audio), `name.audio.wav`
  (the demuxed track as PCM). Release bundles ship a portable
  ffmpeg in `tools/` (BtbN GPL build on Windows, martin-riedl.de static
  build on macOS — see `tools/TOOLS.txt`), so this works with zero
  installs; source checkouts use any ffmpeg on PATH, or drop one into
  `packaging/tools/` before `python build.py`. Raw `.pss` plays in
  VLC/ffplay (video only).

## Not done yet

* Real names for the 58 layer-1 movies (needs the playback table from the
  ELF/overlays). Their audio is solved — see "Movies" above.
* An SMD sequencer/synth for faithful BGM rendering (sequences + bank
  samples + ADSR — a real project; instrument WAVs and the catalogue are
  already extracted by `banks`).
* Full material coverage for texture palettes: materials embedded inside
  `.lex` VIF vertex streams and overlay texture descriptors (multi-palette
  sprite atlases like the casino's `base.xtx`).
* Geometry: `.lex` meshes decode far enough for materials only — full
  model export (xenotool does OBJ) is out of scope here.
* Format decoders for `.esd`/`.esp` scripts, `.sed` SFX banks, `.jnt`
  skeletons.
