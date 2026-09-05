#!/usr/bin/env python3
"""
organ_arranger -- arrange a multi-track MIDI file for a specific solenoid organ.

A file written in a DAW is written for a general instrument: real pitches on
several tracks, velocities, notes anywhere on the keyboard. A solenoid organ
is a specific instrument: a fixed set of pipes, a few drums, some registers,
each nailed to one output slot on the driver boards -- and on a band organ the
*same note number on a different track is a different pipe*.

This tool turns the former into the latter: a single-track, single-channel
file in which every note *is* a driver-board slot, plus a report of every
decision made along the way -- what was dropped, merged, stretched or trimmed.
The source file is never modified.

Usage:
    organ_arranger.py SONG.mid --organ ORGAN.yaml [-o SONG.organ.mid]
                                                  [--report SONG.organ.txt]
                                                  [--dry-run] [--quiet]

The organ definition is normally generated from the instrument's layout
spreadsheet by layout_to_organ.py. See README.md for the format and policies.
"""

from __future__ import annotations

import argparse
import bisect
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import mido
import yaml

__version__ = "0.2.0"

# Output file parameters. Events are placed by absolute time, so the source
# tempo map is *applied* rather than preserved: the output runs at one fixed
# tempo with fine resolution, which is the simplest thing a player can consume.
OUTPUT_TEMPO = 500_000            # microseconds per beat: 120 BPM
OUTPUT_TICKS_PER_BEAT = 960       # about 0.52 ms per tick at that tempo
OUTPUT_VELOCITY = 100             # the boards ignore velocity; anything non-zero
REPORT_MAX_LINES = 200            # per section, so a bad file cannot bury you
EPS = 1e-6                        # seconds; below any tick, above float noise

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]

KIND_PITCHED = "pitched"          # notes keep their written duration
KIND_PULSE = "pulse"              # each note is a strike: fixed length, written length ignored
KIND_RESET = "reset"              # register resets added by the arranger itself


class OrganError(Exception):
    """A problem with the organ definition, as opposed to with the song."""


# ----------------------------------------------------------------------------
# Organ definition
# ----------------------------------------------------------------------------

@dataclass
class Track:
    name: str
    kind: str                              # KIND_PITCHED | KIND_PULSE
    notes: dict[int, tuple[int, ...]]      # score note -> one or more slots
    pulse_ms: int                          # used when kind is pulse
    labels: dict[int, str] = field(default_factory=dict)   # score note -> text, for the report
    # Optional: section name -> the track's notes in it. Divides one track into
    # the ranks a transcriber arranges for (Main's Base / Accompaniment /
    # Melody). The arranger itself does not need it.
    sections: dict[str, list[int]] = field(default_factory=dict)

    def label(self, note: int) -> str:
        return self.labels.get(note) or f"{self.name} {note_name(note)}"


@dataclass(frozen=True)
class Register:
    name: str
    set_slot: int
    reset_slot: int


@dataclass
class Timing:
    """All in milliseconds. See README for what each one is for."""
    min_note_ms: int = 50
    min_gap_ms: int = 30
    pulse_ms: int = 50                # default for pulse tracks that do not say
    register_pulse_ms: int = 100      # the arranger's own preamble/postamble resets
    register_stagger_ms: int = 60
    lead_in_ms: int = 1000
    settle_ms: int = 250
    reset_registers_at_start: bool = True
    reset_registers_at_end: bool = True


