"""browse.py — convert extracted assets into immediately-viewable formats.

The Episode I counterpart of the Episode III kit's ``browse_bundle.py``:
after ``extract`` has produced ``OUTDIR/dump/``, this builds a sibling
``OUTDIR/browse/`` tree you can actually look at and listen to.

* ``textures`` — decode ``.xtx`` to PNG (pure Python, stdlib zlib).

  XS1's ``.xtx`` is a **virtual GS memory dump**, not a plain image (format
  understanding owed to Lakuwu's xenotool). Header: u32 magic, u32 total
  size, u32 sub-image count, u32 header table offset; then one 20-byte
  record per sub-image: ``u16 width, u16 buffer_width, u16 height, u16 pad,
  u32 gs_offset, u32 size, u32 file_addr``. Each sub-image (32-byte
  sub-header, then raster CT32 rows) is composed onto a CT32 canvas —
  256 or 512 pixels wide per ``buffer_width`` 4 / 8 — at the page position
  encoded by ``gs_offset`` (4096-byte GS blocks, ``buffer_width/2`` per
  row). The composed canvas is then unswizzled as one big **PSMT8 8-bpp
  indexed** image at double the CT32 dimensions. 16x16 sub-images are
  256-entry CSM1 palettes (two middle 8-entry runs of each 32 swapped);
  the first palette in the file is applied — multi-palette atlases pick
  their palette per material in the model files, which a standalone
  texture pass can't know. Files with no palette are written as grayscale
  index images. PS2 alpha is 7-bit (128 = opaque) and is scaled
  ``min(a*2, 255)``. The 810 ARX-compressed ``.xtx`` are reported skipped.

* ``audio`` — decode ``.vds``/``.vdm`` streamed audio to 16-bit WAV.

  The streams are headerless PS2 SPU ADPCM, **stereo, block-interleaved
  every 0x400 bytes** (64 frames per channel per block; verified by
  channel-envelope correlation ~0.95 at exactly that granularity). The
  sample rate is 48000 Hz — the constant the scene classes pass to
  ``xeno.Sound.streamPlay`` next to the stream's file id (lifted with the
  ``classes`` command). Genuinely mono streams are detected per file and
  kept mono.

* ``images`` / ``text`` — copy ``.jpg`` / ``.txt`` straight across so the
  browse tree is self-contained.

* ``movies`` — transcode ``.pss``/``.ipu`` (MPEG-2 PS / PS2 IPU) to H.264
  MP4 **via ffmpeg when one can be found** (bundled tools/, PATH, common
  install dirs — same probe order as the Episode III kit). Without ffmpeg
  the kind is skipped with a note; raw ``.pss`` files also play directly
  in VLC / ffplay.
"""
from __future__ import annotations

import os
import shutil
import struct
import subprocess
import sys
import wave
import zlib
from pathlib import Path
from typing import Iterable, Optional

import arx

ALL_KINDS = ("textures", "audio", "banks", "images", "text", "movies")

VOICE_RATE = 48000  # from xeno.Sound.streamPlay(id, 48000, ...) in the .evt scripts


# ---------------------------------------------------------------------------
# PNG writer (stdlib only)
# ---------------------------------------------------------------------------

def _png_chunk(tag: bytes, payload: bytes) -> bytes:
    return (struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))


def write_png(path: Path, width: int, height: int, rgba: bytes) -> None:
    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter: none
        raw += rgba[y * stride : (y + 1) * stride]
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _png_chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# XTX -> PNG
# ---------------------------------------------------------------------------

_ALPHA_SCALE = bytes(min(a * 2, 255) for a in range(256))


def _plausible_clut(pal: Optional[list[bytes]]) -> bool:
    """Reject palette tiles that are clearly not palettes (empty canvas,
    flat fills). Material palette pointers describe *runtime VRAM* slots,
    which usually — but not always — match the tile's position in the file
    canvas; a pointer into unloaded space reads back near-empty."""
    if pal is None:
        return False
    distinct = len({p[:3] for p in pal})
    return distinct >= 16 and any(p[3] for p in pal)


