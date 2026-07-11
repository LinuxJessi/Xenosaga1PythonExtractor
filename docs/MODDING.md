# Modding Xenosaga Episode I — how the repack layer actually works

This is the mechanics document: everything you need to modify the tools —
or write your own — **without re-deriving anything**. Byte-level format
reference lives in [FORMATS.md](FORMATS.md); this file explains how the
pieces fit and why they are built the way they are. Everything below was
established and verified on the USA disc (SLUS-20469) in July 2026.

The worked example throughout is the KOS-MOS hair recolor (`pinkhair.py`),
because it hit every layer: palettes, true-colour pixels, compression,
multiple embedded copies, and a re-framed container.

## 1. Why patching in place works at all

Three properties of this disc make modding unusually clean:

1. **Plain ISO9660.** Every file the ISO filesystem knows about is one
   contiguous run of sectors. Byte N of `XENOSAGA.01` is always at
   `lba(XENOSAGA.01) * 2048 + N` in the image. No fragmentation, ever.
2. **All game data lives in bigfile chains** (`chains.py`): chain 0 =
   `XENOSAGA.00 + .01 + .02`, chain 1 = `XENOSAGA.10 + .11 + .12 + .13`.
   A chain is addressed as one virtual byte space; a TOC at the head of
   each chain (`XENOSAGA.00` / `.10`) maps game paths to sector offsets
   **relative to the chain start**.
3. **Objects are sector-aligned with slack.** An object's *allocation* is
   the gap from its start sector to the next object's start sector. As
   long as a replacement fits its allocation, nothing else moves.

So a mod is: overwrite the object's bytes at the computed image offset,
update the TOC entry's size fields, done. `repack.py` implements exactly
this and nothing more:

- `read_entry(iso, chain, path)` — read + transparently ARX-decompress one
  object straight from an ISO.
