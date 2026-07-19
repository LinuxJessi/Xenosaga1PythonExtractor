# Doing it by hand: Pink KOS-MOS with no kit at all

A thought experiment: this repo, `pinkhair.py`, `arx.py`, `toc.py` — none of it
exists. You have a disc image, a hex editor, Python, GIMP, and PCSX2. This is
the actual command-by-command, format-by-format path from "blank ISO" to
"pink hair, verified in-game." Every byte layout below is real (cross-checked
against [FORMATS.md](FORMATS.md) and [MODDING.md](MODDING.md)); what's
different here is *how you'd arrive at it* using generic tools instead of
this kit, and the literal commands you'd type.

Tool list, once: a hex editor (ImHex or 010 Editor — ImHex is free and has a
pattern-language that doubles as executable format documentation), Python 3
with `Pillow` and `numpy`, GIMP, `dd`/`cmp`/`xxd`/`strings` (coreutils), an
ISO9660 reader (`7z l game.iso` or `isoinfo -l`), and PCSX2 with its built-in
memory viewer/debugger.

---

## Step 1 — Confirm what kind of disc this is

```sh
ls -la "Xenosaga Episode I (USA).iso"          # 8.47 GB — bigger than a
                                                # single-layer ISO9660 volume
                                                # descriptor will admit to
isoinfo -d -i "Xenosaga Episode I (USA).iso"   # volume size in the PVD vs
                                                # actual file size mismatch
                                                # => dual-layer disc, layer
                                                # break not reflected in the
                                                # filesystem metadata
7z l "Xenosaga Episode I (USA).iso"            # list the ISO9660 tree
```

`7z l` shows `SLUS_204.69`, `OV01.OVL`…`OV12.OVL`, 13 `.IRX` files, and seven
oddly generic names: `XENOSAGA.00`…`XENOSAGA.02` and `XENOSAGA.10`…`.13`,
each hundreds of MB to low GB. Everything else the game needs (models,
textures, sound, scripts — thousands of files) is *not* in this listing. That
tells you those seven files are containers with their own internal directory
structure the ISO layer knows nothing about.

Extract them:

```sh
7z x "Xenosaga Episode I (USA).iso" XENOSAGA.00 XENOSAGA.01 XENOSAGA.02 \
                                     XENOSAGA.10 XENOSAGA.11 XENOSAGA.12 XENOSAGA.13
```

## Step 2 — Find the bigfile chains

```sh
xxd XENOSAGA.00 | head -20
xxd XENOSAGA.01 | head -5
```

`XENOSAGA.01`/`.02` start mid-stream (no header signature) while
`XENOSAGA.00` starts with structured binary data — that's your first file of
a chain. Concatenate on that hypothesis and see if offsets inside make sense
later:

```sh
cat XENOSAGA.00 XENOSAGA.01 XENOSAGA.02 > chain0.bin   # 1.43 GB, "chain 0"
cat XENOSAGA.10 XENOSAGA.11 XENOSAGA.12 XENOSAGA.13 > chain1.bin  # 2.82 GB
```

## Step 3 — Reverse the table of contents by hand

```sh
strings -n 8 chain0.bin | sort | uniq -c | sort -rn | head -20
```

One string dominates by a wide margin: `MONOLITHSOFT Xenosaga Episode.1`,
repeating. Repeating filler at a fixed phase inside an otherwise-dense binary
region is the classic signature of "end of a table, rest is padding" — that
tells you roughly where the table ends inside `chain0.bin`.

Open `chain0.bin` in ImHex from offset 0. Byte 0 is a small integer (a base
sector number). What follows is *not* fixed-width — pull up the hex view next
to an ASCII column and you'll see length-prefixed strings that look like path
fragments (`char`, `pc`, `kosmos.xtx`, `scene`, …), each preceded by a
length/flag byte, each followed by what look like a 3-byte little-endian
number and a 4-byte little-endian number. Test the hypothesis in Python:

