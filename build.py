#!/usr/bin/env python3
"""build.py — package the extractor into a self-contained one-folder bundle.

Drives ``packaging/xenosaga1-extractor.spec`` to produce, in ``dist/``:

    dist/Xenosaga-I-Extractor-windows/     (built on Windows)
      Xenosaga-I-Extractor.exe             the GUI launcher — double-click this
      xeno-cli.exe                         the engine the GUI drives
      python3X.dll, _internal/...          shared runtime

    dist/Xenosaga-I-Extractor.app          (built on macOS — double-click this)
    dist/Xenosaga-I-Extractor-macos/       (same build, bare folder form)

Both binaries live in ONE folder (the same shape as the Episode III kit), so
the GUI always finds its engine beside it. Ship the platform folder — zip
it — or, on macOS, the .app. After a build the bundle's own self-test is
run (``xeno-cli self-test``), so a bundle that is missing a module fails here.

Usage:
    python build.py            # build the bundle for THIS platform
    python build.py --clean    # wipe build/ and this platform's dist output first

PyInstaller does not cross-compile: run this under a Windows Python for .exe
files, under macOS for the .app, or under Linux for ELF binaries. The output
names carry the platform, so builds for different OSes coexist in dist/.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "packaging" / "xenosaga1-extractor.spec"
DIST = ROOT / "dist"

PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
BUNDLE = DIST / f"Xenosaga-I-Extractor-{PLATFORM}"
APP = DIST / "Xenosaga-I-Extractor.app"  # macOS only


def _run_selftest(argv) -> bool:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=300)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"  could not run {argv[0]}: {exc}")
        return False
    tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ["(no output)"]
    print(f"  {Path(argv[0]).name} {' '.join(argv[1:])}: exit {r.returncode}, "
          + tail[0].encode("ascii", "backslashreplace").decode("ascii"))
    if r.returncode:                    # say which checks failed, not only the last line
        for line in (r.stdout + r.stderr).splitlines():
            if "FAIL" in line or "Traceback" in line or "Error" in line:
                print("    " + line.encode("ascii", "backslashreplace").decode("ascii"))
    return r.returncode == 0


def main() -> int:
    try:
        from PyInstaller.__main__ import run as pyinstaller
    except ImportError:
        sys.exit(
            "PyInstaller is not installed in this Python.\n"
            "  python -m pip install pyinstaller\n"
            "then re-run:  python build.py"
        )

    for mod, why in (("numpy", "the music renderer"), ("fontTools", "new scripts: glyph outlines"),
                     ("uharfbuzz", "new scripts: shaping")):
        try:
            __import__(mod)
        except ImportError:
            sys.exit(f"{mod} ({why}) is not installed in this Python, and the bundle must carry it.\n"
                     "  python -m pip install -r requirements.txt\n"
                     "then re-run:  python build.py")

    if not SPEC.exists():
        sys.exit(f"spec not found: {SPEC}")

    argv = ["--noconfirm", str(SPEC)]
    if "--clean" in sys.argv:
        argv.insert(0, "--clean")
        # Only this platform's outputs — dist/ may hold the other OS's build.
        shutil.rmtree(BUNDLE, ignore_errors=True)
        if sys.platform == "darwin":
            shutil.rmtree(APP, ignore_errors=True)

    print(f"=== pyinstaller {' '.join(argv)} ===", flush=True)
    pyinstaller(argv)

    ext = ".exe" if sys.platform == "win32" else ""
    gui = BUNDLE / f"Xenosaga-I-Extractor{ext}"
    cli = BUNDLE / f"xeno-cli{ext}"
    ok = gui.exists() and cli.exists()
    print("\n" + ("-" * 60))
    print(f"bundle : {BUNDLE}")
    print(f"  GUI  : {gui.name}  {'OK' if gui.exists() else 'MISSING'}")
    print(f"  CLI  : {cli.name}  {'OK' if cli.exists() else 'MISSING'}")
    if sys.platform == "darwin":
        app_ok = (APP / "Contents" / "MacOS" / "Xenosaga-I-Extractor").exists()
        print(f"  app  : {APP.name}  {'OK' if app_ok else 'MISSING'}")
        ok = ok and app_ok
    if ok:
        # a bundle that is missing a module fails here, not on someone else's machine
        print("self-test from the bundle:")
        ok = _run_selftest([str(cli), "self-test"])
    if ok:
        what = APP.name if sys.platform == "darwin" else f"{BUNDLE.name}/ (zip it)"
        print(f"\nDone. Ship {what}; double-click {APP.name if sys.platform == 'darwin' else gui.name} to launch.")
    else:
        print("\nBuild incomplete or the bundled self-test failed; see the output above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
