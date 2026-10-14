# Models in Blender

`browse --kinds models` turns every `.lex` on the disc into a glTF 2.0
binary (`.glb`) under `browse/models/`, mirroring the `dump/` tree:

```
browse/models/chain0/char/pc/kosmos.glb        body + face merged (4,568 tris)
browse/models/chain0/char/pc/kosmos_face.glb   the face on its own (1,278 tris)
browse/models/chain0/char/pc/kosmos_h.glb      alternate outfit
browse/models/chain0/enemy/…  obj/…  map/…  weapon/…  robo/…
```

Each file carries the decoded texture atlas inside it, so a `.glb` is
self-contained: no texture paths to fix up.

## Import

Blender 2.8 or later, no add-on needed:

1. **File ▸ Import ▸ glTF 2.0 (.glb/.gltf)** and pick the file.
2. Leave the importer defaults. "Shading: Normals" uses the game's own
   vertex normals; "Pack images" keeps the atlas inside the `.blend`.
3. Switch the viewport to **Material Preview** (the sphere icon, or `Z`
   ▸ Material Preview) to see the textures.

What you get:

* **One object per game mesh**, named `NN/group/bone`
  (`01/kosmos_mune/kosmos_body`): the mesh index, the artist's group
  name and the bone it is parented to. Face parts merged in from a
  companion file are prefixed with that file's name
  (`37/kosmos_face/kosmos_face/kosmos_face`).
* **Materials** named `matN_u<min>-<max>_v<min>-<max>`, the region of the
  atlas the material draws from (in atlas pixels, 1024×256 for
  character sheets). `_rgba` marks a true-colour material (hair). All
  share one image texture; alpha is a cutout (`MASK`) except hair, which
  blends.
* **Vertex colours** as the `Col` colour attribute (the game's
  per-vertex lighting tint). The importer wires them into the material;
  if a model looks too dark or too tinted, disconnect the Color
  Attribute node from Base Color.
* **Vertex groups** named `j<id>` on every skinned object, with an
  **Armature** called `skeleton` whose bones all sit at the origin. The
  weights are the game's; the bone *positions* are not decoded yet (they
  live in `.jnt`, see below), so the armature is a flat list of named
  joints. You can weight-paint, retarget or build a rig on top of the
  groups; you cannot pose the import as-is.

Scale and axes: the game is Y-up in metres (KOS-MOS stands 1.73 units
tall), the same as glTF, so the importer's default "Y up → Z up"
conversion puts her upright on Blender's floor. No scaling needed.

## Which files to open

| Directory under `browse/models/chain0/` | What it is |
|---|---|
| `char/pc/` | playable characters: `kosmos`, `shion`, `jr`, `momo`, `chaos`, `ziggy`, variants `*1`, `*2`, `*_h` (alternate outfits), `*_face` (face parts, already merged into the body file) |
| `char/npc/` | every NPC |
| `enemy/`, `enemy/boss/` | enemies and bosses |
| `robo/` | A.G.W.S. mechs |
| `weapon/pc/` | weapons (KOS-MOS's blades are `kos_hw*`) |
| `obj/` | field props |
| `map/` | field and battle maps (`MC_BTL*` are battle arenas; large) |

A body `.glb` already includes its companion files (anything in the same
folder whose name starts with the body's stem and has no atlas of its
own: `kosmos_face.lex`, `kosmos_h_face.lex` …). The companions are also
written on their own, so `kosmos_face.glb` exists if you only want the
head.

## What is and isn't decoded

Done: geometry, normals, UVs (scaled by the model's declared texture
size and wrapped into each material's atlas region the way the GS
region-repeat did), vertex colours, three skin-weight
encodings, in-stream material changes, true-colour hair sheets, the
atlas itself. Verified against Lakuwu's xenotool: identical triangle and
weight-group counts on the retail disc.

Not yet:

* **Skeleton pose and hierarchy** (`.jnt`). Joints exist as named vertex
  groups, bones have no positions. Solving this is the next piece of
  work; the `.jnt` holds 0x40-byte blocks with a parent index each.
* **Animation** — not located on the disc yet (the cutscene motion packs
  in chain 1 are FL00 containers; battle/field motion is an open thread).
* Two map meshes use vertex formats `0x30`/`0xA0` and are skipped; the
  `.glb`'s `extras.warnings` lists them.
* **Eyelashes.** Every character has a `*_matu` lash mesh (UV type
  `0x7F`, a 32×32 sheet). Its vertex colours are flat white, so the dark
  lash look is entirely the texture — and that sheet is 4-bit (PSMT4),
  which the texture decoder does not read yet (the same open thread as
  KOS-MOS's visor HUD). The lashes export with the right UVs onto the
  atlas but show whatever the PNG has in that spot; the material is
  named `…_lash-4bpp-undecoded` so it is easy to hide or recolour.
* Blend shapes (weight format 1024 — the face files carry some) are
  ignored.

## Re-exporting a single model from Python

```python
import arx, lex
from browse import decode_xtx, png_bytes

raw = open("out/dump/chain0/char/pc/kosmos.lex", "rb").read()
model = lex.parse_lex(arx.decompress(raw))             # dump files are ARX-packed
face  = lex.parse_lex(arx.decompress(open(".../kosmos_face.lex", "rb").read()))
lex.merge_companion(model, face, "kosmos_face/")

xtx = arx.decompress(open(".../kosmos.xtx", "rb").read())
w, h, rgba, _ = decode_xtx(xtx, [lex_bytes_for_kosmos, lex_bytes_for_face])
open("kosmos.glb", "wb").write(lex.to_glb(model, png_bytes(w, h, rgba), (w, h)))
```

The format itself is written up in [FORMATS.md](FORMATS.md) § LEX models.
