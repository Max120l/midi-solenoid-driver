#!/usr/bin/env python3
"""
organ_transcribe -- arrange a general multi-track MIDI tune FOR the organ.

organ_arranger takes a file already written for the organ and makes it
playable. This tool is the step before: it takes an ordinary MIDI arrangement
-- any key, any instruments, notes anywhere -- and produces the organ-format
multi-track file the arranger expects, with every note on a pipe that exists.

It is mechanical arranging with musical heuristics, and every decision is
written to a report and to an editable plan, because taste belongs to the
arranger, not to a script. What it does:

  1. Reads the organ definition and derives its RANKS: each pitched track's
     available notes, split where a track has a large gap (Main's bass section
     and melody section are different ranks on the same track).
  2. Analyses the source: drops duplicate tracks, classifies the rest as
     melody, bass, accompaniment or counter-melody, and assigns each to a rank.
  3. Searches every transposition and picks the one whose notes best land on
     the pipes, weighting the melody and bass most.
  4. Folds each voice into its rank's compass an octave at a time, keeping
     lines coherent, and snaps (or drops) the few notes with no pipe.
  5. Thins chords to what a voice may hold, maps drums onto the organ's
     percussion, beats the leader's arm on every downbeat, and writes a simple
     registration.
  6. Runs the result through organ_arranger as a check: zero drops means every
     note has a pipe.

Usage:
    organ_transcribe.py TUNE.mid --organ organ.yaml [-o TUNE.fororgan.mid]
        [--plan PLAN.yaml] [--write-plan PLAN.yaml] [--transpose N|auto]
        [--out-of-scale snap|drop] [--report FILE] [--dry-run]

Start with --write-plan to see and edit the tool's choices, then re-run with
--plan. See README.md.
"""

from __future__ import annotations

import argparse
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import mido
import yaml

import organ_arranger as oa

__version__ = "0.1.0"

DRUM_CHANNEL = 9
RANK_GAP = 7                       # semitones of silence that split one track into two ranks
ONSET_GROUP_S = 0.010              # notes starting within this are one chord
LEADER_PULSE_S = 0.060
REGISTER_PULSE_S = 0.100
OUTPUT_TPB = 480

ROLE_MELODY, ROLE_BASS, ROLE_ACCOMP, ROLE_COUNTER, ROLE_DRUMS, ROLE_DROP = \
    "melody", "bass", "accomp", "counter", "drums", "drop"
ROLE_WEIGHT = {ROLE_MELODY: 3.0, ROLE_BASS: 2.0, ROLE_COUNTER: 1.0, ROLE_ACCOMP: 1.0}
ROLE_POLY = {ROLE_MELODY: 1, ROLE_BASS: 1, ROLE_COUNTER: 2, ROLE_ACCOMP: 3}

# GM drum note -> organ percussion label fragment
DEFAULT_DRUM_MAP = {35: "bass", 36: "bass", 38: "snare", 40: "snare", 37: "snare"}

# Output channel per organ track, mirroring the instrument's own sample file.
TRACK_CHANNEL = {"Main": 0, "TenorCM": 1, "TrebCM": 2, "Drums": 0, "Registers": 0}

NOTE_NAMES = oa.NOTE_NAMES
note_name = oa.note_name
fmt_time = oa.fmt_time


class TranscribeError(Exception):
    pass


# ----------------------------------------------------------------------------
# Ranks: what the organ can play, per region
# ----------------------------------------------------------------------------

@dataclass
class Rank:
    name: str                  # "Main:low", "TenorCM", ...
    track: str                 # organ track to emit on
    notes: list[int]           # available written notes, sorted
    lo: int
    hi: int
    center: float
    pcs: set[int]

    def nearest_note(self, pitch: int) -> int:
        return min(self.notes, key=lambda n: (abs(n - pitch), n))


