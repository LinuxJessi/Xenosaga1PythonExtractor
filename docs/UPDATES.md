# Updates

Newest first. Each entry lists what changed, why, and what you have to do
to benefit from it.

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
