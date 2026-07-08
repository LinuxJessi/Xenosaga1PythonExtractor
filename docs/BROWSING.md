# Browsing the extraction — a guided tour

What you have after running `extract` + `classes` + `browse` against the USA
disc, and how to poke around in it. Numbers below are from a full extraction
of SLUS-20469; yours will match.

```
out/
  dump/       the disc, unpacked: 8,980 files as the game addresses them
  browse/     the same assets converted to formats your desktop opens
  manifest.csv        one row per extracted object (9,000 rows)
  browse/classes_manifest.csv   one row per lifted Java class
```

Rule of thumb: **`dump/` is for tools, `browse/` is for humans.** Everything
in `browse/` is derived from `dump/` (or straight from the ISO) by decoders
described in [FORMATS.md](FORMATS.md).

## The three areas of `dump/`

### `dump/chain0/` — the game's `data\` tree (8,573 files)

This is the world: models, textures, scripts, menus, sound. The game's own
path strings (`data\scene\ST0010.evt`) map 1:1 onto this tree.

| Directory | Files | What it is |
|---|---|---|
| `scene/` | 1,066 | field/event scripts — `ST####.evt` (Java! see [JAVA.md](JAVA.md)) plus decoded-text `.txt` companions with live dev comments |
| `char/` | 868 | playable/NPC character models (`.lex` mesh, `.xtx` texture, `.jnt` skeleton) |
| `obj/` | 799 | scene props (`aobj###/…`) |
| `map/` | 704 | field geometry, `MC_<zone><nn>` naming — zones VOK (Woglinde), DYU, ELS (Elsa), KUK (Kukai), UTA |
| `sound/` | 680 | `.SMD` sequences / `.SWD` wave banks / `.SED` SFX banks (Procyon Studio formats) |
| `carddata/` | 393 | the card-game database + 117 card face textures |
| `umn/` | 348 | U.M.N. email/database mode: `.uml` entries, `event##.txt` dialogue |
| `weapon/` | 261 | weapon models |
| `enemy/` | 279 | enemy models |
| `motion/` | 176 | animation packs (`.fpk`) |
| `movie/` | 131 | small in-engine `.ipu` clips (`movie/small/mvs####.ipu`) |
| `robo/` | 65 | A.G.W.S. mechs |
| `texture/` | 78 | shared textures |

Top-level loose files worth opening: `title.jpg`, `monologo.jpg` (straight
JPEGs), `namco.ipu` / `logo_msi.ipu` (the boot logos), `base.evt` /
`system.evt` (the Java runtime — yes, really), `hdd.res` (HDD-install
resources).

**The eight staff folders.** Episode I's build system published each
developer's personal working directory straight to the retail disc. See
[FINDS.md](FINDS.md) for the full story; the short version:

| Folder | Files | Owner's beat |
|---|---|---|
| `simajiri/` | 1,227 | particle effects — 1,051 `.esd` + 170 `.esp` scripts |
| `yamamoto/` | 1,211 | battle data (1,195 `.bin` stat tables, one per character) + battle BGM + a few movies |
| `tanaka/` | 110 | **the casino** — slots, poker, card art (`slot_1.xtx`, `poker_1.xtx`, `CASINO.res`) |
| `matumoto/` | 88 | per-zone map parameter `.dat` files (incl. `MC_TEST.dat` and `testes.dat`) |
| `nisimori/` | 42 | battle result/announce UI (`.bxx`, `.rbg` backgrounds) |
| `endou/` | 24 | menus, shop, skill tree, save screen (`savemap.bin`, `shopdata.bin`, `etree.bin`) |
| `yajima/` | 11 | title & game-over screens (`gameov.jpg`, `end.jpg`, `gameover.vds`) |
| `karakama/` | 2 | event items (`evtitem.dat`, `itembox.dat`) |

### `dump/chain1/` — streaming data (349 files)

| Directory | Files | What it is |
|---|---|---|
| `sound/vda/` | 103 | streamed voice/cutscene audio (`s######.vda`) |
| `mtnpack/` | 109 | per-cutscene motion packs (`SCE#####.arc` + `.fpk` pairs) |
| `scene/` | 92 | cutscene Java scripts (`SCE#####.evt`, chapter-numbered) |
| `movie/mpeg2/` | 45 | video-only `.pss` movies |

### `dump/layer1/` — the hidden half of the disc (58 files, 3.9 GB)

