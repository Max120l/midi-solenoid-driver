# organ-arranger

Arrange a general MIDI file for a specific solenoid-driven organ.

A file authored in a DAW is written for a *general* instrument: real pitches
on several tracks, GM drums on channel 10, velocities, notes anywhere on the
keyboard. The organ is a *specific* instrument: a fixed set of pipes, a few
drums, some registers, each nailed to one output slot on the driver boards.
Getting from one to the other is arrangement, not format conversion — and it
belongs here, on a machine with a screen and a text editor, not spread across
four write-only microcontrollers.

`organ_arranger.py` reads `song.mid`, writes `song.organ.mid` — a single-track,
single-channel file in which every note *is* a driver-board slot — and writes
`song.organ.txt`, a report of every decision it made. The source is never
modified. Your existing `mido`-based player plays the output unchanged.

## Install

Python 3.10 or later, plus two packages:

```bash
pip install mido pyyaml
```

(`pytest` as well if you want to run the tests.) The tool is a single file;
copy it wherever is convenient.

## Use

```bash
python organ_arranger.py song.mid --organ organ.yaml
```

Writes `song.organ.mid` and `song.organ.txt` next to the source and prints the
report. Options:

| | |
|---|---|
| `-o FILE` | arranged file to write (default `SONG.organ.mid`) |
| `--report FILE` | report to write (default alongside the output, `.txt`) |
| `--dry-run` | report only, write nothing — useful to check a file before committing to it |
| `-q` | do not print the report |

Exit status is 0 on success, 1 if the arranged file ended up with no notes at
all (almost always an organ definition that does not match the song), and 2
for a bad organ definition or an unreadable source.

## The organ definition

One YAML file describes what each of the 64 slots physically is. This is the
only place the instrument's identity lives. See
[`organ.example.yaml`](organ.example.yaml) for a complete, commented example —
**it is a placeholder layout, not a real instrument.**

Every value on the right-hand side of a mapping is an **output slot**: the MIDI
note number the driver boards listen for. With four boards under
`OFFSET_COARSE3` at base notes 48, 64, 80 and 96, the slots are 48–111.

```yaml
output_channel: 1              # boards are built with fixedChannel = 1

pitches:                       # score pitch -> slot
  60: 60
  62: 61

percussion:                    # GM drum note (channel 10) -> slot
  36: 88                       # bass drum
  38: 89                       # snare

register_track: Registers      # track whose name contains this (case-insensitive)
registers:                     # note on that track -> set/reset slots
  60: { name: Trumpet, set: 96, reset: 97 }

timing:                        # all milliseconds; see below
  min_note_ms: 50
  min_gap_ms: 30
  percussion_pulse_ms: 50
  register_pulse_ms: 100
  register_stagger_ms: 60
  lead_in_ms: 1000
  settle_ms: 250
  reset_registers_at_start: true
  reset_registers_at_end: true
```

Several pitches may share one slot — a substitute pipe for a note the organ
lacks. Nothing else may share a slot with anything; the tool refuses to run
if it does. `register_channel: N` is accepted instead of, or as well as,
`register_track`, for files where the registers live on a channel rather than
a named track.

## What it does, in order

**Reads every track with the tempo map applied.** Tempo changes may sit on any
track in a type 1 file; they are gathered first, then each track is walked and
every note placed in absolute seconds. Same-pitch overlaps within one track
pair first-on/first-off, as every DAW does.

**Classifies each note.** On the register track → a register. On channel 10 →
percussion. Anything else → a pitch. In that order, so a register track can
use any notes it likes.

**Maps it onto a slot, or drops it.**

- A pitch the organ has goes to its slot with its written duration.
- A pitch the organ lacks is **dropped and reported**. Files for this organ
  are arranged for it, so anything out of range is a mistake worth seeing
  rather than something to paper over with octave folding.
- A drum becomes a **fixed-length pulse** (`percussion_pulse_ms`) regardless
  of how long it was drawn. A drum is a strike, not a sustain, and DAW drum
  notes are often one tick long anyway.
- A register note spans the section the register is engaged for. Since the
  mechanism latches, it becomes **one pulse to the set coil** where the note
  starts and **one pulse to the reset coil** where it ends.
- Velocity is discarded; pipes have no dynamics and the boards ignore it.

**Makes every slot physically playable.** This is the part a naive converter
gets wrong. Per slot:

- Genuinely **overlapping notes are merged** — two tracks landing on the same
  pipe at once. Without this, one voice's note-off silences the other's
  sustained note, which is exactly the "malformed MIDI" the firmware has to
  defend against; better never to emit it.
- Notes shorter than `min_note_ms` are **stretched** to it. A solenoid needs
  time to pull in; a 5 ms note produces nothing.
- Two notes closer together than `min_gap_ms` cannot re-articulate. The
  earlier note is **trimmed** to leave the gap where that keeps it at least
  `min_note_ms` long, and the pair is **merged** into one note where it would
  not.

Everything above is reported, with a timestamp, the slot and what was there.

**Adds the register preamble and postamble.** With `reset_registers_at_start`,
every register's reset coil is pulsed before the music starts, staggered by
`register_stagger_ms` so they do not all fire at once. This is what makes
set/reset registers *stateless*: whatever happened last time, they are all
closed now, and the boards — which cannot report their state — do not need to.
The music is shifted late by `lead_in_ms`, or by however long the preamble
needs plus a gap, whichever is greater. With `reset_registers_at_end`, the
same happens `settle_ms` after the last note, so the organ is left closed.

**Writes a type 0 file** at a fixed 120 BPM with 960 ticks per beat (about
half a millisecond of resolution), every note on `output_channel`, note-offs
sorted before note-ons at equal ticks. The source tempo map has been applied,
not preserved — the output is for a machine, and bars would not line up in a
DAW anyway once notes have been trimmed and merged.

## The report

```
Summary
  duration         2:41.120   (lead-in 1000 ms)
  pitched notes    1412
  percussion hits  288
  register pulses  22
  dropped          3

Tracks
  Melody: 611 notes -> 608 pitch, 3 dropped
  Bass: 402 notes -> 402 pitch
  Drums: 288 notes -> 288 percussion
  Registers: 4 notes -> 4 register

Dropped: pitch this organ does not have (3)
  0:44.250  Melody: G#6 (92)
  ...
```

The sections worth reading before a first performance are the three
`Dropped` ones (mistakes in the arrangement) and `Merged: re-articulation too
fast to play` (passages the mechanism cannot execute as written).

## Tuning the timings

`min_note_ms`, `min_gap_ms` and `percussion_pulse_ms` are what the solenoids
can physically do and want measuring against the chest, not guessing. Start
with the defaults; if fast passages smear, the gap is too small; if notes
fail to speak, the minimum is too short. The register pulse just needs to be
long enough to throw the latch reliably.

## Tests

```bash
pip install pytest
pytest tests/
```

The transformation is deterministic, so it is tested end to end: a synthetic
organ, small hand-built MIDI files, and assertions on exactly which slot
sounds when. Every policy above has a test.
