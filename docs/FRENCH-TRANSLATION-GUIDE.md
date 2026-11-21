# Doing a French translation by hand with the repack kit

> **Easier now: the translation editor.** The app's **Translation
> editor** page lists every string of the game (dialogue of any length,
> U.M.N., menus, items, battle words, fixed tables), encodes French
> accents for you and builds the patched disc in one click. Start there:
> [TRANSLATION-EDITOR.md](TRANSLATION-EDITOR.md). This guide stays as the
> by-hand route through the text trees; parts of it predate the editor,
> and the notes below say where.

A step-by-step for a human translator doing the actual French wording
themselves — no machine translation, no AI — using the kit's existing
`textpack.py` pipeline (GUI cards 8/9, or `cli.py text-export`/`text-import`).
This documents what the kit genuinely supports today and where its honest
limits are; don't oversell the second half to yourself mid-project.

This guide covers translating **existing text objects** on the disc. FMV
cutscenes are a different problem — they carry no subtitle track to
translate at all, in any language — see
[SUBTITLES.md](SUBTITLES.md) for authoring and burning in new ones.

## What this pipeline covers, and what it doesn't

Two different systems render text in this game, and only one of them has a
finished translate-and-reimport pipeline:

- **U.M.N. Connection Gear chats and mails** (`umn/event*.txt`, `*.uml`) —
  plain EUC-JP text objects (the encoding the engine's font indexing
  expects; earlier versions of this guide said Shift-JIS). `textpack.py`
  covers these: export, translate, re-encode, patch, verified by
  round-trip. Two gaps the editor closes: `text-import` does not update the
  mail-folder and database lists (`header.lst`, `dbheader.lst`), so a
  translated subject or title stays English there, and a database page's
  body sits after a byte the text tree stops at.
- **Scene/cutscene dialogue** — compiled into the `.evt` files as Java
  constant-pool strings, not plain text. The parallel `scene/cf*.txt` files
  that *look* like dialogue are planner/dev-comment sources the renderer
  never reads — translating them changes nothing on screen, which is an
  easy trap to fall into. A **same-byte-length** swap into the actual
  constant pool works today (worked example below); a length-changing
  rewrite needs a class-file/FL00 rebuilder. *Update:* that rebuilder now
  exists: `evt-export` / `evt-import` (and the translation editor) rewrite
  the scripts around dialogue of any length, so full story-dialogue
  translation is possible.

## Step 0 — a font/encoding sanity check, before translating anything

*Update:* this question is answered. The text is EUC-JP, and both game
fonts carry an accent block with é è ê ë à â ç ô û ù ï î œ « » ¿ ¡ ñ and
the rest. Plain EUC-JP encoding does not reach those cells, which is why
typing é into a text tree went wrong; the editor's codec (`txcodec.py`)
writes the font's own codes, handles the two font faces (they swap grave
and acute on E, I, O, U), and redraws the plain-E capitals È É Ê Ë in
font0 when a translation uses them. With the editor, skip this step.
The original advice for the by-hand route follows.

(Written when the text was thought to be Shift-JIS, `cp932`.) French needs accented Latin letters
(é, è, à, ç, ê, œ…) that are outside the plain ASCII range `cp932` is mostly
used for here, and whether the game's own font even has glyphs for them is
**unverified** — nobody has checked. Before sinking hours into wording:

1. Pick one short, low-stakes line (a menu label, a single mail).
2. Translate it into French *with* one accented character.
3. Run it through the export/import/boot cycle below.
4. Look at the actual rendered screen in PCSX2.

Three outcomes: it renders correctly (great, proceed normally); it renders
as a *different*, wrong glyph (the byte you wrote decodes to some kanji
under `cp932` — pick a different word or accept the substitution); or
`text-import` refuses the file outright with a "not representable in
Shift-JIS" error (that character has no `cp932` code point at all — you'll
need to reword around it or fall back to the unaccented letter). Decide
your project's house style now (strict accents with per-case fallback, or
accent-free French throughout) rather than discovering the answer three
hundred files in.

## Step 1 — export the text tree

GUI: card 7, **"Export text for translation."** Fill in the retail ISO and
an output directory, click **Export text**.

Terminal equivalent:

```sh
python cli.py text-export --iso "Xenosaga Episode I (USA).iso" --out french/
```

You get:

```
french/
  chain0/umn/event0001.txt.utf8.txt      # U.M.N. chat scripts
  chain0/scene/ST0210.txt.utf8.txt       # planner text — NOT rendered, skip
  chain1/.../m0001.uml.utf8.txt          # mail bodies
  ...
  textpack_manifest.csv                  # one row per file: byte budget, kind
```

