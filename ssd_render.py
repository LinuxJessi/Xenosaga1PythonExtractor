"""ssd_render.py — render decoded SMD sequences against SWD banks to WAV.

A small SPU2-faithful sampler: per-voice pitch from the driver's exact
equal-temperament model, SPU ADSR envelopes (piecewise-exponential
approximation of the register semantics), linear-interp resampling with
stream-flag loop points. Requires numpy.

Also the batch exporter:
    python ssd_render.py <dump_dir> <out_dir>
writes per music sequence: .mid + .wav (+ .flac if ffmpeg is found) and
per referenced bank: .sf2 — everything generated locally from the user's
own extracted disc data.
"""
from __future__ import annotations

import math
import struct
import sys
import wave
from pathlib import Path

import numpy as np

import ssd
from ssd import Bank, Sequence, SPU_RATE

LOOP_FADE_SECONDS = 6.0
LOOP_TAIL_SECONDS = 2.0


def _tempo_map(seq: Sequence):
    """[(tick, seconds_at_tick, sec_per_tick)] sorted, from all tracks."""
    events = []
    for trk in seq.tracks:
        for ev in trk.events:
            if ev[1] == "tempo":
                events.append((ev[0], float(ev[3])))
            elif ev[1] == "tempo_rel":
                events.append((ev[0], None, float(ev[3])))
    events.sort(key=lambda e: e[0])
    bpm = 120.0
    out = []
    t_sec, last_tick = 0.0, 0
    spt = 60.0 / (bpm * seq.tpqn)
    for ev in events:
        t_sec += (ev[0] - last_tick) * spt
        last_tick = ev[0]
        bpm = max(1.0, bpm + ev[2] if len(ev) == 3 else ev[1])
        spt = 60.0 / (bpm * seq.tpqn)
        out.append((last_tick, t_sec, spt))
    if not out or out[0][0] > 0:
        out.insert(0, (0, 0.0, 60.0 / (120.0 * seq.tpqn)))
    return out


def _tick_to_sec(tmap, tick: int) -> float:
    base_tick, base_sec, spt = tmap[0]
    for t, s, r in tmap:
        if t > tick:
            break
        base_tick, base_sec, spt = t, s, r
    return base_sec + (tick - base_tick) * spt


def _envelope(n: int, attack: float, decay: float, sustain: float,
              sus_rate: float, gate_n: int, release: float) -> np.ndarray:
    """Amplitude envelope over n samples; keyoff at gate_n."""
    t = np.arange(n, dtype=np.float32) / SPU_RATE
    env = np.ones(n, dtype=np.float32)
    a_n = max(int(attack * SPU_RATE), 1)
    ramp = np.linspace(0.0, 1.0, min(a_n, n), dtype=np.float32)
    env[: len(ramp)] = ramp
    # decay to sustain, then sustain decay
    if sustain < 1.0 and a_n < n:
        dt = t[a_n:] - t[a_n]
        tau = max(decay, 1e-3) / 5.0
        env[a_n:] *= sustain + (1.0 - sustain) * np.exp(-dt / tau, dtype=np.float32)
    if sus_rate > 0 and a_n < n:
        dt = t[a_n:] - t[a_n]
        env[a_n:] *= np.exp(-dt * sus_rate, dtype=np.float32)
    # release
    if gate_n < n:
        level = env[max(gate_n - 1, 0)]
        dt = t[gate_n:] - t[gate_n]
        tau = max(release, 1e-3) / 5.0
        env[gate_n:] = level * np.exp(-dt / tau, dtype=np.float32)
    return env


