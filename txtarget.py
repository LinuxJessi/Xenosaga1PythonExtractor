"""txtarget.py — the language a translation goes INTO, and how a build draws it.

The translation editor's source text is the English of the USA disc (or the
Japanese of a Japanese disc); its target can be any language. What a build
has to do depends on what the retail fonts can already draw:

* **builtin** — every letter of the language is in the retail fonts: ASCII
  plus the USA proportional accent block (``txcodec.LATIN_MAP``: French,
  Spanish, German, Italian, Portuguese, Dutch, Catalan, the Nordic
  languages...). ``txcodec`` writes the retail codes; nothing is drawn.
* **script-hybrid** — a Latin alphabet with a few letters the block lacks
  (Polish ł ś ż ć ń ą ę ź, Czech č ř ě ů, Turkish ğ ı ş, Romanian ș ț, ...):
  the letters the block has stay retail, the others are drawn from a
  TrueType font into free font cells.
* **japanese** — the retail fonts draw JIS X 0208 natively (kana,
  punctuation, full-width forms, level-1 kanji, rows A1-CF) EXCEPT level-2
  kanji: EUC rows D0 and up are the USA proportional block on this disc
  (``txcodec`` refuses those codes), so every level-2 kanji a translation
  uses is drawn from a Japanese TrueType font into a kanji cell the
  translation does not use (handed out from the last row backwards).
* **script** — a script the disc never had (Cyrillic, Greek, Arabic,
  Hebrew, Devanagari, Hangul, Han, Thai, Georgian, Ethiopic, Armenian, or
  any other with a font that covers it): every letter is shaped with
  HarfBuzz and drawn from a TrueType font. Right-to-left scripts are
  ordered, wrapped and (optionally) right-aligned at build time.

Kinds are decided from the language's alphabet against the retail fonts
(``_kind_of``), never by hand; ``TARGETS`` is the table the editor offers,
``profile(code)`` one entry, ``coverage()`` the per-character answer for a
set of strings. The build path (``plan_glyphs``) is used by ``txbuild``
whenever a project's translations need any new glyph; a builtin or
Japanese project whose text needs none is built exactly as before.

What a new-script build does (one pass, every family, deterministic):

1. Every translated string of the project is NFC-normalised and split into
   runs: characters the retail face draws natively (ASCII, the accent block,
   JIS) and characters that need a glyph. Protected tokens (``⟦XX⟧`` runs,
   evt ``/[...]`` tags, chat ``\\NN`` escapes, printf formats, U.M.N. ``@x``
   codes) ride along as control placeholders, never shaped or mirrored.
2. ``langpack.Encoder(collect=True)`` collects the glyph keys of every
   field-font string (both faces: font0 for "ev" cutscenes, font1 for the
   rest, each row with its own face). ONE ``glyphs.GlyphSet.allocate`` over
   all of them, most used first, keys sorted for ties: free proportional
   slots, then kanji cells. Never taken: the ASCII slots, the block cells
   and kanji cells ANY retail string still draws (``retail_usage``: every
   row of the four families, so untranslated English keeps its ä — ™ ® ç
   and ・), and the cells of every native character the translation uses
   (in EVERY edited row, also the rows that need no new glyph).
3. Each edited string is encoded on its own with ``Encoder.string`` (never
   a whole tree: that re-wraps untranslated lines). New glyphs come out as
   ``txcodec.glyph_char(code)`` characters, which the family modules
   validate and write like any text inside ``txcodec.glyph_codes()``.
   Right-to-left rows get the Unicode bidi run order, mirrored brackets, a
   wrap width (the row's guide, 370 px for dialogue, or the project's
   ``wrap``) and, with ``align: right``, right alignment. Tokens at a
   line's edges stay there; a state token (``/[...]``, chat ``\\NNN``,
   ``⟦XX⟧``) in the middle of a right-to-left line is refused with the row
   id (word reordering would move it onto other words).
4. Battle-font rows (OV01 ``menuFontPut``) get their letters drawn into the
   battle grid's free cells (``statfont``; cells 125+ first, then 63-123;
   cells the retail strings or any edited battle row's accent escapes use
   are kept; one cell per HarfBuzz cluster, marks placed from their own
   base, ``statfont.cell_clusters``) and one byte each from the
   bytes the 230-byte table does not use (bytes any battle string still
   writes are kept); the new table is written in the same OV01 apply as the
   strings (``txfam_ovl.apply(raw_overrides=...)``); letters beyond the 56
   spare bytes use the ``#XX`` cell escape.
5. Tag-font rows (title ribbons, ``PrintTagFont``) have only A-Z a-z 0-9
   ``: / . -``: a non-Latin letter there is refused with the row id.

Nothing derived is stored: the project holds only ``target`` (code, kind,
script, rtl, font path, face, sizes, align, wrap); the glyph set is
regenerated from the logical text on every build, so a build is
reproducible from the project and the font.

Rendering: the retail Latin is condensed (H = 7 x 15 px on a baseline at
row 20 of the 20x24 cell), so each script has default ``px`` / ``baseline``
/ ``xscale`` (horizontal squeeze) values that match it (``RENDER``); a
project can override them.

Public API (for txserver / txcatalog / txbuild):

    TARGETS                      list of profile dicts (see profile())
    profile(code) -> dict|None   one profile: code, name, endonym, kind, script,
                                 rtl, sample, alphabet, fonts (Noto names),
                                 render {px, baseline, xscale}
    normalise_target(d) -> dict  clean project["target"] (raises TargetError)
    resolve_target(project)      profile defaults + project["target"] (+ language)
    coverage(texts, target, font="field", face="font1", ttf=None, ttf_face=0)
                                 -> {native, glyph, folded, impossible,
                                     missing_in_font, counts}
    system_fonts(script=None, chars=None) -> [{path, face, family, style,
                                 noto, scripts}] fonts installed on this machine
    suggest_fonts(target, chars=None) -> {"noto": [family], "installed": system_fonts()[:12]}
    shaping_available() -> (bool, message)
    needs_glyphs(target, rows, edits) -> bool
    plan_glyphs(retail, target, rows, edits, log) -> GlyphPlan
    retail_usage(retail) -> Usage
    preview(retail, target, text, font="field", face="font1", ctl="c0") -> ([png per line], encoded)

    python3 txtarget.py list
    python3 txtarget.py fonts --script Cyrl
    python3 txtarget.py coverage --code pl --text "Zażółć gęślą jaźń"
    python3 txtarget.py selftest --iso RETAIL [--out DIR]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import font as fontmod  # noqa: E402
import txcodec as tc  # noqa: E402

KINDS = ("builtin", "script-hybrid", "japanese", "script")
DIALOGUE_WRAP = 370          # px: the engine re-wraps dialogue a little under 400
CACHE_DIR = Path.home() / ".cache" / "xs1-txeditor"


class TargetError(tc.TxBuildError):
    """A target the build cannot draw; the message says why (per row id)."""


# ---------------------------------------------------------------------------
# rendering defaults per script
# ---------------------------------------------------------------------------
# px = em size, baseline = cell row of the baseline (retail: ink ends on row
# 19, so 20), xscale = horizontal squeeze to the retail Latin's proportions.
# battle_* = the same for the 10x18 battle grid. The retail battle letters
# are 8 px wide with a 15 px cap height on a baseline at row 17 (measured from
# stat.xtx: H rows 2-16, x rows 6-16), so 20 px squeezed to 0.55 matches them
# (statfont's own default, 13 px on row 13, draws letters half that size).
RENDER: Dict[str, dict] = {
    "Latn": {"px": 21.0, "baseline": 20, "xscale": 0.62},
    "Cyrl": {"px": 21.0, "baseline": 20, "xscale": 0.62},
    "Grek": {"px": 21.0, "baseline": 20, "xscale": 0.62},
    "Armn": {"px": 21.0, "baseline": 20, "xscale": 0.62},
    "Geor": {"px": 20.0, "baseline": 19, "xscale": 0.66},
    "Hebr": {"px": 21.0, "baseline": 20, "xscale": 0.68},
    "Arab": {"px": 20.0, "baseline": 18, "xscale": 0.80},
    "Deva": {"px": 18.0, "baseline": 16, "xscale": 0.80},
    "Thai": {"px": 19.0, "baseline": 17, "xscale": 0.75},
    "Ethi": {"px": 19.0, "baseline": 19, "xscale": 0.75},
    "Hang": {"px": 19.0, "baseline": 17, "xscale": 1.0},
    "Hani": {"px": 19.0, "baseline": 17, "xscale": 1.0},
    "Jpan": {"px": 19.0, "baseline": 17, "xscale": 1.0},
}
BATTLE_RENDER = {"battle_px": 20.0, "battle_baseline": 17, "battle_xscale": 0.55}
_DEFAULT_RENDER = {"px": 20.0, "baseline": 18, "xscale": 0.8}

# Noto families that cover each script (free, SIL OFL)
NOTO: Dict[str, List[str]] = {
    "Latn": ["Noto Sans", "Noto Serif"], "Cyrl": ["Noto Sans", "Noto Serif"],
    "Grek": ["Noto Sans", "Noto Serif"], "Armn": ["Noto Sans Armenian"],
    "Geor": ["Noto Sans Georgian"], "Hebr": ["Noto Sans Hebrew"],
    "Arab": ["Noto Sans Arabic", "Noto Naskh Arabic"], "Deva": ["Noto Sans Devanagari"],
    "Thai": ["Noto Sans Thai"], "Ethi": ["Noto Sans Ethiopic"],
    "Hang": ["Noto Sans KR"], "Hani": ["Noto Sans SC", "Noto Sans TC"],
    "Jpan": ["Noto Sans JP"],
}

# Characters a font must have to count as covering a script (system_fonts).
_PROBE: Dict[str, str] = {
    "Latn": "ąćęłńśźżčďěňřšťůžőűășțğışđĉĝĥĵŝŭ",
    "Cyrl": "абвгдеёжзийклмнопрстуфхцчшщъыьэюяАБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯіїєґ",
    "Grek": "αβγδεζηθικλμνξοπρστυφχψωςάέήίόύώΑΒΓΔΕΖΗΘΙΚΛΜΝΞΟΠΡΣΤΥΦΧΨΩ",
    "Armn": "ԱԲԳԴԵԶԷԸԹԺԻԼԽԾԿՀՁՂՃՄՅՆՇՈՉՊՋՌՍՎՏՐՑՒՓՔՕՖաբգդեզէըթժիլխծկհձղճմյնշոչպջռսվտրցւփքօֆ",
    "Geor": "აბგდევზთიკლმნოპჟრსტუფქღყშჩცძწჭხჯჰ",
    "Hebr": "אבגדהוזחטיכךלמםנןסעפףצץקרשת",
    "Arab": "ءآأؤإئابةتثجحخدذرزسشصضطظعغفقكلمنهوىيپچژگکی،؟",
    "Deva": "अआइईउऊएऐओऔकखगघङचछजझञटठडढणतथदधनपफबभमयरलवशषसहािीुूेैोौंः्।",
    "Thai": "กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮะาำิีึืุูเแโใไ่้๊๋",
    "Ethi": "ሀለሐመሠረሰሸቀበተቸኀነኘአከኸወዐዘዠየደጀገጠጨጰጸፀፈፐ።",
    "Hang": "가나다라마바사아자차카타파하한국어안녕세요",
    "Hani": "的一是不了人我在有他这中大来上国个到说们为子和你地出道也时年",
    "Jpan": "あいうえおかきくけこアイウエオカキクケコ日本語漢字",
}


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------
# (code, English name, endonym, script, rtl, alphabet beyond ASCII, sample,
#  kind for non-Latin scripts). Latin kinds are computed from the alphabet.
_RAW = [
    ("fr", "French", "Français", "Latn", False, "àâæçéèêëîïôœùûüÿÀÂÆÇÉÈÊËÎÏÔŒÙÛÜŸ«»",
     "Portez ce vieux whisky au juge blond qui fume. « Où est KOS-MOS ? »"),
    ("es", "Spanish", "Español", "Latn", False, "áéíñóúüÁÉÍÑÓÚÜ¿¡«»",
     "El veloz murciélago hindú comía feliz cardillo y kiwi. ¿Dónde está?"),
    ("de", "German", "Deutsch", "Latn", False, "äöüßÄÖÜ„“",
     "Victor jagt zwölf Boxkämpfer quer über den großen Sylter Deich."),
    ("it", "Italian", "Italiano", "Latn", False, "àèéìíîòóùúÀÈÉÌÍÎÒÓÙÚ",
     "Ma la volpe col suo balzo ha raggiunto il quieto Fido. Perché è così?"),
    ("pt", "Portuguese", "Português", "Latn", False, "áâãàçéêíóôõúÁÂÃÀÇÉÊÍÓÔÕÚ",
     "Luís argüia à Júlia que «brações, fé, chá, óxido, pôr, zângão» eram palavras."),
    ("pt-BR", "Portuguese (Brazil)", "Português (Brasil)", "Latn", False,
     "áâãàçéêíóôõúÁÂÃÀÇÉÊÍÓÔÕÚ", "À noite, vovô Kowalsky vê o ímã cair no pé do pinguim."),
    ("nl", "Dutch", "Nederlands", "Latn", False, "éëïöüèáóúÉËÏÖÜ",
     "Pa's wijze lynx bezag vroom het fikse aquaduct. Één, twee, drieëntwintig."),
    ("ca", "Catalan", "Català", "Latn", False, "àçéèíïòóúüÀÇÉÈÍÏÒÓÚÜ·",
     "Jove xef, porti whisky amb quinze glaçons d'hidrogen, coi! Col·lega."),
    ("sv", "Swedish", "Svenska", "Latn", False, "åäöÅÄÖ",
     "Flygande bäckasiner söka hwila på mjuka tuvor."),
    ("da", "Danish", "Dansk", "Latn", False, "æøåÆØÅ",
     "Høj bly gom vandt fræk sexquiz på wc."),
    ("no", "Norwegian", "Norsk", "Latn", False, "æøåÆØÅ",
     "Vår sære Zulu fra badeøya spilte jo whist og quickstep i min taxi."),
    ("fi", "Finnish", "Suomi", "Latn", False, "äöåšžÄÖÅŠŽ",
     "Törkylempijävongahdus. Šakki ja žirafi."),
    ("is", "Icelandic", "Íslenska", "Latn", False, "áðéíóúýþæöÁÐÉÍÓÚÝÞÆÖ",
     "Kæmi ný öxi hér, ykist þjófum nú bæði víl og ádrepa."),
    ("ga", "Irish", "Gaeilge", "Latn", False, "áéíóúÁÉÍÓÚ",
     "D'fhuascail Íosa Úrmhac na hÓighe Beannaithe pór Éava agus Ádhaimh."),
    ("af", "Afrikaans", "Afrikaans", "Latn", False, "êëéèîïôûÊËÉÈÎÏÔÛ",
     "Sê vir die mense: môre is ’n nuwe dag."),
    ("sq", "Albanian", "Shqip", "Latn", False, "çëÇË",
     "Unë jam KOS-MOS. Çfarë dëshironi?"),
    ("id", "Indonesian", "Bahasa Indonesia", "Latn", False, "",
     "Saya KOS-MOS. Apa yang bisa saya bantu?"),
    ("eo", "Esperanto", "Esperanto", "Latn", False, "ĉĝĥĵŝŭĈĜĤĴŜŬ",
     "Eĥoŝanĝo ĉiuĵaŭde. Laŭ Ludoviko Zamenhof bongustas freŝa ĉeĥa manĝaĵo."),
    ("pl", "Polish", "Polski", "Latn", False, "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
     "Zażółć gęślą jaźń. Pchnąć w tę łódź jeża lub ośm skrzyń fig."),
    ("cs", "Czech", "Čeština", "Latn", False, "áčďéěíňóřšťúůýžÁČĎÉĚÍŇÓŘŠŤÚŮÝŽ",
     "Příliš žluťoučký kůň úpěl ďábelské ódy."),
    ("sk", "Slovak", "Slovenčina", "Latn", False, "áäčďéíĺľňóôŕšťúýžÁÄČĎÉÍĹĽŇÓÔŔŠŤÚÝŽ",
     "Kŕdeľ šťastných ďatľov učí pri ústí Váhu mĺkveho koňa obhrýzať kôru."),
    ("sl", "Slovenian", "Slovenščina", "Latn", False, "čšžČŠŽ",
     "Hišničin bratec vzgaja polžke še čez zimo."),
    ("hr", "Croatian", "Hrvatski", "Latn", False, "čćđšžČĆĐŠŽ",
     "Gojazni đačić s biciklom drži hmelj i finu vatu u džepu nošnje."),
    ("hu", "Hungarian", "Magyar", "Latn", False, "áéíóöőúüűÁÉÍÓÖŐÚÜŰ",
     "Árvíztűrő tükörfúrógép."),
    ("ro", "Romanian", "Română", "Latn", False, "ăâîșțĂÂÎȘȚ",
     "Înjurând pițigăiat, zoofobul comandă vexat whisky și tequila."),
    ("tr", "Turkish", "Türkçe", "Latn", False, "çğıİöşüÇĞÖŞÜ",
     "Pijamalı hasta yağız şoföre çabucak güvendi."),
    ("et", "Estonian", "Eesti", "Latn", False, "äöõüšžÄÖÕÜŠŽ",
     "Põdur Zagrebi tšellomängija-följetonist Ciqo külmetas kehvas garaažis."),
    ("lt", "Lithuanian", "Lietuvių", "Latn", False, "ąčęėįšųūžĄČĘĖĮŠŲŪŽ",
     "Įlinkdama fechtuotojo špaga sublykčiojusi pragręžė apvalų arbūzą."),
    ("lv", "Latvian", "Latviešu", "Latn", False, "āčēģīķļņšūžĀČĒĢĪĶĻŅŠŪŽ",
     "Glāžšķūņa rūķīši dzērumā čiepj Baha koncertflīģeļu vākus."),
    ("vi", "Vietnamese", "Tiếng Việt", "Latn", False,
     "àáảãạăằắẳẵặâầấẩẫậèéẻẽẹêềếểễệìíỉĩịòóỏõọôồốổỗộơờớởỡợùúủũụưừứửữựỳýỷỹỵđĐ",
     "Tôi là KOS-MOS. Bạn cần giúp gì không?"),
    ("ja", "Japanese", "日本語", "Jpan", False, "", "こんにちは、シオン。漢字と仮名、そして叡智。",
     "japanese"),
    ("ru", "Russian", "Русский", "Cyrl", False, _PROBE["Cyrl"][:66],
     "Съешь же ещё этих мягких французских булок, да выпей чаю.", "script"),
    ("uk", "Ukrainian", "Українська", "Cyrl", False,
     "абвгґдеєжзиіїйклмнопрстуфхцчшщьюяАБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ’",
     "Чуєш їх, доцю, га? Кумедна ж ти, прощайся без ґольфів!", "script"),
    ("bg", "Bulgarian", "Български", "Cyrl", False,
     "абвгдежзийклмнопрстуфхцчшщъьюяАБВГДЕЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЬЮЯ",
     "Жълтата дюля беше щастлива, че пухът, който цъфна, замръзна като гьон.", "script"),
    ("sr", "Serbian (Cyrillic)", "Српски", "Cyrl", False,
     "абвгдђежзијклљмнњопрстћуфхцчџшАБВГДЂЕЖЗИЈКЛЉМНЊОПРСТЋУФХЦЧЏШ",
     "Фијуче ветар у шибљу, леди пасаже и куће иза њих и гунђа у оџацима.", "script"),
    ("el", "Greek", "Ελληνικά", "Grek", False, _PROBE["Grek"],
     "Ξεσκεπάζω την ψυχοφθόρα βδελυγμία.", "script"),
    ("hy", "Armenian", "Հայերեն", "Armn", False, _PROBE["Armn"],
     "Բարեւ, աշխարհ։", "script"),
    ("ka", "Georgian", "ქართული", "Geor", False, _PROBE["Geor"],
     "გამარჯობა, სამყარო!", "script"),
    ("ar", "Arabic", "العربية", "Arab", True, _PROBE["Arab"][:44],
     "مرحبا بالعالم! أنا KOS-MOS (3).", "script"),
    ("fa", "Persian", "فارسی", "Arab", True, _PROBE["Arab"],
     "سلام دنیا! من KOS-MOS هستم.", "script"),
    ("he", "Hebrew", "עברית", "Hebr", True, _PROBE["Hebr"],
     "שלום עולם! אני KOS-MOS.", "script"),
    ("hi", "Hindi", "हिन्दी", "Deva", False, _PROBE["Deva"],
     "नमस्ते दुनिया! मैं KOS-MOS हूँ।", "script"),
    ("th", "Thai", "ไทย", "Thai", False, _PROBE["Thai"],
     "สวัสดีชาวโลก", "script"),
    ("am", "Amharic", "አማርኛ", "Ethi", False, _PROBE["Ethi"],
     "ሰላም ዓለም።", "script"),
    ("ko", "Korean", "한국어", "Hang", False, "", "안녕하세요, 세계! 저는 KOS-MOS입니다.", "script"),
    ("zh-Hans", "Chinese (Simplified)", "简体中文", "Hani", False, "",
     "你好，世界！我是KOS-MOS。", "script"),
    ("zh-Hant", "Chinese (Traditional)", "繁體中文", "Hani", False, "",
     "你好，世界！我是KOS-MOS。", "script"),
]

# right-to-left scripts (used for custom "other" targets too)
RTL_SCRIPTS = {"Arab", "Hebr", "Syrc", "Thaa", "Nkoo", "Adlm", "Mand", "Samr", "Rohg"}


def _native_both(ch: str) -> bool:
    return tc.draws_natively(ch, tc.FIELD, tc.FONT1) and tc.draws_natively(ch, tc.FIELD, tc.FONT0)


def _kind_of(script: str, alphabet: str, given: Optional[str]) -> Tuple[str, str]:
    """(kind, letters the retail fonts lack) for a profile."""
    if script != "Latn":
        return given or "script", ""
    lacking = "".join(c for c in dict.fromkeys(alphabet) if not _native_both(c))
    return ("builtin" if not lacking else "script-hybrid"), lacking


def _make_targets() -> List[dict]:
    out = []
    for rec in _RAW:
        code, name, endo, script, rtl, alphabet, sample = rec[:7]
        given = rec[7] if len(rec) > 7 else None
        kind, lacking = _kind_of(script, alphabet, given)
        out.append({
            "code": code, "name": name, "endonym": endo, "kind": kind, "script": script,
            "rtl": rtl, "alphabet": alphabet, "lacking": lacking, "sample": sample,
            "fonts": list(NOTO.get(script, ["Noto Sans"])),
            "render": dict(RENDER.get(script, _DEFAULT_RENDER)),
        })
    out.append({
        "code": "other", "name": "Other (custom script)", "endonym": "", "kind": "script",
        "script": "", "rtl": False, "alphabet": "", "lacking": "", "sample": "",
        "fonts": [], "render": dict(_DEFAULT_RENDER),
    })
    return out


# ---------------------------------------------------------------------------
# the language registry (txlangs.py): every other language and written form
# ---------------------------------------------------------------------------
# The 47 profiles above are the long-tested ones; txlangs adds the rest (382
# lines: Cherokee, Inuktitut, Navajo, Hawaiian, Yoruba, Tifinagh, N'Ko, Adlam,
# ...). Each registry line becomes a profile of the same shape; its kind is
# computed from its letters exactly as above. A registry line for a language
# that already has a profile only adds its regions and note.
SCRIPT_NAMES: Dict[str, str] = {
    "Latn": "Latin", "Cyrl": "Cyrillic", "Grek": "Greek", "Armn": "Armenian", "Geor": "Georgian",
    "Hebr": "Hebrew", "Arab": "Arabic", "Syrc": "Syriac", "Thaa": "Thaana", "Nkoo": "N'Ko",
    "Deva": "Devanagari", "Beng": "Bengali", "Guru": "Gurmukhi", "Gujr": "Gujarati", "Orya": "Odia",
    "Taml": "Tamil", "Telu": "Telugu", "Knda": "Kannada", "Mlym": "Malayalam", "Sinh": "Sinhala",
    "Thai": "Thai", "Laoo": "Lao", "Tibt": "Tibetan", "Mymr": "Myanmar", "Ethi": "Ethiopic",
    "Cher": "Cherokee", "Cans": "Canadian syllabics", "Khmr": "Khmer", "Mong": "Mongolian",
    "Tglg": "Baybayin", "Bugi": "Lontara", "Bali": "Balinese", "Sund": "Sundanese", "Java": "Javanese",
    "Olck": "Ol Chiki", "Mtei": "Meitei Mayek", "Vaii": "Vai", "Tfng": "Tifinagh", "Adlm": "Adlam",
    "Yiii": "Yi", "Bamu": "Bamum", "Osge": "Osage", "Hmnp": "Nyiakeng Puachue Hmong", "Lisu": "Lisu",
    "Cakm": "Chakma", "Lana": "Tai Tham", "Tavt": "Tai Viet", "Sylo": "Syloti Nagri", "Limb": "Limbu",
    "Copt": "Coptic", "Osma": "Osmanya", "Rohg": "Hanifi Rohingya", "Hang": "Hangul", "Hani": "Han",
    "Jpan": "Japanese", "Kana": "Katakana",
}
for _sc, _nm in SCRIPT_NAMES.items():
    NOTO.setdefault(_sc, ["Noto Sans " + _nm.replace("'", "").replace(" syllabics", " Canadian Aboriginal")
                          if _sc != "Cans" else "Noto Sans Canadian Aboriginal"])


def _generated(name: str) -> str:
    """Letter sets too big to type: '@ko' (KS X 1001 Hangul), '@zh-Hans'
    (GB 2312 level 1), '@zh-Hant' (Big5 frequent), '@ja' (JIS level 1 kanji +
    kana), '@iu' / '@cr' / '@oj' (Canadian syllabics by Unicode name)."""
    import unicodedata

    def rows(codec, hi, lo=(0xA1, 0xFE)):
        out = []
        for a in range(hi[0], hi[1] + 1):
            for b in range(lo[0], lo[1] + 1):
                try:
                    out.append(bytes((a, b)).decode(codec))
                except UnicodeDecodeError:
                    pass
        return "".join(out)
    if name == "ko":
        return rows("euc_kr", (0xB0, 0xC8))
    if name == "zh-Hans":
        return rows("gb2312", (0xB0, 0xD7))
    if name == "zh-Hant":
        return rows("big5", (0xA4, 0xC6), (0x40, 0xFE))
    if name == "ja":
        kana = "".join(chr(c) for c in range(0x3041, 0x3097)) + "".join(chr(c) for c in range(0x30A1, 0x30FB))
        return kana + rows("euc_jp", (0xB0, 0xCF))
    sets = getattr(_txlangs(), "CANS_SETS", {}).get(name)
    if sets:
        out = []
        for c in sets["consonants"]:
            for v in sets["vowels"]:
                for nm in (f"CANADIAN SYLLABICS {c}{v}",):
                    try:
                        out.append(unicodedata.lookup(nm))
                    except KeyError:
                        pass
        for f in sets["finals"]:
            for nm in (f"CANADIAN SYLLABICS FINAL {f}", f"CANADIAN SYLLABICS {f}"):
                try:
                    out.append(unicodedata.lookup(nm))
                    break
                except KeyError:
                    pass
        return "".join(dict.fromkeys(out))
    return ""


def _txlangs():
    try:
        import txlangs
        return txlangs
    except ImportError:
        return None


def _core(script: str) -> str:
    import unicodedata
    core = getattr(_txlangs(), "SCRIPT_CORE", {}).get(script, "")
    if isinstance(core, str):
        return _generated(core[1:]) if core.startswith("@") else core
    out = []
    for lo, hi in core:
        for cp in range(lo, hi + 1):
            ch = chr(cp)
            if unicodedata.category(ch)[0] in "LM":
                out.append(ch)
    return "".join(out)


def _letters(script: str, field: str) -> str:
    """The registry's letters field -> the alphabet (see txlangs' docstring)."""
    field = field.strip()
    base = ""
    extra = []
    for tok in field.split(" ") if field else []:
        if tok.startswith("@"):
            base += _generated(tok[1:])
        elif tok.startswith("="):
            base += tok[1:]
        else:
            extra.append(tok)
    explicit = base != ""
    extra_s = "".join(extra)
    if script == "Latn":
        ups = "".join(c.upper() for c in extra_s if len(c.upper()) == 1 and c.upper() != c)
        return "".join(dict.fromkeys(extra_s + ups))
    if not explicit:
        base = _core(script)
    letters = base + extra_s
    ups = "".join(c.upper() for c in letters if len(c.upper()) == 1 and c.upper() != c)
    return "".join(dict.fromkeys(letters + ups))


def _registry_targets(have: Dict[str, dict]) -> List[dict]:
    L = _txlangs()
    if L is None:
        return []
    samples = getattr(L, "SAMPLES", {})
    vertical = getattr(L, "VERTICAL", {})
    out = []
    for line in L.DATA.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        f = (line.split("|", 6) + [""] * 7)[:7]
        code, name, endo, scripts, letters, regions, note = (x.strip() for x in f)
        sc = (scripts.split() or ["Latn"])[0]
        extra = {"regions": regions.split(), "scripts": scripts.split()}
        notes = " ".join(x for x in (note, vertical.get(sc, "")) if x)
        if notes:
            extra["notes"] = notes
        if code.lower() in have:
            have[code.lower()].update(extra)
            continue
        alphabet = _letters(sc, letters)
        if sc == "Latn":
            # letters of the language's own name belong to its alphabet too (Kɪsukuma)
            own = "".join(c for c in endo if c.isalpha() and ord(c) > 0x7F)
            alphabet = "".join(dict.fromkeys(alphabet + own + "".join(
                c.upper() for c in own if len(c.upper()) == 1)))
        given = "japanese" if sc == "Jpan" else None
        kind, lacking = _kind_of(sc, alphabet, given)
        sample = samples.get(code, "")
        if not sample:
            # no sample sentence on file: the language's own name and some of its letters
            show = "".join(c for c in alphabet if c.islower() or not c.isupper())[:24]
            sample = (endo + " · KOS-MOS · " + show).strip(" ·")
        out.append(dict({
            "code": code, "name": name, "endonym": endo, "kind": kind, "script": sc,
            "rtl": sc in RTL_SCRIPTS, "alphabet": alphabet, "lacking": lacking, "sample": sample,
            "fonts": list(NOTO.get(sc, ["Noto Sans"])),
            "render": dict(RENDER.get(sc, _DEFAULT_RENDER)), "registry": True,
        }, **extra))
    return out


def _all_targets() -> List[dict]:
    base = _make_targets()
    other = base.pop()                       # "other" stays last
    have = {t["code"].lower(): t for t in base}
    for t in base:
        t.setdefault("script_name", SCRIPT_NAMES.get(t["script"], t["script"]))
    reg = _registry_targets(have)
    for t in reg:
        t["script_name"] = SCRIPT_NAMES.get(t["script"], t["script"])
    reg.sort(key=lambda t: t["name"].lower())
    return base + reg + [other]


TARGETS: List[dict] = _all_targets()
_BY_CODE = {t["code"].lower(): t for t in TARGETS}


def languages(query: str = "") -> List[dict]:
    """Every target language (the 47 tested profiles first, then the registry
    A-Z), filtered by code, English name, own name, script or region."""
    q = (query or "").strip().lower()
    out = []
    for t in TARGETS:
        if t["code"] == "other":
            continue
        hay = " ".join([t["code"], t["name"], t.get("endonym", ""), t.get("script_name", ""),
                        " ".join(t.get("regions", []))]).lower()
        if not q or q in hay or t["code"].lower() == q:
            out.append(t)
    return out


def profile(code: str) -> Optional[dict]:
    """The TARGETS entry for a language code (case-insensitive; "pt_BR" and
    "pt-br" both find pt-BR; a region falls back to the base language)."""
    if not code:
        return None
    c = code.replace("_", "-").lower()
    return _BY_CODE.get(c) or _BY_CODE.get(c.split("-", 1)[0])


# ---------------------------------------------------------------------------
# project target
# ---------------------------------------------------------------------------
_TARGET_KEYS = {
    "code": str, "kind": str, "script": str, "language": str, "rtl": bool, "font": str,
    "face": int, "px": float, "baseline": int, "xscale": float, "align": str, "wrap": int,
    "battle_font": str, "battle_face": int, "battle_px": float, "battle_baseline": int,
    "battle_xscale": float,
}


def normalise_target(t: dict) -> dict:
    """Validate ``project["target"]``; returns a clean copy with only known keys:

    code      language code ("ru", "ar", "ja", "pl", "other", ...)
    kind      builtin | script-hybrid | japanese | script (default: the profile's)
    script    ISO 15924 code ("Cyrl"); required for kind script with code "other"
    language  BCP-47 tag for shaping (default: code)
    rtl       right-to-left paragraphs (default: the profile's)
    font      path of the TrueType/OpenType font new glyphs are drawn from
    face      face index inside a .ttc (default 0)
    px, baseline, xscale   glyph size, baseline row, horizontal squeeze
              (defaults: RENDER[script])
    align     "left" | "right" (right needs a wrap width; RTL dialogue)
    wrap      px to wrap translated lines at (0 = leave wrapping to the game;
              default: 0 for left-to-right, the row's guide / 370 px for RTL)
    battle_font, battle_face, battle_px, battle_baseline, battle_xscale
              the same for the battle HUD grid (default: font/face, 13 px)
    """
    if not isinstance(t, dict):
        raise TargetError("target must be an object")
    out: dict = {}
    for k, typ in _TARGET_KEYS.items():
        if k not in t or t[k] is None or t[k] == "":
            continue
        v = t[k]
        try:
            if typ is bool:
                if not isinstance(v, bool):
                    raise ValueError
            elif typ is int:
                if isinstance(v, bool) or int(v) != float(v):
                    raise ValueError
                v = int(v)
            elif typ is float:
                if isinstance(v, bool):
                    raise ValueError
                v = float(v)
            else:
                v = str(v)
        except (TypeError, ValueError):
            raise TargetError(f"{k}: expected {typ.__name__}, got {v!r}") from None
        out[k] = v
    if "kind" in out and out["kind"] not in KINDS:
        raise TargetError(f"kind must be one of {', '.join(KINDS)}, not {out['kind']!r}")
    if "align" in out and out["align"] not in ("left", "right"):
        raise TargetError("align must be 'left' or 'right'")
    for k in ("face", "battle_face", "wrap"):
        if out.get(k, 0) < 0:
            raise TargetError(f"{k} must not be negative")
    for k in ("px", "battle_px"):
        if k in out and not (6.0 <= out[k] <= 40.0):
            raise TargetError(f"{k} must be between 6 and 40")
    for k in ("xscale", "battle_xscale"):
        if k in out and not (0.3 <= out[k] <= 1.5):
            raise TargetError(f"{k} must be between 0.3 and 1.5")
    if "baseline" in out and not (8 <= out["baseline"] <= 23):
        raise TargetError("baseline must be a cell row 8..23")
    if "battle_baseline" in out and not (6 <= out["battle_baseline"] <= 17):
        raise TargetError("battle_baseline must be a cell row 6..17")
    return out


def resolve_target(project: dict) -> dict:
    """The full target a build uses: the profile of ``target.code`` (or of
    ``project["language"]``) with the project's own settings on top. An
    unknown language code with no target is "builtin" (the editor's
    original behaviour: the codec decides what can be written)."""
    given = normalise_target(project.get("target") or {})
    code = given.get("code") or project.get("language") or ""
    prof = profile(code) or {}
    script = given.get("script") or prof.get("script") or ""
    kind = given.get("kind") or prof.get("kind") or ("script" if script and script != "Latn"
                                                     else "builtin")
    render = dict(RENDER.get(script, _DEFAULT_RENDER))
    t = {"code": code, "kind": kind, "script": script,
         "language": given.get("language") or (code if code and code != "other" else ""),
         "rtl": given.get("rtl", prof.get("rtl", script in RTL_SCRIPTS)),
         "font": given.get("font", ""), "face": given.get("face", 0),
         "px": given.get("px", render["px"]), "baseline": given.get("baseline", render["baseline"]),
         "xscale": given.get("xscale", render["xscale"]),
         "align": given.get("align", "left"), "wrap": given.get("wrap"),
         "name": prof.get("name") or code or "?"}
    t["battle_font"] = given.get("battle_font", t["font"])
    t["battle_face"] = given.get("battle_face", t["face"] if "battle_font" not in given else 0)
    for k, v in BATTLE_RENDER.items():
        t[k] = given.get(k, v)
    return t


# ---------------------------------------------------------------------------
# what the retail fonts draw
# ---------------------------------------------------------------------------

def _jis_row(ch: str) -> int:
    try:
        b = ch.encode("euc_jp")
    except UnicodeEncodeError:
        return 0
    return b[0] if len(b) == 2 else 0


def native_predicate(kind: str, font: str = tc.FIELD, face: str = tc.FONT1) -> Callable[[str], bool]:
    """``ch -> bool``: does the build leave ``ch`` to the retail font?

    builtin / japanese: everything ``txcodec.draws_natively`` accepts.
    script / script-hybrid: the same minus JIS rows A6/A7 (Greek, Cyrillic:
    the retail font has them as fixed 20 px full-width kanji-path cells, so
    a Greek or Russian translation gets proportional glyphs drawn instead)."""
    def native(ch: str) -> bool:
        if not tc.draws_natively(ch, font, face):
            return False
        if kind in ("script", "script-hybrid") and font == tc.FIELD and ord(ch) >= 0x80 \
                and ch not in tc.FACE_MAPS[face] and _jis_row(ch) in (0xA6, 0xA7):
            return False
        return True
    return native


def coverage(texts, target, font: str = tc.FIELD, face: str = tc.FONT1,
             ttf: Optional[str] = None, ttf_face: int = 0) -> dict:
    """Which characters of ``texts`` (a string or an iterable of strings) the
    retail font draws, and which a build must draw from a TrueType font.

    ``target`` is a profile, a resolved target or just a kind string.
    -> {"native": str, "glyph": str, "folded": str, "impossible": str,
        "missing_in_font": str|None, "counts": {ch: n}}
    native     drawn by the retail font as is
    glyph      need a new glyph (kinds other than builtin; field and battle font)
    folded     builtin battle/tag text: written as the nearest ASCII (warning)
    impossible no build can draw them here (builtin kind; the tag font;
               raw control characters)
    missing_in_font   glyph characters ``ttf`` has no glyph for (None
               without a ttf); needs fonttools
    Protected tokens (⟦XX⟧, /[..] tags) are not counted."""
    kind = target if isinstance(target, str) else (target or {}).get("kind", "builtin")
    if isinstance(texts, str):
        texts = [texts]
    native = native_predicate(kind, font, face)
    counts: Counter = Counter()
    for t in texts:
        t = unicodedata.normalize("NFC", tc._TOKRUN_RE.sub("", tc._TAG_RE.sub("", t or "")))
        counts.update(c for c in t if c != "\n")
    out = {"native": [], "glyph": [], "folded": [], "impossible": []}
    for ch in sorted(counts):
        if native(ch):
            out["native"].append(ch)
        elif ord(ch) < 0x20 or 0x7F <= ord(ch) < 0xA0 or tc.glyph_code(ch) is not None:
            out["impossible"].append(ch)
        elif font == tc.ASCII:
            f = tc.fold_ascii(ch)
            ok = f is not None and all(c in tc.TAG_GLYPHS for c in f)
            out["folded" if ok else "impossible"].append(ch)
        elif kind == "builtin":
            if font == tc.BATTLE and tc.fold_ascii(ch) is not None:
                out["folded"].append(ch)
            else:
                out["impossible"].append(ch)
        elif unicodedata.category(ch) in ("Zl", "Zp", "Cf", "Cc", "Co", "Cs", "Cn"):
            out["impossible"].append(ch)
        else:
            out["glyph"].append(ch)
    res = {k: "".join(v) for k, v in out.items()}
    res["counts"] = dict(counts)
    res["missing_in_font"] = None
    if ttf and res["glyph"]:
        cmap = font_cmap(ttf, ttf_face)
        if cmap is not None:
            res["missing_in_font"] = "".join(
                c for c in res["glyph"] if ord(c) not in cmap
                and unicodedata.category(c) not in ("Mn", "Me"))
    return res


# ---------------------------------------------------------------------------
# fonts on this machine
# ---------------------------------------------------------------------------

def shaping_available() -> Tuple[bool, str]:
    """(True, versions) when fonttools and uharfbuzz import, else (False, how to fix)."""
    try:
        import fontTools
        import uharfbuzz
    except ImportError as e:
        return False, (f"the kit's font packages are missing ({e.name}): new scripts are drawn "
                       f"from TrueType fonts with fonttools and uharfbuzz, which the kit "
                       f"requires (requirements.txt). The launcher installs them; or run "
                       f"python3 -m pip install --user -r requirements.txt")
    return True, f"fonttools {fontTools.version}, uharfbuzz {uharfbuzz.__version__}"


def font_dirs() -> List[Path]:
    """System and user font folders of this platform (existing ones)."""
    home = Path.home()
    if sys.platform == "darwin":
        c = [Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"),
             Path("/Library/Fonts"), home / "Library/Fonts"]
    elif sys.platform == "win32":
        win = Path(os.environ.get("WINDIR", r"C:\Windows"))
        c = [win / "Fonts"]
        if os.environ.get("LOCALAPPDATA"):
            c.append(Path(os.environ["LOCALAPPDATA"]) / "Microsoft/Windows/Fonts")
    else:
        c = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"),
             home / ".fonts", home / ".local/share/fonts"]
    return [d for d in c if d.is_dir()]


_FONT_EXT = (".ttf", ".otf", ".ttc", ".otc")
# fonts whose cmap claims everything but whose glyphs are placeholders
_FONT_SKIP = re.compile(r"lastresort|braille|emoji", re.I)


def _quiet_fonttools() -> None:
    import logging
    logging.getLogger("fontTools").setLevel(logging.ERROR)


def _open_faces(path: str):
    from fontTools.ttLib import TTCollection, TTFont
    _quiet_fonttools()
    if path.lower().endswith((".ttc", ".otc")):
        return list(TTCollection(path, lazy=True).fonts)
    return [TTFont(path, lazy=True)]


def font_cmap(path: str, face: int = 0) -> Optional[Set[int]]:
    """Code points ``path`` (face ``face``) maps, or None without fonttools."""
    try:
        faces = _open_faces(str(path))
    except ImportError:
        return None
    except Exception as e:
        raise TargetError(f"cannot read the font {path}: {e}") from None
    if face >= len(faces):
        raise TargetError(f"{path} has {len(faces)} face(s); face {face} does not exist")
    return set((faces[face].getBestCmap() or {}).keys())


def _name(ft, nid: int) -> str:
    try:
        n = ft["name"].getDebugName(nid)
        return n or ""
    except Exception:
        return ""


_FONT_INDEX: Optional[List[dict]] = None


def _font_index() -> List[dict]:
    """Every outline font face installed here, with the scripts it covers
    (cached in ~/.cache/xs1-txeditor/fonts.json by path, size and mtime)."""
    global _FONT_INDEX
    if _FONT_INDEX is not None:
        return _FONT_INDEX
    cache_path = CACHE_DIR / "fonts-v1.json"
    try:
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cache = {}
    files = []
    for d in font_dirs():
        for root, _dirs, names in os.walk(d):
            for n in sorted(names):
                if n.lower().endswith(_FONT_EXT) and not _FONT_SKIP.search(n):
                    files.append(os.path.join(root, n))
    out: List[dict] = []
    fresh = {}
    for p in sorted(set(files)):
        try:
            st = os.stat(p)
        except OSError:
            continue
        stamp = f"{st.st_size}:{int(st.st_mtime)}"
        hit = cache.get(p)
        if hit and hit.get("stamp") == stamp:
            faces = hit["faces"]
        else:
            faces = []
            try:
                for i, ft in enumerate(_open_faces(p)):
                    if not any(t in ft for t in ("glyf", "CFF ", "CFF2")):
                        continue
                    cmap = ft.getBestCmap() or {}
                    scripts = [s for s, probe in _PROBE.items()
                               if all(ord(c) in cmap for c in probe)]
                    faces.append({"face": i, "family": _name(ft, 1), "style": _name(ft, 2),
                                  "scripts": scripts})
            except ImportError:
                return []
            except Exception:
                faces = []
        fresh[p] = {"stamp": stamp, "faces": faces}
        for f in faces:
            out.append({"path": p, **f, "noto": "noto" in (f["family"] + p).lower()})
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        if fresh != cache:
            cache_path.write_text(json.dumps(fresh, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    _FONT_INDEX = out
    return out


def system_fonts(script: Optional[str] = None, chars: Optional[str] = None) -> List[dict]:
    """Installed outline fonts: [{path, face, family, style, noto, scripts}],
    Noto first, then by family. ``script`` keeps faces covering that script's
    probe letters (``_PROBE``); ``chars`` keeps faces that map every one of
    those characters (opens the candidate fonts again). Empty without
    fonttools."""
    fonts = _font_index()
    if script:
        fonts = [f for f in fonts if script in f["scripts"]]
    if chars:
        need = {ord(c) for c in chars if unicodedata.category(c) not in ("Mn", "Me")}
        keep = []
        for f in fonts:
            try:
                cmap = font_cmap(f["path"], f["face"])
            except TargetError:
                continue
            if cmap is not None and need <= cmap:
                keep.append(f)
        fonts = keep
    # system-internal faces (".SF ...") and calligraphic Nastaliq are poor
    # first choices for 20 px cells: list them last
    return sorted(fonts, key=lambda f: (f["family"].startswith(".") or "nastaliq" in f["family"].lower(),
                                        not f["noto"], f["style"].lower() not in ("regular", "w3"),
                                        f["family"].lower(), f["style"].lower(),
                                        f["path"], f["face"]))


def suggest_fonts(target: dict, chars: Optional[str] = None) -> dict:
    """{"noto": [family names to download], "installed": system_fonts(...)[:12]}."""
    script = target.get("script") or ""
    return {"noto": list(NOTO.get(script, ["Noto Sans"])),
            "installed": system_fonts(script or None, chars)[:12]}


# ---------------------------------------------------------------------------
# retail usage: cells no new glyph may take
# ---------------------------------------------------------------------------

class Usage:
    """Cells retail text draws. prop: proportional slots; wide: kanji-area
    cells (752..3743); battle_cells / battle_bytes: battle-grid cells and
    table bytes the retail battle strings use."""

    def __init__(self):
        self.prop: Set[int] = set()
        self.wide: Set[int] = set()
        self.battle_cells: Set[int] = set()
        self.battle_bytes: Set[int] = set()
        self.rows = 0


def _field_cells(data: bytes, usage: Usage) -> None:
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b < 0x20:
            i += max(1, fontmod.control_length(data, i))
            continue
        if b == 0x23 and i + 2 < n:                  # engine escape #xx / ##
            if data[i + 1] == 0x23:
                code = fontmod.ascii_to_code(0x23)
                i += 2
            else:
                xx = 0
                for c in data[i + 1:i + 3]:
                    xx = xx * 16 + (int(chr(c), 16) if chr(c) in "0123456789abcdefABCDEF" else 0)
                code = tc.block_code(xx)
                i += 3
        elif b < 0x80:
            code = fontmod.ascii_to_code(b) if b < 0x7F else fontmod.ascii_to_code(0x20)
            i += 1
        else:
            if i + 1 >= n:
                break
            code = (b << 8) | data[i + 1]
            i += 2
        p = fontmod.prop_index(code)
        if p >= 0:
            usage.prop.add(p)
        else:
            c = fontmod.cell_of_code(code)
            if 752 <= c <= 3743:
                usage.wide.add(c)


def _battle_cells(data: bytes, cells: Set[int], raw_bytes: Set[int]) -> None:
    import statfont
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0x23:
            m = re.match(rb"[0-9A-Fa-f]{1,2}", data[i + 1:i + 3])
            if m:
                cells.add(int(m.group(0), 16))
            i += 3
            continue
        if b & 0x80:
            i += 2
            continue
        raw_bytes.add(b)
        k = statfont.TABLE.find(chr(b))
        if k >= 0:
            cells.add(k)
        i += 1


_USAGE: Dict[str, Usage] = {}


def retail_usage(retail: str, log: Callable[[str], None] = lambda m: None) -> Usage:
    """Measure what the retail text the game shows draws: every row of the
    four families that is not hidden (locked rows too; evt ``/[...]`` tags
    are control language and are not measured). Hidden rows are debug prints,
    Java code, Japanese developer pages (one is a font test page that walks
    the whole block) and binary that only looks like text: never drawn, so
    their cells are free for a translation."""
    key = str(Path(retail).resolve())
    if key in _USAGE:
        return _USAGE[key]
    import importlib
    t = time.time()
    u = Usage()
    for mod in ("txfam_umn", "txfam_evt", "txfam_ovl", "txfam_fix"):
        m = importlib.import_module(mod)
        for r in m.rows(retail):
            if r.get("hide"):
                continue
            u.rows += 1
            font = r.get("font", tc.FIELD)
            face = r.get("face") or r.get("measure")
            face = face if face in (tc.FONT0, tc.FONT1) else tc.FONT1
            try:
                data, _ = tc.from_display(r["src"], font, r.get("ctl", "c0"), face)
            except tc.TxEncodeError:
                continue
            if font == tc.FIELD:
                if r.get("ctl") == "evt":
                    data = tc._TAG_RAW_RE.sub(b"", data)
                _field_cells(data, u)
            elif font == tc.BATTLE:
                _battle_cells(data, u.battle_cells, u.battle_bytes)
    log(f"retail usage: {u.rows} rows, {len(u.prop)} proportional slots, "
        f"{len(u.wide)} kanji cells, {len(u.battle_cells)} battle cells "
        f"({time.time() - t:.1f} s)")
    _USAGE[key] = u
    return u


# ---------------------------------------------------------------------------
# protected tokens -> control placeholders
# ---------------------------------------------------------------------------
_PROTECT = {
    "c0": re.compile(r"%[-+0#]*\d*(?:\.\d+)?[lh]?[diouxXcsfeg]|@."),
    "evt": re.compile(r"/\[[^\]\n]*\]|%[-+0#]*\d*[ds]"),
    "chat": re.compile(r"\\[0-7]{1,3}|\\."),
}
_PH_RE = re.compile("\x0e([\x10-\x1f]+)\x0f")
_CTRL_RUN = r"(?:⟦[0-9A-Fa-f]{2}⟧|[\x0e-\x1f])*"
_EDGE_RE = re.compile(r"^(" + _CTRL_RUN + r")(.*?)(" + _CTRL_RUN + r")$", re.S)


def _placeholder(i: int) -> str:
    digits = []
    while True:
        digits.append(chr(0x10 + (i & 0xF)))
        i >>= 4
        if not i:
            break
    return "\x0e" + "".join(reversed(digits)) + "\x0f"


def _protect(text: str, ctl: str) -> Tuple[str, List[str]]:
    saved: List[str] = []

    def sub(m):
        saved.append(m.group(0))
        return _placeholder(len(saved) - 1)
    return _PROTECT.get(ctl, _PROTECT["c0"]).sub(sub, text), saved


def _unprotect(text: str, saved: List[str]) -> str:
    def sub(m):
        i = 0
        for c in m.group(1):
            i = i * 16 + (ord(c) - 0x10)
        return saved[i]
    return _PH_RE.sub(sub, text)


# ---------------------------------------------------------------------------
# the glyph plan
# ---------------------------------------------------------------------------

def _face_of(row: dict) -> str:
    f = row.get("face") or row.get("measure")
    return f if f in (tc.FONT0, tc.FONT1) else tc.FONT1


def _field_rows(rows: Dict[str, dict], edits: Dict[str, str]):
    return [(rid, rows[rid], edits[rid]) for rid in sorted(edits)
            if rid in rows and rows[rid].get("font", tc.FIELD) == tc.FIELD]


def needs_glyphs(target: dict, rows: Dict[str, dict], edits: Dict[str, str]) -> bool:
    """Does any translation need a glyph the retail fonts lack?"""
    kind = target.get("kind", "builtin")
    if kind == "builtin":
        return False
    for rid, text in edits.items():
        row = rows.get(rid)
        if row is None or row.get("font", tc.FIELD) == tc.ASCII:
            continue
        cov = coverage(text, kind, row.get("font", tc.FIELD), _face_of(row))
        if cov["glyph"]:
            return True
    return False


class GlyphPlan:
    """What a new-script build writes besides the strings.

    edits      {id: display text}: the project's text with new letters as
               txcodec.glyph_char codes (feed to the families inside
               ``txcodec.glyph_codes()``); rows that need no glyph unchanged
    glyphset   glyphs.GlyphSet of the field fonts (None if no field glyphs)
    stat_xtx   new yamamoto\\stat\\stat.xtx bytes, or None
    ovl_raw    {("OV01.OVL", 0xA46AF8): new battle byte table} or {}
    report     counts for the log / summary
    """

    def __init__(self):
        self.edits: Dict[str, str] = {}
        self.glyphset = None
        self.stat_xtx: Optional[bytes] = None
        self.ovl_raw: Dict[Tuple[str, int], bytes] = {}
        self.report: Dict[str, object] = {}

    def apply_font(self, font, face: str):
        """Copy of ``font`` (font0 or font1, after the accent fixes) with the
        new glyphs drawn (both faces get the same slots and cells)."""
        f = tc._copy_font(font)
        if self.glyphset is not None:
            self.glyphset.apply(f)
        return f


_ENC_CLASS = []


def _GlyphEncoder(*a, **kw):
    """A langpack.Encoder whose new letters come out as txcodec glyph chars."""
    if not _ENC_CLASS:
        import langpack

        class GlyphEncoder(langpack.Encoder):
            def _code_text(self, key: str) -> str:
                s = self.gs.slots.get(key)
                if s is None:
                    raise TargetError(f"internal: no cell for glyph {key!r}")
                return tc.glyph_char(s["code"])
        _ENC_CLASS.append(GlyphEncoder)
    return _ENC_CLASS[0](*a, **kw)


_STATE_TOKEN = re.compile(r"^(?:/\[|\\)")      # evt tags, chat escapes
_HEXCTL_RE = re.compile(r"⟦[0-9A-Fa-f]{2}⟧")


def _rtl_mid_controls(mid: str, saved: List[str]) -> List[str]:
    """Tokens inside a right-to-left line (not at its edges) that change the
    window's drawing state or timing: evt ``/[...]`` tags, chat ``\\NNN``
    escapes, ``⟦XX⟧`` control codes. printf fields (``%d``) and ``@x`` are
    substituted text and are allowed: they move with their word."""
    bad = []
    for m in _PH_RE.finditer(mid):
        i = 0
        for c in m.group(1):
            i = i * 16 + (ord(c) - 0x10)
        if _STATE_TOKEN.match(saved[i]):
            bad.append(saved[i])
    bad += _HEXCTL_RE.findall(mid)
    return bad


def _encode_with(enc, text: str, ctl: str, rtl: bool) -> str:
    """One logical string -> encoded display text (tokens kept in place)."""
    text = unicodedata.normalize("NFC", text)
    body, saved = _protect(text, ctl)
    out = []
    for para in body.split("\n"):
        if rtl:
            m = _EDGE_RE.match(para)
            pre, mid, post = m.group(1), m.group(2), m.group(3)
            bad = _rtl_mid_controls(mid, saved)
            if bad:
                raise TargetError(
                    f"right-to-left text cannot carry {', '.join(repr(b) for b in bad[:4])} in "
                    f"the middle of a line: the line is reordered word by word for display, so "
                    f"a colour or control code there would end up on other words. Put it at "
                    f"the start or end of the line (a coloured phrase on its own line), or "
                    f"leave it out")
            out.append(pre + enc.string(mid) + post)
        else:
            out.append(enc.string(para))
    return _unprotect("\n".join(out), saved)


def _check_font(path: str, face: int, chars: str, what: str, target: dict) -> None:
    if not path:
        sug = suggest_fonts(target, chars)
        names = [f"{f['path']}" + (f" (face {f['face']})" if f["face"] else "")
                 for f in sug["installed"][:5]]
        raise TargetError(
            f"{target.get('name')} needs new glyphs for {what} ({chars[:40]}), drawn from a "
            f"TrueType/OpenType font, and the project names none (target.font). "
            + (f"Fonts on this machine that cover it: {'; '.join(names)}. " if names else "")
            + f"Free fonts: {', '.join(sug['noto'])} (fonts.google.com/noto).")
    if not Path(path).is_file():
        raise TargetError(f"the target font {path} does not exist")
    cmap = font_cmap(path, face)
    if cmap is not None:
        miss = [c for c in chars if ord(c) not in cmap
                and unicodedata.category(c) not in ("Mn", "Me")]
        if miss:
            raise TargetError(f"the font {Path(path).name} has no glyph for "
                              f"{''.join(miss[:40])} ({', '.join(tc.char_name(c) for c in miss[:3])}"
                              f"{', ...' if len(miss) > 3 else ''}); pick a font that covers "
                              f"{target.get('name')}")


def _field_codes(ch: str, face: str) -> List[int]:
    try:
        b, _ = tc.from_display(ch, tc.FIELD, "c0", face)
    except tc.TxEncodeError:
        return []
    if len(b) == 1 and 0x20 <= b[0] < 0x7F:
        return [fontmod.ascii_to_code(b[0])]
    return [(b[0] << 8) | b[1]] if len(b) == 2 else []


def _plan_field(plan: GlyphPlan, target: dict, frows, usage: Usage, fonts, log,
                keep=()) -> None:
    """``frows``: edited field rows that need a glyph; ``keep``: the other
    edited field rows (no new glyph). The cells both draw natively are
    reserved, as are the cells retail text draws (``usage``)."""
    import langpack
    from glyphs import GlyphSet, TTFRenderer
    kind, rtl = target["kind"], bool(target["rtl"])
    renderer = TTFRenderer(target["font"], target["px"], target["baseline"], target["face"],
                           xscale=target["xscale"])
    shaper = langpack.HBShaper(renderer, target["script"] or None, target["language"] or None)
    gs = GlyphSet(f"{target['font']}#{target['face']}")
    natives = {face: native_predicate(kind, tc.FIELD, face) for face in (tc.FONT0, tc.FONT1)}

    def nwidth(face):
        f = fonts[face]
        return lambda run: f.width_of_bytes(tc.from_display(run, tc.FIELD, "c0", face)[0])

    counts: Counter = Counter()
    used_native: Set[Tuple[str, str]] = set()
    row_errors: List[str] = []
    for face in (tc.FONT0, tc.FONT1):
        col = _GlyphEncoder(gs, shaper, fonts[face], rtl, collect=True,
                            native=natives[face], native_width=nwidth(face))
        for rid, row, text in frows:
            if _face_of(row) != face:
                continue
            try:
                _encode_with(col, text, row.get("ctl", "c0"), rtl)
            except TargetError as e:
                row_errors.append(f"{rid}: {e}")
                continue
            for c in unicodedata.normalize("NFC", text):
                if ord(c) >= 0x80 and natives[face](c):
                    used_native.add((c, face))
        counts.update(col.counts)
    if row_errors:
        raise TargetError(f"{len(row_errors)} translation(s) cannot be encoded; nothing was "
                          f"written:\n  " + "\n  ".join(row_errors[:100]))
    # native letters of edited rows that need no glyph: their cells are in
    # use too (a Japanese row of level-1 kanji next to rows with level-2
    # ones; Polish "Góra" next to rows with ł)
    keep_usage = Usage()
    for rid, row, text in keep:
        face = _face_of(row)
        for c in unicodedata.normalize("NFC", text):
            if ord(c) >= 0x80 and natives[face](c):
                used_native.add((c, face))
        try:
            data, _ = tc.from_display(text, tc.FIELD, row.get("ctl", "c0"), face)
        except tc.TxEncodeError:
            continue
        if row.get("ctl") == "evt":
            data = tc._TAG_RAW_RE.sub(b"", data)
        _field_cells(data, keep_usage)
    if getattr(shaper, "notdef", None):
        raise TargetError(f"the font {Path(target['font']).name} has no glyph for some "
                          f"letters of: {sorted(shaper.notdef)[:6]}")
    order = sorted(counts, key=lambda k: (-counts[k], k))
    t = time.time()
    for k in order:
        gs.add(shaper.glyph(k))
    # cells still in use: retail text, and the native letters of this translation
    res_prop = set(usage.prop) | keep_usage.prop
    res_wide = set(usage.wide) | keep_usage.wide
    for c, face in used_native:
        for code in _field_codes(c, tc.FONT0) + _field_codes(c, tc.FONT1):
            p = fontmod.prop_index(code)
            if p >= 0:
                res_prop.add(p)
            elif (code >> 8) < 0xD0:
                res_wide.add(fontmod.cell_of_code(code))
    from glyphs import GlyphError
    try:
        gs.allocate(order, keep_latin=True, wide_only=(kind == "japanese"),
                    reserved_prop=res_prop, reserved_wide=res_wide,
                    wide_from_end=(kind == "japanese"))
    except GlyphError as e:
        raise TargetError(f"the fonts have no room left: {e} ({len(order)} glyphs needed)")
    tiers = Counter(v["tier"] for v in gs.slots.values())
    plan.glyphset = gs
    plan.report.update({"field_glyphs": len(order), "prop_slots": tiers.get("prop", 0),
                        "wide_cells": tiers.get("wide", 0),
                        "reserved_prop": len(res_prop), "reserved_wide": len(res_wide)})
    log(f"field glyphs: {len(order)} distinct ({sum(counts.values())} uses) -> "
        f"{tiers.get('prop', 0)} proportional slots, {tiers.get('wide', 0)} kanji cells "
        f"(kept {len(res_prop)} slots and {len(res_wide)} kanji cells in use; "
        f"rendered in {time.time() - t:.1f} s)")
    # encode every field row with the patched faces' metrics
    for face in (tc.FONT0, tc.FONT1):
        pf = plan.apply_font(fonts[face], face)
        for rid, row, text in frows:
            if _face_of(row) != face:
                continue
            ctl = row.get("ctl", "c0")
            width = target.get("wrap")
            if width is None:
                width = (row.get("px") or (DIALOGUE_WRAP if ctl == "evt" else 0)) if rtl else 0
            enc = _GlyphEncoder(gs, shaper, pf, rtl, width=int(width or 0),
                                align=target.get("align", "left"),
                                native=natives[face], native_width=nwidth(face))
            plan.edits[rid] = _encode_with(enc, text, ctl, rtl)


def _battle_runs(text: str, native) -> List[Tuple[bool, str]]:
    out: List[Tuple[bool, str]] = []
    for c in text:
        g = not native(c) and not c.isspace()
        if out and out[-1][0] == g:
            out[-1] = (g, out[-1][1] + c)
        else:
            out.append((g, c))
    return out


def _plan_battle(plan: GlyphPlan, target: dict, brows, usage: Usage, retail: str, log,
                 keep=()) -> None:
    """``brows``: edited battle rows that need a glyph; ``keep``: the other
    edited battle rows. Grid cells and table bytes either draws natively
    (``#XX`` escapes for accents, plain table bytes) are left alone."""
    import statfont
    native = native_predicate(target["kind"], tc.BATTLE)
    rtl = bool(target["rtl"])
    cr = statfont.CellRenderer(target["battle_font"], px=target["battle_px"],
                               baseline=target["battle_baseline"],
                               face_index=target["battle_face"],
                               script=target["script"] or None,
                               language=target["language"] or None, rtl=rtl,
                               xscale=target["battle_xscale"])
    parts: Dict[str, list] = {}
    counts: Counter = Counter()
    shaped: Dict[str, List] = {}          # id -> [(is_glyph, str | [keys])] per word

    def clusters(run: str) -> List[str]:
        # one cell per HarfBuzz cluster, marks placed from the cluster's base
        # (statfont.cell_clusters; RTL runs list marks before their base)
        out = []
        for gids, cl, _adv in statfont.cell_clusters(cr.shape(run), cr.r.sx, cr.r.scale):
            k = statfont.cluster_key(gids)
            parts.setdefault(k, cl)
            out.append(k)
        return out

    native_bytes: Set[int] = set()
    map_cells: Set[int] = set()
    for rid, row, text in brows:
        text = unicodedata.normalize("NFC", text)
        words = re.split(r"( +)", text)
        enc_words = []
        for w in words:
            runs = []
            for is_g, s in _battle_runs(w, native):
                if is_g:
                    ks = clusters(s)
                    counts.update(ks)
                    runs.append((True, ks))
                else:
                    try:
                        b, _ = tc.from_display(s, tc.BATTLE, "c0")
                    except tc.TxEncodeError:
                        b = b""
                    _battle_cells(b, map_cells, native_bytes)
                    runs.append((False, s))
            if rtl and any(unicodedata.bidirectional(c) in ("R", "AL") for c in w):
                runs.reverse()          # runs of a right-to-left word, right to left
            enc_words.append(runs)
        if rtl and any(unicodedata.bidirectional(c) in ("R", "AL") for c in text):
            enc_words.reverse()
        shaped[rid] = enc_words
    if not counts:
        return
    for rid, row, text in keep:
        try:
            b, _ = tc.from_display(unicodedata.normalize("NFC", text), tc.BATTLE, "c0")
        except tc.TxEncodeError:
            continue
        _battle_cells(b, map_cells, native_bytes)
    data = __import__("repack").read_entry(retail, 0, statfont.SHEET)
    canvas, clen, subs = statfont.xtx_canvas(data)
    keep_cells = usage.battle_cells | map_cells
    # the empty cells after the accented Latin (125+) first, then the
    # accent area (63-123), whose cells ``#XX`` escapes still reach
    free = [i for i in statfont.FREE if i not in keep_cells and i >= 125] + \
           [i for i in statfont.FREE if i not in keep_cells and i < 125]
    pool = [b for b in statfont.POOL if b not in usage.battle_bytes and b not in native_bytes]
    order = sorted(counts, key=lambda k: (-counts[k], k))
    if len(order) > len(free):
        raise TargetError(f"the battle grid has {len(free)} free cells and the battle-font "
                          f"translations need {len(order)} letters; shorten the battle labels "
                          f"to fewer distinct letters")
    cell_of, byte_of = {}, {}
    for k in order:
        cell_of[k] = free.pop(0)
        if pool:
            byte_of[k] = pool.pop(0)
        statfont.write_cell(canvas, clen, cell_of[k], cr.raster(parts[k]))
    plan.stat_xtx = statfont.xtx_store(data, canvas, subs)
    if byte_of:
        import txfam_ovl
        st = txfam_ovl._load(retail)
        va = txfam_ovl.BATTLE_TABLE_VA
        old = st.exes["OV01.OVL"].items[va].raw
        tbl = bytearray(old)
        for k, b in byte_of.items():
            tbl[cell_of[k]] = b
        plan.ovl_raw[("OV01.OVL", va)] = bytes(tbl)
    for rid, words in shaped.items():
        out = []
        for runs in words:
            for is_g, s in runs:
                if not is_g:
                    out.append(s)
                    continue
                for k in s:
                    if k in byte_of:
                        out.append(tc.glyph_char(byte_of[k]))
                    else:
                        out.append("".join(tc.glyph_char(c) for c in b"#%02X" % cell_of[k]))
        plan.edits[rid] = "".join(out)
    esc = sum(1 for k in order if k not in byte_of)
    plan.report.update({"battle_glyphs": len(order), "battle_table_bytes": len(byte_of),
                        "battle_escapes": esc})
    log(f"battle glyphs: {len(order)} -> grid cells {min(cell_of.values())}-"
        f"{max(cell_of.values())}, {len(byte_of)} table byte(s)"
        + (f", {esc} as #XX escapes (spare table bytes used up)" if esc else ""))


def plan_glyphs(retail: str, target: dict, rows: Dict[str, dict], edits: Dict[str, str],
                log: Callable[[str], None] = print, usage: Optional[Usage] = None) -> GlyphPlan:
    """The whole new-script part of a build (see the module docstring).

    ``target``: resolve_target(project); ``rows``: {id: catalog row} for at
    least every edited id; ``edits``: {id: logical text}. Raises TargetError
    (nothing is written by this function) with every refused row listed."""
    ok, msg = shaping_available()
    if not ok:
        raise TargetError(msg)
    plan = GlyphPlan()
    plan.edits = dict(edits)
    kind = target["kind"]
    errors: List[str] = []
    frows, brows = [], []
    fkeep, bkeep = [], []             # edited rows that need no glyph
    for rid in sorted(edits):
        row = rows.get(rid)
        if row is None:
            continue
        font = row.get("font", tc.FIELD)
        cov = coverage(edits[rid], kind, font, _face_of(row))
        if font == tc.ASCII:
            bad = cov["impossible"]
            if bad:
                errors.append(f"{rid}: drawn by the tag font (window title ribbons), which has "
                              f"only A-Z a-z 0-9 : / . - and space and no room for new letters; "
                              f"{bad[:20]!r} cannot be drawn there. Keep this row in Latin "
                              f"letters (transliterate it) or leave it untranslated.")
            continue
        if cov["impossible"]:
            errors.append(f"{rid}: cannot draw {cov['impossible'][:20]!r} "
                          f"({', '.join(tc.char_name(c) for c in cov['impossible'][:3])})")
        if not cov["glyph"]:
            (bkeep if font == tc.BATTLE else fkeep).append((rid, row, edits[rid]))
            continue
        (brows if font == tc.BATTLE else frows).append((rid, row, edits[rid]))
    if errors:
        raise TargetError(f"{len(errors)} translation(s) cannot be drawn; nothing was written:\n  "
                          + "\n  ".join(errors[:100]))
    fchars = "".join(sorted({c for _, r, t in frows
                             for c in coverage(t, kind, tc.FIELD, _face_of(r))["glyph"]}))
    bchars = "".join(sorted({c for _, _, t in brows
                             for c in coverage(t, kind, tc.BATTLE)["glyph"]}))
    if frows:
        _check_font(target.get("font", ""), target.get("face", 0), fchars, "dialogue and menus",
                    target)
    if brows:
        _check_font(target.get("battle_font", ""), target.get("battle_face", 0), bchars,
                    "battle labels", dict(target, font=target.get("battle_font")))
    plan.report.update({"kind": kind, "field_rows": len(frows), "battle_rows": len(brows),
                        "field_chars": fchars, "battle_chars": bchars})
    if usage is None:
        usage = retail_usage(retail, log)
    fonts = tc.load_fonts(retail)
    if frows:
        _plan_field(plan, target, frows, usage, fonts, log, keep=fkeep)
    if brows:
        _plan_battle(plan, target, brows, usage, retail, log, keep=bkeep)
    return plan


# ---------------------------------------------------------------------------
# preview (editor)
# ---------------------------------------------------------------------------

def preview(retail: str, target: dict, text: str, font: str = tc.FIELD, face: str = tc.FONT1,
            ctl: str = "c0", scale: int = 2) -> Tuple[List[bytes], str]:
    """PNG(s) of ``text`` as the game would draw it after a build (one PNG per
    line), and the encoded display text. Field font only; glyphs are placed
    for this text alone (cells kept: the text's own native letters), so the
    picture is right but the codes are not the ones a full build assigns."""
    import langpack
    rows = {"preview": {"font": font, "face": face, "measure": face, "ctl": ctl}}
    edits = {"preview": text}
    fonts = tc.load_fonts(retail)
    if target.get("kind", "builtin") != "builtin" and needs_glyphs(target, rows, edits):
        plan = plan_glyphs(retail, target, rows, edits, log=lambda m: None, usage=Usage())
        enc = plan.edits["preview"]
        f = plan.apply_font(fonts[face], face)
    else:
        enc, f = text, fonts[face]
    with tc.glyph_codes():
        data, _ = tc.from_display(enc, font, ctl, face)
    return [langpack.render_line(f, line, scale) for line in data.split(b"\n")], enc


# ---------------------------------------------------------------------------
# selftest / CLI
# ---------------------------------------------------------------------------

def _find_font(*names: str) -> Optional[str]:
    for n in names:
        for d in font_dirs():
            p = d / n
            if p.is_file():
                return str(p)
    return None


def selftest(iso: str, out: Optional[str] = None) -> int:
    fails = 0

    def expect(cond, what):
        nonlocal fails
        if not cond:
            fails += 1
            print("  FAIL", what)

    t0 = time.time()
    # 1. profiles: kinds from the alphabets
    kinds = {t["code"]: t["kind"] for t in TARGETS}
    for c in ("fr", "es", "de", "it", "pt", "pt-BR", "nl", "ca", "sv", "da", "no", "is"):
        expect(kinds[c] == "builtin", f"{c} builtin ({profile(c)['lacking']!r} lacking)")
    for c in ("pl", "cs", "sk", "hu", "ro", "tr", "vi", "hr", "eo", "fi"):
        expect(kinds[c] == "script-hybrid", f"{c} hybrid")
    expect(profile("pl")["lacking"] == "ąćęłńśźżĄĆĘŁŃŚŹŻ", "Polish lacks exactly ąćęłńśźż")
    expect(kinds["ja"] == "japanese" and kinds["ru"] == "script" and profile("ar")["rtl"],
           "ja / ru / ar kinds")
    expect(profile("PT_br")["code"] == "pt-BR" and profile("fr-CA")["code"] == "fr", "code lookup")
    for t in TARGETS:
        if t["kind"] == "builtin":
            expect(not coverage(t["sample"], t)["glyph"] and not coverage(t["sample"], t)["impossible"],
                   f"{t['code']} sample fully native")
    print(f"profiles: {len(TARGETS)} ({Counter(kinds.values())})")
    # 2. coverage
    cv = coverage("Zażółć gęślą jaźń", profile("pl"))
    expect(set(cv["glyph"]) == set("żłćęśąź ń") - {" "} and "ó" in cv["native"],
           f"Polish coverage {cv['glyph']!r}")
    cv = coverage("Привет, KOS-MOS!", profile("ru"))
    expect(cv["glyph"] == "".join(sorted("Привет")) and "," in cv["native"], f"ru coverage {cv}")
    cv = coverage("Привет", "builtin")
    expect(cv["glyph"] == "" and cv["native"] == "".join(sorted("Привет")),
           "builtin: JIS Cyrillic is drawn natively (full width)")
    lvl2 = "鬱"                       # JIS level-2 kanji (EUC DDB5)
    expect(lvl2.encode("euc_jp")[0] >= 0xD0, "test kanji is level 2")
    cv = coverage("こんにちは、漢字" + lvl2, profile("ja"))
    expect(cv["glyph"] == lvl2 and "漢" in cv["native"] and "こ" in cv["native"],
           f"Japanese: only level-2 needs a glyph ({cv['glyph']!r})")
    cv = coverage("Конец", profile("ru"), font=tc.ASCII)
    expect(cv["impossible"] == "".join(sorted("Конец")), "tag font refuses Cyrillic")
    cv = coverage("Épée àx", "builtin", font=tc.BATTLE)
    expect(cv["folded"] == "à", "builtin battle folds à")
    cv = coverage("Конец", profile("ru"), font=tc.BATTLE)
    expect(cv["glyph"] == "".join(sorted("Конец")), "battle Cyrillic needs glyphs")
    # 3. dependencies and fonts
    ok, msg = shaping_available()
    print(f"shaping: {msg}")
    expect(ok, "fonttools + uharfbuzz importable")
    t = time.time()
    found = {s: len(system_fonts(s)) for s in ("Cyrl", "Arab", "Jpan", "Hebr", "Deva", "Hang")}
    print(f"installed fonts covering: {found} ({time.time() - t:.1f} s)")
    if sys.platform == "darwin":
        expect(found["Cyrl"] > 0 and found["Arab"] > 0 and found["Jpan"] > 0,
               "macOS ships Cyrillic, Arabic and Japanese fonts")
    # 4. targets in projects
    tgt = resolve_target({"language": "ru", "target": {"code": "ru", "font": "/x.ttf"}})
    expect(tgt["kind"] == "script" and tgt["script"] == "Cyrl" and tgt["px"] == 21.0
           and tgt["battle_font"] == "/x.ttf", "resolve ru")
    expect(resolve_target({"language": "xx"})["kind"] == "builtin", "unknown code -> builtin")
    for bad in ({"kind": "klingon"}, {"px": "big"}, {"align": "centre"}, {"face": -1}):
        try:
            normalise_target(bad)
            expect(False, f"refuse {bad}")
        except TargetError:
            pass
    import txproject
    p = txproject.normalise({"format": 1, "language": "ru", "translations": {},
                             "target": {"code": "ru", "font": "/x.ttf", "junk": 1}})
    expect(p["target"] == {"code": "ru", "font": "/x.ttf"}, "txproject keeps target")
    # 5. placeholders
    s = "/[color(0x329bbe)]Привет %d (3)/[waitkey(64)]"
    body, saved = _protect(s, "evt")
    expect("/[" not in body and _unprotect(body, saved) == s, "protect/unprotect")
    # 6. retail usage vs glyphs.RETAIL_PROP_SLOTS
    import glyphs
    u = retail_usage(iso, print)
    from glyphs import reserved_prop_slots
    ascii_slots = reserved_prop_slots(True) - glyphs.RETAIL_PROP_SLOTS
    extra = sorted(u.prop - ascii_slots)
    print(f"retail block slots outside ASCII: {extra}")
    expect(set(extra) <= glyphs.RETAIL_PROP_SLOTS,
           f"glyphs.RETAIL_PROP_SLOTS covers retail usage (missing "
           f"{sorted(set(extra) - glyphs.RETAIL_PROP_SLOTS)})")
    # 7. Japanese: level-2 drawn into a kanji cell, rendered from the disc's font
    jfont = _find_font("ヒラギノ角ゴシック W3.ttc", "NotoSansJP-Regular.otf", "NotoSansCJK-Regular.ttc")
    outdir = Path(out) if out else None
    if outdir:
        outdir.mkdir(parents=True, exist_ok=True)
    if jfont:
        tj = resolve_target({"language": "ja", "target": {"code": "ja", "font": jfont}})
        rows = {"r1": {"font": tc.FIELD, "face": tc.FONT1, "ctl": "evt"}}
        line = "シオン、憂鬱な謎の本を読んだ。"
        plan = plan_glyphs(iso, tj, rows, {"r1": line}, log=print)
        enc = plan.edits["r1"]
        expect(enc.replace(tc.glyph_char(plan.glyphset.slots[next(iter(plan.glyphset.slots))]["code"]),
                           "鬱") == line, "only the level-2 kanji is replaced")
        slot = next(iter(plan.glyphset.slots.values()))
        expect(slot["tier"] == "wide" and 752 <= slot["cell"] <= 3743, f"wide cell {slot}")
        f1 = plan.apply_font(tc.load_fonts(iso)[tc.FONT1], tc.FONT1)
        expect(slot["cell"] not in u.wide, "level-2 kanji cell is not a cell retail draws")
        u2 = Usage()
        _field_cells(tc.from_display(line.replace("鬱", ""), tc.FIELD, "evt")[0], u2)
        expect(slot["cell"] not in u2.wide, "level-2 cell is not one the line's kanji use")
        with tc.glyph_codes():
            data, _ = tc.from_display(enc, tc.FIELD, "evt", tc.FONT1)
        import langpack
        png = langpack.render_line(f1, data, 3)
        if outdir:
            (outdir / "txtarget_ja_level2.png").write_bytes(png)
            print(f"  wrote {outdir / 'txtarget_ja_level2.png'}")
        # 7b. more level-2 kanji than the 24 empty cells: the level-1 kanji of
        # an edited row that needs no glyph keep their cells
        lv2 = "".join(bytes([0xD0 + i // 94, 0xA1 + i % 94]).decode("euc_jp") for i in range(30))
        rows = {"A": {"font": tc.FIELD, "face": tc.FONT1, "ctl": "evt"},
                "B": {"font": tc.FIELD, "face": tc.FONT1, "ctl": "evt"}}
        plan = plan_glyphs(iso, tj, rows, {"A": lv2, "B": "腕と湾"}, log=lambda m: None, usage=u)
        mine = {fontmod.cell_of_code(cd) for c in "腕湾" for cd in _field_codes(c, tc.FONT1)}
        given = {v["cell"] for v in plan.glyphset.slots.values()}
        expect(len(given) == 30 and not (mine & given),
               f"30 level-2 kanji leave the cells of 腕 湾 alone ({sorted(mine & given)})")
        expect(plan.edits["B"] == "腕と湾", "the no-glyph row is unchanged")
        print(f"ja: 30 level-2 kanji -> cells {min(given)}-{max(given)}; 腕 湾 at {sorted(mine)} kept")
    else:
        print("  (no Japanese font found; Japanese render skipped)")
    # 7c. battle: an accent an edited no-glyph battle row draws by #XX escape
    # keeps its grid cell
    bfont = _find_font("Arial.ttf", "Arial Unicode.ttf", "DejaVuSans.ttf", "NotoSans-Regular.ttf")
    if bfont:
        tcs = resolve_target({"language": "cs", "target": {"code": "cs", "font": bfont}})
        labels = ["čďěň", "řťůž", "ČĎĚŇ", "ŘŤŮŽ", "Éra"]
        rows = {f"b{i}": {"font": tc.BATTLE, "ctl": "c0"} for i in range(len(labels))}
        plan = plan_glyphs(iso, tcs, rows, {f"b{i}": t for i, t in enumerate(labels)},
                           log=lambda m: None, usage=u)
        esc = tc.from_display("É", tc.BATTLE, "c0")[0]
        cell_e = int(esc[1:], 16)
        expect(plan.edits["b4"] == "Éra" and plan.report.get("battle_glyphs") == 16, "cs battle plan")
        import repack as _rp, statfont as _sf
        old = _sf.xtx_canvas(_rp.read_entry(iso, 0, _sf.SHEET))
        new = _sf.xtx_canvas(plan.stat_xtx)

        def cell_px(cv, i):
            x0, y0 = _sf.GRID_X + (i % _sf.COLS) * _sf.CELL_W, _sf.GRID_Y + (i // _sf.COLS) * _sf.CELL_H
            return [_sf.get_index(cv[0], cv[1], x0 + x, y0 + y)
                    for y in range(_sf.CELL_H) for x in range(_sf.CELL_W)]
        expect(cell_px(old, cell_e) == cell_px(new, cell_e),
               f"battle cell {cell_e} (É, #{cell_e:02X}) is not overwritten")
        print(f"cs battle: {plan.report.get('battle_glyphs')} glyphs; É cell {cell_e} kept")
    # 7d. RTL: a colour tag in the middle of a line is refused (it would land
    # on other words); at the line's edges it is fine
    afont = _find_font("SFArabic.ttf", "GeezaPro.ttc", "NotoSansArabic-Regular.ttf")
    if afont:
        ta = resolve_target({"language": "ar", "target": {"code": "ar", "font": afont}})
        rows = {"c1": {"font": tc.FIELD, "face": tc.FONT1, "ctl": "evt"}}
        try:
            plan_glyphs(iso, ta, rows, {"c1": "خذ /[color(0x329bbe)]ملف القطعة/[color(0x808080)] الآن"},
                        log=lambda m: None, usage=u)
            expect(False, "RTL mid-line colour tag refused")
        except TargetError as e:
            expect("c1:" in str(e) and "/[color(0x329bbe)]" in str(e), f"refusal names row and tag: {e}")
        plan = plan_glyphs(iso, ta, rows, {"c1": "/[color(0x329bbe)]ملف القطعة/[color(0x808080)]"},
                           log=lambda m: None, usage=u)
        enc = plan.edits["c1"]
        expect(enc.startswith("/[color(0x329bbe)]") and enc.endswith("/[color(0x808080)]"),
               "RTL edge colour tags stay at the edges")
        print("ar: mid-line colour tag refused, edge tags kept")
    # 8. statfont RTL order: HarfBuzz gives visual order; the encoder keeps it
    import statfont
    afont = _find_font("SFArabic.ttf", "GeezaPro.ttc", "NotoSansArabic-Regular.ttf")
    if afont:
        cr = statfont.CellRenderer(afont, rtl=True, script="Arab", language="ar")
        g = cr.shape("سلام")
        expect([x[1] for x in g] == sorted((x[1] for x in g), reverse=True),
               "HarfBuzz RTL output is in visual order (clusters descending)")
        m = {"g:" + str(x[0]): i for i, x in enumerate(g)}
        e = statfont.Encoder(m, cr, {k: 0x21 + v for k, v in m.items()}).string("سلام")
        expect(e == "".join(chr(0x21 + i) for i in range(len(g))), f"statfont keeps visual order {e!r}")
        # marks and dots go with their own base (HarfBuzz lists an RTL
        # cluster's zero-advance glyphs before the base)
        for word in ("نهاية", "قوّة"):
            g = cr.shape(word)
            cells = statfont.cell_clusters(g, cr.r.sx, cr.r.scale)
            n_cl = len({x[1] for x in g})
            pos, ok_cl = 0, True
            for gids, parts, adv in cells:
                cl = {x[1] for x in g[pos:pos + len(gids)]}
                ok_cl &= len(cl) == 1 and adv > 0 and all(abs(dx) < statfont.CELL_W for _, dx, _ in parts)
                pos += len(gids)
            expect(len(cells) == n_cl and ok_cl,
                   f"battle cells of {word}: one per HarfBuzz cluster, marks over their base")
            print(f"ar battle cells of {word}: {[c[0] for c in cells]}")
    print(f"selftest {'OK' if not fails else f'FAILED ({fails})'} ({time.time() - t0:.1f} s)")
    return 0 if not fails else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    f = sub.add_parser("fonts")
    f.add_argument("--script")
    f.add_argument("--chars")
    c = sub.add_parser("coverage")
    c.add_argument("--code", required=True)
    c.add_argument("--text", required=True)
    c.add_argument("--font", default=tc.FIELD, choices=(tc.FIELD, tc.BATTLE, tc.ASCII))
    c.add_argument("--ttf")
    s = sub.add_parser("selftest")
    s.add_argument("--iso", required=True)
    s.add_argument("--out")
    a = ap.parse_args(argv)
    if a.cmd == "list":
        for t in TARGETS:
            print(f"{t['code']:8} {t['kind']:14} {t['script']:5} {'rtl' if t['rtl'] else '   '} "
                  f"{t['name']} / {t['endonym']}" + (f"  (lacks {t['lacking']})" if t['lacking'] else ""))
        return 0
    if a.cmd == "fonts":
        for x in system_fonts(a.script, a.chars):
            print(f"{x['path']}#{x['face']}  {x['family']} {x['style']}  {' '.join(x['scripts'])}")
        return 0
    if a.cmd == "coverage":
        prof = profile(a.code) or {"kind": "script"}
        print(json.dumps({k: v for k, v in coverage(a.text, prof, a.font, ttf=a.ttf).items()
                          if k != "counts"}, ensure_ascii=False, indent=1))
        return 0
    return selftest(a.iso, a.out)


if __name__ == "__main__":
    sys.exit(main())
