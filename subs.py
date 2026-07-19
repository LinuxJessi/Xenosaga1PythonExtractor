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
    return {"width": v["width"], "height": v["height"], "fps": v["r_frame_rate"],
            "duration": float(fmt["duration"])}


# ---------------------------------------------------------------------------
# SRT scaffolding
# ---------------------------------------------------------------------------

def _srt_timestamp(t: float) -> str:
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    ms = round((s - int(s)) * 1000)
    return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{ms:03d}"


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
# Encode: burn subtitles onto the source video, matching its own container
# parameters, as a raw MPEG-2 elementary stream (no PS container yet)
# ---------------------------------------------------------------------------

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
           "-g", str(gop), "-bf", str(bframes), "-flags", "+cgop",
           # scene-change-triggered GOP breaks are incompatible with closed
           # GOP in ffmpeg's mpeg2video encoder ("not supported yet") unless
           # the threshold is pushed out of reach — this keeps GOP length
           # exactly `gop`, matching the source's fixed 15/3 structure
           "-sc_threshold", "1000000000",
           "-b:v", f"{bitrate_kbps}k", "-maxrate", f"{bitrate_kbps}k",
           "-minrate", f"{bitrate_kbps}k", "-bufsize", f"{bitrate_kbps * 2}k",
           "-f", "mpeg2video", str(out_es)]
    subprocess.run(cmd, check=True, capture_output=True)
    return info


def fit_to_budget(src_pss, srt_path, out_es, max_bytes: int,
                  ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe",
                  attempts: int = 6) -> dict:
    """Encode repeatedly, backing off the bitrate, until the raw ES fits
    under ``max_bytes``. The ES will grow further once re-packetized into
    PES (a few bytes of header per ~64 KB), so this targets a safety margin
    rather than the exact ceiling."""
    duration = probe(src_pss, ffprobe=ffprobe)["duration"]
    bitrate = max(int(max_bytes * 8 / duration / 1000 * 0.85), 200)
    size = None
    for attempt in range(1, attempts + 1):
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

def splice(orig_path, new_es_path, out_path) -> dict:
    """Replace the video elementary stream inside ``orig_path`` with
    ``new_es_path``'s content; every pack/system/audio/padding packet is
    copied verbatim, in its original position and order.

    The new ES is cut into as many chunks as the original had video
    packets, sized in proportion to each original packet's payload length
    (bigger original packets — I-frames — get proportionally bigger new
    chunks too), so the video/audio interleave cadence approximates the
    original's rather than dumping all new video up front. This is a
    best-effort approximation, not a byte-exact timing model — the disc's
    own packets carry no per-packet PTS/DTS beyond an initial anchor, so
    there is no finer-grained ground truth to match against. Verify sync
    on a real movie before trusting a batch of these (docs/SUBTITLES.md).
    """
    orig = Path(orig_path).read_bytes()
    new_es = Path(new_es_path).read_bytes()
    packets = list(iter_packets(orig))
    video = [p for p in packets if p["kind"] == "video"]
    if not video:
        raise ValueError(f"{orig_path}: no video packets found")

    orig_frames = video_track_bytes(packets).count(b"\x00\x00\x01\x00")
    new_frames = new_es.count(b"\x00\x00\x01\x00")
    if orig_frames != new_frames:
        raise ValueError(
            f"{orig_path}: frame count changed ({orig_frames} -> "
            f"{new_frames}) — the carried-over PTS/DTS timestamps would no "
            "longer describe the right timeline. Re-encode at the exact "
            "source fps/duration (check encode_es's -r matches probe())")

    weights = [p["payload_end"] - p["payload_start"] for p in video]
    total_w = sum(weights)
    bounds = [0]
    acc = 0
    for w in weights:
        acc += w
        bounds.append(round(len(new_es) * acc / total_w))
    bounds[-1] = len(new_es)  # rounding must not drop trailing bytes
    chunks = [new_es[bounds[i]:bounds[i + 1]] for i in range(len(weights))]

    out = bytearray()
    vi = 0
    for p in packets:
        if p["kind"] != "video":
            out += p["raw"]
            continue
        chunk = chunks[vi]
        vi += 1
        # No PTS/DTS on any rebuilt packet — tried carrying the original
        # packet's timestamp bytes through (same frame count/fps as the
        # source, so the values are numerically still valid *if* they land
        # on the same decode-order frame), and it produced non-monotonic
        # DTS when tested: ~2.8 PES packets cover each frame here, so
        # proportional-by-byte-size re-splitting doesn't put a "PTS slot"
        # on the same frame it originally described. Omitting timestamps
        # entirely is spec-legal (decoders fall back to deriving timing
        # from the frame-rate) and is the version that round-tripped
        # through ffmpeg with zero warnings in testing.
        pieces = ([chunk[j:j + _MAX_PES_PAYLOAD]
                   for j in range(0, len(chunk), _MAX_PES_PAYLOAD)]
                  or [b""])  # keep the slot even if this chunk landed empty
        for sub in pieces:
            out += (b"\x00\x00\x01\xe0"
                    + struct.pack(">H", 3 + len(sub))
                    + b"\x80\x00\x00" + sub)

    Path(out_path).write_bytes(out)
    return {"orig_size": len(orig), "new_size": len(out),
            "video_packets": len(video), "orig_es_bytes": total_w,
            "new_es_bytes": len(new_es)}


def burn(src_pss, srt_path, out_path, ffmpeg: str = "ffmpeg",
        ffprobe: str = "ffprobe", max_bytes: Optional[int] = None) -> dict:
    """End to end: encode the subtitled video within budget, splice it back
    over the original's video track, original audio untouched throughout."""
    src_pss, out_path = Path(src_pss), Path(out_path)
    if max_bytes is None:
        max_bytes = src_pss.stat().st_size
    with tempfile.TemporaryDirectory() as td:
        es_path = Path(td) / "subbed.m2v"
        fit_report = fit_to_budget(src_pss, srt_path, es_path, max_bytes,
                                   ffmpeg=ffmpeg, ffprobe=ffprobe)
        splice_report = splice(src_pss, es_path, out_path)
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
