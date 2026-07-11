"""gui.py — Local web GUI for the Xenosaga Episode I Python Extractor.

Run me directly:
    python gui.py

Stdlib only. Starts an HTTP server on a free localhost port, opens your
browser, and shells out to ``cli.py`` for the real work. Subprocess output
streams back via Server-Sent Events so you can watch progress live.
"""
from __future__ import annotations

import http.server
import json
import os
import queue
import shlex
import socket
import socketserver
import string
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
CLI = ROOT / "cli.py"
PY = sys.executable or "python3"

FROZEN = bool(getattr(sys, "frozen", False))
if FROZEN:
    _cli_name = "xeno-cli.exe" if os.name == "nt" else "xeno-cli"
    _cli_exe = Path(sys.executable).resolve().parent / _cli_name
    CLI_ARGV: list[str] = [str(_cli_exe)]
    CLI_TARGET = _cli_exe
else:
    CLI_ARGV = [PY, str(CLI)]
    CLI_TARGET = CLI


def _fatal(title: str, body: str) -> None:
    msg = f"{title}\n\n{body}"
    print(f"[gui] FATAL: {msg}", file=sys.stderr)
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, body, f"Xenosaga I Extractor — {title}", 0x10
            )
        except Exception:
            pass
    elif sys.platform == "darwin":
        # Double-clicked .app has no console — surface the error in a dialog.
        try:
            esc = body.replace("\\", "\\\\").replace('"', '\\"')
            subprocess.run(
                ["osascript", "-e",
                 f'display dialog "{esc}" with title '
                 f'"Xenosaga I Extractor — {title}" '
                 'buttons {"OK"} default button 1 with icon stop'],
                timeout=120, check=False,
            )
        except Exception:
            pass
    sys.exit(2)


# ---------------------------------------------------------------------------
# Job runner — one subprocess per launched command, lines streamed to clients
# ---------------------------------------------------------------------------

class Job:
    def __init__(self, cmd: list[str], cwd: Path | None):
        self.id = uuid.uuid4().hex[:12]
        self.cmd = cmd
        self.cwd = str(cwd or ROOT)
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.exit_code: int | None = None
        self.lines: list[str] = []
        self.subscribers: list[queue.Queue[str | None]] = []
        self._lock = threading.Lock()
        self._proc = subprocess.Popen(
            cmd,
            cwd=self.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.lines.append(f"$ {' '.join(shlex.quote(c) for c in cmd)}")
        threading.Thread(target=self._pump, daemon=True).start()

    def _broadcast(self, line: str | None) -> None:
        with self._lock:
            subs = list(self.subscribers)
        for q in subs:
            try:
                q.put_nowait(line)
            except queue.Full:
                pass

    def _pump(self) -> None:
        assert self._proc.stdout is not None
        for line in self._proc.stdout:
            line = line.rstrip("\r\n")
            self.lines.append(line)
            self._broadcast(line)
        rc = self._proc.wait()
        self.exit_code = rc
        self.ended_at = time.time()
        summary = f"[exit {rc}]"
        self.lines.append(summary)
        self._broadcast(summary)
        self._broadcast(None)

    def subscribe(self) -> queue.Queue[str | None]:
        q: queue.Queue[str | None] = queue.Queue(maxsize=50000)
        with self._lock:
            self.subscribers.append(q)
            for line in self.lines:
                try:
                    q.put_nowait(line)
                except queue.Full:
                    break
            if self.ended_at is not None:
                q.put_nowait(None)
        return q

    def unsubscribe(self, q: queue.Queue[str | None]) -> None:
        with self._lock:
            try:
                self.subscribers.remove(q)
            except ValueError:
                pass

    def status(self) -> str:
        if self.exit_code is None:
            return "running"
        return "ok" if self.exit_code == 0 else "err"

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "cmd": self.cmd,
            "status": self.status(),
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
        }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Command builders
# ---------------------------------------------------------------------------

def _str(form, key, default=""):
    v = form.get(key)
    if v is None or v == "":
        return default
    if isinstance(v, list):
        v = v[0]
    return str(v).strip()


def _bool(form, key):
    v = form.get(key)
    if isinstance(v, list):
        v = v[0] if v else ""
    return str(v).lower() in ("1", "true", "yes", "on")


def build_list(form):
    iso = _str(form, "iso")
    if not iso:
        raise ValueError("ISO path is required")
    args = [*CLI_ARGV, "list", "--iso", iso]
    chain = _str(form, "chain")
    if chain in ("0", "1"):
        args += ["--chain", chain]
    return args


def build_extract(form):
    iso = _str(form, "iso")
    out = _str(form, "out")
    if not iso:
        raise ValueError("ISO path is required")
    if not out:
        raise ValueError("Output directory is required")
    args = [*CLI_ARGV, "extract", "--iso", iso, "--out", out]
    chain = _str(form, "chain")
    if chain in ("0", "1"):
        args += ["--chain", chain]
    glob = _str(form, "glob")
    if glob:
        args += ["--glob", glob]
    if _bool(form, "no_carve"):
        args.append("--no-carve")
    if _bool(form, "code"):
        args.append("--code")
    return args


def build_classes(form):
    iso = _str(form, "iso")
    out = _str(form, "out")
    if not iso:
        raise ValueError("ISO path is required")
    if not out:
        raise ValueError("Output directory is required")
    return [*CLI_ARGV, "classes", "--iso", iso, "--out", out]


def build_browse(form):
    out = _str(form, "out")
    if not out:
        raise ValueError("Output directory is required")
    kinds = [k for k in ("textures", "audio", "banks", "images", "text", "movies")
             if _bool(form, k)]
    if not kinds:
        raise ValueError("Pick at least one kind to convert")
    args = [*CLI_ARGV, "browse", "--out", out]
    if len(kinds) < 6:
        args += ["--kinds", ",".join(kinds)]
    rate = _str(form, "rate")
    if rate and rate != "48000":
        args += ["--rate", rate]
    return args


def build_verify(form):
    out = _str(form, "out")
    if not out:
        raise ValueError("Output directory is required")
    return [*CLI_ARGV, "verify", "--out", out]


def build_pinkhair(form):
    iso = _str(form, "iso")
    if not iso:
        raise ValueError("ISO path is required")
    dry = _bool(form, "dry_run")
    out = _str(form, "out")
    if not out and not dry:
        raise ValueError("Output ISO path is required (or tick Dry run)")
    args = [*CLI_ARGV, "pinkhair", "--iso", iso]
    if out:
        args += ["--out", out]
    hue = _str(form, "hue")
    if hue and hue != "0.92":
        args += ["--hue", hue]
    if dry:
        args.append("--dry-run")
    return args


def build_patch(form):
    iso = _str(form, "iso")
    out = _str(form, "out")
    sets = _str(form, "sets")
    if not iso:
        raise ValueError("ISO path is required")
    if not out:
        raise ValueError("Output ISO path is required")
    specs = [s.strip() for s in sets.split(";") if s.strip()]
    if not specs:
        raise ValueError(
            "At least one replacement is required "
            "(chainN:toc\\path=localfile, separate several with ';')")
    args = [*CLI_ARGV, "patch", "--iso", iso, "--out", out]
    for s in specs:
        args += ["--set", s]
    return args


