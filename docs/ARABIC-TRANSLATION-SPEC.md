# Xenosaga Episode I — Arabic translation: implementation spec

A self-contained handoff document. Written for an implementing agent (human
or model) with no prior context on this repo. Everything in §1–§3 is
**verified against the discs and binaries in this repo** (commands included);
§5 lists the open problems as bounded work packages with acceptance tests.

Target: *Xenosaga Episode I – Der Wille zur Macht* (USA), `SLUS_204.69`.
Goal: render translated Arabic text in-game — right-to-left, contextually
shaped, proportionally spaced — for the three text surfaces described in §2,
reusing this kit's existing export/patch pipeline.

---

## 0. Why Arabic is not "just another French"

The kit's [FRENCH-TRANSLATION-GUIDE.md](FRENCH-TRANSLATION-GUIDE.md) covers a
Latin-script translation riding the existing font. Arabic adds four problems
Latin never hits:

1. **Direction.** The engine renders glyphs left-to-right. Arabic reads
   right-to-left. The proven fan-translation strategy on fixed consoles:
   keep the engine dumb, do **all** bidi work at build time — shape, wrap,
   then reverse each rendered line so LTR drawing shows RTL text.
2. **Shaping.** Arabic letters change shape by position (isolated / initial
   / medial / final) and must connect. Solved at build time with
   *presentation forms*: each contextual shape becomes its own glyph slot,
   chosen by the insertion tool (`arabic-reshaper` does exactly this).
   The engine never knows shaping exists.
3. **Glyphs.** The disc font has no Arabic glyphs. We must draw ~100
   presentation-form glyphs and inject them into `font0.tex`/`font1.tex`
   (format: §3 — partially cracked, WP-1 finishes it).
4. **Mixed runs.** Names (KOS-MOS, U.M.N., Zohar) and digits stay LTR
   inside RTL sentences. `python-bidi` handles run ordering at build time.

Build-time order is fixed and non-negotiable:
**shape → bidi-reorder → wrap-to-pixel-width → reverse each line**.
Wrapping after reversal breaks words; wrapping before shaping mismeasures
joined forms.

---

## 1. Repo map (all paths relative to repo root `KOS-MOS ver. 2/`)

| Path | What |
|---|---|
| `Xenosaga Episode I - Der Wille zur Macht (USA)/Xenosaga Episode I - Der Wille zur Macht (USA).iso` | Retail USA disc (source of truth) |
| `Xenosaga Episode I - Der Wille zur Macht (USA)/out/` | Extracted tree: `manifest.csv` (TOC), `browse/` (files), `dump/` (raw chains), `CLASS-MAP.*` (Java event classes) |
| `Xenosaga Episode I - Der Wille zur Macht (USA)/out/browse/code/SLUS_204.69` | The EE ELF — **unstripped** (`nm` works; see §3.2) |
| `Xenosaga1PythonExtractor/` | The tool kit (all tools below live here) |
| — `textpack.py` | Text export/import pipeline for `.txt`/`.uml` (done, shipping) |
| — `repack.py` | `read_entry` / `patch_iso` — in-place ISO patching, TOC-aware |
| — `arx.py` | ARX decompress + byte-perfect recompress |
| — `evt_unpack.py`, `evt.py` | FL00/`.evt` container unpack, Java class carving |
| — `cli.py` / `gui.py` | Front-ends (`text-export`, `text-import`, …) |
| — `docs/FORMATS.md`, `docs/JAVA.md` | FL00 container + JDK 1.1 class-file notes |
| — `docs/MODDING.md` §5–6 | Same-length `.evt` patching (verified in-game) + PINE debugging |
| — `docs/FRENCH-TRANSLATION-GUIDE.md` | The Latin-script baseline workflow this spec extends |
| `flag-hunt/pine.py` | Game-agnostic PCSX2 PINE client (`read_block` dumps all 32 MB EE RAM in <1 s) |

Emulator: PCSX2 with PINE enabled; socket at `$TMPDIR/pcsx2.sock` on macOS.

---

## 2. Where text lives (three surfaces, three difficulty tiers)

