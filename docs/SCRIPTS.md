# Translating into another alphabet: Arabic, Hebrew, Hindi, and the rest

The game draws text one cell at a time, left to right, from a bitmap font
that knows Japanese and Latin. Everything a different script needs —
letters the font never had, joining forms, right-to-left order, conjuncts
— is done at build time by the kit, and the game only ever sees a stream
of glyph codes. This is the usual way console fan translations handle
these scripts; the kit packages it so the whole trip is five commands.

What you need: the retail ISO, a TrueType/OpenType font for your script
(any free one works: Amiri or Noto Naskh Arabic, Noto Sans Hebrew,
Noto Sans Devanagari…), Python, and two optional packages for the
TrueType path:

```sh
pip install fonttools uharfbuzz
```

(Without them you can still draw cells by hand in a PNG sheet — see the
end of this page.)

## What it looks like

Every picture here is a capture of PCSX2 running the LANGTEST disc
described at the end of this page, drawn by the game from fonts and
files the kit wrote. (Earlier versions of this page showed composited
mock-ups; they are gone.)

| | |
|---|---|
| ![Hindi: the Encephalon cold open's first box](images/ingame-hindi-cold-open.png) | ![Hindi: KOS-MOS's program menu](images/ingame-hindi-menu.png) |
| ![Hindi: Allen's question, wrapped by the kit](images/ingame-hindi-question.png) | ![Hindi: the Yes/No box](images/ingame-hindi-yesno.png) |
| ![Arabic: the cold-open line, joined and right-to-left](images/ingame-arabic-cold-open.png) | ![Amharic: a U.M.N. mail body](images/ingame-amharic-mail.png) |
| ![Georgian (Mkhedruli): battle command bars from the overlay strings](images/ingame-georgian-battle.png) | ![Hindi: a subtitle burned into the first movie, the English blurred under it](images/ingame-hindi-movie.png) |

Dialogue, menus and the battle bars go through the game's font, exactly
as `encode` emits them. Movies are different: they carry their English
**burned into the picture**, so `subs-burn` blurs that band while a cue
is up and draws the new line over it, rendered by the kit from any
TrueType font (§ 4b).

## 1. Export the text

```sh
python cli.py text-export --iso GAME.iso --out tr/text        # menus, chats, mails
python cli.py evt-export  --iso GAME.iso --out tr/dialogue    # cutscene + field dialogue
```

Both trees are plain UTF-8. Dialogue files hold one string per line,
`@class.index<TAB>text`; translate the text after the tab and leave the
key. Keep every `⟦XX⟧` marker where it is: those are control codes
(colour, pause, name boxes) the engine reads in logical order. The
Japanese debug prints you will see in the scripts can stay as they are —
the encoder leaves the disc's own Japanese alone.

Write the translation in **logical order**, the way you type it. Do not
reverse anything, do not insert presentation forms, do not pre-wrap
except where the original had line breaks you want to keep.

## 2. Build the glyphs

```sh
python cli.py lang scan --iso GAME.iso --text tr --font Amiri-Regular.ttf \
    --script Arab --language ar --rtl --out tr/font
```

`scan` shapes every string in the tree with HarfBuzz, collects the glyphs
the translation actually uses (joined forms, ligatures, conjuncts, marks
drawn together with their base), gives each one a cell in the game's
font and draws it from your TrueType font into both faces. It writes:

* `glyphset.json` — which glyph got which code; keep it with the project
  so later runs reuse the same codes.
* `font0.tex`, `font1.tex` — the rebuilt fonts, plus `*_preview.png` of
  the sheet that holds the new cells.
* `langpack.json` — the options you used, so `encode` does not need them.

Where the glyphs go: the game has a 256-slot **proportional** block for
Latin text, each slot with its own width. The slots English needs
(digits, A-Z, a-z, space, punctuation — 95 of them) stay; the other 161
are yours. If your translation uses no Latin letters at all, add
`--reclaim-latin` for 213. Glyphs that do not fit, or overflow, go to the
2,992 **kanji cells**, drawn 20 px wide — fine for scripts with
near-uniform letter widths, visibly spaced for Arabic.

Size: `--px 20` (the em size in pixels) suits most fonts in the 24 px
cell; Devanagari with stacked marks may prefer `--px 17 --baseline 16`.
Look at the preview sheet and adjust. Glyphs wider than 18 px are
clipped in the proportional block; `scan` prints how many went to kanji
cells and which words used characters your font lacks.

Scripts tags are HarfBuzz/ISO 15924: `Arab`, `Hebr`, `Deva`, `Beng`,
`Thai`, `Cyrl`, `Grek`, `Geor`, `Armn`… `--rtl` for Arabic, Hebrew,
Syriac, Thaana.

## 2b. Menu and battle words: the executables

Tech names, "End", "Menu", item and skill descriptions, the field menu's
"Skills / Ether / Items" are C strings inside the five code overlays at
the disc root (`OV01.OVL`…) and the boot executable, not in any text
object. They are the third tree:

```sh
python cli.py ovl-export --iso GAME.iso --out tr/exe          # tr/exe/fs/OV01.OVL.txt …
```

One file per executable, `@address<TAB>text`, listing every string the
code points at (9,335 on the USA disc; unreferenced byte runs are left
out). Translate the text, keep the key; `lang scan` and `lang encode`
take this tree like the others (put it under the same `--text` root or
give it its own glyphset run). Overlay strings may be any length:
`ovl-import --grow` appends the long ones to the end of the overlay and
repoints every pointer and `lui`/`addiu` pair, and moves the overlay to
the end of the image if the next file leaves no room (OV01 has 870 bytes
of slack). The boot executable is in place only: its bss follows the
data, so a string there must fit its slot (up to the next non-zero
byte); the importer says which do not. A `lui` shared by several
addresses whose high half would have to change is reported, not
patched. See FORMATS.md § Code overlays for what is going on.

## 2c. The battle screen: its own font

Battle labels — tech names on the command bars, "Drone M", the Ether
and Items lists — are overlay strings (§ 2b), but the battle HUD draws
them with a **second font**: a grid of 10×18 cells in the status sheet
`yamamoto\stat\stat.xtx`, reached through a byte table that knows
digits and A–Z/a–z. The dialogue font's codes draw nothing there. So for
battle text:

```sh
python cli.py statfont extract --iso GAME.iso --out tr/statfont       # grid.png: what the sheet holds
python cli.py statfont scan --iso GAME.iso --text tr/ka --font NotoSansGeorgian-Regular.ttf \
    --script Geor --language ka --out tr/statfont                       # letters -> the 110 free cells
python cli.py statfont encode --text tr/ka --map tr/statfont/statfont.json --out tr/ka-btl
python cli.py ovl-import --iso OUT.iso --retail GAME.iso --text tr/ka-btl --out OUT.iso --grow
python cli.py patch --iso OUT.iso --out OUT.iso --set 'chain0:yamamoto\stat\stat.xtx=tr/statfont/stat.xtx'
```

`scan` shapes the words of the tree (the same HarfBuzz shaping as the
dialogue step), gives each glyph one of the grid's 110 free cells and
one of the 56 bytes the lookup table does not use, draws the glyph into
its cell at `--px` (13 by default; 10 px is all a cell allows) with the
sheet's own anti-aliasing shades, and writes the new `stat.xtx` plus
`statfont.json`; look at `grid.png` before you go on. `encode` turns the
same tree into strings of those bytes and adds one more line to the
overlay file: the lookup table itself (`@a46af8`), rewritten with the
new bytes at the new cells — it is an overlay string like the others,
so `ovl-import` writes it in place. One byte per letter keeps the HUD's
centring right (it centres by byte count); `encode --escape` writes
`#XX` cell escapes instead, which need no table change but start each
label a third too far left. `--retail` points the importer at the
unmodified disc so it can be run again on a disc whose overlay was
already rewritten. Short labels only: a 10 px cell will not hold a
Devanagari conjunct or an Arabic ligature legibly, the bars are about
16 cells wide, and 56 letters is the table's limit (Georgian's 33 fit;
Cyrillic's 66 do not all, so a translation mixes the two methods).

## 3. Encode the text

```sh
python cli.py lang encode --text tr --glyphset tr/font/glyphset.json \
    --out tr-enc --width 400 --align right
```

For every string, `encode` shapes each word again, measures it with the
new metrics, wraps to `--width` pixels (0 = only your own line breaks),
puts the words of right-to-left lines in visual order (Latin names and
numbers stay forward, brackets are mirrored, a line that is entirely
Latin stays left-to-right), optionally pads lines to the right edge, and
writes the glyphs as `⟦XX⟧⟦YY⟧` codes. The output tree has the same
layout as the input, so the importers take it unchanged.

Pixel widths: the dialogue box is about 400 px wide at the proportional
size; mails use a fixed 10 px pitch. When in doubt, measure a long
retail line: `langpack preview` prints the encoded bytes and
`font.Font.width_of_bytes` measures any string. Lines that are too wide
are not an error here — the engine wraps them itself, in the wrong place
for right-to-left text, which is exactly what `--width` prevents.

Check a line before you touch the disc:

```sh
python cli.py lang preview --glyphset tr/font/glyphset.json \
    --text "مرحباً بك في العالم الافتراضي، KOS-MOS (الإصدار 3)." --out line.png
```

That renders the line from the rebuilt cells the way the printer lays
them out.

## 4. Write the ISO

```sh
python cli.py text-import --iso GAME.iso --text tr-enc/text     --out AR.iso --grow
python cli.py evt-import  --iso AR.iso   --text tr-enc/dialogue --out AR.iso --grow
python cli.py ovl-import  --iso AR.iso   --text tr-enc/exe      --out AR.iso --grow
python cli.py patch --iso AR.iso --out AR.iso \
    --set chain0:font0.tex=tr/font/font0.tex --set chain0:font1.tex=tr/font/font1.tex
# battle labels, if you did § 2c: the battle strings tree and the status sheet
python cli.py ovl-import  --iso AR.iso --retail GAME.iso --text tr/ar-btl --out AR.iso --grow
python cli.py patch --iso AR.iso --out AR.iso --set 'chain0:yamamoto\stat\stat.xtx=tr/statfont/stat.xtx'
```

Each importer checks every file and writes nothing if any fails. Two
bytes per letter means most lines grow; `--grow` lets an object that no
longer fits its slot be relocated to the end of the image (about 78 MB
of growth stays burnable; emulators do not care). Omit it to be told
which files are over and by how much.

## 4b. Movies

The cutscene movies (`movie\mpeg2\*.pss` in chains 1 and 2, and the
58 raw layer-1 streams) have their English burned into the frames, so
a subtitle pass has to cover the old line and draw the new one, then
re-encode the video within the original's size and put it back over the
untouched audio. That is the plan poketony's XS1KOR used for the Korean
movies (fixed rows at 512×448, outline and shadow and a dark spread
over the original, a size-guarded MPEG-2 encode, the original ADPCM
kept); the kit does the same without libass, because many ffmpeg builds
lack it:

```sh
python cli.py extract --iso GAME.iso --chain 1 --glob 'movie*1001_1*' --out tr/mov   # the first movie
python cli.py subs-template --src tr/mov/dump/chain1/movie/mpeg2/1001_1.pss \
    --out tr/1001_1.hi.srt --detect               # one cue per interval the English is on screen
#   … write the Hindi into each cue (any text editor or subtitle editor) …
python cli.py subs-burn --src …/1001_1.pss --srt tr/1001_1.hi.srt --out tr/1001_1.hi.pss \
    --font Kohinoor.ttc --face 0 --px 22 --script Deva --language hi
python cli.py patch --iso HI.iso --out HI.iso --set 'chain1:movie\mpeg2\1001_1.pss=tr/1001_1.hi.pss'
```

`--detect` samples the subtitle band four times a second and counts
white pixels with a dark outline next to them, so a bright floor does
not count as text; on `1001_1.pss` it found the ten lines (plus two
stray cues in the noisy opening shot, and one pair of short lines
merged — look over the times before you write). `subs-burn --font`
renders each cue with the same HarfBuzz shaping as the font step, white
with a black outline, and composites it with ffmpeg's `overlay`; the
band at `--mask 0,330,512,90` is blurred while a cue is up. No font and
no HarfBuzz? Draw the cues in any image editor as `cue0001.png`,
`cue0002.png`… and pass `--cues DIR`. Arabic and Hebrew take `--rtl`.
Details and what is and is not verified: [SUBTITLES.md](SUBTITLES.md).

## 5. Boot it

The fastest check costs nothing: with PCSX2 fast boot, the USA disc
reaches the Encephalon tutorial's first dialogue box about a minute
after boot with no input — `scene\ST0210.evt`, the line that was
`"Virtual Tutorial"`. Translate that one line, build, boot, read. If the
letters join and read right-to-left there, the font block, the codes and
the direction handling are all proven at once; everything else is
content.

Things worth a look on the first full boot:

* **Auto line-feed.** The engine wraps proportional text itself when a
  line is too wide. For right-to-left text that break lands in the wrong
  place, so keep `--width` under the real box width.
* **Mails** draw every glyph at a fixed 10 px pitch (their text starts
  with the `⟦15⟧⟦02⟧⟦04⟧⟦07⟧` control that selects that mode). Arabic
  reads oddly at a fixed pitch; keep mail lines short or drop that
  control from the mail header line.
* **Menus and battle UI** come from the executables' own strings:
  § 2b, not the text or dialogue trees.
* **Box width.** The dialogue box wraps proportional text itself at a
  little under 400 px; `--width 370` keeps the kit's breaks and the
  engine's from stacking (a line of the Hindi test came out with one
  word alone on a line at 400).