def derive_ranks(organ: oa.Organ) -> dict[str, Rank]:
    ranks: dict[str, Rank] = {}
    for tname, track in organ.tracks.items():
        if track.kind != oa.KIND_PITCHED or not track.notes:
            continue
        notes = sorted(track.notes)
        regions: list[list[int]] = [[notes[0]]]
        for a, b in zip(notes, notes[1:]):
            if b - a >= RANK_GAP:
                regions.append([b])
            else:
                regions[-1].append(b)
        labels = ["low", "high"] if len(regions) == 2 else [str(i + 1) for i in range(len(regions))]
        for i, region in enumerate(regions):
            name = tname if len(regions) == 1 else f"{tname}:{labels[i]}"
            ranks[name] = Rank(name, tname, region, region[0], region[-1],
                               (region[0] + region[-1]) / 2, {n % 12 for n in region})
    if not ranks:
        raise TranscribeError("the organ has no pitched tracks to arrange for")
    return ranks


# ----------------------------------------------------------------------------
# Source analysis
# ----------------------------------------------------------------------------

@dataclass
class Note:
    start: float
    end: float
    pitch: int


@dataclass
class Source:
    key: str                    # "name#index", stable and unique
    name: str
    index: int
    channel: int
    notes: list[Note]
    median: float = 0.0
    poly: int = 1
    mean_dur: float = 0.0
    duplicate_of: str | None = None

    @property
    def count(self) -> int:
        return len(self.notes)

    @property
    def first(self) -> float:
        return min(n.start for n in self.notes)

    @property
    def last(self) -> float:
        return max(n.end for n in self.notes)


def read_source(mid: mido.MidiFile) -> tuple[list[Source], list[tuple[float, int]], tuple[int, int]]:
    """Sources with absolute-second notes, the tempo map (seconds, tempo), and time signature."""
    clock = oa.TickClock(mid)
    tempo_map = [(clock.secs[i], clock.tempos[i]) for i in range(len(clock.ticks))]
    timesig = (4, 4)
    for track in mid.tracks:
        for msg in track:
            if msg.type == "time_signature":
                timesig = (msg.numerator, msg.denominator)
                break

    sources: list[Source] = []
    for index, track in enumerate(mid.tracks):
        tick = 0
        opens: dict[tuple[int, int], list[float]] = defaultdict(list)
        notes: list[Note] = []
        chans: Counter = Counter()
        for msg in track:
            tick += msg.time
            if msg.type == "note_on" and msg.velocity > 0:
                opens[(msg.channel, msg.note)].append(clock.seconds(tick))
                chans[msg.channel] += 1
            elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                key = (msg.channel, msg.note)
                if opens[key]:
                    notes.append(Note(opens[key].pop(0), clock.seconds(tick), msg.note))
        end = clock.seconds(tick)
        for (_, pitch), starts in opens.items():
            for s in starts:
                notes.append(Note(s, end, pitch))
        if not notes:
            continue
        name = track.name or f"Track {index + 1}"
        src = Source(f"{name}#{index}", name, index, chans.most_common(1)[0][0], sorted(notes, key=lambda n: (n.start, n.pitch)))
        src.median = statistics.median(n.pitch for n in notes)
        src.mean_dur = statistics.mean(n.end - n.start for n in notes)
        events = sorted([(n.start, 1) for n in notes] + [(n.end, -1) for n in notes], key=lambda e: (e[0], e[1]))
        cur = mx = 0
        for _, d in events:
            cur += d
            mx = max(mx, cur)
        src.poly = mx
        sources.append(src)

    # Identical tracks (a doubled lead, a copy left in by the DAW) are noise.
    seen: dict[tuple, str] = {}
    for src in sources:
        sig = tuple((round(n.start, 4), round(n.end, 4), n.pitch) for n in src.notes)
        if sig in seen:
            src.duplicate_of = seen[sig]
        else:
            seen[sig] = src.key
    return sources, tempo_map, timesig


DRUM_NAMES = {"drums", "drum", "drum kit", "drumkit", "percussion", "perc", "kit"}


def is_drums(src: Source) -> bool:
    # Channel 10 is authoritative. A name is only trusted when it is literally
    # the drum track's name: "Steel Drums" is a melodic instrument, and the first
    # version of this treated it as a kit and threw the tune's riff away.
    return src.channel == DRUM_CHANNEL or src.name.strip().lower() in DRUM_NAMES


# ----------------------------------------------------------------------------
# The plan
# ----------------------------------------------------------------------------

@dataclass
class Voice:
    source: str
    rank: str                  # rank name, or "drop"
    role: str
    max_poly: int
    weight: float


