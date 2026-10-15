# Cutscene subtitles: authoring and burning them in

FMV cutscenes (the `.pss` movies — 45 indexed in the TOC, 58 more carved
from layer 1 by `carve.py`) ship with **no subtitle track at all**, in any
language, including Japanese — confirmed by grepping the dumped Java
classes, the exported text tree, and the manifest for anything
caption-shaped (`subtitle`, `telop`, `字幕`, any text object tied to a movie
path); nothing turned up. Dialogue during these movies is audio-only. This
is a different problem from the text pipeline in
[FRENCH-TRANSLATION-GUIDE.md](FRENCH-TRANSLATION-GUIDE.md): there, you're
translating existing text objects. Here, you're **authoring new subtitles
from nothing** and burning them onto the picture, keeping the original
English/Japanese audio.

`subs.py` (CLI: `subs-template`, `subs-burn`, `layer1-list`,
`layer1-patch`; GUI: the four cards after "Patch disc objects") does the
mechanical half of this. Read the honesty section before using it on a
whole game's worth of cutscenes.

## What's actually verified, and what isn't

The container, measured on `movie\mpeg2\1001_1.pss` (20,004,868 bytes,
1,495 frames at 29.97, 49.8 s):

* A run of **16,384-byte packs**: a 14-byte pack header, then PES
  packets of **4,096 bytes** each (the last one shortened so the pack
  closes exactly), video (`E0`) and audio (`BD`) interleaved about 5:1,
  three padding packets near the end, a 15-byte system header in the
  first pack, `00 00 01 B9` at the very end. 1,221 packs, 4,212 video
  packets, 671 audio packets.
* The video is standard MPEG-2, Main Profile @ Main Level, `yuv420p`,
  512×448; the retail stream uses open GOPs (an I frame with two
  leading B frames, 15 pictures) after a 16-picture first GOP, with one
  sequence header for the whole stream. The re-encode uses closed GOPs
  with a sequence header per GOP, which the player accepted.
* 1,455 of the 1,495 pictures carry a PTS in the PES header of the
  packet they start in (DTS too when it differs); the remaining ones
  share a packet with an earlier picture.

What `splice()` now guarantees, checked on every run: the output is the
same size as the source, every pack header, the system header and every
audio packet sit at their original offsets byte-for-byte, every video
slot is a PES packet of its original length (or a padding packet once
the new stream is spent), picture stamps are monotonic in decode order,
the stream ends with `sequence_end`, and `ffmpeg -v warning` decodes it
silently. `fit_to_budget` now targets the elementary stream at the
container's slot capacity, not the file size (which cannot change).

**Verified on PCSX2** (2026-10-15, v2.9.127, LANGTEST disc): see the
results at the end of [SCRIPTS.md](SCRIPTS.md) for the boot of this
movie with ten Hindi cues. Not verified: audio/video sync over a
multi-minute cutscene (this one is 50 s), and the 58 layer-1 movies
beyond `layer1-patch`'s read-back check.

## The PTS/DTS decision (two real bugs this was tested against)

The first version of `splice()` carried each original video packet's
PTS/DTS bytes over verbatim. Frames are split across PES packets at
roughly 2.8 packets per frame, so after the proportional re-chunking a
stamp no longer sat on the picture it described: `ffmpeg` flagged
non-monotonic DTS. The second version omitted PTS/DTS from every
rebuilt video packet, which is spec-legal and round-tripped through
`ffmpeg` with no warnings — and **hung the game**. Booted on PCSX2
(2026-10-15, `1001_1.pss` with Hindi cues), the screen went black at the
movie and the emulator log filled with `isoFile error: Block index is
past the end of file` until a read abort: the console's player never got
a picture it could schedule, streamed to the end of the disc and kept
going. The retail muxer stamps nearly every picture (1,455 of 1,495 in
that movie), and the player depends on it.

