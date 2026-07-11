"""arx.py — ARX compression/decompression (Episode I's bigfile compression).

Algorithm ported from Lakuwu's xenotool (``xeno_arx.c``). ARX is a
word-oriented dictionary coder:

    header:  "ARX\\0", u32 uncompressed size, u32 compressed size, u32 unk,
             u32 lut[30] — the 30 most common words of the payload
    stream:  a single cursor mixes control words and literal words.
             Control bits are consumed MSB-first from u32 control words:
               0   -> copy the next u32 from the stream verbatim
               1   -> a prefix code follows, selecting a LUT entry:
                      0x        -> 2-bit code,  entries 0-1
                      10xx      -> 4-bit code,  entries 2-9   (2 + 3 bits)
                      110xxx    -> 6-bit code,  entries 6-21  (6 + 4 bits)
                      1110xxxxx -> 8-bit code,  entries 14-45 (14 + 5 bits)
             When the control word runs dry the next stream word refills it.

The 6/8-bit index ranges overlap the lower ones and can nominally exceed
the 30-entry table; retail files stay within bounds (guarded here anyway).
"""
from __future__ import annotations

import struct
from collections import Counter

MAGIC = b"ARX\x00"

# Full control-bit sequence (marker 1 + prefix code) per LUT slot. Slots
# 0-1 / 2-5 / 6-13 / 14-29 cost 3 / 5 / 7 / 9 bits; a literal costs 1
# control bit + a 32-bit stream word, so every LUT hit is a win.
_LUT_CODES = [[1, 0, i] for i in range(2)]
_LUT_CODES += [[1, 1, 0, (i - 2) >> 1 & 1, (i - 2) & 1] for i in range(2, 6)]
_LUT_CODES += [[1, 1, 1, 0] + [(i - 6) >> s & 1 for s in (2, 1, 0)]
               for i in range(6, 14)]
_LUT_CODES += [[1, 1, 1, 1] + [(i - 14) >> s & 1 for s in (4, 3, 2, 1, 0)]
               for i in range(14, 30)]


class ARXError(ValueError):
    pass


def is_arx(data: bytes) -> bool:
    return data[:4] == MAGIC


def decompress(data: bytes) -> bytes:
    """Decompress an in-memory ARX blob (header included)."""
    if not is_arx(data):
        raise ARXError("not an ARX blob")
    size_orig, _size_comp, _unk = struct.unpack_from("<III", data, 4)
    lut = struct.unpack_from("<30I", data, 16)
    pos = 16 + 30 * 4
    n_words = len(data) // 4
    words = struct.unpack_from(f"<{n_words}I", data)
    wpos = pos // 4

    out = bytearray()
    out_words_needed = (size_orig + 3) // 4
    written = 0
    buf = 0
    buf_len = 0
    STATE_DATA, STATE_LUT = 0, 1
    state = STATE_DATA
    lut_val = lut_idx = lut_len = 0
    pack = struct.Struct("<I").pack

    while written < out_words_needed and wpos < n_words:
        buf |= words[wpos] << (32 - buf_len)
        wpos += 1
        buf_len += 32
        while buf_len and written < out_words_needed:
            bit = (buf >> 63) & 1
            if state == STATE_DATA:
                if bit:
                    # marker: the next bits form a LUT prefix code
                    state = STATE_LUT
                    lut_val = lut_idx = lut_len = 0
                else:
                    if wpos >= n_words:
                        buf_len = 0
                        break
                    out += pack(words[wpos])
                    wpos += 1
                    written += 1
            else:  # STATE_LUT
                lut_val = ((lut_val << 1) | bit) & 0xFF
                if lut_idx == 0:
                    lut_len = 4 if bit else 2
                elif lut_idx == 1 and lut_len == 4 and bit:
                    lut_len = 6
                elif lut_idx == 2 and lut_len == 6 and bit:
                    lut_len = 8
                lut_idx += 1
                if lut_idx == lut_len:
                    state = STATE_DATA
                    if lut_len == 2:
                        idx = lut_val
                    elif lut_len == 4:
                        idx = 2 + (lut_val & 0x7)
                    elif lut_len == 6:
                        idx = 6 + (lut_val & 0xF)
                    else:
                        idx = 14 + (lut_val & 0x1F)
                    out += pack(lut[idx] if idx < 30 else 0)
                    written += 1
            buf = (buf << 1) & 0xFFFFFFFFFFFFFFFF
            buf_len -= 1

    if written < out_words_needed:
        raise ARXError(
            f"stream ended early: {written * 4}/{size_orig} bytes decoded")
    return bytes(out[:size_orig])


def compress(payload: bytes) -> bytes:
    """Compress ``payload`` into an ARX blob (exact inverse of decompress).

    The LUT holds the 30 most frequent u32 words, most frequent first so
    they get the shortest prefix codes, ties broken by first occurrence —
    which reproduces retail blobs byte-for-byte (verified corpus-wide), so
    Monolith's original packer evidently did the same. Header carries
    unk == 0 and size_comp == total blob length, as retail does.
    """
    n_words = (len(payload) + 3) // 4
    padded = payload + b"\x00" * (n_words * 4 - len(payload))
    words = struct.unpack(f"<{n_words}I", padded) if n_words else ()

    freq = Counter(words)
    first: dict[int, int] = {}
    for i, w in enumerate(words):
        if w not in first:
            first[w] = i
    ranked = sorted(freq.items(), key=lambda t: (-t[1], first[t[0]]))[:30]
    lut = [w for w, _ in ranked]
    slot = {w: i for i, w in enumerate(lut)}
    lut += [0] * (30 - len(lut))

    out: list[int] = []          # u32 words: control words + literals mixed
    ctrl_pos = -1                # index in ``out`` of the active control word
    ctrl_val = 0
    ctrl_n = 32                  # bits used in the active control word

    def put_bit(b: int) -> None:
        nonlocal ctrl_pos, ctrl_val, ctrl_n
        if ctrl_n == 32:
            ctrl_pos = len(out)
            out.append(0)
            ctrl_val = 0
            ctrl_n = 0
        ctrl_val = (ctrl_val << 1) | b
        ctrl_n += 1
        out[ctrl_pos] = ctrl_val << (32 - ctrl_n)

    for w in words:
        i = slot.get(w)
        if i is None:
            put_bit(0)
            out.append(w)
        else:
            for b in _LUT_CODES[i]:
                put_bit(b)

    blob_len = 16 + 30 * 4 + len(out) * 4
    return (MAGIC + struct.pack("<III", len(payload), blob_len, 0)
            + struct.pack("<30I", *lut)
            + struct.pack(f"<{len(out)}I", *out))
