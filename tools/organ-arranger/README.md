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
`base + N − 1`. This instrument's four boards sit at base notes 0, 16, 32 and
48 — board 1 with every switch open — so solenoid 1 is slot 0, solenoid 64 is
slot 63, and 0 is the default:

```bash
python layout_to_organ.py instrument/layout.xlsx -o instrument/organ.yaml
```

A chain set up at 48, 64, 80 and 96 instead would use `--base-note 48`.

Each track heads a group of columns that runs until the next track name; the
row beneath names them. `number` is required; the rest are used when present:
a `section` column on a pitched track divides it into ranks (Main's Base,
Accompaniment and Melody), and registers may be given as `instrument`,
`section` and `action` columns — so two "Violin" stops on different sections
stay distinct — or as a single `Trombone on` / `Trombone off` label.

It infers that tracks named like *drums* or *registers* are pulse tracks
(`--pulse-track NAME=MS` to override), and **warns** about everything it had
to decide:
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
    sections: { Base: [48], Melody: [55] }   # optional: the ranks a transcriber arranges for
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

## Arranging a tune that was never written for the organ

`organ_transcribe.py` is the step *before* the arranger: it takes an ordinary
multi-track MIDI file — any key, any instruments, notes anywhere — and produces
the organ-format multi-track file above, with every note on a pipe that
exists. It is mechanical arranging with musical heuristics, and every decision
lands in a report and an editable plan, because taste belongs to the arranger
and not to a script.

```bash
python organ_transcribe.py tune.mid --organ instrument/organ.yaml --write-plan tune.plan.yaml
# edit tune.plan.yaml, then
python organ_transcribe.py tune.mid --organ instrument/organ.yaml --plan tune.plan.yaml
```

Writes `tune.fororgan.mid` (open it in the DAW, or feed it to the arranger) and
`tune.fororgan.txt`. What it does:

1. **Derives the organ's ranks** from the definition. Where the layout names
   sections, those are the ranks — `Main:Base` (four pipes), `Main:Accompainment`,
   `Main:Melody`, three ranks on one track. Otherwise a track is split
   wherever its notes leave a gap of a fifth or more.
2. **Reads and classifies the source.** Identical tracks (a doubled lead) are
   dropped. The lowest voice is the bass; the busiest reasonably-high line is
   the melody and goes to the widest upper rank; chordal low tracks are
   accompaniment; the rest are counter-melodies, spread across the counter
   ranks by register so they do not all pile onto one ten-note rank. Channel
   10 is the drum kit — a track merely *named* "Steel Drums" is not.
3. **Searches all 24 transpositions** and scores each by duration-weighted
   coverage — the fraction of each voice that lands on a pitch class its rank
   has — weighting the melody ×3 and the bass ×2. The report lists the top
   five; `--transpose N` overrides. Expect near-ties: an E-minor tune that
   also uses F♮ scored A minor (+5) a hair above D minor (−2) on this organ,
   because F♮ becomes a B♭ the organ has rather than a D♯ it does not.
4. **Folds each voice into its rank's compass** one octave at a time,
   preferring an octave that actually has the pipe (an octave displacement is
   far less wrong than a wrong semitone), staying near the line's previous
   note with a gentle pull toward the rank's centre. A voice may have a
   **fallback** rank — the bass gets the accompaniment automatically — and a
   note its own rank cannot play **spills over** there first, which is what a
   band-organ arrangement does with a four-pipe bass. A note with no pipe on
   either is snapped to the nearest one, or dropped with `--out-of-scale
   drop`; every one is listed.
5. **Thins chords** to what a voice may hold — one note for the melody
   (highest) and bass (lowest), two for counters, three for accompaniment.
6. **Maps drums** by GM number onto the organ's percussion, alternating
   between two snares where there are two, dropping what has no equivalent
   (hi-hats, toms, crashes) and saying which. **The leader's arm beats every
   downbeat** from the time signature, for as long as the music plays.
7. **Writes a registration**: the soft stops before the first note, the loud
   ones a quarter-second before the melody enters, everything off at the end.
8. **Runs the result through the arranger.** `Arranger check: 0 dropped` means
   every note has a pipe. Exit status 1 otherwise.

### The plan file

Everything in step 2 is written to the plan and can be changed:

```yaml
transpose: auto            # or an integer
voices:
  - { source: "Steel Drums#1", rank: "Main:Melody", role: melody, max_poly: 1, weight: 3.0 }
  - { source: "Picked Bs.#4",  rank: "Main:Base",   role: bass,   max_poly: 1, weight: 2.0,
      fallback: "Main:Accompainment" }                                 # spill-over rank
  - { source: "Marimba#5",     rank: TenorCM,     role: accomp, max_poly: 3, weight: 1.0 }
  - { source: "Organ 2#2",     rank: drop,        role: counter }      # silence a voice
drums:
  source: "Drums#10"
  map: { 35: bass, 36: bass, 38: snare, 40: snare, 37: snare }
  leader: downbeat           # or none
registration:
  - { at: start,  on: [Clarinet, Cello, ACC viol, MEL flute] }
  - { at: melody, on: [Trombone, Trumpet, MEL violin] }
  - { at: 45.0,   off: [Trumpet] }                                     # seconds
```

Sources are `name#index` so two tracks with the same name stay distinct.

What the tool cannot do is hear. Its output is *correct* for the organ long
before it is *good*; the first listen will say more than the report, and the
plan is where that judgement goes.

## Tests

```bash
pytest tests/
```

All three tools are deterministic and tested end to end: a synthetic organ, a
miniature of the real spreadsheet including its quirks, and a small tune in
the wrong key — with assertions on exactly which slot sounds when.
