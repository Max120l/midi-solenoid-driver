#!/usr/bin/env python3
"""
make_scale -- a test file that plays one organ track's notes in order.

For commissioning: every pipe of a rank, one after another, long enough to
hear each speak and stop. Writes an organ-format multi-track file, so it goes
through organ_arranger like any song and comes out addressed to the slots.

    make_scale.py --organ instrument/organ.yaml --track Main -o scale-main.mid
    make_scale.py --organ instrument/organ.yaml --track Main --section Accompainment --section Melody
    make_scale.py --organ instrument/organ.yaml --track TenorCM --note-s 0.5 --descend
    make_scale.py --organ instrument/organ.yaml --track Drums --note-s 0.2

Pitched tracks play each note for --note-s; pulse tracks (drums, registers)
strike each note once and the length does not matter to the boards. Then:

    organ_arranger.py scale-main.mid --organ instrument/organ.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import mido

import organ_arranger as oa

__version__ = "0.1.0"

TPB = 480
TEMPO = 500_000          # 120 BPM: one beat is 0.5 s


def scale_notes(organ: oa.Organ, track_name: str, sections: list[str], descend: bool) -> list[int]:
    track = organ.find_track(track_name)
    if track is None:
        raise ValueError(f"no track '{track_name}' in the organ; tracks are {list(organ.tracks)}")
    if sections:
        chosen: set[int] = set()
        for s in sections:
            match = [k for k in track.sections if k.lower() == s.lower()]
            if not match:
                raise ValueError(f"track {track.name} has no section '{s}'; sections are {list(track.sections)}")
            chosen.update(track.sections[match[0]])
        notes = sorted(chosen)
    else:
        notes = sorted(track.notes)
    if descend:
        notes = notes + notes[-2::-1]
    return notes


def build(track_name: str, notes: list[int], note_s: float, gap_s: float, title: str) -> mido.MidiFile:
    mid = mido.MidiFile(type=1, ticks_per_beat=TPB)
    cond = mido.MidiTrack()
    cond.append(mido.MetaMessage("track_name", name=title, time=0))
    cond.append(mido.MetaMessage("set_tempo", tempo=TEMPO, time=0))
    cond.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(cond)

    on = int(round(mido.second2tick(note_s, TPB, TEMPO)))
    gap = int(round(mido.second2tick(gap_s, TPB, TEMPO)))
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("track_name", name=track_name, time=0))
    for i, n in enumerate(notes):
        tr.append(mido.Message("note_on", channel=0, note=n, velocity=100, time=0 if i == 0 else gap))
        tr.append(mido.Message("note_off", channel=0, note=n, velocity=0, time=max(1, on)))
    tr.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tr)
    return mid


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="make_scale", description="Write a test file playing one track's notes in order.")
    p.add_argument("--organ", required=True)
    p.add_argument("--track", required=True, help="organ track name, e.g. Main, TenorCM, Drums")
    p.add_argument("--section", action="append", default=[], help="limit to a section of the track; repeatable")
    p.add_argument("--note-s", type=float, default=1.0, help="seconds per note (default 1.0)")
    p.add_argument("--gap-s", type=float, default=0.1, help="silence between notes (default 0.1)")
    p.add_argument("--descend", action="store_true", help="come back down after reaching the top")
    p.add_argument("-o", "--output", help="file to write (default: scale-<track>.mid)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    try:
        organ = oa.Organ.load(a.organ)
        notes = scale_notes(organ, a.track, a.section, a.descend)
    except (oa.OrganError, OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    track = organ.find_track(a.track)
    title = f"{track.name} scale" + (f" ({', '.join(a.section)})" if a.section else "")
    out = Path(a.output) if a.output else Path(f"scale-{track.name.lower()}.mid")
    build(track.name, notes, a.note_s, a.gap_s, title).save(str(out))
    print(f"wrote {out}: {len(notes)} notes on {track.name}, {a.note_s:g} s each: {notes}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
