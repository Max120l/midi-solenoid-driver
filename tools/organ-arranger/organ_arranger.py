#!/usr/bin/env python3
"""
organ_arranger -- arrange a general MIDI file for a specific solenoid-driven organ.

A file written in a DAW is written for a general instrument: real pitches on
several tracks, GM drums on channel 10, velocities, notes anywhere on the
keyboard. A solenoid organ is a specific instrument: a fixed set of pipes, a
few drums, some registers, each nailed to one output slot on the driver boards.

This tool turns the former into the latter -- a single-track, single-channel
file in which every note *is* a driver-board slot -- and writes a report of
every decision it made along the way: what it dropped, merged, stretched or
trimmed. The source file is never modified.

Usage:
    organ_arranger.py SONG.mid --organ ORGAN.yaml [-o SONG.organ.mid]
                                                  [--report SONG.organ.txt]
                                                  [--dry-run] [--quiet]

See README.md alongside this file for the organ definition format and the
policies applied.
"""

from __future__ import annotations

import argparse
import bisect
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

import mido
import yaml

__version__ = "0.1.0"

# Output file parameters. Events are placed by absolute time, so the source
# tempo map is *applied* rather than preserved: the output runs at one fixed
# tempo with fine resolution, which is the simplest thing a player can consume.
OUTPUT_TEMPO = 500_000            # microseconds per beat: 120 BPM
OUTPUT_TICKS_PER_BEAT = 960       # about 0.52 ms per tick at that tempo
OUTPUT_VELOCITY = 100             # the boards ignore velocity; anything non-zero
DRUM_CHANNEL = 9                  # MIDI channel 10, as mido numbers it (0-15)
REPORT_MAX_LINES = 200            # per section, so a bad file cannot bury you
EPS = 1e-6                        # seconds; below any tick, above float noise

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


class OrganError(Exception):
    """A problem with the organ definition, as opposed to with the song."""


# ----------------------------------------------------------------------------
# Organ definition
# ----------------------------------------------------------------------------

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
    percussion_pulse_ms: int = 50
    register_pulse_ms: int = 100
    register_stagger_ms: int = 60
    lead_in_ms: int = 1000
    settle_ms: int = 250
    reset_registers_at_start: bool = True
    reset_registers_at_end: bool = True