- `patch_iso(iso, {(chain, path): payload})` — write payloads back **in
  place** (always run it on a copy). Compressed entries are re-ARX'd
  automatically; the TOC's u32 csize / u24 usize (at `entry.fields_off + 3
  / + 7` inside the TOC file) are patched; oversize replacements are
  refused; every write is verified by read-back.

The TOC grammar is in `toc.py`'s docstring (validated to the byte over all
8,922 entries). `TocEntry.fields_off` is the repack hook: the byte offset
of the entry's sector/size/usize fields within the TOC file.

## 2. ARX — the compressor is a byte-perfect clone

ARX (header `ARX\0`) is a word-oriented dictionary coder: a 30-entry LUT
of common u32 words in the header, then a stream where control bits pick
"literal word follows" (bit 0) or a prefix code selecting a LUT entry
(bit 1 + 2/4/6/8 code bits → slots 0-1 / 2-5 / 6-13 / 14-29, i.e. 3/5/7/9
control bits total per LUT hit vs 33 for a literal). Decoder and encoder
live in `arx.py`, ~80 lines each.

The encoder's LUT is the 30 most frequent u32 words of the payload, most
frequent first, **ties broken by first occurrence in the payload**. That
tie-break was found by comparing against retail: with it, 2,094 of the
2,095 compressed objects on the disc recompress **byte-identically**
(`chain1/mtnpack/SCE02004D.arc` differs only within the LUT order of
equal-frequency words, and round-trips exactly). Monolith's 2002 packer
evidently used the same greedy scheme.

Why byte-identity matters practically: recompression of an *untouched*
region is a no-op, so any diff between a patched ISO and retail is your
edit and nothing else — verification becomes `cmp`.

Header fields: `u32 size_orig, u32 size_comp (whole blob incl. header),
u32 0`. The TOC's csize/usize mirror these.

## 3. Textures — two different kinds of "color" in one canvas

An `.xtx` is a raw GS memory image (see FORMATS.md for the exact header).
Decoding composes sub-images onto a CT32 canvas; the canvas holds **both**:

- **PSMT8 indexed regions** — 8bpp pixels (swizzled) + 256-entry CLUT
  tiles stored as 16x16 CT32 tiles at canvas coordinates, CSM1 entry
  order. Which mesh uses which CLUT comes from the paired `.lex` mesh
  headers (palette byte → canvas coords formula in FORMATS.md), but NOT
  all materials are visible there — some hide in VIF vertex streams.
- **Raw CT32 true-colour regions** — 32-bit RGBA pixels used directly.
  KOS-MOS's long hair-strand sheets are this (canvas x256-383, y0-127 in
  `kosmos.xtx`). **Signature:** decodes to noise as PSMT8, and a 16x16
  tile there has ~250 distinct colours (a CLUT tile is capped at 256 for
  the whole tile and materials never point at it).

Recoloring therefore has two mechanisms:

- **CLUT edit** (hairline, face shading): rewrite palette entries. A
  palette entry is 4 bytes `R G B A` (PS2 alpha is 7-bit: 0x80 = opaque)
  stored raster inside the tile's sub-image — per-entry edits need no
  swizzle or CSM1 awareness at all.
- **Pixel edit** (strand sheets): hue-rotate any pixel matching the "hair
  blue" predicate. Because the edit is per-pixel and position-independent,
  GS swizzling is irrelevant — you can transform the bytes wherever they
  are stored.

The predicate + hue rotation used by `pinkhair.py` (`_is_hair_blue`,
`_recolor_rgb`): light strands `b>140 and b>r+40 and g>r`, CLUT/shadow
blues `b>90 and b>r+30 and b>=g`; rotate hue in HLS keeping luminance,
saturation nudged ×1.1. Changing the target colour is the `hue` parameter
(0..1: 0 red, 0.13 gold, 0.33 green, 0.75 purple, 0.92 pink). Recoloring a
*different* character = redo the curation: decode their textures, list
CLUT tiles via `lex_materials`, identify hair vs armor tiles by eye
(render previews), find any raw-CT32 regions with the noise+distinct-count
signature.

## 4. One texture, twelve carriers — the sweep

The disc embeds copies of `char/pc/kosmos*.xtx` in other containers:

| carrier | form |
|---|---|
| `char/pc/kosmos{,1,2,_h,_h1,_h3,_h5}.xtx` | the standalone files |
| `yamamoto/pc/kosmos{,1}.bin` (battle bundles) | **byte-identical** embed (section table `u32 n, u32 total, u32 off[n]`; lex @0x20, XTX by magic) |
| `scene/cf{0210,0740,1800,3140}.a` (per-scene bundles) | **re-framed**: same canvas bytes but with sporadic 4-byte zero words inserted *and* elided (~2020-byte effective row stride vs the canvas's 2048); non-zero words stay in order |

Patching only the standalone files looks complete and isn't — the opening
Encephalon-sim tutorial (`ST0210`) renders KOS-MOS from `cf0210.a`. The
sweep in `pinkhair.py` handles all forms with three granularities:

1. **64-byte rows / 512-byte strand segments** — plain `bytes.replace`,
   catches standalone files and byte-identical embeds.
2. **16-byte quarter-rows** — carrier *detection* anchors (a file with ≥4
   anchors carries a copy) and span bounding.
3. **4-byte values + a zero-tolerant walker** — for re-framed carriers.
   CLUT entries are replaced by exact value within the anchored span
   (safe: verified the hair ramp shares no exact RGBA word with any other
   tile in the canvas — re-check this if you change tiles!). Pixel rows
   use `patch_reframed_row`: anchor a 16-byte window, then two-pointer
   walk both directions — match → replace old with new, cf-side zero word
   → skip it, canvas-side zero word → skip that, anything else → stop.
   Recovers ~99% of strand pixels; the remainder are rows whose anchor
   windows the framing split.

**Completeness rule:** after any change, re-sweep the whole disc at a
finer granularity than you patched with, and confirm the carrier list is
exactly what you patched. `manifest.csv` + `arx.decompress` + `bytes.find`
is all it takes (see the sweep scripts pattern in `pinkhair.py`).

## 5. Text — the translator pipeline (`textpack.py`)

Two text-object families, 914 objects, all uncompressed:

- **`*.txt` (588)** — whole-file Shift-JIS (cp932): scene scripts (with
  dev comments), U.M.N. event dialogue. May grow up to the object's
  allocation (the manifest records the budget; `patch_iso` updates the
  TOC size when it changes).
- **`*.uml` (326)** — U.M.N. mails: `0x60-byte header | Shift-JIS text
  region, space-padded, ending at the first NUL | binary tail` (a small
  record + the mail's attached JPEG with Photoshop 8BIM blocks; the u32
  at header +0x20 points into those resources). Only the text region is
  editable and its length is **fixed** — imports are space-padded back to
  exactly the original length, header and tail preserved verbatim.

Workflow: `text-export` → edit the `.utf8.txt` tree (any editor, any OS —
BOMs are tolerated, CRLF/LF preserved exactly) → `text-import` validates
every file against its byte budget (Shift-JIS bytes — kana/kanji cost 2)
and writes a patched ISO only when *all* files pass.

Encoding safety nets, in order:

1. A few mails embed raw JIS symbol bytes (the ★●○ family) that strict
   cp932 rejects — exported as `⟦XX⟧` hex markers. U+27E6/27E7 are not
   encodable in cp932, so markers can never collide with game text; leave
   them in place and import restores the original bytes.
2. Every exported file is round-trip self-checked (decode → re-encode ==
   disc bytes). Anything unstable (cp932 has duplicate NEC/IBM mappings)
   falls back to a verbatim `.raw` copy instead of silently corrupting.
3. Import reconstructs `.uml` objects around the fixed text region even
   for `.raw` exports, so headers/attachments can never be truncated.

**Where the rendered dialogue actually is** — two engines, two homes:

- **U.M.N. conversations** (the Connection Gear chats, `umn/event*.txt`,
  `<speech>/<char>/<msg>` markup with `\15\2`-style control codes) and
  the `.uml` mails are plain text objects — the pipeline above covers
  them. No copy of these lines exists in any Java class (verified by
  string-sweeping all ~2,200 carved classes), so the `.txt` really is
  what the U.M.N. viewer renders.
- **Scene/cutscene dialogue** is compiled into the `.evt` Java classes
  as constant-pool strings. The parallel `scene/cf*.txt` planner sources
  (loader scripts, dev comments — some with dialogue-looking copies) are
  **never read for rendering**: translating them changes nothing on
  screen. Any dialogue translation that "works" in the text tree but not
  in-game has fallen into exactly this trap.

A full dialogue translation needs a class-file string rewriter plus an
FL00 container rebuilder — the formats are documented (FORMATS.md,
JAVA.md, `evt.py` carves classes by structure), but the writer does not
exist yet; length-changing edits shift the constant pool. **Same-length
edits, however, ship today**, with the kit alone:

### Worked example: one dialogue line to French

Target: the first line the game renders — `"Virtual Tutorial"` in the
Encephalon-sim tutorial, `scene/ST0210.evt`, chain 0 (the same scene as
the §4 texture story). `"Tutoriel virtuel"` happens to be the same 16
characters, so the swap is structurally free:

```python
from repack import read_entry, patch_iso