def build_text_export(form):
    iso = _str(form, "iso")
    out = _str(form, "out")
    if not iso:
        raise ValueError("ISO path is required")
    if not out:
        raise ValueError("Text directory is required")
    return [*CLI_ARGV, "text-export", "--iso", iso, "--out", out]


def build_text_import(form):
    iso = _str(form, "iso")
    text = _str(form, "text")
    out = _str(form, "out")
    if not iso:
        raise ValueError("ISO path is required")
    if not text:
        raise ValueError("Edited text directory is required")
    if not out:
        raise ValueError("Output ISO path is required")
    return [*CLI_ARGV, "text-import", "--iso", iso, "--text", text, "--out", out]


BUILDERS = {
    "list":        build_list,
    "extract":     build_extract,
    "classes":     build_classes,
    "browse":      build_browse,
    "verify":      build_verify,
    "pinkhair":    build_pinkhair,
    "patch":       build_patch,
    "text-export": build_text_export,
    "text-import": build_text_import,
}


# ---------------------------------------------------------------------------
# Auto-detect helpers
# ---------------------------------------------------------------------------

def _walk_for_isos(root: Path, max_depth: int, want: int, out: list[Path]) -> None:
    if len(out) >= want or max_depth < 0:
        return
    try:
        entries = list(root.iterdir())
    except (OSError, PermissionError):
        return
    entries.sort(key=lambda p: (not p.is_file(), p.name.lower()))
    for child in entries:
        if len(out) >= want:
            return
        try:
            if child.is_file():
                if child.suffix.lower() == ".iso":
                    try:
                        sz = child.stat().st_size
                    except OSError:
                        continue
                    if sz > 100 * 1024 * 1024:
                        out.append(child)
            elif child.is_dir() and not child.name.startswith("."):
                _walk_for_isos(child, max_depth - 1, want, out)
        except (OSError, PermissionError):
            continue


def detect_isos() -> list[str]:
    candidates: list[Path] = []
    locations: list[tuple[Path, int]] = [
        (ROOT.parent, 2),
        (Path.home() / "Downloads", 2),
        (Path.home() / "Desktop", 3),
        (Path.home() / "Documents", 3),
    ]
    for root, depth in locations:
        try:
            if not root.exists() or not root.is_dir():
                continue
        except OSError:
            continue
        _walk_for_isos(root, depth, want=10, out=candidates)
    seen: set[str] = set()
    out: list[str] = []
    for p in candidates:
        s = str(p)
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def detect_deps() -> dict:
    return {
        "python": sys.version.split()[0],
        "cli": str(CLI_TARGET) if CLI_TARGET.exists() else "",
    }


def _prefer_xs1(isos: list[str]) -> str | None:
    """Return the Episode I ISO from the list, preferring xs1/ dir or 'Wille' in name."""
    return next(
        (p for p in isos if "/xs1/" in p or "xs1\\" in p or "Wille" in p),
        isos[0] if isos else None,
    )


def default_iso() -> str:
    return _prefer_xs1(detect_isos()) or ""


def default_out_dir() -> str:
    best = _prefer_xs1(detect_isos())
    if best:
        return str(Path(best).parent / "out")
    return str(Path.home() / "xs1" / "out")


# ---------------------------------------------------------------------------
# Filesystem browser
# ---------------------------------------------------------------------------

def list_roots() -> list[str]:
    roots: list[str] = []
    if os.name == "nt":
        for d in string.ascii_uppercase:
            p = Path(f"{d}:\\")
            if p.exists():
                roots.append(str(p))
    else:
        roots.append("/")
        roots.append(str(Path.home()))
        for parent in ("/mnt", "/media", "/Volumes"):
            try:
                pp = Path(parent)
                if pp.is_dir():
                    for child in sorted(pp.iterdir()):
                        if child.is_dir():
                            roots.append(str(child))
            except (OSError, PermissionError):
                continue
    return list(dict.fromkeys(roots))


def list_dir(path_str: str, ext_filter: str | None = None) -> dict:
    p = Path(path_str).expanduser()
    if not p.exists():
        p = Path(str(Path.home()))
    if not p.is_dir():
        p = p.parent
    p = p.resolve()

    entries: list[dict] = []
    err: str | None = None
    try:
        children = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    except (OSError, PermissionError) as exc:
        children = []
        err = f"{exc}"

    for child in children:
        try:
            if child.is_symlink() and not child.exists():
                continue
            if child.is_dir():
                if child.name.startswith(".") and not child.name.startswith(".."):
                    continue
                entries.append({"name": child.name, "type": "dir", "path": str(child)})
            else:
                if ext_filter:
                    if child.suffix.lower().lstrip(".") != ext_filter.lower().lstrip("."):
                        continue
                stat = child.stat()
                entries.append({
                    "name": child.name, "type": "file",
                    "path": str(child), "size": stat.st_size,
                })
        except (OSError, PermissionError):
            continue

    parent = None
    try:
        if p.parent != p:
            parent = str(p.parent)
    except OSError:
        parent = None

    return {
        "path": str(p),
        "parent": parent,
        "entries": entries,
        "roots": list_roots(),
        "error": err,
    }


# ---------------------------------------------------------------------------
# Embedded UI
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>XENOSAGA I · Extractor</title>
<style>
:root {
  --bg-deep:        #04070d;
  --bg-mid:         #0a1525;
  --bg-panel:       #0c1828;
  --bg-card:        #0e1c30;
  --bg-input:       #060d18;
  --border:         rgba(93, 213, 255, 0.16);
  --border-strong:  rgba(93, 213, 255, 0.38);
  --fg:             #d8e8f5;
  --muted:          #6e8aab;
  --accent:         #5dd5ff;
  --accent-warm:    #ff9d3a;
  --accent-glow:    rgba(93, 213, 255, 0.35);
  --ok:             #62ffa1;
  --warn:           #ffc15d;
  --err:            #ff6e7f;
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body {
  margin: 0;
  background:
    radial-gradient(ellipse 70% 50% at 50% 0%, rgba(93, 213, 255, 0.06) 0%, transparent 70%),
    linear-gradient(180deg, var(--bg-deep) 0%, #02040a 100%);
  color: var(--fg);
  font-family: "Inter", -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 14px; line-height: 1.5;
  overflow: hidden;
}
body::before {
  content: '';
  position: fixed; inset: 0; pointer-events: none; z-index: 1000;
  background: repeating-linear-gradient(180deg,
    transparent 0, transparent 2px,
    rgba(255, 255, 255, 0.012) 2px, rgba(255, 255, 255, 0.012) 3px);
  mix-blend-mode: lighten;
}

header {
  padding: 20px 28px 18px;
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 24px;
  background: linear-gradient(180deg, rgba(10, 21, 37, 0.92) 0%, rgba(10, 21, 37, 0.55) 100%);
  position: relative;
  backdrop-filter: blur(2px);
}
header::after {
  content: ''; position: absolute; left: 0; right: 0; bottom: -1px; height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent) 50%, transparent);
  opacity: 0.5;
}
header .title { display: flex; flex-direction: column; gap: 2px; line-height: 1; }
header h1 {
  margin: 0;
  font-family: "Cormorant Garamond", "Cormorant", "Georgia", serif;
  font-size: 28px; font-weight: 400; letter-spacing: 0.12em;
  text-transform: uppercase;
}
header h1 .i {
  color: var(--accent-warm);
  font-style: italic; font-weight: 500;
  margin: 0 0.15em;
  text-shadow: 0 0 14px rgba(255, 157, 58, 0.5);
}
header h1 .sep {
  color: var(--accent); opacity: 0.7; margin: 0 0.5em;
  font-weight: 300;
}
header h1 .extractor {
  font-family: "Inter", system-ui, sans-serif;
  font-size: 13px; letter-spacing: 0.35em; color: var(--muted);
  font-style: normal; font-weight: 500;
}
header .tagline {
  color: var(--muted); font-style: italic;
  font-family: "Cormorant Garamond", "Georgia", serif;
  font-size: 14px; letter-spacing: 0.04em;
  margin-top: 4px;
}
header .actions { margin-left: auto; display: flex; gap: 8px; }

