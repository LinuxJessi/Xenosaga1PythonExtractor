"""subs.py — burn translated subtitles into a cutscene movie, keeping the
original (English/Japanese) audio untouched, and splice the result back into
the original ``.pss`` container in place of just the video elementary stream.

Two different addressing schemes on this disc need two different repack
steps downstream of this module:

* the 45 TOC-indexed ``movie\\mpeg2\\*.pss`` objects — plain, uncompressed
  TOC entries, so the existing generic ``repack.patch_iso``/``cli.py patch``
  already handles them, unmodified. Nothing new needed there.
* the 58 layer-1 movies (raw sector, outside any TOC/filesystem, recovered
  by ``carve.py``) — nothing in the kit could write back to these before;
  ``patch_layer1`` here does that, keyed to ``carve.scan_layer1``'s index.

Constraint identical to every other repack path in this kit: the patched
movie must be **no larger than** the original object's byte allocation
(the TOC gap-to-next-entry for the 45, or the carved gap-to-next-stream for
the 58 — ``CarvedStream.size`` is defined as exactly that gap, so there is
real headroom the same way texture/text objects have; growth just isn't
possible without a full-disc sector relocation, which this module does not
attempt). Burning subtitles onto existing frames does not inherently need
more bits than the original encode, so hitting that ceiling is a matter of
choosing an encode bitrate (``fit_to_budget`` does this by iterative
search), not a hard blocker.

What this module verifies mechanically: the container re-parses with the
same packet grammar as the original, packet framing is spec-valid MPEG-PS,
and the result fits the size budget. What it does **not** verify — there is
no way to, without a PS2 or PCSX2 session to boot the result — is whether
the PS2's IPU hardware decoder accepts the re-encoded GOP/quantization
choices, or whether audio and video stay in sync over a multi-minute
cutscene once the video track has been fully replaced. Test in PCSX2 before
trusting a batch of these. See docs/SUBTITLES.md.
"""
from __future__ import annotations

import json
import mmap
import os
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from carve import CarvedStream, scan_layer1
from iso9660 import IsoImage

PACK, SYSTEM, PADDING, VIDEO, AUDIO, END = 0xBA, 0xBB, 0xBE, 0xE0, 0xBD, 0xB9
_MAX_PES_PAYLOAD = 65000  # PES_packet_length is u16; stay well clear of 65535


# ---------------------------------------------------------------------------
# MPEG-PS packet parsing (same start-code walk as browse.extract_pss_audio)
# ---------------------------------------------------------------------------

def iter_packets(data: bytes):
    """Walk one .pss as an ordered list of MPEG-PS packets.

    Each yielded dict has ``kind`` (pack/system/video/audio/padding/end/
    stream_0xNN), ``start``/``end`` (byte offsets into ``data``), and
    ``raw`` (the verbatim packet bytes). ``video`` entries additionally
    carry ``payload_start``/``payload_end`` bounding the elementary-stream
    bytes inside the packet (past the PES header).
    """
    i, n = 0, len(data)
    while i + 4 <= n:
        if data[i:i + 3] != b"\x00\x00\x01":
            raise ValueError(f"lost sync at offset {i} (expected a start code)")
        sid = data[i + 3]
        if sid == END:
            # 0xB9 (MPEG program_end_code) is the last real packet on this
            # disc — carved/allocated regions run past it filled with the
            # same "MONOLITHSOFT Xenosaga Episode.1" filler text the TOC's
            # unused tail uses, not more packets. Stop parsing there and
            # keep the filler as one verbatim trailing chunk.
            end = min(i + 4, n)
            yield {"kind": "end", "start": i, "end": end, "raw": data[i:end]}
            if end < n:
                yield {"kind": "tail", "start": end, "end": n,
                       "raw": data[end:n]}
            return
        if sid == PACK:
            end = i + 14  # fixed-size on this disc: zero stuffing bytes
            yield {"kind": "pack", "start": i, "end": end, "raw": data[i:end]}
            i = end
            continue
        (ln,) = struct.unpack_from(">H", data, i + 4)
        end = i + 6 + ln
        entry = {"start": i, "end": end, "raw": data[i:end]}
        if sid == SYSTEM:
            entry["kind"] = "system"
        elif sid == PADDING:
            entry["kind"] = "padding"
        elif sid == VIDEO:
            entry["kind"] = "video"
            hdr_len = data[i + 8]
            entry["payload_start"] = i + 9 + hdr_len
            entry["payload_end"] = end
        elif sid == AUDIO:
            entry["kind"] = "audio"
        else:
            entry["kind"] = f"stream_{sid:#04x}"
        yield entry
        i = end


def video_track_bytes(packets) -> bytes:
    """Concatenate every video packet's ES payload, in stream order — the
    same bytes a standards-compliant MPEG-2 demuxer (ffmpeg included) hands
    its decoder."""
    return b"".join(p["raw"][p["payload_start"] - p["start"]:
                             p["payload_end"] - p["start"]]
                    for p in packets if p["kind"] == "video")


# ---------------------------------------------------------------------------
# ffmpeg/ffprobe plumbing
# ---------------------------------------------------------------------------

def detect_ffprobe(ffmpeg_path: Optional[str] = None) -> Optional[str]:
    """ffprobe normally lives next to ffmpeg; reuse browse.detect_ffmpeg's
    probe order and swap the binary name."""
    import browse
    ffmpeg_path = ffmpeg_path or browse.detect_ffmpeg()
    if ffmpeg_path:
        name = "ffprobe.exe" if os.name == "nt" else "ffprobe"
        cand = Path(ffmpeg_path).with_name(name)
        if cand.is_file():
            return str(cand)
    import shutil
    return shutil.which("ffprobe")