def render_sequence(seq: Sequence, bank: Bank, verbose: bool = False) -> np.ndarray:
    tmap = _tempo_map(seq)
    loop_tick = seq.loop_tick
    loop_end = seq.loop_end          # driver Stop tick = loop boundary
    end_tick = seq.end_tick          # last note-off, for the final ring-out
    looped = loop_tick is not None and loop_end is not None and loop_end > loop_tick
    if looped:
        # play the intro+body once to loop_end, then replay [loop_tick,loop_end)
        # once more shifted by the loop period, then fade. Period is
        # loop_end-loop_tick (NOT end_tick-loop_tick).
        period = loop_end - loop_tick
        passes = [(0, loop_end, 0), (loop_tick, loop_end, period)]
        total_ticks = loop_end + period
    else:
        passes = [(0, end_tick, 0)]
        total_ticks = end_tick

    dur = _tick_to_sec(tmap, total_ticks) + LOOP_TAIL_SECONDS + 3.0
    n_total = int(dur * SPU_RATE) + 1
    mix = np.zeros((n_total, 2), dtype=np.float32)

    # decoded PCM cache as float arrays
    pcm_cache: dict[int, np.ndarray] = {}

    def pcm_for(si: int) -> np.ndarray:
        if si not in pcm_cache:
            raw = bank.samples[si].pcm
            pcm_cache[si] = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        return pcm_cache[si]

    n_voices = 0
    for trk in seq.tracks:
        program = 0
        vol, expr, pan, bend = 100, 127, 64, 0
        # controller state must be tracked through time; build a timeline
        ctrl = sorted((ev for ev in trk.events if ev[1] != "note"),
                      key=lambda e: e[0])
        notes = [ev for ev in trk.events if ev[1] == "note"]
        bend_events = sorted((ev[0], ev[3]) for ev in trk.events if ev[1] == "bend")
        for pass_start, pass_end, tick_shift in passes:
            ci = 0
            program, vol, expr, pan, bend = 0, 100, 127, 64, 0
            for note in notes:
                tick, _, _, key, vel, gate = note
                if not (pass_start <= tick < pass_end):
                    continue
                while ci < len(ctrl) and ctrl[ci][0] <= tick:
                    k, v = ctrl[ci][1], ctrl[ci][3]
                    if k == "program":
                        program = v
                    elif k == "volume":
                        vol = max(0, min(v, 127))
                    elif k == "volume_rel":
                        vol = max(0, min(vol + v, 127))
                    elif k == "expression":
                        expr = max(0, min(v, 127))
                    elif k == "pan":
                        pan = max(0, min(v, 127))
                    elif k == "pan_rel":
                        pan = max(0, min(pan + v, 127))
                    elif k == "bend":
                        bend = v
                    ci += 1
                sp = bank.split_for(program, key)
                if sp is None or sp.sample >= len(bank.samples):
                    continue
                smp = bank.samples[sp.sample]
                pcm = pcm_for(sp.sample)
                if not len(pcm):
                    continue
                base_note16 = smp.base_pitch + ((key + 60 - sp.root) << 8)
                onset_ratio = 2.0 ** (((base_note16 + bend) / 256.0 - 60.0) / 12.0)
                if onset_ratio <= 0 or onset_ratio > 16:
                    continue
                t0 = _tick_to_sec(tmap, tick + tick_shift)
                t1 = _tick_to_sec(tmap, tick + gate + tick_shift)
                gate_n = max(int((t1 - t0) * SPU_RATE), 1)
                release = ssd.spu_release_seconds(sp.adsr2)
                n = gate_n + int(min(release * 3, 10.0) * SPU_RATE)
                start_i = int(t0 * SPU_RATE)
                n = min(n, n_total - start_i)
                if n <= 1:
                    continue
                # read cursor: constant-rate unless the note is bent mid-flight,
                # in which case integrate the per-tick pitch (the driver
                # recomputes SsdNoteToPitch every tick from voice[0x50]).
                changes = [(bt, bv) for (bt, bv) in bend_events
                           if tick < bt < tick + gate]
                if changes:
                    seg = [(0, bend)]
                    for bt, bv in changes:
                        si = int((_tick_to_sec(tmap, bt + tick_shift) - t0) * SPU_RATE)
                        if 0 < si < n:
                            seg.append((si, bv))
                    seg.sort()
                    ratio_arr = np.empty(n, dtype=np.float64)
                    for idx, (si, bv) in enumerate(seg):
                        nxt = seg[idx + 1][0] if idx + 1 < len(seg) else n
                        ratio_arr[si:nxt] = min(
                            2.0 ** (((base_note16 + bv) / 256.0 - 60.0) / 12.0), 16.0)
                    pos = np.empty(n, dtype=np.float64)
                    pos[0] = 0.0
                    np.cumsum(ratio_arr[:-1], out=pos[1:])
                else:
                    pos = np.arange(n, dtype=np.float64) * onset_ratio
                ls = smp.loop_start
                if ls >= 0 and ls < len(pcm):
                    span = len(pcm) - ls
                    over = pos >= ls
                    pos[over] = ls + np.mod(pos[over] - ls, span)
                else:
                    pos = np.minimum(pos, len(pcm) - 1.001)
                i0 = pos.astype(np.int64)
                frac = (pos - i0).astype(np.float32)
                i1 = np.minimum(i0 + 1, len(pcm) - 1)
                data = pcm[i0] * (1.0 - frac) + pcm[i1] * frac
                if ls < 0:
                    data[pos >= len(pcm) - 1] = 0.0   # one-shot: silence past end
                att = ssd.spu_attack_seconds(sp.adsr1)
                dec, sl = ssd.spu_decay_seconds(sp.adsr1)
                sus_rate = ssd.spu_sustain_rate(sp.adsr2)
                env = _envelope(n, att, dec, sl, sus_rate, gate_n, release)
                # driver squares the combined voice volume (SsdDeviceVoiceEvent
                # @0x65fc); ADSR (env) is applied upstream, so square only gain.
                gain = (vel / 127.0) * (vol / 127.0) * (expr / 127.0) \
                    * (sp.volume / 127.0) * (smp.volume / 127.0)
                # constant-power pan (center -3 dB), no boost/clamp
                theta = max(0.0, min(1.0, pan / 127.0)) * (math.pi / 2)
                l_g, r_g = math.cos(theta), math.sin(theta)
                sig = data * env * (gain * gain)
                mix[start_i : start_i + n, 0] += sig * l_g
                mix[start_i : start_i + n, 1] += sig * r_g
                n_voices += 1
    if verbose:
        print(f"    {n_voices} voices rendered")

    if looped:
        fade_start = _tick_to_sec(tmap, total_ticks) - LOOP_FADE_SECONDS
        fs = max(int(fade_start * SPU_RATE), 0)
        fade_n = min(int(LOOP_FADE_SECONDS * SPU_RATE), n_total - fs)
        if fade_n > 0:
            mix[fs : fs + fade_n] *= np.linspace(1.0, 0.0, fade_n,
                                                 dtype=np.float32)[:, None]
            mix[fs + fade_n :] = 0.0
    peak = float(np.max(np.abs(mix))) or 1.0
    mix *= 0.95 / peak                    # normalize each track to -0.4 dBFS
    # trim trailing silence
    nz = np.nonzero(np.max(np.abs(mix), axis=1) > 1e-4)[0]
    if len(nz):
        mix = mix[: min(int(nz[-1]) + SPU_RATE // 2, len(mix))]
    return mix


def write_wav(path: Path, mix: np.ndarray) -> None:
    pcm = (np.clip(mix, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SPU_RATE)
        w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# batch export
# ---------------------------------------------------------------------------

MUSIC_MIN_SIZE = 5000  # below this SMDs are ambience stubs


def _collect_banks(dump: Path) -> list[tuple[Path, Bank]]:
    banks = []
    for p in sorted(dump.rglob("*.SWD")) + sorted(dump.rglob("*.swd")) \
            + sorted(dump.rglob("*.SED")) + sorted(dump.rglob("*.sed")):
        bank = ssd.parse_bank(p.read_bytes(), p.stem)
        if bank and bank.programs:
            banks.append((p, bank))
    return banks


def _match_bank(seq_path: Path, seq: Sequence,
                banks: list[tuple[Path, Bank]]) -> Bank | None:
    used = seq.used_programs() or {0}
    stem = seq_path.stem.lower()
    for p, b in banks:  # exact stem match anywhere beats everything
        if p.stem.lower() == stem:
            return b
    sibling = seq_path.with_suffix(".SWD")
    for p, b in banks:
        if p == sibling:
            return b
    best, best_score = None, -1.0
    for p, b in banks:
        cover = len(used & set(b.programs)) / len(used)
        score = cover + (0.5 if p.parent == seq_path.parent else 0.0)
        if cover > 0 and score > best_score:
            best, best_score = b, score
    return best


def export_all(dump: Path, out: Path, ffmpeg: str | None = None) -> None:
    out.mkdir(parents=True, exist_ok=True)
    banks = _collect_banks(dump)
    print(f"{len(banks)} wave banks parsed")
    for p in sorted(dump.rglob("*.SMD")) + sorted(dump.rglob("*.smd")):
        data = p.read_bytes()
        if len(data) < MUSIC_MIN_SIZE:
            continue
        seq = ssd.parse_sequence(data, p.stem)
        if seq is None:
            continue
        bank = _match_bank(p, seq, banks)
        if bank is None:
            print(f"  {p.stem}: no matching bank, skipped")
            continue
        label = f"{p.stem} ({seq.title!r}, bank {bank.name})"
        print(f"  {label}")
        (out / f"{p.stem}.mid").write_bytes(ssd.sequence_to_midi(seq))
        # name the SF2 after the sequence so each MIDI is self-contained
        # (jingles reuse the battle bank, so a bank-named SF2 wouldn't match)
        (out / f"{p.stem}.sf2").write_bytes(ssd.bank_to_sf2(bank, p.stem))
        mix = render_sequence(seq, bank, verbose=True)
        wav = out / f"{p.stem}.wav"
        write_wav(wav, mix)
        if ffmpeg:
            import subprocess
            flac = out / f"{p.stem}.flac"
            subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-i", str(wav),
                            "-metadata", f"title={seq.title}",
                            "-metadata", f"artist={seq.composer}",
                            "-metadata", "album=Xenosaga Episode I (sequenced)",
                            str(flac)], check=False)


if __name__ == "__main__":
    dump_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    ff = None
    try:
        from browse import detect_ffmpeg
        ff = detect_ffmpeg()
    except Exception:
        pass
    export_all(dump_dir, out_dir, ff)