```python
import struct

def parse_toc(buf):
    off = 1  # skip base-sector byte
    entries = []
    while True:
        b = buf[off]
        if b == 0:
            break                      # end of entries, filler starts here
        is_dir = b & 0x80
        is_compressed = b & 0x40
        namelen = (b & (0x7f if is_dir else 0x3f)) - (2 if is_dir else 1)
        if is_dir:
            pop = buf[off+1]
            name = buf[off+2:off+2+namelen]
            off += 2 + namelen
        else:
            name = buf[off+1:off+1+namelen]
            fields_off = off + 1 + namelen
            if is_compressed:
                sector, csize, usize = struct.unpack_from("<I", buf, fields_off)[0] & 0xFFFFFF, \
                    struct.unpack_from("<I", buf, fields_off+3)[0], \
                    struct.unpack_from("<I", buf, fields_off+7)[0] & 0xFFFFFF
                off = fields_off + 10
            else:
                sector = struct.unpack_from("<I", buf, fields_off)[0] & 0xFFFFFF
                size = struct.unpack_from("<I", buf, fields_off+3)[0]
                off = fields_off + 7
            entries.append(name)
    return entries
```

Run it, count entries, and sanity-check: does the count look like a
plausible file count for a PS2 game (thousands, not millions)? Do
reconstructed paths (walking the dir push/pop bytes) look like real game
paths (`char\pc\kosmos.xtx`, `scene\ST0210.evt`)? If the loop runs off the
end or produces garbage names, your field widths are wrong — this is
iterative: adjust byte widths, re-run, until every entry parses cleanly to
the filler string with zero left over. (This kit's build took 8,922 entries
parsing byte-perfect as the acceptance bar — matching every path against
`data\...` string literals already visible via `strings` on the unstripped
main ELF `SLUS_204.69` is what proves the grammar, not just "it didn't
crash.")

## Step 4 — Pull the KOS-MOS texture out

```python
entries = parse_toc_full(open("chain0.bin","rb").read())  # your fuller version, keeping sector/csize
kosmos = [e for e in entries if b"kosmos" in e.name.lower()]
```

You'll get several hits: `char\pc\kosmos.xtx`, `kosmos1.xtx`,
`kosmos_h.xtx`, etc. — already a hint that there's more than one copy before
you've even looked at battle bundles or scenes.

```sh
python3 - <<'EOF'
data = open("chain0.bin","rb").read()
entry = kosmos_entry   # sector, csize from your parser
sector_size = 2048
offset = entry.sector * sector_size
blob = data[offset:offset+entry.csize]
open("kosmos.raw","wb").write(blob)
EOF
xxd kosmos.raw | head -3
```

`kosmos.raw` does *not* start with a texture magic. It starts with 4 bytes
that read as ASCII `ARX\0`. Compressed.

## Step 5 — Reverse ARX by hand

```sh
xxd kosmos.raw | head -10
```

Header: `ARX\0`, then two u32 fields that — checked against the file size —
look like "original size" and "this file's size." Then a fixed run of 30 u32
values that don't look like pointers (too small, too repetitive across
different compressed files) — a dictionary/LUT of common 32-bit words.

The stream after the LUT has to be a bitstream (compressed data is smaller
than plaintext with a 30-word dictionary only if you're saving several bits
per hit, which means sub-byte control codes, not byte-aligned ones). Rather
than guess blind, pull a handful of compressed blobs of increasing size and
look for the simplest structural invariant: a control word whose bits are
consumed MSB-first, where each 0-bit means "next raw u32 is literal" and each
1-bit begins a variable-length prefix code selecting one of the 30 LUT
slots. Write a decoder, and use the acceptance test that actually proves
you got it right: **decompressed size equals the header's stated original
size, and the first bytes of the result are a recognizable magic** (texture
files start `XTX\0`, event containers start `FL00`). Run it over every
compressed object in `chain0.bin`/`chain1.bin` — if literally all of them
land on a valid magic and the exact stated length, your bitstream reverse is
correct, not lucky.