@dataclass
class Plan:
    transpose: int | str                  # int or "auto"
    voices: list[Voice]
    drums_source: str | None
    drum_map: dict[int, str]              # GM note -> label fragment
    leader: str                           # "downbeat" | "none"
    registration: list[dict]              # [{"at": "start"|"melody"|seconds, "on": [...], "off": [...]}]

    def to_dict(self) -> dict:
        return {
            "transpose": self.transpose,
            "voices": [{"source": v.source, "rank": v.rank, "role": v.role,
                        "max_poly": v.max_poly, "weight": v.weight} for v in self.voices],
            "drums": {"source": self.drums_source, "map": dict(self.drum_map), "leader": self.leader},
            "registration": self.registration,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        try:
            voices = [Voice(str(v["source"]), str(v["rank"]), str(v.get("role", ROLE_COUNTER)),
                            int(v.get("max_poly", ROLE_POLY.get(v.get("role", ROLE_COUNTER), 2))),
                            float(v.get("weight", ROLE_WEIGHT.get(v.get("role", ROLE_COUNTER), 1.0))))
                      for v in d.get("voices", [])]
            drums = d.get("drums") or {}
            t = d.get("transpose", "auto")
            transpose = "auto" if str(t).lower() == "auto" else int(t)
            return cls(transpose, voices, drums.get("source"),
                       {int(k): str(v) for k, v in (drums.get("map") or DEFAULT_DRUM_MAP).items()},
                       str(drums.get("leader", "downbeat")), list(d.get("registration") or []))
        except (KeyError, TypeError, ValueError) as e:
            raise TranscribeError(f"malformed plan: {e}") from e


def auto_plan(sources: list[Source], ranks: dict[str, Rank], organ: oa.Organ) -> Plan:
    """Heuristics, written down so they can be argued with in the plan file."""
    live = [s for s in sources if s.duplicate_of is None]
    drums = sorted((s for s in live if is_drums(s)), key=lambda s: s.channel != DRUM_CHANNEL)
    pitched = [s for s in live if not is_drums(s)]
    voices: list[Voice] = []

    by_center = sorted(ranks.values(), key=lambda r: r.center)
    low_rank = by_center[0]
    high_rank = by_center[-1]

    # Bass: the lowest voice, if there is something genuinely low.
    bass = min(pitched, key=lambda s: s.median, default=None)
    if bass is not None and bass.median >= 55:
        bass = None
    if bass:
        voices.append(Voice(bass.key, low_rank.name, ROLE_BASS, ROLE_POLY[ROLE_BASS], ROLE_WEIGHT[ROLE_BASS]))
        pitched = [s for s in pitched if s is not bass]

    # Melody: the busiest reasonably-high line, favouring thin textures and
    # tracks named as such.
    def melody_score(s: Source) -> float:
        hint = 2.0 if any(w in s.name.lower() for w in ("lead", "melod", "solo", "tune", "vocal")) else 1.0
        return (s.count / max(1, s.poly)) * hint

    # The melody rank is the widest of the upper ranks, and on a band organ
    # that is the upper section of the main rank -- not the highest-pitched
    # rank, which is a ten-note counter-melody rank.
    upper = [r for r in ranks.values() if r.center >= 60] or list(ranks.values())
    melody_rank = max(upper, key=lambda r: (r.name.endswith(":high"), len(r.notes), r.center))
    candidates = [s for s in pitched if s.median >= 64] or pitched
    melody = max(candidates, key=melody_score, default=None)
    if melody:
        voices.append(Voice(melody.key, melody_rank.name, ROLE_MELODY, ROLE_POLY[ROLE_MELODY], ROLE_WEIGHT[ROLE_MELODY]))
        pitched = [s for s in pitched if s is not melody]

    # Accompaniment: chordal, lowish. Counter-melodies: the rest.
    accomp = [s for s in pitched if s.poly >= 3 and s.median < 64]
    counters = [s for s in pitched if s not in accomp]
    other_ranks = [r for r in by_center if r.name not in (melody_rank.name,)]
    if not other_ranks:
        other_ranks = by_center

    def nearest_rank(median: float, pool: list[Rank]) -> Rank:
        return min(pool, key=lambda r: abs(r.center - median))

    for s in accomp:
        voices.append(Voice(s.key, nearest_rank(s.median, other_ranks).name, ROLE_ACCOMP,
                            ROLE_POLY[ROLE_ACCOMP], ROLE_WEIGHT[ROLE_ACCOMP]))

    # Spread counters across the counter ranks by register, high to low, so
    # they do not all pile onto one ten-note rank.
    counter_ranks = [r for r in other_ranks if r.name != low_rank.name] or other_ranks
    counters.sort(key=lambda s: -s.median)
    for i, s in enumerate(counters):
        if len(counter_ranks) >= 2:
            rank = counter_ranks[-1] if i < (len(counters) + 1) // 2 else counter_ranks[0]
            rank = max(counter_ranks, key=lambda r: r.center) if i < (len(counters) + 1) // 2 else min(counter_ranks, key=lambda r: r.center)
        else:
            rank = counter_ranks[0]
        voices.append(Voice(s.key, rank.name, ROLE_COUNTER, ROLE_POLY[ROLE_COUNTER], ROLE_WEIGHT[ROLE_COUNTER]))

    registration = default_registration(organ)
    return Plan("auto", voices, drums[0].key if drums else None, dict(DEFAULT_DRUM_MAP),
                "downbeat", registration)


def register_names(organ: oa.Organ) -> list[str]:
    return [r.name for r in organ.registers]


def default_registration(organ: oa.Organ) -> list[dict]:
    """Soft stops for the intro; the loud ones when the melody enters."""
    names = register_names(organ)
    soft = [n for n in names if any(w in n.lower() for w in ("flute", "acc", "cello", "clarinet"))]
    loud = [n for n in names if any(w in n.lower() for w in ("violin", "trumpet", "trombone"))]
    plan = []
    if soft:
        plan.append({"at": "start", "on": soft})
    if loud:
        plan.append({"at": "melody", "on": loud})
    return plan


# ----------------------------------------------------------------------------
# Transposition
# ----------------------------------------------------------------------------

def coverage(voice_notes: list[Note], rank: Rank, shift: int) -> float:
    total = sum(n.end - n.start for n in voice_notes) or 1e-9
    hit = sum((n.end - n.start) for n in voice_notes if (n.pitch + shift) % 12 in rank.pcs)
    return hit / total


def choose_transposition(plan: Plan, sources: dict[str, Source], ranks: dict[str, Rank]) -> tuple[int, list[tuple[int, float]]]:
    scored: list[tuple[int, float]] = []
    for shift in range(-11, 12):
        score = wsum = 0.0
        for v in plan.voices:
            if v.rank == ROLE_DROP or v.source not in sources or v.rank not in ranks:
                continue
            score += v.weight * coverage(sources[v.source].notes, ranks[v.rank], shift)
            wsum += v.weight
        scored.append((shift, score / wsum if wsum else 0.0))
    best = max(scored, key=lambda s: (round(s[1], 6), -abs(s[0])))
    return best[0], sorted(scored, key=lambda s: (-s[1], abs(s[0])))


# ----------------------------------------------------------------------------
# Placing notes on pipes
# ----------------------------------------------------------------------------

@dataclass
class Placed:
    track: str
    note: int
    start: float
    end: float
    origin: str


@dataclass
class VoiceStats:
    kept: int = 0
    thinned: int = 0
    snapped: int = 0
    dropped: int = 0
    folded: int = 0


def thin_chords(notes: list[Note], max_poly: int, role: str) -> tuple[list[Note], int]:
    """Keep at most max_poly notes per onset: highest for a melody, lowest for a bass."""
    out: list[Note] = []
    removed = 0
    i = 0
    ordered = sorted(notes, key=lambda n: (n.start, n.pitch))
    while i < len(ordered):
        j = i
        while j < len(ordered) and ordered[j].start - ordered[i].start <= ONSET_GROUP_S:
            j += 1
        group = ordered[i:j]
        if len(group) > max_poly:
            group = sorted(group, key=lambda n: n.pitch, reverse=(role != ROLE_BASS))[:max_poly]
            removed += (j - i) - max_poly
        out.extend(group)
        i = j
    return sorted(out, key=lambda n: (n.start, n.pitch)), removed


def fold_voice(notes: list[Note], rank: Rank, shift: int, snap: bool, stats: VoiceStats,
               report: list[str], origin: str) -> list[Placed]:
    """Transpose, fold each note into the rank's compass near the line's last
    note, then snap to the nearest pipe (or drop) if there is none for it."""
    placed: list[Placed] = []
    prev = rank.center
    for n in sorted(notes, key=lambda n: (n.start, n.pitch)):
        p = n.pitch + shift
        candidates = [c for c in range(p % 12, 128, 12) if rank.lo <= c <= rank.hi]
        if not candidates:
            candidates = [rank.lo]
        # Prefer an octave that actually has this pipe: displacing a note by an
        # octave is far less wrong than snapping it to a neighbouring semitone.
        # Among those, stay close to the line's last note, with a gentle pull
        # toward the rank's centre so a line cannot drift away and stay there.
        target = min(candidates, key=lambda c: (c not in rank.notes,
                                                 abs(c - prev) + 0.3 * abs(c - rank.center)))
        if target != p:
            stats.folded += 1
        if target not in rank.notes:
            if not snap:
                stats.dropped += 1
                report.append(f"{fmt_time(n.start)}  {origin}: {note_name(target)} has no pipe on {rank.name}; dropped")
                continue
            snapped = rank.nearest_note(target)
            stats.snapped += 1
            report.append(f"{fmt_time(n.start)}  {origin}: {note_name(target)} -> {note_name(snapped)} (nearest pipe on {rank.name})")
            target = snapped
        prev = target
        placed.append(Placed(rank.track, target, n.start, n.end, origin))
        stats.kept += 1
    return placed


# ----------------------------------------------------------------------------
# Drums, leader, registration
# ----------------------------------------------------------------------------

def percussion_notes(organ: oa.Organ, fragment: str) -> list[int]:
    """Written notes on pulse tracks whose label contains the fragment."""
    hits = []
    for track in organ.tracks.values():
        if track.kind != oa.KIND_PULSE:
            continue
        for note, label in track.labels.items():
            if fragment.lower() in label.lower() and "regist" not in track.name.lower():
                hits.append((track.name, note))
    return hits


def map_drums(src: Source | None, plan: Plan, organ: oa.Organ, report: list[str]) -> tuple[list[Placed], Counter]:
    out: list[Placed] = []
    counts: Counter = Counter()
    if src is None:
        return out, counts
    targets: dict[str, list[tuple[str, int]]] = {}
    for gm, fragment in plan.drum_map.items():
        targets[fragment] = percussion_notes(organ, fragment)
    alternate: Counter = Counter()
    for n in src.notes:
        fragment = plan.drum_map.get(n.pitch)
        if fragment is None or not targets.get(fragment):
            counts["dropped"] += 1
            continue
        options = targets[fragment]
        track, note = options[alternate[fragment] % len(options)]     # two snares: alternate
        alternate[fragment] += 1
        out.append(Placed(track, note, n.start, n.start + LEADER_PULSE_S, f"{fragment} ({n.pitch})"))
        counts[fragment] += 1
    if counts["dropped"]:
        gm_dropped = Counter(n.pitch for n in src.notes if plan.drum_map.get(n.pitch) is None
                             or not targets.get(plan.drum_map.get(n.pitch)))
        report.append("dropped drum notes with no organ percussion: " +
                      ", ".join(f"GM {p} x{c}" for p, c in gm_dropped.most_common()))
    return out, counts


def leader_beats(organ: oa.Organ, first: float, last: float, tempo_map: list[tuple[float, int]],
                 timesig: tuple[int, int]) -> list[Placed]:
    targets = percussion_notes(organ, "leader")
    if not targets:
        return []
    track, note = targets[0]
    beats_per_bar = timesig[0] * 4 / timesig[1]
    out: list[Placed] = []
    # Walk bars through the tempo map from t=0.
    t = 0.0
    seg = 0
    while t < last - 1e-6:            # a downbeat exactly at the end is after the music
        if t >= first - 1e-6:
            out.append(Placed(track, note, t, t + LEADER_PULSE_S, "leader downbeat"))
        # advance one bar at the tempo in force
        while seg + 1 < len(tempo_map) and tempo_map[seg + 1][0] <= t + 1e-9:
            seg += 1
        t += beats_per_bar * tempo_map[seg][1] / 1e6
    return out


def register_note(organ: oa.Organ, name: str, state: str) -> tuple[str, int] | None:
    for track in organ.tracks.values():
        if track.kind != oa.KIND_PULSE or "regist" not in track.name.lower():
            continue
        for note, label in track.labels.items():
            m = label.lower().strip()
            if m.startswith(name.lower()) and m.endswith(" " + state):
                return track.name, note
    return None


def registration_events(plan: Plan, organ: oa.Organ, first: float, melody_first: float | None,
                        last: float, report: list[str]) -> list[Placed]:
    out: list[Placed] = []
    engaged: list[str] = []
    for step in plan.registration:
        at = step.get("at", "start")
        if at == "start":
            t = max(0.0, first - 0.5)
        elif at == "melody":
            t = (melody_first if melody_first is not None else first) - 0.25
        else:
            t = float(at)
        for name in step.get("on", []):
            hit = register_note(organ, name, "on")
            if hit is None:
                report.append(f"registration: no register named '{name}' on this organ")
                continue
            out.append(Placed(hit[0], hit[1], t, t + REGISTER_PULSE_S, f"{name} on"))
            engaged.append(name)
        for name in step.get("off", []):
            hit = register_note(organ, name, "off")
            if hit is None:
                report.append(f"registration: no register named '{name}' on this organ")
                continue
            out.append(Placed(hit[0], hit[1], t, t + REGISTER_PULSE_S, f"{name} off"))
            if name in engaged:
                engaged.remove(name)
    # Leave the organ as we found it.
    t = last + 0.5
    for i, name in enumerate(dict.fromkeys(engaged)):
        hit = register_note(organ, name, "off")
        if hit:
            out.append(Placed(hit[0], hit[1], t + i * 0.06, t + i * 0.06 + REGISTER_PULSE_S, f"{name} off"))
    return out


# ----------------------------------------------------------------------------
# Putting it together
# ----------------------------------------------------------------------------

@dataclass
class Result:
    mid: mido.MidiFile
    plan: Plan
    shift: int
    shifts: list[tuple[int, float]]
    sources: list[Source]
    voice_stats: dict[str, VoiceStats]
    drum_counts: Counter
    leader_count: int
    notes_out: Counter
    lines: list[str] = field(default_factory=list)
    check_dropped: int = 0
    check_report: str = ""


def transcribe(mid: mido.MidiFile, organ: oa.Organ, plan: Plan | None = None,
               snap: bool = True) -> Result:
    ranks = derive_ranks(organ)
    sources, tempo_map, timesig = read_source(mid)
    if not sources:
        raise TranscribeError("the source has no notes")
    by_key = {s.key: s for s in sources}
    if plan is None:
        plan = auto_plan(sources, ranks, organ)
    lines: list[str] = []

    for v in plan.voices:
        if v.source not in by_key:
            lines.append(f"plan: source '{v.source}' is not in this file; ignored")
        elif v.rank != ROLE_DROP and v.rank not in ranks:
            raise TranscribeError(f"plan: rank '{v.rank}' does not exist; ranks are {sorted(ranks)}")

    if plan.transpose == "auto":
        shift, shifts = choose_transposition(plan, by_key, ranks)
    else:
        shift = int(plan.transpose)
        _, shifts = choose_transposition(plan, by_key, ranks)

    placed: list[Placed] = []
    voice_stats: dict[str, VoiceStats] = {}
    melody_first: float | None = None
    for v in plan.voices:
        src = by_key.get(v.source)
        if src is None or v.rank == ROLE_DROP:
            continue
        stats = VoiceStats()
        notes, thinned = thin_chords(src.notes, v.max_poly, v.role)
        stats.thinned = thinned
        placed.extend(fold_voice(notes, ranks[v.rank], shift, snap, stats, lines, f"{src.name} ({v.role})"))
        voice_stats[v.source] = stats
        if v.role == ROLE_MELODY:
            melody_first = src.first if melody_first is None else min(melody_first, src.first)

    music_first = min((p.start for p in placed), default=0.0)
    music_last = max((p.end for p in placed), default=0.0)

    drum_src = by_key.get(plan.drums_source) if plan.drums_source else None
    drums, drum_counts = map_drums(drum_src, plan, organ, lines)
    placed.extend(drums)

    leader = leader_beats(organ, music_first, music_last, tempo_map, timesig) if plan.leader == "downbeat" else []
    placed.extend(leader)

    placed.extend(registration_events(plan, organ, music_first, melody_first, music_last, lines))

    out = build_output(placed, organ, tempo_map, timesig, mid.tracks[0].name if mid.tracks else "arranged")
    notes_out = Counter(p.track for p in placed)

    result = Result(out, plan, shift, shifts, sources, voice_stats, drum_counts, len(leader), notes_out, lines)
    # The arranger is the referee: if it drops nothing, every note has a pipe.
    _, check = oa.arrange(out, organ)
    result.check_dropped = check.dropped
    result.check_report = check.render(organ, "(transcribed)", "(check)")
    return result


def build_output(placed: list[Placed], organ: oa.Organ, tempo_map: list[tuple[float, int]],
                 timesig: tuple[int, int], title: str) -> mido.MidiFile:
    tempo = tempo_map[0][1] if tempo_map else 500_000
    tpb = OUTPUT_TPB

    def ticks(seconds: float) -> int:
        return int(round(mido.second2tick(seconds, tpb, tempo)))

    mid = mido.MidiFile(type=1, ticks_per_beat=tpb)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name=title, time=0))
    conductor.append(mido.MetaMessage("time_signature", numerator=timesig[0], denominator=timesig[1], time=0))
    conductor.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))
    conductor.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(conductor)

    order = [t for t in TRACK_CHANNEL if t in organ.tracks] + [t for t in organ.tracks if t not in TRACK_CHANNEL]
    for tname in order:
        events: list[tuple[int, int, int]] = []
        channel = TRACK_CHANNEL.get(tname, 0)
        for p in placed:
            if p.track != tname:
                continue
            s, e = ticks(p.start), ticks(p.end)
            if e <= s:
                e = s + 1
            events.append((s, 1, p.note))
            events.append((e, 0, p.note))
        events.sort()
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=tname, time=0))
        prev = 0
        for tick, on, note in events:
            if on:
                track.append(mido.Message("note_on", channel=channel, note=note, velocity=100, time=tick - prev))
            else:
                track.append(mido.Message("note_off", channel=channel, note=note, velocity=0, time=tick - prev))
            prev = tick
        track.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(track)
    return mid


