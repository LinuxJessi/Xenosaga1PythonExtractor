"""ssd.py — Procyon Studio SSD sequenced-BGM support: SMD/SWD -> MIDI/SF2/WAV.

Format knowledge reverse-engineered from the game's own IOP sound driver
(SSD.IRX ships unstripped; every sequencer opcode handler is symbol-named).
See docs/FORMATS.md "Sequenced BGM" for the full derivation notes.

SMD sequence file ("smdm"):
  0x08 u32 file size, header to 0x28 (the u8 @ 0x20 is NOT a timebase —
  the driver's tick rate is fixed at 96 PPQN, see the TPQN note below),
  then chunks of [u16 type, u16 size]: type 2 = metadata strings,
  type 3 = track (u8 midi_channel @ +6, event stream from +8), type 0 = end.

  Events: 0x00-0x7F = note-on, byte is the VELOCITY; next byte packs
  [7:6]=count of big-endian gate bytes (0 = reuse last gate),
  [5:4]=octave step -1..+2 applied to the running octave,
  [3:0]=semitone. Key = octave_base + semitone; gate = duration in ticks.
  Opcodes >= 0x80 dispatch through SsdSeqFuncTrap (operand length from
  SsdSeqFuncLength); the ones that matter are implemented in _walk_track.

SWD wave bank ("swdm", also embedded in "seds" SFX banks):
  0x12 u16 bank id, 0x15 u8 program count, 0x24 u32 body size,
  0x28 u32 body offset, chunks from 0x40 (same [type,size] scheme):
  type 3 = sample table, 32-byte entries:
      u32 body-relative offset, u32 loop-start offset, u8 volume, u8 pan,
      s16 base pitch (note16 units: semitone<<8, relative to key 60 at
      48 kHz -> native rate = 48000 * 2^(pitch/256/12)), u16 ADSR1,
      u16 ADSR2, char name[16]
  type 4 = program chunk: u16 offset table (one per program, 0 = absent)
  -> program record: u8 split count @ +3, 16-byte splits from +0x60:
      u8 sample index, u8 root key, u8 key_lo, u8 key_hi, u8 volume,
      u8 pan, u8 flags, u8 pad, u16 ADSR1, u16 ADSR2, pad
  ADSR values are raw SPU2 envelope registers.

Pitch: SPU pitch = 0x1000 * 2^((note16/256 - 60)/12) (table-verified exact
equal temperament), where note16 = sample_base_pitch + (note+60-root)<<8.
"""
from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from browse import decode_spu_adpcm

# The driver's timebase is FIXED at 96 ticks per quarter note. Ground truth
# from SSD.IRX: the sequencer ISR (SsdMainInterruptProcess) fires every 2 ms
# (USec2SysClock(2_000_000)/1000 in SsdInitTimer), each firing subtracts
# rate@ctx+0x44 from a 16.16 accumulator@+0x40 and advances one sequence tick
# on underflow; SsdSeqTempoAbsolute computes rate = ((tempo*53687)>>8 *
# master_scale)>>8 with master_scale 0x100 neutral (upper half of the 16.16
# word SsdSetSeqMasterTempo stores at +0x7c). So ticks/sec = 500*rate/65536
# = 1.6*tempo = tempo*96/60 exactly. The SMD header byte @0x20 (retail:
# 100/120/123/127) is NOT a timebase — treating it as one rendered battle
# themes 25% fast while U.M.N. Mode (100) was only 4% off.
TPQN = 96
SPU_RATE = 48000

# ---------------------------------------------------------------------------
# SWD wave banks
# ---------------------------------------------------------------------------


@dataclass
class Sample:
    index: int
    name: str
    offset: int          # body-relative byte offset of ADPCM data
    volume: int
    base_pitch: int      # s16, note16 units
    adsr1: int
    adsr2: int
    adpcm: bytes = b""
    pcm: bytes = b""     # decoded s16le mono
    loop_start: int = -1  # in samples, -1 = one-shot

    @property
    def native_rate(self) -> int:
        return round(SPU_RATE * 2.0 ** (self.base_pitch / 256.0 / 12.0))


@dataclass
class Split:
    sample: int
    root: int
    key_lo: int
    key_hi: int
    volume: int
    pan: int
    adsr1: int
    adsr2: int


