#!/usr/bin/env python3
"""
layout_to_organ -- generate the organ definition from the layout spreadsheet.

The spreadsheet is the instrument's source of truth: one row per solenoid,
and column pairs per DAW track saying which note on that track drives it.
This turns it into the organ.yaml the arranger reads. Edit the sheet, re-run.

Expected layout (header row 1 names the tracks, row 2 the sub-columns, data
from row 3):

    Solenoid | Main        | TenorCM     | ... | Drums        | Registers
             | number|note | number|note | ... | number|name  | number|action
    1        |             |             |     |              | 122  |Trombone on
    2        |             |             |     |              | 121  |Trombone off
    15       |             |             |     | 25   |Bass   |
    16       |             | 60    |C    |     |              |
    ...

Each solenoid row has exactly one (track, note) entry. Solenoid N drives
driver-board slot base_note + N - 1. This instrument's four boards sit at base
notes 0, 16, 32 and 48 -- board 1 with every switch open -- so solenoid 1 is
slot 0 and solenoid 64 is slot 63, and 0 is the default.

Tracks named like "drums" or "registers" become pulse tracks (a note is a
strike); everything else is pitched. Register set/reset pairs are read from
the action text, "<name> on" / "<name> off".

Usage:
    layout_to_organ.py LAYOUT.xlsx [-o organ.yaml] [--base-note 48] [--name "..."]
                                   [--pulse-track NAME=MS ...] [--sheet NAME]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import openpyxl
import yaml

__version__ = "0.1.0"

DEFAULT_PULSE_TRACKS = {"drum": 50, "regist": 100}    # name substring -> pulse ms
ACTION_RE = re.compile(r"^\s*(?P<name>.*?)\s+(?P<state>on|off)\s*$", re.IGNORECASE)


class LayoutError(Exception):
    pass


@dataclass
class Entry:
    solenoid: int
    track: str
    note: int
    label: str | None


def read_layout(path: Path, sheet: str | None = None) -> tuple[list[str], list[Entry], list[str]]:
    """Returns (track names in sheet order, entries, warnings)."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if len(rows) < 3:
        raise LayoutError("spreadsheet has no data rows")

    header = rows[0]
    groups: list[tuple[str, int, int]] = []          # (track name, number col, label col)
    for col, cell in enumerate(header):
        if col == 0 or cell is None or not str(cell).strip():
            continue
        groups.append((str(cell).strip(), col, col + 1))
    if not groups:
        raise LayoutError("no track names found in the header row")

    entries: list[Entry] = []
    warnings: list[str] = []
    for r, row in enumerate(rows[2:], start=3):
        raw_sol = row[0] if row else None
        if raw_sol is None or str(raw_sol).strip() == "":
            continue
        try:
            solenoid = int(float(str(raw_sol).strip()))
        except ValueError:
            warnings.append(f"row {r}: solenoid '{raw_sol}' is not a number; skipped")
            continue
        found = []
        for track, ncol, lcol in groups:
            num = row[ncol] if ncol < len(row) else None
            if num is None or str(num).strip() == "":
                continue
            try:
                note = int(float(str(num).strip()))
            except ValueError:
                warnings.append(f"row {r}: solenoid {solenoid}, {track}: note '{num}' is not a number; skipped")
                continue
            label = row[lcol] if lcol < len(row) else None
            label = str(label).strip() if label is not None and str(label).strip() else None
            found.append(Entry(solenoid, track, note, label))
        if not found:
            warnings.append(f"solenoid {solenoid}: no entry in any track (unused output)")
        elif len(found) > 1:
            which = ", ".join(f"{e.track} {e.note}" for e in found)
            raise LayoutError(f"solenoid {solenoid} has entries on several tracks: {which}")
        else:
            entries.append(found[0])
    return [g[0] for g in groups], entries, warnings


def pulse_ms_for(track: str, overrides: dict[str, int]) -> int | None:
    """Pulse length if this is a pulse track, else None."""
    for name, ms in overrides.items():
        if name.lower() == track.lower():
            return ms
    for fragment, ms in DEFAULT_PULSE_TRACKS.items():
        if fragment in track.lower():
            return ms
    return None


def pair_registers(entries: list[Entry], slot_of) -> tuple[list[dict], list[str]]:
    """Set/reset pairs from '<name> on' / '<name> off' labels."""
    warnings: list[str] = []
    by_name: dict[str, list[tuple[int, str | None, int]]] = defaultdict(list)   # name -> (note, state, slot)
    for e in entries:
        m = ACTION_RE.match(e.label or "")
        if m:
            by_name[m.group("name").strip()].append((e.note, m.group("state").lower(), slot_of(e.solenoid)))
        else:
            warnings.append(f"register solenoid {e.solenoid}: cannot read '{e.label}' as '<name> on/off'; "
                            f"it will be a plain pulse with no reset pairing")

    registers: list[dict] = []
    for name, items in by_name.items():
        if len(items) != 2:
            warnings.append(f"register '{name}': expected an on and an off, found {len(items)}; skipped")
            continue
        ons = [i for i in items if i[1] == "on"]
        offs = [i for i in items if i[1] == "off"]
        if len(ons) == 1 and len(offs) == 1:
            set_slot, reset_slot = ons[0][2], offs[0][2]
        else:
            # Both say the same thing -- a typo in the sheet. The convention in
            # the layout is that the higher note is "on"; assume that and say so.
            hi, lo = sorted(items, key=lambda i: i[0], reverse=True)
            set_slot, reset_slot = hi[2], lo[2]
            warnings.append(f"register '{name}': both labelled '{items[0][1]}'; assuming note {hi[0]} is on "
                            f"and note {lo[0]} is off -- check the sheet")
        registers.append({"name": name, "set": set_slot, "reset": reset_slot})
    return registers, warnings