### Tier A — U.M.N. chats & mails: **pipeline already done**
588 `*.txt` + 326 `*.uml` objects, plain Shift-JIS (cp932), uncompressed,
single-copy. `textpack.py export/import` round-trips them with per-file byte
budgets and all-or-nothing validation. Verified shippable for Latin;
for Arabic these files go through the new shaping pipeline (§4) first, then
the same import. `.txt` may grow to its sector allocation; `.uml` text
region is a **fixed-length** slot (space-padded) — zero slack.

**Trap (verified):** `scene/cf*.txt` files look like dialogue but are
planner/dev sources the renderer never reads. Translating them changes
nothing on screen. The real scene dialogue is Tier B.

### Tier B — scene/cutscene dialogue: **inside JDK 1.1 Java class files**
Compiled into `.evt` FL00 containers as `CONSTANT_Utf8` constant-pool
strings. Today only **same-byte-length** swaps ship (worked example in
MODDING.md §5, verified in-game; strings carry leading spaces for centering,
trailing `\n` + NUL counted in the length prefix). Arbitrary-length rewrites
need WP-3 (class rewriter + FL00 rebuilder). Key facts making WP-3 tractable:
- Class files are *index*-addressed, not offset-addressed — a constant-pool
  entry may change length freely as long as the whole class is re-serialized.
- `.evt` scene containers are uncompressed and single-copy on disc (no
  disc-wide sweep needed, unlike textures).
- The hard ceiling is the object's ISO sector allocation (`manifest.csv`
  budget; `patch_iso` updates TOC size within it).
- Strings are Java **modified UTF-8** in the pool. The renderer's expected
  encoding for bytes ≥0x80 is unverified; for Arabic this is moot — we
  choose our own single-byte codepage (§4.2), so translated strings are
  written as raw bytes in our codepage, wrapped in a valid Utf8 entry.
  (Modified UTF-8 would 2-byte-encode values ≥0x80 — WP-3 must test
  whether the runtime reads the pool as raw bytes or decodes UTF-8;
  the same-length ASCII experiments can't distinguish these. See WP-3
  acceptance test.)

### Tier C — engine/menu text: **in the ELF and overlays**
Menu labels, battle UI, item/skill names. Not yet inventoried. The ELF is
unstripped and statically linked; strings are findable with `strings`/graph
xrefs. Editing: same-length in place is trivial; longer strings need
pointer repointing inside the ELF (static binary — pointers are absolute
EE addresses in `.data`, patchable once found). Bounded by WP-5.

### Tier D — text baked into textures & video
Location cards, menu art (XTX — format cracked, repack pipeline exists,
**12-copy disc sweep required** for shared textures, see xenotool notes);
pre-rendered `.pss` movies in `out/dump/layer1/` may carry burned-in
English. Inventory in WP-5; re-authoring is art work, not engine work.

---

## 3. The font system (the critical path)

### 3.1 Font files — format ~70 % cracked (this repo, 2026-07-18 session)

`data\font0.tex` and `data\font1.tex` (referenced by name in the ELF),
TOC chain 0 as `font0.tex`/`font1.tex`. Both exactly **492,096 bytes**;
contents differ (two faces — candidates: dialog vs. menu, or main vs.
`rubyFont` — WP-1 confirms).

Verified structure (`python3 -c "from repack import read_entry; ..."`):

```
0x00000          64-byte header: 12 zero bytes, then BE u16s
                 01 3C | 00 51 | 00 BC | 00 00 | 00 00 00 08, rest zero
                 (0x13C=316, 0x51=81, 0xBC=188 — meaning unassigned)
0x00040–0x780FF  glyph pixel data, encoding NOT yet decoded (see below)
0x78100–0x7823F  proportional-metrics table: 160 byte-pairs
                 (left_bearing, advance_width), e.g. 04 11 = lead 4, 17 px.
                 Full-width entries read 00 13. Matches the ELF's
                 xglFontCheckProportional / xglFontGetProportionalSize API.
                 160 slots ≈ the single-byte (ASCII+) glyph range.
```

