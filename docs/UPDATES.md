# Updates

Newest first: what changed, why, and what it means for you.

## 2026-10-15 — menu and battle words, movie subtitles, more scripts on screen

Two gaps the script pipeline still had, both closed and both seen
running on PCSX2 from the LANGTEST disc; the mock-up pictures in
SCRIPTS.md are replaced by captures.

**The executables' strings.** Tech names, `End`, `Menu`, item and skill
descriptions and the field menu's labels are C literals in the five
code overlays (`OV01.OVL`…) and the boot ELF. `ovl-export` writes them
as a third text tree (9,335 referenced strings on the USA disc, found
through pointer words and `lui`/`addiu` pairs with the overlays' own
symbol tables), `ovl-import` writes them back: in place when they fit,
otherwise appended to the overlay's load segment with every reference
repointed, the overlay going to the end of the image if its slot is
full (`--grow`). The memory past the largest overlay is unused on this
disc, which is what makes the append safe; the boot ELF has a bss after
its data and stays in-place only. Georgian (Mkhedruli) tech names,
seven of them relocated past the overlay's old end, are drawn by the
battle bars. The approach follows poketony's XS1KOR, which moved
overlay strings for the Korean disc.

**Movies in any script without libass.** `subs-burn --font` renders each
SRT cue itself — HarfBuzz shaping through the same code as the font
step, white with a black outline — and composites it with ffmpeg's
plain `overlay` filter, blurring the band that holds the burned-in
English while a cue is up; `--cues DIR` takes hand-drawn PNGs instead.
`subs-template --detect` times a skeleton from the original subtitle
(white pixels with a dark outline next to them, sampled four times a
second). The first Woglinde movie with ten Hindi cues plays in the game.
This too is XS1KOR's plan (fixed rows, outline/shadow/dark spread,
size-guarded re-encode over the untouched audio), re-done with filters
every ffmpeg build has.

**The battle screen's own font.** The command bars and HUD labels are
not drawn with font0/font1 at all: OV01 keeps a 25-column grid of 10×18
cells in the status sheet `yamamoto\stat\stat.xtx` and a 230-byte
lookup table (digits, A–Z, a–z, four punctuation marks). Two-byte codes
draw nothing there, which the first Georgian attempt showed as empty
bars. `statfont scan` paints a translation's letters into the grid's
110 free cells and gives each a byte the table lacks; `statfont encode`
writes the battle strings with those bytes and the rewritten table (an
overlay string like the others) into the tree `ovl-import` takes. KOS-
MOS's tech bars read in Mkhedruli on PCSX2.

**Movie timing, the hard way.** Two more things the first boot taught:
the console's movie player needs a PTS/DTS on nearly every picture (the
retail muxer stamps 1,455 of 1,495), and it reads the container by the
sector, so packs must stay 16,384 bytes with 4,096-byte packets, with
video delivered no more than about 0.3 s ahead of its decode time.
`subs.splice` is now a small muxer that reproduces that shape and
pacing from the original's own stamps; the earlier "omit the stamps"
version hung the game on a black screen.

**Hindi on screen.** The cold open's title, KOS-MOS's program menu,
Allen's question and the Yes/No box, drawn by the game from Kohinoor
Devanagari cells with conjuncts intact. One lesson: the dialogue box
wraps a little under 400 px, so `--width 370` for the kit's own breaks.

New: `ovl-export`, `ovl-import [--grow] [--retail]`, `statfont
extract|scan|encode`, `subs-template --detect`,
`subs-burn --font/--face/--px/--script/--language/--rtl/--cues/--mask/--bottom/--keep-cues`;
GUI cards for the executable strings and the battle font;
`repack.patch_fs_file` / `relocate_fs_file`; FORMATS.md § Code overlays
and § The battle screen's font.

## 2026-10-15 — v1.2.0: the script pipeline, tested on the console

Everything in the October notes below ships as **v1.2.0**: models and
armatures in Blender, the texture fixes, dialogue of any length, the
third chain, the fonts and the new-script pipeline. This entry records
what changed in the script pipeline after the first in-game run.

