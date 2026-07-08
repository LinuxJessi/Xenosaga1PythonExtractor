# Finds, easter eggs, and what the disc says about Monolith Soft

Everything below is on the retail USA disc (SLUS-20469) and reproducible
from a clean extraction with this kit. Paths are relative to `out/`.

## The disc is organized by *person*, not by asset type

Episode I's build system published each developer's personal working folder
straight to the retail disc. Eight Japanese surnames sit at the top of the
`data\` tree, and together they sketch the team's org chart:

| Folder | Files | What this person owned |
|---|---|---|
| `simajiri/` (Shimajiri) | 1,227 | particle effects — 1,051 `.esd` + 170 `.esp` scripts; the largest folder on the disc |
| `yamamoto/` | 1,211 | battle data — 1,195 `.bin` stat/animation tables (one per playable character and variant), the battle BGM, and a private stash of movies |
| `tanaka/` | 110 | the casino minigames — slot, poker, and card art (`slot_1.xtx`, `poker_1.xtx`, `CASINO.res`, `help_all.xtx`) |
| `matumoto/` (Matsumoto) | 88 | per-zone map parameter tables (`MC_UTA01.dat` … one per map) |
| `nisimori/` (Nishimori) | 42 | battle result/announce UI, `.rbg` battle backgrounds (`town`, `ship`, `aircarrier`) |
| `endou/` (Endou/Endo) | 24 | the menus: save screen, shop, skill tree, ether tree, parameter-up tables |
| `yajima/` | 11 | title screen, ending and game-over art (`gameov.jpg`, `end.jpg`, `gameover.vds`) |
| `karakama/` | 2 | event items (`evtitem.dat`, `itembox.dat`) |

The engine addresses these paths by name — `data\yajima\gameover.vds` is a
string literal in the executable. By Episode III, Monolith had switched to
sober asset-type directories (`bat/`, `ef/`, `evt/`), so this is a
2002-only window into who sat where.

Staff surnames also show up *inside* the data: scene scripts cast NPCs as
`CID_MORIYAMA` and `CID_TOGASHI`, and the event planners each get a Java
constants class named after them. `base.evt`'s `xeno/plan` package holds
the game-wide constant tables (`ChrNo`, `MapNo`, `SceneNo`, `FlagNo`,
`EventConstants`, `PartyData`, …) and, right beside them, `F_Kojima`,
`F_Fuji`, `F_Konishi`, `F_Nakahara`, `F_Sakisako`, `F_Yone`, `F_Koji`, and
`F_Gash` — one event-flag table per planner, so each scripter owned a
namespace of story flags under their own surname. (On disc these are
24-byte stub class files; the engine's `JNI_loadClassDB` supplies the
constant values at runtime.) The scripts even tell you whose flags they're
flipping: a leftover Japanese debug print reads
`*********FUJI_24をセットしたつもり**************` — "(I) intended to set
FUJI_24" — tying the `F_Fuji` class to its `FUJI_##` flag series.
Reference counts rank the planners by workload: `F_Koji` is referenced by
236 classes, `F_Nakahara` 131, `F_Yone` 105, `F_Sakisako` 33, `F_Fuji`
24, `F_Konishi` 6, `F_Kojima` 4, and `F_Gash` 0 (present but never used —
someone got a namespace and no flags).

## The TOC filler is a signature

The unused tail of each binary table-of-contents file is filled with the
string `MONOLITHSOFT Xenosaga Episode.1` repeated end to end, phase-locked
to the file offset. It's simultaneously charming and a perfectly good
end-of-entries sentinel — this kit actually uses it as one.

## Debug tools shipped inside the cutscenes

Because the event scripts are compiled Java (see [JAVA.md](JAVA.md)) with
debug attributes intact, we can see exactly what development scaffolding
shipped in retail scenes:

* **An in-scene camera editor.** Two dozen scenes carry `CameraTest` /
  `CameraTool` harnesses, and nearly every big story scene has the
  `CamHistory` keyframe recorder — a ring buffer of camera poses
  (`CamHistory_set/get/del/clear`, link tables, FOV tracking) that dumps
  recorded moves as spline data, with its banners still in the string
  pool: `*********CameraHistory SPL*********`,
  `*************Focus Printout*************`, and an overflow error
  (`CameraHistory Overflow!!`). The camera staff could record, scrub, and
  export camera moves live inside the scene — and all of it compiled into
  the shipping disc.
* **A render-capture tool.** Four scenes (`SCE01007A1`, `SCE02001A`,
  `SCE02011`, `SCE02041`) contain a complete `CaptureTool`: an interactive
  in-engine menu (`Capture_SelectChr`, `Capture_SelectFolder`) for
  capturing the scene as separated render layers, with color and depth
  passes. Its mode strings spell out the compositing pipeline:
  `CaptureMODE ---Chr(pic+zpic)`, `---MObj(pic+zpic)`,
  `---Particle(pic+zpic)`, `---SoftImage BG(pic+zpic)`, `---All Screen.` —
  note **SoftImage**, naming the 3D package Monolith's artists rendered
  backgrounds from. Banners still fire at runtime:
  `*********CaptureTool Standby*****************`,
  `*********Capture Start!!!*****************`.
* **Debug pad hooks.** Scene controllers read `Xpad1P` input with
  `PADL3`/`PADR3` fields and menu-select state, remnants of on-screen debug
  menus (`menuSelected`, `selectMenu` fields in almost every scene).
* **Event flow breadcrumbs.** Every scene logs `>>>>>>>>>> CUT /[$1]` and
  `Event Out`, and carries its flag/jump wiring as plain strings:
  `XEVEFLAG:EV01005_F`, `XEVEJNAME:SCE01006` — the whole story graph is
  greppable.

## The story graph is made of copy-pasted dummy scenes

Not every "cutscene" is a cutscene: 25 bytecode-distinct copies of a
template class called `SCEDummy` fill gaps in the event chain. Each one
is identical except for two strings — the flag it sets and where to jump
next (`XEVEFLAG:EV01005_F` → `XEVEJNAME:SCE01006`) — so the entire story
flow is greppable as plain text across the class files. Its sibling
`JumpBattle` exists in 17 copies (one per `ST90##` battle-jump map).
Someone recompiled the same `.java` template dozens of times with
different constants rather than parameterize it — very 2002, very
relatable.

## Strings worth grepping for

A few favorites from the ~2,200 class files' constant pools (scene in
parentheses):

* `"Damn slacker!!"` (SCE01010) and `"Both of ya morons, shut up!"`
  (SCE02029) — localized dialogue sitting in Java constant pools like
  it's the most normal thing in the world.
* `"...Duh! I forgot to ask her out after work..."` (SCE01012 — Allen
  being Allen, in bytecode).
* `"KickEvent_case0!!!!!!!!!!!!!!"` (ST0231) — a debug print with
  fourteen exclamation marks.
* `"CameraHistory Overflow!!"` — the camera editor running out of
  keyframe slots.
* `*********シュミレーターで勝ってきました**************` /
  `…負けてきました…` — "came back from the simulator having won/lost",
  battle-sim outcome hooks (and note the common シュミレーター
  misspelling of シミュレーター).
* `*********全回復しました**************` — "fully healed" (15 scenes).
* `2Fの壊れ物を壊しました。次回は生成しません。` — "broke the 2F
  breakable; won't respawn next time." The persistence model, narrating
  itself.
* `/[label(Temp Person)]` — a dialogue speaker named "Temp Person" in 13
  scenes' string pools.

These are EUC-JP bytes inside the class files (a spec violation that
breaks naive tools — see [JAVA.md](JAVA.md)); the kit's class map decodes
them correctly.

## Placeholder theater: the MC68000 conversation

`umn/event00.txt` is a placeholder U.M.N. event where the cast recite
**Motorola CPU part numbers** at each other — SHION and UKUN solemnly
exchanging `MC68000`, `MC68010`, `MC68020`. Programmer lorem ipsum,
shipped. (The `MC_*` map prefix suggests the same processor-family joke
naming runs through the map codebase too.)