main {
  display: grid; grid-template-columns: 1fr 480px;
  height: calc(100vh - 82px);
}
#cards {
  padding: 22px 28px 80px; overflow-y: auto;
}
#cards::-webkit-scrollbar { width: 10px; }
#cards::-webkit-scrollbar-thumb { background: rgba(93, 213, 255, 0.18); border-radius: 5px; }
#cards::-webkit-scrollbar-track { background: transparent; }

#right {
  border-left: 1px solid var(--border);
  background: linear-gradient(180deg, rgba(12, 24, 40, 0.85) 0%, rgba(8, 16, 28, 0.95) 100%);
  display: flex; flex-direction: column; min-height: 0;
}
#right h2 {
  font-family: "Cormorant Garamond", "Georgia", serif;
  font-size: 14px; font-weight: 500;
  margin: 14px 16px 6px;
  color: var(--accent);
  text-transform: uppercase; letter-spacing: 0.22em;
  position: relative; padding-left: 14px;
}
#right h2::before {
  content: ''; position: absolute; left: 0; top: 50%; width: 7px; height: 1px;
  background: var(--accent); transform: translateY(-50%);
  box-shadow: 0 0 6px var(--accent-glow);
}

#status-box { padding: 0 16px 14px; }
#status-box .row {
  display: flex; justify-content: space-between;
  padding: 5px 0; font-size: 13px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}
#status-box .row:last-child { border-bottom: 0; }
#status-box .row span:first-child {
  color: var(--muted); font-size: 12px; letter-spacing: 0.04em; text-transform: uppercase;
}
#status-box .row .v {
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
  color: var(--muted); max-width: 60%;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
#status-box .row .v.ok  { color: var(--ok); }
#status-box .row .v.bad { color: var(--err); }

#log {
  flex: 1; margin: 0; padding: 14px 16px;
  background: #02050b; color: #b8d3eb;
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px; line-height: 1.55;
  overflow-y: auto; white-space: pre-wrap;
  border-top: 1px solid var(--border);
  min-height: 0;
}
#log::-webkit-scrollbar { width: 8px; }
#log::-webkit-scrollbar-thumb { background: rgba(93, 213, 255, 0.2); border-radius: 4px; }

.card {
  background: linear-gradient(180deg, rgba(14, 28, 48, 0.7) 0%, rgba(10, 21, 37, 0.7) 100%);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 16px 18px;
  margin-bottom: 12px;
  transition: border-color 0.18s, box-shadow 0.18s;
  position: relative;
}
.card::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 2px;
  background: transparent; transition: background 0.18s, box-shadow 0.18s;
  border-radius: 6px 0 0 6px;
}
.card.run { border-color: var(--accent); box-shadow: 0 0 22px rgba(93, 213, 255, 0.12); }
.card.run::before { background: var(--accent); box-shadow: 0 0 8px var(--accent-glow); }
.card.ok  { border-color: rgba(98, 255, 161, 0.4); }
.card.ok::before  { background: var(--ok); }
.card.err { border-color: rgba(255, 110, 127, 0.5); }
.card.err::before { background: var(--err); }

.card-head {
  display: flex; align-items: baseline; gap: 14px;
  cursor: pointer; user-select: none;
}
.card-head h3 {
  margin: 0;
  font-family: "Cormorant Garamond", "Georgia", serif;
  font-size: 18px; font-weight: 500;
  letter-spacing: 0.04em;
}
.card-head .step {
  font-family: "Cormorant Garamond", "Georgia", serif;
  color: var(--accent-warm); font-style: italic;
  font-size: 16px; min-width: 26px;
}
.card-head .badge {
  font-size: 10px; padding: 3px 9px; border-radius: 2px;
  background: rgba(110, 138, 171, 0.12);
  color: var(--muted);
  margin-left: auto;
  text-transform: uppercase; letter-spacing: 0.14em; font-weight: 600;
}
.card-head .badge.run { background: rgba(93, 213, 255, 0.18);  color: var(--accent); }
.card-head .badge.ok  { background: rgba(98, 255, 161, 0.18);  color: var(--ok); }
.card-head .badge.err { background: rgba(255, 110, 127, 0.18); color: var(--err); }
.card-head .chev {
  color: var(--muted); transition: transform 0.2s;
  margin-left: 10px; font-size: 12px;
}
.card.open .chev { transform: rotate(180deg); }

.card-body { display: none; margin-top: 14px; }
.card.open .card-body { display: block; }
.card-body p.help {
  margin: 0 0 14px; color: var(--muted); font-size: 13px; line-height: 1.55;
}
.card-body p.help code { color: var(--accent); font-size: 12px; }

.card .row {
  display: grid; grid-template-columns: 130px 1fr; gap: 12px;
  margin-bottom: 9px; align-items: center;
}
.card .row label {
  color: var(--muted); font-size: 12px;
  text-transform: uppercase; letter-spacing: 0.08em;
}
.card .row .with-pick {
  display: grid; grid-template-columns: 1fr auto; gap: 6px;
}
.card .row .check-wrap {
  display: flex; align-items: center; gap: 10px;
}
.card .row .check-wrap span { color: var(--muted); font-size: 12px; }

.card input[type=text], .card textarea, .card select {
  width: 100%; background: var(--bg-input); color: var(--fg);
  border: 1px solid var(--border);
  border-radius: 3px; padding: 8px 10px;
  font: inherit;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  font-size: 12.5px; transition: border-color 0.15s, box-shadow 0.15s;
}
.card input[type=text]:focus, .card textarea:focus, .card select:focus {
  outline: none; border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent-glow), 0 0 10px rgba(93, 213, 255, 0.18);
}
.card select option { background: var(--bg-mid); }
.card input[type=checkbox] {
  accent-color: var(--accent); width: 16px; height: 16px;
}
.card textarea { min-height: 60px; resize: vertical; line-height: 1.5; }
.card .actions { margin-top: 14px; display: flex; gap: 8px; }