@dataclass
class Organ:
    name: str
    output_channel: int                    # zero-based internally; 1-16 in the file
    tracks: dict[str, Track]               # keyed by the name as written in the definition
    registers: list[Register]
    timing: Timing
    slot_min_len_ms: dict[int, int] = field(default_factory=dict)   # filled in by validate()

    @classmethod
    def load(cls, path) -> "Organ":
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        if not isinstance(raw, dict):
            raise OrganError("organ definition must be a mapping at the top level")
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Organ":
        try:
            name = str(raw.get("name", "organ"))
            out_ch = int(raw.get("output_channel", 1))
            timing = Timing(**(raw.get("timing") or {}))

            tracks: dict[str, Track] = {}
            for tname, tdef in (raw.get("tracks") or {}).items():
                tdef = tdef or {}
                kind = str(tdef.get("kind", KIND_PITCHED)).lower()
                if kind not in (KIND_PITCHED, KIND_PULSE):
                    raise OrganError(f"track {tname}: kind must be '{KIND_PITCHED}' or '{KIND_PULSE}', not '{kind}'")
                notes: dict[int, tuple[int, ...]] = {}
                for k, v in (tdef.get("notes") or {}).items():
                    slots = v if isinstance(v, (list, tuple)) else [v]
                    notes[int(k)] = tuple(int(s) for s in slots)
                labels = {int(k): str(v) for k, v in (tdef.get("labels") or {}).items()}
                sections = {str(k): [int(x) for x in (v or [])]
                            for k, v in (tdef.get("sections") or {}).items()}
                tracks[str(tname)] = Track(str(tname), kind, notes,
                                           int(tdef.get("pulse_ms", timing.pulse_ms)), labels, sections)

            registers = []
            for r in (raw.get("registers") or []):
                registers.append(Register(str(r["name"]), int(r["set"]), int(r["reset"])))
        except OrganError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise OrganError(f"malformed organ definition: {e}") from e

        organ = cls(name=name, output_channel=out_ch - 1, tracks=tracks,
                    registers=registers, timing=timing)
        organ.validate()
        return organ

    def validate(self) -> None:
        if not 0 <= self.output_channel <= 15:
            raise OrganError("output_channel must be 1-16")
        if not self.tracks:
            raise OrganError("no tracks defined; the organ would play nothing")

        t = self.timing
        for fld in ("min_note_ms", "min_gap_ms", "pulse_ms", "register_pulse_ms",
                    "register_stagger_ms", "lead_in_ms", "settle_ms"):
            if getattr(t, fld) < 0:
                raise OrganError(f"timing.{fld} must not be negative")
        if t.pulse_ms == 0 or t.register_pulse_ms == 0:
            raise OrganError("pulse lengths must be greater than zero")

        # A slot is one pipe, and one pipe belongs to one track. Within a track
        # several notes may share a slot (a substitute pipe) and one note may
        # sound several slots (a doubled rank); across tracks, never.
        owner: dict[int, str] = {}
        self.slot_min_len_ms = {}
        for track in self.tracks.values():
            if track.kind == KIND_PULSE and track.pulse_ms <= 0:
                raise OrganError(f"track {track.name}: pulse_ms must be greater than zero")
            for note, slots in track.notes.items():
                if not 0 <= note <= 127:
                    raise OrganError(f"track {track.name}: note {note} is not a MIDI note number (0-127)")
                if len(set(slots)) != len(slots):
                    raise OrganError(f"track {track.name}: note {note} lists the same slot twice")
                for slot in slots:
                    if not 0 <= slot <= 127:
                        raise OrganError(f"track {track.name}: slot {slot} is not a MIDI note number (0-127)")
                    prev = owner.get(slot)
                    if prev is not None and prev != track.name:
                        raise OrganError(f"slot {slot} is used by both track {prev} and track {track.name}")
                    owner[slot] = track.name
                    self.slot_min_len_ms[slot] = (track.pulse_ms if track.kind == KIND_PULSE
                                                  else t.min_note_ms)
            for section, sec_notes in track.sections.items():
                for n in sec_notes:
                    if n not in track.notes:
                        raise OrganError(f"track {track.name}: section {section} lists note {n}, "
                                         f"which the track does not have")

        for reg in self.registers:
            if reg.set_slot == reg.reset_slot:
                raise OrganError(f"register {reg.name}: set and reset are the same slot")
            for slot, which in ((reg.set_slot, "set"), (reg.reset_slot, "reset")):
                if not 0 <= slot <= 127:
                    raise OrganError(f"register {reg.name}: {which} slot {slot} is not a MIDI note number")
                owning = owner.get(slot)
                if owning is not None and self.tracks[owning].kind != KIND_PULSE:
                    raise OrganError(f"register {reg.name}: {which} slot {slot} belongs to pitched "
                                     f"track {owning}; a register coil wants a pulse track")
                self.slot_min_len_ms.setdefault(slot, t.register_pulse_ms)

    def find_track(self, midi_track_name: str) -> Track | None:
        """Exact name match first, case-insensitively; else a unique substring."""
        wanted = midi_track_name.strip().lower()
        for key, track in self.tracks.items():
            if key.strip().lower() == wanted:
                return track
        hits = [track for key, track in self.tracks.items()
                if key.strip() and key.strip().lower() in wanted]
        return hits[0] if len(hits) == 1 else None

    @property
    def all_slots(self) -> set[int]:
        return set(self.slot_min_len_ms)


# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------

def fmt_time(seconds: float) -> str:
    minutes, rem = divmod(max(seconds, 0.0), 60)
    return f"{int(minutes)}:{rem:06.3f}"


def note_name(n: int) -> str:
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


@dataclass
class RawNote:
    """A note as it appears in the source, in absolute seconds."""
    track: str
    channel: int
    note: int
    start: float
    end: float


@dataclass
class Interval:
    """A note as the organ will play it: one slot, on at start, off at end."""
    slot: int
    start: float
    end: float
    origin: str       # human-readable, for the report
    kind: str         # KIND_*


class Report:
    """Every line carries the time it refers to, in *source* (DAW) seconds, so
    sections can be rendered chronologically and cross-referenced against the
    file the user is actually editing."""

    def __init__(self) -> None:
        self.sections: dict[str, list[tuple[float, str]]] = defaultdict(list)
        self.counts: Counter = Counter()
        self.tracks: list[tuple[str, int]] = []            # (name, raw note count), file order
        self.track_kinds: dict[str, Counter] = defaultdict(Counter)
        self.missing_tracks: list[str] = []                # in the organ, absent from the file
        self.notes: Counter = Counter()                    # final notes by kind
        self.lead_in_s = 0.0
        self.duration_s = 0.0

    def add(self, section: str, at: float, line: str) -> None:
        self.sections[section].append((at, line))
        self.counts[section] += 1

    @property
    def dropped(self) -> int:
        return sum(n for s, n in self.counts.items() if s.startswith("Dropped"))

    def render(self, organ: Organ, source: str, output: str) -> str:
        L: list[str] = []
        L.append(f"organ_arranger {__version__}")
        L.append(f"source : {source}")
        L.append(f"output : {output}")
        L.append(f"organ  : {organ.name}")
        L.append("times  : as in the source file, before the lead-in")
        L.append("")
        L.append("Summary")
        L.append(f"  duration         {fmt_time(self.duration_s)}   (lead-in {self.lead_in_s * 1000:.0f} ms)")
        L.append(f"  pitched notes    {self.notes[KIND_PITCHED]}")
        L.append(f"  pulses           {self.notes[KIND_PULSE]}   (drums, registers, anything struck)")
        L.append(f"  register resets  {self.notes[KIND_RESET]}   (added before and after the music)")
        L.append(f"  dropped          {self.dropped}")
        L.append("")
        L.append("Tracks")
        for name, raw_count in self.tracks:
            kinds = self.track_kinds.get(name, Counter())
            if kinds["ignored"]:
                L.append(f"  {name}: {raw_count} notes -> ignored, no such track in the organ definition")
                continue
            parts = []
            if kinds[KIND_PITCHED]:
                parts.append(f"{kinds[KIND_PITCHED]} pitched")
            if kinds[KIND_PULSE]:
                parts.append(f"{kinds[KIND_PULSE]} pulses")
            if kinds["dropped"]:
                parts.append(f"{kinds['dropped']} dropped")
            detail = ", ".join(parts) if parts else "nothing usable"
            L.append(f"  {name}: {raw_count} notes -> {detail}")
        for name in self.missing_tracks:
            L.append(f"  {name}: defined in the organ but not present in this file")
        for section in sorted(self.sections):
            lines = sorted(self.sections[section], key=lambda entry: entry[0])
            L.append("")
            L.append(f"{section} ({len(lines)})")
            for _, line in lines[:REPORT_MAX_LINES]:
                L.append("  " + line)
            if len(lines) > REPORT_MAX_LINES:
                L.append(f"  ... and {len(lines) - REPORT_MAX_LINES} more")
        return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------
# Reading the source
# ----------------------------------------------------------------------------