What ships now: `picture_units` splits the new elementary stream at
every picture (taking the sequence/GOP header in front of it along),
`picture_times` gives each picture a DTS one frame after the previous
one in decode order and a PTS of its GOP's display base plus its
temporal reference, on the original's time base (first PTS/DTS and
3,003-tick frames read from the source), and `splice()` starts a PES
packet at every picture with that stamp. Pack headers, the system header
and every audio packet are still the original bytes in their original
order; the stream ends with the sequence_end code the retail stream has
(XS1KOR's "safe tail"). `ffmpeg` sees monotonic DTS and 1,495 stamped
pictures. Whether the console's player is satisfied is the boot test in
the next section's results.

## Workflow

1. **Extract and convert the movie normally first** (`browse` /
   *Convert for browsing*) — you need the MP4 to actually watch/listen
   while timing cues.
2. **`subs-template --src MOVIE.pss --out MOVIE.LANG.srt --detect`** — an
   SRT skeleton with one cue per interval the original burned-in subtitle
   is on screen. The detector samples the subtitle band four times a
   second and counts white pixels that have a dark outline next to them,
   so a bright floor does not count as text; on the first Woglinde movie
   (`1001_1.pss`) it found the ten lines with two stray cues in the noisy
   opening shot and one pair of short lines merged. Without `--detect`
   you get uniform 5 s cues. Either way, check the times against the
   extracted MP4 and write the translation into each cue.
3. **Time and translate in a real subtitle editor** if you prefer
   (Aegisub, Subtitle Edit — anything that edits SRT). Multi-line cues
   are fine; the burner centres each line.
4. **`subs-burn --src MOVIE.pss --srt MOVIE.LANG.srt --out MOVIE.LANG.pss --font FONT.ttf`**
   — with `--font` the kit renders every cue itself: HarfBuzz shaping (so
   `--script Deva --language hi`, or `--script Arab --language ar --rtl`,
   come out joined and ordered), white letters with a black outline and
   shadow, one PNG per cue, composited by ffmpeg's plain `overlay`
   filter. While a cue is up, the band where the original English sits
   is blurred (`--mask x,y,w,h`, default `0,330,512,90`; `--mask none` to
   leave it). This is the scheme poketony's XS1KOR used for the Korean
   movies — fixed rows at 512×448, outline/shadow/dark spread over the
   original line, then a size-guarded MPEG-2 re-encode muxed over the
   untouched audio — minus libass, so any ffmpeg build works. The optional
   `fonttools` and `uharfbuzz` packages are needed for `--font`; without
   them, draw the cues in any image editor as `cue0001.png`,
   `cue0002.png`… (RGBA, frame width) and pass `--cues DIR`. Without
   either option the old libass `subtitles` filter is used, which many
   ffmpeg builds (Homebrew's, on this project's own machine) do not have.
   `--max-bytes` defaults to the source file's own size, which is always
   safe since nothing here can grow past its original allocation;
   `--keep-cues DIR` saves the rendered PNGs for a look.
5. **Write it back**:
   - TOC-indexed movies (the 45 `movie\mpeg2\*.pss`): the existing generic
     `patch` command already handles this, unmodified —
     `cli.py patch --iso GAME.iso --out MODDED.iso --set 'chain1:movie\mpeg2\XXXX.pss=MOVIE.LANG.pss'`.
   - Layer-1 movies (the 58 raw-sector ones): `layer1-list --iso GAME.iso`
     to find the index/name, then
     `layer1-patch --iso GAME.iso --out MODDED.iso --index N --file MOVIE.LANG.pss`.
6. **Boot the patched ISO in PCSX2** and actually watch the scene. This is
   the only way to catch an IPU-compatibility problem or A/V drift — there
   is no way to check either from a terminal.

## Constraint model (same as everywhere else in this kit)

A patched movie must fit within its original object's byte allocation —
growth is not possible without relocating every later object, which
nothing here attempts:

* TOC movies: the gap to the next TOC entry's sector (same rule
  `repack.py` already enforces for every other object type).
* Layer-1 movies: `CarvedStream.size`, defined by `carve.py` as the gap to
  the next carved stream's start (or end of image, for the last one) —
  real headroom, the same way TOC allocations are, just addressed by raw
  sector instead of a TOC entry. Same-size-or-smaller is safe; anything
  larger needs a full-disc sector relocation this module does not
  attempt.

Burning subtitles onto existing frames doesn't inherently need more bits
than the original encode, so hitting the ceiling is a bitrate choice
(`fit_to_budget` searches for one), not a hard blocker in practice.