```python
def arx_decompress(blob):
    import struct
    assert blob[:4] == b"ARX\0"
    size_orig, size_comp, _unk = struct.unpack_from("<III", blob, 4)
    lut = struct.unpack_from("<30I", blob, 16)
    out = bytearray()
    pos = 16 + 30*4
    control = 0
    bits_left = 0
    while len(out) < size_orig:
        if bits_left == 0:
            control = struct.unpack_from("<I", blob, pos)[0]; pos += 4
            bits_left = 32
        bit = (control >> 31) & 1
        control = (control << 1) & 0xFFFFFFFF
        bits_left -= 1
        if bit == 0:
            word = struct.unpack_from("<I", blob, pos)[0]; pos += 4
        else:
            # read a short prefix (2/4/6/8 bits) to pick a LUT slot,
            # consuming from `control`/refilling as it runs out —
            # this is the part you tune against real files until every
            # blob round-trips to size_orig with a valid magic.
            word = lut[pick_slot(control, bits_left)]
        out += struct.pack("<I", word)
    return bytes(out)
```

```sh
python3 -c "
import glob
for f in glob.glob('extracted/*.bin'):
    d = open(f,'rb').read()
    if d[:4] == b'ARX\x00':
        out = arx_decompress(d)
        assert out[:4] in (b'XTX\x00', b'FL00'), f
"
```

## Step 6 — Decode the texture (XTX) format

```sh
xxd kosmos.decompressed | head -5
```

`XTX\0`, then a total-size u32, a sub-image count u32, and an offset to a
sub-image table. Each sub-image record is a fixed 20 bytes: width, a
"buffer width" field, height, padding, then a GS memory offset, a byte size,
and a file address. The pixel bytes for each sub-image live at
`file_addr + 0x20` — miss that 32-byte sub-header and every pixel shifts,
which looks like a subtly-wrong swizzle rather than an obviously broken
image (this is the single easiest mistake to make and hardest to notice).

Composing this into a normal raster needs the PS2 GS's block-swizzle
addressing — this is public, documented PS2 hardware behavior (search
"PS2 GS PSMT8 swizzle" / "unswizzle8"), not something specific to this game.
Implement it once as a generic function:

```python
def unswizzle8(canvas_bytes, width, height):
    # standard PS2 PSMT8 8x8-block deswizzle; same routine used across
    # dozens of PS2 titles' modding tools — do not hand-roll from scratch,
    # copy a verified reference implementation and unit-test against a
    # texture you can eyeball-check (a title screen or logo, not hair).
    ...
```

Compose each sub-image onto a shared canvas at
`x = (gs_offset//4096 % (buffer_width//2)) * 64`,
`y = (gs_offset//4096 // (buffer_width//2)) * 32`, unswizzle the whole
canvas, and export:

```python
from PIL import Image
Image.frombytes("P", (canvas_w, canvas_h), indexed_bytes).save("kosmos_canvas.png")
```

