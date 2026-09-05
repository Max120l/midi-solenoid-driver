"""Tests for layout_to_organ, against a miniature of the real layout sheet."""

import sys
from pathlib import Path

import openpyxl
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import layout_to_organ as lo  # noqa: E402
import organ_arranger as oa   # noqa: E402


def make_sheet(path: Path, rows, header=("Solenoid", "Main", None, "TenorCM", None, "Drums ", None, "Registers", None)):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(list(header))
    ws.append([None, "number", "note", "number", "note", "number", "note", "number", "action"])
    for r in rows:
        ws.append(list(r))
    wb.save(path)
    return path


def sample(tmp_path):
    # Mirrors the shape of the real sheet, including its quirks: a trailing
    # space in a header, a doubled note, an empty solenoid, and a register
    # pair where both rows say "on".
    return make_sheet(tmp_path / "layout.xlsx", [
        (1, None, None, None, None, None, None, 122, "Trombone on"),
        (2, None, None, None, None, None, None, 121, "Trombone off"),
        (3, None, None, None, None, None, None, 107, "Trumpet on"),
        (4, None, None, None, None, None, None, 106, "Trumpet on"),      # typo: should be off
        (5, None, None, None, None, 25, "Bass", None, None),
        (6, None, None, 60, "C", None, None, None, None),
        (7, 55, "G", None, None, None, None, None, None),
        (8, 55, "G", None, None, None, None, None, None),                # doubled pipe
        (9, None, None, None, None, None, None, None, None),             # unused output
        (10, 60, "C", None, None, None, None, None, None),
    ])


def test_reads_tracks_in_sheet_order_and_strips_header_whitespace(tmp_path):
    order, entries, warnings = lo.read_layout(sample(tmp_path))
    assert order == ["Main", "TenorCM", "Drums", "Registers"]
    assert any("solenoid 9" in w and "unused" in w for w in warnings)


def test_slots_are_base_plus_solenoid_minus_one(tmp_path):
    order, entries, _ = lo.read_layout(sample(tmp_path))
    organ, _ = lo.build_organ(order, entries, base_note=48, name="t", pulse_overrides={})
    assert organ["tracks"]["TenorCM"]["notes"][60] == 48 + 6 - 1
    assert organ["tracks"]["Main"]["notes"][60] == 48 + 10 - 1


def test_same_note_number_on_two_tracks_stays_separate(tmp_path):
    order, entries, _ = lo.read_layout(sample(tmp_path))
    organ, _ = lo.build_organ(order, entries, 48, "t", {})
    assert organ["tracks"]["Main"]["notes"][60] != organ["tracks"]["TenorCM"]["notes"][60]


def test_doubled_note_becomes_a_list_and_is_warned_about(tmp_path):
    order, entries, _ = lo.read_layout(sample(tmp_path))
    organ, warnings = lo.build_organ(order, entries, 48, "t", {})
    assert organ["tracks"]["Main"]["notes"][55] == [48 + 7 - 1, 48 + 8 - 1]
    assert any("note 55 drives 2 solenoids" in w for w in warnings)


def test_drums_and_registers_are_pulse_tracks_with_their_own_lengths(tmp_path):
    order, entries, _ = lo.read_layout(sample(tmp_path))
    organ, _ = lo.build_organ(order, entries, 48, "t", {})
    assert organ["tracks"]["Drums"]["kind"] == "pulse" and organ["tracks"]["Drums"]["pulse_ms"] == 50
    assert organ["tracks"]["Registers"]["kind"] == "pulse" and organ["tracks"]["Registers"]["pulse_ms"] == 100
    assert organ["tracks"]["Main"]["kind"] == "pitched" and "pulse_ms" not in organ["tracks"]["Main"]


def test_register_pairs_come_from_the_action_labels(tmp_path):
    order, entries, _ = lo.read_layout(sample(tmp_path))
    organ, warnings = lo.build_organ(order, entries, 48, "t", {})
    regs = {r["name"]: r for r in organ["registers"]}
    assert regs["Trombone"] == {"name": "Trombone", "set": 48, "reset": 49}
    # both rows said "on": the higher note is assumed on, and it is flagged
    assert regs["Trumpet"] == {"name": "Trumpet", "set": 50, "reset": 51}
    assert any("Trumpet" in w and "both labelled" in w for w in warnings)


def test_labels_are_carried_through_for_the_report(tmp_path):
    order, entries, _ = lo.read_layout(sample(tmp_path))
    organ, _ = lo.build_organ(order, entries, 48, "t", {})
    assert organ["tracks"]["Registers"]["labels"][122] == "Trombone on"
    assert organ["tracks"]["Drums"]["labels"][25] == "Bass"


def test_generated_definition_loads_in_the_arranger(tmp_path):
    order, entries, _ = lo.read_layout(sample(tmp_path))
    organ, _ = lo.build_organ(order, entries, 48, "t", {})
    text = lo.render_yaml(organ, tmp_path / "layout.xlsx", 48)
    loaded = oa.Organ.from_dict(yaml.safe_load(text))
    assert loaded.find_track("Main").notes[55] == (54, 55)
    assert loaded.registers[0].name == "Trombone"


def test_pulse_track_override_and_cli(tmp_path):
    sample(tmp_path)
    out = tmp_path / "organ.yaml"
    rc = lo.main([str(tmp_path / "layout.xlsx"), "-o", str(out), "--pulse-track", "Main=75"])
    assert rc == 0
    d = yaml.safe_load(out.read_text(encoding="utf-8"))
    assert d["tracks"]["Main"]["kind"] == "pulse" and d["tracks"]["Main"]["pulse_ms"] == 75
    assert "GENERATED" in out.read_text(encoding="utf-8")


def test_a_solenoid_on_two_tracks_is_an_error(tmp_path):
    make_sheet(tmp_path / "bad.xlsx", [(1, 60, "C", 60, "C", None, None, None, None)])
    with pytest.raises(lo.LayoutError, match="several tracks"):
        lo.read_layout(tmp_path / "bad.xlsx")
