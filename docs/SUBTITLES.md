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

Verified by testing against a real carved movie from this disc
(`layer1_045`, 2.6 MB, 195 frames) and a real patched copy of the retail
ISO:

* The source `.pss` video is standard MPEG-2, Main Profile @ Main Level,
  closed GOP (15 frames, 2 B-frames between references), progressive,
  `yuv420p` — a mainstream, well-documented format, not something exotic
  to this game. `subs.py` reads these parameters per-file via `ffprobe`
  rather than hardcoding them, so it should generalize across all 103
  movies even if a few differ.
* `subs.splice()` reassembles the container with every pack/system/
  audio/padding packet copied byte-for-byte from the original, in original
  order — only the video track's PES payloads are replaced. The result
  re-parses with the same packet grammar, decodes cleanly through `ffmpeg`
  (`ffmpeg -v warning -i out.pss -f null -` — zero warnings beyond the
  expected "no timestamp" notices), and produces the same frame count as
  the source.
* `layer1_patch()` writes the result into a copy of a real retail ISO,
  verified by read-back, and every *other* carved movie's sector/size
  stayed byte-identical afterward (`layer1-list` before/after matches) —
  patching one movie doesn't disturb its neighbors.
* The size constraint (patched movie ≤ original object's byte allocation)
  is enforced by `fit_to_budget`'s iterative bitrate search before the
  splice is even attempted, and re-checked after.

**Not** verified, because nothing short of the real thing can confirm it:

* **Whether the PS2's IPU hardware decoder accepts the re-encoded stream.**
  `ffmpeg`'s MPEG-2 decoder is deliberately lenient; PS2 hardware may be
  stricter about quantization matrices, VBV timing, or other encoder
  choices `ffmpeg`'s `mpeg2video` encoder makes that this module doesn't
  control precisely. Boot a patched ISO in PCSX2 before trusting a batch.
* **Audio/video sync over a long cutscene.** See the PTS section below —
  timestamps are omitted entirely rather than reused incorrectly, which is
  spec-legal but less precise than the original stream's periodic
  PTS/DTS. Short movies are unlikely to show drift; check a long one.
* **Font/rendering fidelity of the `subtitles` filter itself** — that's
  `libass`/your chosen font's problem, not this module's, but it's worth a
  visual check same as the accented-character caveat in
  [FRENCH-TRANSLATION-GUIDE.md](FRENCH-TRANSLATION-GUIDE.md) Step 0.

## The PTS/DTS decision (a real bug this was tested against)

The obvious-looking approach — reuse each original video packet's PTS/DTS
bytes verbatim, since frame count and fps are unchanged so the timeline is
still numerically valid — was tried and **rejected** after testing. On the
sampled movie, video frames are split across PES packets at roughly 2.8
packets per frame, not 1:1, and `splice()`'s proportional-by-byte-size
re-chunking doesn't guarantee a "PTS-carrying slot" lands on the same
decode-order frame it originally described once the payload has been
resplit. Reusing the bytes anyway produced **non-monotonic DTS** values
that `ffmpeg` flagged repeatedly (`Application provided invalid, non
monotonically increasing dts`) — it warned rather than failed, but PS2
hardware might not be as forgiving.

The shipped behavior omits PTS/DTS from every rebuilt video packet, which
is spec-legal (decoders fall back to deriving timing from the sequence
header's frame-rate) and is the version that round-tripped through
`ffmpeg` with zero warnings. The tradeoff: the original stream refreshed
its timing anchor periodically (66 of 549 packets on the sample movie);
the rebuilt one never does. If a long cutscene drifts audio from video in
testing, that's the first thing to revisit — the fix would need actual
per-frame PTS/DTS computed from decode order (accounting for B-frame
reordering), not simply carried over.

## Workflow

1. **Extract and convert the movie normally first** (`browse` /
   *Convert for browsing*) — you need the MP4 to actually watch/listen
   while timing cues.
2. **`subs-template --src MOVIE.pss --out MOVIE.LANG.srt`** — a blank,
   uniformly-spaced skeleton (default 5s cues). This is a starting point,
   not a real auto-timer.
3. **Time and translate in a real subtitle editor** (Aegisub, Subtitle
   Edit — anything that edits SRT) against the extracted MP4. Retime every
   cue by ear; don't trust the uniform spacing.
4. **`subs-burn --src MOVIE.pss --srt MOVIE.LANG.srt --out MOVIE.LANG.pss`**
   — requires an `ffmpeg` with the `subtitles` filter (`libass`). Check
   with `ffmpeg -filters | grep subtitle`; not every build has it (a stock
   Homebrew install on this project's own dev machine didn't, at time of
   writing — don't assume). `--max-bytes` defaults to the source file's
   own size, which is always safe since nothing here can grow past its
   original allocation.
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