@dataclass
class Bank:
    name: str
    bank_id: int
    samples: list[Sample] = field(default_factory=list)
    programs: dict[int, list[Split]] = field(default_factory=dict)

    def split_for(self, program: int, key: int) -> Optional[Split]:
        splits = self.programs.get(program)
        if not splits:
            return None
        for s in splits:
            if s.key_lo <= key <= s.key_hi:
                return s
        return splits[-1]


def _adpcm_loop_start(adpcm: bytes) -> int:
    """Loop start in output samples from the stream's flag bytes.

    SPU2 flag byte per 16-byte frame: bit2 marks the loop start frame;
    the final frame carries bit0 (end); bit1 there means "repeat" (loop)
    rather than stop. Returns -1 for one-shot samples.
    """
    loop_frame = -1
    loops = False
    for i, base in enumerate(range(0, len(adpcm) - 15, 16)):
        flags = adpcm[base + 1]
        if flags & 0x4:
            loop_frame = i
        if flags & 0x1:
            loops = bool(flags & 0x2)
            break
    return loop_frame * 28 if (loops and loop_frame >= 0) else -1


def parse_bank(data: bytes, name: str = "") -> Optional[Bank]:
    """Parse a swdm bank (accepts seds wrappers by scanning for 'swdm')."""
    if data[:4] == b"seds":
        pos = data.find(b"swdm")
        if pos < 0:
            return None
        data = data[pos:]
    if data[:4] != b"swdm" or len(data) < 0x50:
        return None
    (bank_id,) = struct.unpack_from("<H", data, 0x12)
    prog_count = data[0x15]
    body_size, body_off = struct.unpack_from("<II", data, 0x24)
    if body_off <= 0 or body_off > len(data):
        return None
    bank = Bank(name=name, bank_id=bank_id)

    chunks: dict[int, tuple[int, int]] = {}
    off = 0x40
    while off + 4 <= body_off:
        ctype, csize = struct.unpack_from("<HH", data, off)
        if ctype == 0 or csize < 0x10:
            break
        chunks[ctype] = (off + 0x10, csize - 0x10)
        off += csize

    s_off, s_sz = chunks.get(3, (0, 0))
    body = data[body_off : body_off + body_size]
    entries = []
    for i in range(s_sz // 32):
        e = s_off + i * 32
        rel, loop_field = struct.unpack_from("<II", data, e)
        vol = data[e + 8]
        (pitch,) = struct.unpack_from("<h", data, e + 0xA)
        adsr1, adsr2 = struct.unpack_from("<HH", data, e + 0xC)
        nm = data[e + 0x10 : e + 0x20].rstrip(b"\x00").decode("ascii", "replace")
        if rel >= body_size:
            break
        entries.append(Sample(i, nm, rel, vol, pitch, adsr1, adsr2))
    # Each sample runs from its offset to the SPU end-frame flag (bit0 of the
    # second byte of a 16-byte frame). The driver decodes exactly this way
    # (SsdKeyonChannelProgram sets the voice start address and the SPU streams
    # to the end flag) — the whole body is one DMA blob and there is no
    # "next sample offset" concept. Carving by next-offset zero-lengths any
    # two entries that share a body offset (common: sample 0 and a trailing
    # placeholder both at offset 0), silencing a real instrument.
    for smp in entries:
        end = len(body)
        for base in range(smp.offset, len(body) - 15, 16):
            if body[base + 1] & 0x1:
                end = base + 16
                break
        smp.adpcm = body[smp.offset : end]
        smp.pcm = decode_spu_adpcm(smp.adpcm)
        smp.loop_start = _adpcm_loop_start(smp.adpcm)
    bank.samples = entries

    p_off, p_sz = chunks.get(4, (0, 0))
    if p_off and prog_count:
        offs = struct.unpack_from(f"<{prog_count}H", data, p_off)
        for pi, po in enumerate(offs):
            if not po or p_off + po + 0x60 > len(data):
                continue
            p = p_off + po
            splits = []
            for si in range(data[p + 3]):
                s = p + 0x60 + si * 16
                if s + 16 > len(data):
                    break
                a1, a2 = struct.unpack_from("<HH", data, s + 8)
                splits.append(
                    Split(data[s], data[s + 1], data[s + 2], data[s + 3],
                          data[s + 4], data[s + 5], a1, a2)
                )
            if splits:
                bank.programs[pi] = splits
    return bank


# ---------------------------------------------------------------------------
# SMD sequences
# ---------------------------------------------------------------------------

# (absolute_tick, kind, data...) intermediate events
#   note: (tick, "note", channel, key, velocity, gate_ticks)
#   ctrl: (tick, "tempo"|"program"|"volume"|"expression"|"pan"|"bend"|"transpose", channel, value)


@dataclass
class SeqTrack:
    channel: int
    events: list = field(default_factory=list)
    loop_tick: Optional[int] = None   # tick of the 0x91 Repeat marker
    stop_tick: Optional[int] = None   # tick of the 0x90 Stop (loop boundary)


@dataclass
class Sequence:
    name: str
    title: str
    composer: str
    tpqn: int
    tracks: list[SeqTrack] = field(default_factory=list)

    @property
    def end_tick(self) -> int:
        end = 0
        for t in self.tracks:
            for ev in t.events:
                tick = ev[0] + (ev[5] if ev[1] == "note" else 0)
                end = max(end, tick)
        return end

    @property
    def loop_tick(self) -> Optional[int]:
        ticks = [t.loop_tick for t in self.tracks if t.loop_tick is not None]
        return min(ticks) if ticks else None

    @property
    def loop_end(self) -> Optional[int]:
        """The driver's loop boundary: earliest Stop tick among looping tracks.

        The loop period is loop_end - loop_tick (NOT end_tick - loop_tick;
        end_tick is the last note-off, used only for the final ring-out tail)."""
        ticks = [t.stop_tick for t in self.tracks
                 if t.loop_tick is not None and t.stop_tick is not None]
        return min(ticks) if ticks else None

    def used_programs(self) -> set[int]:
        used = set()
        for t in self.tracks:
            for ev in t.events:
                if ev[1] == "program":
                    used.add(ev[3])
        return used


_FIXED_LEN = {  # operand byte counts (opcode excluded) for skipped opcodes
    0x87: 4, 0x8A: 2, 0x8C: 3, 0x8D: 2, 0x8E: 3,
    0xA0: 2, 0xA1: 3, 0xA6: 3, 0xAB: 3, 0xAE: 3,
    0xB1: 3, 0xB7: 2, 0xB8: 1, 0xB9: 1, 0xBA: 1, 0xBB: 1,
    0xC4: 1, 0xC5: 1, 0xC8: 3, 0xD5: 3, 0xD7: 1,
    0xD8: 3, 0xD9: 3, 0xDC: 0, 0xE3: 1, 0xE4: 3, 0xE5: 3,
    0xEB: 1, 0xEC: 3, 0xED: 3, 0xF0: 3, 0xF1: 3, 0xF2: 2,
    0xF6: 1, 0xF7: 1, 0xF8: 3, 0xFC: 2, 0xFD: 5, 0xFE: 1,
    0x9B: 1, 0x9E: 2, 0x9F: 2, 0xA2: 1, 0xA3: 1, 0xA4: 1, 0xA5: 1,
    0xAD: 1, 0xB2: 1, 0xB3: 1, 0xB4: 1, 0xB5: 1, 0xB6: 1, 0xBC: 1,
    0xBD: 1, 0xBE: 1, 0xCC: 3, 0xD3: 2,
}

MAX_TICKS = 10 * 60 * 1000  # runaway guard


def _walk_track(stream: bytes, start: int, end: int, channel: int) -> SeqTrack:
    """Interpret one track's event stream into absolute-tick events.

    The driver's master loop is 0x91 Repeat (SsdSeqRepeat stores the current
    stream pointer as the loop-return point) + 0x90 Stop (SsdSeqStop resumes
    from that point forever). We record the 0x91 tick as loop_tick and the
    0x90 tick as stop_tick, then stop the offline walk; render/MIDI use those
    for a seamless repeat with period stop_tick - loop_tick. Nested loops
    (0x98/0x99, count 0 = infinite) and forward Jumps (0x92, used as skips)
    are executed as the driver does.
    """
    trk = SeqTrack(channel)
    pos, tick = start, 0
    octave = 60          # semitone base, OctaveAbsolute(n) -> n*12
    gate = 0             # last note duration
    last_delta = 0
    transpose = 0
    repeat_tick: Optional[int] = None   # tick of the last 0x91 Repeat marker
    tick_of_pos: dict[int, int] = {}
    loop_stack: list[list] = []  # [count_left, pos, octave]
    pending_tie_key: Optional[int] = None

    def emit(kind, value):
        trk.events.append((tick, kind, channel, value))

    while pos < end and tick < MAX_TICKS:
        op = stream[pos]
        if op < 0x80:  # note-on, op = velocity
            velocity = op or 1
            b = stream[pos + 1]
            n_gate = b >> 6
            octave += ((b >> 4) & 3) * 12 - 12
            key = octave + (b & 0xF)
            pos += 2
            if n_gate:
                gate = 0
                for _ in range(n_gate):
                    gate = (gate << 8) | stream[pos]
                    pos += 1
            trk.events.append((tick, "note", channel, key + transpose,
                               velocity, max(gate, 1)))
            pending_tie_key = key + transpose
            continue

        pos += 1
        if op in (0x84, 0x85, 0x86):        # Delta1/2/3, little-endian wait
            n = op - 0x83
            v = int.from_bytes(stream[pos : pos + n], "little")
            pos += n
            tick += v
            last_delta = v
        elif op == 0x80:                    # wait = last gate
            tick += gate
            last_delta = gate
        elif op == 0x81:                    # wait = last delta
            tick += last_delta
        elif op in (0x82, 0x83):            # wait = delta/gate + signed byte
            base = last_delta if op == 0x82 else gate
            v = base + struct.unpack_from("b", stream, pos)[0]
            pos += 1
            tick += max(v, 0)
            last_delta = v
        elif op == 0x88:                    # Rest: u16 wait, keyoff
            v = int.from_bytes(stream[pos : pos + 2], "little")
            pos += 2
            tick += v
            pending_tie_key = None
        elif op == 0x89:                    # Tie: u16 wait, note sustains
            v = int.from_bytes(stream[pos : pos + 2], "little")
            pos += 2
            if pending_tie_key is not None:
                for i in range(len(trk.events) - 1, -1, -1):
                    ev = trk.events[i]
                    if ev[1] == "note" and ev[3] == pending_tie_key:
                        trk.events[i] = (ev[0], "note", ev[2], ev[3], ev[4],
                                         (tick + v) - ev[0])
                        break
            tick += v
        elif op == 0x90:                    # Stop: resume from the Repeat point
            if repeat_tick is not None:     # master loop -> record it, then stop
                trk.loop_tick = repeat_tick
                trk.stop_tick = tick
            break
        elif op == 0x91:                    # Repeat: mark the loop-return point
            repeat_tick = tick
            tick_of_pos[pos] = tick
        elif op == 0x92:                    # Jump, s16 relative -> master loop
            rel = int.from_bytes(stream[pos : pos + 2], "little")
            target = pos + ((rel - 1 + 0x8000) % 0x10000) - 0x8000
            pos += 2
            if target < pos and target in tick_of_pos:
                trk.loop_tick = tick_of_pos[target]
                break
            if target < pos:  # backward jump to unmarked pos: stop
                trk.loop_tick = 0
                break
            pos = target
        elif op == 0x93:                    # If(signal): never taken offline
            pos += 3
        elif op == 0x94:
            octave = stream[pos] * 12
            pos += 1
        elif op == 0x95:
            octave += struct.unpack_from("b", stream, pos)[0] * 12
            pos += 1
        elif op == 0x96:
            octave += 12
        elif op == 0x97:
            octave -= 12
        elif op == 0x98:                    # LoopTop, count byte
            count = stream[pos]
            pos += 1
            loop_stack.append([count - 1 if count else 1, pos, octave])
            if not count:
                trk.loop_tick = tick
        elif op == 0x99:                    # LoopEnd
            if loop_stack:
                fr = loop_stack[-1]
                fr[0] -= 1
                if fr[0] < 0:
                    loop_stack.pop()
                else:
                    pos, octave = fr[1], fr[2]
        elif op == 0x9A:                    # LoopEscape (on signal): ignore
            pass
        elif op == 0x9C:
            emit("tempo", stream[pos])
            pos += 1
        elif op == 0x9D:
            emit("tempo_rel", struct.unpack_from("b", stream, pos)[0])
            pos += 1
        elif op == 0xA8:                    # BankMSB
            pos += 1
        elif op == 0xA9:                    # BankLSB
            pos += 1
        elif op == 0xAA:                    # WaveChange (direct sample keyon)
            pos += 1
        elif op == 0xAC:
            emit("program", stream[pos])
            pos += 1
        elif op == 0xD0:
            transpose = struct.unpack_from("b", stream, pos)[0]
            pos += 1
        elif op == 0xD1:
            transpose += struct.unpack_from("b", stream, pos)[0]
            pos += 1
        elif op == 0xD2:                    # Tune: SsdSeqTune = operand*8 note16
            emit("bend", struct.unpack_from("b", stream, pos)[0] * 8)
            pos += 1
        elif op == 0xD4:                    # Bender: s16 note16 offset
            emit("bend", struct.unpack_from("<h", stream, pos)[0])
            pos += 2
        elif op == 0xDD:
            emit("bend", 0)
        elif op == 0xDF:                    # Expression
            emit("expression", stream[pos])
            pos += 1
        elif op == 0xE0:
            emit("volume", stream[pos])
            pos += 1
        elif op == 0xE1:
            emit("volume_rel", struct.unpack_from("b", stream, pos)[0])
            pos += 1
        elif op == 0xE2:                    # VolumeFade tgt,u16 time: jump to tgt
            emit("volume", stream[pos])
            pos += 3
        elif op == 0xE8:
            emit("pan", max(stream[pos] - 1, 0))
            pos += 1
        elif op == 0xE9:
            emit("pan_rel", struct.unpack_from("b", stream, pos)[0])
            pos += 1
        elif op == 0xEA:                    # PanpotMove tgt,u16 time
            emit("pan", max(stream[pos] - 1, 0))
            pos += 3
        elif op == 0xF9:                    # Label (SsdSeqLabel -> +1)
            pos += 1
        elif op == 0xFA:                    # SMPTETime (SsdSeqSMPTETime -> +3)
            pos += 3
        elif op == 0xFB:                    # SMPTEFrame (SsdSeqSMPTEFrame -> +2)
            pos += 2
        else:
            pos += _FIXED_LEN.get(op, 0)
    return trk


def parse_sequence(data: bytes, name: str = "") -> Optional[Sequence]:
    if data[:4] != b"smdm":
        return None
    tpqn = TPQN  # data[0x20] is not a timebase (see TPQN note)
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
    title = strs[0] if strs else name
    composer = strs[2] if len(strs) > 2 else ""

    seq = Sequence(name, title, composer, tpqn)
    off = 0x28
    while off + 4 <= len(data):
        ctype, csize = struct.unpack_from("<HH", data, off)
        if ctype == 0 or csize < 4:
            break
        if ctype == 3:
            channel = data[off + 6]
            seq.tracks.append(_walk_track(data, off + 8, off + csize, channel))
        off += csize
    return seq if seq.tracks else None


# ---------------------------------------------------------------------------
# Standard MIDI file export
# ---------------------------------------------------------------------------


def _vlq(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append(0x80 | (n & 0x7F))
        n >>= 7
    return bytes(reversed(out))


_MIDI_CHANNELS = [c for c in range(16) if c != 9]  # skip GM drum channel 9


def sequence_to_midi(seq: Sequence) -> bytes:
    """SMF type 1; one MIDI track per SMD track, meta on track 0.

    The SMD driver is a tracker with one independent voice record per track
    (no 16-way channel mux, no drum channel), so we allocate a distinct MIDI
    channel per note-bearing track, round-robin over {0..8,10..15} — never
    channel 9, where GM synths would silence pitched parts or turn them into
    drums. Sequences with more than 15 melodic tracks (the battle themes)
    exceed GM's channel budget and reuse channels; the per-track WAV render
    is the faithful output in that case.
    """
    chunks = []
    def _has_channel_msgs(trk):
        return any(ev[1] in ("note", "program", "volume", "volume_rel",
                             "expression", "pan", "pan_rel", "bend")
                   for ev in trk.events)
    ch_of, nxt = {}, 0
    for i, trk in enumerate(seq.tracks):
        if _has_channel_msgs(trk):
            ch_of[i] = _MIDI_CHANNELS[nxt % len(_MIDI_CHANNELS)]
            nxt += 1
    meta = bytearray()
    meta += b"\x00\xff\x03" + _vlq(len(seq.title)) + seq.title.encode("ascii", "replace")
    if seq.composer:
        t = seq.composer.encode("ascii", "replace")
        meta += b"\x00\xff\x02" + _vlq(len(t)) + t
    # tempo events live on their source tracks; collect onto track 0
    tempos = []
    bpm = 120.0
    for trk in seq.tracks:
        for ev in trk.events:
            if ev[1] == "tempo":
                tempos.append((ev[0], float(ev[3])))
            elif ev[1] == "tempo_rel":
                tempos.append((ev[0], None, float(ev[3])))
    tempos.sort(key=lambda e: e[0])
    last = 0
    for ev in tempos:
        if len(ev) == 3:
            bpm += ev[2]
        else:
            bpm = ev[1]
        bpm = max(bpm, 1.0)
        meta += _vlq(ev[0] - last) + b"\xff\x51\x03" + int(60_000_000 / bpm).to_bytes(3, "big")
        last = ev[0]
    meta += b"\x00\xff\x2f\x00"
    chunks.append(bytes(meta))

    for i, trk in enumerate(seq.tracks):
        ch = ch_of.get(i, 0)
        # (tick, order, midi bytes); order keeps note-off before note-on at same tick
        msgs = []
        vol, pan = 100, 64
        for ev in trk.events:
            tick, kind = ev[0], ev[1]
            if kind == "note":
                _, _, _, key, vel, gate = ev
                if 0 <= key <= 127:
                    msgs.append((tick, 1, bytes((0x90 | ch, key, max(1, min(vel, 127))))))
                    msgs.append((tick + gate, 0, bytes((0x80 | ch, key, 0))))
            elif kind == "program":
                msgs.append((tick, 0, bytes((0xC0 | ch, ev[3] & 0x7F))))
            elif kind == "volume":
                vol = max(0, min(ev[3], 127))
                msgs.append((tick, 0, bytes((0xB0 | ch, 7, vol))))
            elif kind == "volume_rel":
                vol = max(0, min(vol + ev[3], 127))
                msgs.append((tick, 0, bytes((0xB0 | ch, 7, vol))))
            elif kind == "expression":
                msgs.append((tick, 0, bytes((0xB0 | ch, 11, min(ev[3], 127)))))
            elif kind == "pan":
                pan = max(0, min(ev[3], 127))
                msgs.append((tick, 0, bytes((0xB0 | ch, 10, pan))))
            elif kind == "pan_rel":
                pan = max(0, min(pan + ev[3], 127))
                msgs.append((tick, 0, bytes((0xB0 | ch, 10, pan))))
            elif kind == "bend":
                # note16 -> pitch wheel with default +/-2 semitone range
                bend = max(-8192, min(8191, int(ev[3] / 512.0 * 8192)))
                v = bend + 8192
                msgs.append((tick, 0, bytes((0xE0 | ch, v & 0x7F, v >> 7))))
        msgs.sort(key=lambda m: (m[0], m[1]))
        buf = bytearray()
        last = 0
        for tick, _, msg in msgs:
            buf += _vlq(tick - last) + msg
            last = tick
        buf += b"\x00\xff\x2f\x00"
        chunks.append(bytes(buf))

    out = bytearray()
    out += b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), seq.tpqn)
    for c in chunks:
        out += b"MTrk" + struct.pack(">I", len(c)) + c
    return bytes(out)