button {
  background: linear-gradient(180deg, #5dd5ff 0%, #3da8d8 100%);
  color: #02101e; border: 0;
  padding: 8px 16px; border-radius: 3px;
  font: inherit; font-weight: 600; font-size: 12px;
  letter-spacing: 0.1em; text-transform: uppercase;
  cursor: pointer; transition: filter 0.15s, transform 0.05s;
  box-shadow: 0 0 12px rgba(93, 213, 255, 0.25);
}
button:hover { filter: brightness(1.15); }
button:active { transform: translateY(1px); }
button.ghost {
  background: transparent; color: var(--fg);
  border: 1px solid var(--border);
  font-weight: 500; box-shadow: none;
}
button.ghost:hover { background: rgba(93, 213, 255, 0.08); border-color: var(--accent); }
button.pick {
  padding: 7px 12px; font-size: 11px;
  background: rgba(93, 213, 255, 0.12); color: var(--accent);
  border: 1px solid var(--border); box-shadow: none; font-weight: 500;
}
button.pick:hover { background: rgba(93, 213, 255, 0.2); border-color: var(--accent); }
button:disabled { opacity: 0.4; cursor: not-allowed; }

code, kbd {
  background: rgba(93, 213, 255, 0.08); color: var(--accent);
  border-radius: 2px; padding: 1px 5px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
}

/* ------------------------------------------------------------------ */
/* File picker modal                                                   */
/* ------------------------------------------------------------------ */
.modal[hidden] { display: none; }
.modal {
  position: fixed; inset: 0; z-index: 100;
  display: flex; align-items: center; justify-content: center;
}
.modal-backdrop {
  position: absolute; inset: 0;
  background: rgba(2, 5, 11, 0.82);
  backdrop-filter: blur(4px);
  animation: fadeIn 0.15s ease-out;
}
.modal-window {
  position: relative; z-index: 1;
  background: linear-gradient(180deg, var(--bg-card) 0%, var(--bg-mid) 100%);
  border: 1px solid var(--border-strong);
  box-shadow: 0 0 0 1px var(--border), 0 0 80px rgba(93, 213, 255, 0.18), 0 20px 60px rgba(0,0,0,0.5);
  border-radius: 8px;
  width: 760px; max-width: 92vw; height: 580px; max-height: 88vh;
  display: flex; flex-direction: column;
  animation: slideIn 0.18s ease-out;
  overflow: hidden;
}
@keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
@keyframes slideIn {
  from { opacity: 0; transform: translateY(-8px) scale(0.985); }
  to   { opacity: 1; transform: translateY(0) scale(1); }
}
.modal-header {
  padding: 16px 22px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 12px;
  background: linear-gradient(180deg, rgba(93, 213, 255, 0.04), transparent);
}
.modal-header h2 {
  margin: 0;
  font-family: "Cormorant Garamond", "Georgia", serif;
  font-size: 18px; font-weight: 500; letter-spacing: 0.06em;
}
.modal-header .x {
  margin-left: auto; background: transparent; border: 0; color: var(--muted);
  font-size: 22px; cursor: pointer; padding: 0 6px; box-shadow: none;
  text-transform: none; letter-spacing: 0;
}
.modal-header .x:hover { color: var(--err); background: transparent; }
.fs-toolbar {
  padding: 12px 18px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 8px;
  background: rgba(2, 5, 11, 0.3);
}
.fs-crumb {
  flex: 1; overflow-x: auto; white-space: nowrap;
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 12px;
}
.fs-crumb a { color: var(--accent); cursor: pointer; padding: 2px 4px; border-radius: 2px; }
.fs-crumb a:hover { background: rgba(93, 213, 255, 0.12); }
.fs-crumb .sep { color: var(--muted); margin: 0 2px; opacity: 0.6; }
.fs-list { flex: 1; overflow-y: auto; padding: 4px 6px; }
.fs-list::-webkit-scrollbar { width: 10px; }
.fs-list::-webkit-scrollbar-thumb { background: rgba(93, 213, 255, 0.18); border-radius: 5px; }
.fs-entry {
  display: flex; align-items: center; gap: 12px;
  padding: 8px 12px; cursor: pointer; border-radius: 3px; font-size: 13px;
}
.fs-entry:hover { background: rgba(93, 213, 255, 0.08); }
.fs-entry.selected { background: rgba(93, 213, 255, 0.18); outline: 1px solid var(--accent); }
.fs-entry .icon { width: 18px; text-align: center; opacity: 0.85; font-size: 14px; }
.fs-entry .icon.dir { color: var(--accent-warm); }
.fs-entry .name { flex: 1; }
.fs-entry .size {
  margin-left: auto; color: var(--muted); font-size: 11px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
}
.fs-empty { padding: 30px; text-align: center; color: var(--muted); font-size: 13px; }
.fs-roots {
  border-top: 1px solid var(--border);
  padding: 8px 18px; display: flex; flex-wrap: wrap; gap: 6px;
  font-size: 11px; align-items: center;
}
.fs-roots span { color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; margin-right: 4px; }
.fs-roots a {
  color: var(--accent); padding: 3px 8px; border-radius: 2px;
  border: 1px solid var(--border); cursor: pointer;
  font-family: ui-monospace, "SF Mono", Menlo, monospace; font-size: 11px;
}
.fs-roots a:hover { background: rgba(93, 213, 255, 0.12); border-color: var(--accent); }
.modal-footer {
  padding: 14px 18px; border-top: 1px solid var(--border);
  display: flex; align-items: center; gap: 10px;
  background: rgba(2, 5, 11, 0.3);
}
.modal-footer .sel-path {
  flex: 1; min-width: 0; color: var(--muted); font-size: 12px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.modal-footer .sel-path.has { color: var(--fg); }
.modal-error {
  padding: 8px 18px; color: var(--err); font-size: 12px;
  background: rgba(255, 110, 127, 0.06);
  border-bottom: 1px solid rgba(255, 110, 127, 0.2);
}
.fs-quick {
  padding: 8px 18px; border-bottom: 1px solid var(--border);
  display: flex; flex-wrap: wrap; gap: 6px; align-items: center;
  background: rgba(93, 213, 255, 0.03);
}
.fs-quick span { color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.1em; }
.fs-quick a {
  color: var(--fg); padding: 4px 10px; border-radius: 2px;
  border: 1px solid var(--border); cursor: pointer; font-size: 12px;
  font-family: ui-monospace, "SF Mono", Menlo, monospace;
  white-space: nowrap; max-width: 320px; overflow: hidden; text-overflow: ellipsis;
}
.fs-quick a:hover { background: rgba(93, 213, 255, 0.12); border-color: var(--accent); color: var(--accent); }
.fs-quick a.selected { background: rgba(93, 213, 255, 0.18); border-color: var(--accent); color: var(--accent); }
</style>
</head>
<body>
<header>
  <div class="title">
    <h1>
      <span>Xenosaga</span><span class="i"> I</span>
      <span class="sep">·</span><span class="extractor">EXTRACTOR</span>
    </h1>
    <div class="tagline">Der Wille zur Macht · disc unpacker</div>
  </div>
  <div class="actions">
    <button class="ghost" id="btn-refresh">Refresh status</button>
  </div>
</header>
<main>
  <section id="cards"></section>
  <aside id="right">
    <h2>Environment</h2>
    <div id="status-box"></div>
    <h2>Output</h2>
    <pre id="log"></pre>
  </aside>
</main>

<!-- File picker modal -->
<div class="modal" id="fs-modal" hidden>
  <div class="modal-backdrop" data-close></div>
  <div class="modal-window">
    <div class="modal-header">
      <h2 id="fs-title">Select…</h2>
      <button type="button" class="x" data-close>×</button>
    </div>
    <div class="fs-toolbar">
      <button type="button" class="ghost" id="fs-up">↑ Up</button>
      <div class="fs-crumb" id="fs-crumb"></div>
    </div>
    <div class="modal-error" id="fs-error" hidden></div>
    <div class="fs-quick" id="fs-quick" hidden></div>
    <div class="fs-list" id="fs-list"></div>
    <div class="fs-roots" id="fs-roots"></div>
    <div class="modal-footer">
      <div class="sel-path" id="fs-sel">No selection</div>
      <button type="button" class="ghost" data-close>Cancel</button>
      <button type="button" id="fs-select" disabled>Select</button>
    </div>
  </div>
</div>

<script>
const log = document.getElementById('log');
let currentEventSource = null;
const cardEls = {};
let _detectedIsos = [];

// ------------------------------------------------------------------ helpers
function setStatus(name, status) {
  const el = cardEls[name];
  if (!el) return;
  el.classList.remove('run','ok','err');
  if (status) el.classList.add(status);
  const badge = el.querySelector('.badge');
  badge.classList.remove('run','ok','err');
  badge.textContent = (status || 'idle');
  if (status) badge.classList.add(status);
}

function appendLog(line) {
  log.textContent += line + '\n';
  log.scrollTop = log.scrollHeight;
}

function fmtSize(bytes) {
  if (bytes < 1024) return bytes + ' B';
  const units = ['KB','MB','GB','TB'];
  let v = bytes / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(v >= 10 ? 0 : 1) + ' ' + units[i];
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}

// ------------------------------------------------------------------ runner
async function runCommand(name, formData) {
  if (currentEventSource) currentEventSource.close();
  log.textContent = '';
  setStatus(name, 'run');
  const res = await fetch('/run/' + name, {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: formData,
  });
  if (!res.ok) {
    const t = await res.text();
    let msg = t;
    try { const j = JSON.parse(t); if (j && j.error) msg = j.error; } catch (_) {}
    setStatus(name, 'err');
    appendLog('[error] ' + msg);
    return;
  }
  const {job_id} = await res.json();
  const es = new EventSource('/stream/' + job_id);
  currentEventSource = es;
  es.onmessage = (e) => {
    if (e.data === '__eof__') {
      es.close();
      fetch('/job/' + job_id).then(r => r.json()).then(j => {
        setStatus(name, j.status === 'ok' ? 'ok' : 'err');
      });
    } else {
      appendLog(e.data);
    }
  };
  es.onerror = () => { es.close(); };
}

// ------------------------------------------------------------------ picker
const fsModal  = document.getElementById('fs-modal');
const fsTitle  = document.getElementById('fs-title');
const fsCrumb  = document.getElementById('fs-crumb');
const fsList   = document.getElementById('fs-list');
const fsRoots  = document.getElementById('fs-roots');
const fsError  = document.getElementById('fs-error');
const fsQuick  = document.getElementById('fs-quick');
const fsSel    = document.getElementById('fs-sel');
const fsSelectBtn = document.getElementById('fs-select');
const fsUp     = document.getElementById('fs-up');

const fsState = { mode: 'file', filter: null, current: null,
                  parent: null, selected: null, onChoose: null };

function _renderQuickPick() {
  const isos = fsState.filter === 'iso' ? _detectedIsos : [];
  if (!isos.length) { fsQuick.hidden = true; return; }
  fsQuick.hidden = false;
  fsQuick.innerHTML = '<span>Detected:</span>' + isos.map(p =>
    `<a class="qp" data-path="${escapeHtml(p)}" title="${escapeHtml(p)}">${escapeHtml(p.split('/').pop().split('\\').pop())}</a>`
  ).join('');
  fsQuick.querySelectorAll('.qp').forEach(a => {
    a.addEventListener('click', () => {
      fsQuick.querySelectorAll('.qp').forEach(x => x.classList.remove('selected'));
      a.classList.add('selected');
      fsState.selected = a.dataset.path;
      fsSel.classList.add('has'); fsSel.textContent = a.dataset.path;
      fsSelectBtn.disabled = false;
    });
    a.addEventListener('dblclick', () => {
      fsState.selected = a.dataset.path;
      chooseAndClose();
    });
  });
}

function openPicker({mode, filter, startPath, title, onChoose}) {
  fsState.mode = mode; fsState.filter = filter || null;
  fsState.current = null; fsState.parent = null;
  fsState.selected = null; fsState.onChoose = onChoose;
  fsTitle.textContent = title || (mode === 'dir' ? 'Select a folder' : 'Select a file');
  fsSel.classList.remove('has'); fsSel.textContent = 'No selection';
  fsSelectBtn.disabled = mode === 'file';
  fsError.hidden = true;
  _renderQuickPick();
  fsModal.hidden = false;
  loadDir(startPath || '');
}

function closePicker() { fsModal.hidden = true; fsState.onChoose = null; }

async function loadDir(path) {
  fsError.hidden = true;
  try {
    const qs = new URLSearchParams();
    if (path) qs.set('path', path);
    if (fsState.filter) qs.set('filter', fsState.filter);
    const res = await fetch('/browse-fs?' + qs.toString());
    if (!res.ok) { fsError.hidden = false; fsError.textContent = 'Cannot read directory.'; return; }
    const data = await res.json();
    if (data.error) { fsError.hidden = false; fsError.textContent = data.error; }
    fsState.current = data.path; fsState.parent = data.parent;
    fsState.selected = null;
    if (fsState.mode === 'dir') {
      fsState.selected = data.path; fsSel.classList.add('has');
      fsSel.textContent = data.path; fsSelectBtn.disabled = false;
    } else {
      fsSel.classList.remove('has'); fsSel.textContent = 'No selection'; fsSelectBtn.disabled = true;
    }
    fsUp.disabled = !data.parent;
    renderCrumb(data.path); renderList(data.entries); renderRoots(data.roots);
  } catch (err) {
    fsError.hidden = false;
    fsError.textContent = 'Error loading directory: ' + err.message;
  }
}

function renderCrumb(fullPath) {
  const sep = fullPath.indexOf('\\') >= 0 && fullPath.indexOf('/') < 0 ? '\\' : '/';
  let parts;
  if (sep === '\\') {
    parts = fullPath.split('\\').filter(Boolean);
    if (parts.length && /^[A-Z]:$/i.test(parts[0])) parts[0] = parts[0] + '\\';
  } else {
    parts = ['/'].concat(fullPath.split('/').filter(Boolean));
  }
  let acc = '';
  fsCrumb.innerHTML = parts.map((p, i) => {
    if (sep === '/') acc = (i === 0) ? '/' : (acc.endsWith('/') ? acc : acc + '/') + p;
    else acc = (i === 0) ? p : (acc + (acc.endsWith('\\') ? '' : '\\') + p);
    const a = `<a data-path="${escapeHtml(acc)}">${escapeHtml(p)}</a>`;
    return i === parts.length - 1 ? a : a + '<span class="sep">/</span>';
  }).join('');
  fsCrumb.querySelectorAll('a').forEach(a => a.addEventListener('click', () => loadDir(a.dataset.path)));
}

function renderList(entries) {
  if (!entries.length) {
    fsList.innerHTML = `<div class="fs-empty">(empty${fsState.filter ? ' — no .' + fsState.filter + ' files here' : ''})</div>`;
    return;
  }
  fsList.innerHTML = entries.map(e => {
    const icon = e.type === 'dir' ? '📁' : '📄';
    const iconCls = e.type === 'dir' ? 'icon dir' : 'icon';
    const size = e.type === 'file' ? `<span class="size">${fmtSize(e.size)}</span>` : '';
    return `<div class="fs-entry" data-type="${e.type}" data-path="${escapeHtml(e.path)}">
      <span class="${iconCls}">${icon}</span>
      <span class="name">${escapeHtml(e.name)}</span>${size}
    </div>`;
  }).join('');
  fsList.querySelectorAll('.fs-entry').forEach(el => {
    el.addEventListener('click', () => {
      if (el.dataset.type === 'dir') { loadDir(el.dataset.path); }
      else if (fsState.mode === 'file') {
        fsList.querySelectorAll('.fs-entry.selected').forEach(x => x.classList.remove('selected'));
        el.classList.add('selected');
        fsState.selected = el.dataset.path;
        fsSel.classList.add('has'); fsSel.textContent = el.dataset.path;
        fsSelectBtn.disabled = false;
      }
    });
    el.addEventListener('dblclick', () => {
      if (el.dataset.type === 'dir') loadDir(el.dataset.path);
      else if (fsState.mode === 'file') { fsState.selected = el.dataset.path; chooseAndClose(); }
    });
  });
}

function renderRoots(roots) {
  fsRoots.innerHTML = '<span>Roots:</span>' +
    roots.map(r => `<a data-path="${escapeHtml(r)}">${escapeHtml(r)}</a>`).join('');
  fsRoots.querySelectorAll('a').forEach(a => a.addEventListener('click', () => loadDir(a.dataset.path)));
}

function chooseAndClose() {
  if (!fsState.selected) return;
  const cb = fsState.onChoose; closePicker(); if (cb) cb(fsState.selected);
}

fsUp.addEventListener('click', () => { if (fsState.parent) loadDir(fsState.parent); });
fsSelectBtn.addEventListener('click', chooseAndClose);
document.querySelectorAll('[data-close]').forEach(el => el.addEventListener('click', closePicker));
document.addEventListener('keydown', (e) => { if (!fsModal.hidden && e.key === 'Escape') closePicker(); });

// ------------------------------------------------------------------ card builder
function makeCard(step, name, title, help, fields, buttonText) {
  const div = document.createElement('div');
  div.className = 'card';
  div.dataset.name = name;
  const fieldsHtml = fields.map(f => {
    if (f.type === 'select') {
      const opts = (f.options || []).map(([v, l]) =>
        `<option value="${escapeHtml(v)}"${v === (f.value||'') ? ' selected' : ''}>${escapeHtml(l)}</option>`
      ).join('');
      return `<div class="row"><label>${f.label}</label><select name="${f.name}">${opts}</select></div>`;
    }
    if (f.type === 'checkbox') {
      return `<div class="row"><label>${f.label}</label>
        <div class="check-wrap"><input type="checkbox" name="${f.name}" ${f.value ? 'checked' : ''}>
        <span>${f.hint || ''}</span></div></div>`;
    }
    const pick = f.pick;
    const input = `<input type="text" name="${f.name}" placeholder="${f.placeholder || ''}" value="${escapeHtml(f.value || '')}">`;
    if (pick) {
      const label = pick.mode === 'dir' ? 'Browse folder' : 'Browse file';
      return `<div class="row"><label>${f.label}</label>
        <div class="with-pick">${input}
        <button type="button" class="pick"
          data-pick="${pick.mode}"
          data-pick-filter="${pick.filter || ''}"
          data-pick-for="${f.name}">${label}</button></div></div>`;
    }
    return `<div class="row"><label>${f.label}</label>${input}</div>`;
  }).join('');
  div.innerHTML = `
    <div class="card-head">
      <span class="step">${step}.</span>
      <h3>${title}</h3>
      <span class="badge">idle</span>
      <span class="chev">▼</span>
    </div>
    <div class="card-body">
      ${help ? `<p class="help">${help}</p>` : ''}
      <form data-cmd="${name}">${fieldsHtml}
        <div class="actions">
          <button type="submit">${buttonText}</button>
          <button type="button" class="ghost dryrun">Show command</button>
        </div>
      </form>
    </div>`;
  div.querySelector('.card-head').addEventListener('click', () => div.classList.toggle('open'));
  div.querySelectorAll('button.pick').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const targetName = btn.dataset.pickFor;
      const inputEl = btn.closest('.row').querySelector(`input[name="${targetName}"]`);
      openPicker({
        mode: btn.dataset.pick,
        filter: btn.dataset.pickFilter || null,
        startPath: inputEl.value || undefined,
        title: btn.dataset.pick === 'dir'
          ? `Select folder for "${targetName}"`
          : `Select ${btn.dataset.pickFilter ? '.' + btn.dataset.pickFilter : 'file'} for "${targetName}"`,
        onChoose: (p) => { inputEl.value = p; },
      });
    });
  });
  div.querySelector('form').addEventListener('submit', (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    e.target.querySelectorAll('input[type=checkbox]').forEach(cb => fd.set(cb.name, cb.checked ? 'on' : ''));
    runCommand(name, new URLSearchParams(fd).toString());
  });
  div.querySelector('.dryrun').addEventListener('click', (e) => {
    e.stopPropagation();
    const form = e.target.closest('form');
    const fd = new FormData(form);
    form.querySelectorAll('input[type=checkbox]').forEach(cb => fd.set(cb.name, cb.checked ? 'on' : ''));
    fetch('/preview/' + name, {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: new URLSearchParams(fd).toString(),
    }).then(r => r.json()).then(j => {
      log.textContent = '';
      appendLog('# Would run:');
      appendLog(j.cmd.map(c => /\s/.test(c) ? `"${c}"` : c).join(' '));
    });
  });
  cardEls[name] = div;
  return div;
}