class TickClock:
    """Converts absolute ticks to absolute seconds through the file's tempo map.

    Tempo changes may live on any track in a type 1 file (usually the first),
    so they are gathered from all of them before any track is walked.
    """

    def __init__(self, mid: mido.MidiFile) -> None:
        changes: list[tuple[int, int]] = []
        for track in mid.tracks:
            tick = 0
            for msg in track:
                tick += msg.time
                if msg.type == "set_tempo":
                    changes.append((tick, msg.tempo))
        changes.sort(key=lambda c: c[0])
        if not changes or changes[0][0] != 0:
            changes.insert(0, (0, 500_000))   # the MIDI default, 120 BPM

        self.tpb = mid.ticks_per_beat
        self.ticks: list[int] = []
        self.secs: list[float] = []
        self.tempos: list[int] = []
        acc = 0.0
        prev_tick, prev_tempo = changes[0]
        self.ticks.append(prev_tick)
        self.secs.append(0.0)
        self.tempos.append(prev_tempo)
        for tick, tempo in changes[1:]:
            acc += mido.tick2second(tick - prev_tick, self.tpb, prev_tempo)
            self.ticks.append(tick)
            self.secs.append(acc)
            self.tempos.append(tempo)
            prev_tick, prev_tempo = tick, tempo

    def seconds(self, tick: int) -> float:
        i = bisect.bisect_right(self.ticks, tick) - 1
        return self.secs[i] + mido.tick2second(tick - self.ticks[i], self.tpb, self.tempos[i])


def extract_notes(mid: mido.MidiFile, clock: TickClock, report: Report) -> list[RawNote]:
    """Pair note-ons with note-offs, per track, in absolute seconds.

    Same-pitch overlaps on one track are paired first-on/first-off, which is
    what every DAW does. A note-off with nothing open is ignored and reported;
    a note still open at end of track is closed there and reported.
    """
    notes: list[RawNote] = []
    for index, track in enumerate(mid.tracks):
        name = track.name or f"Track {index + 1}"
        tick = 0
        open_notes: dict[tuple[int, int], list[float]] = defaultdict(list)
        count = 0
        for msg in track:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                open_notes[(msg.channel, msg.note)].append(clock.seconds(tick))
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if open_notes[key]:
                    start = open_notes[key].pop(0)
                    notes.append(RawNote(name, msg.channel, msg.note, start, clock.seconds(tick)))
                    count += 1
                else:
                    at = clock.seconds(tick)
                    report.add("Stray note-offs (ignored)", at,
                               f"{fmt_time(at)}  {name}: {note_name(msg.note)} ch{msg.channel + 1}")
        end = clock.seconds(tick)
        for (channel, note), starts in open_notes.items():
            for start in starts:
                notes.append(RawNote(name, channel, note, start, end))
                count += 1
                report.add("Unterminated notes (closed at end of track)", start,
                           f"{fmt_time(start)}  {name}: {note_name(note)} ch{channel + 1}")
        report.tracks.append((name, count))
    return notes


# ----------------------------------------------------------------------------
# Mapping onto the organ
# ----------------------------------------------------------------------------

def map_notes(raw: list[RawNote], organ: Organ, report: Report) -> list[Interval]:
    out: list[Interval] = []
    seen_organ_tracks: set[str] = set()

    for n in raw:
        track = organ.find_track(n.track)
        if track is None:
            report.track_kinds[n.track]["ignored"] += 1
            continue
        seen_organ_tracks.add(track.name)

        slots = track.notes.get(n.note)
        if slots is None:
            report.add("Dropped: note not on this organ", n.start,
                       f"{fmt_time(n.start)}  {n.track}: {note_name(n.note)} ({n.note})")
            report.track_kinds[n.track]["dropped"] += 1
            continue

        if track.kind == KIND_PULSE:
            # A strike, not a sustain: fixed pulse, written length ignored. DAW
            # drum notes are frequently one tick long; register on/off notes
            # are commands, not durations.
            end = n.start + track.pulse_ms / 1000
        else:
            end = n.end
        for slot in slots:
            out.append(Interval(slot, n.start, end, track.label(n.note), track.kind))
        report.track_kinds[n.track][track.kind] += 1

    # Tracks the organ knows about that never appeared: usually a renamed track
    # in the DAW, and worth seeing before wondering why a whole rank is silent.
    for key, track in organ.tracks.items():
        if track.name not in seen_organ_tracks and not any(
                organ.find_track(name) is track for name, _ in report.tracks):
            report.missing_tracks.append(key)
    return out