PATH = r"scene\ST0210.evt"
data = read_entry(iso, 0, PATH)              # uncompressed FL00 container
patched = data.replace(b'"Virtual Tutorial"', b'"Tutoriel virtuel"')
patch_iso(iso, {(0, PATH): patched})         # run on a copy, as always
```

Verified in-game (USA disc, PCSX2). The rules that make this safe:

1. **Byte length must not change.** The string lives in a length-prefixed
   constant-pool entry; everything after the pool is index-addressed, not
   offset-addressed, so an in-place same-length swap disturbs nothing.
   Shorter translations: pad with spaces inside the quotes/line rather
   than shortening the entry.
2. **The string carries its layout verbatim.** The ST0210 entry is
   6 leading spaces (manual centering) + the quoted title + `\n` + NUL
   (the length prefix *includes* that trailing NUL — disc-wide quirk).
   Keep all of it; replace only the words.
3. **Stick to ASCII for now.** The encoding the renderer expects for
   non-ASCII constant-pool bytes (Shift-JIS vs Java's modified UTF-8) is
   not yet established — accents are unverified territory.
4. **No sweep needed.** Unlike textures (§4), each scene `.evt` has
   exactly one TOC entry disc-wide (`grep manifest.csv`) — one patch is
   complete.

Finding a line: unpack the containers (`evt_unpack.py --dump ... --out
...`), then `grep -r` the extracted classes for the on-screen text; or
grep the dumped `.evt` directly — scene containers are uncompressed, so
dialogue is plain bytes.

Smoke-testing the result costs nothing: with PCSX2 fast boot, the USA
disc lands on this exact ST0210 line in about a minute with **zero
input** — the patched line is literally the first thing the game shows
(boot-flow files `base.evt` / `system.evt` / ELF verified bit-identical
to retail while this happens; it is the game's own cold open). Boot the
patched ISO, read the first dialog box, done.

## 6. Debugging against the running game (PINE)

PCSX2's PINE socket (`flag-hunt/pine.py`, game-agnostic) is the ground
truth for "what did the game actually load":

- `read_block` dumps all 32 MB of EE RAM in under a second.
- Search the dump for old vs new byte signatures. A partially patched
  palette leaves an exact fingerprint (the hair fix was found because the
  in-RAM CLUT showed precisely the 30 row-patched entries pink and 91
  blue — the signature of the cf-bundle copy, not the standalone file).
- `write32/64` pokes prototype an edit live before you burn an ISO.
  GS-resident textures refresh on the next upload (scene change/dialog
  advance), so a poke may take a moment to show.
- Expect in-RAM CLUTs to be **tinted** copies (the engine bakes scene
  lighting into palettes at load: RGB shifted, alpha intact).

## 7. Adding a command to the kit (CLI + GUI + packaged builds)

A new feature has exactly four touch points; miss one and it works on
your machine but not in the release zips:

1. **Engine module** (`repack.py` / `textpack.py` / yours) — keep it
   stdlib-only and importable.
2. **`cli.py`** — a `cmd_<name>` function + entry in the subcommand table
   in `main()`. Lazy-import your module inside the function (keeps CLI
   startup instant).
3. **`gui.py`** — a `build_<name>(form)` builder returning
   `[*CLI_ARGV, "<subcommand>", ...]`, an entry in `BUILDERS`, and a
   `makeCard(...)` block in the boot script (field types: text with
   optional file/dir picker, select, checkbox). The GUI is one
   self-contained HTML string served by stdlib `http.server`; it shells
   out to the CLI, so the two can never drift. Test headless with
   `PORT=8931 python3 gui.py --no-browser` + `curl -X POST
   127.0.0.1:8931/preview/<name>`.
4. **`packaging/xenosaga1-extractor.spec`** — add lazily-imported modules
   to `HIDDEN` (PyInstaller's static analysis finds top-level imports on
   its own, but the entries are load-bearing for function-level imports).

Packaging (`build.py` → PyInstaller one-folder bundle) puts `gui` and
`xeno-cli` side by side; frozen `gui` finds the CLI as a sibling binary.
`.github/workflows/release.yml` builds Windows and macOS zips on tag push
(PyInstaller does not cross-compile — Linux users run from source or
`python build.py` locally; the launchers `launch.bat` / `launch.command` /
`launch.sh` cover all three OSes for source checkouts).

## 8. Verification checklist for any repack

1. Patched objects: read back from the new ISO, confirm your edit and
   `len(new) <= allocation` (repack enforces, but look).
2. Untouched neighbours byte-identical (`read_entry` old vs new).
3. Compressed entries: `arx.decompress(new blob)` round-trips.
4. Whole-disc re-sweep at finer granularity than you patched with (§4).
5. Boot it. PINE-dump RAM in the target scene and search for your bytes
   if anything looks wrong (§6).