def _clut_at(canvas: bytearray, clen: int, palx: int, paly: int) -> Optional[list[bytes]]:
    """Read a 256-entry CSM1 palette from the CT32 canvas at (palx, paly)."""
    if palx + 16 > clen or paly + 16 > clen:
        return None
    pal = []
    for ey in range(16):
        row = ((paly + ey) * clen + palx) * 4
        for ex in range(16):
            p = canvas[row + ex * 4 : row + ex * 4 + 4]
            pal.append(bytes((p[0], p[1], p[2], min(p[3] * 2, 255))))
    for g in range(8):  # CSM1 storage order -> logical order
        for j in range(8):
            k, m = g * 32 + 8 + j, g * 32 + 16 + j
            pal[k], pal[m] = pal[m], pal[k]
    return pal


def _scan_for_clut(canvas: bytearray, clen: int) -> Optional[list[bytes]]:
    """Find an embedded palette tile in a canvas with no other palette source.

    Menu/backdrop textures (casino, UI, dev-folder art) park their CLUT as a
    16x16 CT32 tile in an unused corner of the same canvas; the consuming
    code (e.g. OV11.OVL's texture descriptors) addresses it by GS block
    pointer. Standalone we scan for it: raw alpha <= 0x80 throughout and a
    rich colour count, trying the conventional spots first, then all
    block-aligned tiles bottom-right first.
    """
    def rich(px: int, py: int) -> bool:
        distinct = set()
        for ey in range(16):
            row = ((py + ey) * clen + px) * 4
            for ex in range(16):
                r, g, b, a = canvas[row + ex * 4 : row + ex * 4 + 4]
                if a > 0x80:
                    return False
                distinct.add((r, g, b))
        return len(distinct) >= 64

    spots = [(0, 224), (240, 240), (224, 240), (176, 240), (112, 64), (128, 0)]
    spots += [(x, y) for y in range(clen - 16, -1, -16)
              for x in range(clen - 16, -1, -16)]
    for px, py in spots:
        if px + 16 <= clen and py + 16 <= clen and rich(px, py):
            return _clut_at(canvas, clen, px, py)
    return None