def has_subtitles_filter(ffmpeg: str = "ffmpeg") -> bool:
    """The ``subtitles`` filter needs libass compiled into ffmpeg — not
    every build has it (confirmed: a stock ``brew install ffmpeg`` on some
    configurations does not). Check rather than assume; fail loudly if
    missing instead of silently producing un-subtitled video."""
    out = subprocess.run([ffmpeg, "-hide_banner", "-filters"],
                         capture_output=True, text=True).stdout
    return any(line.split()[1] == "subtitles"
               for line in out.splitlines() if len(line.split()) > 1
               and not line.startswith("Filters:"))


def probe(path, ffprobe: str = "ffprobe") -> dict:
    v = json.loads(subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout)["streams"][0]
    fmt = json.loads(subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True).stdout)["format"]
    frames = video_track_bytes(list(iter_packets(Path(path).read_bytes()))).count(b"\x00\x00\x01\x00")
    return {"width": v["width"], "height": v["height"], "fps": v["r_frame_rate"],
            "duration": float(fmt["duration"]), "frames": frames}


# ---------------------------------------------------------------------------
# SRT scaffolding
# ---------------------------------------------------------------------------

def _srt_timestamp(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = round((s - int(s)) * 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


def write_srt_detected(src_pss, out_srt, ffmpeg: str = "ffmpeg", **kw) -> int:
    """SRT skeleton timed from the burned-in subtitle: one empty cue per
    interval the original text is on screen. Fill in the lines."""
    segs = detect_burned_in(src_pss, ffmpeg=ffmpeg, **kw)
    lines = []
    for i, (a, b) in enumerate(segs, 1):
        lines += [str(i), f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}",
                  f"[line {i}: what is said from {a:.2f}s to {b:.2f}s]", ""]
    Path(out_srt).write_text("\n".join(lines), encoding="utf-8")
    return len(segs)


def write_srt_template(src_path, out_srt, cue_seconds: float = 5.0,
                       ffprobe: str = "ffprobe") -> int:
    """A minimal, uniformly-timed blank skeleton — a starting point, not a
    real auto-timer. Real cue timing needs a human watching/listening to
    the extracted MP4 (Aegisub / Subtitle Edit, any SRT-capable editor);
    retime cues there before translating, don't trust the uniform spacing
    this writes."""
    duration = probe(src_path, ffprobe=ffprobe)["duration"]
    lines, n, t = [], 1, 0.0
    while t < duration:
        end = min(t + cue_seconds, duration)
        lines += [str(n), f"{_srt_timestamp(t)} --> {_srt_timestamp(end)}",
                  "(translate this line)", ""]
        t, n = end, n + 1
    Path(out_srt).write_text("\n".join(lines), encoding="utf-8")
    return n - 1


# ---------------------------------------------------------------------------
# Subtitles without libass: cues rendered by the kit, composited by ffmpeg
# ---------------------------------------------------------------------------
# The game's movies carry their English burned into the picture, so a new
# language has to cover the old line and draw its own. XS1KOR (the Korean
# localisation) did this with an ASS overlay at the game's 512x448: fixed
# subtitle rows, outline + shadow + a dark "spread" behind the letters to
# bury the original, rendered through ffmpeg/libass, then re-encoded as
# MPEG-2 within the original's size and muxed over the untouched audio.
# The same plan is followed here, but the text is rasterised by the kit
# itself (HarfBuzz shaping + the outline filler from glyphs.py, so Arabic,
# Devanagari, Hebrew… shape correctly) into one PNG per cue, and ffmpeg's
# plain `overlay`/`boxblur` filters do the compositing. Those filters are in
# every ffmpeg build; `subtitles`/`drawtext` are not. A directory of
# hand-made PNGs (any image editor) works in place of the font.

def _srt_time(s: str) -> float:
    s = s.strip().replace(",", ".")
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def parse_srt(path) -> list:
    """-> [(start_s, end_s, text)] with text lines joined by '\n'."""
    cues = []
    block: list = []
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines() + [""]:
        line = raw.rstrip("\r")
        if line.strip():
            block.append(line)
            continue
        if len(block) >= 2 and "-->" in block[1 if block[0].strip().isdigit() else 0]:
            i = 1 if block[0].strip().isdigit() else 0
            a, b = block[i].split("-->")
            text = "\n".join(block[i + 1:]).strip()
            if text:
                cues.append((_srt_time(a), _srt_time(b.split()[0]), text))
        block = []
    return cues


def _png_rgba(width: int, height: int, rows: list) -> bytes:
    import zlib
    from font import _png_chunk
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    return (b"\x89PNG\r\n\x1a\n"
            + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + _png_chunk(b"IDAT", zlib.compress(raw, 9))
            + _png_chunk(b"IEND", b""))


class CueRenderer:
    """Draw one subtitle cue (one or two lines) as a white, black-outlined
    RGBA PNG the width of the frame, shaped with HarfBuzz."""

    def __init__(self, font, px: float = 22.0, face_index: int = 0,
                 script: Optional[str] = None, language: Optional[str] = None,
                 rtl: bool = False, width: int = 512, outline: int = 2,
                 shadow: int = 1, supersample: int = 3):
        from glyphs import TTFRenderer
        self.r = TTFRenderer(font, px=px, face_index=face_index,
                             features=None, supersample=supersample)
        # subtitles want the font's own ligatures and kerning back
        self.r.features = {}
        self.script, self.language, self.rtl = script, language, rtl
        self.width, self.px = width, px
        self.outline, self.shadow, self.ss = outline, shadow, supersample
        self.line_h = int(round(px * 1.35))
        asc = self.r.tt["hhea"].ascent * self.r.scale
        self.ascent = int(round(min(asc, px * 1.1)))

    def _coverage(self, text: str, W: int, H: int, baseline: int) -> list:
        """Even-odd scanline fill of a shaped line, centred, 0..255 per pixel."""
        ss = self.ss
        r = self.r
        glyphs = r.shape(text, "rtl" if self.rtl else "ltr", self.script, self.language)
        total = sum(g[2] for g in glyphs) * r.scale
        pen = max((W - total) / 2.0, 2.0)
        edges = []
        for gid, _, adv, xo, yo in glyphs:
            for contour in r.contours(gid):
                pts = [((pen + (xo + x) * r.scale) * ss,
                        (baseline - (yo + y) * r.scale) * ss) for x, y in contour]
                for i in range(len(pts)):
                    x0, y0 = pts[i]
                    x1, y1 = pts[(i + 1) % len(pts)]
                    if y0 != y1:
                        edges.append((x0, y0, x1, y1))
            pen += adv * r.scale
        cov = [[0] * W for _ in range(H)]
        if not edges:
            return cov
        ys = sorted(set(range(int(max(0, min(min(e[1], e[3]) for e in edges))),
                             int(min(H * ss, max(max(e[1], e[3]) for e in edges)) + 1))))
        for sy in ys:
            yc = sy + 0.5
            xs = []
            for x0, y0, x1, y1 in edges:
                if (y0 <= yc < y1) or (y1 <= yc < y0):
                    xs.append(x0 + (yc - y0) * (x1 - x0) / (y1 - y0))
            xs.sort()
            row = cov[sy // ss]
            for i in range(0, len(xs) - 1, 2):
                a, b = int(xs[i] + 0.5), int(xs[i + 1] + 0.5)
                for sx in range(max(a, 0), min(b, W * ss)):
                    row[sx // ss] += 1
        n = ss * ss
        return [[min(255, c * 255 // n) for c in row] for row in cov]

    def render(self, text: str) -> tuple:
        """-> (png_bytes, width, height) for a cue of 1..n lines."""
        lines = [ln for ln in text.split("\n")] or [""]
        W = self.width
        pad = self.outline + self.shadow + 2
        H = self.line_h * len(lines) + 2 * pad
        text_cov = [[0] * W for _ in range(H)]
        for i, ln in enumerate(lines):
            if not ln.strip():
                continue
            base = pad + i * self.line_h + self.ascent
            cov = self._coverage(ln, W, H, base)
            for y in range(H):
                tr, cr = text_cov[y], cov[y]
                for x in range(W):
                    if cr[x]:
                        tr[x] = max(tr[x], cr[x])
        # outline: max over a square neighbourhood; shadow: offset copy
        o = self.outline
        edge = [[0] * W for _ in range(H)]
        for y in range(H):
            for x in range(W):
                m = 0
                for dy in range(-o, o + 1):
                    yy = y + dy
                    if 0 <= yy < H:
                        row = text_cov[yy]
                        for dx in range(-o, o + 1):
                            xx = x + dx
                            if 0 <= xx < W and row[xx] > m:
                                m = row[xx]
                edge[y][x] = m
        sh = self.shadow
        rows = []
        for y in range(H):
            row = bytearray()
            for x in range(W):
                t = text_cov[y][x]
                e = edge[y][x]
                s = edge[y - sh][x - sh] if sh and y >= sh and x >= sh else 0
                a = max(e, s * 3 // 4)
                if t:
                    # white letter over the black edge
                    row += bytes((255, 255, 255, max(t, a)))
                    continue
                row += bytes((0, 0, 0, a))
            rows.append(row)
        return _png_rgba(W, H, rows), W, H


def render_cues(cues, renderer: CueRenderer, out_dir) -> list:
    """-> [(png_path, start, end)]; one PNG per cue, named by index."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for i, (a, b, text) in enumerate(cues, 1):
        png, _, _ = renderer.render(text)
        p = out_dir / f"cue{i:04d}.png"
        p.write_bytes(png)
        out.append((p, a, b))
    return out


def load_cue_dir(cues, cue_dir) -> list:
    """Hand-made PNGs: ``cue0001.png`` … matched to the SRT cues in order."""
    cue_dir = Path(cue_dir)
    out = []
    for i, (a, b, _) in enumerate(cues, 1):
        p = cue_dir / f"cue{i:04d}.png"
        if not p.exists():
            raise FileNotFoundError(f"{p} (one PNG per SRT cue, cue0001.png …)")
        out.append((p, a, b))
    return out


def _enable(cues) -> str:
    return "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in cues) or "0"


def encode_es_overlay(src_pss, cue_pngs, out_es, ffmpeg: str = "ffmpeg",
                      ffprobe: str = "ffprobe", bitrate_kbps: int = 1500,
                      gop: int = 15, bframes: int = 2,
                      mask: Optional[tuple] = (0, 330, 512, 118),
                      bottom_margin: int = 28, band: str = "black",
                      band_pad: float = 0.25) -> dict:
    """Like ``encode_es`` but composites the cue PNGs with ``overlay`` and,
    while a cue is up, covers the band that holds the original subtitle
    (``mask`` = x, y, w, h; None to leave the picture alone): ``band`` is
    ``"black"`` for a solid bar (the default: it hides the English
    completely, costs the encoder nothing and leaves the rest of the
    picture its bits) or ``"blur"`` for a box blur of the band. The cover
    opens ``band_pad`` seconds before each cue and closes that long after
    it: SRT times are rarely closer than a few frames to the original
    line's own fade-in and fade-out, and a bar that is a few frames
    early hides nothing worth seeing, while one that is late shows the
    English."""
    info = probe(src_pss, ffprobe=ffprobe)
    fps, dur = info["fps"], info["duration"]
    W, H = info["width"], info["height"]
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src_pss)]
    for p, _, _ in cue_pngs:
        cmd += ["-loop", "1", "-framerate", fps, "-t", f"{dur:.3f}", "-i", str(p)]
    parts = []
    cur = "[0:v]"
    if mask and cue_pngs and band != "none":
        x, y, w, h = mask
        on = _enable([(max(a - band_pad, 0.0), b + band_pad) for _, a, b in cue_pngs])
        if band == "blur":
            parts.append(f"{cur}split[base][forblur]")
            parts.append(f"[forblur]crop={w}:{h}:{x}:{y},boxblur=luma_radius=10:luma_power=2:"
                         f"chroma_radius=5:chroma_power=2[blur]")
            parts.append(f"[base][blur]overlay={x}:{y}:enable='{on}'[v0]")
        elif band == "black":
            parts.append(f"{cur}drawbox=x={x}:y={y}:w={w}:h={h}:color=black:t=fill:enable='{on}'[v0]")
        else:
            raise ValueError(f"band must be 'black', 'blur' or 'none', not {band!r}")
        cur = "[v0]"
    for i, (_, a, b) in enumerate(cue_pngs, 1):
        nxt = f"[v{i}]"
        parts.append(f"{cur}[{i}:v]overlay=(W-w)/2:H-h-{bottom_margin}:"
                     f"enable='between(t,{a:.3f},{b:.3f})'{nxt}")
        cur = nxt
    parts.append(f"{cur}format=yuv420p[out]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[out]", "-an",
            "-c:v", "mpeg2video", "-profile:v", "main", "-level:v", "main",
            "-r", fps, "-s", f"{W}x{H}",
            *_quality_args(bitrate_kbps, gop, bframes),
            "-frames:v", str(info["frames"]),
            "-f", "mpeg2video", str(out_es)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"ffmpeg failed:\n{r.stderr[-2000:]}")
    return info


def detect_burned_in(src_pss, ffmpeg: str = "ffmpeg", band: tuple = (0, 330, 512, 118),
                     step: float = 0.25, min_pixels: int = 120,
                     min_len: float = 0.6, gap: float = 0.3) -> list:
    """Find when the original burned-in subtitle is on screen: sample the
    band every ``step`` seconds as grey PNGs and count near-white pixels.
    -> [(start, end)] merged over gaps shorter than ``gap``."""
    from font import png_to_gray
    x, y, w, h = band
    with tempfile.TemporaryDirectory() as td:
        pattern = str(Path(td) / "%05d.png")
        cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src_pss),
               "-vf", f"fps={1 / step},crop={w}:{h}:{x}:{y},format=gray", pattern]
        subprocess.run(cmd, check=True, capture_output=True)
        frames = sorted(Path(td).glob("*.png"))
        counts = []
        for f in frames:
            fw, fh, g = png_to_gray(f.read_bytes())
            # subtitle letters are white with a dark outline: count bright
            # pixels that have a dark pixel within two steps, which a bright
            # floor or sky does not
            n = 0
            for yy in range(2, fh - 2):
                row = yy * fw
                for xx in range(2, fw - 2):
                    if g[row + xx] > 235 and (
                            g[row + xx - 2] < 70 or g[row + xx + 2] < 70
                            or g[row - 2 * fw + xx] < 70 or g[row + 2 * fw + xx] < 70):
                        n += 1
            counts.append(n)
    segs = []
    on = None
    for i, c in enumerate(counts + [0]):
        t = i * step
        if c >= min_pixels and on is None:
            on = t
        elif c < min_pixels and on is not None:
            segs.append([on, t])
            on = None
    merged: list = []
    for a, b in segs:
        if merged and a - merged[-1][1] < gap:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged if b - a >= min_len]


# ---------------------------------------------------------------------------
# Encode: burn subtitles onto the source video, matching its own container
# parameters, as a raw MPEG-2 elementary stream (no PS container yet)
# ---------------------------------------------------------------------------

# MPEG-2 encoder settings shared by both paths. The retail streams run about
# 2.7 Mbit/s with pictures up to 80 KB; a VBV of the Main Level maximum lets
# the encoder spend that on an I frame, rate-distortion macroblock decisions
# and trellis quantisation buy about a decibel at the same rate. A tight
# VBV (an early version used 0.3 x bitrate) starved scene changes into
# macroblocks; ffmpeg's two-pass mode refuses constant-rate streams, so
# this stays single-pass.
def _quality_args(bitrate_kbps: int, gop: int, bframes: int) -> list:
    return ["-g", str(gop), "-bf", str(bframes), "-flags", "+cgop",
            "-sc_threshold", "1000000000",
            "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
            "-minrate", f"{bitrate_kbps}k", "-bufsize", "1835k",
            "-mbd", "rd", "-trellis", "2", "-dc", "10", "-cmp", "2", "-subcmp", "2"]


def encode_es(src_pss, srt_path, out_es, ffmpeg: str = "ffmpeg",
             ffprobe: str = "ffprobe", bitrate_kbps: int = 1500,
             gop: int = 15, bframes: int = 2) -> dict:
    if not has_subtitles_filter(ffmpeg):
        raise RuntimeError(
            f"{ffmpeg} has no 'subtitles' filter (needs libass compiled in) "
            "— check with `ffmpeg -filters | grep subtitle`; install a full "
            "ffmpeg build (most package-manager formulas include libass) "
            "rather than a minimal/custom one")
    info = probe(src_pss, ffprobe=ffprobe)
    srt_escaped = str(Path(srt_path).resolve()).replace("\\", "/").replace(":", "\\:")
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src_pss), "-an",
           "-vf", f"subtitles='{srt_escaped}'",
           "-c:v", "mpeg2video", "-profile:v", "main", "-level:v", "main",
           "-r", info["fps"], "-s", f"{info['width']}x{info['height']}",
           "-pix_fmt", "yuv420p",
           *_quality_args(bitrate_kbps, gop, bframes),
           "-f", "mpeg2video", str(out_es)]
    subprocess.run(cmd, check=True, capture_output=True)
    return info


def fit_to_budget(src_pss, srt_path, out_es, max_bytes: int,
                  ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
                  attempts: int = 6, encoder=None) -> dict:
    """Encode repeatedly, backing off the bitrate, until the raw ES fits
    under ``max_bytes``. The ES will grow further once re-packetized into
    PES (a few bytes of header per ~64 KB), so this targets a safety margin
    rather than the exact ceiling."""
    duration = probe(src_pss, ffprobe=ffprobe)["duration"]
    bitrate = max(int(max_bytes * 8 / duration / 1000 * 0.95), 200)
    size = None
    for attempt in range(1, attempts + 1):
        if encoder is not None:
            encoder(bitrate)
        else:
            encode_es(src_pss, srt_path, out_es, ffmpeg=ffmpeg, ffprobe=ffprobe,
                      bitrate_kbps=bitrate)
        size = Path(out_es).stat().st_size
        if size <= max_bytes:
            return {"bitrate_kbps": bitrate, "size": size, "attempts": attempt}
        bitrate = max(int(bitrate * max_bytes / size * 0.9), 100)
    raise RuntimeError(
        f"could not fit {out_es} under {max_bytes} bytes in {attempts} "
        f"attempts (last try: {size} bytes at {bitrate} kbps)")


# ---------------------------------------------------------------------------
# Splice: swap only the video track, keep every other packet byte-identical
# ---------------------------------------------------------------------------

def _pes_times(raw: bytes):
    """(pts, dts) of a PES packet, either may be None."""
    flags2, hlen = raw[7], raw[8]
    def ts(b):
        return ((b[0] >> 1) & 7) << 30 | b[1] << 22 | (b[2] >> 1) << 15 | b[3] << 7 | b[4] >> 1
    pts = ts(raw[9:14]) if flags2 & 0x80 else None
    dts = ts(raw[14:19]) if flags2 & 0x40 else None
    return pts, dts


def _ts_bytes(prefix: int, t: int) -> bytes:
    return bytes(((prefix << 4) | ((t >> 29) & 0x0E) | 1,
                  (t >> 22) & 0xFF, ((t >> 14) & 0xFE) | 1,
                  (t >> 7) & 0xFF, ((t << 1) & 0xFE) | 1))


def _pes(payload: bytes, pts=None, dts=None) -> bytes:
    if pts is None:
        hdr = b"\x80\x00\x00"
    elif dts is None or dts == pts:
        hdr = b"\x80\x80\x05" + _ts_bytes(2, pts)
    else:
        hdr = b"\x80\xC0\x0A" + _ts_bytes(3, pts) + _ts_bytes(1, dts)
    return b"\x00\x00\x01\xe0" + struct.pack(">H", len(hdr) + len(payload)) + hdr + payload


def picture_units(es: bytes) -> list:
    """Split an MPEG-2 ES into (offset, temporal_ref, gop_start) per picture;
    each unit starts at the sequence/GOP header that precedes its picture,
    if any, so a PES packet can begin there."""
    starts = []
    pending = None
    i = 0
    while True:
        j = es.find(b"\x00\x00\x01", i)
        if j < 0 or j + 5 >= len(es):
            break
        code = es[j + 3]
        if code in (0xB3, 0xB8):                 # sequence / GOP header
            if pending is None:
                pending = j
        if code == 0x00:                         # picture start
            tref = (es[j + 4] << 2) | (es[j + 5] >> 6)
            starts.append([pending if pending is not None else j, tref, False])
            pending = None
        i = j + 3
    # mark GOP starts: a unit whose span holds a GOP or sequence header
    for k, u in enumerate(starts):
        nxt = starts[k + 1][0] if k + 1 < len(starts) else len(es)
        head = es[u[0]:min(nxt, u[0] + 64)]
        u[2] = b"\x00\x00\x01\xb8" in head or b"\x00\x00\x01\xb3" in head
    return [tuple(u) for u in starts]


def picture_times(units, first_pts: int, first_dts: int, frame_ticks: int) -> list:
    """PTS/DTS per picture in decode order: DTS counts up one frame per
    picture; PTS = the GOP's display base + temporal reference. Open GOPs
    (an I frame with leading B frames) are handled by the temporal
    reference alone, as the standard defines it."""
    out = []
    gop_base = first_pts
    gop_len = 0
    for n, (_, tref, gop_start) in enumerate(units):
        if gop_start and n:
            gop_base += gop_len * frame_ticks
            gop_len = 0
        dts = first_dts + n * frame_ticks
        pts = gop_base + tref * frame_ticks
        out.append((pts, dts))
        gop_len += 1
    return out


MAX_VIDEO_LEAD = 0.35     # seconds a picture may be delivered ahead of its decode time
SCR_DELAY = 4500          # 90 kHz ticks between a pack's clock and the earliest unit in it


def pack_scr(raw: bytes) -> int:
    """The 90 kHz system clock reference of an MPEG-2 pack header."""
    b = raw[4:10]
    return (((b[0] >> 3) & 7) << 30 | (b[0] & 3) << 28 | b[1] << 20
            | (b[2] >> 3) << 15 | (b[2] & 3) << 13 | b[3] << 5 | b[4] >> 3)


def _pack_groups(packets):
    """Group packets by pack: [(pack_packet, [members…]), …]."""
    groups = []
    for p in packets:
        if p["kind"] == "pack":
            groups.append((p, []))
        elif groups:
            groups[-1][1].append(p)
    return groups


def video_capacity(orig_path) -> dict:
    """How many elementary-stream bytes the original container's video
    slots can hold once every picture carries a PTS/DTS stamp."""
    orig = Path(orig_path).read_bytes()
    packets = list(iter_packets(orig))
    video = [p for p in packets if p["kind"] == "video"]
    pictures = video_track_bytes(packets).count(b"\x00\x00\x01\x00")
    slots = sum(p["end"] - p["start"] - 9 for p in video)
    return {"slots": len(video), "pictures": pictures,
            "es_bytes": slots - 10 * pictures - 16}


def _scr_bytes(template: bytes, scr: int) -> bytes:
    """A 14-byte MPEG-2 pack header with ``scr`` (90 kHz base, extension 0)
    and the template's mux rate / stuffing bytes."""
    b4 = 0x40 | ((scr >> 30) & 7) << 3 | 0x04 | ((scr >> 28) & 3)
    b5 = (scr >> 20) & 0xFF
    b6 = ((scr >> 15) & 0x1F) << 3 | 0x04 | ((scr >> 13) & 3)
    b7 = (scr >> 5) & 0xFF
    b8 = (scr & 0x1F) << 3 | 0x04
    b9 = 0x01
    return template[:4] + bytes((b4, b5, b6, b7, b8, b9)) + template[10:14]


def splice(orig_path, new_es_path, out_path) -> dict:
    """Mux ``new_es_path`` with the original's audio into a container shaped
    like the original: 16,384-byte packs of 4,096-byte PES packets.

    The console's player reads packs by the sector and schedules by the
    stamps, so the shape has to be exact and the timing has to be the
    retail muxer's: every audio packet is the original's bytes, placed
    when its PTS comes within 0.2 s of the pack clock; video is placed
    when the picture under the cursor is within 0.3 s of its decode time
    (the retail stream never delivers a picture earlier than that, nor
    late), each PES packet carrying the PTS/DTS of the first picture that
    starts in it; a slot nothing is due for holds padding; each pack's
    clock is written as the earliest stamp it carries minus one frame,
    never decreasing, as measured on the retail stream. The system
    header is copied; the stream ends with sequence_end and the program
    end code. Pictures still delivered late (the encode burst past what
    the slots can carry) are counted in the report; ``burn`` re-encodes
    lower until there are none.
    """
    orig = Path(orig_path).read_bytes()
    new_es = Path(new_es_path).read_bytes().rstrip(b"\x00")
    if not new_es.endswith(b"\x00\x00\x01\xb7"):
        new_es += b"\x00\x00\x01\xb7"
    packets = list(iter_packets(orig))
    video = [p for p in packets if p["kind"] == "video"]
    audio = [p for p in packets if p["kind"] == "audio"]
    packs = [p for p in packets if p["kind"] == "pack"]
    system = [p for p in packets if p["kind"] == "system"]
    if not video or not packs:
        raise ValueError(f"{orig_path}: no video packets found")
    pack_size = packs[1]["start"] - packs[0]["start"] if len(packs) > 1 else 16384
    pack_hdr = packs[0]["raw"][:14]
    sys_hdr = system[0]["raw"] if system else b""
    slot = 4096

    orig_frames = video_track_bytes(packets).count(b"\x00\x00\x01\x00")
    units = picture_units(new_es)
    if orig_frames != len(units):
        raise ValueError(
            f"{orig_path}: frame count changed ({orig_frames} -> "
            f"{len(units)}) — re-encode at the exact source fps/duration "
            "(check encode_es's -r matches probe())")
    stamped = [t for t in (_pes_times(p["raw"]) for p in video) if t[0] is not None]
    first_pts = stamped[0][0]
    first_dts = stamped[0][1] if stamped[0][1] is not None else first_pts
    dtss = [d for _, d in stamped if d is not None]
    frame_ticks = 3003
    if len(dtss) >= 2:
        deltas = sorted(b - a for a, b in zip(dtss, dtss[1:]) if b > a)
        if deltas:
            med = deltas[len(deltas) // 2]
            frame_ticks = med // max(1, round(med / 3003))
    times = picture_times(units, first_pts, first_dts, frame_ticks)
    unit_offsets = [u[0] for u in units]
    unit_at = {u[0]: t for u, t in zip(units, times)}

    import bisect
    first_scr, last_scr = pack_scr(packs[0]["raw"]), pack_scr(packs[-1]["raw"])
    byte_rate = len(orig) * 90000 / max(1, last_scr - first_scr)   # bytes per 90 kHz tick
    audio_q = [(_pes_times(p["raw"])[0], p["raw"]) for p in audio]
    a_lead, v_lead = int(0.2 * 90000), int(MAX_VIDEO_LEAD * 90000)
    max_packs = len(packs) + (len(orig) - packs[-1]["start"] - pack_size) // pack_size

    out = bytearray()
    cursor, ai, prev_scr = 0, 0, first_scr
    late = stamped_pictures = padding_slots = 0
    worst_late = 0
    k = 0
    # the clock is the last pack's SCR: time advances with the stamps the
    # packs carry (the retail stream steps 0, 1 or 2 frames per pack), and
    # by one frame when a pack has nothing due and holds only padding
    clock = first_scr
    while cursor < len(new_es) or ai < len(audio_q):
        if k >= max_packs:
            raise ValueError(
                f"{orig_path}: the new video does not fit the container at this pace "
                f"({len(new_es) - cursor} bytes and {len(audio_q) - ai} audio packets "
                "left) — encode at a lower bitrate")
        body = bytearray(sys_hdr if k == 0 else b"")
        stamps = []
        has_video = False
        room = pack_size - 14 - len(body)
        while room > 0:
            size = min(slot, room)
            # interleave by time: whichever stream's next unit is due first
            # goes in the slot (audio: its PTS; video: the decode time of
            # the picture under the cursor), the way a muxer orders access
            # units. Audio packets are 4,096 bytes and skip a shorter slot.
            vid_due = times[bisect.bisect_right(unit_offsets, cursor) - 1][1] if cursor < len(new_es) else None
            aud_due = audio_q[ai][0] if ai < len(audio_q) else None
            take_audio = (aud_due is not None and len(audio_q[ai][1]) <= size
                          and (vid_due is None or aud_due <= vid_due))
            if take_audio:
                body += audio_q[ai][1]
                stamps.append(audio_q[ai][0])
                ai += 1
                room -= len(audio_q[ai - 1][1])
                continue
            if cursor < len(new_es):
                hdr = 9
                pts = dts = None
                for _ in range(3):
                    span_end = cursor + (size - hdr)
                    j = bisect.bisect_left(unit_offsets, cursor)
                    if j < len(unit_offsets) and unit_offsets[j] < span_end:
                        pts, dts = unit_at[unit_offsets[j]]
                        want = 14 if dts == pts else 19
                    else:
                        pts = dts = None
                        want = 9
                    if want == hdr:
                        break
                    hdr = want
                j = bisect.bisect_left(unit_offsets, cursor)
                if not (j < len(unit_offsets) and unit_offsets[j] < cursor + (size - hdr)):
                    pts = dts = None
                    hdr = 9
                payload = new_es[cursor:cursor + (size - hdr)]
                if len(payload) < size - hdr:
                    tail = size - hdr - len(payload)
                    if tail >= 6:
                        body += _pes(payload, pts, dts)
                        body += b"\x00\x00\x01\xbe" + struct.pack(">H", tail - 6) + b"\xff" * (tail - 6)
                    else:
                        body += _pes_stuffed(payload, pts, dts, tail)
                else:
                    body += _pes(payload, pts, dts)
                if pts is not None:
                    stamped_pictures += 1
                    stamps.append(dts if dts is not None else pts)
                has_video = True
                cursor += len(payload)
                room -= size
                continue
            body += b"\x00\x00\x01\xbe" + struct.pack(">H", size - 6) + b"\xff" * (size - 6)
            padding_slots += 1
            room -= size
        if stamps:
            # the pack clock sits ~0.05 s before the earliest unit it carries
            # (retail: 0.02-0.09 s); a pack whose content was due before the
            # previous pack's clock is late, which the caller treats as a
            # bitrate problem
            scr = max(min(stamps) - SCR_DELAY, prev_scr)
            if min(stamps) < prev_scr:
                late += 1
                worst_late = max(worst_late, prev_scr - min(stamps))
        elif has_video:
            scr = prev_scr                      # continuation of a picture: no time passes
        else:
            scr = prev_scr + frame_ticks        # nothing due: let a frame of time pass
        out += _scr_bytes(pack_hdr, scr) + body
        prev_scr = clock = scr
        k += 1
    out += b"\x00\x00\x01\xb9"
    Path(out_path).write_bytes(out)
    return {"orig_size": len(orig), "new_size": len(out), "packs": k,
            "video_packets": len(video), "new_es_bytes": len(new_es),
            "pictures": len(units), "stamped": stamped_pictures,
            "padding_slots": padding_slots, "late": late,
            "worst_late_s": round(worst_late / 90000, 3), "frame_ticks": frame_ticks}


def _pes_stuffed(payload: bytes, pts, dts, extra: int) -> bytes:
    """A video PES packet with ``extra`` 0xFF stuffing bytes in its header."""
    if pts is None:
        hdr = bytes((0x80, 0x00, extra)) + b"\xff" * extra
    elif dts is None or dts == pts:
        hdr = bytes((0x80, 0x80, 5 + extra)) + _ts_bytes(2, pts) + b"\xff" * extra
    else:
        hdr = bytes((0x80, 0xC0, 10 + extra)) + _ts_bytes(3, pts) + _ts_bytes(1, dts) + b"\xff" * extra
    return b"\x00\x00\x01\xe0" + struct.pack(">H", len(hdr) + len(payload)) + hdr + payload


def burn(src_pss, srt_path, out_path, ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe", max_bytes: Optional[int] = None,
        renderer: Optional[CueRenderer] = None, cue_dir=None,
        mask: Optional[tuple] = (0, 330, 512, 118), bottom_margin: int = 28,
        keep_cues=None, band: str = "black", band_pad: float = 0.25) -> dict:
    """End to end: encode the subtitled video within budget, splice it back
    over the original's video track, original audio untouched throughout.

    With ``renderer`` (a font) or ``cue_dir`` (hand-made PNGs) the cues are
    composited by ffmpeg's ``overlay`` and the original subtitle band is
    covered while a cue is up (``mask``; ``band`` = "black" bar or "blur");
    otherwise the libass ``subtitles`` filter is used as before."""
    src_pss, out_path = Path(src_pss), Path(out_path)
    if max_bytes is None:
        max_bytes = src_pss.stat().st_size
    # the container keeps its exact size; what must fit is the elementary
    # stream inside the original's video slots
    es_budget = video_capacity(src_pss)["es_bytes"]
    with tempfile.TemporaryDirectory() as td:
        es_path = Path(td) / "subbed.m2v"
        encoder = None
        if renderer is not None or cue_dir is not None:
            cues = parse_srt(srt_path)
            png_dir = Path(keep_cues) if keep_cues else Path(td) / "cues"
            pngs = (load_cue_dir(cues, cue_dir) if cue_dir is not None
                    else render_cues(cues, renderer, png_dir))

            def encoder(bitrate, _pngs=pngs):
                encode_es_overlay(src_pss, _pngs, es_path, ffmpeg=ffmpeg, ffprobe=ffprobe,
                                  bitrate_kbps=bitrate, mask=mask, bottom_margin=bottom_margin,
                                  band=band, band_pad=band_pad)
        fit_report = fit_to_budget(src_pss, srt_path, es_path, es_budget,
                                   ffmpeg=ffmpeg, ffprobe=ffprobe, encoder=encoder)
        bitrate = fit_report["bitrate_kbps"]
        for attempt in range(6):
            try:
                splice_report = splice(src_pss, es_path, out_path)
            except ValueError as e:
                if "lower bitrate" not in str(e):
                    raise
                splice_report = {"late": 1, "new_size": 0}
            if not splice_report["late"] and splice_report["new_size"] <= max_bytes:
                break
            bitrate = int(bitrate * 0.96)
            if encoder is not None:
                encoder(bitrate)
            else:
                encode_es(src_pss, srt_path, es_path, ffmpeg=ffmpeg, ffprobe=ffprobe,
                          bitrate_kbps=bitrate)
            fit_report = {**fit_report, "bitrate_kbps": bitrate, "attempts": fit_report["attempts"] + attempt + 1}
        else:
            raise RuntimeError(f"{out_path}: could not pace the video inside the container "
                               f"even at {bitrate} kbps")
    if splice_report["new_size"] > max_bytes:
        raise RuntimeError(
            f"{out_path}: spliced size {splice_report['new_size']} still "
            f"exceeds budget {max_bytes} even after the bitrate fit — retry "
            "with a smaller --max-bytes safety margin")
    return {**fit_report, **splice_report}


# ---------------------------------------------------------------------------
# Layer-1 (raw-sector, no TOC) patch — the piece nothing in the kit did before
# ---------------------------------------------------------------------------

def list_layer1(iso_path) -> list[CarvedStream]:
    iso = IsoImage(iso_path)
    try:
        return scan_layer1(iso)
    finally:
        iso.close()


def patch_layer1(iso_path, name_or_index, new_bytes: bytes,
                 verbose: bool = True) -> dict:
    """Overwrite one carved layer-1 movie **in place** (work on a copy of
    the ISO, as with every other patch path in this kit). ``new_bytes``
    must not exceed the stream's recovered allocation (the gap to the next
    carved stream's start, or to the end of the image for the last one) —
    there is no TOC entry here to resize, and no slack beyond that gap
    without relocating every later movie, which this function refuses to
    attempt."""
    iso = IsoImage(iso_path)
    try:
        streams = scan_layer1(iso)
    finally:
        iso.close()
    matches = ([s for s in streams if s.index == name_or_index]
              if isinstance(name_or_index, int) else
              [s for s in streams if s.name == name_or_index])
    if not matches:
        raise ValueError(f"no layer-1 movie matching {name_or_index!r}")
    stream = matches[0]
    if len(new_bytes) > stream.size:
        raise ValueError(
            f"{stream.name}: new size {len(new_bytes)} exceeds allocation "
            f"{stream.size}")
    with open(iso_path, "r+b") as fh:
        mm = mmap.mmap(fh.fileno(), 0)
        mm[stream.start: stream.start + len(new_bytes)] = new_bytes
        mm.flush()
        readback = bytes(mm[stream.start: stream.start + len(new_bytes)])
        mm.close()
    if readback != new_bytes:
        raise RuntimeError(f"{stream.name}: read-back mismatch")
    if verbose:
        print(f"patched layer1 {stream.name}: -> {len(new_bytes)} bytes "
              f"(allocation {stream.size})")
    return {"name": stream.name, "index": stream.index,
            "old_size": stream.size, "new_size": len(new_bytes)}
