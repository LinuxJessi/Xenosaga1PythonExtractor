"""Render every model-less (.lex-less) standalone .xtx of an extraction with
a given browse.py, in parallel — the 35-second regression harness behind the
UI-sheet palette rules (full `browse --kinds textures` is ~40 min).

    python docs/research/lexless_render.py <kit-or-copy-dir> <out-dir> <extraction-root>
    python docs/research/lexless_render.py --diff <out-A> <out-B>

<extraction-root> is a `cli.py extract` output holding dump/ and browse/code/
(the OV*.OVL overlays). Compare two runs with --diff: it lists every PNG
whose bytes differ, with the palette source and opaque fraction of each.
Decode the "before" with `git show <tag>:browse.py` copied next to the kit's
other modules (symlink arx.py etc. beside it).
"""
import csv
import sys
from multiprocessing import Pool
from pathlib import Path

from PIL import Image


def _jobs(dump: Path) -> list[Path]:
    srcs = sorted(dump.rglob("*.xtx"))
    stems: dict[Path, list[str]] = {}
    for s in srcs:
        stems.setdefault(s.parent, []).append(s.stem.lower())
    companions = set()
    for s in srcs:
        for lx in s.parent.glob("*.lex"):
            st = lx.stem.lower()
            if st in stems[s.parent]:
                continue
            owners = [t for t in stems[s.parent] if st.startswith(t)]
            if owners and max(owners, key=len) == s.stem.lower():
                companions.add(s)
    return [s for s in srcs
            if not s.with_suffix(".lex").is_file() and s not in companions]


_G: dict = {}


def _init(mod_dir: str, code_dir: str) -> None:
    sys.path.insert(0, mod_dir)
    import browse  # noqa: E402  (the module under test)
    _G["browse"] = browse
    _G["cbps"] = browse.scan_ovl_cluts(Path(code_dir))
    scan = getattr(browse, "scan_ovl_descriptors", None)
    _G["rects"] = scan(Path(code_dir)) if scan else {}


def _one(args: tuple[str, str, str]) -> tuple[str, str, int, int]:
    src, dump, out = Path(args[0]), Path(args[1]), Path(args[2])
    browse = _G["browse"]
    data = src.read_bytes()
    name = src.name.lower()
    kw = {"ovl_rects": _G["rects"].get(name)} if _G["rects"] else {}
    r = browse.decode_xtx(data, [], _G["cbps"].get(name), **kw)
    rel = src.relative_to(dump)
    if r is None:
        return (str(rel), "none", 0, 0)
    w, h, rgba, source = r
    dest = out / rel.with_suffix(".png")
    dest.parent.mkdir(parents=True, exist_ok=True)
    Image.frombytes("RGBA", (w, h), bytes(rgba)).save(dest)
    return (str(rel), source, w, h)


def render(mod_dir: Path, out: Path, root: Path) -> None:
    dump, code = root / "dump", root / "browse" / "code"
    out.mkdir(parents=True, exist_ok=True)
    jobs = _jobs(dump)
    print(f"{len(jobs)} lex-less .xtx")
    with Pool(initializer=_init, initargs=(str(mod_dir), str(code))) as pool:
        rows = pool.map(_one, [(str(j), str(dump), str(out)) for j in jobs],
                        chunksize=4)
    with open(out / "index.csv", "w", newline="") as f:
        csv.writer(f).writerows(rows)
    from collections import Counter
    print(Counter(r[1] for r in rows))


def diff(a: Path, b: Path) -> None:
    ia = {r[0]: r for r in csv.reader(open(a / "index.csv"))}
    ib = {r[0]: r for r in csv.reader(open(b / "index.csv"))}
    changed = []
    for k in ia:
        pa, pb = a / Path(k).with_suffix(".png"), b / Path(k).with_suffix(".png")
        if not (pa.exists() and pb.exists()):
            continue
        x, y = Image.open(pa), Image.open(pb)
        if x.tobytes() == y.tobytes():
            continue
        oa = sum(1 for p in x.getdata() if p[3]) / (x.width * x.height)
        ob = sum(1 for p in y.getdata() if p[3]) / (y.width * y.height)
        changed.append((k, ia[k][1], ib.get(k, ["", "?"])[1], round(oa, 3),
                        round(ob, 3), x.size))
    print(f"{len(changed)} changed")
    for c in changed:
        print(c)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--diff"]:
        diff(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        render(Path(sys.argv[1]).resolve(), Path(sys.argv[2]), Path(sys.argv[3]))