# ---------------------------------------------------------------------------
# SPU2 envelope (shared by the renderer and the SF2 approximation)
# ---------------------------------------------------------------------------


def _spu_env_step(shift: int, step_val: int) -> tuple[int, int]:
    """(step, ticks_between_steps) per nocash: step applied every cycle group."""
    step = step_val << max(0, 11 - shift)
    ticks = 1 << max(0, shift - 11)
    return step, ticks


def spu_attack_seconds(adsr1: int) -> float:
    exp_mode = bool(adsr1 & 0x8000)
    shift = (adsr1 >> 10) & 0x1F
    step_sel = (adsr1 >> 8) & 3
    step, ticks = _spu_env_step(shift, 7 - step_sel)
    if step <= 0:
        return 10.0
    level, t = 0, 0
    while level < 0x7FFF and t < SPU_RATE * 10:
        s = step
        if exp_mode and level >= 0x6000:
            s = max(step >> 2, 1)
        level += s
        t += ticks
    return t / SPU_RATE


def spu_decay_seconds(adsr1: int) -> tuple[float, float]:
    """(decay_seconds_to_sustain, sustain_level_fraction)."""
    shift = (adsr1 >> 4) & 0xF
    sl = ((adsr1 & 0xF) + 1) / 16.0
    step, ticks = _spu_env_step(shift, 8)
    level, t = 0x7FFF, 0
    target = 0x7FFF * sl
    while level > target and t < SPU_RATE * 30:
        dec = max((step * level) >> 15, 1)
        level -= dec
        t += ticks
    return t / SPU_RATE, sl


