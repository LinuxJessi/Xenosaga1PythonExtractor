# Xenosaga Episode I — format reference (deep notes)

Working notes behind the README's format summaries: exact offsets, layouts,
and the verification evidence, so none of it has to be re-derived. All
byte orders little-endian unless stated. Credit where formats were learned
from [Lakuwu's xenotool](https://github.com/Lakuwu/xenotool) is marked.

## ARX compression (`arx.py`) — via xenotool

Word-oriented dictionary coder over u32s.

```
header:  "ARX\0"  u32 size_orig  u32 size_comp  u32 unk  u32 lut[30]
stream:  one cursor mixes control words and literal words.
         control bits consumed MSB-first from u32 control words:
           0          -> next u32 in the stream is a literal, copy verbatim
           1          -> prefix code selects a LUT entry:
                         0x        2-bit code, entries 0-1
                         10xx      4-bit code, entries 2 + (v & 7)
                         110xxx    6-bit code, entries 6 + (v & 0xF)
                         1110xxxxx 8-bit code, entries 14 + (v & 0x1F)
         control word exhausts -> next stream word refills it
```

Validated: every ARX blob on the disc decompresses; decompressed `.xtx`
start `XTX\0`, `.arc` start `FL00`, sizes match `size_orig` and the TOC's
usize field. (Nominal 8-bit codes can index past 30; retail files don't.)

**Compressor** (`arx.py: compress`, 2026-07-11): LUT = the 30 most frequent
u32 words, most frequent first (shortest codes), **ties broken by first
occurrence in the payload** — with that tie-break the output reproduces
retail blobs *byte-identically* for 2,094/2,095 compressed objects on the
USA disc (`chain1/mtnpack/SCE02004D.arc` differs in a tie only; same size,
exact round-trip). So Monolith's packer used the same greedy scheme. Full
control-bit sequences per slot: `1 0 x` (slots 0-1), `1 10xx` (2-5),
`1 110xxx` (6-13), `1 111xxxxx` (14-29) — 3/5/7/9 bits vs 33 for a literal.
Header size_comp = whole blob length incl. header; unk = 0.

## Repack notes (`repack.py`, `pinkhair.py`)

* TOC `cfile` size fields are patched in place (u32 csize @ fields_off+3,
  u24 usize @ +7); an object's allocation = gap to the next entry's sector.
* Character CLUT recolors keep ARX output size identical in practice.
* The disc embeds copies of `char/pc/kosmos*.xtx` inside
  `yamamoto/pc/kosmos*.bin` (battle bundles: section table `u32 n, u32
  total, u32 off[n]`; lex @ 0x20, XTX findable by magic — these embeds are
  **byte-identical**) and inside the uncompressed per-scene bundles
  `scene/cf0210.a`, `cf0740.a`, `cf1800.a`, `cf3140.a` — 12 hair-palette
  carriers total (verified by disc-wide sweep; quarter-row sweep confirms
  no others).
* **cf*.a re-frames the texture**: XTX magic + sub-header present but the
  entry layout differs and the canvas is stored with 4-byte inserts at a
  ~2020-byte effective row stride, so only some 64-byte CLUT rows survive
  contiguously (cf0210.a: 26/32 half-rows intact, 6 split). A row-level
  sweep alone therefore *partially* patches them — caught in the act via
  PINE RAM forensics: the running game builds the opening-sim KOS-MOS
  from cf0210.a, and its in-RAM CLUT showed exactly the 30 row-patched
  entries pink and 91 blue. Fix: anchored entry-level (aligned 4-byte
  value) replacement; safe because the hair ramp shares no exact RGBA
  word with any other tile in the canvas (verified 0 overlap).
* The engine also bakes scene lighting into CLUTs at load time (RAM
  copies differ from disc in RGB but not alpha), so RAM-vs-file palette
  comparisons must expect tinted variants.

## XTX textures (`browse.py: decode_xtx`) — via xenotool, extended

An `.xtx` is a raw GS memory image, not a picture.

```
0x00  "XTX\0"   u32 total_size   u32 sub_count   u32 header_table_offset
per sub-image (20 bytes each, at header_table_offset + 20*i):
  u16 width  u16 buffer_width  u16 height  u16 pad
  u32 gs_offset  u32 size  u32 file_addr
sub-image pixels: file_addr + 0x20 (32-byte sub-header first — forgetting
                  this shifts everything and scrambles the swizzle subtly)
```

Decode pipeline: compose each sub-image (raster CT32 rows) onto a CT32
canvas — 256 px wide for buffer_width 4, 512 for 8 (0 means 8) — at
x0 = (gs_offset/4096 % (bw/2))*64, y0 = (gs_offset/4096 // (bw/2))*32.
Unswizzle the whole canvas as PSMT8 (the widely shared `unswizzle8`
routine) giving an 8bpp index image at 2x canvas dimensions; crop to the
max extent of non-palette subs.

Palettes: 256-entry CLUTs stored as 16x16 CT32 tiles, CSM1 order (swap the
two middle 8-entry runs of each 32). PS2 alpha is 7-bit: scale
`min(a*2, 255)`. Palette sources, in priority order:

1. Dedicated 16x16 sub-image in the same file.
2. Paired `.lex` model materials (below).
3. Corner-scan: menu/backdrop textures (casino `tanaka/`, dev folders) park
   CLUT tiles in unused canvas corners, addressed by GS block pointer (CBP)
   in the consuming overlay's texture descriptors. OV11.OVL (casino) holds
   28-byte descriptors `{u16 u, v, w-1, h-1, 0, CBP, texslot}`; mapping:
   `page = cbp/32`, pages laid canvas_width/64 per row (64x32 px), block
   within page via the PSMCT32 block table (8x8 px blocks). Standalone we
   scan block-aligned 16x16 tiles (all raw alpha <= 0x80, >= 64 distinct
   colours), conventional spots first: (0,224), (240,240), (224,240),
   (176,240), (112,64), (128,0). 105/109 tanaka files verified correct.

## LEX models — materials only (`browse.py: lex_materials`) — via xenotool

```
LexHeader 0xB0 bytes; u32 nmesh @ 0x44; mesh addr table (u32 each) @ 0xB0.
MeshHeader (at each mesh addr): PaletteInfo @ +0x120 (pal2 @ +0x124,
pal @ +0x125), UVInfo @ +0x130, header is 0x190 bytes.
palette byte -> canvas coords:
  palx = (pal>>4 % 2)*256 + (pal&0xF / 2)*32 + (pal2>>7)*16
  paly = (pal>>4 / 2)*32  + (pal&0xF % 2)*16
UV types: 0xFF -> umin=x*64+x1*32, vmin=y*64+y1*32, umax=umin+(w+1)*16,
vmax=vmin+(h+1)*16 (bitfields per xenotool lex_file.h); 0x0A family ->
umin=(b0&0x3F)<<4, vmin=b2, umax=((b1<<2)|(b0>>6))+1, vmax=((b4<<6)|(b3>>2))+1.
pal == 0xFF -> no palette (direct). Extra materials inside VIF vertex
streams are NOT parsed yet — some atlas regions still get a neighbour's
palette.
```

**Caveat (regression bitten once):** material palx/paly describe *runtime
VRAM* CLUT slots. For most textures the file canvas is laid out to match,
but not always — `simajiri/hama.lex` points at (128,0) while the file's
CLUT tile sits at (64,0), so trusting the pointer paints the image with
empty canvas (blank output). Every palette read must pass a plausibility
check (>= 16 distinct RGB values, some nonzero alpha) before use;
implausible reads fall back embedded-palette -> corner-scan -> grayscale.

## FL00 event containers / Java (`evt.py`)

`.evt` = FL00 wrapper of real Java class files, format 45.3 (JDK 1.1,
stock Sun javac, `SourceFile`/`LineNumberTable` intact). The FL00 table
mixes real classes with 24-byte `cafebabe` stubs and misses classes in
regions the table doesn't describe — so carve by walking the class-file
structure itself (constant pool -> fields -> methods -> attributes) for
exact lengths and true names. Constant-pool name strings are
NUL-terminated (console C-string convenience). ~2,200 unique classes;
`system.evt` ships `java/lang/Object`, `String`, `StringBuffer`.
The constant pools are a goldmine of engine facts readable without a JVM
(stream rates and ids sit next to `streamPlay` refs).

Dialogue facts (established by patching the ST0210 "Virtual Tutorial"
line to French and verifying in-game):

- Rendered scene dialogue = CONSTANT_Utf8 pool entries, stored with
  their layout verbatim: leading spaces for centering, trailing `\n`,
  and the trailing NUL counted in the u2 length (same quirk as the name
  strings above). Control codes are raw bytes here, unlike the `\NN`
  escape text seen in the planner `.txt` sources.
- A same-byte-length replacement is structurally free — nothing after
  the pool is byte-offset-addressed — so `read_entry` →
  `bytes.replace` → `patch_iso` is a complete dialogue edit
  (MODDING.md §5 has the worked example). Length changes shift the pool
  and need the not-yet-built class rewriter + FL00 rebuilder.
- Scene `.evt` objects are uncompressed and **single-copy** in the TOC —
  no texture-style duplicate sweep for dialogue.
- U.M.N. event dialogue is *not* in the classes (string-swept all
  carved classes): it renders from the `umn/event*.txt` text objects —
  textpack territory. The scene-side `cf*.txt` planner sources, by
  contrast, are never read for rendering.
- Renderer encoding for non-ASCII constant-pool bytes (Shift-JIS vs
  modified UTF-8) is still unestablished — open thread.

## Streamed audio `.vds`/`.vdm` (`browse.py: decode_voice_stream`)

Headerless PS2 SPU ADPCM, **stereo, block-interleaved every 0x400 bytes**
(64 frames per channel per block), 48000 Hz. Frame = 16 bytes:
`[filter<<4|shift] [flags] [14 payload bytes]`, filter <= 4, shift <= 12,
flags 0x02 in stream bodies. Predictor: `s = (nib<<(12-shift)) +
trunc((h1*f0 + h2*f1)/64)` with filters (0,0),(60,0),(115,-52),(98,-55),
(122,-60) — division truncates toward zero (matches ffmpeg `adpcm_psx`;
plain >>6 floors and drifts ~1.7% RMS).

Diagnosis lesson: decoded as sequential mono this sounds pitch-correct but
half-speed, "echoy" (37 ms L/R alternation), "tapping" (block-boundary
predictor glitches), choppy in music — and no sample-rate change fixes it.
Detection: deinterleave at candidate block sizes, decode halves, correlate
— sample correlation spikes only at 0x400; channel envelope correlation
~0.95 (a mono stream wrongly split scores ~0). Rate source: scene classes
call `xeno.Sound.streamPlay(_, _, id, 48000)`. Big `.vds` (s29xxxx, up to
20 MB) are cutscene music+voice mixes.

## Sequenced BGM: SMD/SWD (`browse.py: parse_swd, smd_info`)

Procyon Studio format; music is sequenced, not streamed. Composer credit
is embedded in retail files ("Yasunori Mitsuda / PROCYON STUDIO").

```
SWD ("swdm"): u32 body_size @ 0x24, u32 body_offset @ 0x28
  (body_offset + body_size == file size). Sample table @ 0x50, 32-byte
  entries: u32 body-relative offset, 12 param bytes (pitch/ADSR — not
  decoded), 16-byte ASCII name. Table ends at first invalid entry.
  Bodies are 100% valid SPU frames; samples end at frames with flags bit0
  set. Instrument names are real ("Timpani", "F.Horn", "CelloBassSTCC#3").
SMD ("smdm"): NUL-terminated ASCII metadata from 0x2C: title, game,
  composer, studio, note. Size >= ~5 KB separates real music from ambience
  stubs. Sequence body (note events) not yet decoded — an SMD synth is the
  open project for faithful rendering.
SED ("seds" + embedded swdm): SFX banks.
```

Locations: `chain0/sound/smd/` (field/menu + ambience), `chain0/yamamoto/
snd/smd/` (battle themes + ~1 MB banks), `chain0/sound/sed/` (SFX).
Engine side: `SsdPlaySequence`, `command_loadsmd`, `Java_xeno_Sound_
sequencePlay__I`, format string `data\sound\smd\%s.SMD`. Real music
catalogue (10 sequences): Battle1 x2, LastBattle, Escape! x3, U.M.N.Mode
x2, Jingle2 x2.

## Movies (`.pss`) — solved (`browse.py: extract_pss_audio`)

MPEG-2 PS, video 512x448 29.97 fps at stream id 0xE0. Audio is MPEG
**private stream 1 (0xBD)**: each PES payload starts with a 4-byte
substream tag (`ff a1 00 00`); the concatenated payloads form a Sony ADS
stream — `"SShd"` header (u32 header size, u32 fmt 0x10 = SPU ADPCM,
u32 rate 48000, u32 channels 2, u32 interleave 0x400) then `"SSbd"` +
size + body. The game's own header thereby confirms the empirically
derived `.vds` layout. ffmpeg misparses these packets as phantom
"mp2, 0 channels" streams (and its container durations are bogus —
trust the decoded audio length). The kit demuxes 0xBD itself, decodes,
and muxes AAC into the MP4; movies with audio yield `name.mp4` +
`name.video.mp4` + `name.audio.wav` so fan projects get the tracks
divorced. Layer-1 movies (58, carved from outside the ISO filesystem)
carry the audio; the 45 TOC `movie/mpeg2/*.pss` are video-only with
`.vdm` companions.

## Verification techniques that paid off (reusable)

- Cross-decode against ffmpeg's `adpcm_psx` via a VAGp wrap:
  `b'VAGp' + pack('>III', 0x20, 0, size) + pack('>I', rate) + 12*b'\0' + name[16]`.
- SPU-validity scan (fraction of 16-byte frames with legal headers) finds
  raw ADPCM in unknown containers.
- ADPCM predictor-continuity comparison ranks candidate frame orderings.
- Envelope correlation of deinterleaved halves proves/disproves stereo.
- Structural round-trip (re-parse every emitted artifact) catches carver bugs.
- Read constants out of the lifted Java class constant pools instead of
  disassembling MIPS.
- Check community tools (xentax, github) BEFORE brute-forcing GS swizzles.
