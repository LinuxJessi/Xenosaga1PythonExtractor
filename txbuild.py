"""txbuild.py — build a translated ISO from a translation-editor project.

The editor (``/editor``) saves a project JSON (txproject): the retail id
of the disc it was made against and ``{row id: {"text", "state"}}``. This
module turns that into a patched copy of the retail image in one pass:

  1. check the project was made for this disc (``txproject.retail_id``);
  2. split the translations by family (row id prefix ``umn: evt: ovl:
     fix:``) and validate EVERY family against the retail image; any error
     stops the build with a per-id list before a single byte is written;
  3. clone the retail image to ``OUT.partial`` (an APFS clone on macOS,
     a reflink on Linux, a full copy elsewhere after a free-space check);
  4. apply umn, evt, ovl, fix in that order, each family reading its
     originals from the RETAIL image and writing the clone in place;
  4b. when the project's target needs letters the retail fonts lack (a new
     script, a Latin language with extra letters, level-2 kanji; see
     ``txtarget``), every translated string is encoded first with ONE glyph
     allocation over all families, and the new glyphs are drawn into font0,
     font1 (and the battle grid ``stat.xtx`` plus its byte table in OV01) in
     the same build; builtin languages skip this and build as before;
  5. when a field-font translation uses È É Ê Ë, redraw those capitals in
     font0 (``txcodec.fix_capital_accents``; retail font0 has a plain E in
     those cells), and when one uses — ‾ _ give those cells real metrics in
     both faces (``txcodec.fix_unmeasured``; retail stores the no-metrics
     pair and the engine draws a 5 px stub); patch the fonts same-size;
  6. re-read every edited row from the clone with the family readers,
     compare it to the text that was asked for, and check that no two
     objects of any chain (or ISO9660 file) overlap;
  7. rename ``OUT.partial`` to ``OUT``.

Only an untouched image can be the base: an image whose TOC already points
past a chain's end (objects relocated by an earlier build or import) is
refused, whatever the project's retail id says. ``OUT.lock`` keeps two builds
from writing the same output at once.

Why a ``.partial`` name: a build that fails half-way (disc full, a reader
that disagrees) must never leave a file under the final name that looks
like a finished translation. On any failure the partial file is deleted.

Why the clone is careful on macOS: ``cp -c`` silently falls back to a
full 8.5 GB copy when the target cannot clone (another volume, exFAT), so
``cp -c`` is only used when the output folder is on the retail image's
own APFS volume; otherwise the full copy goes through the free-space
check like any other platform.

The GUI never imports this module: its Build button runs
``cli.py tx-build`` as a Job like every other card.

    python3 txbuild.py build --project P.json --retail RETAIL.iso --out OUT.iso [--overwrite]
    python3 txbuild.py selftest --iso RETAIL [--scratch DIR]
    python3 txbuild.py selftest-targets --iso RETAIL [--scratch DIR] [--render DIR] [--cases ru,ar,ja,pl,fr]

A project may carry ``"target"`` (txtarget.normalise_target): the language
the translation goes into and, for one that needs new glyphs, the font they
are drawn from, e.g. ``{"code": "ru", "font": "/path/NotoSans-Regular.ttf"}``.
Without it the project's ``language`` picks the profile; unknown codes build
the builtin way.
"""
from __future__ import annotations

import argparse
import importlib
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import txcodec as tc  # noqa: E402
import txproject  # noqa: E402
import txtarget  # noqa: E402

FAMILIES = (("umn", "txfam_umn"), ("evt", "txfam_evt"), ("ovl", "txfam_ovl"), ("fix", "txfam_fix"))
SPARE = 1 << 30                       # free space kept on top of a full copy


class BuildError(tc.TxBuildError):
    """Build refused or failed; the message says why (per id for bad edits)."""


# ---------------------------------------------------------------------------
# cloning
# ---------------------------------------------------------------------------

def _mount_fstype(path: Path) -> Optional[str]:
    """macOS: filesystem type of the mount holding ``path`` (from mount(8))."""
    try:
        out = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    best, kind = "", None
    target = str(path.resolve())
    for line in out.splitlines():
        m = re.match(r"^.+? on (.+) \(([^,)]+)", line)
        if not m:
            continue
        mp = m.group(1)
        if (target == mp or target.startswith(mp.rstrip("/") + "/")) and len(mp) >= len(best):
            best, kind = mp, m.group(2)
    return kind


def _full_copy(src: Path, dst: Path, log) -> None:
    need = src.stat().st_size + SPARE
    free = shutil.disk_usage(dst.parent).free
    if free < need:
        raise BuildError(
            f"not enough free space for a full copy in {dst.parent}: "
            f"{free / 2**30:.1f} GiB free, {need / 2**30:.1f} GiB needed "
            f"(the image plus 1 GiB). Free some space, or put the output on the "
            f"same APFS volume as the retail image, where it is a clone that costs "
            f"almost nothing.")
    log(f"copying the retail image ({src.stat().st_size / 2**30:.1f} GiB, full copy)...")
    shutil.copyfile(str(src), str(dst))