# ----------------------------------------------------------------------------
# Report
# ----------------------------------------------------------------------------

def render_report(r: Result, organ: oa.Organ, ranks: dict[str, Rank], source: str, output: str) -> str:
    L = [f"organ_transcribe {__version__}", f"source : {source}", f"output : {output}", f"organ  : {organ.name}", ""]
    L.append("Ranks (what the organ can play)")
    for name, rk in sorted(ranks.items(), key=lambda kv: kv[1].center):
        L.append(f"  {name:<12} {note_name(rk.lo)}-{note_name(rk.hi)}  {len(rk.notes)} pipes  "
                 f"pitch classes {' '.join(NOTE_NAMES[p] for p in sorted(rk.pcs))}")
    L.append("")
    L.append("Source tracks")
    for s in r.sources:
        tag = f"duplicate of {s.duplicate_of.split('#')[0]}" if s.duplicate_of else ""
        L.append(f"  [{s.index}] {s.name:<28} ch{s.channel + 1:<3} {s.count:>5} notes  "
                 f"median {note_name(int(s.median)):<4} poly {s.poly}  {tag}")
    L.append("")
    L.append(f"Transposition: {r.shift:+d} semitones" + ("" if r.plan.transpose == "auto" else "  (from plan)"))
    for shift, score in r.shifts[:5]:
        L.append(f"  {shift:+3d}  coverage {score * 100:5.1f}%" + ("  <- chosen" if shift == r.shift else ""))
    L.append("")
    L.append("Voices")
    by_key = {s.key: s for s in r.sources}
    for v in r.plan.voices:
        s = by_key.get(v.source)
        st = r.voice_stats.get(v.source)
        if s is None or st is None:
            L.append(f"  {v.source:<32} -> {v.rank:<12} {v.role:<8} (not used)")
            continue
        L.append(f"  {s.name:<32} -> {v.rank:<12} {v.role:<8} kept {st.kept:>4}  "
                 f"thinned {st.thinned:>3}  folded {st.folded:>4}  snapped {st.snapped:>3}  dropped {st.dropped:>3}")
    if r.drum_counts:
        L.append("")
        L.append("Drums")
        for k, c in r.drum_counts.most_common():
            L.append(f"  {k:<12} {c}")
        L.append(f"  leader       {r.leader_count} downbeats")
    L.append("")
    L.append("Registration")
    for step in r.plan.registration:
        L.append(f"  at {str(step.get('at')):<8} on: {', '.join(step.get('on', [])) or '-'}"
                 + (f"   off: {', '.join(step['off'])}" if step.get("off") else ""))
    L.append("")
    L.append("Output")
    for t, c in sorted(r.notes_out.items()):
        L.append(f"  {t:<12} {c} notes")
    L.append("")
    L.append(f"Arranger check: {r.check_dropped} dropped" + ("  (every note has a pipe)" if r.check_dropped == 0 else "  <- LOOK"))
    if r.lines:
        L.append("")
        L.append(f"Notes ({len(r.lines)})")
        for line in r.lines[:oa.REPORT_MAX_LINES]:
            L.append("  " + line)
        if len(r.lines) > oa.REPORT_MAX_LINES:
            L.append(f"  ... and {len(r.lines) - oa.REPORT_MAX_LINES} more")
    return "\n".join(L) + "\n"


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="organ_transcribe",
                                description="Arrange a general multi-track MIDI tune for the organ.")
    p.add_argument("tune")
    p.add_argument("--organ", required=True)
    p.add_argument("-o", "--output", help="organ-format .mid to write (default: TUNE.fororgan.mid)")
    p.add_argument("--plan", help="plan YAML to follow instead of the automatic one")
    p.add_argument("--write-plan", metavar="FILE", help="write the plan used, for editing")
    p.add_argument("--transpose", default=None, help="semitones, or 'auto' (default: from plan, else auto)")
    p.add_argument("--out-of-scale", choices=("snap", "drop"), default="snap")
    p.add_argument("--report")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("-q", "--quiet", action="store_true")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    try:
        organ = oa.Organ.load(a.organ)
    except (oa.OrganError, OSError, yaml.YAMLError) as e:
        print(f"error: organ definition: {e}", file=sys.stderr)
        return 2
    source = Path(a.tune)
    try:
        mid = mido.MidiFile(str(source))
    except (OSError, ValueError, EOFError, KeyError, IndexError) as e:
        print(f"error: cannot read {source}: {e}", file=sys.stderr)
        return 2

    plan = None
    if a.plan:
        try:
            with open(a.plan, encoding="utf-8") as f:
                plan = Plan.from_dict(yaml.safe_load(f) or {})
        except (OSError, yaml.YAMLError, TranscribeError) as e:
            print(f"error: plan: {e}", file=sys.stderr)
            return 2
    try:
        if plan is None:
            ranks = derive_ranks(organ)
            sources, _, _ = read_source(mid)
            plan = auto_plan(sources, ranks, organ)
        if a.transpose is not None:
            plan.transpose = "auto" if a.transpose.lower() == "auto" else int(a.transpose)
        result = transcribe(mid, organ, plan, snap=(a.out_of_scale == "snap"))
    except TranscribeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    output = Path(a.output) if a.output else source.with_suffix(".fororgan.mid")
    report_path = Path(a.report) if a.report else output.with_suffix(".txt")
    text = render_report(result, organ, derive_ranks(organ), str(source), str(output))

    if not a.dry_run:
        result.mid.save(str(output))
        report_path.write_text(text, encoding="utf-8")
    if a.write_plan:
        Path(a.write_plan).write_text(
            "# Plan for organ_transcribe. Edit and re-run with --plan.\n"
            "# rank: one of the ranks listed in the report, or 'drop'.\n"
            "# role: melody | bass | counter | accomp.  transpose: semitones or auto.\n"
            + yaml.safe_dump(result.plan.to_dict(), sort_keys=False, default_flow_style=None),
            encoding="utf-8")
    if not a.quiet:
        print(text, end="")
    return 0 if result.check_dropped == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