## Worked example: the LANGTEST disc

A disc that carries several scripts at once, built on 2026-10-14 to see
the pipeline on the screen rather than in previews. It starts from the
pink-hair recolour ISO (so it doubles as a check that unrelated patches
coexist) and adds an Arabic cold-open line and an Amharic U.M.N. mail,
both drawn from the same rebuilt font files. Every step is a command
anyone can repeat; `tr/` is the working directory.

```sh
# 0. the base: a copy of an already-modded disc
cp "Xenosaga Episode I (PINK).iso" "Xenosaga Episode I (LANGTEST).iso"

# 1. the texts, in logical order, in two small trees (one script each)
python cli.py evt-export  --iso "…(USA).iso" --out tr/dialogue     # take ST0210.evt.txt, change one line
python cli.py text-export --iso "…(USA).iso" --out tr/text         # take umn/0000.uml.utf8.txt, rewrite it
#    tr/ar/chain0/scene/ST0210.evt.txt   @3.244z  "درس افتراضي"   (was "Virtual Tutorial")
#    tr/am/chain0/umn/0000.uml.utf8.txt  Subject/Sender lines + Amharic body, no ⟦15⟧⟦02⟧⟦04⟧⟦07⟧

# 2. glyphs, one scan per script, the second extending the first
python cli.py lang scan --iso "…(USA).iso" --text tr/ar --font SFArabic.ttf \
    --script Arab --language ar --rtl --out tr/font-ar
python cli.py lang scan --font0 tr/font-ar/font0.tex --font1 tr/font-ar/font1.tex \
    --extend tr/font-ar/glyphset.json --text tr/am --font KefaIII.ttf \
    --script Ethi --language am --px 18 --out tr/font-am
#    -> 8 Arabic + 73 Ethiopic glyphs in 81 proportional slots; 80 remain free

# 3. encode, each tree with its own glyphset
python cli.py lang encode --text tr/ar --glyphset tr/font-ar/glyphset.json --out tr/ar-enc
python cli.py lang encode --text tr/am --glyphset tr/font-am/glyphset.json --out tr/am-enc --width 400

# 4. write everything into the disc (the mail fits its slot; --grow is harmless)
python cli.py evt-import  --iso LANGTEST.iso --text tr/ar-enc --out LANGTEST.iso --grow
python cli.py text-import --iso LANGTEST.iso --text tr/am-enc --out LANGTEST.iso --grow
python cli.py patch --iso LANGTEST.iso --out LANGTEST.iso \
    --set chain0:font0.tex=tr/font-am/font0.tex --set chain0:font1.tex=tr/font-am/font1.tex

# ---- second day: Hindi dialogue, Georgian battle words, a Hindi movie ----
# 5. Hindi: seven lines of ST0210 on the path to the first battle (title,
#    KOS-MOS's question, the four-item program menu, Allen's confirmation, Yes/No)
#    tr/hi/chain0/scene/ST0210.evt.txt   @3.244z "आभासी प्रशिक्षण", @3.315z "सामान्य हमले\nटेक हमले\n…", @3.330z "हाँ\nनहीं" …
python cli.py lang scan --font0 tr/font-am/font0.tex --font1 tr/font-am/font1.tex \
    --extend tr/font-am/glyphset.json --text tr/hi --font Kohinoor.ttc --face 0 \
    --script Deva --language hi --px 17 --baseline 16 --out tr/font-hi
#    -> 70 distinct glyphs: 61 more proportional slots, 9 wide cells (conjuncts over 18 px)
python cli.py lang encode --text tr/hi --glyphset tr/font-hi/glyphset.json --out tr/hi-enc --width 370

# 6. Georgian: the battle words, from the executable-strings tree
python cli.py ovl-export --iso GAME.iso --out tr/exe      # find the addresses in tr/exe/fs/OV01.OVL.txt
#    tr/ka/fs/OV01.OVL.txt  (same header line, twelve @address lines)
#    @a4c4b8 NEEDLE→ნემსი  @a4c4d0 BLASTER→ბლასტერი  @a46970 End→დასრულება  @a46978 Menu→მენიუ
#    @a4c480 Stun Shock→შოკი  @a4c490 Power Kick→ძლიერი დარტყმა  @a4cf88 R・BLADE→რ-დანა  @a4b5d8 F・GSHOT→ფ-გსროლა …
python cli.py lang scan --font0 tr/font-hi/font0.tex --font1 tr/font-hi/font1.tex \
    --extend tr/font-hi/glyphset.json --text tr/ka --font SFGeorgian.ttf --script Geor --language ka --px 18 --out tr/font-ka
#    -> 22 Mkhedruli glyphs; the proportional block is now full (161 of 161), the last 12 in wide cells
python cli.py lang encode --text tr/ka --glyphset tr/font-ka/glyphset.json --out tr/ka-enc

# 7. the first movie (Shion and Allen in the lab, ten lines), Hindi cues over the blurred English
python cli.py extract --iso GAME.iso --chain 1 --glob 'movie*1001_1*' --out tr/mov --no-carve
python cli.py subs-template --src tr/mov/dump/chain1/movie/mpeg2/1001_1.pss --out tr/1001_1.hi.srt --detect
#    … fill the ten cues …
python cli.py subs-burn --src tr/mov/dump/chain1/movie/mpeg2/1001_1.pss --srt tr/1001_1.hi.srt \
    --out tr/1001_1.hi.pss --font Kohinoor.ttc --face 0 --px 22 --script Deva --language hi
#    -> 19,921,499 bytes (was 20,004,868), 2,728 kbps, first attempt

# 8. into the disc
python cli.py evt-import --iso LANGTEST.iso --text tr/hi-enc --out LANGTEST.iso --grow
python cli.py ovl-import --iso LANGTEST.iso --text tr/ka-enc --out LANGTEST.iso --grow
#    -> OV01.OVL: 7 strings moved to 0xa5b8f8…, 5 in place, 425,114 -> 425,264 bytes (fits its slot)
python cli.py patch --iso LANGTEST.iso --out LANGTEST.iso \
    --set chain0:font0.tex=tr/font-ka/font0.tex --set chain0:font1.tex=tr/font-ka/font1.tex \
    --set 'chain1:movie\mpeg2\1001_1.pss=tr/1001_1.hi.pss'
```