**Seen running, not just previewed.** A LANGTEST disc built from the
pink-hair example carries an Arabic dialogue line in the Encephalon
cold open and an Amharic U.M.N. mail. PCSX2 draws both from the rebuilt
`font0.tex`: the Arabic joined and right-to-left in the dialogue box,
the Amharic body wrapped at the mail viewer's 400 px, proportional,
with its Latin run and Ethiopic punctuation in place. The pictures are
`docs/images/ingame-*.png`; the full command sequence is
[SCRIPTS.md](SCRIPTS.md) § Worked example. The earlier `preview-*.png`
mock-ups stay in the guide labelled as such.

**Two scripts in one font.** `lang scan --extend glyphset.json` adds a
second script (another TrueType font, another HarfBuzz tag) to the
slots an earlier scan allocated, so Arabic and Ethiopic share one pair
of fonts on the LANGTEST disc: 81 proportional slots used, 80 free.

**Encoder fixes from the first real texts.**

* A token that mixes scripts (an Amharic word glued to a Latin model
  name, a bracket on an Arabic word) is split into runs by class and
  each run encoded by its own rule; before, the whole token went to the
  target script and lost its Latin half.
* Raw C0 controls and `⟦XX⟧` escapes pass through untouched, and the
  leading control sequence of a line is hoisted ahead of any
  right-to-left reordering so mail bodies keep their mode byte.
* The disc's own Japanese-range symbols (■ bullets, box drawing,
  fullwidth forms) are left as they are instead of being re-shaped.
* Leading and trailing spaces survive; lines wrap only at spaces, never
  inside a word; a paragraph with no right-to-left run stays
  left-to-right.

**Still to verify.** Relocation past the image end (`--grow` onto new
sectors) has been read back through the TOC on a scratch copy but not
booted; both LANGTEST objects fit their slots. The mail list's subject
and sender in a new script have not been seen either: a loaded save
keeps the inbox list in RAM and only re-reads the body.

**Driving PCSX2 from a script, for anyone repeating this.** Pad presses
posted to the emulator by process id need a hold of about 300 ms;
shorter taps are dropped at the title screen even though hotkeys land.
The field menu will not open while the character stands on a ladder.

## 2026-10-14 — translation without length limits, and fonts for new scripts

