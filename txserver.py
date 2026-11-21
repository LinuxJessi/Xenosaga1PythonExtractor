"""txserver.py — server side of the translation editor page (/editor, /tx/*).

gui.Handler delegates here before its own routes:

    if txserver.handle_get(self, url.path, url.query): return
    if txserver.handle_post(self, url.path, body_bytes, content_type): return

Both return False for any path that is not ``/editor`` or under ``/tx/``.
They answer through the handler's own ``_send`` / ``_send_json``.

Reads (catalog, checks, previews) run in-process because they are fast
and read-only. Writes go to two places only: the project JSON the
translator picked (atomic temp + rename) and, for Build, gui's existing
``/run/tx-build`` Job which shells out to ``cli.py tx-build`` like every
other card, so the CLI and the GUI keep one code path for ISO writes.

Safety: a per-launch token (``TOKEN``) is baked into the served page and
every POST must carry it in ``X-TX-Token``; requests whose Host header is
not the loopback address are refused, so another web page in the same
browser cannot drive the editor (or overwrite a project) by guessing the
port. Game text is never interpolated into HTML here; the page inserts
it with textContent.

Development: with ``TX_FAKE=1`` in the environment, ``/tx/catalog``
serves a tiny built-in catalog (and ``/tx/check`` / ``/tx/preview`` fall
back to a minimal token checker and plain textpack encoding when txcodec
is not importable), so the page can be exercised before the family
readers exist. Never set it for real work.

    python3 txserver.py selftest [--iso RETAIL]     # headless route test
    PORT=8932 python3 txserver.py serve              # page alone, no gui.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import sys
import threading
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import parse_qs, quote

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import txproject  # noqa: E402
import txbackup  # noqa: E402

TOKEN = secrets.token_hex(16)
FAKE = os.environ.get("TX_FAKE") == "1"
MAX_BODY = 64 * 1024 * 1024

_LOCK = threading.Lock()
_CATALOGS: Dict[tuple, dict] = {}       # (iso path, mtime) -> catalog + index
_CURRENT: Dict[str, object] = {"iso": None, "key": None}


# ---------------------------------------------------------------------------
# catalog
# ---------------------------------------------------------------------------

FAKE_ROWS = [
    {"id": "evt:fake:0:1", "fam": "evt", "cat": "Dialogue — field maps", "grp": "ST0210", "ord": 1,
     "src": "/[label(Shion)]Allen, have you seen\nKOS-MOS anywhere?", "ctx": "Shion",
     "font": "field", "ctl": "evt", "measure": "font0", "px": 480, "lim": None, "lock": None, "hide": None},
    {"id": "evt:fake:0:2", "fam": "evt", "cat": "Dialogue — field maps", "grp": "ST0210", "ord": 2,
     "src": "/[label(Allen)]Er... no, Chief.", "ctx": "Allen",
     "font": "field", "ctl": "evt", "measure": "font0", "px": 480, "lim": None, "lock": None, "hide": None},
    {"id": "evt:fake:0:3", "fam": "evt", "cat": "Dialogue — field maps", "grp": "ST0219", "ord": 1,
     "src": "/[label(Allen)]Er... no, Chief.", "ctx": "Allen",
     "font": "field", "ctl": "evt", "measure": "font0", "px": 480, "lim": None, "lock": None, "hide": None},
    {"id": "evt:fake:0:4", "fam": "evt", "cat": "Dialogue — field maps", "grp": "ST0219", "ord": 2,
     "src": "debug: room init", "ctx": "", "font": "ascii", "ctl": "evt", "measure": None, "px": None,
     "lim": None, "lock": None, "hide": "debug print"},
    {"id": "umn:fake:0003:0", "fam": "umn", "cat": "U.M.N. mail", "grp": "umn/0003", "ord": 0,
     "src": "⟦15⟧⟦02⟧Subject: Test mail\nBody text, 100% genuine.", "ctx": "body",
     "font": "field", "ctl": "c0", "measure": "font1", "px": None, "lim": None, "lock": None, "hide": None},
    {"id": "umn:fake:chat:1", "fam": "umn", "cat": "U.M.N. chat", "grp": "umn/chat01", "ord": 0,
     "src": "\\15\\2Hey, %s! You have %d new items.", "ctx": "<char>",
     "font": "field", "ctl": "chat", "measure": "font1", "px": None, "lim": None, "lock": None, "hide": None},
    {"id": "ovl:fake:SLUS_204.69:362720", "fam": "ovl", "cat": "Field menu & system", "grp": "SLUS_204.69",
     "ord": 0, "src": "⟦0D⟧PAUSE", "ctx": "", "font": "field", "ctl": "c0", "measure": "font1",
     "px": None, "lim": 12, "lock": None, "hide": None},
    {"id": "ovl:fake:OV01:1", "fam": "ovl", "cat": "Battle HUD", "grp": "OV01 accNameTbl", "ord": 0,
     "src": "Boost", "ctx": "accNameTbl", "font": "battle", "ctl": "c0", "measure": None, "px": None,
     "lim": 8, "lock": None, "hide": None},
    {"id": "ovl:fake:OV01:2", "fam": "ovl", "cat": "Battle HUD", "grp": "OV01 accNameTbl", "ord": 1,
     "src": "Reserve", "ctx": "PrintTagFont ribbon", "font": "ascii", "ctl": "c0", "measure": None,
     "px": None, "lim": 8, "lock": "shared descriptor (referenced 313 times)", "hide": None},
] + [
    {"id": f"evt:fake:1:{i}", "fam": "evt", "cat": "Dialogue — cutscenes", "grp": "SCE01001 / playSCENE5",
     "ord": i, "src": f"/[label(KOS-MOS)]Line {i}: target acquired.", "ctx": "KOS-MOS", "font": "field",
     "ctl": "evt", "measure": "font0", "px": 480, "lim": None, "lock": None, "hide": None}
    for i in range(240)
]


def _fake_catalog() -> dict:
    from txcatalog import aggregate, build_tree
    rows = aggregate([[dict(r) for r in FAKE_ROWS]], log=lambda m: None)
    return {"rows": rows, "cats": build_tree(rows), "retail_id": "fake",
            "notes": ["TX_FAKE=1: built-in sample catalog, not the disc"]}


def get_catalog(iso: str) -> dict:
    """Catalog for ``iso``, cached in memory per (path, mtime)."""
    if FAKE:
        key = ("<fake>", 0)
    else:
        p = Path(iso).expanduser()
        if not p.is_file():
            raise FileNotFoundError(f"no such ISO: {iso}")
        key = (str(p.resolve()), p.stat().st_mtime_ns)
    with _LOCK:
        cat = _CATALOGS.get(key)
        if cat is None:
            if FAKE:
                cat = _fake_catalog()
            else:
                import txcatalog
                cat = txcatalog.build_catalog(key[0])
            cat["_index"] = {r["id"]: r for r in cat["rows"]}
            cat["_iso"] = key[0] if not FAKE else iso
            _CATALOGS[key] = cat
            if not FAKE:
                threading.Thread(target=_warm_families, args=(key[0],), daemon=True).start()
        _CURRENT["iso"] = key[0] if not FAKE else iso
        _CURRENT["key"] = key
    return cat


def _warm_families(iso: str) -> None:
    """Load the family readers' rows in the background (the catalog may come
    from the disk cache), so the first per-row family check is instant."""
    import importlib
    for name in ("txfam_umn", "txfam_fix", "txfam_ovl", "txfam_evt"):
        try:
            importlib.import_module(name).validate({}, iso)
        except Exception:
            pass


def catalog_part_bytes(cat: dict, part: str, gz: bool) -> bytes:
    """JSON bytes of one compact part ("main" = visible rows plus the tree and
    notes, "hidden" = rows hidden by default), built once per catalog and kept
    with it, raw and gzipped."""
    import gzip
    import txcatalog
    with _LOCK:
        cache = cat.setdefault("_wire", {})
        key = (part, gz)
        if key not in cache:
            raw = cache.get((part, False))
            if raw is None:
                payload = txcatalog.compact(cat["rows"], part)
                if part == "main":
                    payload.update({"cats": cat["cats"], "retail_id": cat.get("retail_id", ""),
                                    "notes": cat.get("notes", []), "fake": FAKE,
                                    "total": len(cat["rows"])})
                raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                cache[(part, False)] = raw
            if gz:
                cache[key] = gzip.compress(raw, 6)
        return cache[key]


def _current() -> Optional[dict]:
    with _LOCK:
        return _CATALOGS.get(_CURRENT["key"]) if _CURRENT["key"] else None


def _rows_for(body: dict) -> dict:
    """Catalog of the request's ``iso`` (the page always sends it), else the
    last one loaded. The catalog carries its own image path in ``_iso``, so a
    check never mixes one image's rows with another's fonts."""
    iso = body.get("iso")
    if iso:
        return get_catalog(iso)
    cat = _current()
    if cat is None:
        raise LookupError("no catalog loaded yet; open a retail ISO first")
    return cat


# ---------------------------------------------------------------------------
# disc detection (the editor's step 1 picker)
# ---------------------------------------------------------------------------

# txproject.retail_id of the untouched Xenosaga Episode I (USA) image
KNOWN_RETAIL_IDS = {"834146435111e58c2a8495065d9cf4e4a2bb0b82"}
_DISC_INFO: Dict[tuple, dict] = {}


def _disc_info(path: str) -> dict:
    """{"xs1": bool, "rid": str} for one .iso, cached per (path, size, mtime)."""
    st = os.stat(path)
    key = (path, st.st_size, st.st_mtime_ns)
    info = _DISC_INFO.get(key)
    if info is None:
        info = {"xs1": False, "rid": ""}
        try:
            from iso9660 import IsoImage
            iso = IsoImage(path)
            try:
                elf = iso.boot_elf_name() or ""
            finally:
                iso.close()
            if elf.upper() == txproject.BOOT_ELF:
                info["xs1"] = True
                info["rid"] = txproject.retail_id(path)
        except Exception:
            pass
        _DISC_INFO[key] = info
    return info


def detect_discs() -> List[dict]:
    """Xenosaga Episode I (USA) images in the usual places, the untouched
    retail image(s) first: [{"path", "retail": bool}]. Other games and
    discs are left out (gui.detect_isos keeps the first ten .iso of any
    kind, which can push the right disc off its list)."""
    import gui
    cands: list = []
    for root, depth in ((gui.ROOT.parent, 2), (Path.home() / "Downloads", 2),
                        (Path.home() / "Desktop", 3), (Path.home() / "Documents", 3)):
        try:
            if root.is_dir():
                gui._walk_for_isos(root, depth, 400, cands)
        except OSError:
            continue
    out, seen = [], set()
    for p in cands:
        s = str(p)
        if s in seen:
            continue
        seen.add(s)
        try:
            info = _disc_info(s)
        except OSError:
            continue
        if info["xs1"]:
            out.append({"path": s, "retail": info["rid"] in KNOWN_RETAIL_IDS})
    out.sort(key=lambda d: not d["retail"])
    return out


# ---------------------------------------------------------------------------
# checks and previews
# ---------------------------------------------------------------------------

_FONTS: Dict[str, dict] = {}


def _txcodec():
    try:
        import txcodec
        return txcodec
    except ImportError:
        if FAKE:
            return None
        raise


def _fonts(iso: str) -> dict:
    tc = _txcodec()
    if tc is not None:
        return tc.load_fonts(iso)
    if iso not in _FONTS:
        from langpack import _load_disc_fonts
        f0, f1 = _load_disc_fonts(iso, None, None)
        _FONTS[iso] = {"font0": f0, "font1": f1}
    return _FONTS[iso]


_FAKE_TOK = re.compile(r"(?:⟦[0-9A-Fa-f]{2}⟧)+|/\[[^\]]*\]|\\\d+|%[-+0-9.]*[dsxXc]")


def _fake_check(text: str, row: dict) -> dict:
    errors, warnings = [], []
    a, b = Counter(_FAKE_TOK.findall(row["src"])), Counter(_FAKE_TOK.findall(text))
    if a != b:
        missing = list((a - b).elements())
        extra = list((b - a).elements())
        hard = any(t.startswith(("⟦", "/[")) for t in missing + extra)
        msg = f"protected tokens differ (missing {missing}, extra {extra})"
        (errors if hard else warnings).append(msg)
    nbytes = len(text.encode("utf-8"))
    if row.get("lim") is not None and nbytes > row["lim"]:
        errors.append(f"{nbytes} bytes > limit {row['lim']}")
    return {"ok": not errors, "errors": errors, "warnings": warnings + ["(TX_FAKE check)"],
            "nbytes": nbytes, "px": None}


def _face(row: dict) -> str:
    """Face the row's text is encoded for: evt rows carry ``face`` (font0 and
    font1 swap grave/acute), otherwise the width face, otherwise font1."""
    f = row.get("face") or row.get("measure")
    return f if f in ("font0", "font1") else "font1"


def check_one(row: dict, text: str, iso: Optional[str], target: Optional[dict] = None) -> dict:
    """One row's checks. ``target`` is the resolved project target
    (``resolve_target_body``); for a target that draws new letters the text
    goes through ``_target_row_check`` first, so the editor refuses what the
    build's glyph plan refuses and the codec sees ordinary glyph codes."""
    if row.get("lock"):
        return {"ok": False, "errors": [f"locked: {row['lock']}"], "warnings": [],
                "nbytes": None, "px": None}
    tc = _txcodec()
    if tc is None:
        return _fake_check(text, row)
    pre_errors: List[str] = []
    pre_warnings: List[str] = []
    checked = text
    glyphs = ""
    if target and target.get("kind", "builtin") != "builtin":
        pre_errors, checked, glyphs, pre_warnings = _target_row_check(row, text, target)
        if checked is None:
            return {"ok": False, "errors": pre_errors, "warnings": pre_warnings,
                    "nbytes": None, "px": None, "glyphs": glyphs}
    measure = None
    if row.get("measure") and iso and not glyphs:
        # new letters have no retail width: the width is known after the build
        try:
            measure = _fonts(iso).get(row["measure"])
        except Exception:
            measure = None
    try:
        with tc.glyph_codes() if glyphs else _nullctx():
            res = tc.check(checked, row["src"], row["font"], row["ctl"], limit_bytes=row.get("lim"),
                           measure=measure, width_px=row.get("px"), face=_face(row))
    except Exception as exc:
        return {"ok": False, "errors": [f"checker failed: {type(exc).__name__}: {exc}"],
                "warnings": [], "nbytes": None, "px": None}
    errors = pre_errors + [e for e in res.get("errors", []) if e not in pre_errors]
    warnings = pre_warnings + [w for w in res.get("warnings", []) if w not in pre_warnings]
    if iso and not FAKE and text != row["src"]:
        with tc.glyph_codes() if glyphs else _nullctx():
            fe, fw = _family_rules(row, checked, iso)
        errors += [e for e in fe if e not in errors]
        warnings += [w for w in fw if w not in warnings]
    out = {"ok": not errors, "errors": errors, "warnings": warnings,
           "nbytes": res.get("nbytes"), "px": res.get("px")}
    if glyphs:
        out["glyphs"] = glyphs
    if iso and not FAKE and text != row["src"] and res.get("px_lines"):
        _length_warnings(row, res["px_lines"], iso, out)
    return out


