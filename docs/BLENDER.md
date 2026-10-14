# The models in Blender

Run `browse` with the **Models** box ticked (or `--kinds models` on the
command line) and every `.lex` model on the disc becomes a `.glb` file
under `browse/models/`, in the same folders the game uses:

```
browse/models/chain0/char/pc/kosmos.glb        KOS-MOS, face included
browse/models/chain0/char/pc/kosmos_face.glb   just her face
browse/models/chain0/char/pc/kosmos_h.glb      her alternate outfit
browse/models/chain0/char/npc/andrew.glb       Cherenkov
browse/models/chain0/enemy/… robo/… weapon/… obj/… map/…
```

Each file carries its texture inside it, so there are no paths to fix
up. Drop one into Blender and it just works.

## Importing

1. **File ▸ Import ▸ glTF 2.0** and pick the `.glb`.
2. Leave the importer settings alone.
3. Press `Z` and choose **Material Preview** so you see the textures.

Blender 2.8 or newer, no add-ons needed.

## What you will see

**Objects.** One per game mesh, named `number/group/bone`, for example
`01/kosmos_mune/kosmos_body`. The group is the artist's name for the
part; the bone is what it hangs off. Parts that came from a separate
face file are prefixed with that file's name.

**Materials.** Named after the region of the texture they use, like
`mat3_u256-320_v128-256`. They all share one image. Hair materials end
in `_rgba` and blend; everything else uses a cut-out alpha.

**Vertex colours** in the `Col` attribute. That is the game's baked-in
shading tint. If a model looks too dark, unplug the Color Attribute node
from Base Color in the material.

**An armature** named after the character, built from the `.jnt`
skeleton file: the real joint hierarchy in the game's pose, plus the
face file's own small skeleton for eyes and mouth. Every bone sits on
the mesh, arms and hair included; the skin weights are the game's own,
as vertex groups named `j<number>` for the rotation joints, with the
offset, translate and bone-end nodes in between. You can pose the
character straight away.

**Bone display.** Blender's importer gives every bone an icosphere
shape, so a 121-bone character looks covered in spiky balls. Select the
armature, Properties ▸ Armature ▸ Viewport Display ▸ Display As: Stick
(and tick In Front), or in the importer options set Bone Shape to
"Blender" before importing.

**Scale.** The game works in metres, Y up, same as glTF. KOS-MOS stands
1.73 m tall and lands upright on Blender's floor with no scaling.

## Which files to open

| Folder under `browse/models/chain0/` | What is in it |
|---|---|
| `char/pc/` | the party: `kosmos`, `shion`, `jr`, `momo`, `chaos`, `ziggy`; `*1`, `*2`, `*_h` are alternate outfits; `*_face` are the face parts (already merged into the body files) |
| `char/npc/` | every NPC; Cherenkov is `andrew`, Albedo is `albelt` |
| `enemy/`, `enemy/boss/` | enemies and bosses |
| `robo/` | the A.G.W.S. mechs |
| `weapon/pc/` | weapons |
| `obj/` | props |
| `map/` | field and battle maps (`MC_BTL*` are the battle arenas; large) |

A body file already includes its companions: anything in the same
folder whose name starts with the body's name and has no texture of its
own (`kosmos_face.lex`, `kosmos_h_face.lex`). The companions are also
written on their own, so `kosmos_face.glb` is there if you only want
the head.

## Known gaps

* **No eyelashes.** Every character has a lash mesh, but its texture is
  not in the character's own atlas: the rect it points at is only the
  video-memory slot a shared 4-bit lash sheet is uploaded into at
  runtime, and that sheet has not been found on the disc yet. The
  lashes are in the file with correct UVs; their material is named
  `…_lash-4bpp-undecoded` so it is easy to hide.
* **No animation.** Not located on the disc yet.
* Two map meshes use vertex formats the parser skips, and blend shapes
  are ignored. Anything skipped is listed in the `.glb`'s `extras`.

## From Python

```python
import arx, lex
from browse import decode_xtx, png_bytes

body_lex = arx.decompress(open("out/dump/chain0/char/pc/kosmos.lex", "rb").read())
face_lex = arx.decompress(open("out/dump/chain0/char/pc/kosmos_face.lex", "rb").read())
body = lex.parse_lex(body_lex)
lex.merge_companion(body, lex.parse_lex(face_lex), "kosmos_face/")

xtx = arx.decompress(open("out/dump/chain0/char/pc/kosmos.xtx", "rb").read())
w, h, rgba, _ = decode_xtx(xtx, [body_lex, face_lex])
open("kosmos.glb", "wb").write(lex.to_glb(body, png_bytes(w, h, rgba), (w, h)))
```

The file format itself is written up in [FORMATS.md](FORMATS.md) under
"LEX models".