The three walls a translation project hit are gone, and credit for the
first look over each of them goes to poketony's
[XS1KOR](https://github.com/poketony/XS1KOR), the Korean localisation of
the Japanese disc, whose tools showed the shape of the answer; the
implementations here are the kit's own, against the USA disc.

**Dialogue of any length.** `evt-export` / `evt-import` (`evttext.py`)
write every string literal in every scene and cutscene script as a text
line and rebuild the Java classes and their FL00 container around the
translations. An identity rebuild of all 567 containers is byte-exact.
Mails (`.uml`) may grow too: the attached images move and their pointers
follow. And when an object no longer fits its sector allocation,
`--grow` relocates it to the end of the image and repoints its TOC entry
— the engine reads by raw sector, so the place does not matter. Tested
on a scratch copy of the disc: relocated, read back through the TOC,
neighbours untouched, relocated again, shrunk back in place.

**The third chain.** The disc's second layer carries its own ISO9660
volume with `XENOSAGA.20`–`.24`: 266 objects the kit had been reading
only as raw movie sectors, including 89 cutscene scripts and 44 voice
streams. `list`, `extract`, `classes`, text and dialogue export all see
chain 2 now; the raw layer-1 carve is skipped when the chain is there.

**Fonts, exactly.** `font.py` decodes `font0.tex`/`font1.tex` to four
sheets of 20x24 cells and back byte-for-byte, and knows how the
executable addresses them: JIS order with two folds, a 256-cell
proportional block for Latin text with a permuted metrics table, and
`right - left + 3` pixels per glyph. The metrics match the inked columns
of every ASCII glyph, which is how the block was confirmed. The game's
text turns out to be EUC-JP, not Shift-JIS; `textpack` now decodes it as
such (byte-identical for ASCII, correct symbols for the mails' bullets).

**New scripts.** `lang scan` shapes a translated tree with HarfBuzz,
gives each glyph the translation uses a proportional slot (161 free, or
213 if the translation needs no Latin letters) or a kanji cell, and
draws it from a TrueType font into both faces. `lang encode` shapes,
measures, wraps, orders right-to-left lines with embedded Latin runs
kept forward and brackets mirrored, and writes the codes. Previews of
Arabic, Hebrew and Devanagari lines render correctly from the rebuilt
cells, and the screen agrees: on PCSX2 the Encephalon cold open reads
"درس افتراضي" joined and right-to-left, and a U.M.N. mail body in
Amharic wraps and mixes with Latin, both drawn by the game from the
rebuilt fonts (SCRIPTS.md § Worked example has the pictures and the
disc recipe).

New CLI: `evt-export`, `evt-import [--grow]`, `text-import --grow`,
`font-extract`, `font-rebuild`, `lang scan|encode|preview`; GUI cards for
the dialogue and script steps. Optional packages for the TrueType path:
`pip install fonttools uharfbuzz`.

## 2026-10-14 — skeletons: the `.jnt` files decoded, armatures in the game's pose

Models now import into Blender with a real armature instead of a flat
list of joints. The `.jnt` file is a tree of 64-byte nodes: a type
(offset, rotation joint, translate, bone end, name), a parent index, a
local position and three rotation angles. The skin's weight groups are
the rotation joints in order, which checks out on every model tried,
and the face file's own small skeleton comes along for eyes and mouth.

Every bone lands on the mesh, hands and hair included, from the files
alone. The one trap: the three angles are rotations about Z, Y and X in
that order, not X, Y, Z. Read the usual way they give a convincing
T-pose with the hair raised while legs and spine still line up, and
for a while that looked like a missing bind pose. It was settled by
running the game: PCSX2 with PINE enabled, a save state with KOS-MOS on
screen, and a RAM dump that contains the engine's own per-bone matrix
table. Brute-forcing the rotation reading against that table gave an
exact match for Z, Y, X and nothing close for any other reading. The
capture tool (`docs/research/pine_pose.py`) stays in the kit as the way
that was verified; nobody needs to run it. Byte layout and the evidence:
[FORMATS.md](FORMATS.md) § JNT skeletons.

**Eyelashes: solved.** Every character's lash mesh (UV type `0x7F`,
flagged true-colour) draws a small true-colour strip of the atlas that
the decoder had been rendering through a palette: a dark navy gradient
fading to transparent, which the lash polygons stretch into strokes.
The decoder now draws those rects as raw true colour unconditionally
(the coherence guard that protects hair sheets would reject a mostly
transparent strip), and KOS-MOS has her lashes back
(`docs/images/kosmos-face.png`). The long detour through "4-bit sheets"
was wrong: the regions on her atlas that split into two clean 4-bit
images are 8-bit-swizzled data with two images packed per byte by the
artists; read with PCSX2's verified GS 4-bit layout they are noise, so
the game never treats them as 4-bit textures.

## 2026-10-14 — models in Blender, and the texture fixes that came with them

**Models.** `browse` has a new kind, **Models**: every `.lex` on the disc
becomes a `.glb` under `browse/models/` with its texture inside, ready
for Blender (File ▸ Import ▸ glTF 2.0). You get the mesh, normals, UVs,
vertex colours and skin-weight groups; face files are merged into their
bodies and also written alone. The parser (`lex.py`) follows Lakuwu's
xenotool and was checked against it: KOS-MOS comes out at 3,290
triangles and 46 weight groups in both. On the retail disc the run
writes 1,315 `.glb` files (1,145 of them textured) holding 5.15 million
triangles, 663 MB in all, with 104 face files merged into their bodies,
nothing unparsed and six files carrying a warning; 760 of them carry a
posed skeleton. The how-to is [BLENDER.md](BLENDER.md).

**Texture fixes.** Looking at models in Blender showed up four bugs in
the texture decoder, some there since the first release. All fixed:

* *Two-part atlases overwrote themselves.* 42 character textures
  (Cherenkov, Jr., MOMO, Shion and Virgil variants, Van, Joachim and
  more) came out as beige noise because the second half of the image
  was placed on top of the first. The placement rule is now fitted on
  every multi-part texture on the disc. Seventeen menu sheets improve
  with it too: the card-game tutorial pages, the slot frame, the battle
  font.
* *Materials hidden in the vertex stream were ignored.* A mesh can
  switch palette mid-stream (Cherenkov's sleeves). The decoder now sees
  those materials, which was an open item in the format notes since
  v1.0.0.
* *Hair sheets were a guess.* Albedo's white hair came out half beige.
  The model says exactly which regions are true colour, so the decoder
  now uses that instead of a noise test.
* *Narrower atlases were scaled wrong in the model export.* UVs now use
  the texture size the model declares, not the width of the decoded
  image. KOS-MOS happened to be the full width, which is why she looked
  right from the start and Cherenkov did not.

Checked by re-rendering all 1,019 model textures on the disc with the
old and new code: 756 change. Textures decoded with their own model
palette rise from 790 to 940, and the fallback guesswork drops from 155
to 14. Every file whose transparency grew was looked at by eye, and all
were improvements; the only wash is seven tiny card-game banners that
were unreadable before and after.

**Casino.** The card faces, the slot machine and the concept-art
gallery in the casino were the last textures that came out wrong. These
menu sheets use several palettes at once, and the game's menu code
(`OV11.OVL`) says which palette colours which part of the sheet. The
kit now reads those descriptors, with a check that keeps a sheet from
borrowing another sheet's palette. This closes GitHub issue #1.

**Still open.** The skeleton pose and hierarchy (`.jnt`), the eyelash
textures (4 bits per pixel, undecoded, flagged in the `.glb` as
`…_lash-4bpp-undecoded`), two map meshes in vertex formats the parser
skips, and the slot machine's reel strips, which no descriptor covers.

For the byte-level details of each change see [FORMATS.md](FORMATS.md);
the regression harness lives in `docs/research/lexless_render.py`.

## 2026-09-24 — scene-archive textures: the "undecodable variant" was ARX

The ~500 embedded XTX hits the sweep reported as *undecodable* (all in
`scene/cf*.a`) were never a texture variant. Scene archives, `.fpk`
packs and `.arc` bundles keep their members **ARX-compressed in place**,
and the ARX coder passes literal words through verbatim — so a packed
texture's *header* still reads as `XTX\0` plus sane sizes inside the
compressed stream, while its pixels do not exist until decompression.

* `browse --kinds textures` now decompresses every in-file ARX container
  first (`browse.py: iter_arx_containers`; 1,864 inside the 112 retail
  scene archives: 603 XTX, 778 `lex` models, 483 FPK packs), decodes
  the XTX members, and palettes them with the `lex` models packed beside
  them (nearest member first). Raw hits inside a container's span are
  skipped, so the bogus rows are gone — and so are two "textures" the
  old sweep had decoded out of the middle of a compressed stream
  (`cf3021.a_208e4c`, `cf3070.a_61690c`: garbage).
* Retail result: **1,363 embedded PNGs, 1,696 duplicates skipped, 0
  undecodable** (was 1,365 / 1,095 / 528). Nothing new to *look at*:
  all 603 packed textures are byte-identical copies of standalone
  `.xtx` you already have — the NPC skins (`char/`, 266), enemies
  (`enemy/`, 197), objects (`obj/`, 66), map atlases (`map/`, 65) and
  mechs (`robo/`, 9) each field scene packs for itself. What is new is
  the **provenance map**: `browse/embedded_textures.csv` now lists, per
  scene archive, every texture it carries and which standalone PNG it
  equals (`packed` = `arx`). That is the query the recolor tooling
  needed by hand ("which `cf*.a` carry KOS-MOS's palette?"), answered
  for every texture on the disc.
* `docs/FORMATS.md` gets the `.a` archive layout (TOC + 0x100-byte
  record header + ARX member) and corrects the repack note: the
  "re-framed canvas with 4-byte inserts" that `pinkhair.py` works around
  is the ARX bit stream (the entry-level sweep works because the hair
  words are stream literals).

## 2026-09-25 — v1.1.0 verification pass: casino palettes and rescues

Proving issue #1 against the July build (v1.0.0 renders decoded from
the tagged `browse.py`) turned up three things the interim decoders
had got wrong on the lex-less UI sheets. All folded into v1.1.0.

### UI sheets keep their conventional-spot palette

The candidate ranking penalises transparency (right for character
atlases, where a wrong palette hides garbage behind alpha 0). UI frames
legitimately have transparent cut-outs — slot-reel windows, the window
frame — so on those sheets the ranking preferred washed-out grey
palettes parked for other sub-images over the sheet's own CLUT at a
conventional corner spot, which v1.0.0's first-hit rule had picked
correctly. For textures without a `.lex`, the first conventional-spot
palette is now kept when it renders 50–95 % opaque and is, penalty
aside, about as coherent as the ranked winner (or the winner renders
the sheet nearly flat). Changes exactly six sheets, all for the
better: `tanaka/slot_2` (slot frame), `poker_2` (pay table), `w1`
(neon reel labels), `window`, `carddata/card_Rare` (rarity labels),
`texture/fm8009` (the Elsa deck map).

### Block-rescue pass gated

The interim build still repainted one tile of the casino slot machine
(`tanaka/slot_1`, an orange square) that v1.0.0 rendered black. Cause:
the 64-px **block-rescue** pass (a wrong palette "explains" a near-black
noise block better than the base palette). Checked across every
lex-less texture where that pass fired (32 files, 54 blocks): every one
of those rescues was wrong — grey blobs over the card-tutorial text, a
noise strip on the casino menu, the orange tile. The pass now runs only
for model-bound textures (a `.lex` exists), the same gate as the
raw-CT32 pass below. Verified through the real pipeline: all 114
`char/pc` + `char/npc` atlases byte-identical; the casino sheets now
differ from v1.0.0 only by the blanked palette squares (issue #1) and by
the sheets v1.0.0 got wrong (data viewer, slot frame).

Still open on the KOS-MOS atlas: the two plane-packed 4bpp visor-HUD
sheets. New lead recorded in FORMATS.md: they are drawn by *effect*
libraries (`simajiri/esd/eve017.esd`, `eve107.esd`, `boss0185.esd`,
`scene/cf0680.a`) as PSMT4 from the character VRAM slot with CLUT
pointers 14400/14401 — palettes those effects bring themselves.

### Raw-CT32 pass gated

Verifying issue #1 against the July build turned up a regression from
the 2026-08-21 decoder: the raw-CT32 pass (which draws true-colour
hair sheets straight from the canvas) also fired on lex-less UI sheets,
where dithered 8bpp art — two indices alternating — reads as a
"coherent" checker when taken as CT32. The casino slot reels
(`tanaka/slot_1`) came out blue/yellow checkered and the poker felt
(`tanaka/poker_1`) half garbled. The pass now runs only for model-bound
textures (a `.lex` exists); verified byte-identical output on all 114
`char/pc` + `char/npc` atlases, clean reels and felt. Still imperfect in
the casino: `base.png` (slot symbol sheet) is tinted by a wrong overlay
palette, as it was in every build.

To pick this up: re-run `browse --kinds textures` on an existing dump
(no re-extract). Pure-Python ARX adds ~2 minutes to the sweep.

## 2026-08-21 — texture decode overhaul, embedded content sweep, BGM tempo fix

Full retest of the extractor against the retail USA ISO (two complete
extract→classes→verify→browse passes; `verify` clean on all 8,980 objects
both times). Everything below is on the **extract/browse (viewing) side**
— nothing changes what `patch`/`pinkhair`/`text-import` write to an ISO.

### New content recovered

* **Embedded texture sweep** — ~3,000 validated XTX blobs live *inside*
  other files (`.esd`/`.esp` effect libraries, `.a` scene archives,
  battle `.bin`, and the NLNK/NBGL/NBXX UI containers `.npr`/`.rbg`/
  `.bxx`: title screens, help pages, room backdrops). `browse --kinds
  textures` now sweeps every dump file and writes **1,365 new PNGs** to
  `browse/textures_png/_embedded/` (1,095 byte-identical duplicates are
  recorded, not re-written) with a per-find manifest in
  `browse/embedded_textures.csv`. The ~500 undecodable hits are all one
  in-`.a` XTX variant whose sub-image pointers are GS addresses (pixel
  data streamed separately) — open thread.
* **PS2ICON3D resources** — `hdd.res` (the HDD-install bundle) is now
  unpacked by the `images` kind into its boot CNF, `icon.sys`, and the
  memory-card `.ico` 3D icon model.
* **Hidden string tables** — the `text` kind sniffs text out of
  binary-extension files: `evtitem.dat` (event-item descriptions),
  `CASINO.res` (casino dialogue), dev `.info`/`.uml` configs — exported
  as readable `*.strings.txt`. The `.info` files turned out to be
  EUC-JP, not Shift-JIS; the decoder now auto-picks the reading without
  mojibake.

### Texture decode fixes (the KOS-MOS hunt)

* **Raw CT32 regions render as art, not noise.** Character atlases mix
  8bpp paletted art with true-colour CT32 regions (KOS-MOS's and NPCs'
  hair-strand sheets). Per 64px block, when the palette render measures
  as dither noise but the canvas read directly as CT32 is coherent, the
  block is drawn straight from the canvas. Confirmed by the file format
  itself: lex materials with `pal=0xFF` are true-colour bindings whose
  rects (in canvas coords) mark exactly these regions.
* **Companion-model materials merged.** A `.lex` with no `.xtx` of its
  own (`kosmos_face.lex`, `kosmos_h_face.lex`, …105 textures have such
  companions) binds its materials onto the sibling atlas whose stem is
  the longest prefix of the lex stem. This is ground truth the
  heuristics can't reach — it's what finally rendered **KOS-MOS's red
  eye** correctly (palette tile (448,112), bound by kosmos_face.lex).
* **Map atlas rescue.** Overworld atlases (`map/MC_*.xtx`) bind their
  parked CLUTs through VIF-stream materials the static parser never
  sees; the per-block rescue pool now includes every CLUT-looking tile
  in the canvas (recovered e.g. MC_DYU01's second DURANDAL sign and
  starfield).
* **Fine-grained trusted-palette rescue.** 32px blocks outside every
  parsed material rect repaint when one of the *same file's* material
  palettes renders them near-perfectly (thresholds fitted so all true
  fixes pass and all false repaints are blocked; zero opacity
  regressions across samples).
* **All-16×16 sprite files decode.** `carddata/game/1p2p.xtx` (the 1P/2P
  indicators) was the one undecodable texture on the disc — every
  sub-image was being classified as a palette tile. Now 1,269/1,269
  standalone textures decode.
* **Known residual:** two regions of the KOS-MOS atlas are plane-packed
  4bpp visor-HUD sheets (symbol sheet, keypad, HUD text — each index
  byte's two nibbles are two separate images) whose 16-colour palettes
  are not stored on disc. Rendering them faithfully needs a live-RAM
  palette capture (PCSX2 + PINE). Details in FORMATS.md.

### Sequenced BGM tempo fix

* **The driver's timebase is fixed at 96 PPQN.** The SMD header byte @
  0x20 (retail 100/120/123/127) is *not* ticks-per-quarter — reversed
  from SSD.IRX: a 2 ms timer ISR advances one sequence tick per
  underflow of a 16.16 accumulator, rate = `((tempo*53687)>>8 *
  master)>>8`, so ticks/sec = 1.6×tempo = tempo×96/60 exactly.
  First-pass tooling read 0x20 as PPQN and played the battle themes
  ~25% fast (U.M.N. Mode at 100 was only 4% off, which masked the bug).
  `music-export` output is now at correct speed: BATTLE1/2 162.7 s,
  LastBattle 305.5 s, jingles 34.9 s.

### To pick these up

Re-run `browse` (and `music-export`) on an existing dump — no
re-extract needed; the disc data was never wrong, only its conversion.
The packaged Windows build must be rebuilt (GitHub Actions release
workflow) or run from source; older exes predate all of the above.

## Earlier (uncommitted work now landed alongside this update)

* Sequenced-BGM engine: `ssd.py` (SMD→MIDI, SWD→SoundFont2) +
  `ssd_render.py` (numpy SPU2 sampler → WAV/FLAC), `cli.py
  music-export`, reversed from the unstripped SSD.IRX driver.
* Battle voice/SE banks (`yamamoto/snd/sed/*.bin`) → `battle_audio`
  browse kind (1,502 sample WAVs, stereo pairs stitched).
* XTX palette pipeline v2: overlay CBP descriptors, coherence-ratio
  candidate ranking, CLUT-tile blanking (GitHub issue #1).
* Arabic-translation groundwork spec (`docs/ARABIC-TRANSLATION-SPEC.md`).