@dataclass
class Organ:
    name: str
    output_channel: int                 # zero-based internally; 1-16 in the file
    pitches: dict[int, int]             # score pitch      -> output slot
    percussion: dict[int, int]          # GM drum note     -> output slot
    registers: dict[int, Register]      # register note    -> set/reset slots
    register_track: str | None          # substring of the register track's name
    register_channel: int | None        # zero-based, or None
    timing: Timing

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
            pitches = {int(k): int(v) for k, v in (raw.get("pitches") or {}).items()}
            percussion = {int(k): int(v) for k, v in (raw.get("percussion") or {}).items()}
            registers = {}
            for k, v in (raw.get("registers") or {}).items():
                registers[int(k)] = Register(str(v["name"]), int(v["set"]), int(v["reset"]))
            rt = raw.get("register_track")
            rc = raw.get("register_channel")
            timing = Timing(**(raw.get("timing") or {}))
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise OrganError(f"malformed organ definition: {e}") from e
        organ = cls(
            name=name,
            output_channel=out_ch - 1,
            pitches=pitches,
            percussion=percussion,
            registers=registers,
            register_track=str(rt) if rt is not None else None,
            register_channel=int(rc) - 1 if rc is not None else None,
            timing=timing,
        )
        organ.validate()
        return organ

    def validate(self) -> None:
        if not 0 <= self.output_channel <= 15:
            raise OrganError("output_channel must be 1-16")
        if self.register_channel is not None and not 0 <= self.register_channel <= 15:
            raise OrganError("register_channel must be 1-16")

        # Every slot must be a MIDI note, and no slot may serve two purposes.
        # Several score pitches may share one slot (a substitute pipe); nothing
        # else may share anything.
        owner: dict[int, tuple[str, str]] = {}

        def claim(slot: int, category: str, label: str) -> None:
            if not 0 <= slot <= 127:
                raise OrganError(f"{label}: slot {slot} is not a MIDI note number (0-127)")
            prev = owner.get(slot)
            if prev is not None and not (prev[0] == "pitch" and category == "pitch"):
                raise OrganError(f"slot {slot} is used by both {prev[1]} and {label}")
            owner[slot] = (category, label)

        for pitch, slot in self.pitches.items():
            if not 0 <= pitch <= 127:
                raise OrganError(f"pitch {pitch} is not a MIDI note number (0-127)")
            claim(slot, "pitch", f"pitch {pitch}")
        for drum, slot in self.percussion.items():
            if not 0 <= drum <= 127:
                raise OrganError(f"percussion {drum} is not a MIDI note number (0-127)")
            claim(slot, "percussion", f"percussion {drum}")
        for note, reg in self.registers.items():
            if not 0 <= note <= 127:
                raise OrganError(f"register note {note} is not a MIDI note number (0-127)")
            if reg.set_slot == reg.reset_slot:
                raise OrganError(f"register {reg.name}: set and reset are the same slot")
            claim(reg.set_slot, "register", f"register {reg.name} set")
            claim(reg.reset_slot, "register", f"register {reg.name} reset")

        if self.registers and self.register_track is None and self.register_channel is None:
            raise OrganError("registers are defined but neither register_track nor "
                             "register_channel says where to find them in a song")

        t = self.timing
        for fld in ("min_note_ms", "min_gap_ms", "percussion_pulse_ms", "register_pulse_ms",
                    "register_stagger_ms", "lead_in_ms", "settle_ms"):
            if getattr(t, fld) < 0:
                raise OrganError(f"timing.{fld} must not be negative")
        if t.percussion_pulse_ms == 0 or t.register_pulse_ms == 0:
            raise OrganError("pulse lengths must be greater than zero")

    @property
    def all_slots(self) -> set[int]:
        slots = set(self.pitches.values()) | set(self.percussion.values())
        for reg in self.registers.values():
            slots.add(reg.set_slot)
            slots.add(reg.reset_slot)
        return slots


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
    kind: str         # "pitch" | "percussion" | "register"


class Report:
    """Every line carries the time it refers to, in *source* (DAW) seconds, so
    sections can be rendered chronologically and cross-referenced against the
    file the user is actually editing."""

    def __init__(self) -> None:
        self.sections: dict[str, list[tuple[float, str]]] = defaultdict(list)
        self.counts: Counter = Counter()
        self.tracks: list[tuple[str, int]] = []            # (name, raw note count)
        self.track_kinds: dict[str, Counter] = defaultdict(Counter)
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
        L.append(f"  pitched notes    {self.notes['pitch']}")
        L.append(f"  percussion hits  {self.notes['percussion']}")
        L.append(f"  register pulses  {self.notes['register']}")
        L.append(f"  dropped          {self.dropped}")
        L.append("")
        L.append("Tracks")
        for name, raw_count in self.tracks:
            kinds = self.track_kinds.get(name, Counter())
            parts = [f"{kinds[k]} {k}" for k in ("pitch", "percussion", "register") if kinds[k]]
            if kinds["dropped"]:
                parts.append(f"{kinds['dropped']} dropped")
            detail = ", ".join(parts) if parts else "nothing usable"
            L.append(f"  {name}: {raw_count} notes -> {detail}")
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

def is_register_note(note: RawNote, organ: Organ) -> bool:
    if organ.register_track and organ.register_track.lower() in note.track.lower():
        return True
    if organ.register_channel is not None and note.channel == organ.register_channel:
        return True
    return False