// ------------------------------------------------------------------ status
async function loadStatus() {
  const r = await fetch('/detect');
  const d = await r.json();
  _detectedIsos = d.isos || [];
  const box = document.getElementById('status-box');
  const rows = [
    ['Python',          d.deps.python,                    true],
    ['CLI',             d.deps.cli || 'NOT FOUND',        !!d.deps.cli],
    ['Dependencies',    'zero (stdlib only)',              true],
  ];
  box.innerHTML = rows.map(([k, v, ok]) =>
    `<div class="row"><span>${k}</span><span class="v ${ok ? 'ok' : 'bad'}" title="${escapeHtml(String(v))}">${escapeHtml(String(v))}</span></div>`
  ).join('');
  return d;
}

// ------------------------------------------------------------------ boot
(async () => {
  const detected = await loadStatus();
  const defaultIso = detected.default_iso || (detected.isos || [])[0] || '';
  const defaultOut = detected.default_out || '';

  const cards = document.getElementById('cards');

  cards.appendChild(makeCard(1, 'list', 'Browse disc index',
    'Stream all TOC entries to the log panel. ' +
    'Chain 0 = 8,573 <code>data\\</code> files; chain 1 = 349 voice/scene/movie files. ' +
    'Listing both chains produces ~9K lines — use a specific chain to keep the output readable.',
    [
      {name: 'iso',   label: 'ISO path', value: defaultIso,
       pick: {mode: 'file', filter: 'iso'}},
      {name: 'chain', label: 'Chain', type: 'select',
       options: [['', 'Both chains'], ['0', 'Chain 0  (data\\,  8,573 files)'], ['1', 'Chain 1  (voice / scene / movies,  349 files)']]},
    ], 'List'));

  cards.appendChild(makeCard(2, 'extract', 'Extract assets',
    'Writes <code>OUTDIR/dump/chain0/</code>, <code>chain1/</code>, and <code>layer1/</code> ' +
    '(the 58 layer-1 movies carved from outside the filesystem). ' +
    'Enable <em>Extract code</em> to also pull the unstripped ELFs and IOP modules into <code>browse/code/</code>. ' +
    'ARX-compressed entries (2,095 files) are written as stored and flagged in the manifest — ' +
    '<em>Convert for browsing</em> decompresses them transparently.',
    [
      {name: 'iso',      label: 'ISO path',    value: defaultIso,
       pick: {mode: 'file', filter: 'iso'}},
      {name: 'out',      label: 'Output dir',  value: defaultOut,
       pick: {mode: 'dir'}},
      {name: 'chain',    label: 'Chain',        type: 'select',
       options: [['', 'Both chains'], ['0', 'Chain 0 only'], ['1', 'Chain 1 only']]},
      {name: 'glob',     label: 'Glob filter',  placeholder: 'e.g. movie\\* or *.evt (optional)'},
      {name: 'no_carve', label: 'Skip layer 1', type: 'checkbox',
       hint: 'skip the 58 layer-1 movie streams (faster if you only want data files)'},
      {name: 'code',     label: 'Extract code', type: 'checkbox', value: true,
       hint: 'pull SLUS_204.69, OVL overlays, IOP modules into browse/code/'},
    ], 'Extract'));

  cards.appendChild(makeCard(3, 'classes', 'Lift Java event scripts',
    'Episode I\'s cutscenes are scripted in <em>actual Java</em> (JDK 1.1) — every ' +
    '<code>.evt</code> file is a container of real class files, debug info intact. ' +
    'This reads all 480 <code>.evt</code> containers straight from the disc image (no full ' +
    'extract needed) and writes ~2,200 <code>.class</code> files in javap-ready package layout to ' +
    '<code>OUTDIR/browse/classes/</code>, plus <code>classes_manifest.csv</code>. ' +
    'Decompile with <code>javap -c -p</code> or any Java decompiler (Krakatau, CFR).',
    [
      {name: 'iso', label: 'ISO path',   value: defaultIso,
       pick: {mode: 'file', filter: 'iso'}},
      {name: 'out', label: 'Output dir', value: defaultOut, pick: {mode: 'dir'}},
    ], 'Lift classes'));

  cards.appendChild(makeCard(4, 'browse', 'Convert for browsing',
    'Turns the extracted dump into things you can open: <code>.xtx</code> textures to PNG ' +
    '(the format is a raw GS memory image — composed, unswizzled and palette-mapped in pure Python), ' +
    'streamed voice to 48&nbsp;kHz WAV, and <code>.jpg</code>/<code>.txt</code> copied across, all under ' +
    '<code>OUTDIR/browse/</code>. Movies (<code>.pss</code>/<code>.ipu</code>) convert to MP4 when ' +
    'ffmpeg is installed — without it they are skipped (raw <code>.pss</code> plays in VLC as-is). ' +
    'Run <em>Extract assets</em> first.',
    [
      {name: 'out',      label: 'Output dir',  value: defaultOut, pick: {mode: 'dir'}},
      {name: 'textures', label: 'Textures',    type: 'checkbox', value: true,
       hint: '.xtx → PNG, ARX-compressed ones included (1,269 files, ~2 min)'},
      {name: 'audio',    label: 'Voice audio', type: 'checkbox', value: true,
       hint: '.vds/.vdm → WAV mono (104 files, ~3 min)'},
      {name: 'rate',     label: 'Voice rate',  type: 'select', value: '48000',
       options: [['48000', '48000 Hz (engine default)'], ['64000', '64000 Hz'],
                 ['96000', '96000 Hz'], ['44100', '44100 Hz'],
                 ['32000', '32000 Hz'], ['24000', '24000 Hz']]},
      {name: 'banks',    label: 'Sound banks', type: 'checkbox', value: true,
       hint: '.swd music/SFX banks → per-instrument WAVs + SMD music catalogue (BGM is sequenced — see README)'},
      {name: 'images',   label: 'Images',      type: 'checkbox', value: true,
       hint: 'copy .jpg as-is'},
      {name: 'text',     label: 'Text',        type: 'checkbox', value: true,
       hint: '.txt → UTF-8 (game text is Shift-JIS with \\NN control codes)'},
      {name: 'movies',   label: 'Movies',      type: 'checkbox', value: true,
       hint: '.pss/.ipu → MP4 with sound (demuxed from private stream 1), plus separate .video.mp4 / .audio.wav copies'},
    ], 'Convert'));

  cards.appendChild(makeCard(5, 'verify', 'Verify extraction',
    'Re-checks every row in <code>manifest.csv</code>: file existence, byte size, ' +
    '<code>ARX\\0</code> magic on compressed entries, and content magic for <code>.pss</code> / <code>.jpg</code> / <code>.ipu</code>. ' +
    'Prints a summary line — zero problems means the extraction is clean.',
    [
      {name: 'out', label: 'Output dir', value: defaultOut, pick: {mode: 'dir'}},
    ], 'Verify'));

  const pinkDefault = defaultIso
    ? defaultIso.replace(/\.iso$/i, '') + ' (PINK).iso' : '';

  cards.appendChild(makeCard(6, 'pinkhair', 'Recolor KOS-MOS\'s hair',
    'The whole mod pipeline in one click: recolors her hair in every carrier on the disc ' +
    '(6 character textures, 2 battle bundles, 4 scene bundles — CLUT palettes <em>and</em> the ' +
    'true-colour strand sheets), then writes a bootable patched ISO. The retail ISO is never ' +
    'modified; the output is a full copy (~8.5 GB, a few minutes). ' +
    'Tick <em>Dry run</em> to see what would change without writing anything.',
    [
      {name: 'iso',     label: 'Retail ISO',  value: defaultIso,
       pick: {mode: 'file', filter: 'iso'}},
      {name: 'out',     label: 'Output ISO',  value: pinkDefault,
       placeholder: 'e.g. Xenosaga Episode I (PINK).iso'},
      {name: 'hue',     label: 'Hair colour', type: 'select', value: '0.92',
       options: [['0.92', 'Pink (0.92)'], ['0.85', 'Magenta (0.85)'],
                 ['0.75', 'Purple (0.75)'], ['0.33', 'Green (0.33)'],
                 ['0.13', 'Gold (0.13)'], ['0.0', 'Red (0.0)']]},
      {name: 'dry_run', label: 'Dry run',     type: 'checkbox',
       hint: 'sweep and report only — write no ISO'},
    ], 'Recolor'));

  cards.appendChild(makeCard(7, 'text-export', 'Export text for translation',
    'Pulls all 914 text objects off the disc into an editable UTF-8 tree: 588 <code>.txt</code> ' +
    '(scene scripts, U.M.N. event dialogue) and 326 <code>.uml</code> U.M.N. mails (their fixed-length ' +
    'text slot; the binary header and attached image ride along untouched). ' +
    '<code>textpack_manifest.csv</code> lists every file\'s byte budget — Shift-JIS bytes, so kana/kanji ' +
    'count double. Edit the <code>.utf8.txt</code> files in any editor, then run <em>Import translated text</em>. ' +
    'Note: dialogue spoken during cutscenes lives in the Java <code>.evt</code> scripts instead — ' +
    'see docs/MODDING.md.',
    [
      {name: 'iso', label: 'Retail ISO', value: defaultIso,
       pick: {mode: 'file', filter: 'iso'}},
      {name: 'out', label: 'Text dir',   placeholder: 'directory for the editable text tree',
       pick: {mode: 'dir'}},
    ], 'Export text'));

  cards.appendChild(makeCard(8, 'text-import', 'Import translated text',
    'Re-encodes the edited tree back to Shift-JIS, checks every file against its byte budget ' +
    '(clear per-file errors if anything is over or uses characters Shift-JIS can\'t encode — ' +
    'nothing is written until all files pass), and writes a patched ISO. ' +
    'Only files that actually changed are patched.',
    [
      {name: 'iso',  label: 'Retail ISO',       value: defaultIso,
       pick: {mode: 'file', filter: 'iso'}},
      {name: 'text', label: 'Edited text dir',  placeholder: 'the tree from Export text',
       pick: {mode: 'dir'}},
      {name: 'out',  label: 'Output ISO',       placeholder: 'path for the translated ISO copy'},
    ], 'Import text'));

  cards.appendChild(makeCard(9, 'patch', 'Patch disc objects (advanced)',
    'Replace any TOC object with a local file and write a modified ISO — the generic ' +
    'repack layer behind the recolor card. Content is given <em>uncompressed</em>; entries the ' +
    'TOC marks compressed are ARX-recompressed with a byte-perfect clone of Monolith\'s packer. ' +
    'Objects cannot outgrow their sector allocation; every write is verified by read-back. ' +
    'Format: <code>chain0:char\\pc\\kosmos.xtx=C:\\mods\\my_kosmos.xtx</code> — separate several with <code>;</code>',
    [
      {name: 'iso',  label: 'Retail ISO',   value: defaultIso,
       pick: {mode: 'file', filter: 'iso'}},
      {name: 'out',  label: 'Output ISO',   placeholder: 'path for the patched ISO copy'},
      {name: 'sets', label: 'Replacements', placeholder: 'chainN:toc\\path=localfile; ...'},
    ], 'Patch'));

  // Open the extract card by default
  cards.querySelector('[data-name="extract"]').classList.add('open');
})();

