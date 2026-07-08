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
it — or, on macOS, the .app.

Usage:
    python build.py            # build the bundle for THIS platform
    python build.py --clean    # wipe build/ and this platform's dist output first

PyInstaller does not cross-compile: run this under a Windows Python for .exe
files, under macOS for the .app, or under Linux for ELF binaries. The output
names carry the platform, so builds for different OSes coexist in dist/.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = ROOT / "packaging" / "xenosaga1-extractor.spec"
DIST = ROOT / "dist"

PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
BUNDLE = DIST / f"Xenosaga-I-Extractor-{PLATFORM}"
APP = DIST / "Xenosaga-I-Extractor.app"  # macOS only


def main() -> int:
    try:
        from PyInstaller.__main__ import run as pyinstaller
    except ImportError:
        sys.exit(
            "PyInstaller is not installed in this Python.\n"
            "  python -m pip install pyinstaller\n"
            "then re-run:  python build.py"
        )

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
        what = APP.name if sys.platform == "darwin" else f"{BUNDLE.name}/ (zip it)"
        print(f"\nDone. Ship {what}; double-click {APP.name if sys.platform == 'darwin' else gui.name} to launch.")
    else:
        print("\nBuild incomplete — see PyInstaller output above.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