def spu_release_seconds(adsr2: int) -> float:
    exp_mode = bool(adsr2 & 0x20)
    shift = adsr2 & 0x1F
    step, ticks = _spu_env_step(shift, 8)
    level, t = 0x7FFF, 0
    while level > 0x100 and t < SPU_RATE * 30:
        dec = max((step * level) >> 15, 1) if exp_mode else step
        level -= dec
        t += ticks
    return t / SPU_RATE


def spu_sustain_rate(adsr2: int) -> float:
    """Sustain-phase level decay per second (fraction/s), 0 = hold."""
    if not (adsr2 & 0x4000):  # increasing sustain: treat as hold
        return 0.0
    exp_mode = bool(adsr2 & 0x8000)
    shift = (adsr2 >> 8) & 0x1F
    step_sel = (adsr2 >> 6) & 3
    step, ticks = _spu_env_step(shift, 8 - step_sel)
    if step <= 0:
        return 0.0
    if exp_mode:
        # exponential: fraction decayed per tick group ~ step/0x8000
        return (step / 0x8000) * (SPU_RATE / ticks)
    return (step / 0x7FFF) * (SPU_RATE / ticks)


# ---------------------------------------------------------------------------
# SoundFont 2 writer
# ---------------------------------------------------------------------------


def _timecents(seconds: float) -> int:
    seconds = max(seconds, 0.001)
    return max(-12000, min(8000, round(1200 * math.log2(seconds))))


