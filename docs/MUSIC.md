# The music of Xenosaga Episode I — how it works, and what the disc told us

Xenosaga Episode I does not stream its music. The orchestral score by
Yasunori Mitsuda is *sequenced*: the disc carries note data (`.SMD`) and
instrument banks of sampled orchestra (`.SWD`), and a driver on the PS2's
sound processor plays them live, the way a MIDI file plays against a
SoundFont. This kit turns those files back into MIDI, SoundFont 2 and
rendered WAV/FLAC — and because Monolith shipped the driver with its
debug symbols intact, everything below is read from the game's own code,
not guessed.

```sh
python cli.py extract --iso GAME.iso --out OUTDIR         # once
python cli.py music-export --dump OUTDIR/dump --out OUTDIR/browse/music
python cli.py browse --out OUTDIR --kinds banks,battle_audio,audio
```

| Output | What it is |
|---|---|
| `browse/music/*.mid` | each sequence as a standard MIDI file (96 PPQN, tempo map, loop markers) |
| `browse/music/*.sf2` | each bank as a SoundFont 2 — the game's own orchestra samples, ADSR and key splits |
| `browse/music/*.wav` / `.flac` | rendered by the kit's SPU2-faithful sampler (needs numpy; FLAC needs ffmpeg) |
| `browse/soundbanks/` | every bank's samples as WAV + `smd_catalog.csv` (title, composer, notes per sequence) |
| `browse/battle_audio/` | the battle voice / sound-effect banks, 1,502 sample WAVs |
| `browse/audio/` | the streamed stereo voice/ambience/cutscene streams as WAV |

The MIDI, SoundFonts and renders are derived from the disc's own data.
They are for your own use; regenerate them from your own copy of the game
rather than sharing them.

## Three audio systems on one disc

1. **Sequenced BGM** — 115 `.SMD` sequences and 289 `.SWD` banks under
   `sound/smd`, `sound/sed` and the battle folder `yamamoto/snd`. Played
   by `SSD.IRX`, Procyon Studio's sound driver running on the IOP.
2. **Streamed audio** — 93 `.vds` files (92 under `chain1/sound/vda`,
   plus `yajima/gameover.vds`): headerless stereo SPU ADPCM at 48 kHz.
   Spoken lines, ambience beds, and the big pre-mixed music+voice
   tracks of in-engine cutscenes (up to 20 MB each).
3. **Movie audio** — inside the `.pss` cutscene movies, a Sony ADS
   stream tucked into MPEG private stream 1. The kit demuxes it itself
   because ffmpeg sees phantom "mp2, 0 channels" streams there.

Only the first is *music* in the composer's sense. Of the 115 sequences,
ten are real pieces: **Battle1** (two copies), **LastBattle**,
**Escape!** (three copies), **U.M.N. Mode** (two copies) and
**Jingle2** (two copies). The other 105 are 196-byte ambience stubs.
That is the famous quiet of the field maps, visible in the data.

## How a sequence plays

An `.SMD` is a stack of tracks, each an event stream the driver walks
with a dispatch table (`SsdSeqFuncTrap`) of 128 opcode handlers.

* **Note-ons are tiny.** Any byte below 0x80 is a note-on and the byte
  *is the velocity*. The next byte packs three things: how many
  duration bytes follow (or "reuse the last duration"), an octave step
  of −1 to +2 relative to a running octave, and the semitone. A melody
  costs about two bytes per note. This is why a full battle theme is
  17 KB.
* **The clock is fixed at 96 ticks per quarter note.** The driver arms
  a 2 ms interrupt; each firing subtracts a rate from a 16.16
  fixed-point accumulator and advances one tick on underflow. The rate
  is `tempo × 53687 >> 8`, which works out to exactly 1.6 × tempo ticks
  per second — tempo × 96 / 60. So the tempo opcode's operand is literal
  BPM.
* **Looping is two opcodes.** `Repeat` (0x91) bookmarks the stream
  position; `Stop` (0x90) jumps back to it forever. All 115 sequences
  loop this way; the loop length is simply the distance between them.
  The opcode named `Jump` is a forward skip, not the loop.
* Everything else is what you would expect of a 2002 sequencer: rests
  and ties, nested count loops with a 12-byte stack frame per track,
  program change, absolute and relative volume/pan/expression, pitch
  bend and fine tune, key transpose, and a set of SMPTE/label opcodes
  the driver ignores at runtime.

## How the instruments sound

An `.SWD` bank is a sample table plus programs.

* **Samples** are SPU ADPCM, the PS2's native 16-byte-frame codec, with
  loop points carried in the stream's own frame flags. Each sample
  stores a base pitch in 1/256-semitone units relative to key 60 at
  48 kHz, so its native rate is `48000 × 2^(pitch/3072)`. Retail banks
  decode to 22.05, 32, 44.1 and 48 kHz — plus deliberate detunes of a
  few cents (32,007 Hz-style rates are intentional, not errors).
* **Programs** map key ranges to samples (up to 16 splits each) with a
  root key, volume, pan and two raw SPU2 ADSR registers. The SoundFont
  export carries all of it.
* **Pitch is exact equal temperament.** The driver's pitch table
  (`SsdAllPitchTable`) matches `0x1000 × 2^((note − 60)/12)` to the
  register value, so the SoundFont plays in tune with the game.
* **The driver squares the volume.** The combined velocity × channel ×
  expression × split × sample level is squared before it reaches the
  SPU volume register (`SsdDeviceVoiceEvent`). A renderer that applies
  it linearly sounds flat and crowded; the kit squares it.