The ISO9660 filesystem only describes layer 0 of the dual-layer DVD. All of
layer 1 is movies, addressed by raw sector number and invisible to normal
ISO tools — `carve.py` recovers them without any index. Names encode the
absolute sector (`layer1_047_lba3551180.pss`) so the mapping to real titles
can be recovered later from the playback code. Sizes run from 2.5 MB
(`_045`) to 208 MB (`_047`). These are the full-quality cutscenes **with
audio**; they play in VLC/ffplay as-is (video) and convert to proper MP4s
with `browse --kinds movies`.

## `browse/` — the human-readable mirror

Each `browse` kind writes one subtree, preserving `dump/` paths:

| Directory | Count | From | Open with |
|---|---|---|---|
| `textures_png/` | 1,273 PNGs | `.xtx` (+ sibling `.lex` palettes) | anything |
| `audio/` | 104 WAVs | `.vds`/`.vdm`/`.vda` streams | anything |
| `soundbanks/` | per-bank WAVs + `smd_catalog.csv` | `.swd`/`.smd` | anything |
| `text/` | 588 UTF-8 files | Shift-JIS `.txt` | your editor |
| `movies/` | 247 MP4s | `.pss`/`.ipu` (needs ffmpeg) | anything |
| `images/` | 13 JPEGs | `.jpg` on disc | anything |
| `classes/` | ~2,200 `.class` | `.evt` FL00 containers | `javap -c -p`, CFR, Krakatau |
| `code/` | 19 binaries | ISO filesystem (`--code`) | Ghidra / readelf |

Highlights per subtree:

* **`textures_png/chain0/tanaka/`** — 109 casino graphics: slot reels, poker
  tables, card faces. The single best "wait, this shipped in a folder named
  after a guy?" browse.
* **`textures_png/chain0/carddata/`** — all 117 card-game faces.
* **`audio/chain1/sound/vda/`** — every streamed voice line/cutscene mix as
  48 kHz stereo WAV.
* **`soundbanks/smd_catalog.csv`** — every music sequence with its embedded
  metadata; the titled ones literally credit
  `Yasunori Mitsuda / PROCYON STUDIO` inside the retail files.
* **`text/chain0/scene/`** — event text with the developers' Japanese
  comments still inline (see FINDS.md for gems).
* **`movies/layer1/`** — the 58 real cutscenes as MP4; movies with audio also
  get `name.video.mp4` + `name.audio.wav` splits for undub/redub work.
* **`code/`** — `SLUS_204.69` (unstripped!), five `OV*.OVL` overlays (also
  unstripped), and 13 IOP modules including the PS2 HDD/network stack
  (`DEV9.IRX`, `HDD.IRX`, `PFS.IRX`, `SMAP.IRX`).

## The manifests

**`manifest.csv`** (9,000 rows) — columns
`area,path,sector,size,compressed,usize,bigfile,bigfile_offset`. `area` is
one of `chain0` (8,573) / `chain1` (349) / `layer1` (58) / `code` (19);
`compressed=1` marks the 2,095 ARX-compressed entries (`usize` = their
uncompressed size). `sector`/`bigfile`/`bigfile_offset` let you find any
object back in the raw disc image. `verify --out out/` re-checks every row.

**`browse/classes_manifest.csv`** — columns
`area,evt,class,offset,size,sha1,written`: which `.evt` container each Java
class came from, at what offset, with a content hash (duplicate classes
across containers are deduplicated by sha1; bytecode-distinct variants of
the same name get a `__<sha8>` suffix).

## Recipes

```sh
# What's the biggest thing on the disc?
sort -t, -k4 -rn out/manifest.csv | head

# Find every file for one character
grep 'char\\\\kosmos' out/manifest.csv

# Read a cutscene script (after `classes` — the tree is flat, inner classes
# as Scene$Inner.class; engine classes under xeno/ and java/lang/)
javap -c -p "out/browse/classes/SCE01012.class"

# Which scenes reference a given voice file? (constant pools are searchable text)
grep -rl 's190024' out/browse/classes/

# Re-decode voice at a different rate for comparison
python cli.py browse --out out/ --kinds audio --rate 44100

# Just the casino art
python cli.py browse --out out/ --kinds textures   # then open browse/textures_png/chain0/tanaka/
```

For byte-level format details behind all of this, see
[FORMATS.md](FORMATS.md). For the disc's oddities and leftovers, see
[FINDS.md](FINDS.md). For the Java event system, see [JAVA.md](JAVA.md).