Pixel-region facts (so the next agent doesn't repeat dead ends):
- Byte entropy 4.86 bits/byte — **not** compressed; `arx.decompress`
  rejects it at every plausible offset (not ARX).
- Autocorrelation of the zero-byte mask peaks broadly at **~255–257 bytes**
  (and its 512 harmonic) — a real periodicity, but none of these render as
  glyphs when tried linearly.
- Ruled out by rendering: linear 992×992 4bpp; 126-byte cells as 24×21/2bpp
  (LSB and MSB), 12×21/4bpp (both nibble orders), 21×24, 28×18; 256-byte
  cells as 16×16/8bpp and 32×16/4bpp; per-row planar and byte-interleaved
  2bpp variants. Rendered rows show correlated blank bands → row
  granularity is real; the **within-row/column pixel order is swizzled**,
  consistent with a GS-native (PSMT4-family) pre-swizzled layout.

### 3.2 The engine API — unstripped symbols (the fastest route to truth)

`out/browse/code/SLUS_204.69` is `not stripped`. The complete font renderer
is symbol-named. Key symbols (from `nm`, addresses are EE VAs):

```
0021b5b0 T xglFontLoad              ← reads font*.tex: THE format decoder
0021b620 T xglFontInitial
0021ae48 T xglFontReloadTexture     ← GS upload path (format/PSM constants)
00218ab0 T xglFontGetKanjiClutUV    ← glyphs drawn as CLUT'd textured quads
0021ab78 T xglFontCheckProportional ← reads the 160-entry metrics table
0021ac78 T xglFontGetProportionalSize
0021b568 T xglFontGetStringWidth    ← the wrap-width oracle (WP-4 uses this)
0021b328 T xglFontGetStringWidth2
0021ad60 T xglFontAscii2Euc         ← charset: ASCII→EUC-JP index mapping!
00219138 T xglFontPrint   00219188 T xglFontPrintDirect
002190e8 T xglFontPrintf  00218d70 t xglFontPrintSub
0021b130 T xglFontFlush   00219be8 t xglFontFlushCore
00275160 T MenuFontLoad   0027df60/0027dfc0 t FontTexReload/FontTexChange
0025c2e0 t rubyFont       00883a00 b FontImage   (bss: decoded font in RAM)
00254970 t FontTestP0/P1/P2, 00254f28 t FontTestLine
       + ELF string: "Proportional font auto linefeed test"
```

Load-bearing conclusions already safe to rely on:
- **The font is indexed via EUC-JP** (`xglFontAscii2Euc`): single-byte
  ASCII is remapped into the EUC table before lookup. The codepage plan
  (§4.2) therefore targets the *single-byte glyph range* backing those 160
  metrics entries.
- **Proportional rendering exists and is table-driven** — Arabic's variable
  widths are natively supported once the metrics table is rewritten.
- **The engine has auto line-feed** for proportional text. For RTL this is
  a *hazard*, not a feature: a pre-reversed line that auto-wraps breaks in
  the wrong place. The build pipeline must emit lines measurably narrower
  than every textbox and carry explicit `\n` (§4.3, WP-4).
- Text is drawn as textured quads from a GS-resident font texture
  (`GetKanjiClutUV`), refreshed on scene change (`FontTexReload`).

### 3.3 Two independent attack routes for the remaining format unknowns

Route 1 — static: decompile `xglFontLoad` + `xglFontReloadTexture`
(unstripped MIPS, ~straightforward). The GIF/BITBLTBUF setup in the reload
path names the exact PSM, buffer width, and swizzle; the load path names
the header fields and per-glyph addressing.

Route 2 — dynamic (no RE required): boot the game in PCSX2, pause on any
dialog, `flag-hunt/pine.py read_block` the 32 MB EE RAM, and read the
decoded font at `FontImage` (0x883a00, bss). Correlate RAM bytes against
file bytes to recover the transform empirically; or skip correlation
entirely and treat *RAM* as the injection target for a live proof (poke
Arabic glyph bytes into FontImage, trigger `FontTexReload` by advancing a
dialog, observe).

---

## 4. The Arabic build pipeline (new tool: `arabicpack.py`)

A build-time transformer sitting between translated UTF-8 source files and
the existing importers. Python; dependencies: `arabic-reshaper`,
`python-bidi` (pure-Python, vendorable).

### 4.1 Text transform (per message)

```
logical Arabic UTF-8 (translator-authored, with engine control codes kept verbatim)
  → arabic_reshaper.reshape()          # contextual forms + lam-alef ligatures
  → bidi.algorithm.get_display()        # RTL base direction; LTR runs (Latin names, digits) resolved
  → wrap(width_px, metrics_table)       # greedy wrap on spaces, measured in
                                        #   *our* advance widths from font.tex metrics
  → per-line reverse                    # so the LTR renderer draws RTL correctly
  → encode to game codepage (§4.2)      # 1 byte per glyph slot
  → right-align pad (optional, per textbox convention)
  → hand off to textpack.py import / WP-3 class rewriter (budget-checked)
```

Control codes (`\15\2` speaker/color/pause markup documented in the French
guide) must be tokenized out before shaping and re-inserted at the same
*logical* position after reversal — they are engine-order, not visual-order.

### 4.2 Codepage & glyph plan

The single-byte glyph range holds **160 proportional slots** (the metrics
table's exact size). Budget for Arabic:

| Need | Slots |
|---|---|
| ASCII digits, basic punctuation, Latin caps for names (keep) | ~50 |
| Arabic presentation forms: 28 letters × 1–4 forms (many letters need only 2) | ~76 |
| Lam-alef ligatures (4), hamza forms (~8), Arabic punctuation ؟ ، ؛ | ~14 |
| **Total** | **~140 / 160** ✓ |

Strategy: repurpose the slots currently holding lowercase Latin + symbols
we don't need; keep digits and A–Z (proper nouns render in Latin LTR —
bidi already orders them). Emit a `codepage.json` (Unicode presentation
form ↔ byte) consumed by both the encoder and the glyph-sheet injector.
Kana/kanji glyph banks are untouched — Tier A files may still contain JIS
symbols via the existing `⟦XX⟧` escape mechanism.

Glyph art: render a free Arabic font (e.g. SIL Scheherazade/Amiri, or a
pixel font like GNU Unifont's Arabic block for a first pass) into the cell
raster at the height WP-1 establishes, 4-level antialiasing to match the
existing glyphs' 0/4/8/C nibble levels.

### 4.3 Wrapping rule

Never let the engine's auto-linefeed fire. `arabicpack.py` must know each
textbox's pixel width (WP-4 measures them; `xglFontGetStringWidth` is the
in-engine oracle to validate against) and emit lines with explicit breaks
at ≤ width − safety margin. Acceptance: no auto-wrap observed across the QA
script (§WP-6).

---

## 5. Work packages

Ordered; WP-1/WP-2 are the critical path. Each has an acceptance test an
agent can run without human judgment.

### WP-1 — finish the font.tex format
**In:** §3.1 facts, §3.3 routes, `SLUS_204.69`, retail ISO.
**Do:** Route 1 and/or Route 2 until header fields, glyph indexing
(EUC point → file offset), cell raster, bit depth, and swizzle are written
down in FORMATS.md. Deliver `font.py`: `decode(tex_bytes) → {code: Glyph}`,
`encode` (byte-perfect round-trip), metrics read/write.
**Accept:** `encode(decode(font0.tex)) == font0.tex` byte-identical; a
rendered glyph sheet PNG shows legible ASCII + kana + kanji.

### WP-2 — Arabic glyph injection proof
**In:** `font.py`, `codepage.json` draft, any Arabic glyph art.
**Do:** Replace ~10 slots (one word's worth of forms), rewrite metrics,
`patch_iso` both font files (check `manifest.csv` for duplicate copies of
`font*.tex` first — textures on this disc historically have up to 12).
Patch one same-length Tier-B line (MODDING.md §5 recipe, ST0210 cold-open
line — renders ~1 min after boot with zero input) to bytes that hit the new
slots, pre-reversed.
**Accept:** PCSX2 screenshot of the cold open showing connected RTL Arabic.
This single screenshot de-risks the whole project.

### WP-3 — class-file string rewriter + FL00 rebuilder
**In:** docs/JAVA.md, docs/FORMATS.md, `evt.py` carving, CLASS-MAP.json.
**Do:** Parse JDK 1.1 class files; replace `CONSTANT_Utf8` bodies with
arbitrary-length bytes; re-serialize; rebuild the FL00 container offset
table; verify object fits its sector budget; integrate with `patch_iso`.
Resolve the modified-UTF-8 question: patch one string to bytes ≥0x80 both
raw and UTF-8-encoded, boot, see which renders the intended glyph.
**Accept:** a length-*changing* English edit of the ST0210 line renders
in-game; a full-disc rewrite pass with identity strings produces a
byte-identical ISO (harness honesty check).

### WP-4 — `arabicpack.py` + textbox metrology
**In:** §4 design, WP-1 metrics, WP-3 rewriter, textpack.py.
**Do:** Implement the transform chain with golden-file unit tests
(shape/bidi/wrap/reverse each pinned); measure dialog, U.M.N. chat, mail,
and menu box widths (RE the callers of `xglFontGetStringWidth`, or
empirically bisect with ruler strings); wire `export-ar`/`import-ar`
into `cli.py` (kit integration has 4 touch points — MODDING.md §7).
**Accept:** round-trip idempotence (`import-ar` twice → "nothing to
write"); a 200-line sample batch imports with zero budget failures and no
auto-wrap in spot checks.

### WP-5 — full text inventory & Tier C/D
**Do:** Enumerate every translatable string: Tier A manifest (exists),
Tier B string dump across all ~2,200 carved classes (`evt_unpack.py
--dump` + sweep), Tier C ELF/overlay strings with xrefs, Tier D
textures/movies with baked text. Emit `inventory.csv` (surface, file, id,
en_text, byte_budget, box_width).
**Accept:** row counts reported per tier; three random Tier C strings
successfully repointed/patched and verified in-game.

### WP-6 — translation content & QA loop
**Do:** Export → translate (human or model, house style doc: Modern
Standard Arabic; proper nouns in Latin; Eastern vs. Western digits —
pick once) → `import-ar` → automated boot-and-screenshot passes over a
scene list (PINE can drive/verify state; `flag-hunt` agent knows the
replay tooling).
**Accept:** the QA scene list renders with zero overflow, zero tofu
(unmapped byte → visible sentinel glyph slot), zero auto-wraps.

---

## 6. Trap list (each has already burned time — do not rediscover)

1. `scene/cf*.txt` planner files are **not** rendered text (Tier A trap).
2. Same-length rule for Tier B until WP-3 lands; the Utf8 length prefix
   includes a trailing NUL, and leading spaces are manual centering.
3. `.uml` text slots are fixed-length; Arabic is often *longer* than
   English — mails are the tightest budget on the disc.
4. `⟦XX⟧` markers in exports are raw-byte escapes — never translate them.
5. Engine auto-linefeed must never fire on reversed lines (§4.3).
6. Control codes are logical-order tokens — extract before shaping,
   re-insert after reversal (§4.1).
7. Sweep `manifest.csv` for duplicate copies before declaring any file
   patched (textures ship up to 12 copies; check `font*.tex` too).
8. In-RAM CLUTs are lighting-tinted copies — don't byte-compare palettes
   against files when using the PINE route (MODDING.md §6).
9. GS-resident font textures refresh lazily — after a RAM poke, advance a
   dialog line to force re-upload before judging the result.
10. Run every patch on a **copy** of the retail ISO; `text-import` is
    all-or-nothing, raw `patch_iso` is not.

## 7. Reproduction snippets

```python
# read a TOC object (run from Xenosaga1PythonExtractor/)
from repack import read_entry, patch_iso
ISO = r"../Xenosaga Episode I - Der Wille zur Macht (USA)/Xenosaga Episode I - Der Wille zur Macht (USA).iso"
font = read_entry(ISO, 0, "font0.tex")          # 492096 bytes
metrics = font[0x78100:0x78240]                  # 160 (lead, advance) pairs

# font symbols
# nm "…/out/browse/code/SLUS_204.69" | grep -iE 'font|moji'

# EE RAM ground truth while the game runs (PINE socket $TMPDIR/pcsx2.sock)
# flag-hunt/pine.py: read_block(0x883a00, N)  → decoded FontImage
```

---
*Provenance: structure/metrics/symbol findings verified in this repo on
2026-07-18–19 against the USA ISO and SLUS_204.69. Everything labeled
"unassigned"/"unverified" is exactly that — trust the tests, not the prose.*