## How we cracked it

`SSD.IRX` ships **unstripped**. Every sequencer opcode handler has its
symbol name, the dispatch table and the operand-length table are in
`.data`, and the pitch and timer maths are a few dozen MIPS instructions
each. The kit's `ssd.py` is a transcription of that driver's behaviour,
not a statistical guess about the file format. `extract --code` drops
the driver into `browse/code/IOP/` if you want to read it yourself.

Two lessons cost real time and are worth passing on:

* **The header byte that is not a timebase.** Byte 0x20 of every `.SMD`
  holds 100, 120, 123 or 127 — plausible PPQN values — and the first
  tooling used it as one. Battle themes came out 25 % fast. U.M.N. Mode,
  whose byte is 100, was only 4 % off, which masked the bug for weeks.
  The driver never reads that byte; the clock is fixed.
* **Advance by what the handler returns.** The operand-length table
  disagrees with a few handlers about how many bytes they consume; the
  dispatcher trusts the handler. Trusting the table injected two phantom
  notes at tick 0 of every track (`SMPTEOffset` is 5 bytes, not 1).

The conversion was then put through a six-dimension adversarial audit
(timing, pitch, envelopes, loop structure, bank carving, levels) — 21
findings, none refuted after checking against the driver — and an
independent re-render through FluidSynth agreed with the kit's sampler.

## Fun facts

* **The music credits itself.** Every real piece embeds its metadata in
  ASCII: title, game, `Yasunori Mitsuda`, `PROCYON STUDIO`, and a notes
  field. `BATTLE1.SMD`'s note reads **"IBENT BATTLE"** — a romaji slip
  for "EVENT BATTLE"; `BATTLE2.SMD` spells it correctly. Both battle
  themes are titled "Battle1", both jingles "Jingle2", and the game name
  alternates between `XENOSAGA` and `Xenosaga` from file to file. Nobody
  updated the metadata, and some ambience stubs carry the credit line
  too.
* **The orchestra is in the bank names.** The Battle1 bank has 60
  samples and 27 programs named like a session library: `TimpC#2 FF`,
  `TimpG1 FF`, `SnareFF / SnareMF / SnarePP`, `F.HornSTCD3`,
  `F.HornCRSG#3`, `TrbFFC4LP.vag`, `CelloBassSTCC#3`, `ViolinSTCB5`,
  `Anvil`, `RollCymbal`. The suffixes are dynamics and articulations
  (FF/MF/PP, STC staccato, CRS crescendo, LP looped) — multisampled
  orchestra with dynamic layers, played back live on a 2002 console.
* **Sixty seconds of orchestra in 17 KB.** The note data for Battle1 is
  17,168 bytes; LastBattle, the longest piece on the disc, is 23,960
  bytes for a five-minute loop. The samples are where the megabytes go.
* **Rendered lengths** (one loop plus a fade): Battle1/Battle2 162.7 s,
  LastBattle 305.5 s, Escape! 175.2 s, U.M.N. Mode 139.0 s, the jingles
  34.9 s.
* **The voice streams hid their layout in plain sight.** The `.vds`
  streams have no header. Decoded as mono they sounded pitch-correct but
  half-speed, "echoey", and tapped at block boundaries — and no sample
  rate fixed it, because the problem was a stereo interleave every 0x400
  bytes. The confirmation came from the movies: their embedded ADS
  header spells out `48000 Hz, 2 channels, interleave 0x400` — the
  game's own spec for the format we had derived by correlation.
* **The battle voices are 535 little banks.** `yamamoto/snd/sed` holds
  per-character attack, boost, special, bed and weapon banks (`km_`,
  `so_`, `jr_`, `mo_`, `cs_`, `z8_` — KOS-MOS, Shion, Jr., MOMO, chaos
  and Ziggy, "Ziggurat 8"), 125 boss banks and enemy-tech banks. 238
  samples ship as stereo halves named `.aif.L` / `.aif.R`, straight from
  the studio's file names.
* **Not every "voice" stream is a voice.** The 92 streams under
  `sound/vda` mix spoken lines with ambience and machinery; a 12-second
  engine drone sits next to a 35-second monologue. A voiced-frame
  measure (fraction of frames with a strong pitch period) separates them
  reliably.
* **One stream lives alone.** `yajima/gameover.vds` is the only streamed
  audio in chain 0 — the game-over music, parked in a developer's
  personal folder.

## Still open

* **Reverb.** The sequences use the SPU2 reverb send, but the reverb
  depth is per-scene engine state, not in the files, so renders are dry.
* **Envelopes are an approximation.** The SoundFont carries the raw
  SPU2 ADSR registers; the kit's sampler models them piecewise-
  exponentially. Close, not bit-exact.
* The kit's sampler is deliberately simple (linear-interpolation
  resampling, no per-voice filtering). For the most faithful listen,
  play the MIDI against the SoundFont in a real synth and compare.

## Where the details are

* `docs/FORMATS.md` § "Sequenced BGM" — the byte-level spec with the
  driver offsets it was read from, and the streamed-audio and movie
  sections.
* `ssd.py` (SMD → MIDI, SWD → SoundFont 2) and `ssd_render.py` (the
  sampler and batch exporter).
* `docs/FINDS.md` and `docs/HISTORY.md` — the metadata quirks and the
  recording history (London Philharmonic Orchestra, Metro Voices,
  Joanne Hogg) in context.