Notes from doing it:

* **Mails and the fixed pitch.** Retail mail bodies open with the
  control `⟦15⟧⟦02⟧⟦04⟧⟦07⟧`, which makes every glyph advance 10 px.
  The Amharic body simply leaves it out, so the viewer lays the text out
  proportionally like dialogue. The `Subject: ` and `Sender: ` labels and
  the `\r\x02` marker after `Subject: ` are kept exactly; the encoder
  passes raw control characters through like `⟦XX⟧` markers.
* **One disc, several fonts.** `--extend` keeps the slots the first scan
  gave out; `--font0/--font1` point the second scan at the first scan's
  output instead of the retail files. Each script is encoded with the
  glyphset that contains its glyphs (the later one contains both).
* **Scripts the font lacks** are reported by `scan` ("word uses
  characters this font lacks") rather than silently drawn as boxes;
  that is how the Hebrew word in an Arabic test tree showed up.
* **Patching a disc an emulator is running from** is a bad idea: the
  first boot of LANGTEST hung at the publisher logo after the fonts were
  rewritten underneath it. Finish the disc, then boot it.

Driving the emulator without touching the desktop: PCSX2 started with
`-nogui -fastboot -nofullscreen -batch LANGTEST.iso`, focus handed back
to the previous application, pad presses posted to PCSX2's PID with
Quartz (`CGEventPostToPid`, the Cioppinosaga `bg_input.py` method), the
window captured by id with `screencapture -l`. No keystroke or screenshot
touches the user's own windows.

**Results, 2026-10-14, PCSX2 v2.7.524 on the LANGTEST disc:**

| | |
|---|---|
| ![In-game: the Encephalon cold-open line in Arabic](images/ingame-arabic-cold-open.png) | ![In-game: a U.M.N. mail body in Amharic](images/ingame-amharic-mail.png) |

![Magnified: the Arabic line as the game drew it](images/ingame-arabic-cold-open-crop.png)

* The first dialogue box of a New Game shows ``"درس افتراضي"`` joined and
  right-to-left, drawn by the game's own printer from the rebuilt
  `font0.tex`: raw two-byte codes in the proportional block take the same
  path as the ASCII table's output. That was the one open question.
* The first inbox mail on the Woglinde (savestate, `umn\0080.uml` re-written
  while the game was running, inbox re-opened) shows the Amharic body
  wrapped at 400 px with mixed Latin, Ethiopic punctuation and the ■
  bullet — proportional, because the body omits the fixed-pitch control.
  The subject/sender line stayed English: the mail list is cached in RAM
  when the save is loaded, so only the body was re-read from disc.
* Not exercised by this disc: relocation past the image end (both
  objects fit their slots). The scratch build that relocates ST0210 reads
  back correctly through the TOC but has not been booted.

Driving notes that cost time, so you do not pay them again: PCSX2 pad
keys need a **~300 ms hold** when posted by PID (80-90 ms taps are
ignored at the title even though hotkeys like F8 register); Circle is
confirm; the field menu will not open while standing on a ladder; the
`-nogui` launch is fine for captures but was not needed.

**Results, 2026-10-15, PCSX2 v2.9.127, the same disc with the second
day's additions:**

| | |
|---|---|
| ![Hindi: the cold open's title box](images/ingame-hindi-cold-open.png) | ![Hindi: KOS-MOS's program menu](images/ingame-hindi-menu.png) |
| ![Hindi: Allen's question](images/ingame-hindi-question.png) | ![Hindi: the Yes/No box](images/ingame-hindi-yesno.png) |
| ![Hindi: subtitle in the first movie](images/ingame-hindi-movie.png) | ![Hindi: another cue of the same movie](images/ingame-hindi-movie-2.png) |

![Georgian: KOS-MOS's command bars in the battle font](images/ingame-georgian-battle.png)

![Magnified: the bars](images/ingame-georgian-battle-crop.png)

* **Hindi dialogue**: every line on the path to the first battle, with
  conjuncts (क्ष, द्ध, प्र) drawn as single cells. The engine's own wrap
  of the long lines sits a little under 400 px, hence `--width 370`.
* **The movie**: the lab scene plays with ten Hindi cues over the
  blurred English and the original audio, and the game goes on to the
  next scene. The first two builds did not: without a PTS/DTS on nearly
  every picture the player sat on a black frame and read to the end of
  the disc, and with the stamps but the packs drifted off 16 KB the
  player raised "A seek error has occurred". The muxer in `subs.splice`
  now keeps the retail pack shape and pacing (SUBTITLES.md).
* **The battle bars**: KOS-MOS's turn shows მუშტი (PUNCH), ბლასტერი
  (BLASTER) and ფ-გსროლა (F・GSHOT) in Mkhedruli, drawn by the HUD from
  cells `statfont scan` painted into the status sheet and strings of one
  byte per letter plus the rewritten lookup table. The first attempt
  encoded the same words for the dialogue font and the bars came up
  empty — the two fonts are separate, which is what § 2c is about. The
  `#XX` escape form drew the letters too but started each label a third
  too far left (the HUD centres by byte count).
* Twelve overlay strings were moved past OV01's old end for this and
  read back correctly in RAM; a relocated ASCII string drew as well as
  an in-place one.



## Hand-drawn cells instead of a TrueType font

If you would rather draw the letters yourself, or have no shaping
packages: make a PNG whose width is a multiple of 20 and height a
multiple of 24, one glyph per cell left-to-right, top-to-bottom, white
on black (grey levels are kept as the font's four shades), and a text
file with one key per line in the same order. Keys are the characters
(for Arabic, the presentation forms — the built-in table turns words into
them). Then:

```sh
python cli.py lang scan --iso GAME.iso --text tr --cells mycells.png --keys keys.txt --rtl --out tr/font
```

`encode` and the rest are unchanged. XS1KOR, the Korean localisation of
the Japanese disc, did it this way for 2,048 Hangul cells.

## Credits

The font files' GS layout and the fact that a TrueType face renders into
these cells and plays were first shown by poketony's
[XS1KOR](https://github.com/poketony/XS1KOR); the engine's cell
addressing, the proportional slot permutation and the width rule were
recovered from the USA executable for this kit, and the shaping,
ordering and encoding pipeline is the kit's own.