You'll find part of the canvas decodes to coherent (if palette-less) shapes
and part decodes to pure noise. Count distinct byte values per 16x16 tile —
tiles with ≤256 distinct values are plausible indexed regions; a tile with
close to the full 16×16=256-pixel range of *distinct RGBA values* when
reinterpreted as 32-bit-per-pixel is not an index region at all, it's raw
true-color pixel data (KOS-MOS's long hair-strand sheets are exactly this).
That distinction is why "recolor the hair" isn't one edit — it's two,
in two different pixel formats, in two different places on the canvas.

## Step 7 — Find the palette

The indexed region needs a 256-color CLUT to mean anything. Palettes here are
stored as 16×16 true-color tiles, in CSM1 order (the two middle 8-entry runs
of every 32 are swapped — another PS2-hardware quirk, not a game-specific
one). Three places to look, in order, because different textures use
different sources: (1) a dedicated palette tile inside the same `.xtx`, (2)
the paired 3D model (`.lex`) file's per-material palette pointer — parse its
mesh headers, find a `pal`/`pal2` byte pair, and convert that to canvas
coordinates with the bit-math in [FORMATS.md](FORMATS.md#lex-models), (3) for
menu/UI textures with no model at all, scan unused canvas corners for a
16×16 block that looks like a palette (capped distinct-color count, plausible
alpha). Validate any candidate before trusting it — a wrong-but-plausible
palette pointer renders a technically-valid but blank or garbage image, which
is easy to mistake for "this texture just doesn't have a palette."

## Step 8 — Edit the pixels

Two edits, two tools:

- **Palette edit** (CLUT): open the 16×16 palette tile as a raw 4-bytes-per-
  pixel (RGBA, PS2 alpha is 7-bit — scale by 2 for a normal 0–255 alpha) grid
  in GIMP (`File → Open As Layers`, import as raw 64×64 RGBA if you crop the
  16×16 tile up to pixel size, or just edit the underlying bytes directly
  with a hex editor since it's only ~30–90 entries that are actually
  "hair-blue" — a script that hue-rotates any `(r,g,b)` where `b` dominates
  is faster and more consistent than manual selection).
- **True-color strand sheet**: open the composed canvas PNG in GIMP,
  `Select by Color` on the blue strand pixels (or replicate the same
  programmatic hue-rotate predicate — anything satisfying "blue channel
  clearly dominant over red and green" — so palette and pixel edits use one
  consistent rule instead of two different-looking pinks), rotate hue in
  HLS space keeping lightness fixed so shading/highlights survive, export.

```python
import colorsys
def recolor(r, g, b, hue=0.92):          # 0.92 ~ rose pink
    h, l, s = colorsys.rgb_to_hls(r/255, g/255, b/255)
    nr, ng, nb = colorsys.hls_to_rgb(hue, l, min(1.0, s*1.1))
    return round(nr*255), round(ng*255), round(nb*255)
```

## Step 9 — Re-encode back into the exact original layout

Whatever you changed has to go back in *the same swizzled byte layout it
came from* — same dimensions, same sub-image boundaries, same GS offsets.
Write the inverse of Step 6's unswizzle, re-pack your edited canvas pixels
into each sub-image's original slot, and reassemble the `XTX\0` container
with its original header fields untouched (only pixel bytes changed, so all
size fields stay identical — this matters, because a size mismatch here
would desync every offset after it).

```sh
cmp <(python3 xtx_encode.py kosmos_edited.png --template kosmos.decompressed) \
    kosmos.decompressed
# expect it to differ ONLY in the byte ranges you intentionally changed —
# verify that by also diffing offsets, not just "cmp says different"
```

## Step 10 — Recompress with your own ARX encoder

You need an *encoder* now, matching the scheme you reversed in Step 5:
pick the 30 most frequent u32 words in the payload as your LUT (ties broken
by first occurrence — the tie-break that makes recompressed *untouched*
files byte-identical to retail, which is your regression check: run your
encoder over files you have *not* edited and `cmp` against the original
compressed bytes on disc — any diff there means your heuristic doesn't match
the original packer's, and you should fix that before trusting it on files
you *have* edited), emit the same header layout, and encode literal-vs-LUT
control bits the same way you decoded them.

```sh
python3 arx_encode.py kosmos_edited.raw > kosmos_edited.arx
python3 -c "assert arx_decompress(open('kosmos_edited.arx','rb').read()) == open('kosmos_edited.raw','rb').read()"
```

Also check the size: `len(kosmos_edited.arx)` must be `<=` the original
compressed object's byte allocation on disc (the gap to the next object's
sector — get this from your TOC parser, `next_entry.sector - this_entry.sector`,
times 2048). If your edit made the compressed form *bigger*, you cannot
write it back in place without relocating every object after it — avoid this
by keeping palette/pixel edits value-for-value swaps of existing byte
patterns rather than introducing new ones where possible.

## Step 11 — Patch the disc in place

```sh
cp "Xenosaga Episode I (USA).iso" "Xenosaga Episode I (PINK).iso"

python3 - <<'EOF'
iso_offset = xenosaga00_lba*2048 + entry.sector*2048   # locate inside the ISO
with open("Xenosaga Episode I (PINK).iso", "r+b") as f:
    f.seek(iso_offset)
    f.write(open("kosmos_edited.arx","rb").read())
    # patch the TOC's stored csize (u32 @ fields_off+3) and usize
    # (u24 @ fields_off+7) to the new compressed size — same file, different offset
    f.seek(toc_file_offset_in_iso + entry.fields_off + 3)
    f.write(struct.pack("<I", len(open("kosmos_edited.arx","rb").read())))
EOF
```

Read it back through your own Step-4/5/6 pipeline against the patched ISO
and confirm it decodes to your edited image, not the original.

## Step 12 — Find the other eleven copies

Boot the patched ISO in PCSX2. Field model: pink. Battle screen and the
opening cutscene: still blue. The texture is embedded elsewhere,
independently.

```sh
python3 - <<'EOF'
original = open("kosmos.decompressed","rb").read()
needle = original[0x2000:0x2040]   # a distinctive 64-byte slice of a
                                    # known-unique hair row, not the whole
                                    # file (some carriers only hold parts)
for path in ["chain0.bin", "chain1.bin"]:
    data = open(path,"rb").read()
    idx = 0
    while True:
        idx = data.find(needle, idx)
        if idx == -1: break
        print(path, hex(idx))
        idx += 1
EOF
```

This finds byte-identical embeds inside per-character "battle bundle" files
(`yamamoto\pc\kosmos*.bin` — small archives with their own tiny header:
entry count, total size, then an offset table; the model data starts at a
fixed offset and the texture is findable inside by its own `XTX\0` magic).
Patch those the same way as Step 11, just at a different container's
internal offset instead of the chain TOC's.

It also turns up *partial, non-contiguous* matches inside per-scene bundle
files (`scene\cf0210.a` and siblings) — long stretches of your needle align,
then desync at a regular interval. That's not corruption, it's a different
framing: the same canvas bytes but with small zero-filler words spliced in
at near-regular intervals (a different effective row stride than the
standalone file's). A straight `bytes.replace` only catches the aligned
portion. You need a tolerant matcher: anchor on a short window you're
confident is intact, then walk outward comparing old-vs-new while treating
an inserted zero-word on either side as "skip, don't compare" rather than
"mismatch, stop."

```python
def patch_reframed(data, anchor_at, old_row, new_row):
    # two-pointer walk from the anchor: same value -> replace, one side has
    # a zero word the other doesn't -> skip past it, real mismatch -> stop.
    # recovers most but not all pixels; anything the anchor window itself
    # straddles a splice point is a known, acceptable gap.
    ...
```

Palette entries inside these re-framed bundles can be replaced by exact
4-byte value match instead (safe only if you've checked your new palette
color doesn't happen to equal some *other*, unrelated tile's color anywhere
else on that canvas — check this explicitly, don't assume it).

## Step 13 — Prove you found all of them

```sh
for f in chain0.bin chain1.bin; do
  python3 finer_sweep.py "$f" --granularity 16   # smaller anchor than you
done                                              # patched with
```

Re-run the search at a *finer* grain than whatever you used to patch. If it
still finds unpatched instances, you missed a carrier. Stop only when the
sweep comes back with exactly the set of locations you already patched —
that equality is the actual completeness proof, not "the field model looks
right."

## Step 14 — Verify

```sh
cmp "Xenosaga Episode I (USA).iso" "Xenosaga Episode I (PINK).iso"
# expect differences ONLY at the byte ranges you intentionally touched —
# diff the offset list against your own patch log, not just eyeballing cmp
```

1. Every patched object round-trips: `arx_decompress` of what's now on disc
   equals your edited, re-swizzled bytes.
2. Every neighboring object (the one before/after in sector order) is
   byte-identical to the original disc — proves you didn't clobber an
   adjacent file's allocation.
3. Boot the patched ISO in PCSX2. If a scene still shows old-colored hair,
   open PCSX2's memory viewer, dump EE RAM, and search for both your new
   palette bytes and the original ones — a **partial** match (some entries
   updated, others not) pinpoints exactly which carrier you missed, because
   the engine loads different carriers for different contexts (field vs.
   battle vs. that specific cutscene's bundle).
4. Expect in-RAM palettes to differ slightly from your disc bytes in RGB
   (not alpha) even when everything is correct — the engine tints palettes
   with scene lighting at load time. Don't chase that as a bug.

That's the complete manual path: ISO9660 → two custom bigfile chains → a
hand-reversed binary TOC → a hand-reversed word-dictionary compressor → a
GS-memory texture format mixing indexed and true-color regions on one
canvas → a hand-reversed swizzle → palette resolution via a paired model
file → pixel edits in two different encodings → a hand-built matching
compressor respecting the original packer's size behavior → an in-place
binary patch of a copy of the ISO → an exhaustive byte-level sweep for
duplicate embeds in two more container formats → verification against a
running emulator's live memory. Every one of those format facts is written
up properly, with the evidence, in [FORMATS.md](FORMATS.md) and
[MODDING.md](MODDING.md) — this document is the path to *discovering* them
without those already in hand.
