#!/usr/bin/env python3
"""
preview_gm -- make an organ-format file listenable on a computer.

The transcriber's TUNE.fororgan.mid is the multi-track score: real pitches,
one track per organ track. Played through a General MIDI synth as is, every
track is a piano and the drums and register commands are piano notes too.
This writes TUNE.preview.mid with a sound per rank, the organ's drums moved
to the GM drum channel, and the register track left out:

    preview_gm.py TUNE.fororgan.mid                 # -> TUNE.preview.mid
    preview_gm.py TUNE.fororgan.mid --organ instrument/organ.yaml   # drum names from the definition

It is a preview, not the organ: pipes do not sound like GM patches, and the
arranger's merges, stretches and trims are not applied. For the real thing,
run organ_arranger and play the .organ.mid.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import mido

__version__ = "0.1.0"

# GM program per organ track (zero-based program numbers)
PROGRAMS = {
    "Melody": 21,        # Accordion: the melody pipes, reedy and bright
    "Accompainment": 24, # Nylon guitar: bass and accompaniment, so the oom-pah is audible apart
    "Main": 21,          # older definitions, before Main was split in two
    "TenorCM": 71,       # Clarinet
    "TrebCM": 72,        # Piccolo
}
DEFAULT_PROGRAM = 19     # Church Organ, for any other pitched track
GM_DRUM_CHANNEL = 9

# organ drum label fragment -> GM drum note
GM_DRUMS = {"bass": 36, "snare": 38, "leader": 76, "wood": 76, "cymbal": 49, "tri": 81}
DEFAULT_GM_DRUM = 37     # side stick, for anything unnamed

# the instrument's own drum notes, used when no organ.yaml is given
FALLBACK_DRUM_LABELS = {21: "Leader", 35: "Bass", 38: "Snare", 39: "Snare"}


def drum_labels_from_organ(path: str | None) -> dict[int, str]:
    if not path:
        return dict(FALLBACK_DRUM_LABELS)
    import yaml
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    for tname, tdef in (raw.get("tracks") or {}).items():
        if "drum" in tname.lower():
            return {int(k): str(v) for k, v in ((tdef or {}).get("labels") or {}).items()}
    return dict(FALLBACK_DRUM_LABELS)


def gm_drum_note(label: str) -> int:
    low = label.lower()
    for fragment, note in GM_DRUMS.items():
        if fragment in low:
            return note
    return DEFAULT_GM_DRUM


def preview(mid: mido.MidiFile, drum_labels: dict[int, str]) -> mido.MidiFile:
    out = mido.MidiFile(type=1, ticks_per_beat=mid.ticks_per_beat)
    for track in mid.tracks:
        name = track.name or ""
        if name.lower().startswith("regist"):
            continue
        new = mido.MidiTrack()
        is_drums = name.lower().startswith("drum")
        notes_in_track = [m for m in track if m.type in ("note_on", "note_off")]
        channel = GM_DRUM_CHANNEL if is_drums else (notes_in_track[0].channel if notes_in_track else 0)
        if notes_in_track and not is_drums:
            new.append(mido.Message("program_change", channel=channel,
                                    program=PROGRAMS.get(name, DEFAULT_PROGRAM), time=0))
        for m in track:
            if m.type in ("note_on", "note_off"):
                if is_drums:
                    m = m.copy(channel=GM_DRUM_CHANNEL, note=gm_drum_note(drum_labels.get(m.note, "")))
                else:
                    m = m.copy(channel=channel)
            new.append(m)
        out.tracks.append(new)
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="preview_gm", description="GM-playable preview of an organ-format file.")
    p.add_argument("score", help="TUNE.fororgan.mid from organ_transcribe (or any organ-format file)")
    p.add_argument("-o", "--output", help="file to write (default: TUNE.preview.mid)")
    p.add_argument("--organ", help="organ.yaml, for the drum names")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)
    src = Path(a.score)
    try:
        mid = mido.MidiFile(str(src))
        labels = drum_labels_from_organ(a.organ)
    except (OSError, ValueError, EOFError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    out = Path(a.output) if a.output else src.with_name(src.name.replace(".fororgan", "").rsplit(".", 1)[0] + ".preview.mid")
    preview(mid, labels).save(str(out))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