# ---------------------------------------------------------------------------
# length guides: how long the original game's own text of each kind ever gets
# ---------------------------------------------------------------------------
# Most rows have no hard limit (dialogue grows freely on the disc), so nothing
# stops a translation from running past its text box on screen. What can be
# known without booting the game is what retail itself does: per category,
# the widest line and the largest number of lines any original string has.
# A translation past both is flagged (a warning: the box sizes themselves are
# not decoded, so this is a guide, like the px guides).

_LENGTH_GUIDES: Dict[str, Dict[str, dict]] = {}


def _est_lines(px_lines: List[int], width: int) -> int:
    """Lines on screen when each line wider than ``width`` is wrapped."""
    return sum(max(1, -(-w // width)) for w in px_lines)


def _length_guides(iso: str) -> Dict[str, dict]:
    """Per category, over the visible, unlocked field-font rows of ``iso``:
    ``total`` = the longest original string (px of all its lines added up),
    ``lines`` = the most line breaks + 1 in one original string, ``est`` = the
    most lines on screen of one original string once lines wider than the
    row's own px guide are wrapped (rows that have a guide)."""
    key = str(iso)
    got = _LENGTH_GUIDES.get(key)
    if got is not None:
        return got
    tc = _txcodec()
    src_px: Dict[str, List[int]] = {}
    out: Dict[str, dict] = {"": src_px}     # "" = each row's own original, by id
    try:
        fonts = _fonts(iso)
        rows = get_catalog(iso)["rows"]
    except Exception:
        return {}
    for r in rows:
        if r.get("hide") or r.get("lock") or not r.get("measure") or r.get("font") != tc.FIELD:
            continue
        m = fonts.get(r["measure"])
        if m is None:
            continue
        try:
            data = tc.from_display(r["src"], r["font"], r["ctl"], _face(r))[0]
        except Exception:
            continue
        pxl = src_px[r["id"]] = _px_lines(tc, m, r, data)
        g = out.setdefault(r["cat"], {"total": 0, "lines": 0, "est": 0})
        g["total"] = max(g["total"], sum(pxl))
        g["lines"] = max(g["lines"], len(pxl))
        if r.get("px"):
            g["est"] = max(g["est"], _est_lines(pxl, int(r["px"])))
    _LENGTH_GUIDES[key] = out
    return out


def _px_lines(tc, m, row: dict, data: bytes) -> List[int]:
    if row["ctl"] == "evt":
        data = tc._TAG_RAW_RE.sub(b"", data)
    return [m.width_of_bytes(x) for x in data.split(b"\n")]


def _length_warnings(row: dict, px_lines: List[int], iso: str, out: dict) -> None:
    """Adds to a check result: ``len_pct`` (drawn length against the original,
    100 = the same), ``wide`` (a line is past the row's px guide and can be
    wrapped), and a warning when the text is longer than anything the original
    game shows in this category."""
    guides = _length_guides(iso)
    g = guides.get(row.get("cat"))
    if not g:
        return
    src_pxl = guides[""].get(row["id"]) or []
    total, src = sum(px_lines), sum(src_pxl)
    if src:
        out["len_pct"] = round(100 * total / src)
    if row.get("px"):
        width = int(row["px"])
        if max(px_lines) > width and g["lines"] > 1:
            out["wide"] = True
        est = _est_lines(px_lines, width)
        if est > g["est"] and est > _est_lines(src_pxl or [0], width):
            out["warnings"].append(f"about {est} lines on screen; no original text of this kind takes "
                                   f"more than {g['est']}. It may not fit its box: shorten it, then "
                                   f"look at this screen in the emulator")
    elif total > g["total"] and total > src:
        out["warnings"].append(f"longer than any original text of this kind ({total} px of letters; "
                               f"the longest original has {g['total']}). It may not fit its box: "
                               f"shorten it, then look at this screen in the emulator")


def _wrap_width(row: dict, iso: str) -> Optional[int]:
    return int(row["px"]) if row.get("px") else None


_GLUE_BEFORE = set("?!:;»”)")          # French spacing: never start a line with these
_WORD_RE = re.compile(r"(?:/\[[^\]\n]*\]|[^ ])+")


def wrap_text(row: dict, text: str, iso: str) -> str:
    """Break the lines of ``text`` that are wider than the row's width (see
    ``_wrap_width``) at spaces. Lines that fit, and line breaks already there,
    are kept. ``/[...]`` tags are never split."""
    tc = _txcodec()
    width = _wrap_width(row, iso)
    m = _fonts(iso).get(row.get("measure")) if row.get("measure") else None
    if not width or m is None:
        return text

    def px(line: str) -> int:
        data = tc.from_display(line, row["font"], row["ctl"], _face(row))[0]
        if row["ctl"] == "evt":
            data = tc._TAG_RAW_RE.sub(b"", data)
        return m.width_of_bytes(data)

    res = []
    for line in text.split("\n"):
        try:
            if px(line) <= width:
                res.append(line)
                continue
            words = []
            for w in _WORD_RE.findall(line):
                if words and w and all(c in _GLUE_BEFORE for c in w):
                    words[-1] += " " + w
                elif words and words[-1] in ("«", "“", "("):
                    words[-1] += " " + w
                else:
                    words.append(w)
            cur = ""
            for w in words:
                cand = w if not cur else cur + " " + w
                if cur and px(cand) > width:
                    res.append(cur)
                    cur = w
                else:
                    cur = cand
            if cur:
                res.append(cur)
        except ValueError:              # a letter the font lacks: leave the line alone
            res.append(line)
    return "\n".join(res)


class _nullctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# per-row checks for targets that draw new letters (txtarget)
# ---------------------------------------------------------------------------

_CMAPS: Dict[tuple, Optional[frozenset]] = {}
_CMAP_LOCK = threading.Lock()


def _cmap(path: str, face: int = 0) -> Optional[frozenset]:
    """Code points of one font face (cached per path, face, mtime); None when
    the font cannot be read or fonttools is missing."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, int(face), st.st_mtime_ns)
    with _CMAP_LOCK:
        if key in _CMAPS:
            return _CMAPS[key]
    try:
        import txtarget
        cm = txtarget.font_cmap(path, int(face))
        val = frozenset(cm) if cm is not None else None
    except Exception:
        val = None
    with _CMAP_LOCK:
        _CMAPS[key] = val
    return val


def _drawn(c: str) -> bool:
    """A character that needs its own glyph in a font (not a combining mark,
    which the shaper places from its base letter, and not a space)."""
    import unicodedata
    return not c.isspace() and unicodedata.category(c) not in ("Mn", "Me")


def _char_list(chars: str, n: int = 12) -> str:
    import txcodec as tc
    shown = " ".join(chars[:n]) + (" …" if len(chars) > n else "")
    names = ", ".join(tc.char_name(c) for c in chars[:2])
    return f"{shown} ({names}{', ...' if len(chars) > 2 else ''})"


def _target_row_check(row: dict, text: str, t: dict):
    """What the build's glyph plan (txtarget.plan_glyphs / _plan_field /
    _check_font / _encode_with) would refuse for this row, before the codec
    runs. -> (errors, text for the codec with every new letter replaced by a
    glyph code, or None when the codec need not run; the new letters;
    warnings)."""
    import unicodedata
    import txcodec as tc
    import txtarget as tt
    font = row.get("font") or tc.FIELD
    face = _face(row)
    errors: List[str] = []
    warnings: List[str] = []
    cov = tt.coverage(text, t, font, face)
    if font == tc.ASCII:
        if cov["impossible"]:
            errors.append(f"drawn by the tag font (window title ribbons), which has only A-Z a-z 0-9 "
                          f": / . - and space and no room for new letters; {_char_list(cov['impossible'])} "
                          f"cannot be drawn there. Keep this row in Latin letters (transliterate it) "
                          f"or leave it untranslated")
            return errors, None, "", warnings
        return errors, text, "", warnings
    if cov["impossible"]:
        errors.append(f"cannot draw {_char_list(cov['impossible'])}")
    glyph = cov["glyph"]
    if not glyph:
        return errors, text, "", warnings
    battle = font == tc.BATTLE
    fpath = t.get("battle_font") if battle else t.get("font")
    fface = int((t.get("battle_face") if battle else t.get("face")) or 0)
    where = "battle labels" if battle else "dialogue and menus"
    need = "".join(c for c in glyph if _drawn(c))
    if not fpath:
        errors.append(f"{_char_list(need or glyph)} must be drawn from a font file for {where}, and "
                      f"none is chosen: pick one in step 2 (Language setup)")
    elif not Path(fpath).is_file():
        errors.append(f"the font file {fpath} does not exist (step 2, Language setup)")
    else:
        cmap = _cmap(fpath, fface)
        if cmap is not None:
            miss = "".join(c for c in need if ord(c) not in cmap)
            if miss:
                errors.append(f"the font {_font_label(fpath, fface)} has no letter for "
                              f"{_char_list(miss)}; pick a font that covers them (step 2)")
    if t.get("rtl") and not battle and hasattr(tt, "_protect") and hasattr(tt, "_rtl_mid_controls"):
        body, saved = tt._protect(unicodedata.normalize("NFC", text), row.get("ctl", "c0"))
        for para in body.split("\n"):
            m = tt._EDGE_RE.match(para)
            bad = tt._rtl_mid_controls(m.group(2), saved) if m else []
            if bad:
                errors.append(f"right-to-left text cannot carry {', '.join(repr(b) for b in bad[:4])} in "
                              f"the middle of a line: the line is reordered word by word for display, "
                              f"so a colour or control code there would end up on other words. Put it "
                              f"at the start or end of the line, or leave it out")
                break
    # every new letter becomes a glyph code, as in a build, so the codec and
    # the family rules see ordinary text (bytes: 2 a letter in the field font,
    # 1 in the battle grid; the build may differ where letters join)
    subs = {}
    for i, c in enumerate(sorted(set(glyph))):
        subs[c] = tc.glyph_char((0x80 + i % 0x60) if battle else (0xB0A1 + (i // 94) * 0x100 + i % 94))
    checked = "".join(subs.get(c, c) for c in unicodedata.normalize("NFC", text))
    if row.get("lim") is not None:
        warnings.append(f"{len(set(glyph))} new letter(s): the byte count is an estimate until the "
                        f"build places them")
    return errors, checked, glyph, warnings


_FAMILY_MODULES = {"umn": "txfam_umn", "evt": "txfam_evt", "ovl": "txfam_ovl", "fix": "txfam_fix"}


def _family_rules(row: dict, text: str, iso: str):
    """The build's own per-row rules (speaker names, message buffers, choice
    counts, field sizes, shared budgets) plus txfam_ovl's notes on strings
    two printers draw, so the editor says what the build will say.
    -> (errors, warnings)."""
    name = _FAMILY_MODULES.get(row.get("fam") or row["id"].split(":", 1)[0])
    if name is None:
        return [], []
    try:
        import importlib
        mod = importlib.import_module(name)
        errors = list(mod.validate({row["id"]: text}, iso).get(row["id"], []))
        warnings: List[str] = []
        if hasattr(mod, "warnings") and row.get("font") in ("battle", "ascii"):
            warnings = [w for w in mod.warnings({row["id"]: text}, iso).get(row["id"], [])
                        if "two printers" in w or "tag label" in w]
    except Exception as exc:
        return [f"family check failed: {type(exc).__name__}: {exc}"], []
    return errors, warnings


_EVT_TAG = re.compile(rb"/\[[^\]]*\]")
_CHAT_ESC = re.compile(rb"\\\d+")


def _png_urls(pngs) -> List[str]:
    return ["data:image/png;base64," + base64.b64encode(p).decode("ascii") for p in pngs]


def preview_png(row: dict, text: str, iso: str, target: Optional[dict] = None) -> dict:
    """{"pngs": [data URL per line], "png": first, "face": "font0"|"font1"}
    (+ "font_label" when new letters were drawn from the target's font)"""
    if target and target.get("kind", "builtin") != "builtin" and row["font"] == "field":
        import txtarget as tt
        face = _face(row) if (row.get("face") or row.get("measure")) else \
            ("font0" if row.get("ctl") == "evt" else "font1")
        if tt.coverage(text, target, "field", face)["glyph"]:
            if not target.get("font"):
                raise ValueError("these letters are drawn from a font file: choose one in step 2 "
                                 "(Language setup) to see them")
            ctl = row.get("ctl") or "c0"
            shown = text
            if ctl == "evt":
                shown = re.sub(r"/\[[^\]\n]*\]", "", text)
            elif ctl == "chat":
                shown, ctl = re.sub(r"\\\d+", "", text), "c0"
            try:
                pngs, _enc = tt.preview(iso, target, shown, "field", face, ctl)
            except tt.TargetError as exc:
                raise ValueError(str(exc)) from None
            urls = _png_urls(pngs)
            return {"pngs": urls, "png": urls[0] if urls else None, "face": face, "warnings": [],
                    "font_label": "preview with " + _font_label(target["font"], target.get("face", 0))}
    if row["font"] == "battle":
        raise ValueError("battle HUD strings are drawn from the stat.xtx grid, not font0/font1; "
                         "no preview here")
    if row["font"] == "ascii":
        raise ValueError("this string is drawn by the ASCII-only ribbon font; no preview here")
    face = _face(row) if (row.get("face") or row.get("measure")) else \
        ("font0" if row.get("ctl") == "evt" else "font1")
    tc = _txcodec()
    warnings: List[str] = []
    if tc is not None:
        data, warnings = tc.from_display(text, row["font"], row["ctl"], face)
    else:
        from textpack import encode_text
        data = encode_text(text)
    if row.get("ctl") == "evt":
        data = _EVT_TAG.sub(b"", data)
    elif row.get("ctl") == "chat":
        data = _CHAT_ESC.sub(b"", data)
    from langpack import render_line
    font = _fonts(iso)[face]
    if tc is not None and tc.missing_capital_accents(font, face):
        # the build redraws these capitals (txcodec.fix_capital_accents); show that
        key = f"{iso}|{face}|fixed"
        if key not in _FONTS:
            _FONTS[key] = tc.fix_capital_accents(font, face)
        font = _FONTS[key]
    if tc is not None and tc.needs_metric_fix(text, font, face):
        # the build gives — ‾ _ real metrics when a translation uses them
        font = tc.fix_unmeasured(font, face)
    pngs = []
    for line in data.split(b"\n"):
        png = render_line(font, line, 2)
        pngs.append("data:image/png;base64," + base64.b64encode(png).decode("ascii"))
    return {"pngs": pngs, "png": pngs[0] if pngs else None, "face": face,
            "warnings": list(warnings)}


# ---------------------------------------------------------------------------
# step 2, Language setup: source text, target language, font, try-it preview
# ---------------------------------------------------------------------------
# Everything here reads; nothing writes. The page keeps the choice in the
# project (project["target"], project["source"], project["language_name"]).
# txtarget is being extended with a language registry (languages(query),
# detect_scripts(text), profile_for_custom(name, sample), capacity(...));
# each is used when present and replaced by a smaller local answer when not.

_PROFILE_KEYS = ("code", "name", "endonym", "kind", "script", "rtl", "alphabet", "lacking",
                 "sample", "fonts", "registry")


def _clean_profile(p: dict) -> dict:
    out = {k: p.get(k) for k in _PROFILE_KEYS if k in p}
    out.setdefault("code", "")
    out.setdefault("name", out["code"])
    out.setdefault("endonym", "")
    out.setdefault("kind", "builtin")
    out.setdefault("script", "")
    out["rtl"] = bool(out.get("rtl"))
    for k in ("alphabet", "lacking", "sample"):
        out[k] = str(out.get(k) or "")
    out["fonts"] = list(out.get("fonts") or [])
    for k in ("script_name", "aliases", "region", "regions", "speakers", "notes"):
        if p.get(k):
            out[k] = p[k]
    if not out.get("script_name"):
        out["script_name"] = _script_display(out["script"], out["sample"] + out["alphabet"])
    return out


def _script_display(code: str, sample: str = "") -> str:
    """A script's name for people: the registry's when it has one, else the
    Unicode name of the sample's letters (Cherokee, Devanagari, Nko...)."""
    import txtarget as tt
    names = getattr(tt, "SCRIPT_NAMES", None)
    if isinstance(names, dict) and names.get(code):
        return str(names[code])
    found = _scripts_in(sample)
    if found:
        return found[0]["name"]
    return code


_UNAME_RE = re.compile(r"^(.*?) (?:CAPITAL |SMALL )?(?:LETTER|SYLLABLE|CHARACTER|DIGIT|VOWEL|SIGN|"
                       r"MARK|LIGATURE|IDEOGRAPH|NUMBER|CONSONANT|TONE|ABBREVIATION|MEDIAL|COMBINING|"
                       r"FINAL|INITIAL|PUNCTUATION|SUBJOINED|INDEPENDENT|DEPENDENT)\b")


def _scripts_in(text: str) -> List[dict]:
    """[{"name", "count", "letters"}] of the scripts of ``text``'s letters, by
    the Unicode character names (no table): most letters first, Latin last
    unless it is the only one."""
    import unicodedata
    counts: Counter = Counter()
    letters: Dict[str, str] = {}
    for c in dict.fromkeys(text or ""):
        cat = unicodedata.category(c)
        if not (cat.startswith("L") or cat in ("Mn", "Mc", "Nd")) or ord(c) < 0x80 and cat == "Nd":
            continue
        try:
            nm = unicodedata.name(c)
        except ValueError:
            continue
        if nm.startswith("CJK "):
            key = "Han"
        else:
            m = _UNAME_RE.match(nm)
            key = (m.group(1) if m else nm.split()[0]).title()
        n = sum(1 for x in text if x == c)
        counts[key] += n
        letters[key] = letters.get(key, "") + c
    # a name cut short by an unusual character name ("OL CHIKI MU TTUDDAG" -> "Ol")
    # joins the longer name it starts ("Ol Chiki")
    for k in list(counts):
        longer = [o for o in counts if o != k and o.startswith(k + " ")]
        if longer:
            counts[longer[0]] += counts.pop(k)
            letters[longer[0]] += letters.pop(k)
    out = [{"name": k, "count": n, "letters": letters[k]} for k, n in counts.most_common()]
    out.sort(key=lambda d: (d["name"] == "Latin" and len(out) > 1, -d["count"]))
    return out


def list_languages(query: str = "") -> List[dict]:
    """Target languages for the setup list (txtarget.languages when present,
    else TARGETS filtered by English name, endonym or code)."""
    import txtarget as tt
    q = (query or "").strip().lower()
    if hasattr(tt, "languages"):
        try:
            got = tt.languages(query or "")
            return [_clean_profile(p) for p in got if isinstance(p, dict) and p.get("code") != "other"]
        except Exception:
            pass
    out = []
    for p in tt.TARGETS:
        if p["code"] == "other":
            continue
        hay = " ".join((p["code"], p["name"], p.get("endonym", ""))).lower()
        if not q or q in hay:
            out.append(_clean_profile(p))
    return out


def custom_language(name: str, sample: str, code: str = "") -> dict:
    """The kind, scripts and direction of a language the list lacks, from a
    sample sentence (txtarget.profile_for_custom when present)."""
    import unicodedata
    import txcodec as tc
    import txtarget as tt
    sample = unicodedata.normalize("NFC", sample or "")
    scripts = _scripts_in(sample)
    prof = None
    if hasattr(tt, "profile_for_custom"):
        try:
            prof = tt.profile_for_custom(name, sample)
        except Exception:
            prof = None
    if not isinstance(prof, dict):
        iso_script = ""
        if hasattr(tt, "detect_scripts"):
            try:
                ds = tt.detect_scripts(sample)
                if isinstance(ds, dict):
                    ds = sorted(ds.items(), key=lambda kv: -(kv[1] if isinstance(kv[1], (int, float)) else 0))
                for it in ds or []:
                    sc = it.get("script") if isinstance(it, dict) else (it[0] if isinstance(it, (list, tuple)) else it)
                    if sc and sc not in ("Zyyy", "Zinh", "Latn"):
                        iso_script = str(sc)
                        break
                    if sc == "Latn" and not iso_script:
                        iso_script = "Latn"
            except Exception:
                iso_script = ""
        letters = "".join(c for c in dict.fromkeys(sample) if not c.isspace())
        blt = tt.coverage(letters, "builtin")
        scr = tt.coverage(letters, "script")
        names = {d["name"] for d in scripts}
        if names & {"Hiragana", "Katakana"}:
            kind = "japanese"           # kana: the retail fonts' own Japanese, rare kanji drawn
        elif names - {"Latin"}:
            kind = "script"             # any other script: every letter drawn from a font
        elif not blt["impossible"]:
            kind = "builtin"            # Latin letters the accent block already has
        else:
            kind = "script-hybrid"      # Latin with a few letters drawn from a font
        rtl_n = sum(1 for c in sample if unicodedata.bidirectional(c) in ("R", "AL"))
        ltr_n = sum(1 for c in sample if unicodedata.bidirectional(c) == "L")
        prof = {"code": code or "other", "name": name or "My language", "endonym": "", "kind": kind,
                "script": iso_script if iso_script and (iso_script != "Latn" or kind != "script") else "",
                "rtl": rtl_n > ltr_n, "alphabet": "", "sample": sample,
                "lacking": scr["glyph"] if kind != "builtin" else ""}
    prof = _clean_profile(prof)
    if code:
        prof["code"] = code
    if name:
        prof["name"] = name
    prof["sample"] = prof["sample"] or sample
    prof["scripts"] = scripts
    prof["custom"] = True
    if not prof.get("script_name"):
        prof["script_name"] = scripts[0]["name"] if scripts else ""
    elif scripts and prof["script_name"] == prof.get("script"):
        prof["script_name"] = scripts[0]["name"] if scripts[0]["name"] != "Latin" or len(scripts) == 1 \
            else prof["script_name"]
    return prof


def resolve_target_body(t: Optional[dict], language: str = "") -> dict:
    """project["target"] as the page sends it -> txtarget.resolve_target."""
    import txtarget as tt
    t = dict(t or {})
    proj = {"target": {k: v for k, v in t.items() if k in tt._TARGET_KEYS}, "language": language or t.get("code", "")}
    return tt.resolve_target(proj)


def needed_letters(target: dict, extra: str = "") -> str:
    """Letters of the target language (alphabet, sample, the script's probe
    letters, plus ``extra``: the project's own text) that need a glyph."""
    import txtarget as tt
    prof = tt.profile(target.get("code", "")) or {}
    probe = getattr(tt, "_PROBE", {}).get(target.get("script") or prof.get("script") or "", "")
    text = (prof.get("alphabet") or "") + (prof.get("sample") or "") + probe + (target.get("sample") or "") + (extra or "")
    cov = tt.coverage(text, target)
    return "".join(c for c in cov["glyph"] if _drawn(c))


_FONT_ALL: Dict[str, object] = {"ready": False, "faces": None, "thread": None}


def _all_faces() -> List[dict]:
    """Every installed outline face with its cmap (built once, ~3 s)."""
    import txtarget as tt
    with _CMAP_LOCK:
        if _FONT_ALL["faces"] is not None:
            return _FONT_ALL["faces"]
    faces = []
    for f in tt._font_index():
        cm = _cmap(f["path"], f["face"])
        if cm:
            faces.append(dict(f, _cmap=cm))
    with _CMAP_LOCK:
        _FONT_ALL["faces"] = faces
    return faces


def _warm_fonts() -> None:
    """Read every installed font's cmap in the background (once), so the
    font list of the Language setup answers at once."""
    with _CMAP_LOCK:
        if _FONT_ALL["thread"] is not None or FAKE:
            return
        _FONT_ALL["thread"] = threading.Thread(target=lambda: _safe(_all_faces), daemon=True)
        _FONT_ALL["thread"].start()


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None


def _font_label(path: str, face: int = 0) -> str:
    """The font's own family (and style unless Regular), from the installed
    index or the file's name table; the file name as a last resort."""
    try:
        import txtarget as tt
        for f in tt._font_index():
            if f["path"] == path and f["face"] == int(face or 0):
                st = f.get("style") or ""
                return f["family"] + ("" if st.lower() in ("regular", "") else " " + st)
        faces = tt._open_faces(path)
        ft = faces[int(face or 0)]
        fam, st = tt._name(ft, 1), tt._name(ft, 2)
        if fam:
            return fam + ("" if st.lower() in ("regular", "") else " " + st)
    except Exception:
        pass
    return Path(path).name


# Ties between fonts that draw every letter go to plain, widely installed
# sans faces (they stay readable at the game's 20-odd pixel size), and away
# from monospace, typewriter, display and condensed families.
_CLEAR_FAMILIES = ("noto sans", "helvetica neue", "helvetica", "arial", "pt sans", "segoe ui", "dejavu sans",
                   "liberation sans", "open sans", "lucida grande", "verdana", "tahoma", "geneva", "apple sd gothic",
                   "hiragino sans", "pingfang", "kohinoor", "geeza pro", "sf arabic", "arial hebrew", "thonburi",
                   "kefa", "mshtakan", "kailasa")
_ODD_WORDS = ("mono", "typewriter", "courier", "black", "narrow", "condensed", "comic", "chalk", "marker",
              "papyrus", "script", "brush", "stencil", "impact", "trattatello", "herculanum", "zapfino",
              "unicode ms", "symbol", "dings", "webdings", "bodoni", "didot", "rounded")


def rank_fonts(chars: str, prefer: List[str], limit: int = 14) -> List[dict]:
    """Installed faces ranked by how many of ``chars`` they draw; ties go to a
    family named after the language or script (``prefer``), then Noto, then
    Regular styles. -> [{path, face, family, style, label, covers, missing}]"""
    need = [c for c in dict.fromkeys(chars) if _drawn(c)]
    if not need:
        return []
    words = [w.lower() for w in prefer if w and len(w) > 2]
    rows = []
    for f in _all_faces():
        cm = f["_cmap"]
        have = sum(1 for c in need if ord(c) in cm)
        if not have:
            continue
        fam = f["family"] or Path(f["path"]).stem
        named = any(w in (fam + " " + Path(f["path"]).stem).lower().replace(" ", "")
                    or w in fam.lower() for w in [x.replace(" ", "") for x in words] + words)
        low = fam.lower()
        clear = next((i for i, c in enumerate(_CLEAR_FAMILIES) if low.startswith(c)), len(_CLEAR_FAMILIES))
        odd = any(w in low for w in _ODD_WORDS)
        rows.append((-(have / len(need)), "nastaliq" in low,
                     fam.startswith(".") or "lastresort" in low, not named,
                     not f.get("noto"), odd, (f.get("style") or "").lower() not in ("regular", "w3", "book", "roman", ""),
                     clear, low, f["path"], f["face"], f, have))
    rows.sort(key=lambda r: r[:11])
    out, seen = [], set()
    for r in rows:
        f, have = r[11], r[12]
        key = (f["family"], f.get("style"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"path": f["path"], "face": f["face"], "family": f["family"], "style": f.get("style", ""),
                    "label": _font_label(f["path"], f["face"]), "covers": round(have / len(need), 4),
                    "missing": "".join(c for c in need if ord(c) not in f["_cmap"])[:40]})
        if len(out) >= limit:
            break
    return out


def font_info(path: str, chars: str = "") -> dict:
    """Faces of one font file and how much of ``chars`` each draws."""
    import txtarget as tt
    p = Path(path).expanduser()
    if not p.is_file():
        raise FileNotFoundError(f"no font file at {p}")
    if p.suffix.lower() not in (".ttf", ".otf", ".ttc", ".otc"):
        raise ValueError("a font file is .ttf, .otf, .ttc or .otc")
    try:
        faces = tt._open_faces(str(p))
    except ImportError:
        raise ValueError(tt.shaping_available()[1]) from None
    except Exception as exc:
        raise ValueError(f"cannot read the font {p.name}: {exc}") from None
    need = [c for c in dict.fromkeys(chars or "") if _drawn(c)]
    out = []
    for i, ft in enumerate(faces):
        cm = _cmap(str(p), i) or frozenset()
        have = sum(1 for c in need if ord(c) in cm)
        fam, st = tt._name(ft, 1), tt._name(ft, 2)
        out.append({"face": i, "family": fam, "style": st,
                    "label": (fam or p.stem) + ("" if st.lower() in ("regular", "") else " " + st),
                    "covers": round(have / len(need), 4) if need else None,
                    "missing": "".join(c for c in need if ord(c) not in cm)[:40]})
    return {"path": str(p), "faces": out}


def target_preview(iso: str, target_in: dict, text: str, face: str = "font1") -> dict:
    """The Try-it box: ``text`` drawn as the game would after a build, with
    the target's font for the letters the retail font lacks."""
    import txcodec as tc
    import txtarget as tt
    t = resolve_target_body(target_in, target_in.get("code", ""))
    face = face if face in ("font0", "font1") else "font1"
    text = (text or "")[:400]
    ttf = t.get("font") or None
    if ttf and not Path(ttf).is_file():
        return {"error": f"the font file {ttf} does not exist"}
    cov = tt.coverage(text, t, tc.FIELD, face, ttf=ttf, ttf_face=t.get("face", 0))
    info = {"native": cov["native"], "glyph": "".join(c for c in cov["glyph"] if _drawn(c)),
            "impossible": cov["impossible"], "missing": cov.get("missing_in_font") or "",
            "kind": t["kind"], "rtl": bool(t.get("rtl")),
            "font_label": _font_label(ttf, t.get("face", 0)) if ttf else ""}
    if cov["impossible"]:
        what = ("the game font has no letter for it and this language is set up as built in: "
                "choose a language that draws new letters, or \"My language isn't listed\"") \
            if t["kind"] == "builtin" else "no build can draw it"
        return dict(info, error=f"{_char_list(cov['impossible'])}: {what}")
    if info["glyph"] and not ttf:
        return dict(info, error=f"{_char_list(info['glyph'])} are drawn from a font file: choose one "
                                f"below", need_font=True)
    if info["missing"]:
        return dict(info, error=f"{info['font_label']} has no letter for {_char_list(info['missing'])}")
    if not text.strip():
        return dict(info, pngs=[])
    try:
        if t["kind"] == "builtin" or not info["glyph"]:
            with tc.glyph_codes():
                data, _w = tc.from_display(text, tc.FIELD, "c0", face)
            from langpack import render_line
            font = _fonts(iso)[face]
            pngs = [render_line(font, line, 2) for line in data.split(b"\n")]
        else:
            pngs, _enc = tt.preview(iso, t, text, tc.FIELD, face, "c0")
    except (tt.TargetError, tc.TxEncodeError, ValueError) as exc:
        return dict(info, error=str(exc))
    return dict(info, pngs=_png_urls(pngs))


def target_capacity(iso: str, target_in: dict, edits: Dict[str, str]) -> Optional[object]:
    """txtarget.capacity(target, rows, edits) when the module has it."""
    import txtarget as tt
    if not hasattr(tt, "capacity"):
        return None
    t = resolve_target_body(target_in, target_in.get("code", ""))
    cat = get_catalog(iso)
    rows = cat["_index"]
    edits = {k: str(v) for k, v in (edits or {}).items() if k in rows and v}
    try:
        return tt.capacity(t, rows, edits)
    except TypeError:
        return tt.capacity(t, {k: rows[k] for k in edits}, edits)


# Japanese source (txsource): aligned once per pair of discs, in a thread so
# the page can show progress; the result stays in memory (and txsource keeps
# its own disk cache, so a relaunch costs ~1 s).
_SOURCE_JOBS: Dict[tuple, dict] = {}


def _source_key(usa: str, src: str) -> tuple:
    a, b = Path(usa).expanduser().resolve(), Path(src).expanduser().resolve()
    return (str(a), a.stat().st_mtime_ns, str(b), b.stat().st_mtime_ns)


def source_job(usa: str, src: str) -> dict:
    import txsource
    if not Path(usa).expanduser().is_file():
        raise FileNotFoundError(f"no such ISO: {usa}")
    if not Path(src).expanduser().is_file():
        raise FileNotFoundError(f"no such ISO: {src}")
    key = _source_key(usa, src)
    with _LOCK:
        job = _SOURCE_JOBS.get(key)
        if job is None:
            job = {"state": "running", "log": "starting", "result": None, "error": None, "wire": {}}
            _SOURCE_JOBS[key] = job

            def run():
                try:
                    rows = get_catalog(key[0])["rows"] if not FAKE else None
                    job["log"] = "aligning the two discs (about 12 s the first time)"
                    res = txsource.align(key[0], key[2], rows=rows,
                                         log=lambda m: job.__setitem__("log", str(m)[:200]))
                    job["result"] = res
                    job["state"] = "done"
                except Exception as exc:   # SourceError carries a readable message
                    job["error"] = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}: {exc}"
                    job["state"] = "error"
            threading.Thread(target=run, daemon=True).start()
    return job


def source_wire(job: dict, gz: bool) -> bytes:
    import gzip
    with _LOCK:
        if (gz,) not in job["wire"]:
            raw = job["wire"].get((False,))
            if raw is None:
                r = job["result"]
                raw = json.dumps({"state": "done", "source": r.get("source"), "texts": r.get("texts", {}),
                                  "partial": r.get("partial", []), "stats": r.get("stats", {}),
                                  "by_cat": r.get("by_cat", {}), "notes": r.get("notes", [])},
                                 ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                job["wire"][(False,)] = raw
            if gz:
                job["wire"][(True,)] = gzip.compress(raw, 6)
        return job["wire"][(gz,)]


def identify_source(path: str) -> dict:
    import txsource
    p = Path(path).expanduser()
    if not p.is_file():
        return {"kind": None, "ok": False, "message": f"no file at {p}"}
    kind = txsource.identify_disc(str(p))
    msgs = {"reloaded": (True, "Xenosaga Episode I Reloaded (SLPS_290.05): the Japanese text is read from it"),
            "usa": (False, "this is the USA disc; the Japanese source is the Reloaded disc"),
            "jp-original": (False, "the 2002 original Japanese disc is recognised but not supported yet; "
                                   "use Xenosaga Episode I Reloaded"),
            None: (False, "not a Xenosaga Episode I disc this editor can read the Japanese from")}
    ok, msg = msgs.get(kind, msgs[None])
    return {"kind": kind, "ok": ok, "message": msg, "path": str(p)}


# ---------------------------------------------------------------------------
# request plumbing
# ---------------------------------------------------------------------------

def _q(query) -> dict:
    if isinstance(query, dict):
        return {k: (v[0] if isinstance(v, list) else v) for k, v in query.items()}
    return {k: v[0] for k, v in parse_qs(query or "", keep_blank_values=True).items()}


def _host_ok(handler) -> bool:
    host = (handler.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _err(handler, status: int, msg: str) -> bool:
    handler._send_json(status, {"error": msg})
    return True


def page_html() -> str:
    from txeditor_page import EDITOR_HTML
    import txspeakers
    spk = json.dumps(txspeakers.load(), ensure_ascii=False).replace("</", "<\\/")
    return EDITOR_HTML.replace("__TX_TOKEN__", TOKEN).replace(
        "__TX_FAKE__", "true" if FAKE else "false").replace("__TX_SPEAKERS__", spk)


def handle_get(handler, path: str, query) -> bool:
    if path != "/editor" and not path.startswith("/tx/"):
        return False
    if not _host_ok(handler):
        return _err(handler, 403, "editor routes answer on the loopback address only")
    q = _q(query)
    try:
        if path == "/editor":
            handler._send(200, page_html(), "text/html; charset=utf-8")
            return True
        if path == "/tx/detect":
            handler._send_json(200, {"discs": [] if FAKE else detect_discs()})
            return True
        if path == "/tx/catalog":
            iso = q.get("iso", "")
            if not iso and not FAKE:
                return _err(handler, 400, "missing ?iso=")
            cat = get_catalog(iso)
            part = q.get("part", "main")
            if part == "rows":      # the old list-of-dicts shape, for scripts and debugging
                payload = {k: v for k, v in cat.items() if not k.startswith("_")}
                payload["fake"] = FAKE
                handler._send(200, json.dumps(payload, ensure_ascii=False), "application/json; charset=utf-8")
                return True
            if part not in ("main", "hidden"):
                return _err(handler, 400, "part must be main, hidden or rows")
            gz = "gzip" in (handler.headers.get("Accept-Encoding") or "").lower()
            body = catalog_part_bytes(cat, part, gz)
            headers = {"Vary": "Accept-Encoding"}
            if gz:
                headers["Content-Encoding"] = "gzip"
            handler._send(200, body, "application/json; charset=utf-8", headers)
            return True
        if path == "/tx/project":
            p = q.get("path", "")
            if not p:
                return _err(handler, 400, "missing ?path=")
            fp = Path(p).expanduser()
            if not fp.is_file():
                return _err(handler, 404, f"no project file at {fp}")
            handler._send_json(200, {"path": str(fp), "project": txproject.load(str(fp)),
                                     "mtime": _mtime(fp)})
            return True
        if path == "/tx/languages":
            import txtarget
            _warm_fonts()
            ok, msg = txtarget.shaping_available()
            handler._send_json(200, {"languages": list_languages(q.get("q", "")),
                                     "registry": hasattr(txtarget, "languages"),
                                     "capacity": hasattr(txtarget, "capacity"),
                                     "shaping": {"ok": ok, "message": msg}})
            return True
        if path == "/tx/identify-source":
            handler._send_json(200, identify_source(q.get("path", "")))
            return True
        if path == "/tx/source":
            usa, src = q.get("usa", ""), q.get("src", "")
            if not usa or not src:
                return _err(handler, 400, "missing ?usa= or ?src=")
            job = source_job(usa, src)
            if job["state"] == "running":
                handler._send_json(200, {"state": "running", "log": job["log"]})
            elif job["state"] == "error":
                with _LOCK:      # let the next request try again (another disc, a fixed path)
                    _SOURCE_JOBS.pop(_source_key(usa, src), None)
                handler._send_json(200, {"state": "error", "error": job["error"]})
            else:
                gz = "gzip" in (handler.headers.get("Accept-Encoding") or "").lower()
                headers = {"Vary": "Accept-Encoding"}
                if gz:
                    headers["Content-Encoding"] = "gzip"
                handler._send(200, source_wire(job, gz), "application/json; charset=utf-8", headers)
            return True
        if path == "/tx/export-tsv":
            p = q.get("path", "")
            if not p:
                return _err(handler, 400, "missing ?path=")
            project = txproject.load(str(Path(p).expanduser()))
            cat = get_catalog(q["iso"]) if q.get("iso") else _current()
            if cat is None:
                return _err(handler, 409, "no catalog loaded yet; open a retail ISO first")
            text = txproject.export_tsv(project, cat["rows"], only_translated=q.get("only") == "1")
            name = Path(p).stem + ".tsv"
            handler._send(200, text, "text/tab-separated-values; charset=utf-8",
                          {"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}"})
            return True
    except txproject.ProjectError as exc:
        return _err(handler, 400, str(exc))
    except (FileNotFoundError, LookupError) as exc:
        return _err(handler, 404, str(exc))
    except Exception as exc:
        return _err(handler, 500, f"{type(exc).__name__}: {exc}")
    return _err(handler, 404, "no such editor route")


_PROJECT_LOCK = threading.Lock()


def _body_target(body: dict) -> Optional[dict]:
    """The resolved target a check / preview request names (``target`` = the
    project's target, ``language`` = its language), or None for the builtin
    path (no target, or one that draws nothing new)."""
    if FAKE or not (body.get("target") or body.get("language")):
        return None
    try:
        t = resolve_target_body(body.get("target"), str(body.get("language") or ""))
    except Exception:
        return None
    return t if t.get("kind", "builtin") != "builtin" else None


def _backups(handler, body: dict) -> bool:
    """POST /tx/backups {path, action, ...}: list | checkpoint (label, project)
    | restore (backup) | copy (dest, project, overwrite). See txbackup."""
    p = str(body.get("path") or "")
    if not p or Path(p).suffix.lower() != ".json":
        return _err(handler, 400, "missing project path (.json)")
    fp = Path(p).expanduser()
    act = str(body.get("action") or "list")
    data = None
    if body.get("project") is not None:
        data = txproject.dumps(txproject.normalise(body["project"])).encode("utf-8")
    try:
        with _PROJECT_LOCK:
            if act == "list":
                pass
            elif act == "checkpoint":
                made = txbackup.checkpoint(str(fp), str(body.get("label") or ""), data)
                handler._send_json(200, {"made": made, "backups": txbackup.list_backups(str(fp)),
                                         "folder": str(txbackup.folder(str(fp)))})
                return True
            elif act == "restore":
                before = txbackup.restore(str(fp), str(body.get("backup") or ""))
                handler._send_json(200, {"before": before, "project": txproject.load(str(fp)),
                                         "mtime": _mtime(fp), "backups": txbackup.list_backups(str(fp)),
                                         "folder": str(txbackup.folder(str(fp)))})
                return True
            elif act == "copy":
                if data is None:
                    data = fp.read_bytes()
                out = txbackup.save_copy(data, str(body.get("dest") or ""), bool(body.get("overwrite")))
                handler._send_json(200, {"copy": out})
                return True
            else:
                return _err(handler, 400, f"unknown action {act!r}")
    except (txbackup.BackupError, txproject.ProjectError) as exc:
        return _err(handler, 422, str(exc))
    except OSError as exc:
        return _err(handler, 500, f"{type(exc).__name__}: {exc}")
    handler._send_json(200, {"backups": txbackup.list_backups(str(fp)), "folder": str(txbackup.folder(str(fp)))})
    return True


def _mtime(fp: Path) -> str:
    """Version stamp of a project file (a string: ns mtimes exceed JS integers)."""
    try:
        return str(fp.stat().st_mtime_ns)
    except OSError:
        return ""


def handle_post(handler, path: str, body_bytes: bytes, content_type: str = "") -> bool:
    if path != "/editor" and not path.startswith("/tx/"):
        return False
    if not _host_ok(handler):
        return _err(handler, 403, "editor routes answer on the loopback address only")
    if not secrets.compare_digest(handler.headers.get("X-TX-Token", ""), TOKEN):
        return _err(handler, 403, "missing or wrong X-TX-Token (reload the editor page)")
    if len(body_bytes or b"") > MAX_BODY:
        return _err(handler, 413, "request too large")
    try:
        body = json.loads((body_bytes or b"{}").decode("utf-8") or "{}")
    except ValueError as exc:
        return _err(handler, 400, f"body must be JSON ({exc})")
    if not isinstance(body, dict):
        return _err(handler, 400, "body must be a JSON object")
    try:
        if path == "/tx/project":
            p = body.get("path") or ""
            if not p:
                return _err(handler, 400, "missing path")
            fp = Path(p).expanduser()
            if fp.suffix.lower() != ".json":
                return _err(handler, 400, "project files must end in .json")
            with _PROJECT_LOCK:
                if body.get("create") and fp.exists():
                    return _err(handler, 409, f"{fp} already exists; open it instead")
                # last-writer-wins would let a second tab (or another program)
                # silently drop translations: a save names the version it
                # started from, and a file changed since then is not replaced
                base = body.get("mtime")
                if base is not None and fp.exists() and str(base) != _mtime(fp):
                    handler._send_json(409, {
                        "error": f"{fp.name} was changed elsewhere (another editor tab?) since "
                                 f"it was opened here; not overwritten",
                        "conflict": True, "project": txproject.load(str(fp)),
                        "mtime": _mtime(fp)})
                    return True
                project = body.get("project") or txproject.new_project(body.get("language") or "fr")
                if not project.get("retail_id") and body.get("iso") and not FAKE:
                    project["retail_id"] = txproject.retail_id(body["iso"])
                backup = None
                if not body.get("create"):
                    try:                 # the file as it was, at most every 10 minutes (txbackup)
                        backup = txbackup.auto(str(fp))
                    except OSError:
                        backup = None
                txproject.save(str(fp), project)
                handler._send_json(200, {"ok": True, "path": str(fp), "backup": backup,
                                         "project": txproject.load(str(fp)), "mtime": _mtime(fp)})
            return True
        if path == "/tx/backups":
            return _backups(handler, body)
        if path == "/tx/custom-language":
            handler._send_json(200, custom_language(str(body.get("name", "")), str(body.get("sample", "")),
                                                    str(body.get("code", "") or "")))
            return True
        if path == "/tx/fonts":
            t = resolve_target_body(body.get("target"), str(body.get("language", "")))
            t["sample"] = str((body.get("target") or {}).get("sample", "") or "")
            chars = needed_letters(t, str(body.get("chars", ""))[:20000])
            prefer = [str(x) for x in body.get("prefer") or []] + [t.get("name", "")]
            # the right regional forms of Han characters and the usual system face per script
            sc = str(t.get("script") or "")
            code = str(t.get("code") or "")
            prefer += {"Hans": ["PingFang SC", "Heiti SC"], "Hant": ["PingFang TC", "Heiti TC"],
                       "Kore": ["Apple SD Gothic Neo"], "Jpan": ["Hiragino Sans"]}.get(sc, [])
            if code == "ja":
                prefer += ["Hiragino Sans"]
            elif code.startswith("zh") and not sc:
                prefer += ["PingFang SC" if "Hant" not in code else "PingFang TC"]
            handler._send_json(200, {"chars": chars, "fonts": rank_fonts(chars, prefer)})
            return True
        if path == "/tx/font-info":
            try:
                handler._send_json(200, font_info(str(body.get("path", "")), str(body.get("chars", ""))[:20000]))
            except ValueError as exc:
                return _err(handler, 422, str(exc))
            return True
        if path == "/tx/target-preview":
            iso = body.get("iso") or (_current() or {}).get("_iso")
            if not iso:
                return _err(handler, 409, "choose the USA disc first (step 1): the preview uses its fonts")
            handler._send_json(200, target_preview(str(iso), body.get("target") or {},
                                                   str(body.get("text", "")), str(body.get("face", "font1"))))
            return True
        if path == "/tx/capacity":
            iso = body.get("iso") or (_current() or {}).get("_iso")
            if not iso:
                return _err(handler, 409, "choose the USA disc first (step 1)")
            res = target_capacity(str(iso), body.get("target") or {}, body.get("edits") or {})
            handler._send_json(200, {"available": res is not None, "capacity": res})
            return True
        if path == "/tx/check":
            cat = _rows_for(body)
            idx = cat["_index"]
            iso = cat.get("_iso")
            target = _body_target(body)
            out = {}
            for it in body.get("items") or []:
                row = idx.get(it.get("id"))
                if row is None:
                    out[it.get("id")] = {"ok": False, "errors": ["unknown id"], "warnings": [],
                                         "nbytes": None, "px": None}
                    continue
                out[row["id"]] = check_one(row, str(it.get("text", "")), iso, target)
            handler._send_json(200, {"results": out})
            return True
        if path == "/tx/wrap":
            cat = _rows_for(body)
            row = cat["_index"].get(body.get("id"))
            if row is None:
                return _err(handler, 404, "unknown id")
            text = str(body.get("text", ""))
            target = _body_target(body)
            if FAKE or not cat.get("_iso") or (target and target.get("kind", "builtin") != "builtin"):
                handler._send_json(200, {"text": text, "changed": False})
                return True
            new = wrap_text(row, text, str(cat["_iso"]))
            handler._send_json(200, {"text": new, "changed": new != text})
            return True
        if path == "/tx/preview":
            cat = _rows_for(body)
            row = cat["_index"].get(body.get("id"))
            if row is None:
                return _err(handler, 404, "unknown id")
            iso = cat.get("_iso")
            if FAKE and not (iso and Path(str(iso)).is_file()):
                return _err(handler, 409, "TX_FAKE: give a real ISO path to render previews")
            try:
                res = preview_png(row, str(body.get("text", "")), str(iso), _body_target(body))
            except ValueError as exc:   # includes txcodec.TxEncodeError
                return _err(handler, 422, str(exc))
            handler._send_json(200, res)
            return True
        if path == "/tx/import-tsv":
            cat = _rows_for(body)
            project, n, problems = txproject.import_tsv(
                str(body.get("tsv", "")), body.get("project") or txproject.new_project(),
                cat["rows"], state=body.get("state") or "draft")
            handler._send_json(200, {"project": project, "applied": n, "problems": problems})
            return True
        if path == "/tx/group":
            op = body.get("op")
            if op == "format":
                items = [(int(n), str(t)) for n, t in body.get("items") or []]
                handler._send_json(200, {"text": txproject.format_group(items, body.get("language", ""))})
                return True
            if op == "parse":
                parsed, notes = txproject.parse_group(str(body.get("text", "")))
                handler._send_json(200, {"items": {str(k): v for k, v in parsed.items()},
                                         "notes": notes})
                return True
            return _err(handler, 400, "op must be 'format' or 'parse'")
    except txproject.ProjectError as exc:
        return _err(handler, 400, str(exc))
    except (FileNotFoundError, LookupError) as exc:
        return _err(handler, 404, str(exc))
    except Exception as exc:
        return _err(handler, 500, f"{type(exc).__name__}: {exc}")
    return _err(handler, 404, "no such editor route")


# ---------------------------------------------------------------------------
# standalone harness (dev + selftest) — mirrors gui.Handler's helpers
# ---------------------------------------------------------------------------

def _make_server(port: int):
    import http.server
    import socketserver
    from urllib.parse import urlparse

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def _send(self, status, body, content_type="text/plain", extra_headers=None):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, status, payload):
            self._send(status, json.dumps(payload), "application/json")

        def do_GET(self):  # noqa: N802
            url = urlparse(self.path)
            if handle_get(self, url.path, url.query):
                return
            if url.path == "/":
                return self._send(302, "", extra_headers={"Location": "/editor"})
            if url.path in ("/detect", "/browse-fs"):
                import gui
                if url.path == "/detect":
                    return self._send_json(200, {"isos": gui.detect_isos(), "default_iso": ""})
                q = _q(url.query)
                return self._send_json(200, gui.list_dir(q.get("path", ""), q.get("filter") or None))
            self._send(404, "not found")

        def do_POST(self):  # noqa: N802
            url = urlparse(self.path)
            n = int(self.headers.get("Content-Length", "0") or 0)
            body = self.rfile.read(n) if n else b""
            if handle_post(self, url.path, body, self.headers.get("Content-Type", "")):
                return
            self._send(404, "not found")

    class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    return S(("127.0.0.1", port), H)


def selftest(iso: Optional[str]) -> int:
    """Drive every route through a real socket with urllib."""
    import tempfile
    import urllib.request
    global FAKE
    FAKE = not iso
    srv = _make_server(0)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    ok = True

    def check(cond, what):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + what)
        ok = ok and bool(cond)

    def req(method, path, body=None, token=True):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(base + path, data=data, method=method)
        r.add_header("Content-Type", "application/json")
        if token:
            r.add_header("X-TX-Token", TOKEN)
        try:
            with urllib.request.urlopen(r, timeout=600) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    st, raw = req("GET", "/tx/detect")
    check(st == 200 and isinstance(json.loads(raw).get("discs"), list), "GET /tx/detect")
    st, page = req("GET", "/editor")
    check(st == 200 and TOKEN.encode() in page and b"__TX_TOKEN__" not in page, "GET /editor injects token")
    isoq = quote(iso or "fake")
    import gzip
    import time
    from txcatalog import expand
    t0 = time.time()
    st, raw = req("GET", f"/tx/catalog?iso={isoq}")
    cat = json.loads(raw) if st == 200 else {}
    st2, raw2 = req("GET", f"/tx/catalog?iso={isoq}&part=hidden")
    hid = json.loads(raw2) if st2 == 200 else {"n": 0, "col": {}, "dict": {}}
    rows = (expand(cat) if cat.get("n") else []) + (expand(hid) if hid.get("n") else [])
    check(st == 200 and st2 == 200 and rows and len(rows) == cat.get("total"),
          f"GET /tx/catalog main {cat.get('n')} + hidden {hid.get('n')} rows "
          f"({len(raw):,} + {len(raw2):,} B, {time.time() - t0:.2f} s)")
    st, legacy = req("GET", f"/tx/catalog?iso={isoq}&part=rows")
    lrows = json.loads(legacy).get("rows", []) if st == 200 else []
    check(sorted(r["id"] for r in lrows) == sorted(r["id"] for r in rows), "part=rows has the same ids")
    r = urllib.request.Request(base + f"/tx/catalog?iso={isoq}", headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(r, timeout=600) as resp:
        enc, zbody = resp.headers.get("Content-Encoding"), resp.read()
    check(enc == "gzip" and gzip.decompress(zbody) == raw,
          f"gzip when accepted ({len(zbody):,} B)")
    edit = next((r for r in rows if not r["lock"] and r["font"] == "field"), rows[0] if rows else None)
    st, _ = req("POST", "/tx/check", {"items": [{"id": edit["id"], "text": "x"}]}, token=False)
    check(st == 403, "POST without token -> 403")
    st, raw = req("POST", "/tx/check", {"items": [{"id": edit["id"], "text": edit["src"]},
                                                 {"id": "nope", "text": "x"}]})
    res = json.loads(raw).get("results", {}) if st == 200 else {}
    check(st == 200 and edit["id"] in res and res["nope"]["ok"] is False, f"POST /tx/check {res.get(edit['id'])}")
    with tempfile.TemporaryDirectory() as td:
        pp = os.path.join(td, "proj (fr).json")
        proj = txproject.new_project("fr", cat.get("retail_id", ""))
        proj["translations"][edit["id"]] = {"text": "Élan « ¿sí? »\nœ", "state": "done"}
        st, _ = req("POST", "/tx/project", {"path": pp, "project": proj})
        st2, raw = req("GET", "/tx/project?path=" + quote(pp))
        check(st == 200 and st2 == 200 and json.loads(raw)["project"]["translations"] == proj["translations"],
              "project save/load")
        st, _ = req("POST", "/tx/project", {"path": pp, "create": True, "project": proj})
        check(st == 409, "create refuses to overwrite")
        # two tabs: B saves from the version it opened after A already saved over it
        m0 = json.loads(raw)["mtime"]
        a = dict(proj, translations={**proj["translations"], "x:a": {"text": "A", "state": "draft"}})
        st, ra = req("POST", "/tx/project", {"path": pp, "project": a, "mtime": m0})
        b = dict(proj, translations={**proj["translations"], "x:b": {"text": "B", "state": "draft"}})
        st2, rb = req("POST", "/tx/project", {"path": pp, "project": b, "mtime": m0})
        on_disk = txproject.load(pp)["translations"]
        check(st == 200 and st2 == 409 and json.loads(rb).get("conflict") and "x:a" in on_disk
              and "x:b" not in on_disk, "a save from a stale version is refused (409), not last-writer-wins")
        st, _ = req("POST", "/tx/project", {"path": pp, "project": b, "mtime": json.loads(ra)["mtime"]})
        check(st == 200, "a save naming the current version goes through")
        st, tsv = req("GET", f"/tx/export-tsv?path={quote(pp)}&iso={isoq}")
        st2, raw = req("POST", "/tx/import-tsv", {"tsv": tsv.decode("utf-8"), "project": txproject.new_project("fr")})
        back = json.loads(raw) if st2 == 200 else {}
        check(st == 200 and back.get("applied") == 1 and
              back["project"]["translations"][edit["id"]]["text"] == "Élan « ¿sí? »\nœ",
              f"TSV export/import round-trip ({back.get('problems')})")
    st, raw = req("POST", "/tx/group", {"op": "format", "items": [[1, "a\nb"], [2, "c"]]})
    txt = json.loads(raw)["text"]
    st2, raw = req("POST", "/tx/group", {"op": "parse", "text": "noise\n" + txt})
    check(json.loads(raw)["items"] == {"1": "a\nb", "2": "c"}, "group format/parse")
    if iso:
        # a later catalog load (another tab, another image) must not change what
        # a request that names its image is checked against
        with _LOCK:
            saved = dict(_CURRENT)
            _CATALOGS[("<other>", 0)] = {"rows": [], "_index": {}, "_iso": None}
            _CURRENT["key"], _CURRENT["iso"] = ("<other>", 0), None
        st, raw = req("POST", "/tx/check", {"items": [{"id": edit["id"], "text": edit["src"]}], "iso": iso})
        check(st == 200 and json.loads(raw)["results"][edit["id"]]["ok"],
              "check names its image: another catalog loaded later does not answer it")
        with _LOCK:
            _CURRENT.update(saved)
            del _CATALOGS[("<other>", 0)]
        byid = {r["id"]: r for r in rows}
        spk = next(r for r in rows if r["cat"] == "Speaker names" and not r["lock"])
        import txfam_evt
        menu = next(r for r in txfam_evt._rows_by_id(iso).values() if r.get("_items")
                    and not r["lock"] and r["src"].count("\n") == 1 and "/[" not in r["src"])
        twice = next((r for r in rows if r["fam"] == "ovl" and r["font"] == "battle" and not r["lock"]
                      and "field font" in r.get("ctx", "")), None)
        card = byid.get("fix:card:0:name")
        items = [{"id": spk["id"], "text": "Allen (ingénieur)"},
                 {"id": menu["id"], "text": "Allons-y"},
                 {"id": card["id"], "text": "Croiseur lourd de la Confédéré"},
                 {"id": edit["id"], "text": "Pourquoi\u202f? C’est «\u00a0incroyable\u00a0» !"}]
        if twice:
            items.append({"id": twice["id"], "text": "Élémentaire"})
        st, raw = req("POST", "/tx/check", {"items": items, "iso": iso})
        res = json.loads(raw)["results"]
        check(not res[spk["id"]]["ok"] and any("'('" in e for e in res[spk["id"]]["errors"]),
              "speaker name with '(' is an error in the editor, as in the build")
        check(not res[menu["id"]]["ok"], "choice list with a missing line is an error")
        check(not res[card["id"]]["ok"], "card name over 32 bytes is an error")
        check(res[edit["id"]]["ok"], f"French no-break spaces check OK ({res[edit['id']]['warnings'][:1]})")
        if twice:
            check(any("two printers" in w for w in res[twice["id"]]["warnings"]),
                  "a name two printers draw warns what each shows")
        st, raw = req("POST", "/tx/preview", {"id": edit["id"], "text": edit["src"]})
        prv = json.loads(raw) if raw else {}
        check(st == 200 and str(prv.get("png", "")).startswith("data:image/png;base64,"),
              f"POST /tx/preview ({st} {str(prv)[:120]})")
    srv.shutdown()
    print("selftest", "passed" if ok else "FAILED")
    return 0 if ok else 1


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("selftest", help="route test (fake catalog unless --iso)")
    s.add_argument("--iso")
    sub.add_parser("serve", help="serve /editor alone on $PORT (dev)")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest(a.iso)
    port = int(os.environ.get("PORT", "8932"))
    srv = _make_server(port)
    print(f"editor on http://127.0.0.1:{port}/editor  (TX_FAKE={'1' if FAKE else '0'})")
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
