# The Java event layer — Xenosaga Episode I's cutscenes are class files

The single strangest and best-documented fact about this disc: **every
cutscene and field-event script in Xenosaga Episode I is compiled Java** —
real `0xCAFEBABE` class files, format version 45.3 (JDK 1.1), executed by a
JVM interpreter embedded in the PS2 engine. In 2002. On a machine with no
JIT, 32 MB of RAM, and no business running Java at all.

This document explains what's on the disc, how the engine runs it, and how
to read and decompile it yourself. Everything here was verified against the
retail USA disc (SLUS-20469).

## The evidence, in layers

### 1. The engine embeds a JVM

`SLUS_204.69` ships **unstripped**, and its symbol table gives the game away:

* **402 `Java_xeno_*` native methods** — the C side of a JNI-style bridge:
  `Java_xeno_Camera_setFov__F`, `Java_xeno_Movie_start__I`,
  `Java_xeno_vm_Math_atan2__FF`, `Java_xeno_Sound_streamPlay…`
* The interpreter machinery by name: `JNI_loadClassLibrary`,
  `JNI_loadClassDB`, `JNI_searchClasses`, `Call_JavaMethod`,
  `Const2JavaString`, `ATTR_InnerClasses`.

The native-method name mangling is a **custom simplified scheme**, not
standard JNI: after the `__`, `I`=int, `F`=float, `Z`=boolean, `C`=char,
`B`=byte, and `a` = array-of-next-type (`fovSPL__aFI` = `(float[], int)`);
return types aren't encoded. Census by class: `util_Runtime` 65, `Chr` 65,
`Unit` 48, `Camera` 36, `Effect` 16, `Stage` 14, `util_Window` 10,
`PlayControl` 8, `Sound` 7, plus `vm_Math` / `vm_Thread` / `vm_System`.

### 2. The `.evt` files are class-file containers

Every `.evt` on the disc (`chain0/base.evt`, `chain0/system.evt`, 386
`chain0/scene/ST####.evt` field scripts, 92 `chain1/scene/SCE#####.evt`
cutscene scripts) is an `FL00` container of Java class files:

```
0x00  "FL00"
0x04  u16 ?          u16 ?  (version / dir count?)
0x08  u32 file size
0x0c  u32 offset of directory-name string     ("xeno/plan" in base.evt)
0x10  u16 dir-name length
0x12  u16 entry count
0x14  entry table: (u32 name_off, u32 name_len, u32 data_off, u32 data_size) each
...   class payloads (each starts CA FE BA BE, major version 45)
tail  NUL-separated name string table
```

Three traps for the unwary (all handled by `evt.py` / `evt_unpack.py`):

* **Untabled classes.** The entry table doesn't describe everything —
  `system.evt` tables only its `java/lang` classes while 24 more (the whole
  `xeno.util`/`xeno.vm` runtime) sit untabled after the tabled payloads. The
  reliable approach is to walk the class-file structure itself (constant
  pool → fields → methods → attributes) at every `CAFEBABE`, which yields
  exact byte ranges and true fully-qualified names.
* **24-byte stubs.** 349 "classes" across the disc (`ChrNo`, `MapNo`, …) are
  24-byte constant-holder stubs — a magic and a name, body resolved by the
  VM elsewhere (see `JNI_loadClassDB`).
* **Post-javac NUL padding.** Every `CONSTANT_Utf8` constant-pool entry has
  a stray trailing `0x00` appended after its declared length — a console
  C-string convenience added by Monolith's packaging step. Modern
  `javap`/ASM choke on it until stripped (`evt_unpack.py --normalize`).
  Relatedly, Japanese string literals are raw **EUC-JP** bytes inside those
  Utf8 entries, not the modified UTF-8 the JVM spec requires.

### 3. It ships its own Java runtime

`system.evt` contains `java/lang/Object`, `java/lang/String`, and
`java/lang/StringBuffer` — the game carries the bottom of the Java class
library with it. `base.evt` holds the `xeno.*` engine peer classes
(`Camera`, `Chr`, `Unit`, `Stage`, `Sound`, `Movie`, `PlayControl`, …)
whose `native` methods land on those 402 SLUS symbols. The on-disc classes
declare 401 native methods against 402 native symbols in the ELF — one
orphan, still unidentified.

### 4. It was compiled with a stock Sun javac

Across all 480 containers the only class-file attributes present are
`Code`, `ConstantValue`, `InnerClasses`, `LineNumberTable`, `SourceFile`,
and `Synthetic` — and notably **zero `StackMap`**, so these were never run
through a J2ME/CLDC preverifier. Format 45.3 + retained debug attributes +
that attribute set = a stock **Sun javac of the JDK 1.1.x line** with
default flags. `SourceFile` names the original sources (`Camera.java`,
`Chr.java`, per-scene `ST0010.java`, …) and `LineNumberTable` survives, so
decompiled output can carry original line numbers.

## What the scripts look like

~2,200 unique classes across 480 containers, organized like a game, not
like an app:

| Superclass | Classes | Role |
|---|---|---|
| `Chr` | 565 | character actor |
| `Unit` | 423 | generic map unit/object |
| `Enepc` | 398 | enemy/NPC controller |
| `Stage` | 386 | field/scene logic |
| `MAPUnit` | 285 | placed map object |
| `Scene` | 92 | top-level cutscene controller |
| `Camera` | 26 | camera script |

Naming scheme:

* `ST####` — field/dungeon map scripts, numbered by map ID (386 of them:
  129 in the 0xxx range, 114 in 1xxx, 102 in 2xxx, 19 in 3xxx, and 22
  `ST90##` battle-jump maps).
* `SCE#####` — story cutscenes, numbered by chapter: 44 `SCE01###`
  scripts and 48 `SCE02###`, with letter suffixes for branch variants
  (`SCE01003B1`, `SCE02004A/B/C/D`). Placeholder story beats are 25
  copies of a `SCEDummy` template that differ only in two strings — the
  flag to set and the scene to jump to (`XEVEFLAG:EV01005_F`,
  `XEVEJNAME:SCE01006`) — so the story graph is recoverable by grep.
* Inner classes per scene: `$Camerawork` (one method per camera cut:
  `cut1`, `cut2`, …), `$Lightwork` (lighting splines), per-character actor
  classes (`$shion`, `$allen`, `$kosmos` with `act1`…`actN` beat methods),
  `$Monitor`/`$Door`/`$Obj` prop controllers.
* The top-level `Scene` subclass is the master controller and carries one
  field per actor/object/flag used anywhere in the scene — the record is
  `ST2110` with 499 fields, and the big cutscene masters run 300+ fields
  and 160+ methods.

The scripts use real Java semantics against a compact engine API:

* `xeno.vm.Thread` (`create`/`start`/`stop`) for concurrent cutscene
  action — every scene runs a main thread and a play thread;
  `xeno.vm.System` adds `sleep`/`waitFor`/`waitSignal` synchronization.
* `xeno.util.Spline` for interpolated motion — scenes are full of
  `posSPL`/`rotSPL`/`colSPL` fields driving characters, cameras, and even
  light colors along splines.
* `xeno.Camera` with a film-crew API: `transSPL`/`rotateSPL`/`fovSPL`
  spline moves plus a `setCF*` follow-camera family
  (`setCFPedestalToPlayer`, `setCFAnglePers`, …).
* `xeno.Chr` for actors: `move`, `talkto`, `look_eye_control`,
  `setMotion*`, `setHand`, `hairStop` (yes, a dedicated call to stop hair
  physics).
* `xeno.util.Window`/`Menu` for dialogue boxes and choices — the actual
  dialogue is string literals in the constant pool (English in the USA
  release, with EUC-JP Japanese surviving in debug prints).

And they left the dev tools in: many scenes still contain a full
**in-scene camera editor** (`CameraTest`, `CamHistory_set/get/del`, spline
printouts) and a **capture tool** (`CaptureTool`, `Capture_SelectChr`,
`Capture_SelectFolder`) — debug scaffolding compiled into the retail
cutscene scripts. See [FINDS.md](FINDS.md).

## Working with the classes

### Lift them out

```sh
# one-command version: writes out/browse/classes/ + classes_manifest.csv
python cli.py classes --iso "Xenosaga Episode I (USA).iso" --out out/

# research version: works from an extracted dump/, writes both raw and
# normalized (NUL-stripped, JVM-loadable) trees + anomaly log
python evt_unpack.py --dump out/dump --out out/java
```

### Read them

```sh
# disassemble (any modern JDK still accepts format 45)
javap -c -p out/java/classes_norm/chain0_scene_ST0010/ST0010.class

# decompile to .java — CFR handles the 1.1-era bytecode cleanly
java -jar cfr.jar out/java/classes_norm/chain0_scene_ST0010 --outputdir out/java/src
```

Use the normalized tree (or `cli.py classes` output, which normalizes as it
dedups) for tooling; the raw tree preserves the on-disc bytes for
byte-matching work.

Tip: you don't need a decompiler at all for a lot of questions. The
constant pools are plain searchable text — dialogue lines, `.fpk`/`.vds`
file references, flag names (`XEVEFLAG:EV01005_F`), and engine call
parameters (the 48000 passed to `xeno.Sound.streamPlay`) all sit in there
as strings. `grep -r` over the class tree answers "which scene says this
line" in seconds.

### Map them

`class_map.py` parses every class (constant pool, fields, methods,
attributes, string literals) into a machine-readable JSON —
`python class_map.py <classdir> <out.json>` — which is what the per-scene
class index was generated from.

## Why Java? (informed speculation)

Nothing about this architecture is documented publicly — no interview or
GDC talk we could find acknowledges it (if you find one, please open an
issue). But the design logic is legible from the artifacts:

* **It's a proven authoring model.** A scene = a class; actors = inner
  classes; cuts = methods; concurrency = threads. Monolith's scripters got
  a real language with a real compiler catching type errors at build time,
  in 2001–2002, when most studios were writing ad-hoc bytecode VMs with no
  tooling at all.
* **javac was free and battle-tested**, and JDK 1.1 class files are simple
  enough to interpret on an EE core without drama. The engine only needed
  the interpreter + 402 native bindings, not a full JRE — and the subset of
  `java/lang` they needed, they just compiled and shipped.
* **It didn't survive.** By Episode II/III the event system moved to a
  proprietary opcode VM (`.xep`/`.xev`). The Java experiment is unique to
  Episode I, which makes this disc a one-off preservation target: a
  commercial PS2 JRPG whose entire narrative logic decompiles back to
  readable, line-numbered Java.

## Related reading

* [FORMATS.md](FORMATS.md) — FL00 byte layout and the other container
  formats.
* [FINDS.md](FINDS.md) — the debug tools, dev folders, and easter eggs the
  class files revealed.
* [BROWSING.md](BROWSING.md) — where everything lands on disk.