document.getElementById('btn-refresh').addEventListener('click', loadStatus);
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Xenosaga1GUI/1.0"

    def log_message(self, fmt, *args):  # noqa: N802
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
        path = url.path
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML, "text/html; charset=utf-8")
        if path == "/detect":
            return self._send_json(200, {
                "root": str(ROOT),
                "isos": detect_isos(),
                "deps": detect_deps(),
                "default_iso": default_iso(),
                "default_out": default_out_dir(),
            })
        if path == "/browse-fs":
            params = parse_qs(url.query, keep_blank_values=True)
            return self._send_json(200, list_dir(
                params.get("path", [""])[0],
                params.get("filter", [""])[0] or None,
            ))
        if path.startswith("/job/"):
            job_id = path[len("/job/"):]
            with JOBS_LOCK:
                job = JOBS.get(job_id)
            if not job:
                return self._send_json(404, {"error": "no such job"})
            return self._send_json(200, job.to_summary())
        if path.startswith("/stream/"):
            return self._serve_stream(path[len("/stream/"):])
        return self._send(404, "not found")

    def do_POST(self):  # noqa: N802
        url = urlparse(self.path)
        path = url.path
        length = int(self.headers.get("Content-Length", "0") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        form = parse_qs(body, keep_blank_values=True)
        if path.startswith("/preview/"):
            return self._preview(unquote(path[len("/preview/"):]), form)
        if path.startswith("/run/"):
            return self._run(unquote(path[len("/run/"):]), form)
        return self._send(404, "not found")

    def _preview(self, name, form):
        builder = BUILDERS.get(name)
        if not builder:
            return self._send_json(400, {"error": f"unknown command: {name}"})
        try:
            cmd = builder(form)
        except Exception as exc:
            return self._send_json(400, {"error": str(exc)})
        return self._send_json(200, {"cmd": cmd})

    def _run(self, name, form):
        builder = BUILDERS.get(name)
        if not builder:
            return self._send_json(400, {"error": f"unknown command: {name}"})
        try:
            cmd = builder(form)
        except Exception as exc:
            return self._send_json(400, {"error": str(exc)})
        try:
            job = Job(cmd, cwd=ROOT)
        except OSError as exc:
            # The CLI failed to launch at all (missing xeno-cli binary next to
            # the GUI, bad path, permissions). Surface it instead of dying
            # silently — otherwise the browser just sees a dropped request.
            shown = " ".join(shlex.quote(c) for c in cmd)
            return self._send_json(500, {"error":
                f"could not start the extractor:\n  $ {shown}\n  {type(exc).__name__}: {exc}\n\n"
                f"Expected CLI at: {CLI_TARGET}"
                + ("  (NOT FOUND — rebuild so xeno-cli sits next to the GUI)"
                   if not CLI_TARGET.exists() else "")})
        with JOBS_LOCK:
            JOBS[job.id] = job
        return self._send_json(200, {"job_id": job.id})

    def _serve_stream(self, job_id):
        with JOBS_LOCK:
            job = JOBS.get(job_id)
        if not job:
            return self._send(404, "no such job")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        q = job.subscribe()
        try:
            while True:
                try:
                    line = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                if line is None:
                    self.wfile.write(b"data: __eof__\n\n")
                    self.wfile.flush()
                    return
                safe = line.replace("\r", "")
                for chunk in safe.split("\n"):
                    self.wfile.write(b"data: " + chunk.encode("utf-8") + b"\n")
                self.wfile.write(b"\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            job.unsubscribe(q)


class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    if not CLI_TARGET.exists():
        if FROZEN:
            _fatal(
                "CLI binary missing",
                f"Expected {CLI_TARGET.name} next to gui.exe.\n\n"
                "Re-extract the release zip — running gui.exe straight from "
                "the zip preview will fail. Right-click the zip → Extract All…",
            )
        else:
            _fatal("cli.py missing", f"Expected cli.py next to gui.py at {CLI_TARGET}.")

    port = int(os.environ.get("PORT") or _free_port())
    url = f"http://localhost:{port}/"

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        _fatal(
            "Could not start local server",
            f"Failed to bind to 127.0.0.1:{port} — {exc}.",
        )

    print(f"[gui] Xenosaga Episode I Extractor")
    print(f"[gui] Open: {url}")
    print(f"[gui] Press Ctrl+C to stop.")

    if "--no-browser" not in sys.argv:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[gui] stopping")
    finally:
        server.server_close()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException as exc:
        import traceback
        _fatal(
            "Unexpected error during startup",
            f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}",
        )