def map_notes(raw: list[RawNote], organ: Organ, report: Report) -> list[Interval]:
    t = organ.timing
    reg_pulse = t.register_pulse_ms / 1000
    perc_pulse = t.percussion_pulse_ms / 1000
    out: list[Interval] = []

    for n in raw:
        where = f"{fmt_time(n.start)}  {n.track}"
        if is_register_note(n, organ):
            reg = organ.registers.get(n.note)
            if reg is None:
                report.add("Dropped: note on the register track that is not a register",
                           n.start, f"{where}: {note_name(n.note)} ({n.note})")
                report.track_kinds[n.track]["dropped"] += 1
                continue
            # A register note spans the section it is engaged for. The
            # mechanism latches, so it gets a pulse on the way in and a pulse
            # to the other coil on the way out.
            out.append(Interval(reg.set_slot, n.start, n.start + reg_pulse, f"{reg.name} on", "register"))
            out.append(Interval(reg.reset_slot, n.end, n.end + reg_pulse, f"{reg.name} off", "register"))
            report.track_kinds[n.track]["register"] += 1

        elif n.channel == DRUM_CHANNEL:
            slot = organ.percussion.get(n.note)
            if slot is None:
                report.add("Dropped: percussion this organ does not have",
                           n.start, f"{where}: GM {n.note}")
                report.track_kinds[n.track]["dropped"] += 1
                continue
            # A drum is a strike, not a sustain: fixed pulse, authored length
            # ignored. DAW drum notes are frequently one tick long anyway.
            out.append(Interval(slot, n.start, n.start + perc_pulse, f"drum {n.note}", "percussion"))
            report.track_kinds[n.track]["percussion"] += 1

        else:
            slot = organ.pitches.get(n.note)
            if slot is None:
                report.add("Dropped: pitch this organ does not have",
                           n.start, f"{where}: {note_name(n.note)} ({n.note})")
                report.track_kinds[n.track]["dropped"] += 1
                continue
            out.append(Interval(slot, n.start, n.end, note_name(n.note), "pitch"))
            report.track_kinds[n.track]["pitch"] += 1

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

    # Lead-in and the register preamble. Pulsing every reset before the music
    # starts is what makes set/reset registers stateless: whatever happened
    # last time, they are all closed now. The music is shifted late enough
    # that the preamble is finished, with a gap, before anything else fires.
    lead_in = t.lead_in_ms / 1000
    preamble: list[Interval] = []
    if t.reset_registers_at_start and organ.registers:
        at = 0.0
        for key in sorted(organ.registers):
            reg = organ.registers[key]
            preamble.append(Interval(reg.reset_slot, at, at + pulse,
                                     f"{reg.name} reset (preamble)", "register"))
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
        min_for = {"pitch": t.min_note_ms, "percussion": t.percussion_pulse_ms,
                   "register": t.register_pulse_ms}
        last = max((max(iv.end, iv.start + min_for[iv.kind] / 1000) for iv in intervals), default=0.0)
        at = last + t.settle_ms / 1000
        for key in sorted(organ.registers):
            reg = organ.registers[key]
            intervals.append(Interval(reg.reset_slot, at, at + pulse,
                                      f"{reg.name} reset (postamble)", "register"))
            at += stagger

    by_slot: dict[int, list[Interval]] = defaultdict(list)
    for iv in intervals:
        by_slot[iv.slot].append(iv)

    min_note_for = {
        "pitch": t.min_note_ms / 1000,
        "percussion": t.percussion_pulse_ms / 1000,
        "register": t.register_pulse_ms / 1000,
    }
    final: list[Interval] = []
    for slot in sorted(by_slot):
        ivs = by_slot[slot]
        kind = ivs[0].kind
        final.extend(settle_slot(ivs, min_note_for[kind], t.min_gap_ms / 1000, report,
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
        description="Arrange a general MIDI file for a specific solenoid-driven organ.",
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

    if sum(report.notes.values()) == 0:
        print("warning: the arranged file contains no notes at all -- "
              "check the organ definition against this song", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