def clone(src: Path, dst: Path, log: Callable[[str], None] = print) -> str:
    """Copy ``src`` to ``dst`` as cheaply as the platform allows.
    -> "clone" | "reflink" | "copy"."""
    if dst.exists():
        dst.unlink()
    if sys.platform == "darwin":
        same_dev = src.stat().st_dev == dst.parent.stat().st_dev
        if same_dev and _mount_fstype(dst.parent) == "apfs":
            subprocess.run(["cp", "-c", str(src), str(dst)], check=True)
            return "clone"
    elif sys.platform.startswith("linux"):
        r = subprocess.run(["cp", "--reflink=always", str(src), str(dst)],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return "reflink"
        if dst.exists():
            dst.unlink()
        log("this filesystem cannot reflink; falling back to a full copy")
    _full_copy(src, dst, log)
    return "copy"


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def _face(row: dict) -> str:
    f = row.get("face") or row.get("measure")
    return f if f in (tc.FONT0, tc.FONT1) else tc.FONT1


def _canonical(row: dict, text: str) -> Optional[str]:
    """What the readers show for ``text`` after a write (folds, raw codes)."""
    try:
        data, _ = tc.from_display(text, row.get("font", tc.FIELD), row.get("ctl", "c0"), _face(row))
    except tc.TxEncodeError:
        return None
    return tc.to_display(data, row.get("ctl", "c0"), row.get("font", tc.FIELD), _face(row))


def load_edits(project: dict) -> Tuple[Dict[str, str], int]:
    """Non-empty translations of the project -> ({id: text}, drafts)."""
    edits, drafts = {}, 0
    for rid, t in (project.get("translations") or {}).items():
        text = t.get("text", "") if isinstance(t, dict) else str(t)
        if not text.strip():
            continue
        edits[rid] = text
        if isinstance(t, dict) and t.get("state") != "done":
            drafts += 1
    return edits, drafts


def split_edits(edits: Dict[str, str]) -> Tuple[Dict[str, Dict[str, str]], List[str]]:
    per = {fam: {} for fam, _ in FAMILIES}
    unknown = []
    for rid, text in edits.items():
        fam = rid.split(":", 1)[0]
        if fam in per:
            per[fam][rid] = text
        else:
            unknown.append(rid)
    return per, unknown


def _modules(per: Dict[str, Dict[str, str]]) -> Dict[str, object]:
    return {fam: importlib.import_module(mod) for fam, mod in FAMILIES if per[fam]}


def validate_all(retail: str, per: Dict[str, Dict[str, str]], mods: Dict[str, object],
                 log: Callable[[str], None] = print) -> Tuple[Dict[str, List[str]], Dict[str, dict]]:
    """Errors of every family at once (nothing is written) and the retail
    rows of the edited ids."""
    errors: Dict[str, List[str]] = {}
    rows: Dict[str, dict] = {}
    for fam, mod in mods.items():
        t = time.time()
        fam_rows = {r["id"]: r for r in mod.rows(retail)}
        for rid in per[fam]:
            if rid in fam_rows:
                rows[rid] = fam_rows[rid]
        errs = mod.validate(per[fam], retail)
        for rid, es in errs.items():
            if es:
                errors.setdefault(rid, []).extend(es)
        log(f"validate {fam}: {len(per[fam])} edit(s), {len(errs)} with errors "
            f"({time.time() - t:.1f} s)")
    return errors, rows


def layout_problems(iso_path: str) -> Tuple[int, List[str]]:
    """(objects relocated past their chain's end, overlapping extents) of an
    image: every TOC object of every chain plus every ISO9660 file that is not
    a chain part, as absolute byte ranges. Retail has 0 and none."""
    from chains import CHAINS
    from iso9660 import IsoImage
    import repack
    iso = IsoImage(iso_path)
    ivs: List[Tuple[int, int, str]] = []
    relocated = 0
    try:
        parts = {n for names in CHAINS.values() for n in names}
        for chain in CHAINS:
            writer, entries, _ = repack._load_chain(iso.mm, iso, chain)
            for e in entries:
                if e.byte_offset >= writer.total:
                    relocated += 1
                if e.size:
                    for a, _src, n in writer._spans(e.byte_offset, e.size):
                        ivs.append((a, a + n, f"chain{chain} {e.path}"))
        for name, f in iso.files.items():
            if name not in parts and f.size:
                ivs.append((f.lba * repack.SECTOR, f.lba * repack.SECTOR + f.size, name))
    finally:
        iso.close()
    ivs.sort()
    problems: List[str] = []
    end, owner = -1, ""
    for a, b, label in ivs:
        if a < end:
            problems.append(f"{label} (from byte {a}) overlaps {owner} (to byte {end})")
        if b > end:
            end, owner = b, label
    return relocated, problems


def _take_lock(out: Path) -> Path:
    """Create ``OUT.lock`` holding our pid; refuse while another live build
    holds it (a lock left by a dead process is taken over)."""
    lock = out.with_name(out.name + ".lock")
    for _ in range(2):
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                pid = int(lock.read_text().strip() or "0")
                age = time.time() - lock.stat().st_mtime
            except (OSError, ValueError):
                pid, age = 0, 0.0
            alive = age < 60 if pid <= 0 else True
            # signal 0 only asks "is it alive" on POSIX; on Windows os.kill(pid, 0)
            # sends a console event to that process, so the lock is trusted there
            if pid > 0 and os.name != "nt":
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    alive = False
                except PermissionError:
                    pass
            if alive:
                raise BuildError(f"another build{f' (pid {pid})' if pid > 0 else ''} is writing "
                                 f"{out.name}; wait for it to finish (or delete {lock.name} "
                                 f"if no build is running)")
            try:
                lock.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as fh:
            fh.write(str(os.getpid()))
        return lock
    raise BuildError(f"could not take {lock.name}")


def _verify(out: str, retail: str, per: Dict[str, Dict[str, str]], mods: Dict[str, object],
            rows: Dict[str, dict]) -> Tuple[int, List[str]]:
    """Re-read every edited row from ``out``. -> (rows checked, problems)."""
    got: Dict[str, str] = {}
    problems: List[str] = []
    if "umn" in mods:
        st = mods["umn"]._load(out)
        got.update({r["id"]: r["src"] for r in st.rows if r["id"] in per["umn"]})
    if "evt" in mods:
        got.update(mods["evt"].read_back(out, list(per["evt"])))
    if "ovl" in mods:
        for rid, (raw, _va, agree) in mods["ovl"].read_back(out, retail).items():
            if rid in per["ovl"]:
                row = rows[rid]
                got[rid] = tc.to_display(raw or b"", "c0", row.get("font", tc.FIELD), _face(row))
                if not agree:
                    problems.append(f"{rid}: its references no longer point at one string")
    if "fix" in mods:
        fx = mods["fix"]
        fields, _objs = fx.scan(out)
        got.update({f.id: fx._decode(f) for f in fields if f.id in per["fix"]})
    n = 0
    for fam, edits in per.items():
        for rid, text in edits.items():
            n += 1
            row = rows[rid]
            back = got.get(rid)
            if back is None:
                problems.append(f"{rid}: not found when re-reading the output")
                continue
            want = {text, _canonical(row, text)}
            if back in want:
                continue
            # enemy quotes keep their slot: a shorter text comes back space-padded
            if fam == "fix" and back.rstrip(" ") in {w.rstrip(" ") for w in want if w}:
                continue
            # U.M.N. rows: blank lines at either end belong to the page frame
            if fam == "umn" and back.strip("\n") in {w.strip("\n") for w in want if w}:
                continue
            problems.append(f"{rid}: reads back {back[:60]!r}, expected {text[:60]!r}")
    return n, problems


def _same_file(a: Path, b: Path) -> bool:
    if a.resolve() == b.resolve():
        return True
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def build(project_path: str, retail_iso: str, out_iso: str,
          log: Callable[[str], None] = print, overwrite: bool = False) -> dict:
    """Build ``out_iso`` from the project. Raises BuildError (nothing left
    under ``out_iso`` or ``out_iso.partial``) when anything is wrong."""
    t0 = time.time()
    retail = Path(retail_iso).expanduser()
    out = Path(out_iso).expanduser()
    partial = out.with_name(out.name + ".partial")
    if not retail.is_file():
        raise BuildError(f"no retail image at {retail}")
    if _same_file(out, retail) or _same_file(partial, retail):
        raise BuildError("the output path is the retail image; pick another name "
                         "(the retail image is never written)")
    if out.suffix.lower() != ".iso":
        raise BuildError(f"the output should end in .iso: {out}")
    if out.exists() and not overwrite:
        raise BuildError(f"{out} already exists; pass --overwrite to replace it")
    if not out.parent.is_dir():
        raise BuildError(f"no such folder: {out.parent}")

    project = txproject.load(str(Path(project_path).expanduser()))
    rid = txproject.retail_id(str(retail))
    want_rid = project.get("retail_id") or ""
    if want_rid and want_rid != rid:
        raise BuildError(
            f"this project was made for a different disc image (retail id {want_rid[:12]}..., "
            f"this image {rid[:12]}...). Pick the untouched USA image the project was "
            f"started from; a patched copy cannot be the base of a build.")
    if not want_rid:
        log("note: the project has no retail id; building against this image anyway")
    relocated, overlaps = layout_problems(str(retail))
    if relocated or overlaps:
        raise BuildError(
            f"{retail.name} is not an untouched image: {relocated} object(s) already sit "
            f"past their chain's end" + (f" and {len(overlaps)} overlap" if overlaps else "")
            + " (an earlier build or import grew them). A patched copy cannot be the base "
              "of a build; pick the untouched USA image.")
    edits, drafts = load_edits(project)
    if not edits:
        raise BuildError("the project has no translations yet; nothing to build")
    per, unknown = split_edits(edits)
    if unknown:
        raise BuildError(f"{len(unknown)} id(s) belong to no family: " + ", ".join(unknown[:10]))
    try:
        target = txtarget.resolve_target(project)
    except txtarget.TargetError as e:
        raise BuildError(f"the project's target is not valid: {e}") from None
    log(f"project {Path(project_path).name}: language {project.get('language') or '?'} "
        f"(target {target['code'] or '?'}, {target['kind']}), "
        f"{len(edits)} translation(s) ({drafts} still draft): "
        + ", ".join(f"{fam} {len(per[fam])}" for fam, _ in FAMILIES if per[fam]))

    mods = _modules(per)
    plan = None
    if target["kind"] != "builtin":
        erows = edited_rows(str(retail), per, mods)
        if txtarget.needs_glyphs(target, erows, edits):
            ok, msg = txtarget.shaping_available()
            if not ok:
                raise BuildError(f"{target['name']} needs new glyphs: {msg}")
            t = time.time()
            try:
                plan = txtarget.plan_glyphs(str(retail), target, erows, edits, log)
            except txtarget.TargetError as e:
                raise BuildError(str(e)) from None
            log(f"new glyphs planned ({time.time() - t:.1f} s)")
            per, _ = split_edits(plan.edits)
    with (tc.glyph_codes() if plan else _nothing()):
        return _build_validated(retail, out, partial, overwrite, project_path, per, mods, edits,
                                drafts, plan, target, log, t0)


class _nothing:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def edited_rows(retail: str, per: Dict[str, Dict[str, str]],
                mods: Dict[str, object]) -> Dict[str, dict]:
    """{id: catalog row} for every edited id (the families' rows(), cached)."""
    out: Dict[str, dict] = {}
    for fam, mod in mods.items():
        want = per[fam]
        for r in mod.rows(retail):
            if r["id"] in want:
                out[r["id"]] = r
    return out


def _build_validated(retail: Path, out: Path, partial: Path, overwrite: bool, project_path: str,
                     per, mods, edits, drafts, plan, target, log, t0) -> dict:
    errors, rows = validate_all(str(retail), per, mods, log)
    if errors:
        lines = [f"  {k}: {m}" for k, ms in sorted(errors.items()) for m in ms]
        raise BuildError(f"{len(errors)} translation(s) cannot be built; nothing was written:\n"
                         + "\n".join(lines[:200])
                         + (f"\n  ... and {len(lines) - 200} more" if len(lines) > 200 else ""))

    if "ovl" in mods and hasattr(mods["ovl"], "warnings"):
        for rid, ws in sorted(mods["ovl"].warnings(per["ovl"], str(retail)).items()):
            for w in ws:
                if "two printers" in w:
                    log(f"warning: {rid}: {w}")

    field_text = "".join(t for k, t in edits.items()
                         if rows.get(k, {}).get("font", tc.FIELD) == tc.FIELD)
    fix_font0 = sorted({c for c in field_text if c in tc.FONT0_UNACCENTED})

    lock = _take_lock(out)
    try:
        summary = _build_locked(retail, out, partial, overwrite, per, mods, rows, edits, drafts,
                                field_text, fix_font0, log, t0, plan)
        summary["target"] = {k: target.get(k) for k in ("code", "kind", "script", "rtl", "font")}
        if plan is not None:
            summary["glyphs"] = {k: v for k, v in plan.report.items()
                                 if k not in ("field_chars", "battle_chars")}
        return summary
    finally:
        try:
            lock.unlink()
        except FileNotFoundError:
            pass


def _build_locked(retail: Path, out: Path, partial: Path, overwrite: bool, per, mods, rows,
                  edits, drafts, field_text: str, fix_font0, log, t0: float,
                  plan=None) -> dict:
    if out.exists() and not overwrite:
        raise BuildError(f"{out} already exists; pass --overwrite to replace it")
    if partial.exists():
        log(f"removing a stale {partial.name} from an earlier build")
        partial.unlink()
    summary = {"out": str(out), "edits": len(edits), "drafts": drafts}
    try:
        t = time.time()
        how = clone(retail, partial, log)
        log(f"{how} of the retail image -> {partial.name} ({time.time() - t:.1f} s)")
        for fam, _ in FAMILIES:
            if fam in mods:
                t = time.time()
                kw = {}
                if fam == "ovl" and plan is not None and plan.ovl_raw:
                    kw["raw_overrides"] = plan.ovl_raw       # the battle byte table
                mods[fam].apply(str(partial), str(retail), per[fam], log=log, **kw)
                log(f"applied {fam}: {len(per[fam])} edit(s) ({time.time() - t:.1f} s)")
        if plan is not None and plan.ovl_raw and "ovl" not in mods:
            # battle glyphs but no ovl edits cannot happen (battle rows are ovl rows)
            raise BuildError("internal: a battle byte table without ovl edits")
        fonts_done = []
        fonts = tc.load_fonts(str(retail))
        patches = {}
        for face in (tc.FONT0, tc.FONT1):
            font = fonts[face]
            what = []
            missing = tc.missing_capital_accents(font, face)
            if missing and (face == tc.FONT1 or fix_font0):
                font = tc.fix_capital_accents(font, face)
                what.append("".join(missing))
            dashes = tc.needs_metric_fix(field_text, fonts[face], face)
            if dashes:
                font = tc.fix_unmeasured(font, face, dashes)
                what.append("metrics " + "".join(dashes))
            if plan is not None and plan.glyphset is not None:
                font = plan.apply_font(font, face)
                what.append(f"{len(plan.glyphset.slots)} new glyphs")
            if what:
                fixed = font.to_bytes()
                if len(fixed) != len(fonts[face].to_bytes()):
                    raise BuildError(f"{face}.tex changed size in the font fix")
                patches[(0, f"{face}.tex")] = fixed
                fonts_done.append(f"{face} ({', '.join(what)})")
        if plan is not None and plan.stat_xtx is not None:
            import statfont
            patches[(0, statfont.SHEET)] = plan.stat_xtx
            fonts_done.append(f"stat.xtx ({plan.report.get('battle_glyphs')} battle glyphs)")
        if patches:
            from repack import patch_iso
            patch_iso(str(partial), patches, verbose=False, grow=False)
            log(f"{'fonts' if plan is not None else 'font accent fix'}: redrew {', '.join(fonts_done)} "
                f"(translations use {''.join(fix_font0) or 'no È É Ê Ë'})")
        summary["fonts"] = fonts_done
        t = time.time()
        n, problems = _verify(str(partial), str(retail), per, mods, rows)
        if patches:
            from repack import read_entry
            for (chain, name), data in patches.items():
                if read_entry(str(partial), chain, name) != data:
                    problems.append(f"{name}: does not read back as patched")
        if plan is not None and plan.ovl_raw:
            back = mods["ovl"].read_back(str(partial), str(retail))
            for (file, va), raw in plan.ovl_raw.items():
                got = back.get(mods["ovl"].row_id(file, va))
                if got is None or got[0] != raw:
                    problems.append(f"{file} @{va:x}: the battle byte table does not read back")
        _reloc, overlaps = layout_problems(str(partial))
        problems += [f"layout: {x}" for x in overlaps]
        if problems:
            raise BuildError(f"{len(problems)} row(s) did not read back as written:\n  "
                             + "\n  ".join(problems[:50]))
        log(f"verified: {n} edited row(s) read back from the output ({time.time() - t:.1f} s)")
        os.replace(str(partial), str(out))      # replaces an existing output in one step
    except BaseException:
        if partial.exists():
            partial.unlink()
            log(f"removed {partial.name}")
        raise
    summary["seconds"] = round(time.time() - t0, 1)
    log(f"done: {out} ({len(edits)} translation(s), {summary['seconds']} s)")
    return summary


# ---------------------------------------------------------------------------
# selftest
# ---------------------------------------------------------------------------

FR_WORDS = ("Élève à la fenêtre, ça goûte où Noël a bu ; cœur naïf « oui »",
            "Señor, ¿qué día es? ¡Último camión! pingüino, mamá",
            "Été", "Ève", "Ça", "Où", "Señal")
_SPLIT = {
    "c0": re.compile(r"((?:⟦[0-9A-Fa-f]{2}⟧)+|\n|%[-+0#]*\d*(?:\.\d+)?[lh]?[diouxXcsfeg]|@.)"),
    "evt": re.compile(r"((?:⟦[0-9A-Fa-f]{2}⟧)+|/\[[^\]\n]*\]|\n|%[-+0#]*\d*[ds])"),
    "chat": re.compile(r"((?:⟦[0-9A-Fa-f]{2}⟧)+|\\[0-7]{1,3}|\\.|\n)"),
}


def _translate(src: str, ctl: str, phrase: str) -> str:
    """Replace the words of ``src`` with ``phrase``, keeping every token in place."""
    parts = _SPLIT.get(ctl, _SPLIT["c0"]).split(src)
    done = False
    for i in range(0, len(parts), 2):
        if parts[i].strip():
            parts[i] = "" if done else phrase
            done = True
    return "".join(parts) if done else src


def _pick(mod, rows: List[dict], retail: str, per_cat: int = 2) -> Dict[str, str]:
    edits: Dict[str, str] = {}
    seen: Dict[str, int] = {}
    for r in rows:
        if r.get("lock") or r.get("hide") or seen.get(r["cat"], 0) >= per_cat:
            continue
        for phrase in FR_WORDS:
            text = _translate(r["src"], r.get("ctl", "c0"), phrase)
            if text == r["src"] or mod.validate({**edits, r["id"]: text}, retail):
                continue
            edits[r["id"]] = text
            seen[r["cat"]] = seen.get(r["cat"], 0) + 1
            break
    return edits


def selftest(iso: str, scratch: Optional[str] = None) -> int:
    import json
    import tempfile
    t0 = time.time()
    ok = True

    def fail(msg):
        nonlocal ok
        ok = False
        print("FAIL:", msg)

    work = Path(scratch or tempfile.gettempdir()) / f"txbuild-selftest-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    out = work / "selftest (FR).iso"
    proj = work / "selftest (FR).json"
    try:
        edits: Dict[str, str] = {}
        for fam, name in FAMILIES:
            mod = importlib.import_module(name)
            picked = _pick(mod, mod.rows(iso), iso)
            print(f"picked {len(picked)} {fam} edit(s)")
            edits.update(picked)
        # one font0 line with È/É so the accent fix runs, and an em dash for the metric fix
        evt = importlib.import_module("txfam_evt")
        line = "Élève — Ève"
        cut = next(r for r in evt.rows(iso) if r.get("face") == tc.FONT0 and not r.get("lock")
                   and not r.get("hide") and r["id"] not in edits
                   and evt.validate({r["id"]: _translate(r["src"], "evt", line)}, iso) == {})
        edits[cut["id"]] = _translate(cut["src"], "evt", line)
        # three long lines in one field container: it outgrows its sectors and is
        # relocated, so the output is a patched image (refused as a base below)
        long_line = " ".join([FR_WORDS[0]] * 14)[:700]
        by_path: Dict[str, List[dict]] = {}
        for r in evt.rows(iso):
            if r["cat"] == evt.CAT_FIELD and not r.get("lock") and not r.get("hide") \
                    and r["id"] not in edits and "⟦" not in r["src"]:
                by_path.setdefault(evt.parse_id(r["id"])[1], []).append(r)
        grow = next(rs for rs in by_path.values() if len(rs) >= 3)[:3]
        for r in grow:
            edits[r["id"]] = _translate(r["src"], "evt", long_line)
        project = txproject.new_project("fr", txproject.retail_id(iso))
        project["translations"] = {k: {"text": v, "state": "done"} for k, v in edits.items()}
        txproject.save(str(proj), project)
        quiet = lambda m: None  # noqa: E731

        # refusals: retail as output, foreign retail id, a bad edit -> nothing written
        for label, kw, expect in (
                ("out = retail", dict(out_iso=iso), "retail image"),
                ("not .iso", dict(out_iso=str(work / "x.bin")), "end in .iso")):
            try:
                build(str(proj), iso, log=quiet, **kw)
                fail(f"{label}: accepted")
            except BuildError as e:
                if expect not in str(e):
                    fail(f"{label}: {e}")
        bad = dict(project, translations=dict(project["translations"]))
        locked = next(r for r in evt.rows(iso) if r.get("lock"))
        bad["translations"][locked["id"]] = {"text": "Bonjour", "state": "done"}
        bad_path = work / "bad.json"
        txproject.save(str(bad_path), bad)
        try:
            build(str(bad_path), iso, str(out), log=quiet)
            fail("locked edit accepted")
        except BuildError as e:
            if locked["id"] not in str(e):
                fail(f"locked edit: message lacks the id: {e}")
        if out.exists() or Path(str(out) + ".partial").exists():
            fail("a refused build left a file behind")
        wrong = dict(project, retail_id="0" * 40)
        txproject.save(str(bad_path), wrong)
        try:
            build(str(bad_path), iso, str(out), log=quiet)
            fail("foreign retail id accepted")
        except BuildError:
            pass
        lock = out.with_name(out.name + ".lock")
        lock.write_text(str(os.getpid()))
        try:
            build(str(proj), iso, str(out), log=quiet)
            fail("build accepted while another build holds the lock")
        except BuildError as e:
            if "another build" not in str(e):
                fail(f"lock: {e}")
        finally:
            lock.unlink()
        print("refusals: out=retail, not .iso, locked id, foreign retail id, output locked: "
              "all refused, nothing left")

        lines: List[str] = []
        summary = build(str(proj), iso, str(out), log=lines.append)
        for x in lines:
            if x.startswith(("verified", "font accent", "clone", "copy", "reflink", "done")):
                print("  " + x)
        if not out.exists() or Path(str(out) + ".partial").exists():
            fail("output not under its final name")
        from repack import read_entry
        if read_entry(str(out), 0, "font0.tex") == read_entry(iso, 0, "font0.tex"):
            fail("font0 was not patched although a font0 line uses É")
        f1 = tc.load_fonts(iso)[tc.FONT1]
        if read_entry(str(out), 0, "font1.tex") != tc.fix_unmeasured(f1, tc.FONT1, ["—"]).to_bytes():
            fail("font1 is not retail plus the — metrics")
        relocated, overlaps = layout_problems(str(out))
        if overlaps:
            fail(f"output layout overlaps: {overlaps[:3]}")
        if relocated:
            # a project with no retail id passes the id check: the layout check stops it
            txproject.save(str(bad_path), dict(project, retail_id=""))
            try:
                build(str(bad_path), str(out), str(work / "again.iso"), log=quiet)
                fail("a patched image was accepted as the base")
            except BuildError as e:
                if "not an untouched image" not in str(e):
                    fail(f"patched base: {e}")
        print(f"layout: {relocated} relocated object(s), no overlaps; patched base refused: "
              f"{bool(relocated)}")
        try:
            build(str(proj), iso, str(out), log=quiet)
            fail("existing output overwritten without overwrite=True")
        except BuildError:
            pass
        print(f"build: {summary['edits']} edits, fonts {summary['fonts']}, {summary['seconds']} s")
        print(json.dumps({k: v for k, v in summary.items() if k != "out"}))
    finally:
        for p in (out, Path(str(out) + ".partial"), proj, work / "bad.json", work / "again.iso"):
            if p.exists():
                p.unlink()
        try:
            work.rmdir()
        except OSError:
            pass
    print(f"clone deleted: {not out.exists()}; total {time.time() - t0:.1f} s")
    print("selftest OK" if ok else "selftest FAILED")
    return 0 if ok else 1


# -- new-script targets (txtarget) -------------------------------------------------

def battle_png(raw: bytes, stat_xtx: bytes, table: bytes, scale: int = 3) -> bytes:
    """What OV01 menuFontPut draws for ``raw`` with this stat.xtx grid and byte
    table: ``#XX`` = cell XX, a byte = its index in the table, two-byte codes
    skipped. 10x18 cells side by side, as a grey PNG."""
    import statfont
    from font import sheet_to_png
    canvas, clen, _subs = statfont.xtx_canvas(stat_xtx)
    cells: List[int] = []
    i = 0
    while i < len(raw):
        b = raw[i]
        if b == 0x23 and re.match(rb"[0-9A-Fa-f]{2}", raw[i + 1:i + 3]):
            cells.append(int(raw[i + 1:i + 3], 16))
            i += 3
        elif b & 0x80:
            i += 2
        else:
            k = table.find(bytes((b,)))
            cells.append(k if k >= 0 else 0)
            i += 1
    W, H = max(1, len(cells)) * statfont.CELL_W, statfont.CELL_H
    img = bytearray(W * H)
    for n, c in enumerate(cells):
        rows = statfont.read_cell(canvas, clen, c)
        for y in range(H):
            for x in range(statfont.CELL_W):
                img[y * W + n * statfont.CELL_W + x] = statfont.SHADE.get(rows[y][x], 128)
    big = bytearray()
    for y in range(H):
        line = b"".join(bytes([v]) * scale for v in img[y * W:(y + 1) * W])
        big += line * scale
    return sheet_to_png(bytes(big), W * scale, H * scale)


def _field_png(font, raw: bytes, scale: int = 3) -> List[bytes]:
    import langpack
    return [langpack.render_line(font, line, scale) for line in raw.split(b"\n")]


def _readback_raw(out: str, retail: str, ids: List[str], rows: Dict[str, dict]) -> Dict[str, bytes]:
    """Engine bytes of each edited row as the built image holds them."""
    import txfam_evt
    import txfam_ovl
    got: Dict[str, bytes] = {}
    evt_ids = [i for i in ids if i.startswith("evt:")]
    if evt_ids:
        for rid, disp in txfam_evt.read_back(out, evt_ids).items():
            got[rid] = tc.from_display(disp, tc.FIELD, "evt", _face(rows[rid]))[0]
    if any(i.startswith("ovl:") for i in ids):
        for rid, (raw, _va, _ok) in txfam_ovl.read_back(out, retail).items():
            if rid in ids:
                got[rid] = raw or b""
    return got


def selftest_targets(iso: str, scratch: Optional[str] = None,
                     render_dir: Optional[str] = None, cases: Optional[List[str]] = None) -> int:
    """Build clones for new-script targets and check them (see txtarget):
    ru (Cyrillic: ST0210 lines, a cutscene line, an item name, a battle word),
    ar (Arabic RTL, right-aligned), ja (kana + level-1 + level-2 kanji),
    ja2 (30 level-2 kanji beside a level-1 row that must draw unchanged),
    pl (Polish hybrid), fr (byte-identical to the builtin path of the
    original txbuild when ``XS1_TXBUILD_ORIG`` names a copy of it). Every
    edited row is rendered from the BUILT image's fonts into ``render_dir``."""
    import importlib.util
    import json
    import tempfile
    import txfam_evt
    import txfam_ovl
    from repack import read_entry
    t0 = time.time()
    ok = True

    def fail(msg):
        nonlocal ok
        ok = False
        print("FAIL:", msg)

    work = Path(scratch or tempfile.gettempdir()) / f"txbuild-targets-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    rdir = Path(render_dir) if render_dir else work / "render"
    rdir.mkdir(parents=True, exist_ok=True)
    rid0 = txproject.retail_id(iso)
    ev = {r["id"]: r for r in txfam_evt.rows(iso)}
    ov = {r["id"]: r for r in txfam_ovl.rows(iso)}
    allrows = dict(ev, **ov)

    def editable(r):
        return not r.get("lock") and not r.get("hide")

    st0210 = [r for k, r in ev.items() if "ST0210.evt" in k and editable(r)
              and r["cat"] == txfam_evt.CAT_FIELD and len(r["src"]) > 12]
    cut = next(r for k, r in ev.items() if "SCE01001" in k and editable(r)
               and r["cat"] == txfam_evt.CAT_CUT and r.get("face") == tc.FONT0
               and "⟦" not in r["src"] and len(r["src"]) > 12)
    item = next(r for r in ov.values() if r["cat"] == txfam_ovl.CAT_ITEMS and r["font"] == tc.FIELD
                and editable(r) and r["lim"] is None)
    battle = ov["ovl:OV01.OVL:a46970"]
    fonts_found = {
        "cyr": txtarget._find_font("Arial Unicode.ttf"),
        "ar": txtarget._find_font("SFArabic.ttf", "GeezaPro.ttc", "Arial Unicode.ttf"),
        "ja": txtarget._find_font("ヒラギノ角ゴシック W3.ttc", "NotoSansJP-Regular.otf"),
        "pl": txtarget._find_font("Arial.ttf", "Arial Unicode.ttf"),
    }
    print(f"fonts: {fonts_found}")
    L = lambda r, phrase: _translate(r["src"], r.get("ctl", "c0"), phrase)  # noqa: E731
    specs = {
        "ru": ({"code": "ru", "font": fonts_found["cyr"]}, {
            st0210[0]["id"]: L(st0210[0], "Привет! Это виртуальное обучение, Шион."),
            st0210[1]["id"]: L(st0210[1], "Обычные атаки: Съешь же ещё этих мягких французских "
                                          "булок, да выпей чаю (KOS-MOS)."),
            st0210[2]["id"]: L(st0210[2], "Ёжик щурится — «Юла» №3"),
            cut["id"]: L(cut, "Ж Ш Щ Ю Я ж ш щ ю я: сцена с font0"),
            item["id"]: "Лечебный гель",
            battle["id"]: "Конец",
        }),
        "ar": ({"code": "ar", "font": fonts_found["ar"], "align": "right"}, {
            st0210[0]["id"]: L(st0210[0], "مرحبا! هذا تدريب افتراضي."),
            st0210[1]["id"]: L(st0210[1], "هجمات عادية KOS-MOS (3) ثم هجمات تقنية وقوائم فرعية "
                                          "ثم الانتقال إلى القتال الحقيقي بعد التدريب الافتراضي"),
            item["id"]: "هلام علاجي",
            battle["id"]: "نهاية",
        }),
        "ja": ({"code": "ja", "font": fonts_found["ja"]}, {
            st0210[0]["id"]: L(st0210[0], "シオン、こんにちは。今日は憂鬱な謎の本を読んだ。"),
            st0210[1]["id"]: L(st0210[1], "通常攻撃と必殺技。鬱屈した嘲笑、囁き。"),
            cut["id"]: L(cut, "ケイオス「麒麟の牙だ」"),
            battle["id"]: "おわり",
        }),
        # more level-2 kanji than the 24 empty cells, next to a row of
        # level-1 kanji that needs no glyph: that row must draw unchanged
        "ja2": ({"code": "ja", "font": fonts_found["ja"]}, {
            st0210[3]["id"]: L(st0210[3], "".join(bytes([0xD0 + i // 94, 0xA1 + i % 94]).decode("euc_jp")
                                                  for i in range(30))),
            st0210[4]["id"]: L(st0210[4], "腕と湾"),
        }),
        "pl": ({"code": "pl", "font": fonts_found["pl"]}, {
            st0210[0]["id"]: L(st0210[0], "Zażółć gęślą jaźń."),
            st0210[1]["id"]: L(st0210[1], "Pchnąć w tę łódź jeża lub ośm skrzyń fig. Ćma, Śląsk, Żuraw."),
            item["id"]: "Żel leczniczy",
            battle["id"]: "Łódź",
        }),
    }
    results = {}
    f_ret = {f: tc.Font.from_bytes(read_entry(iso, 0, f"{f}.tex")) for f in (tc.FONT0, tc.FONT1)}
    try:
        for case, (tgt, edits) in specs.items():
            if cases and case not in cases:
                continue
            if not tgt.get("font"):
                print(f"[{case}] no font on this machine, skipped")
                continue
            print(f"\n[{case}] {len(edits)} edits, font {Path(tgt['font']).name}")
            proj = work / f"t-{case}.json"
            out = work / f"t-{case}.iso"
            project = txproject.new_project(case, rid0)
            project["target"] = tgt
            project["translations"] = {k: {"text": v, "state": "done"} for k, v in edits.items()}
            txproject.save(str(proj), project)
            lines: List[str] = []
            try:
                summary = build(str(proj), iso, str(out), log=lines.append)
            except BuildError as e:
                fail(f"{case}: build refused: {e}")
                print("\n".join("  " + x for x in lines[-15:]))
                continue
            for x in lines:
                if x.startswith(("field glyphs", "battle glyphs", "fonts:", "verified", "retail usage",
                                 "new glyphs", "done")):
                    print("  " + x)
            results[case] = summary.get("glyphs")
            print(f"  glyphs: {json.dumps(summary.get('glyphs'))}")
            f_out = {f: tc.Font.from_bytes(read_entry(str(out), 0, f"{f}.tex"))
                     for f in (tc.FONT0, tc.FONT1)}
            raws = _readback_raw(str(out), iso, list(edits), allrows)
            stat = read_entry(str(out), 0, "yamamoto\\stat\\stat.xtx")
            table = txfam_ovl.read_back(str(out), iso).get("ovl:OV01.OVL:a46af8", (b"",))[0] or b""
            for n, rid in enumerate(sorted(edits)):
                raw = raws.get(rid)
                if raw is None:
                    fail(f"{case}: {rid} not read back")
                    continue
                row = allrows[rid]
                if row["font"] == tc.FIELD and not txtarget.coverage(
                        edits[rid], txtarget.resolve_target(project), tc.FIELD, _face(row))["glyph"]:
                    # a row that needs no new glyph draws as on the retail font
                    if _field_png(f_out[_face(row)], raw) != _field_png(f_ret[_face(row)], raw):
                        fail(f"{case}: {rid} needs no glyph but draws differently on the built font")
                    else:
                        print(f"  {rid}: no new glyph, draws as on the retail font")
                if row["font"] == tc.BATTLE:
                    (rdir / f"{case}_{n}_battle.png").write_bytes(battle_png(raw, stat, table))
                    print(f"  {rid}: {len(raw)} bytes {raw!r} -> {case}_{n}_battle.png")
                else:
                    pngs = _field_png(f_out[_face(row)], raw)
                    for k, png in enumerate(pngs):
                        (rdir / f"{case}_{n}_{k}.png").write_bytes(png)
                    print(f"  {rid} ({_face(row)}): {len(raw)} bytes, {len(pngs)} line(s) -> "
                          f"{case}_{n}_*.png")
            for p in (out, proj):
                if p.exists():
                    p.unlink()
        if not cases or "fr" in cases:
            ok &= _fr_regression(iso, work, rid0, ev, ov)
    finally:
        for p in work.glob("t-*"):
            p.unlink()
        for p in work.glob("fr-*"):
            p.unlink()
        try:
            work.rmdir()            # fails (harmlessly) when render_dir is inside it
        except OSError:
            pass
    print(f"\nrenders in {rdir}; total {time.time() - t0:.1f} s")
    print("selftest-targets OK" if ok else "selftest-targets FAILED")
    return 0 if ok else 1


def _fr_regression(iso: str, work: Path, rid0: str, ev: Dict[str, dict], ov: Dict[str, dict]) -> bool:
    """A French project builds byte-identical with the original txbuild."""
    import importlib.util
    orig_path = os.environ.get("XS1_TXBUILD_ORIG")
    edits = {}
    import txfam_evt
    import txfam_ovl
    for rows, mod, n in ((ev, txfam_evt, 3), (ov, txfam_ovl, 3)):
        for r in rows.values():
            if len([k for k in edits if k.startswith(r["id"].split(":")[0])]) >= n:
                break
            if r.get("lock") or r.get("hide") or r["font"] == tc.ASCII:
                continue
            for phrase in FR_WORDS:
                text = _translate(r["src"], r.get("ctl", "c0"), phrase)
                if text != r["src"] and not mod.validate({r["id"]: text}, iso):
                    edits[r["id"]] = text
                    break
    battle = ov["ovl:OV01.OVL:a46970"]
    edits[battle["id"]] = "Fin à é"
    project = txproject.new_project("fr", rid0)
    project["translations"] = {k: {"text": v, "state": "done"} for k, v in edits.items()}
    proj = work / "fr-proj.json"
    txproject.save(str(proj), project)
    new_iso, old_iso = work / "fr-new.iso", work / "fr-old.iso"
    quiet = lambda m: None  # noqa: E731
    s_new = build(str(proj), iso, str(new_iso), log=quiet)
    print(f"\n[fr] {len(edits)} edits, new path: glyphs={s_new.get('glyphs')} "
          f"target={s_new.get('target')}")
    if not orig_path:
        print("[fr] XS1_TXBUILD_ORIG not set: no original txbuild to compare with; "
              "checked only that no glyph plan ran")
        good = s_new.get("glyphs") is None
    else:
        spec = importlib.util.spec_from_file_location("txbuild_orig", orig_path)
        orig = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(orig)
        orig.build(str(proj), iso, str(old_iso), log=quiet)
        r = subprocess.run(["cmp", str(new_iso), str(old_iso)], capture_output=True, text=True)
        good = r.returncode == 0 and s_new.get("glyphs") is None
        print(f"[fr] byte-identical to the original txbuild: {r.returncode == 0} "
              f"({r.stdout.strip() or 'cmp: no difference'})")
    for p in (new_iso, old_iso, proj):
        if p.exists():
            p.unlink()
    if not good:
        print("FAIL: French build differs from the builtin path")
    return good


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a translated ISO from a project")
    b.add_argument("--project", required=True)
    b.add_argument("--retail", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--overwrite", action="store_true")
    s = sub.add_parser("selftest")
    s.add_argument("--iso", required=True)
    s.add_argument("--scratch")
    st = sub.add_parser("selftest-targets", help="build clones for ru/ar/ja/pl targets and "
                        "the French regression; render every edited row")
    st.add_argument("--iso", required=True)
    st.add_argument("--scratch")
    st.add_argument("--render")
    st.add_argument("--cases", help="comma list of ru,ar,ja,ja2,pl,fr (default all)")
    a = ap.parse_args(argv)
    if a.cmd == "selftest":
        return selftest(a.iso, a.scratch)
    if a.cmd == "selftest-targets":
        return selftest_targets(a.iso, a.scratch, a.render,
                                a.cases.split(",") if a.cases else None)
    try:
        build(a.project, a.retail, a.out, overwrite=a.overwrite)
    except (BuildError, tc.TxBuildError, txproject.ProjectError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