# ----------------------------------------------------------------------------
# Making each slot physically playable
# ----------------------------------------------------------------------------

def settle_slot(intervals: list[Interval], min_note: float, min_gap: float,
                report: Report, label: str, time_offset: float = 0.0) -> list[Interval]:
    """Turn everything aimed at one slot into a clean, playable sequence.

    Two passes. First, genuinely overlapping notes are merged, because one pipe
    cannot sound twice at once and a naive note-off from one voice would cut
    the other short. Second, every note is made at least `min_note` long and
    every re-articulation is given at least `min_gap` of silence, by trimming
    the earlier note where that leaves it long enough and merging the pair
    where it would not. Every intervention is reported.

    Intervals arrive in output time (lead-in applied); `time_offset` is
    subtracted for the report so it reads in source time like everything else.
    Comparisons carry EPS so that a pulse of exactly the minimum length, or a
    note starting exactly where the last ended, is not misread through float
    noise as too short or overlapping.
    """
    ordered = sorted(intervals, key=lambda i: (i.start, i.end))

    def src(t: float) -> float:
        return t - time_offset

    merged: list[Interval] = []
    for iv in ordered:
        if merged and iv.start < merged[-1].end - EPS:
            prev = merged[-1]
            report.add("Merged: overlapping notes on one slot", src(iv.start),
                       f"{fmt_time(src(iv.start))}  {label}: {prev.origin} + {iv.origin}")
            prev.end = max(prev.end, iv.end)
            continue
        merged.append(Interval(iv.slot, iv.start, iv.end, iv.origin, iv.kind))

    out: list[Interval] = []
    for iv in merged:
        length = iv.end - iv.start
        if length < min_note - EPS:
            report.add("Stretched: note shorter than the solenoid can play", src(iv.start),
                       f"{fmt_time(src(iv.start))}  {label}: {iv.origin} "
                       f"{length * 1000:.0f} ms -> {min_note * 1000:.0f} ms")
            iv.end = iv.start + min_note
        if out:
            prev = out[-1]
            gap = iv.start - prev.end
            if gap < min_gap - EPS:
                trimmed_end = iv.start - min_gap
                if trimmed_end - prev.start >= min_note - EPS:
                    report.add("Trimmed: shortened a note to leave a re-articulation gap",
                               src(prev.start),
                               f"{fmt_time(src(prev.start))}  {label}: {prev.origin} ends "
                               f"{fmt_time(src(prev.end))} -> {fmt_time(src(trimmed_end))}")
                    prev.end = trimmed_end
                else:
                    report.add("Merged: re-articulation too fast to play, joined into one note",
                               src(prev.start),
                               f"{fmt_time(src(prev.start))}  {label}: {prev.origin} + {iv.origin}")
                    prev.end = max(prev.end, iv.end)
                    continue
        out.append(iv)
    return out


# ----------------------------------------------------------------------------
# The whole arrangement
# ----------------------------------------------------------------------------

