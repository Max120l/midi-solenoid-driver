# organ-arranger

Arrange a multi-track MIDI file for a specific solenoid organ.

A file authored in a DAW is written for a *general* instrument: real pitches
on several tracks, velocities, notes anywhere on the keyboard. The organ is a
*specific* instrument: a fixed set of pipes, a few drums, some registers, each
nailed to one output slot on the driver boards — and on a band organ **the
same note number on a different track is a different pipe.** Getting from one
to the other is arrangement, not format conversion, and it belongs here, on a
machine with a screen and a text editor, not spread across four write-only
microcontrollers.

Two tools:

- **`layout_to_organ.py`** turns the instrument's layout spreadsheet into
  `organ.yaml`. The spreadsheet is the source of truth; edit it and re-run.
- **`organ_arranger.py`** reads `song.mid` and `organ.yaml`, writes
  `song.organ.mid` — one track, one channel, every note a driver-board slot —
  and `song.organ.txt`, a report of every decision it made. The source is
  never modified. Your `mido`-based player plays the output unchanged.

## Install

Python 3.10 or later:

```bash
pip install mido pyyaml openpyxl
```

(`pytest` as well to run the tests.)

## The layout spreadsheet

One row per solenoid; one column pair per DAW track:

| Solenoid | Main<br>number · note | TenorCM<br>number · note | Drums<br>number · name | Registers<br>number · action |
|---|---|---|---|---|
| 1 | | | | 122 · Trombone on |
| 2 | | | | 121 · Trombone off |
| 15 | | | 25 · Bass | |
| 16 | | 60 · C | | |
| 28 | 60 · C | | | |

Each solenoid has exactly one `(track, note)` entry. Solenoid *N* drives slot
`base + N − 1` — with four boards at base notes 48, 64, 80 and 96, solenoid 1
is slot 48 and solenoid 64 is slot 111.

```bash
python layout_to_organ.py "MIDI layout.xlsx" -o organ.yaml --base-note 48
```

It infers that tracks named like *drums* or *registers* are pulse tracks
(`--pulse-track NAME=MS` to override), pairs registers from the action text
`<name> on` / `<name> off`, and **warns** about everything it had to decide:
a note that drives two solenoids (kept as a doubled rank), a solenoid with no
entry, a register pair where both rows say "on" (it assumes the higher note is
"on" and tells you), a label it could not parse. Read the warnings; they are
the sheet's typos.

## Arranging a song

```bash
python organ_arranger.py song.mid --organ organ.yaml
```

Writes `song.organ.mid` and `song.organ.txt` next to the source and prints the
report. `-o FILE`, `--report FILE`, `--dry-run` (report only, write nothing),
`-q`. Exit status is 0 on success, 1 if nothing from the song survived
(almost always track names that do not match), 2 for a bad definition or an
unreadable source.

## The organ definition

See [`organ.example.yaml`](organ.example.yaml) for a commented example — a
placeholder layout, not a real instrument.

```yaml
output_channel: 1
tracks:
  Main:                        # matched to the DAW track by name
    kind: pitched              # notes keep their written duration
    notes: { 48: 60, 55: [64, 65] }   # note on this track -> slot(s)
  Drums:
    kind: pulse                # every note is a strike of fixed length
    pulse_ms: 50
    notes: { 25: 100 }
    labels: { 25: Bass }       # optional, for the report
  Registers:
    kind: pulse
    pulse_ms: 100
    notes: { 122: 104, 121: 105 }
registers:                     # set/reset pairs, as slots
  - { name: Trombone, set: 104, reset: 105 }
timing: { ... }
```

Track names match the DAW's exactly, case-insensitively, or by unique
substring. Within a track, several notes may share a slot (a substitute pipe)
and one note may sound several slots (a doubled rank); a slot may never belong
to two tracks. A register coil must be on a pulse track. The tool refuses to
run otherwise.

## What it does, in order

**Reads every track with the tempo map applied.** Tempo changes may sit on any
track in a type 1 file; they are gathered first, then each track is walked and
every note placed in absolute seconds. Same-pitch overlaps within one track
pair first-on/first-off, as every DAW does.

**Maps each note through its track**, or drops it:

- A note the track's map knows goes to its slot(s). On a pitched track it
  keeps its written duration; on a pulse track it becomes a **fixed-length
  strike** — a drum is a hit, a register note is a command, and DAW drum notes
  are often one tick long anyway.
- A note the track's map lacks is **dropped and reported**. Files for this
  organ are arranged for it, so anything unmapped is a mistake worth seeing,
  not something to paper over with octave folding.
- A track the organ does not know is **ignored and reported**; a track the
  organ expects but the file lacks is reported too. Both are usually a renamed
  track in the DAW, and worth seeing before wondering why a rank is silent.
- Velocity is discarded; pipes have no dynamics and the boards ignore it.

**Makes every slot physically playable.** Per slot:

- Genuinely **overlapping notes are merged** — the same pipe asked to sound
  twice at once. Without this, one note-off silences the other's sustain,
  which is exactly the "malformed MIDI" the firmware has to defend against;
  better never to emit it.
- Notes shorter than `min_note_ms` are **stretched** to it; a solenoid needs
  time to pull in.
- Two notes closer than `min_gap_ms` cannot re-articulate. The earlier is
  **trimmed** to leave the gap where that keeps it at least `min_note_ms`
  long, and the pair is **merged** where it would not.

**Adds the register preamble and postamble.** With `reset_registers_at_start`,
every register's reset coil is pulsed before the music, staggered by
`register_stagger_ms`. This is what makes set/reset registers *stateless*:
whatever happened last time, they are all closed now, and the boards — which
cannot report their state — do not need to. The music is shifted late by
`lead_in_ms`, or by however long the preamble needs plus a gap, whichever is
greater. With `reset_registers_at_end`, the same happens `settle_ms` after the
last note.

**Writes a type 0 file** at a fixed 120 BPM, 960 ticks per beat, every note on
`output_channel`, note-offs before note-ons at equal ticks. The tempo map has
been applied, not preserved; the output is for a machine.

## The report

```
Summary
  duration         2:41.120   (lead-in 1000 ms)
  pitched notes    1412
  pulses           288   (drums, registers, anything struck)
  register resets  14    (added before and after the music)
  dropped          3

Tracks
  Main: 611 notes -> 608 pitched, 3 dropped
  Drums: 288 notes -> 288 pulses
  Registers: 14 notes -> 14 pulses
  Cakewalk TTS-1 1: 0 notes -> ignored, no such track in the organ definition
  TrebCM: defined in the organ but not present in this file

Dropped: note not on this organ (3)
  0:44.250  Main: G#6 (92)
  ...
```

All times are **source (DAW) time**, before the lead-in, in chronological
order, so they can be read against the file you are editing. The sections
worth reading before a first performance are `Dropped` (mistakes in the
arrangement) and `Merged: re-articulation too fast to play` (passages the
mechanism cannot execute as written).

## Tuning the timings

`min_note_ms`, `min_gap_ms` and the pulse lengths are what the solenoids can
physically do and want measuring against the chest. If fast passages smear the
gap is too small; if notes fail to speak the minimum is too short; the
register pulse just needs to throw the latch reliably.

## Tests

```bash
pytest tests/
```

Both tools are deterministic and tested end to end: a synthetic organ and a
miniature of the real spreadsheet — including its quirks — with assertions on
exactly which slot sounds when.
