# Xenosaga Episode I — format reference (deep notes)

Working notes behind the README's format summaries: exact offsets, layouts,
and the verification evidence, so none of it has to be re-derived. All
byte orders little-endian unless stated. Credit where formats were learned
from [Lakuwu's xenotool](https://github.com/Lakuwu/xenotool) is marked.

## Bigfile TOC (`toc.py`) — reverse-engineered from scratch

Each chain's first bigfile (`XENOSAGA.00` / `XENOSAGA.10`) begins with a
binary table of contents mapping game paths to sector offsets **relative
to the start of the TOC file**; the data region runs seamlessly through
the sibling bigfiles in order. Validated by parsing both TOCs to the byte
(zero desyncs across 8,922 entries) and by matching every recovered path
against the engine's own `data\...` string literals in the unstripped ELF.

```
toc      = [u8 data_base_sector] entry* [0x00] filler
file     = [b]        [name: b-1 bytes]        [u24le sector] [u32le size]
cfile    = [b | 0x40] [name: (b&0x3f)-1 bytes] [u24le sector] [u32le csize] [u24le usize]
dir      = [b | 0x80] [u8 pop_count]           [name: (b&0x7f)-2 bytes]
filler   = "MONOLITHSOFT Xenosaga Episode.1\0" repeated, phase-locked to offset % 32
```

* `dir` pops `pop_count` path levels then pushes its name — the entry
  list is a serialized pre-order walk of the directory tree.
* `cfile` entries (2,095) are ARX-compressed; the stored payload's
  `ARX\0` header sizes match the TOC's csize/usize fields.
* The unused TOC tail is the repeating, offset-phase-locked
  `MONOLITHSOFT` string — a clean end-of-entries sentinel.
* An entry's *allocation* = gap from its start sector to the next
  entry's start sector; `TocEntry.fields_off` (byte offset of the
  sector/size fields within the TOC file) is the repack layer's patch
  hook. Same "region chain" model Episode III later used with its
  `X3.*` files and text `Lba*.txt` tables, just binary and without a
  per-disc split.

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
* **cf*.a members are ARX-compressed in place** (understood 2026-09-24;
  see "Scene archives" below). What earlier looked like a "re-framed"
  canvas with 4-byte inserts at a ~2020-byte stride is the ARX bit
  stream: the coder passes literal 32-bit words through verbatim with
  control words interleaved, so only *some* 64-byte CLUT rows survive
  contiguously (cf0210.a: 26/32 half-rows intact, 6 split). A row-level
  sweep alone therefore *partially* patches them — caught in the act via
  PINE RAM forensics: the running game builds the opening-sim KOS-MOS
  from cf0210.a, and its in-RAM CLUT showed exactly the 30 row-patched
  entries pink and 91 blue. The shipped fix — anchored entry-level
  (aligned 4-byte value) replacement — works because every hair-ramp
  word is a stream literal (none is one of the container's 30 LUT
  words) and shares no exact RGBA word with any other tile (verified 0
  overlap). The principled route is decompress → patch → `arx.compress`
  → write back, which needs the `.a` TOC re-pointed when the container
  size changes; not built.
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
canvas — clen = 256 px wide for buffer_width 4, 512 for 8 (0 means 8) —
at y0 = (gs_offset // (clen*32)) * 32, x0 = (gs_offset % (clen*32)) // 32:
gs_offset counts 32 per pixel column and clen*32 per 32-row band, so a
second part sits at x = 128 per 4096 and a new row starts at 65536
(clen 512) / 32768 (clen 256). Fitted on all 166 multi-sub layouts on
the retail disc. (Until 2026-10-14 the kit used 64-px blocks —
x0 = (gs/4096 % bw/2)*64 — which put the second half of the 42 two-part
character atlases (`npc/andrew`, `pc/jr1`, `pc/momo2`, `npc/virgil1` …)
at x=128 instead of 256, overwriting the first part; those atlases then
failed their .lex palettes and fell back to a monotone scan palette.)
Unswizzle the whole canvas as PSMT8 (the widely shared `unswizzle8`
routine) giving an 8bpp index image at 2x canvas dimensions; crop to the
max extent of non-palette subs.

Palettes: 256-entry CLUTs stored as 16x16 CT32 tiles, CSM1 order (swap the
two middle 8-entry runs of each 32). PS2 alpha is 7-bit: scale
`min(a*2, 255)`. Palette sources, in priority order:

1. Dedicated 16x16 sub-image in the same file.
2. Paired `.lex` model materials (below).
3. Overlay descriptors (`scan_ovl_descriptors` / `scan_ovl_cluts`): the
   consuming overlay addresses the CLUT by GS block pointer (CBP) in
   28-byte texture descriptors `{u32 u, v, w-1, h-1, 0, CBP, texslot}` —
   OV11.OVL (casino) references `data\tanaka\*.xtx` by path string.
   Mapping: `page = cbp/32`, pages laid canvas_width/64 per row (64x32
   px), block within page via the PSMCT32 block table (8x8 px blocks).
   Descriptor->texture binding would need runtime tracing, so each
   referenced texture gets the overlay's whole descriptor list. The
   distinct CBPs are base-palette candidates (least-noisy render wins,
   below) and the descriptors are the sheet's *materials*: a UI sheet is
   multi-palette by design (OV11's card grid draws the 81x121 pip cards
   from CBP 1016, the J/Q/K/Joker from 1020 and the card back from
   1004; the slot sheet keys nine elements to a column of nine CLUTs at
   canvas x=112). `decode_xtx` paints a descriptor *group* (every rect
   sharing one CBP) when its tile is CLUT-like and parked within 32 px
   (chained) of the sheet's base tile, and the group's rects render with
   a median adjacent/far ratio <= 0.9x the base palette's at >= half its
   opacity (text strips legitimately go transparent). Measured on OV11:
   true groups score 0.31-0.85; the same descriptors applied to a sheet
   they do not belong to (poker pay table, exchange, BG — which park
   CLUT-like tiles at the same corner) score 0.99-12. Big rects paint
   first so specific ones win; accepted tiles are blanked from output.
4. Corner-scan: menu/backdrop textures park CLUT tiles in unused canvas
   corners. Scan 16x16 tiles (all raw alpha <= 0x80, >= 64 distinct
   colours), conventional spots first: (0,224), (240,240), (224,240),
   (176,240), (112,64), (128,0); then the 16px grid bottom-right first;
   then the 8px grid (CBPs can address half-block-aligned tiles).

For textures without a `.lex` (UI sheets) the first conventional-spot
candidate wins over the ranked one when it renders 50–95 % opaque and
is, transparency penalty aside, about as coherent as the winner (or the
winner renders the sheet nearly flat): frame-like sheets park their own
CLUT at a corner spot, and the penalty below otherwise hands them a
washed-out palette meant for another sub-image (casino slot frame,
window, pay table; 2026-09-25).

Choosing between palette candidates uses a noise metric
(`_region_noise`): mean L1 RGB distance between horizontally adjacent
opaque pixels — the correct palette renders coherent art (low), a wrong
one renders dither noise (high). The chosen CLUT tile — plus the
connected cluster of palette-looking tiles around it (card sheets park
a strip of colour-variant CLUTs together) — is blanked (transparent)
in the output when it falls inside the visible extent: it is palette
data, not art (the "square of noise in the corner", GitHub issue #1). Per-material repaints are skipped when they render
their UV rect clearly noisier than the base palette (>1.3x), and 64px
blocks no parsed material covers get rescued by the least-noisy known
palette when the base render is clearly garbage (>= 35 noise, winner
< 0.5x). The rescue pool is the parsed material palettes PLUS every
CLUT-looking tile parked in the canvas (map atlases bind their parked
CLUTs through VIF-stream materials the static parser never sees — the
right palette is in the file, just unreferenced; this recovered e.g. the
second DURANDAL sign and starfield blocks of MC_DYU01).

Companion models: a `.lex` with no `.xtx` of its own (kosmos_face.lex,
kosmos_h_face.lex — 105 textures have such companions) binds its
materials onto the sibling atlas whose stem is the longest prefix of the
lex stem (kosmos_h_face -> kosmos_h.xtx). Merging those materials is
ground truth the heuristics can't reach: kosmos_face.lex binds palette
tile (448,112) to rect (896,960,128,192) — KOS-MOS's red eye, which the
coherence ranking misjudged (radial iris art scores high on the
adjacent/far ratio even under the correct palette).

32px trusted-material rescue: blocks outside every painted rect that are
visibly imperfect under the base palette (bn > 0.25) repaint when one of
the PARSED MATERIAL palettes renders them near-perfectly (n < 0.25 and
< 0.6*bn; thresholds fitted on the KOS-MOS atlas — true fixes measured
<= 0.16, false repaints >= 0.51). Outfit art usually continues past a
parsed rect under the same palette. Lex note: materials with pal = 0xFF
(+0x126 = 0x07) are TRUE-COLOUR — their rect is in canvas coords and
marks a raw CT32 region (KOS-MOS meshes 31-36 = the hair sheets).

Known residual: kosmos x384-512 y128-256 is plane-packed 4bpp (each
index byte's two nibbles are two SEPARATE 4bpp images — visor-HUD
symbol sheet + keypad panel, clean in grayscale) whose 16-colour CLUTs
are not in the file. These are NOT GS PSMT4 textures: read through
PCSX2's verified PSMT4 block/column tables the region is noise; it is
PSMT8-swizzled data packed two images per byte by the artists. The
eyelashes (materials of UV type 0x7F, pal 0xFF) are unrelated to this:
their rect (umin = b1*4, vmin = (b4&0xF)*64 + (b3>>7)*32, 32x32 canvas
px) holds a true-colour dark alpha gradient; `decode_xtx` draws it raw
(unguarded) and the lash polygons stretch it into strokes.
Lead (2026-09-25): the lex VIF streams carry GS `TEX0` A+D writes
(u64 value + u64 reg 0x06/0x16). `kosmos.lex` draws PSMT8 from
tbp 14336/10752 (two VRAM slots), tbw 16, 1024x256, with CLUT pointers
15208/15212/15216/14968/14972 — which map (`_cbp_to_xy`, relative to
tbp) onto the canvas tiles (192,112) (208,112) (224,96) (224,80)
(240,80): ground-truth palettes, no heuristics. No `T4` draw exists in
any kosmos lex; the 4bpp draws from that slot come from effect
libraries (`simajiri/esd/eve017.esd`, `eve107.esd`, `boss0185.esd`,
`scene/cf0680.a`): `TEX0` psm T4, tw 10 th 8, cbp 14400/14401 (canvas
(128,0)/(136,0) — art, not a parked CLUT: the effects must upload the
palette themselves). Rendering the nibble planes through the 8x2 words
at those tiles gives noise, so both the effect-side palette upload and
the exact PSMT4-in-CT32 nibble layout are still to be derived.

True-colour materials (pal 0xFF) name their raw-CT32 rect in canvas
coordinates; `lex_truecolor_rects` feeds them to `decode_xtx`, which
draws those rects from the canvas directly (2x nearest, alpha 7→8 bit).
The heuristic below only covers what no material names.

Final pass — raw CT32 regions: a canvas can mix 8bpp paletted art with
TRUE-COLOUR CT32 pixel regions (KOS-MOS/NPC hair-strand sheets). No
palette can render those. Per 64px output block, if the finished render
still measures as dither noise (`_rgba_noise` ratio >= 0.9; correctly
paletted blocks measure below that) and the canvas region read directly
as CT32 is more coherent, the block is drawn straight from the canvas
(each canvas pixel = 2x2 output pixels, alpha scaled 7->8 bit). This
finally renders the KOS-MOS hair band as hair instead of noise. The pass
runs only when the texture has a `.lex` (true-colour regions are a
model feature): on lex-less UI sheets it mis-fired on dithered 8bpp art
(two indices alternating reads as a coherent checker in CT32) and
repainted the casino slot reels — regression found 2026-09-24.
Regions failing both readings stay garbled — the remaining static limit.

## Scene archives `.a` (+ `.fpk`, `.arc`) — ARX-packed members (`browse.py: iter_arx_containers`)

`scene/cf*.a` (112 files, one per field scene) is a flat bundle:

```
u32 count; u32 offset[count]      16-byte-aligned; some entries point INTO a
                                  member (streaming pages), not all are starts
record: 0x100 bytes of float data (placement/lighting; not parsed), then
        the member — usually an ARX container:
  "ARX\0" u32 usize u32 csize(includes this 16-byte header + 30-word LUT)
  u32 0; LUT[30]; bit stream          -> arx.decompress(data[o:o+csize])
member kinds (retail, 112 archives): 1,864 containers = lex 778,
XTX 603, FPK 483. Uncompressed members exist too (JNT\0 joint tables,
raw XTX sprites with their own 16x16 palette, raw 256x256 backdrops).
(`.fpk`, `.arc` and a few `.bin`/`.npr` are whole-file ARX objects the
TOC layer already decompresses; the 44 `mtnpack/*.arc` are FL00 wrappers
of motion data, not Java — `carve_classes` finds nothing in them.)
```

The ARX coder passes literal words through verbatim, so a packed XTX's
**header survives inside the compressed stream**: `XTX\0`, a plausible
total size and a sub-image table whose `file_addr` reads as a VIF DIRECT
code (`0x5000xx01`) — that is what the raw sweep used to report as an
"in-`.a` XTX variant with GS pointers, pixels streamed separately" (528
undecodable hits on retail; all of them). There is no such variant. The
sweep now decompresses every container first (`iter_arx_containers`:
sane header + decompresses to exactly `usize`), skips raw hits that fall
inside a container's span, and decodes the XTX members with the `lex`
members packed beside them as their palette source (nearest member
first — its material 0 seeds the base palette — the rest merged as
companions, same as `kosmos_face.lex` on the standalone atlases).
Result on retail: all 603 decode (0 undecodable) and every one is
byte-identical to a standalone `char/`, `enemy/`, `obj/`, `map/` or
`robo/` `.xtx` — each scene packs private copies of what it draws. The
sweep therefore adds no new art, but `embedded_textures.csv` now maps
every scene archive to the textures it carries (the "12 copies of the
KOS-MOS palette" hunt in the Repack notes, answered disc-wide).

Dead ends worth not repeating: the "GS pointer" fields ARE consistent
(`size` = (w*h*4+32)/16 qwords, addresses chain with no gaps) because
they are the real header words; decoding the bytes after them as pixels
finds coherent-looking art for the first ~60 KB (the stream's literal
words are mostly pixel data) and then noise — the compressed stream is
shorter than the texture, so the "pixels" run into the next member.

## LEX models (`lex.py`) — geometry, materials, skin weights — via xenotool

Structure (Lakuwu's xenotool `lex_file.h`, re-implemented in `lex.py`;
triangle and weight-group counts byte-checked against xenotool on retail):

```
LexHeader 0xB0: magic "lex\0"; name @0x10 (32, "/kosmos"); artist @0x30
  (15, "takami"); u32 nmatrix @0x40, nmesh @0x44; u32 addr[5] @0x50
  (addr[0] = matrix table: nmatrix PAIRS of float[16], each pair = a
  transform and its inverse — row-major, A·B = I); float max[3] @0x70,
  min[3] @0x80, orig[3] @0x90. Mesh address table (u32 × nmesh) @0xB0.
MeshHeader 0x190 at each mesh addr: bone_name[32] @0; u32 weight_format
  @0x20, data_offset @0x24 (0x190), data_len @0x28, bone_idx @0x2C (index
  into the matrix pairs); u16 bone_table[32] @0x30 (bone_table[0] =
  bone_idx+1, then the weight-group ids this mesh skins to); float
  max/min/orig @0x70/0x80/0x90; u8 vertex_format @0xC0 (& 0xF0: 0x80 =
  floats with normals, 0x10 = no normals, byte colours); MaterialColor
  3×RGBA floats @0xD0; PaletteInfo @0x120 (pal2 @+4, pal @+5); UVInfo
  @0x130 (16 B); group_name[32] @0x150; material_name[32] @0x170.
Mesh data: VU1 display list of VIF codes (u16 imm, u8 num, u8 cmd):
  0x00 NOP, 0x01 STCYCL, 0x20 STMASK (+4 B), 0x6x/0x7x UNPACK (type =
  cmd & 0xF: components = (type>>2)+1, bytes = (4,2,1)[type&3]; VU addr
  = imm & 0x3FF), 0x17 MSCNT = "run the micro-program on what was
  uploaded" = one strip batch.
  An UNPACK to addr 0 starts with a 16-B block header: u8 count (strip
  vertices) @0, u8 format @4, u8 tag @5. tag 0x40 = a MATERIAL BLOCK
  follows instead of vertices (128 B: 16 unk, UVInfo, PaletteInfo,
  PaletteInfo, MaterialColor 48, 16 unk — or 64 B without colours): the
  material for every later strip of this mesh. Otherwise the batch is:
    vertex_format 0x80: upload A = count × float4 (x,y,z,u) then count ×
      float4 (nx,ny,nz,v); upload B = count × float4 RGBA (/128 = 1.0);
      upload C (weights) when weight_format != 0. (A single upload may
      hold A+B+C back to back.)
    vertex_format 0x10: A = count × float4 (x,y,z,u); B = count × float v;
      C/D = colours as bytes (/128) and weights.
  weight_format 1: C = count × 4 u32 bone refs, then count × 4 float
    weights; 3: count × 4 u32 refs (weight 1.0 each); 5: per vertex 2 u32
    refs + 2 float weights. A bone ref r maps to bone_table[r/4 - 184 + 1]
    (VU address of the bone matrix slot -> weight-group id); 0 = unused.
    1024 = shape-key data (no skin). Weight-group ids are shared with the
    .jnt skeleton (KOS-MOS: 46 groups, ids 53-98).
  Batches whose first UNPACK is NOT to addr 0 (three single-row uploads
    to 0x3C8/0x3CA/0x3F0) are VU constant uploads, not vertices — skip
    them (xenotool mis-decodes those as stale-count garbage strips).
  Strip: triangles (v, v+1, v+2) for v in 0..count-3, winding flipped on
    odd v. Positions are MODEL SPACE already (KOS-MOS is 1.73 units tall,
    y up); the matrix table is not needed to assemble the mesh.
UV: GS ST coordinates — texel = (s × 2^TW, t × 2^TH) with TW/TH from the
  TEX0 register writes in the model's own VIF streams (`_tex0_size`:
  u64 value + u64 reg 0x06/0x16; 1024×256 for character sheets) — NOT the
  decoded atlas size, which can be narrower (Cherenkov 928×256);
  then wrap each TRIANGLE by whole periods into the material rect (GS
  region-repeat) — per primitive, not per vertex, or seam triangles tear.
  True-colour materials (pal 0xFF) give their rect in canvas coordinates:
  double it (the CT32 hair sheets).
Materials (UVInfo): type 0x00 = untextured; 0xFF: umin = x*64 + x1*32,
  vmin = y*64 + y1*32, umax = umin + (w+1)*16, vmax = vmin + (h+1)*16
  (bitfields per xenotool); 0x0A family: umin = (b0&0x3F)<<4, vmin = b2,
  umax = ((b1<<2)|(b0>>6))+1, vmax = ((b4<<6)|(b3>>2))+1.
Palette byte -> canvas tile: palx = (pal>>4 % 2)*256 + (pal&0xF / 2)*32 +
  (pal2>>7)*16; paly = (pal>>4 / 2)*32 + (pal&0xF % 2)*16; pal == 0xFF ->
  no palette (true colour). `browse.lex_materials` takes its palette
  bindings from `lex.parse_lex` — mesh headers AND the VIF-embedded
  material blocks (a mesh switches palette mid-stream: Cherenkov's
  jacket draws its sleeves from a second CLUT only the in-stream block
  names).
Not decoded: vertex formats 0x30/0xA0 (2 map files); weight formats
  515/517. The `.jnt` skeleton is decoded (next section).
Export (`lex.py: to_glb`): glTF 2.0 binary, one primitive per
  (mesh, material), PNG atlas embedded, COLOR_0 vertex colours, a flat
  skin of identity-bound joints named `j<id>` so Blender gets the vertex
  groups; per-lex `.glb` under `browse/models/`.
```

**Caveat:** every palette read must pass a plausibility check (>= 16
distinct RGB values, some nonzero alpha) before use; implausible reads
fall back embedded-palette -> overlay -> corner-scan -> grayscale. (The
old note that `simajiri/hama.lex` "points at (128,0) while the file's
CLUT tile sits at (64,0)" was the sub-image placement bug above — with
the correct placement the tile IS at (128,0).)

## JNT skeletons (`lex.py: parse_jnt`) — hierarchy and rest pose

```
Header 0x20: "JNT\0"; u16 @4 (3); u16 block_count @6; u16 @8 (74 on
  every file — not a count); u16 @0xA; u16 @0xC; u16 @0xE; u32 table
  offset @0x10 (0xF0); u16 @0x14; u16 @0x16; name[8] @0x18 ("KOSMOS").
0x20..table: 52 u32 — [17..42] maps the model's 26 mesh bones (lex
  bone_idx) to type-1 blocks near the end of the table (0 = none).
Blocks: 64 bytes each at `table offset`:
  +0  float, +8 float      per-joint constants (limits?), often 0
  +16 u16 type             1 offset node, 2 ROTATION JOINT, 3 translate,
                           4 bone end (pos.x = length), 6 name record
  +18 u16 flag             0..3 / 6, meaning unknown (not a rotation order)
  +30 u16 parent           block index; self or out of range = root
  +32 float3 position      local, metres
  +48 float3 rotation      local Euler radians, applied as Rz * Ry * Rx
  type 6: ascii group[8] @+0 ("BODY"/"FACE"), label[16] @+8 ("KUBI")
Skin joints = the type-2 blocks in order: the k-th weight-group id of the
model (ids are contiguous per model) is the k-th rotation block —
checked on 8 models (46/46, 36/36, 26/26, 47/47 …).
World = parent_world * T(position) * R(rotation). Verified by the
vertex centroids weighted to each joint: hips/knees/ankles/toes land at
0.98/0.54/0.16/0.01 m on KOS-MOS, mirrored; face .jnt joints already
land in body space (eyes within 2 cm of the eye meshes).
Rotation convention, the part that bit: the stored triple is (angle
about Z, angle about Y, angle about X), R = Rz(a)·Ry(b)·Rx(c). Read as
(x, y, z) it produces a convincing-looking T-pose with the hair raised
while legs and spine still match, which cost an afternoon. Settled by
reading the engine's own per-node matrix table out of the running game
(EE RAM over PINE, `docs/research/pine_pose.py`; one 64-byte entry per
block in block order, row-vector convention with w = 1 in each row's
4th slot, entry 0 = field placement) and brute-forcing axis labelling ×
composition order × sign against it: Z,Y,X is exact (0.0000 m over 120
nodes), every other reading is off by 0.02–1.5 m. With it, joints sit
on their weighted vertices on every model tried (mean 0.07–0.12 m, the
half-bone offset you expect). No capture is needed; the tool stays as
the verification method.
```

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

## Sequenced BGM: SMD/SWD — solved (`ssd.py`, renderer in `ssd_render.py`)

Procyon Studio format; music is sequenced, not streamed. Composer credit
is embedded in retail files ("Yasunori Mitsuda / PROCYON STUDIO").
Fully decoded 2026-07-19 from the game's own IOP driver: **SSD.IRX ships
unstripped** — every opcode handler is symbol-named, and the dispatch
table (`SsdSeqFuncTrap` @ .data 0xE740) + operand-length table
(`SsdSeqFuncLength` @ 0xEFC0) give the complete opcode map without
guessing. `ssd.py` converts SMD->MIDI and SWD->SoundFont 2;
`ssd_render.py` (numpy) renders WAV with SPU ADSR emulation;
`cli.py music-export` drives it all.

```
SMD ("smdm"): u32 size @ 8, metadata strings @ 0x2C. The u8 @ 0x20
  (retail: 100/120/123/127) is NOT a timebase. Timing is fixed in the
  driver: SsdInitTimer arms a 2 ms tick (USec2SysClock(2_000_000)/1000,
  handler SsdMainInterruptProcess); each tick subtracts rate@+0x44 from a
  16.16 accumulator @+0x40, one sequence tick per underflow.
  SsdSeqTempoAbsolute: rate = ((tempo*53687)>>8 * master)>>8, master =
  s16 upper half of the 16.16 word SsdSetSeqMasterTempo stores @+0x7c
  (0x100 = neutral). Net: ticks/sec = 500*rate/65536 = 1.6*tempo =
  tempo * 96/60 -> the timebase is 96 PPQN, and the 0x9C tempo byte is
  literal BPM. (First-pass tooling read 0x20 as PPQN and played battle
  themes 25% fast; U.M.N. Mode's 100 was only 4% off, masking the bug.)
  Chunks from 0x28: [u16 type, u16 size]:
  type 2 = metadata, type 3 = track (u8 midi_ch @ +6, events from +8),
  type 0 = end.
  Events < 0x80: note-on; event byte IS the velocity. Next byte:
  [7:6] gate-byte count (0 = reuse last), [5:4] octave step -1..+2,
  [3:0] semitone; key = running_octave + semitone; gate bytes big-endian
  = duration in ticks. Selected opcodes (operands little-endian; the
  full 0x80-0xFF map is the SsdSeqFuncTrap dump in ssd.py):
    0x80 wait=gate     0x81 wait=last-delta   0x82 wait=last-delta+s8
    0x83 wait=gate+s8  0x84/85/86 wait u8/u16/u24 (LE!)
    0x88 rest u16 (keyoff)   0x89 tie u16 (extends note)
    0x90 Stop / 0x91 Repeat = the MASTER LOOP: SsdSeqRepeat@0x6b64 stores the
      current stream ptr as the loop-return point; SsdSeqStop@0x6af8 jumps back
      to it forever (all 115 retail music tracks loop this way; loop period =
      Stop_tick - Repeat_tick). 0x92 jump s16-rel is a forward skip, NOT the
      loop. 0x93 if-signal (offline: never taken).
    0x94 octave=n*12   0x95..97 octave rel/up/down
    0x98 looptop u8-count (0=infinite)  0x99 loopend (12-byte stack
    frames at track+0x80, saves stream pos + octave)
    0x9C tempo=BPM     0x9D tempo rel    0xAC program change
    0xD0/D1 key transpose abs/rel   0xD2 tune (SsdSeqTune = operand*8 note16)
    0xD4 bender s16 note16   0xDF expression   0xE0/E1 volume abs/rel
    0xE8/E9 pan abs/rel
  Gotchas: (1) driver symbol names for 0x82/0x83 (AddGate/AddAfterDelta) are
  swapped relative to the fields they actually read. (2) The dispatcher
  advances by each handler's RETURN value, not the SsdSeqFuncLength table, so
  a few operand lengths differ from that table -- notably 0xFD SMPTEOffset is
  5 bytes (every SMD opens track0 with `fd 00*5`; a 1-byte misparse injects
  two phantom tick-0 note-ons). 0xF9/0xFA/0xFB Label/SMPTE = 1/3/2 bytes.
SWD ("swdm"): u16 bank_id @ 0x12, u8 program_count @ 0x15,
  u32 body_size @ 0x24, u32 body_offset @ 0x28. Chunks from 0x40 (same
  [type,size] scheme): type 3 = sample table, 32-byte entries:
    u32 body-rel offset, u32 loop-start, u8 volume, u8 pan,
    s16 base_pitch, u16 ADSR1, u16 ADSR2, char name[16]
  type 4 = program chunk: u16 offset table (0 = absent) -> program:
    u8 split_count @ +3, LFO params @ +0x10, 16-byte splits @ +0x60:
    u8 sample, u8 root_key, u8 key_lo, u8 key_hi, u8 volume, u8 pan,
    u8 flags, u8 pad, u16 ADSR1, u16 ADSR2 (raw SPU2 registers).
  Pitch is exact equal temperament (table-verified against
  SsdAllPitchTable): SPU pitch = 0x1000 * 2^((note16/256 - 60)/12),
  note16 = base_pitch + (note + 60 - root_key) << 8. So a sample's
  native rate = 48000 * 2^(base_pitch/256/12) (retail banks decode to
  22.05/32/44.1/48 kHz +- per-sample tuning cents). Carve each sample from
  its offset to the SPU end-frame flag (byte[base+1] bit0 set); the body is
  one DMA blob and there is NO next-offset concept -- two entries at the same
  offset (common: sample 0 + a trailing placeholder) both decode fully.
  Loop points come from the ADPCM stream flags (bit2 = loop start frame, end
  frame bit1 = repeat); the sample-entry loop field is unused (0x20000).
  Instrument names are real ("Timpani", "F.Horn", "CelloBassSTCC#3").
  Levels: the driver SQUARES the combined voice volume before the SPU VOLL/R
  register (SsdDeviceVoiceEvent@0x65fc), so a faithful render squares
  vel*chanvol*expr*splitvol*samplevol (ADSR is applied linearly upstream).
SED ("seds" + embedded swdm): SFX banks; ENV_* music banks live in
  sound/sed/ as name-matched SWDs next to their sound/smd/ sequences.
Battle sed wrapper (yamamoto/snd/sed/*.bin, 535 files): the battle
  voice/SE banks — per-character attack banks (km_/so_/jr_/mo_/cs_/z8_
  x _at/_bst/_sp/_bed/_wp), 125 boss* banks, et* enemy-tech banks.
    u32 chunk_count (even, 2..10), u32 total_size (== file size),
    chunk_count x u32 ascending chunk offsets; chunks alternate "seds"
    (SFX program metadata) and "swdm" (a standard wave bank, parsed by
    parse_swd like any .SWD). Per-sample native rate comes from
    base_pitch as above — 32007/30007/22053 Hz-style values are
    intentional cents detune, keep them. Quirk: a few banks (boss033,
    fere001, guno002, guno102, jr, utma003) lead their sample table
    with zero-named placeholder entries; skip those slots, don't stop.
    Samples named X.aif.L/X.aif.R, X_L/X_R or X.L/X.R are stereo
    halves of one recording (238 pairs on the disc).
  chain0/sound/sed/*.SED are the same seds payload without the outer
  .bin wrapper; their samples ship in the sibling name-matched .SWD.
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


## Fonts (`font.py`) — layout via XS1KOR, addressing from the executable

`font0.tex` (dialogue face) and `font1.tex` (menu face), chain 0, one copy
each, 492,096 bytes on the USA disc (491,776 on the Japanese one). The GS
layout was worked out by poketony for [XS1KOR](https://github.com/poketony/XS1KOR),
the Korean localisation of the Japanese disc; it is re-implemented in
`font.py` from that description. The addressing and metrics rules below
come from the unstripped `SLUS_204.69` (`xglFontGetKanjiClutUV`,
`xglFontCheckProportional`, `xglFontGetProportionalSize`,
`xglFontAscii2Euc`, `xglFontGetStringWidth2`) and were checked against the
rendered sheets: for every ASCII glyph the metrics pair equals the inked
columns of the cell the mapping picks.

```
0x00000  0x20 header: 12 zero bytes, BE u16 0x013C 0x0051 0x00BC 0 0 0x0008, zeros
0x00020  block 0, 0x3C000 bytes          -> sheets 0, 1
0x3C020  0x20 header (identical)
0x3C040  block 1, 0x3C000 bytes          -> sheets 2, 3
0x78040  metrics: (left, right) byte pairs per proportional slot
         USA 256 pairs (to 0x78240); JP 96 pairs (to 0x78100)
```

A block is a 320x192 PSMCT32 upload (`dbw` 0x140) that the engine reads
back as a 640x768 PSMT4 texture (six 128-row tiles, `dbw` 64): walk the
PSMCT32 swizzle to place the words in GS memory, then the PSMT4 swizzle
to read nibbles. Nibble bits 0-1 are one 4-level sheet, bits 2-3 another.
A sheet is 32 x 32 cells of 20 x 24 px; a file holds 4,096 cells.

**Cells.** JIS X 0208 order, 94 per row from cell 0, with two folds:

| cell | content |
|---|---|
| 0 .. 657 | rows 1-7 (symbols, digits, Latin, kana, Greek, Cyrillic) |
| 658 .. 751 | row 13 (circled numbers, units) *and* row 8 (box drawing) — the engine maps both here; row 8's glyphs in the shipped sheet sit 33 cells earlier, so box drawing was never drawable |
| 752 .. 3665 | rows 16-46 kanji, linear (`752 + (row-16)*94 + cell`) |
| 3666 .. 3743 | row 47: the last level-1 kanji, then 32 extra kanji the USA script needs |
| 3744 .. 4031 | USA block: 32 more kanji (codes 0xD0A1..0xD0C0), then the **256 proportional cells** (0xD0C1..0xD3xx) |
| 4032 .. 4095 | a tiny ruby face (kana + ASCII at half size) |

Rows 9-12 and 14-15 are unassigned in JIS and alias kanji cells — they
are not free space. `cell_of_code()` is the engine's formula:
`94*hi + lo - 0xA1`, plus `0xC4E2` for rows below 13, `0xC30C` for rows
13-15, `0xC250` from row 16 (all mod 0x10000). Cell → sheet `>> 10`, row
`(>> 5) & 31`, column `& 31`.

**Proportional text** (what dialogue uses) does not draw the full-width
row-3 Latin cells. `xglFontCheckProportional` turns a code into a slot
0..255 (digits 0-9, A-Z 11-36, space 37, a-z 38-63; the USA punctuation
and Latin-1 rows through a bit permutation that `font.prop_index()`
reproduces), the metrics pair for that slot is `(left, right)` — the
inked columns of the glyph's cell — and the drawn advance is
`right - left + 3`. A pair whose `left` is 0 means "no metrics": the
engine substitutes a default width, so new proportional glyphs must ink
from column 1. The cell drawn for a slot is `3744 + n` where *n* is the
code's offset from 0xD0A1 in 94-per-row order (`font.cell_of_prop()`), so
the whole proportional face is the eight rows 3776..4031 of sheet 3. Slot
usage on the USA disc: 229 of 256.

`xglFontAscii2Euc` maps ASCII bytes to these codes (table in `font.py`:
letters and digits to row 3, punctuation to the 0xD1/0xD2 block, `\` to
the yen cell) and `#xx` in a string selects code 0xD0A1 + 0x20 + xx.
Two-byte codes written raw in text take exactly the same path as the
output of that table (`xglFontGetStringWidth2` classifies bytes ≥ 0x80 as
code pairs and calls `CheckProportional` on them), which is what lets a
translation use the proportional block for a new script.

Mail text (`.uml`) starts with `15 02 04 07`: control 0x15 sets the
width mode byte; mode non-zero makes every glyph advance a fixed 10 px.
Control-code operand lengths (`xglFontGetSPcodeSize`): one byte for
0x01-0x05, 0x0D, 0x18, 0x19, 0x1B; three for 0x06, 0x0C, 0x0F; five for
0x0E, 0x10; six 0x11; two 0x12, 0x13; seven 0x17; 0x08 takes a flag byte
plus 2/2/3/2/2 bytes per set bit 0-4; 0x15 takes a length byte and that
many more. `font.control_length()` encodes the table.

## UML mail objects — text region may grow

```
0x00  "UML\0" "@@@@"  flags…
0x20  u32 image block start (= end of the text region, 16-byte aligned)
0x28  (u32 image offset, u32 display position) pairs, up to 7, 0 = unused
0x60  EUC-JP text with control codes, NUL-terminated, space-padded
...   JPEG stream(s) + tail
```

The pointer table and the alignment rule are from XS1KOR's `uml_tool.py`.
`textpack.uml_rebuild()` keeps the retail layout while the text fits the
old slot; past it, the image block moves to the next 16-byte boundary
after the NUL and every pointer at 0x20/0x28+8k inside the old block is
shifted. Verified: identity rebuild of all 326 mails is byte-exact.

## FL00 rebuild (`evttext.py`) — when a class changes size

Splice the new class payload at its table row's `data_off`, then add the
size delta to every later row's `name_off` and `data_off`, to the
directory-name offset at 0x0C, and set 0x08 to the new file length. The
entry table itself never changes size. This is the shift rule XS1KOR's
`xeno_evt.py` established; `evttext.selftest` confirms an identity rebuild
of all 567 scene/cutscene containers (chains 0, 1 and 2) is byte-exact.
`base.evt`/`system.evt` carry extra rows between the table and the first
class and are refused (nothing to translate in them anyway).

Class files are re-serialised from their parsed constant pool, so a
`CONSTANT_Utf8` may take any length up to 65,535 bytes; the packer's
trailing NUL is preserved when present.

## TOC: three chains, and relocated objects

The second layer of the DVD-9 carries its own ISO9660 volume (PVD at
sector 2,077,328 = layer 0's volume-space count; file LBAs relative to
2,077,312) with `XENOSAGA.20`-`.24`: **chain 2**, 266 objects — 89
cutscene `.evt`, 58 movies, 44 voice streams, 42 `.fpk`, 33 `.arc` — none
of which are in chains 0/1. Same TOC grammar; `iso9660.py` walks both
volumes and `chains.py` lists all three chains.

Every chain is packed to the sector: the only slack is inside the last
sector of each object. The engine addresses objects by raw sector
(`xglCdReadFile` → `sceCdRead`, LBA = TOC file LBA + entry sector), so
`repack.patch_iso(grow=True)` appends an object that outgrew its slot to
the end of the image, repoints the entry's u24 sector and bumps the
layer-1 volume-space count. The image was 4,134,912 sectors; the DVD-9
ceiling is 4,173,824, about 78 MB of growth before it stops being
burnable (emulators do not care). The idea of relocating instead of
rebuilding is XS1KOR's; the append placement is this kit's, forced by the
packed layout.

## Code overlays `OV01`-`OV12` and the strings in them (`ovltext.py`)

Five plain ELF files at the disc root, loaded by `xglCdLoadOverlay` with
`sceSifLoadElf("cdrom0:\OVnn.OVL;1")` into one shared region: `OV01`
(field and battle, 0xA00000-0xA5B8F8, one `PT_LOAD`, no bss), `OV02`,
`OV10`, `OV12` at 0x9FF000 and `OV11` at 0xA00000, each shorter than
OV01. Each keeps its `.symtab` (1,081 sized functions in OV01). The
boot ELF's bss ends at 0x99D910, and 0xA5B8F8-0xA80000 reads as zero on
the running game with OV01 loaded, so an overlay may grow at the end of
its load segment.

Battle and menu words are C literals in these files: OV01 holds the tech
names (`NEEDLE`, `BLASTER`…, pointed at from the `btst` table), `End`,
`Menu`, `Ether List`, `Items List`, every item/skill description; the
boot ELF holds the field menu's `Skills / Ether / Items`. Strings are
referenced two ways: 32-bit pointer words in data, and `lui`/`addiu`
pairs in code (a `lui` is often shared by several addresses, and the
`addiu` may sit in a branch arm far from it). `ovltext.Overlay` lists
only strings that have at least one reference (2,614 in OV01, 9,335 in
all six files) and rewrites them: in place when the new bytes fit up to
the next non-zero byte, otherwise appended to the load segment with
`p_filesz`/`p_memsz`, the section size and `e_shoff` adjusted and every
reference repointed; a shared `lui` whose high half would have to change
blocks the move and is reported instead. On the ISO the overlay is
written back in place when the next extent leaves room (870 bytes after
OV01) or appended to the image with its directory record's extent and
length repointed (`repack.relocate_fs_file`). Verified on PCSX2: seven
OV01 strings moved to 0xA5B8F8+ and drawn by the battle menu.

## The battle screen's font (`statfont.py`)

The battle HUD does not use font0/font1. OV01's `menuFontPut` draws from
a 25-column grid of **10×18-pixel cells** inside the status sheet
`yamamoto\stat\stat.xtx` (first sub-image, 384×128 CT32 = 768×256 as
PSMT8; the grid's cell 0 is at PSMT8 x=512, y=0; TEX0 is set up by
`menuFontEnvMake` as PSMT8, buffer width 16, 1024×256). The glyph for a
byte is `menuFontIdxGet`: index of the byte in a 230-byte table
`" 0123456789A-Za-z"` + 84 spaces + `.` (147) + `'` (152) + `/` (175) +
`-` (195); bytes with bit 7 set go to `menuDFontIdxGet`, a 2-byte table
whose matches draw cells 175+ (`／ 　 ・ ＾` and a few more); and the
sequence `#XX` (two hex digits, read with `sscanf("%X")`) draws cell
`XX` directly. Cells 63–174 except the four punctuation marks (110
cells) hold accented Latin no USA byte reaches. Cell pixels use index 4
for the solid stroke and 5–15 as anti-aliasing shades (0 = clear).
`statfont scan` paints new glyphs into those free cells and gives each a
byte the table lacks (the 28 printable punctuation marks it does not
list, then the control range minus tab/LF/CR: 56 bytes); `statfont
encode` writes the strings with those bytes and emits the rewritten
table as the `@a46af8` line of the overlay tree (the table is a
NUL-terminated string the code points at, so `ovl-import` writes it in
place). The HUD centres a label by its byte count, which is why the
`#XX` escape (`encode --escape`, three bytes a letter) draws the right
glyphs but starts the label too far left. `srcRow` passed to
`menuStatSprite` is unused; the cell's (x, y) is the UV.