def arrange(mid: mido.MidiFile, organ: Organ) -> tuple[mido.MidiFile, Report]:
    report = Report()
    clock = TickClock(mid)
    raw = extract_notes(mid, clock, report)
    intervals = map_notes(raw, organ, report)
    t = organ.timing
    pulse = t.register_pulse_ms / 1000
    stagger = t.register_stagger_ms / 1000

    # Lead-in and the register preamble. Pulsing every reset coil before the
    # music starts is what makes set/reset registers stateless: whatever
    # happened last time, they are all closed now. The music is shifted late
    # enough that the preamble is finished, with a gap, before anything fires.
    lead_in = t.lead_in_ms / 1000
    preamble: list[Interval] = []
    if t.reset_registers_at_start and organ.registers:
        at = 0.0
        for reg in organ.registers:
            preamble.append(Interval(reg.reset_slot, at, at + pulse,
                                     f"{reg.name} reset (preamble)", KIND_RESET))
            at += stagger
        preamble_end = at - stagger + pulse
        lead_in = max(lead_in, preamble_end + t.min_gap_ms / 1000)
    for iv in intervals:
        iv.start += lead_in
        iv.end += lead_in
    intervals.extend(preamble)
    report.lead_in_s = lead_in

    # Postamble: leave the registers closed when the song ends, too.
    if t.reset_registers_at_end and organ.registers:
        last = max((max(iv.end, iv.start + organ.slot_min_len_ms.get(iv.slot, t.register_pulse_ms) / 1000)
                    for iv in intervals), default=0.0)
        at = last + t.settle_ms / 1000
        for reg in organ.registers:
            intervals.append(Interval(reg.reset_slot, at, at + pulse,
                                      f"{reg.name} reset (postamble)", KIND_RESET))
            at += stagger

    by_slot: dict[int, list[Interval]] = defaultdict(list)
    for iv in intervals:
        by_slot[iv.slot].append(iv)

    final: list[Interval] = []
    for slot in sorted(by_slot):
        min_len = organ.slot_min_len_ms.get(slot, t.register_pulse_ms) / 1000
        final.extend(settle_slot(by_slot[slot], min_len, t.min_gap_ms / 1000, report,
                                 f"slot {slot}", time_offset=lead_in))

    for iv in final:
        report.notes[iv.kind] += 1
    report.duration_s = max((iv.end for iv in final), default=0.0)

    return build_output(final, organ), report


def build_output(intervals: list[Interval], organ: Organ) -> mido.MidiFile:
    tpb, tempo, channel = OUTPUT_TICKS_PER_BEAT, OUTPUT_TEMPO, organ.output_channel

    # (tick, 0 for off / 1 for on, slot). Offs sort before ons at the same
    # tick, so a slot can never see its next on before its previous off.
    events: list[tuple[int, int, int]] = []
    for iv in intervals:
        start = int(round(mido.second2tick(iv.start, tpb, tempo)))
        end = int(round(mido.second2tick(iv.end, tpb, tempo)))
        if end <= start:
            end = start + 1
        events.append((start, 1, iv.slot))
        events.append((end, 0, iv.slot))
    events.sort()

    mid = mido.MidiFile(type=0, ticks_per_beat=tpb)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("track_name", name=f"{organ.name} (arranged)", time=0))
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    prev = 0
    for tick, is_on, slot in events:
        if is_on:
            msg = mido.Message("note_on", channel=channel, note=slot,
                               velocity=OUTPUT_VELOCITY, time=tick - prev)
        else:
            msg = mido.Message("note_off", channel=channel, note=slot,
                               velocity=0, time=tick - prev)
        track.append(msg)
        prev = tick
    track.append(mido.MetaMessage("end_of_track", time=0))
    return mid


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="organ_arranger",
        description="Arrange a multi-track MIDI file for a specific solenoid organ.",
    )
    parser.add_argument("song", help="the source .mid file, as authored in a DAW")
    parser.add_argument("--organ", required=True, help="organ definition (YAML)")
    parser.add_argument("-o", "--output", help="arranged .mid to write (default: SONG.organ.mid)")
    parser.add_argument("--report", help="report file to write (default: alongside the output, .txt)")
    parser.add_argument("--dry-run", action="store_true", help="report only; write nothing")
    parser.add_argument("-q", "--quiet", action="store_true", help="do not print the report")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    try:
        organ = Organ.load(args.organ)
    except (OrganError, OSError, yaml.YAMLError) as e:
        print(f"error: organ definition: {e}", file=sys.stderr)
        return 2

    source = Path(args.song)
    try:
        mid = mido.MidiFile(str(source))
    except (OSError, ValueError, EOFError, KeyError, IndexError) as e:
        print(f"error: cannot read {source}: {e}", file=sys.stderr)
        return 2

    output = Path(args.output) if args.output else source.with_suffix(".organ.mid")
    report_path = Path(args.report) if args.report else output.with_suffix(".txt")

    arranged, report = arrange(mid, organ)
    text = report.render(organ, str(source), str(output))

    if not args.dry_run:
        arranged.save(str(output))
        report_path.write_text(text, encoding="utf-8")
    if not args.quiet:
        print(text, end="")

    if report.notes[KIND_PITCHED] + report.notes[KIND_PULSE] == 0:
        print("warning: nothing from the song survived arrangement -- "
              "check the track names in the organ definition against this file", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