Every `.txt`/`.uml` slot on the disc becomes a `.utf8.txt` file (or `.raw`
if its bytes don't round-trip cleanly through the encoding — leave those
alone). `textpack_manifest.csv` records each file's `budget_bytes` — this
is the hard ceiling your French text must fit inside, in *encoded bytes*,
not characters.

## Step 2 — translate

Open each `.utf8.txt` in any editor (any OS — the round-trip preserves
CRLF/LF and tolerates a BOM). Rules that keep the import step happy:

- **Leave control-code markup exactly as found** — sequences like `\15\2`
  are engine formatting codes (speaker color, pauses), not prose; translate
  around them, never inside them.
- **Leave `⟦XX⟧` markers untouched.** These are hex-escaped bytes the
  decoder couldn't turn into a character on export (a handful of
  mails embed raw JIS symbol codes like ★/●/○). They round-trip back to
  their original byte on import — deleting or "translating" one corrupts
  the file.
- **Watch length, not just for `.uml`.** `.txt` objects may grow up to
  their sector allocation (`budget_bytes` in the manifest — often has real
  slack, since Japanese kana/kanji cost 2 bytes each and French is mostly
  1-byte-per-character, so you often have *more* room than the source
  text used). `.uml` mail bodies are the opposite: the text region is a
  **fixed** slot with zero slack — French is often more verbose than the
  Japanese it replaces, so short mails are the ones most likely to blow
  their budget. Reword tighter rather than truncate silently.

Skip `scene/*.txt` planner files for actual dialogue work — see the section
above. They're worth reading for context/dev comments, just not for
in-game text.

## Step 3 — import (validated, all-or-nothing)

GUI: card 8, **"Import translated text."** Fields: retail ISO, the edited
tree from Step 1, and an output ISO path. Click **Import text**.

```sh
python cli.py text-import --iso "Xenosaga Episode I (USA).iso" \
    --text french/ --out "Xenosaga Episode I (FR).iso"
```

`text-import` re-encodes every changed file and checks it
against its budget **before writing anything** — if even one file is over
budget or contains an un-encodable character, you get a full list of every
failure (file, byte overage or bad character position) and *nothing is
written*. Fix the flagged files and re-run; there's no partial/corrupt
output state to worry about. Only files that actually changed from the
original are patched into the copy.

## Step 4 — verify

1. Read the failure list (if any) literally — `"m0142.uml.utf8.txt: text is
   212 bytes, slot holds 198 (over by 14)"` tells you exactly which mail and
   by how much to cut.
2. Boot `Xenosaga Episode I (FR).iso` in PCSX2 and actually read a few
   translated screens — mails, then a U.M.N. chat. This is the only way to
   catch the font/glyph problem from Step 0 recurring on a word you didn't
   test.
3. Keep the `french/` tree under version control as your source of truth —
   `text-import` is deterministic and idempotent (re-running with no
   changes reports "nothing to write"), so the ISO is always reproducible
   from the tree plus a clean retail disc.

## Step 5 (advanced, limited) — one line of scene dialogue

*Update:* superseded. `evt-export` / `evt-import` and the translation
editor rebuild the scripts around lines of any length, with accents. The
same-length byte swap below is kept for the record.

For the subset that's safe today: a same-length swap directly on the
`.evt` bytes, bypassing `textpack.py` entirely (this is `repack.py`'s raw
`read_entry`/`patch_iso`, not the text pipeline — scene `.evt` objects are
single-copy uncompressed containers, so no disc-wide sweep is needed here,
unlike textures):

```python
from repack import read_entry, patch_iso

PATH = r"scene\ST0210.evt"
data = read_entry(iso, 0, PATH)
patched = data.replace(b'"Virtual Tutorial"', b'"Tutoriel virtuel"')  # same
                                                                        # byte
                                                                        # length
patch_iso(iso, {(0, PATH): patched})
```

Constraints, non-negotiable until a class-file rewriter exists:

1. **Byte length must not change at all** — the string is a length-prefixed
   Java constant-pool entry; everything after the pool is index-addressed,
   so only a same-length swap is safe. A shorter French line gets
   space-padded inside the quotes, never shortened.
2. Preserve the string's exact surrounding layout (leading spaces used for
   manual centering, trailing `\n`, and a trailing NUL that counts toward
   the stored length — a disc-wide quirk).
3. Stick to ASCII here — the renderer's expected encoding for non-ASCII
   constant-pool bytes (Shift-JIS vs Java's modified UTF-8) is unverified,
   same open question as Step 0 but for a different code path.

Finding lines to translate this way: `evt_unpack.py --dump ... --out ...`
then `grep -r` the extracted classes (or grep the dumped `.evt` files
directly — scene containers are uncompressed) for the on-screen English
string. This does not scale to a full script — it's a line-at-a-time
technique for the highest-value fixed-length lines until the real rewriter
gets built.
