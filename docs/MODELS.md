# Models: `.lex` meshes and `.jnt` skeletons

What the kit knows about Episode I's models, how each part was proven, how
they become `.glb` files, and what is still open. The Episode II and III
kits have a page of the same name for their own formats (`.pxy` / `.xhr`);
Episode I's are different files with the same job. To *use* the models, read
[BLENDER.md](BLENDER.md); the byte layouts are in [FORMATS.md](FORMATS.md)
under "LEX models" and "JNT skeletons". Code: `lex.py` (parser, skeleton,
glTF writer) and the `models` kind in `browse.py`.

## Getting them

```sh
python cli.py extract --iso GAME.iso --out out/
python cli.py browse  --out out/ --kinds models
```

writes one `.glb` per model under `out/browse/models/`, in the game's own
folders (`chain0/char/pc/kosmos.glb`). There is no single-model command, as
the other two kits have; `BLENDER.md` ends with the six lines of Python that
convert one.

## What is on the disc

Counted on the USA disc (SLUS_204.69) on 2026-11-21:

| | Count |
|---|---|
| `.lex` model files | 1,317 |
| `.jnt` skeleton files | 760 |
| `.xtx` texture files | 1,269 |
| models written as `.glb` | MODELS_GLB |
| of those, textured | MODELS_TEXTURED |
| with a skeleton from a `.jnt` | MODELS_SKEL |
| companion models merged into their body | MODELS_COMPANIONS |
| triangles, all models | MODELS_TRIS |
| files the parser could not read | MODELS_UNPARSED |

A **companion** is a model with no texture file of its own that shares a
body's texture: `kosmos_face.lex` beside `kosmos.lex`. `browse` merges it
into the body's `.glb` and also writes it on its own.

## The pieces

### Mesh (`.lex`)

A header (name, artist, bounding box, a table of matrix pairs), then one
block per mesh. A mesh's vertex data is a PS2 VU1 display list: VIF
commands that unpack vertex batches and run the micro-program once per
triangle strip.

* **Positions are already in model space**, in metres, Y up. KOS-MOS is
  1.73 units tall. The matrix table is not needed to assemble the mesh.
* **Two vertex formats** are decoded: floats with normals (0x80) and floats
  without normals with byte colours (0x10). Formats 0x30 and 0xA0 are not
  (two map files).
* **A strip** of `count` vertices is `count - 2` triangles, the winding
  flipped on every odd one.
* **Materials can change in mid-stream.** A batch whose header carries tag
  0x40 is not vertices but a material block for every later strip of that
  mesh. Cherenkov's jacket draws its sleeves from a second palette that only
  such a block names, so a reader that takes materials from the mesh headers
  alone colours them wrong.
* **Batches that do not start at VU address 0** are constant uploads, not
  vertices, and are skipped.

**Proof.** Triangle and weight-group counts were checked byte for byte
against Lakuwu's xenotool on the retail disc, which is where the structure
was learned.

### Texture coordinates and the texture

UVs are GS texture coordinates: a texel is `(s * 2^TW, t * 2^TH)`, with the
texture size taken from the `TEX0` register writes inside the model's own
display list (1024 x 256 for a character sheet), **not** from the size of
the decoded picture, which can be narrower. Each *triangle* is then wrapped
by whole periods into its material's rectangle; doing it per vertex tears
the triangles that straddle a seam.

A material names its palette by a byte pair that maps to a 16 x 16 tile on
the texture's canvas; palette byte 0xFF means true colour (hair sheets),
whose rectangle is given in canvas coordinates. Which palette each region of
a texture uses is therefore known only from the models that draw it, and
that is why the texture decoder takes the `.lex` files as input
([FORMATS.md](FORMATS.md), "XTX textures").

### Skin

Three weight formats are decoded: four bone references with four float
weights, four references at weight 1.0, and two references with two
weights. A reference is the VU address of a bone-matrix slot, which the
mesh's bone table turns into a weight-group id. The ids are shared with the
skeleton. Formats 515 and 517 are not decoded; format 1024 is shape-key
data and is ignored.

### Skeleton (`.jnt`)

64-byte blocks in a table: offset nodes, **rotation joints**, translate
nodes, bone ends and name records, each with a parent index, a local
position in metres and a local rotation. The *k*-th weight-group id of a
model is the *k*-th rotation joint; that pairing was checked on eight
models (46 of 46 joints on KOS-MOS).

The rotation is stored as three Euler angles in the order **Z, Y, X**:
`R = Rz(a) · Ry(b) · Rx(c)`. Read as X, Y, Z it gives a pose that looks
almost right, with the legs and spine in place and the hair raised. The
order was settled against the running game: the engine's own per-node
matrix table was read out of PCSX2's memory and every reading of the three
angles compared with it. Z, Y, X is exact (0.0000 m over 120 nodes); every
other reading is off by 2 cm to 1.5 m. With it the joints sit on the
vertices they move on every model tried, from the files alone; no capture
is needed to convert a model.

The `.glb` gets an armature with the real hierarchy in the game's rest
pose, the skin weights as vertex groups, and the face file's own small
skeleton for eyes and mouth.

## What is not done

* **Motion.** The clips are located and not decoded, so the models do not
  move. `chain0/motion/` holds 176 `.fpk` files; the 55 named `*_cf.fpk`
  are `FPK` packs of `FCV` clips (`kosmos_cf.fpk` has 31 `FCV` headers;
  looked at on the USA disc, 2026-11-21). A clip is a frame count, the
  skeleton's name, then one record per skeleton block in `.jnt` block order
  with nine channels (position, rotation and scale, three each). A constant
  channel stores one float; a keyed channel stores packed keys whose format
  has not been read. The Episode II and III kits export motion as glTF
  animations; their format is not this one.
* **Blend shapes** (weight format 1024) are ignored.
* **Two vertex formats and two weight formats** are skipped, as above;
  whatever is skipped is listed in the `.glb`'s `extras`.
* **No importer.** An edited mesh cannot be written back; texture recolours
  are the route ([MODDING.md](MODDING.md)).
* **Not compared with the running game** beyond the skeleton check above:
  the colour scale of the vertex colours, the alpha modes and the mirror
  sense of the models are as the files give them.