def bank_to_sf2(bank: Bank, name: str) -> bytes:
    """Emit a SoundFont 2.01 file: one preset+instrument per program."""
    used = sorted({s.sample for splits in bank.programs.values() for s in splits})
    smap = {}  # sample index -> (sf2 sample id, start, end, loop_s, loop_e, rate)
    pcm_all = bytearray()
    shdr = bytearray()
    for sid, si in enumerate(used):
        smp = bank.samples[si]
        start = len(pcm_all) // 2
        pcm_all += smp.pcm
        end = len(pcm_all) // 2
        pcm_all += b"\x00" * 92  # 46-sample guard
        if smp.loop_start >= 0:
            ls, le = start + smp.loop_start, end
        else:
            ls, le = start, end
        smap[si] = (sid, start, end, ls, le, smp.native_rate)
    for si in used:
        sid, start, end, ls, le, rate = smap[si]
        smp = bank.samples[si]
        nm = (smp.name or f"smp{si}").encode("ascii", "replace")[:19]
        shdr += nm.ljust(20, b"\x00")
        shdr += struct.pack("<IIIIIBbHH", start, end, ls, le, rate, 60, 0, 0, 1)
    shdr += b"EOS".ljust(20, b"\x00") + struct.pack("<IIIIIBbHH", 0, 0, 0, 0, 0, 0, 0, 0, 0)

    def gen(op, amount):
        return struct.pack("<Hh", op, amount)

    def genu(op, lo, hi):
        return struct.pack("<HBB", op, lo, hi)

    inst = bytearray()
    ibag = bytearray()
    igen = bytearray()
    phdr = bytearray()
    pbag = bytearray()
    pgen = bytearray()
    ibag_n = igen_n = pbag_n = pgen_n = 0

    progs = sorted(bank.programs)
    for pi in progs:
        inst += f"prog{pi:03d}".encode().ljust(20, b"\x00") + struct.pack("<H", ibag_n)
        for sp in bank.programs[pi]:
            smp = bank.samples[sp.sample]
            sid, *_ = smap[sp.sample]
            ibag += struct.pack("<HH", igen_n, 0)
            ibag_n += 1
            g = bytearray()
            g += genu(43, max(0, min(sp.key_lo, 127)), max(0, min(sp.key_hi, 127)))
            # volume: split vol * sample vol. The driver squares the combined
            # voice volume (SsdDeviceVoiceEvent @0x65fc), so the static split
            # level belongs in the squared domain -> *2 on the centibel figure.
            lin = max((sp.volume / 127.0) * (smp.volume / 127.0), 1e-4)
            g += gen(48, min(1440, round(-200 * math.log10(lin) * 2)))
            if sp.pan != 64:
                g += gen(17, round((sp.pan - 64) / 64 * 500))
            att = spu_attack_seconds(sp.adsr1)
            dec, sl = spu_decay_seconds(sp.adsr1)
            rel = spu_release_seconds(sp.adsr2)
            g += gen(34, _timecents(att))
            if sl < 1.0:
                g += gen(36, _timecents(dec))
                # sustain level (gen 37) is applied linearly upstream of the
                # voice-volume square, so it must NOT be doubled (unlike gen 48)
                g += gen(37, min(1440, round(-200 * math.log10(max(sl, 1e-4)))))
            g += gen(38, _timecents(rel))
            g += gen(58, max(0, min(sp.root, 127)))          # overridingRootKey
            if smp.loop_start >= 0:
                g += gen(54, 1)                              # sampleModes: loop
            # leftover fine tune beyond the integer native rate
            exact = SPU_RATE * 2.0 ** (smp.base_pitch / 256.0 / 12.0)
            cents = round(1200 * math.log2(exact / smp.native_rate)) if smp.native_rate else 0
            if cents:
                g += gen(52, max(-99, min(99, cents)))
            g += gen(53, sid)                                # sampleID (last)
            igen += g
            igen_n += len(g) // 4
    inst += b"EOI".ljust(20, b"\x00") + struct.pack("<H", ibag_n)
    ibag += struct.pack("<HH", igen_n, 0)

    for n, pi in enumerate(progs):
        phdr += f"prog{pi:03d}".encode().ljust(20, b"\x00")
        phdr += struct.pack("<HHHIII", pi, 0, pbag_n, 0, 0, 0)
        pbag += struct.pack("<HH", pgen_n, 0)
        pbag_n += 1
        pgen += gen(41, n)  # instrument index
        pgen_n += 1
    phdr += b"EOP".ljust(20, b"\x00") + struct.pack("<HHHIII", 0, 0, pbag_n, 0, 0, 0)
    pbag += struct.pack("<HH", pgen_n, 0)

    def sub(tag, payload):
        if len(payload) & 1:
            payload += b"\x00"
        return tag + struct.pack("<I", len(payload)) + payload

    info = sub(b"ifil", struct.pack("<HH", 2, 1))
    info += sub(b"isng", b"EMU8000\x00")
    info += sub(b"INAM", name.encode("ascii", "replace") + b"\x00")
    info += sub(b"IENG", b"Yasunori Mitsuda / PROCYON STUDIO\x00")
    info += sub(b"ICMT", b"Converted from Xenosaga Episode I SWD bank by ssd.py\x00")
    info_list = b"LIST" + struct.pack("<I", 4 + len(info)) + b"INFO" + info
    sdta = sub(b"smpl", bytes(pcm_all))
    sdta_list = b"LIST" + struct.pack("<I", 4 + len(sdta)) + b"sdta" + sdta
    pdta = (sub(b"phdr", bytes(phdr)) + sub(b"pbag", bytes(pbag))
            + sub(b"pmod", b"\x00" * 10) + sub(b"pgen", bytes(pgen) + b"\x00" * 4)
            + sub(b"inst", bytes(inst)) + sub(b"ibag", bytes(ibag))
            + sub(b"imod", b"\x00" * 10) + sub(b"igen", bytes(igen) + b"\x00" * 4)
            + sub(b"shdr", bytes(shdr)))
    pdta_list = b"LIST" + struct.pack("<I", 4 + len(pdta)) + b"pdta" + pdta
    body = b"sfbk" + info_list + sdta_list + pdta_list
    return b"RIFF" + struct.pack("<I", len(body)) + body
