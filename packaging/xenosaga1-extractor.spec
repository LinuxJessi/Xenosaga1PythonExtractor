# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec — Xenosaga Episode I Python Extractor (release build).

Self-contained, one-folder bundle (the same shape as the Episode III kit).
A single ``COLLECT`` puts BOTH binaries in one directory so the GUI always
finds its engine right next to itself. The folder name carries the platform
so Windows / macOS / Linux builds can coexist in ``dist/``:

    dist/Xenosaga-I-Extractor-windows/     (built on Windows)
      Xenosaga-I-Extractor.exe             windowed launcher — the local web GUI
      xeno-cli.exe                         console; the CLI the GUI shells out to
      _internal/, python3X.dll             shared runtime (deduped across both exes)

    dist/Xenosaga-I-Extractor.app          (built on macOS — double-click this)
    dist/Xenosaga-I-Extractor-macos/       (same build, bare folder form)

Ship the platform folder (zip it) — or, on macOS, the .app. Because both
executables live side by side, ``gui.py``'s frozen path
(``Path(sys.executable).parent / "xeno-cli[.exe]"``) resolves with no
guesswork — inside the .app both land in ``Contents/MacOS/``.

AV-friendliness (mirrors the III kit's reasoning):
* one-folder, not one-file — one-file unpacks to %TEMP% at launch, a classic
  packer heuristic AV engines flag.
* ``upx=False`` — UPX packing is the single strongest "malware" signal.
* GUI ``console=False`` (no scary console window); CLI ``console=True`` so it
  still prints when run directly from a terminal.

PyInstaller does NOT cross-compile — run this on the OS you want binaries for.

Build (from repo root):
    python -m pip install pyinstaller
    pyinstaller --noconfirm --clean packaging/xenosaga1-extractor.spec
"""
import sys
from pathlib import Path

HERE = Path(SPECPATH).resolve()
REPO = HERE.parent

PLATFORM = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")

ICON = HERE / "icon.ico"
ICON_ARG = str(ICON) if ICON.exists() else None

# README/LICENSE ride along so the unpacked folder is self-documenting.
DATAS = [
    (str(REPO / "README.md"), "."),
    (str(REPO / "LICENSE"), "."),
    (str(HERE / "tools" / "TOOLS.txt"), "tools"),
]
DATAS = [(src, dst) for src, dst in DATAS if Path(src).exists()]

# Bundle a portable ffmpeg when one has been dropped into packaging/tools/
# (the release workflow downloads one per OS; locally it's optional). It
# lands in the runtime dir under tools/, where browse.py's detect_ffmpeg()
# probes first — so movie conversion works with zero installs.
FFMPEG = HERE / "tools" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
BINARIES = [(str(FFMPEG), "tools")] if FFMPEG.exists() else []

# cli.py imports these at module top, so Analysis finds them anyway; listing
# them keeps the bundle correct even if a future refactor imports them lazily.
HIDDEN = ["arx", "browse", "carve", "chains", "evt", "iso9660", "toc"]


def _ver(name):
    p = HERE / name
    return str(p) if p.exists() else None


def _analysis(entry):
    return Analysis(
        [str(REPO / entry)],
        pathex=[str(REPO)],
        binaries=BINARIES,
        datas=DATAS,
        hiddenimports=HIDDEN,
        hookspath=[],
        runtime_hooks=[],
        excludes=["tkinter", "test", "unittest"],
        noarchive=False,
    )


gui_a = _analysis("gui.py")
cli_a = _analysis("cli.py")

gui_pyz = PYZ(gui_a.pure, gui_a.zipped_data)
cli_pyz = PYZ(cli_a.pure, cli_a.zipped_data)

gui_exe = EXE(
    gui_pyz,
    gui_a.scripts,
    [],
    exclude_binaries=True,
    name="Xenosaga-I-Extractor",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=ICON_ARG,
    version=_ver("version_info_gui.txt"),
)

cli_exe = EXE(
    cli_pyz,
    cli_a.scripts,
    [],
    exclude_binaries=True,
    name="xeno-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    icon=ICON_ARG,
    version=_ver("version_info_cli.txt"),
)

coll = COLLECT(
    gui_exe,
    gui_a.binaries,
    gui_a.zipfiles,
    gui_a.datas,
    cli_exe,
    cli_a.binaries,
    cli_a.zipfiles,
    cli_a.datas,
    strip=False,
    upx=False,
    name=f"Xenosaga-I-Extractor-{PLATFORM}",
)

# macOS: also wrap the folder into a double-clickable .app. Both executables
# end up in Contents/MacOS/, so the GUI's sibling lookup for xeno-cli still
# resolves. PyInstaller ad-hoc-signs the bundle; a locally built app opens
# with a plain double-click, a downloaded one needs right-click -> Open once
# (unsigned developer) — see the README.
if sys.platform == "darwin":
    ICNS = HERE / "icon.icns"
    app = BUNDLE(
        coll,
        name="Xenosaga-I-Extractor.app",
        icon=str(ICNS) if ICNS.exists() else None,
        bundle_identifier="io.github.linuxjessi.xenosaga1extractor",
        info_plist={
            "CFBundleName": "Xenosaga I Extractor",
            "CFBundleDisplayName": "Xenosaga I Extractor",
            "NSHighResolutionCapable": True,
        },
    )
