# Xenosaga Episode I Python Extractor

Unpack, browse and mod **Xenosaga Episode I — Der Wille zur Macht** (PS2,
USA release SLUS-20469) with nothing but Python. Point it at your own disc
image and you get every file on the disc in a form your computer can
open: textures as PNG, **models as glTF for Blender**, voices and music
as WAV, cutscenes as MP4, game text as UTF-8, and the cutscene scripts as
Java you can read. Then write your changes back to a bootable ISO.

The kit ships no game data. You need your own copy of the disc.

![KOS-MOS, Cherenkov and Albedo imported into Blender from the kit's .glb files](docs/images/blender-models.png)

## What's new (October 2026)

* **Models open in Blender.** Every character, enemy, weapon, mech, prop
  and map on the disc comes out as a `.glb` with its texture inside.
  File ▸ Import ▸ glTF 2.0 and you have the mesh, its UVs, vertex
  colours and skin-weight groups. Faces are merged into their bodies.
  How-to and what to expect: [docs/BLENDER.md](docs/BLENDER.md).
* **Character textures look right.** Opening models in Blender showed
  four texture bugs that had been there since the first release, and
  all are fixed: 42 character atlases that came out as beige noise
  (Cherenkov, Jr., MOMO, Shion and Virgil variants among them), sleeves
  and trims drawn with the wrong palette, hair sheets half-missing, and
  the texture scale on narrower atlases. 756 of the 1,019 model
  textures on the disc changed, all for the better.
* **The casino is fixed.** Card faces, the slot machine and the
  concept-art gallery were the last textures that came out wrong. The
  game's menu code says which palette colours which part of a sheet,
  and the kit now reads that. This closes GitHub issue #1.
* **The soundtrack plays.** The sequenced music is rendered to WAV and
  FLAC, with MIDI and SoundFont files alongside, at the right tempo.
  The story of how: [docs/MUSIC.md](docs/MUSIC.md).

Still on the list: the skeleton pose (models import with weight groups
but no posed armature), the eyelash textures (stored 4 bits per pixel,
not decoded yet), and a few map meshes in formats the model parser
skips. The full change log is in [docs/UPDATES.md](docs/UPDATES.md).

## Running it

There is a point-and-click app and a command line. Both do the same
things.

**The app.** Download the bundle for your system from the Releases
page and double-click it. Nothing to install.

| | |
|---|---|
| Windows | `Xenosaga-I-Extractor-windows/Xenosaga-I-Extractor.exe` |
| macOS | `Xenosaga-I-Extractor.app` (first time: right-click ▸ Open, because of Gatekeeper) |

**From a source checkout** (Python 3.9 or newer, no packages to
install): double-click `launch.bat` (Windows), `launch.command` (macOS)
or `launch.sh` (Linux), or run `python gui.py`. The app opens as a page
in your browser, finds your disc image, and shows one card per job
with a live log.

**From a terminal:**

```sh
python cli.py extract --iso "Xenosaga Episode I (USA).iso" --out out/ --code
python cli.py classes --iso "Xenosaga Episode I (USA).iso" --out out/
python cli.py browse  --out out/
```

`extract` unpacks the disc into `out/dump/`. `browse` converts it into
`out/browse/`, which is the folder you actually open: PNGs, GLBs, WAVs,
MP4s and text. A guided tour of what is in there, and which folders are
worth opening first, is in [docs/BROWSING.md](docs/BROWSING.md).

## What you get

| | Where | Format |
|---|---|---|
| Textures | `browse/textures_png/` | PNG, plus 1,400 more pulled out of effect, scene and menu containers |
| Models | `browse/models/` | glTF binary (`.glb`) with the texture embedded |
| Voices and cutscene audio | `browse/audio/` | WAV |
| Music | `browse/music/` (the **Render the soundtrack** card, or `music-export`) | WAV, FLAC, MIDI, SoundFont |
| Battle voices and sound effects | `browse/battle_audio/` | WAV |
| Cutscene movies | `browse/movies/` | MP4, including the 58 full cutscenes hidden on the disc's second layer |
| Game text | `browse/text/` | UTF-8 |
| Cutscene scripts | `browse/classes/` | Java class files (yes, really: [docs/JAVA.md](docs/JAVA.md)) |
| Executables | `browse/code/` | the main program and overlays, with all their function names intact |

## Modding

The disc is unusually friendly to patching. The kit writes changes back
**in place**, so everything you did not touch stays byte-for-byte the
same as the retail disc.

* **Replace any file:** `python cli.py patch --iso GAME.iso --out MODDED.iso --set 'chain0:char\pc\kosmos.xtx=my_kosmos.xtx'`
* **Recolour KOS-MOS's hair** with one click (`pinkhair`). It is the
  worked example because the texture is stored in twelve places on the
  disc and the kit finds all of them.
* **Translate the game.** `text-export` writes every menu and mail text
  as plain UTF-8 files; edit them; `text-import` checks they fit and
  writes a patched ISO. Cutscene dialogue lives in the Java scripts and
  can be changed today as long as the new line is the same length.
* **Subtitle the cutscenes.** The movies have no subtitle track, so the
  kit lets you write one and burns it into the video.

| | |
|---|---|
| ![KOS-MOS model close-up, recolored hair](docs/images/pinkhair-model-closeup.png) | ![Battle screen, recolored KOS-MOS landing a HI-CRITICAL](docs/images/pinkhair-battle.png) |

How all of that works, in enough detail to build on:
[docs/MODDING.md](docs/MODDING.md).

## How the disc is put together

Three things explain most of what the kit does.

* **The disc has seven big files and nothing else.** `XENOSAGA.00` to
  `.13` hold all the game data. Read `.00+.01+.02` end to end and you
  have "chain 0", the game world: 8,573 models, textures, scripts and
  sound banks. `.10` to `.13` are chain 1, the streamed voice and
  cutscene data. A table of contents at the front of each chain maps
  game paths like `char\pc\kosmos.xtx` to where the bytes are.
* **Half the disc is invisible.** The second layer of the DVD is not in
  the filesystem at all. It is 58 full cutscene movies that the game
  plays by raw sector number. The kit finds them by scanning.
* **About 2,100 files are compressed** with Monolith's own ARX scheme.
  The kit's compressor is a byte-perfect clone of the original, which
  is what makes clean patching possible.

Two bonuses for the curious: every executable still has its function
names, and the cutscenes are compiled Java 1.1, debug info and all.
The disc also shipped eight developers' personal working folders;
[docs/FINDS.md](docs/FINDS.md) and [docs/HISTORY.md](docs/HISTORY.md)
have those stories.

The Japanese release is expected to work (nothing is tied to the USA
serial) but has not been tested on a real JP disc. If something breaks,
please open an issue with the exact error.

## Docs

| Read this | When you want to |
|---|---|
| [BROWSING.md](docs/BROWSING.md) | know what is in the output and where to look first |
| [BLENDER.md](docs/BLENDER.md) | bring the models into Blender |
| [MODDING.md](docs/MODDING.md) | change the game: patching, recolours, translation, verification |
| [SUBTITLES.md](docs/SUBTITLES.md) | add subtitles to the cutscenes |
| [MUSIC.md](docs/MUSIC.md) | understand and render the soundtrack |
| [JAVA.md](docs/JAVA.md) | read the cutscene scripts |
| [FORMATS.md](docs/FORMATS.md) | see the byte-level details of every file format |
| [CODEBASE.md](docs/CODEBASE.md) | find your way around the code |
| [UPDATES.md](docs/UPDATES.md) | see what changed and when |
| [FINDS.md](docs/FINDS.md), [HISTORY.md](docs/HISTORY.md) | enjoy the leftovers and the history the disc records |

## Building the app yourself

`python build.py` (needs `pip install pyinstaller`) makes the
self-contained bundle. Build on Windows for the `.exe` and on macOS for
the `.app`; the GitHub Actions workflow builds both when a version tag
is pushed.

## Credits

ARX decompression, the texture canvas layout and the model format were
learned from [Lakuwu's xenotool](https://github.com/Lakuwu/xenotool) and
re-implemented here, with credit where each piece is used in
[docs/FORMATS.md](docs/FORMATS.md). The ARX compressor, the table of
contents, the hidden-layer movie carver, the Java class carver, the
audio and music decoders, the overlay-palette and model-material
texture work, and the whole repack layer were reverse-engineered for
this kit.
