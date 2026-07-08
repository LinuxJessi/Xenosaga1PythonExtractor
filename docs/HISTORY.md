# Historical context — what this disc records about Monolith Soft, 2002

The extraction is more than assets: it's a snapshot of a young studio's
working practices, frozen at mastering time (2002-12-02). This page gives
the context for reading it. Disc-derived facts are verifiable with this
kit; external history is sourced inline, with confidence noted where the
public record is thin.

## The studio

Monolith Soft was founded **October 1, 1999** by ex-Square staff
[Tetsuya Takahashi, Hirohide Sugiura, and Yasuyuki Honne](https://en.wikipedia.org/wiki/Monolith_Soft),
after Takahashi directed *Xenogears* (Square, 1998) and wanted creative
freedom Square wouldn't give the series. **Namco** funded the new studio
as a subsidiary and published its work; *Xenosaga* was conceived as the
spiritual successor to *Xenogears* — Sugiura has said the company was
founded in part
[to make a Xenogears 2 possible](https://www.siliconera.com/monolith-soft-was-founded-to-try-and-make-xenogears-2-happen/).
Nintendo [bought 80% of Monolith Soft from Bandai Namco in 2007](https://www.gamespot.com/articles/nintendo-buys-monolith-soft/1100-6169813/)
and owns it outright today — the same studio now makes *Xenoblade
Chronicles* and supports *Zelda*.

*Xenosaga Episode I: Der Wille zur Macht* was the studio's first game:
**Japan February 28, 2002; North America February 25, 2003** (this disc,
SLUS-20469). Directed by Takahashi, written by Takahashi with
[Soraya Saga](https://en.wikipedia.org/wiki/Xenosaga_Episode_I), produced
by Sugiura, published by Namco.

Episode I was a first game in a deeper sense too, and the disc shows it:
an in-house engine (`xgl*` graphics layer), an experimental Java scripting
system ([JAVA.md](JAVA.md)), staff work folders published straight to
retail, and an unstripped executable. By Episode III (2006) all of this
had been professionalized away — stripped binaries, asset-type
directories, a proprietary cutscene VM. Episode I is the one disc where
the scaffolding shipped.

## The subtitle

*Der Wille zur Macht* — "The Will to Power" — is the title of Nietzsche's
posthumously compiled book. The whole trilogy keeps the scheme: *Episode
II: Jenseits von Gut und Böse* ("Beyond Good and Evil", 2004), *Episode
III: Also sprach Zarathustra* ("Thus Spoke Zarathustra", 2006). The
TOC filler string on this disc spells it
`MONOLITHSOFT Xenosaga Episode.1`.

## The people in the filesystem

The disc's `data\` tree is organized by developer surname
([FINDS.md](FINDS.md) has the folder-by-folder breakdown). Matching those
names against the game's published credits, where the record allows:

| Disc name | Best match in credits | Confidence |
|---|---|---|
| `yajima/` | Toshiaki Yajima, programmer on Episode I | confirmed |
| `F_Kojima` (planner flag table) | **Koh Kojima, Quest Planner** on Episode I — later scenario writer on *Baten Kaitos Origins* and **director of Xenoblade Chronicles** (and XC2/XC3 director/producer). The Xenoblade director's earliest work is addressable as a Java constants class on this disc. | confirmed |
| `F_Sakisako` | Shinji Sakisako, credited on Episodes I & II | confirmed (name; role unverified) |
| `simajiri/` | Masato Shimajiri, credited across the Xenosaga series | likely |
| `tanaka/` | a Tanaka among the ~246 credited staff; *the* famous Tanaka on this game is Kunihiko Tanaka, the character designer, but the casino-art folder may be a different Tanaka | likely |
| `endou/`, `karakama/` | appear alongside "Yajima Test" in the game's leftover debug sound-test menus (per TCRF), consistent with staff self-naming | likely |
| `matumoto/`, `nisimori/`, `yamamoto/`, `F_Fuji`, `F_Konishi`, `F_Nakahara`, `F_Yone`, `F_Koji`, `F_Gash` | not yet matched — common surnames or nicknames; needs a manual pass over the full MobyGames/GameFAQs credits | unverified |

If you can close any of these gaps from the actual credit roll, please
open an issue.

## The music

Composer **Yasunori Mitsuda** (of *Chrono Trigger*/*Chrono Cross*, and
*Xenogears* before this) scored Episode I through his company **Procyon
Studio** — and the retail sequence files literally embed the credit:
`Yasunori Mitsuda / PROCYON STUDIO` is in the `.SMD` metadata this kit
extracts to `smd_catalog.csv`. The orchestral score was recorded with the
**London Philharmonic Orchestra** and the Metro Voices choir; Joanne Hogg
sang the theme songs, continuing from *Xenogears*.

The famous quiet of the game's field maps is real and visible in the
data: only ~10 sequenced tracks on the disc are actual music; the other
~110 are tiny ambience stubs. Mitsuda has described the approach as
film-style —
[every piece written for a specific scene](https://shmuplations.com/yasunorimitsuda/),
agreed with Takahashi, rather than looping field themes. (An explicit
"we chose silence" quote is not on record; the scene-scoring rationale
is.)

## The PS2 HDD ghost

The disc ships the full PS2 expansion-bay driver stack — `DEV9.IRX`,
`ATAD.IRX`, `HDD.IRX`, `PFS.IRX`, and even `SMAP.IRX`, the Ethernet
driver — plus `hdd.res`/`hddi.bin` resources and `TitleHddInstall*`
functions in the title-screen overlay.

Context: in Japan the
[PS2 HDD + Network Adaptor launched July 2001](https://www.psdevwiki.com/ps2/Hard_Drive),
and the BB Unit / PlayStation BB Navigator ecosystem shipped in early
2002 — exactly Episode I's development window. Monolith built (at
minimum) an HDD-install path against that ecosystem. The USA release
kept all of it on disc even though the US HDD wouldn't exist until March
2004 — a fossil of the PlayStation BB future Sony was still promising
when this game was mastered.

## The Java scripting layer is (apparently) undocumented

As far as a determined search can tell, **no interview, conference talk,
or article has ever documented that Xenosaga Episode I's event system is
a Java VM** running JDK 1.1 class files. The public ROM-hacking and
decompilation projects for this game don't mention it either. The
write-up in [JAVA.md](JAVA.md) — derived entirely from the retail disc's
unstripped symbols and the class files themselves — appears to be
original documentation of a genuinely odd piece of 2002 engine history.
If prior coverage exists, we'd love a pointer: open an issue.
