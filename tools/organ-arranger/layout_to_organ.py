#!/usr/bin/env python3
"""
layout_to_organ -- generate the organ definition from the layout spreadsheet.

The spreadsheet is the instrument's source of truth: one row per solenoid,
and a group of columns per DAW track saying which note on that track drives
it. This turns it into the organ.yaml the arranger reads. Edit the sheet,
re-run.

Layout: row 1 names the tracks, each name heading a group of columns that
runs until the next name; row 2 names the columns within each group; data
from row 3. Every group needs a `number` column (the note on that track).
Other columns are used when present:

    Solenoid | Main                  | TenorCM     | Drums        | Registers
             | number|note |section  | number|note | number|note  | number|instrument|section|action
    1        |                       |             |              | 122   |Trombone  |Base   |on
    2        |                       |             |              | 121   |Trombone  |Base   |off
    15       |                       |             | 35    |Bass  |
    16       |                       | 60    |C    |              |
    26       | 41    |F    |Base     |             |              |
    28       | 60    |C    |Accomp.  |             |              |

  - `section` on a pitched track divides it into ranks -- Main's Base,
    Accompaniment and Melody are three ranks on one track -- and is passed
    through so the transcriber can arrange for them.
  - Registers pair into set/reset coils by instrument and section, so two
    "Violin" registers on different sections stay distinct. The older
    single-column form, "Trombone on" / "Trombone off", is still accepted.
  - `note` on a pulse track (Bass, Snare, Leader) becomes its label.

Each solenoid row has exactly one (track, note) entry. Solenoid N drives
driver-board slot base_note + N - 1. This instrument's four boards sit at base
notes 0, 16, 32 and 48 -- board 1 with every switch open -- so solenoid 1 is
slot 0 and solenoid 64 is slot 63, and 0 is the default.

Tracks named like "drums" or "registers" become pulse tracks (a note is a
strike); everything else is pitched.

Usage:
    layout_to_organ.py LAYOUT.xlsx [-o organ.yaml] [--base-note 0] [--name "..."]
                                   [--pulse-track NAME=MS ...] [--sheet NAME]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import openpyxl
import yaml

__version__ = "0.2.0"

DEFAULT_PULSE_TRACKS = {"drum": 50, "regist": 100}    # name substring -> pulse ms
ACTION_RE = re.compile(r"^\s*(?P<name>.*?)\s+(?P<state>on|off)\s*$", re.IGNORECASE)


class LayoutError(Exception):
    pass


@dataclass
class Entry:
    solenoid: int
    track: str
    note: int
    fields: dict[str, str] = field(default_factory=dict)    # lower-cased column name -> text

    def get(self, *names: str) -> str | None:
        for n in names:
            v = self.fields.get(n)
            if v:
                return v
        return None


def _text(cell) -> str | None:
    if cell is None:
        return None
    s = str(cell).strip()
    return s or None


def read_layout(path: Path, sheet: str | None = None) -> tuple[list[str], list[Entry], list[str]]:
    """Returns (track names in sheet order, entries, warnings)."""
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    ws = wb[sheet] if sheet else wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    if len(rows) < 3:
        raise LayoutError("spreadsheet has no data rows")

    header, subheader = rows[0], rows[1]
    width = max(len(header), len(subheader))
    starts = [c for c in range(1, len(header)) if _text(header[c])]
    if not starts:
        raise LayoutError("no track names found in the header row")

    groups: list[tuple[str, dict[str, int]]] = []          # (track, column name -> index)
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else width
        cols: dict[str, int] = {}
        for c in range(start, end):
            name = _text(subheader[c]) if c < len(subheader) else None
            if name:
                cols[name.lower()] = c
        track = _text(header[start])
        if "number" not in cols:
            raise LayoutError(f"track '{track}': no 'number' column under it")
        groups.append((track, cols))

    entries: list[Entry] = []
    warnings: list[str] = []
    for r, row in enumerate(rows[2:], start=3):
        raw_sol = _text(row[0]) if row else None
        if raw_sol is None:
            continue
        try:
            solenoid = int(float(raw_sol))
        except ValueError:
            warnings.append(f"row {r}: solenoid '{raw_sol}' is not a number; skipped")
            continue
        found: list[Entry] = []
        for track, cols in groups:
            num = _text(row[cols["number"]]) if cols["number"] < len(row) else None
            if num is None:
                continue
            try:
                note = int(float(num))
            except ValueError:
                warnings.append(f"row {r}: solenoid {solenoid}, {track}: note '{num}' is not a number; skipped")
                continue
            fields = {name: _text(row[c]) for name, c in cols.items() if name != "number" and c < len(row)}
            found.append(Entry(solenoid, track, note, {k: v for k, v in fields.items() if v}))
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


def register_identity(e: Entry) -> tuple[str, str] | None:
    """(register name, 'on'|'off') for a register entry, from either layout."""
    instrument = e.get("instrument")
    if instrument:
        state = (e.get("action") or "").lower()
        if state not in ("on", "off"):
            return None
        section = e.get("section")
        name = f"{instrument} {section}" if section else instrument
        return name, state
    m = ACTION_RE.match(e.get("action", "note", "name") or "")
    if m:
        return m.group("name").strip(), m.group("state").lower()
    return None


def register_label(e: Entry) -> str | None:
    ident = register_identity(e)
    if ident:
        return f"{ident[0]} {ident[1]}"
    return e.get("action", "note", "name", "instrument")


def pair_registers(entries: list[Entry], slot_of) -> tuple[list[dict], list[str]]:
    """Set/reset pairs, one per register name."""
    warnings: list[str] = []
    by_name: dict[str, list[tuple[int, str, int]]] = defaultdict(list)   # name -> (note, state, slot)
    for e in entries:
        ident = register_identity(e)
        if ident is None:
            warnings.append(f"register solenoid {e.solenoid}: cannot tell what '{register_label(e)}' switches "
                            f"on or off; it will be a plain pulse with no reset pairing")
            continue
        by_name[ident[0]].append((e.note, ident[1], slot_of(e.solenoid)))

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
            # Both say the same thing -- a typo in the sheet. The layout's
            # convention is that the higher note is "on"; assume that and say so.
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
        is_registers = "regist" in track.lower()
        pulse = pulse_ms_for(track, pulse_overrides)
        notes: dict[int, list[int]] = defaultdict(list)
        labels: dict[int, str] = {}
        sections: dict[str, list[int]] = defaultdict(list)
        for e in sorted(tentries, key=lambda e: e.solenoid):
            notes[e.note].append(slot_of(e.solenoid))
            label = register_label(e) if is_registers else e.get("note", "name", "label")
            if label and (pulse is not None):          # pitched tracks' note names are not worth a label
                labels[e.note] = label
            section = e.get("section")
            if section and pulse is None and e.note not in sections[section]:
                sections[section].append(e.note)
        for note, slots in notes.items():
            if len(slots) > 1:
                warnings.append(f"track '{track}': note {note} drives {len(slots)} solenoids "
                                f"({', '.join(str(s - base_note + 1) for s in slots)}); "
                                f"kept as a doubled note -- if that is a typo, fix the sheet")
        tdef: dict = {"kind": "pulse" if pulse is not None else "pitched"}
        if pulse is not None:
            tdef["pulse_ms"] = pulse
        tdef["notes"] = {n: (s[0] if len(s) == 1 else s) for n, s in sorted(notes.items())}
        if labels:
            tdef["labels"] = dict(sorted(labels.items()))
        if sections:
            tdef["sections"] = {sec: sorted(ns) for sec, ns in sections.items()}
        tracks[track] = tdef

    registers: list[dict] = []
    for track in tracks:
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
        f"# `sections` divides a pitched track into the ranks the transcriber arranges\n"
        f"# for. `registers` lists set/reset coil pairs so the arranger can close every\n"
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
