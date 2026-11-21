"""pseudoloc.py — fill an exported text tree with a syllabary respelling of
the English, to test the translation pipeline end to end.

NOT a translation. Each English word is respelled phonetically in Cherokee
syllabary (``--script cher``), so every menu, help line and label comes out
in glyphs the game's font does not have, at roughly the length a real
translation would be. Run ``lang scan`` / ``lang encode`` on the result and
import it like any translation; a missing string, a slot that is too small, a
text path that cannot draw the script all show up at once.

    python pseudoloc.py --text tr/exe --out tr/pseudo [--from 4c3380 --to 4cc000]
    python cli.py lang scan   --iso GAME.iso --text tr/pseudo --font PlantagenetCherokee.ttf \\
                              --script Cher --language chr --px 18 --out tr/font
    python cli.py lang encode --text tr/pseudo --glyphset tr/font/glyphset.json --out tr/pseudo-enc
    python cli.py ovl-import  --iso GAME.iso --text tr/pseudo-enc --out TEST.iso --grow --skip-unfit

Only ovltext files are read (``# ovltext`` header, ``@address<TAB>text``).
Left alone: control escapes (``\\n``), format codes, paths and debug prints,
Japanese, names and abbreviations in ``KEEP`` (A.G.W.S., KOS-MOS, T.Pts …),
and the strings in ``EXCLUDE``: window titles drawn by the game's sprite-letter
text, an ASCII-only path that cannot show other scripts (see SCRIPTS.md).
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

_SYL = ("a e i o u v ga ka ge gi go gu gv ha he hi ho hu hv la le li lo lu lv ma me mi mo mu "
        "na hna nah ne ni no nu nv qua que qui quo quu quv sa s se si so su sv da ta de te di "
        "ti do du dv dla tla tle tli tlo tlu tlv tsa tse tsi tso tsu tsv wa we wi wo wu wv "
        "ya ye yi yo yu yv").split()
_ROWS = {"g": "ga ge gi go gu gv", "h": "ha he hi ho hu hv", "l": "la le li lo lu lv",
         "m": "ma me mi mo mu mu", "n": "na ne ni no nu nv", "s": "sa se si so su sv",
         "d": "da de di do du dv", "t": "ta te ti do du dv", "w": "wa we wi wo wu wv",
         "y": "ya ye yi yo yu yv", "c": "tsa tse tsi tso tsu tsv", "": "a e i o u v"}
_CONS = {**{c: "g" for c in "kgcq"}, "h": "h", **{c: "l" for c in "lr"}, "m": "m", "n": "n",
         **{c: "s" for c in "szx"}, "d": "d", "t": "t", **{c: "w" for c in "wvfbp"},
         **{c: "y" for c in "yj"}}
VOWELS = "aeiou"

KEEP = set("a.g.w.s. u.m.n. kos-mos shion jr. momo ziggy virgil chaos mary bunnie t.pts e.pts "
           "s.pts hp ep lv exp hi lw".split())
EXCLUDE = {"Menu", "Combatant", "Set Skills", "SetList/HI", "SetList/LW", "Reserve",
           "Registered Reserve", "Registered Combatant"}
SKIP = re.compile(r"Illegal|error|Create|serial|Test|delete|Init|overflow|over flow|sizeof|"
                  r"Debug|Obtained|Format|Corrupt|\\\\|%|=|\.(bin|fpk|jnt|xtx|lex|jpg|new)|^data")


def _s(names: str) -> str:
    return "".join(chr(0x13A0 + _SYL.index(t)) for t in names.split())


def _syl(row: str, v: str) -> str:
    names = _ROWS[row].split()
    return _s(names[VOWELS.index(v)] if v in VOWELS else names[5])


def word(w: str) -> str:
    w = (w.lower().replace("sh", "C").replace("ch", "C").replace("th", "t").replace("ph", "f")
         .replace("ck", "k").replace("qu", "kw"))
    out, i = [], 0
    while i < len(w):
        c = w[i]
        if c in VOWELS:
            out.append(_syl("", c)); i += 1
        elif c == "C" or c in _CONS:
            row = "c" if c == "C" else _CONS[c]
            if i + 1 < len(w) and w[i + 1] in VOWELS:
                out.append(_syl(row, w[i + 1])); i += 2
            elif row == "s":
                out.append(_s("s")); i += 1
            else:
                out.append(_syl(row, "v")); i += 1
        else:
            i += 1
    return "".join(out)


_SPLIT = re.compile(r"(\\n|[A-Za-z]+(?:\.[A-Za-z]+)+\.?|[A-Za-z]+-[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*|[^A-Za-z\\]+|\\)")


def respell(text: str) -> str:
    out = []
    for p in _SPLIT.split(text):
        if not p:
            continue
        if re.fullmatch(r"[A-Za-z]+", p) and p.lower() not in KEEP and (p.upper() != p or len(p) >= 4):
            out.append(word(p))
        else:
            out.append(p)
    return "".join(out)


def eligible(text: str) -> bool:
    plain = text.replace("\\n", "")
    return (text not in EXCLUDE and not SKIP.search(text) and not re.search(r"[^\x20-\x7e]", plain)
            and re.search(r"[A-Za-z]{3,}", text) is not None and not (text.isupper() and len(text) < 7))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--text", required=True, help="ovl-export tree")
    ap.add_argument("--out", required=True)
    ap.add_argument("--from", dest="lo", default="0", help="first address (hex)")
    ap.add_argument("--to", dest="hi", default="ffffffff", help="last address (hex)")
    ap.add_argument("--script", default="cher", choices=["cher"])
    a = ap.parse_args(argv)
    lo, hi = int(a.lo, 16), int(a.hi, 16)
    n = 0
    for f in sorted(Path(a.text).rglob("*.txt")):
        src = f.read_text(encoding="utf-8-sig").split("\n")
        if not src or not src[0].startswith("# ovltext"):
            continue
        out = src[:2]
        for line in src[2:]:
            m = re.match(r"@([0-9a-f]+)\t(.*)$", line)
            if not m or not lo <= int(m.group(1), 16) <= hi or not eligible(m.group(2)):
                continue
            new = respell(m.group(2))
            if new != m.group(2):
                out.append(f"@{m.group(1)}\t{new}"); n += 1
        dst = Path(a.out) / f.relative_to(a.text)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"respelled {n} strings -> {a.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