def build_organ(track_order: list[str], entries: list[Entry], base_note: int, name: str,
                pulse_overrides: dict[str, int]) -> tuple[dict, list[str]]:
    warnings: list[str] = []

    def slot_of(solenoid: int) -> int:
        slot = base_note + solenoid - 1
        if not 0 <= slot <= 127:
            raise LayoutError(f"solenoid {solenoid} would be slot {slot}, outside 0-127; check --base-note")
        return slot

    by_track: dict[str, list[Entry]] = defaultdict(list)
    for e in entries:
        by_track[e.track].append(e)

    tracks: dict[str, dict] = {}
    for track in track_order:
        tentries = by_track.get(track, [])
        if not tentries:
            warnings.append(f"track '{track}': no solenoids assigned; omitted")
            continue
        notes: dict[int, list[int]] = defaultdict(list)
        labels: dict[int, str] = {}
        for e in sorted(tentries, key=lambda e: e.solenoid):
            notes[e.note].append(slot_of(e.solenoid))
            if e.label:
                labels[e.note] = e.label
        for note, slots in notes.items():
            if len(slots) > 1:
                warnings.append(f"track '{track}': note {note} drives {len(slots)} solenoids "
                                f"({', '.join(str(s - base_note + 1) for s in slots)}); "
                                f"kept as a doubled note -- if that is a typo, fix the sheet")
        tdef: dict = {}
        pulse = pulse_ms_for(track, pulse_overrides)
        tdef["kind"] = "pulse" if pulse is not None else "pitched"
        if pulse is not None:
            tdef["pulse_ms"] = pulse
        tdef["notes"] = {n: (s[0] if len(s) == 1 else s) for n, s in sorted(notes.items())}
        if labels:
            tdef["labels"] = dict(sorted(labels.items()))
        tracks[track] = tdef

    registers: list[dict] = []
    for track, tdef in tracks.items():
        if "regist" in track.lower():
            regs, w = pair_registers(by_track[track], slot_of)
            registers.extend(regs)
            warnings.extend(w)

    organ = {
        "name": name,
        "output_channel": 1,
        "tracks": tracks,
        "registers": registers,
        "timing": {
            "min_note_ms": 50,
            "min_gap_ms": 30,
            "pulse_ms": 50,
            "register_pulse_ms": 100,
            "register_stagger_ms": 60,
            "lead_in_ms": 1000,
            "settle_ms": 250,
            "reset_registers_at_start": True,
            "reset_registers_at_end": True,
        },
    }
    return organ, warnings


def render_yaml(organ: dict, source: Path, base_note: int) -> str:
    header = (
        f"# Organ definition for organ_arranger.\n"
        f"#\n"
        f"# GENERATED from {source.name} by layout_to_organ.py -- edit the spreadsheet\n"
        f"# and re-run rather than editing this file, or your changes will be lost.\n"
        f"#\n"
        f"# Slots are the MIDI notes the driver boards listen for: solenoid N is slot\n"
        f"# {base_note} + N - 1. Under each track, `notes` maps the note as written on\n"
        f"# that track in the DAW to the slot(s) it sounds. A pulse track treats every\n"
        f"# note as a strike of fixed length; a pitched track keeps written durations.\n"
        f"# `registers` lists set/reset coil pairs so the arranger can close every\n"
        f"# register before and after the music.\n"
        f"\n"
    )
    body = yaml.safe_dump(organ, sort_keys=False, default_flow_style=None, allow_unicode=True, width=100)
    return header + body


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="layout_to_organ",
                                description="Generate organ.yaml from the layout spreadsheet.")
    p.add_argument("layout", help="the .xlsx layout, one row per solenoid")
    p.add_argument("-o", "--output", help="organ.yaml to write (default: alongside the sheet)")
    p.add_argument("--base-note", type=int, default=0,
                   help="slot of solenoid 1; solenoid N is base + N - 1 (default 0: board 1 "
                        "with every switch open)")
    p.add_argument("--name", help="organ name for the definition (default: the sheet's file name)")
    p.add_argument("--sheet", help="worksheet name (default: the first)")
    p.add_argument("--pulse-track", action="append", default=[], metavar="NAME=MS",
                   help="force a track to be a pulse track with this pulse length; repeatable")
    p.add_argument("--dry-run", action="store_true", help="print the result, write nothing")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    overrides: dict[str, int] = {}
    for item in a.pulse_track:
        try:
            k, v = item.split("=", 1)
            overrides[k.strip()] = int(v)
        except ValueError:
            p.error(f"--pulse-track expects NAME=MS, got '{item}'")

    source = Path(a.layout)
    try:
        order, entries, warnings = read_layout(source, a.sheet)
        organ, more = build_organ(order, entries, a.base_note, a.name or source.stem, overrides)
        warnings += more
    except (LayoutError, OSError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    text = render_yaml(organ, source, a.base_note)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    if a.dry_run:
        print(text, end="")
        return 0
    out = Path(a.output) if a.output else source.with_name("organ.yaml")
    out.write_text(text, encoding="utf-8")
    n_slots = sum(len(v) if isinstance(v, list) else 1
                  for t in organ["tracks"].values() for v in t["notes"].values())
    print(f"wrote {out}: {len(organ['tracks'])} tracks, {n_slots} solenoids, "
          f"{len(organ['registers'])} registers, {len(warnings)} warnings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