## Gamera in the battle programmer's folder

`yamamoto/mv/` — the battle-data programmer's private movie stash —
contains `gamera.pss`, named after the giant flying kaiju turtle, next to
`kiss.pss`, `momo_a.pss`, `momo_b.pss`, and `gr.pss`. Convert them with
`browse --kinds movies` and see what a Monolith battle programmer kept on
hand in 2002.

## The music that credits itself

The sequenced BGM files embed their own metadata, and the retail files
literally carry the credit line `Yasunori Mitsuda / PROCYON STUDIO` in
ASCII. The catalog (`browse/soundbanks/smd_catalog.csv`) has its own
charm:

* `BATTLE1.SMD`'s note field reads **"IBENT BATTLE"** — a romaji typo of
  "EVENT BATTLE" (`BATTLE2.SMD` spells it correctly).
* `BATTLE1` and `BATTLE2` are both titled "Battle1"; both jingles are
  titled "Jingle2". Nobody updated the metadata.
* Only ~10 sequences are real music (Battle1 ×2, LastBattle, Escape! ×3,
  U.M.N. Mode ×2, Jingle2 ×2). The other ~110 `ENV_*` entries are tiny
  ambience stubs — the data-side confirmation of the game's famous
  near-silent field maps.

## Dev comments still in the shipping text

The decoded scene text (`browse/text/chain0/scene/`) keeps the writers' and
scripters' working notes inline, in Japanese:

* `cf0110.txt`: `;;;誰も歩いてない` ("nobody's walking around"),
  `andrew_cfの代わり（仮）` ("stand-in for andrew_cf (temporary)"),
  `モーションデータのロード。ファイル名で指定。` ("load motion data,
  specify by filename").
* `umn/db_fileno.txt` is a hand-commented index (`// A`, `// B`, …) mapping
  the U.M.N. database file numbers — a programmer's cheat sheet shipped as
  data.

## Leftover test and dummy assets

* `matumoto/MC_TEST.dat` — a test map parameter file; its sibling is
  unfortunately-but-genuinely named `testes.dat`.
* Twelve `dummy.*` placeholder assets across `char/`, `obj/`, `enemy/`
  (`dummy.lex`/`.xtx`/`.jnt`, `dummy_face.*`) plus `sound/sed/DUMMY_C.SED` —
  the engine's stand-in model/sound set.
* `motion/oldm_cf.fpk`, `motion/oldw_cf.fpk` — superseded "old" motion
  packs nobody deleted.
* The audio pipeline's own rate-comparison set survives as
  `browse/audio/_RATE_TEST/` after a browse run — the kit reproduces the
  comparison used to pin the 48 kHz stream rate.

## The PS2 HDD support nobody used

A 2003 USA release ships the full PS2 hard-disk and **network adapter**
IOP driver stack: `DEV9.IRX`, `HDD.IRX`, `PFS.IRX`, `ATAD.IRX`, and
`SMAP.IRX` (the Ethernet driver), plus `hdd.res`/`hddi.bin` resources and
`TitleHddInstall*` functions in the OV02 overlay. This is PlayStation BB
plumbing — the Japanese HDD ecosystem — kept alive in a region where the
HDD wouldn't launch until 2004 and the install feature was never surfaced.
See [HISTORY.md](HISTORY.md) for context.

## An unstripped retail executable

Not an easter egg, but the biggest gift on the disc: `SLUS_204.69` and all
five overlays shipped with **full ELF symbol tables** — 8,100+ named
functions (`xgl*` graphics layer, `Java_xeno_*` script bridge, `Poker*` in
the casino overlay, `TitleHddInstall*`…). Episode III shipped stripped;
Episode I forgot. Combined with the Java layer's `SourceFile`/
`LineNumberTable` attributes, this is one of the most self-documenting
commercial PS2 discs known.

Build timestamp, for the record: `Build:Nov 24 2002 11:02:50` (in
OV10.OVL), mastered 2002-12-02, released in NA February 2003.