def lex_materials(lex: bytes) -> list[tuple[int, int, int, int, int, int]]:
    """(palx, paly, umin, umax, vmin, vmax) per mesh material of a .lex model.

    Reads only the fixed-offset mesh headers (palette byte at +0x125, UV info
    at +0x130 — layout from xenotool's lex_file.h); the extra material blocks
    embedded in the VIF vertex streams are not chased.
    """
    if lex[:4] != b"lex\x00" or len(lex) < 0xB0:
        return []
    (nmesh,) = struct.unpack_from("<I", lex, 0x44)
    if not (0 < nmesh <= 4096):
        return []
    mats = []
    for i in range(nmesh):
        off = 0xB0 + 4 * i
        if off + 4 > len(lex):
            break
        (addr,) = struct.unpack_from("<I", lex, off)
        if addr + 0x190 > len(lex):
            continue
        pal2, pal = lex[addr + 0x124], lex[addr + 0x125]
        if pal == 0xFF:
            continue
        t = lex[addr + 0x130]
        b = lex[addr + 0x131 : addr + 0x140]
        if t == 0x00:
            continue
        if t == 0xFF:
            w, x1, x = b[0] & 0xF, (b[1] >> 3) & 1, b[1] >> 4
            h, y1, y = b[2] >> 4, b[3] >> 7, b[4] & 0xF
            umin, vmin = x * 64 + x1 * 32, y * 64 + y1 * 32
            umax, vmax = umin + (w + 1) * 16, vmin + (h + 1) * 16
        else:  # 0x0a and friends
            umin = (b[0] & 0x3F) << 4
            vmin = b[2]
            umax = ((b[1] << 2) | (b[0] >> 6)) + 1
            vmax = ((b[4] << 6) | (b[3] >> 2)) + 1
        pal_hi, pal_lo = pal >> 4, pal & 0xF
        palx = (pal_hi % 2) * 256 + (pal_lo // 2) * 32 + (pal2 >> 7) * 16
        paly = (pal_hi // 2) * 32 + (pal_lo % 2) * 16
        mats.append((palx, paly, umin, umax, vmin, vmax))
    return mats


def decode_xtx(data: bytes, lex: bytes = b"") -> Optional[tuple[int, int, bytes]]:
    """(width, height, RGBA) of the composed texture, or None.

    ``lex`` — the paired model file, when there is one; its materials say
    which palette tile in the canvas colours which UV region.
    """
    if data[:4] != b"XTX\x00" or len(data) < 0x24:
        return None
    _total, count, hdr_addr = struct.unpack_from("<III", data, 4)
    if not (1 <= count <= 64) or hdr_addr + 20 * count > len(data):
        return None
    subs = []
    for i in range(count):
        base = hdr_addr + 20 * i
        w, bufw, h = struct.unpack_from("<HHH", data, base)
        gs_off, _size, addr = struct.unpack_from("<III", data, base + 8)
        if addr + 32 + w * h * 4 > len(data):
            return None
        subs.append((w, bufw, h, gs_off, addr))
    bufw = subs[0][1] or 8
    clen = {4: 256, 8: 512}.get(bufw)
    if clen is None:
        return None
    canvas = bytearray(clen * clen * 4)  # CT32 canvas, clen x clen pixels
    embedded_pal: Optional[list[bytes]] = None
    max_x = max_y = 0
    for w, _, h, gs_off, addr in subs:
        px = data[addr + 32 : addr + 32 + w * h * 4]
        block = gs_off // 4096
        x0 = (block % (bufw // 2)) * 64
        y0 = (block // (bufw // 2)) * 32
        if x0 + w > clen or y0 + h > clen:
            continue
        for y in range(h):
            dst = ((y0 + y) * clen + x0) * 4
            canvas[dst : dst + w * 4] = px[y * w * 4 : (y + 1) * w * 4]
        if w == 16 and h == 16 and count > 1:
            # a palette tile: composed into the canvas (materials address it
            # there) but not part of the visible image extent
            if embedded_pal is None:
                embedded_pal = _clut_at(canvas, clen, x0, y0)
            continue
        max_x = max(max_x, (x0 + w) * 2)
        max_y = max(max_y, (y0 + h) * 2)
    if not max_x or not max_y:
        return None

    # The canvas holds PSMT8 indices swizzled into CT32; unswizzle the
    # cropped region ("unswizzle8", the widely shared PS2 routine).
    W, H, tw = max_x, max_y, clen * 2
    idx = bytearray(W * H)
    for y in range(H):
        block_row = (y & ~0xF) * tw
        swap_selector = (((y + 2) >> 2) & 1) * 4
        col_row = ((((y & ~3) >> 1) + (y & 1)) & 7) * tw * 2
        byte_y = (y >> 1) & 1
        drow = y * W
        for x in range(W):
            idx[drow + x] = canvas[
                block_row + (x & ~0xF) * 2 + col_row
                + ((x + swap_selector) & 7) * 4 + byte_y + ((x >> 2) & 2)]

    mats = lex_materials(lex) if lex else []
    base_pal = embedded_pal
    if base_pal is None and mats:
        cand = _clut_at(canvas, clen, mats[0][0], mats[0][1])
        if _plausible_clut(cand):
            base_pal = cand
    if base_pal is None:
        base_pal = _scan_for_clut(canvas, clen)
    base_lut = ([bytes(p) for p in base_pal] if base_pal
                else [bytes((i, i, i, 255)) for i in range(256)])

    out = bytearray(W * H * 4)
    for y in range(H):
        drow = y * W * 4
        irow = y * W
        for x in range(W):
            out[drow + x * 4 : drow + x * 4 + 4] = base_lut[idx[irow + x]]
    # per-material regions override the base palette
    for palx, paly, umin, umax, vmin, vmax in mats:
        pal = _clut_at(canvas, clen, palx, paly)
        if pal is None or pal is base_pal or not _plausible_clut(pal):
            continue
        for v in range(max(0, vmin), min(H, vmax)):
            drow = v * W * 4
            irow = v * W
            for u in range(max(0, umin), min(W, umax)):
                out[drow + u * 4 : drow + u * 4 + 4] = pal[idx[irow + u]]
    return W, H, bytes(out)


# ---------------------------------------------------------------------------
# SPU ADPCM (.vds/.vdm) -> WAV
# ---------------------------------------------------------------------------

_SPU_FILTERS = ((0, 0), (60, 0), (115, -52), (98, -55), (122, -60))
_SIGNED_NIBBLE = tuple(n - 16 if n >= 8 else n for n in range(16))


def decode_spu_adpcm(data: bytes) -> bytes:
    """Decode headerless SPU ADPCM to 16-bit little-endian mono PCM."""
    import array

    out = array.array("h")
    h1 = h2 = 0
    nib = _SIGNED_NIBBLE
    for base in range(0, len(data) - 15, 16):
        hdr = data[base]
        shift = hdr & 0x0F
        filt = hdr >> 4
        if filt > 4 or shift > 12:  # invalid frame; keep sync, emit silence
            out.extend((0,) * 28)
            continue
        f0, f1 = _SPU_FILTERS[filt]
        up = 12 - shift
        for b in data[base + 2 : base + 16]:
            for n in (nib[b & 0x0F], nib[b >> 4]):
                # predictor divides by 64 rounding toward zero (matches the
                # SPU / ffmpeg adpcm_psx exactly; plain >> 6 floors instead)
                p = h1 * f0 + h2 * f1
                s = (n << up) + (p // 64 if p >= 0 else -((-p) // 64))
                if s > 32767:
                    s = 32767
                elif s < -32768:
                    s = -32768
                h2 = h1
                h1 = s
                out.append(s)
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes()


STEREO_BLOCK = 0x400  # L/R interleave granularity of .vds/.vdm streams


def decode_voice_stream(data: bytes) -> tuple[bytes, int]:
    """Decode a .vds/.vdm stream -> (interleaved 16-bit PCM, channel count).

    The streams are stereo, block-interleaved every 0x400 bytes (64 SPU
    frames per channel per block) — found empirically: deinterleaved halves
    of retail files correlate strongly at exactly this granularity and at no
    other, with envelope correlation ~0.95. Each channel is decoded with its
    own predictor chain. A per-file envelope check keeps genuinely mono
    streams mono instead of shredding them into 21 ms chunks.
    """
    G = STEREO_BLOCK
    pairs = len(data) // (2 * G)
    if pairs < 4:
        return decode_spu_adpcm(data), 1
    left = b"".join(data[i : i + G] for i in range(0, pairs * 2 * G, 2 * G))
    right = b"".join(data[i + G : i + 2 * G] for i in range(0, pairs * 2 * G, 2 * G))
    import array

    L = array.array("h")
    L.frombytes(decode_spu_adpcm(left))
    R = array.array("h")
    R.frombytes(decode_spu_adpcm(right))
    n = min(len(L), len(R))

    # envelope correlation: ~1 for L/R of one recording, ~0 for a mono
    # stream wrongly split into alternating time chunks
    win = 4800
    eL = [sum(abs(L[j]) for j in range(i, i + win, 8)) for i in range(0, n - win, win)]
    eR = [sum(abs(R[j]) for j in range(i, i + win, 8)) for i in range(0, n - win, win)]
    m = len(eL)
    if m >= 4:
        sa, sb = sum(eL) / m, sum(eR) / m
        num = da = db = 0.0
        for a, b in zip(eL, eR):
            x, y = a - sa, b - sb
            num += x * y
            da += x * x
            db += y * y
        if num / ((da * db) ** 0.5 + 1e-9) < 0.5:
            return decode_spu_adpcm(data), 1  # genuinely mono

    out = array.array("h", bytes(4 * n))
    out[0::2] = L[:n]
    out[1::2] = R[:n]
    if sys.byteorder == "big":
        out.byteswap()
    return out.tobytes(), 2


def write_wav(path: Path, pcm: bytes, rate: int, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)


# ---------------------------------------------------------------------------
# SWD wave banks / SMD sequences (Procyon Studio sequenced BGM)
# ---------------------------------------------------------------------------

BANK_RATE = 32000  # audition rate; true per-note pitch comes from the sequences


def parse_swd(data: bytes) -> Optional[list[tuple[str, int, int]]]:
    """(name, start, end) byte ranges of the SPU samples in a swdm bank."""
    if data[:4] != b"swdm" or len(data) < 0x80:
        return None
    body_size, body_off = struct.unpack_from("<II", data, 0x24)
    if body_off < 0x70 or body_off + body_size > len(data):
        return None
    entries = []
    off = 0x50
    while off + 32 <= body_off:
        (rel,) = struct.unpack_from("<I", data, off)
        name = data[off + 16 : off + 32].rstrip(b"\x00")
        if rel >= body_size or not name or not all(32 <= c < 127 for c in name):
            break
        entries.append((name.decode(), rel))
        off += 32
    if not entries:
        return None
    order = sorted(range(len(entries)), key=lambda i: entries[i][1])
    out = []
    for k, i in enumerate(order):
        name, rel = entries[i]
        end = entries[order[k + 1]][1] if k + 1 < len(order) else body_size
        out.append((name, body_off + rel, body_off + end))
    return out


def _trim_sample(data: bytes, start: int, end: int) -> bytes:
    """Cut a bank sample after its first ADPCM end-flagged frame."""
    for base in range(start, min(end, len(data)) - 15, 16):
        if data[base + 1] & 1:
            return data[start : base + 16]
    return data[start:end]


def smd_info(data: bytes) -> Optional[list[str]]:
    """The ASCII metadata strings of an smdm sequence (title, game, ...)."""
    if data[:4] != b"smdm":
        return None
    strs = []
    p = 0x2C
    while len(strs) < 5 and p < min(len(data), 0x200):
        q = data.find(b"\x00", p)
        if q <= p:
            break
        s = data[p:q]
        if not all(32 <= c < 127 for c in s):
            break
        strs.append(s.decode())
        p = q + 1
    return strs


# ---------------------------------------------------------------------------
# ffmpeg detection (same probe order as the Episode III kit)
# ---------------------------------------------------------------------------

_FFMPEG_POSIX = ("/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg",
                 "/opt/homebrew/bin/ffmpeg", "/snap/bin/ffmpeg")
_FFMPEG_WIN = (r"C:\ffmpeg\bin\ffmpeg.exe",
               r"C:\Program Files\ffmpeg\bin\ffmpeg.exe")


def detect_ffmpeg() -> Optional[str]:
    name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        for tools in (
            exe_dir / "tools",                        # manual drop next to the exe
            exe_dir / "_internal" / "tools",          # bundled, one-folder build
            exe_dir.parent / "Frameworks" / "tools",  # bundled, macOS .app
        ):
            cand = tools / name
            if cand.is_file():
                return str(cand)
    hit = shutil.which("ffmpeg")
    if hit:
        return hit
    for cand in _FFMPEG_WIN if os.name == "nt" else _FFMPEG_POSIX:
        p = Path(os.path.expandvars(cand))
        if p.is_file():
            return str(p)
    return None


def extract_pss_audio(data: bytes) -> Optional[tuple[bytes, int, int]]:
    """(PCM, channels, rate) from a .pss movie's private stream, or None.

    Xenosaga movies mux audio as MPEG private stream 1 (0xBD): each PES
    payload starts with a 4-byte substream tag (ff a1 00 00); concatenated
    payloads form a Sony ADS stream — "SShd" header declaring format 0x10
    (SPU ADPCM), rate, channels and interleave (0x400 — the same layout as
    the .vds voice streams), then "SSbd" + body. ffmpeg misparses these
    packets, which is why plain conversion yields video-only files.
    """
    chunks = []
    i = 0
    n = len(data)
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 6 > n:
            break
        sid = data[i + 3]
        if sid == 0xBA:
            i += 14
            continue
        if sid == 0xB9:
            break
        (ln,) = struct.unpack_from(">H", data, i + 4)
        if sid == 0xBD and i + 9 <= n:
            hdr_len = data[i + 8]
            pstart = i + 9 + hdr_len + 4  # + substream tag
            pend = min(i + 6 + ln, n)
            if pstart < pend:
                chunks.append(data[pstart:pend])
        i += 6 + ln
    raw = b"".join(chunks)
    if raw[:4] != b"SShd" or len(raw) < 40:
        return None
    (hsize,) = struct.unpack_from("<I", raw, 4)
    fmt, rate, ch, inter = struct.unpack_from("<IIII", raw, 8)
    body_at = 8 + hsize
    if fmt != 0x10 or raw[body_at : body_at + 4] != b"SSbd":
        return None
    body = raw[body_at + 8 :]
    if ch == 2 and inter:
        pairs = len(body) // (2 * inter)
        left = b"".join(body[k : k + inter]
                        for k in range(0, pairs * 2 * inter, 2 * inter))
        right = b"".join(body[k + inter : k + 2 * inter]
                         for k in range(0, pairs * 2 * inter, 2 * inter))
        import array

        L = array.array("h")
        L.frombytes(decode_spu_adpcm(left))
        R = array.array("h")
        R.frombytes(decode_spu_adpcm(right))
        m = min(len(L), len(R))
        out = array.array("h", bytes(4 * m))
        out[0::2] = L[:m]
        out[1::2] = R[:m]
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes(), 2, rate
    return decode_spu_adpcm(body), 1, rate


def convert_movie(ffmpeg: str, src: Path, dest: Path) -> bool:
    """Convert one movie.

    For movies carrying audio this produces THREE files so fan projects
    (undubs/redubs) get the tracks already divorced:
        <name>.mp4        muxed video + audio
        <name>.video.mp4  video only (the same encode, remux — no 2nd pass)
        <name>.audio.wav  the demuxed stream, decoded to PCM
    Video-only movies produce just <name>.mp4.
    """
    audio = None
    if src.suffix.lower() == ".pss":
        try:
            audio = extract_pss_audio(src.read_bytes())
        except Exception:
            audio = None

    def run(args) -> bool:
        try:
            return subprocess.run(args, capture_output=True).returncode == 0
        except OSError:
            return False

    base = [ffmpeg, "-y", "-loglevel", "error"]
    demux = ["-f", "ipu"] if src.suffix.lower() == ".ipu" else []
    enc = ["-c:v", "libx264", "-crf", "20", "-preset", "veryfast",
           "-pix_fmt", "yuv420p", "-an"]
    if not audio:
        return run(base + demux + ["-i", str(src)] + enc + [str(dest)])

    pcm, ch, rate = audio
    video_only = dest.with_suffix("") .with_name(dest.stem + ".video.mp4")
    audio_wav = dest.with_name(dest.stem + ".audio.wav")
    write_wav(audio_wav, pcm, rate, ch)
    if not run(base + demux + ["-i", str(src)] + enc + [str(video_only)]):
        return False
    # mux = stream copy of the encode + AAC of the wav; the wav stays on disk
    return run(base + ["-i", str(video_only), "-i", str(audio_wav),
                       "-map", "0:v:0", "-map", "1:a:0",
                       "-c:v", "copy", "-c:a", "aac", "-shortest", str(dest)])


# ---------------------------------------------------------------------------
# The bundle builder
# ---------------------------------------------------------------------------

def _dump_files(dump: Path, exts: tuple[str, ...]) -> Iterable[Path]:
    for p in sorted(dump.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts:
            yield p


def _load(src: Path, bump) -> bytes:
    """Read a dump file, transparently ARX-decompressing (empty on failure)."""
    data = src.read_bytes()
    if arx.is_arx(data):
        try:
            data = arx.decompress(data)
            bump("arx_decompressed")
        except arx.ARXError:
            bump("arx_failed")
            return b""
    return data


def build_browse(out_dir: Path, kinds: Iterable[str], rate: int = VOICE_RATE,
                 log=lambda s: print(s, file=sys.stderr)) -> dict:
    dump = out_dir / "dump"
    if not dump.is_dir():
        raise FileNotFoundError(f"{dump} not found — run extract first")
    browse = out_dir / "browse"
    stats: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        stats[key] = stats.get(key, 0) + n

    kinds = set(kinds)

    if "textures" in kinds:
        tdir = browse / "textures_png"
        srcs = list(_dump_files(dump, (".xtx",)))
        log(f"textures: decoding {len(srcs)} .xtx -> PNG ...")
        for i, src in enumerate(srcs, 1):
            data = _load(src, bump)
            lex = b""
            sib = src.with_suffix(".lex")
            if sib.is_file():
                lex = _load(sib, bump)
            decoded = decode_xtx(data, lex) if data else None
            if decoded is None:
                bump("textures_undecodable")
                continue
            w, h, rgba = decoded
            dest = tdir / src.relative_to(dump).with_suffix(".png")
            dest.parent.mkdir(parents=True, exist_ok=True)
            write_png(dest, w, h, rgba)
            bump("textures_png")
            if i % 50 == 0:
                log(f"  {i}/{len(srcs)}  ({src.relative_to(dump)})")
        log(f"textures: {stats.get('textures_png', 0)} PNGs "
            f"({stats.get('textures_undecodable', 0)} undecodable)")

    if "audio" in kinds:
        adir = browse / "audio"
        srcs = list(_dump_files(dump, (".vds", ".vdm")))
        log(f"audio: decoding {len(srcs)} voice streams -> WAV at {rate} Hz ...")
        for i, src in enumerate(srcs, 1):
            data = _load(src, bump)
            if not data:
                continue
            dest = adir / src.relative_to(dump).with_suffix(".wav")
            dest.parent.mkdir(parents=True, exist_ok=True)
            pcm, channels = decode_voice_stream(data)
            write_wav(dest, pcm, rate, channels)
            bump("audio_stereo" if channels == 2 else "audio_mono")
            if i % 5 == 0:
                log(f"  {i}/{len(srcs)}  ({src.relative_to(dump)})")
        log(f"audio: {stats.get('audio_stereo', 0)} stereo + "
            f"{stats.get('audio_mono', 0)} mono WAVs at {rate} Hz")

    if "banks" in kinds:
        import csv as _csv

        bdir = browse / "soundbanks"
        swds = list(_dump_files(dump, (".swd",)))
        smds = list(_dump_files(dump, (".smd",)))
        log(f"banks: carving {len(swds)} .swd wave banks -> per-instrument WAVs "
            f"(audition rate {BANK_RATE} Hz) ...")
        for i, src in enumerate(swds, 1):
            data = _load(src, bump)
            bank = parse_swd(data) if data else None
            if not bank:
                bump("banks_skipped")
                continue
            outd = bdir / src.relative_to(dump).with_suffix("")
            outd.mkdir(parents=True, exist_ok=True)
            for name, s, e in bank:
                safe = "".join(c if c.isalnum() or c in "._-#" else "_" for c in name)
                pcm = decode_spu_adpcm(_trim_sample(data, s, e))
                write_wav(outd / f"{safe}.wav", pcm, BANK_RATE)
                bump("bank_samples")
            bump("banks")
            if i % 25 == 0:
                log(f"  {i}/{len(swds)}  ({src.relative_to(dump)})")
        rows = []
        for src in smds:
            data = _load(src, bump)
            strs = smd_info(data) if data else None
            if strs is None:
                continue
            rows.append({
                "file": str(src.relative_to(dump)), "size": len(data),
                "title": strs[0] if strs else "",
                "game": strs[1] if len(strs) > 1 else "",
                "composer": strs[2] if len(strs) > 2 else "",
                "studio": strs[3] if len(strs) > 3 else "",
                "notes": strs[4] if len(strs) > 4 else "",
                "music": int(len(data) >= 5000),  # tiny SMDs are ambience stubs
            })
        if rows:
            bdir.mkdir(parents=True, exist_ok=True)
            with open(bdir / "smd_catalog.csv", "w", newline="") as fh:
                w = _csv.DictWriter(fh, fieldnames=[
                    "file", "size", "title", "game", "composer", "studio",
                    "notes", "music"])
                w.writeheader()
                w.writerows(rows)
        log(f"banks: {stats.get('banks', 0)} banks -> "
            f"{stats.get('bank_samples', 0)} instrument samples; "
            f"{len(rows)} sequences catalogued in soundbanks/smd_catalog.csv "
            f"({sum(r['music'] for r in rows)} look like real music)")

    if "images" in kinds:
        idir = browse / "images"
        for src in _dump_files(dump, (".jpg", ".jpeg")):
            data = _load(src, bump)
            if not data:
                continue
            dest = idir / src.relative_to(dump)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            bump("images")
        log(f"images: {stats.get('images', 0)} copied")

    if "text" in kinds:
        xdir = browse / "text"
        for src in _dump_files(dump, (".txt",)):
            data = _load(src, bump)
            if not data:
                continue
            dest = xdir / src.relative_to(dump)
            dest.parent.mkdir(parents=True, exist_ok=True)
            # game text is Shift-JIS with inline \NN control codes; recode to
            # UTF-8 so editors show the Japanese instead of mojibake
            try:
                dest.write_text(data.decode("cp932"), encoding="utf-8")
                bump("text_utf8")
            except UnicodeDecodeError:
                dest.write_bytes(data)
                bump("text_raw")
        log(f"text: {stats.get('text_utf8', 0)} transcoded to UTF-8, "
            f"{stats.get('text_raw', 0)} copied raw")

    if "movies" in kinds:
        ffmpeg = detect_ffmpeg()
        if not ffmpeg:
            log("movies: SKIPPED — no ffmpeg found (install it or drop one in "
                "tools/ next to the exe). Raw .pss files play in VLC/ffplay.")
            stats["movies_skipped_no_ffmpeg"] = 1
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            mdir = browse / "movies"
            work = []
            for src in _dump_files(dump, (".pss", ".ipu")):
                with open(src, "rb") as fh:
                    if fh.read(4) == b"ARX\x00":
                        bump("movies_arx_skipped")
                        continue
                dest = mdir / src.relative_to(dump).with_suffix(".mp4")
                dest.parent.mkdir(parents=True, exist_ok=True)
                work.append((src, dest))
            jobs = max(2, (os.cpu_count() or 4) // 2)
            log(f"movies: converting {len(work)} files with {ffmpeg} "
                f"({jobs} at a time — this is the slow step)")
            with ThreadPoolExecutor(max_workers=jobs) as pool:
                futs = {pool.submit(convert_movie, ffmpeg, s, d): s for s, d in work}
                done = 0
                for fut in as_completed(futs):
                    done += 1
                    if fut.result():
                        bump("movies_mp4")
                    else:
                        bump("movies_failed")
                        log(f"  FAILED {futs[fut].relative_to(dump)}")
                    if done % 20 == 0 or done == len(work):
                        log(f"  {done}/{len(work)}")
            log(f"movies: {stats.get('movies_mp4', 0)} converted, "
                f"{stats.get('movies_failed', 0)} failed")

    return stats
