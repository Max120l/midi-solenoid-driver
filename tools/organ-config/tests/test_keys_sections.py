"""The section view of organ_keys: blocks are ranks, keys walk up in pitch order."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import organ_keys as ok  # noqa: E402

RAW = {
    "solenoid_1_note": 0,
    "tracks": {
        "Registers": {"kind": "pulse", "notes": {122: 1, 121: 2},
                      "labels": {122: "Trombone Base on", 121: "Trombone Base off"}},
        "Drums": {"kind": "pulse", "notes": {35: 15, 21: 64, 38: 56, 39: 55},
                  "labels": {35: "Bass", 21: "Leader", 38: "Snare", 39: "Snare"}},
        "TrebCM": {"kind": "pitched", "notes": {84: 21, 86: 53}},
        "TenorCM": {"kind": "pitched", "notes": {60: 16, 62: 61}},
        "Accompainment": {"kind": "pitched", "notes": {36: 27, 41: 26, 48: 36, 50: 38, 47: 40},
                          "sections": {"Base": [36, 41], "Accompainment": [48, 50]}},
        "Melody": {"kind": "pitched", "notes": {72: 41, 74: 39, 76: 43, 77: 37, 78: 46, 79: 35, 81: 48,
                                                 82: 33, 83: 50, 84: 31, 86: 52, 88: 29, 89: 54}},
    },
}


class Sent:
    def __init__(self):
        self.msgs = []

    def __call__(self, m):
        self.msgs.append(m)

    def ons(self):
        return [m.note for m in self.msgs if m.type == "note_on"]


def section_console(view="section"):
    s = Sent()
    labels, first = ok.labels_from_organ(RAW)
    groups, slabels = ok.groups_from_organ(RAW)
    return ok.Console(send=s, labels=labels, sections=groups, section_labels=slabels, view=view), s


def test_groups_come_in_the_organs_order_and_each_in_pitch_order():
    groups, labels = ok.groups_from_organ(RAW)
    names = [n for n, _ in groups]
    # a note of a sectioned track that is in no section falls under the track's own name
    assert names == ["Melody", "Accompainment", "Base", "TenorCM", "TrebCM", "Drums", "Registers"]
    by = dict(groups)
    assert by["Melody"] == [41, 39, 43, 37, 46, 35, 48, 33, 50, 31, 52, 29, 54]     # C5 .. F6
    assert by["Base"] == [27, 26] and by["Accompainment"] == [40, 36, 38]           # B2 from the track, then C3 D3
    assert by["Drums"] == [64, 15, 56, 55]                                          # Leader, Bass, Snare, Snare
    assert by["Registers"] == [2, 1]                                                # off then on, as written
    assert sum(len(s) for s in by.values()) == 28
    assert labels[41] == "C5 b3" and labels[27] == "C2 b2" and labels[61] == "D4 b4"
    assert labels[56] == "Snare" and labels[1] == "Trom B+"


def test_keys_walk_up_the_selected_section_and_stop_at_its_end():
    c, s = section_console()
    assert c.current()[0] == "Melody"
    assert c.solenoid_for_key("1") == 41 and c.solenoid_for_key("8") == 33
    assert c.solenoid_for_key("q") == 50 and c.solenoid_for_key("t") == 54
    assert c.solenoid_for_key("y") is None                     # Melody has 13 pipes, no 14th key
    c.handle_key("c", 0.0)                                     # third block: Base
    assert c.current()[0] == "Base" and c.solenoid_for_key("2") == 26 and c.solenoid_for_key("3") is None
    c.handle_key("m", 0.0)                                     # seventh: Registers
    assert c.current()[0] == "Registers" and c.solenoid_for_key("1") == 2
    c.handle_key("\t", 0.0)
    assert c.current()[0] == "Melody"                          # Tab wraps over seven blocks
    assert c.key_for_solenoid(54) == "t" and c.key_for_solenoid(26) == "2"


def test_a_plays_the_sections_scale_in_pitch_order():
    c, s = section_console()
    c.handle_key("v", 0.0)                                     # TenorCM
    c.handle_key("a", 100.0)
    for t in range(0, 30):
        now = 100.0 + t * 0.1
        c.tick_scale(now)
        c.tick(now)
    assert s.ons() == [15, 60]                                 # solenoids 16, 61 -> notes 15, 60
    assert "TenorCM" in c.status


def test_s_switches_views_and_the_same_key_means_a_different_solenoid():
    c, _ = section_console(view="board")
    assert c.solenoid_for_key("1") == 1
    c.handle_key("x", 0.0)
    assert c.board == 1
    c.handle_key("s", 0.0)
    assert c.view == "section" and c.group == 0 and c.solenoid_for_key("1") == 41
    assert c.label(41) == "C5 b3"
    c.handle_key("s", 0.0)
    assert c.view == "board" and c.solenoid_for_key("1") == 1 and c.label(41) == "C5"


def test_b_n_m_only_select_blocks_that_exist():
    c, _ = section_console(view="board")
    c.handle_key("b", 0.0)
    assert c.board == 0 and "only 4 blocks" in c.status
    c.handle_key("s", 0.0)
    c.handle_key("b", 0.0)
    assert c.current()[0] == "TrebCM"


def test_s_without_an_organ_definition_explains_itself():
    c = ok.Console(send=lambda m: None)
    c.handle_key("s", 0.0)
    assert c.view == "board" and "--organ" in c.status
    assert c.groups()[0][0] == "board 1"


def test_render_by_section_shows_ranks_and_folds_the_others_when_the_terminal_is_short():
    c, _ = section_console()
    c.handle_key("x", 0.0)                                     # Accompainment
    c.handle_key("h", 0.0)
    c.handle_key("1", 0.0)                                     # holds solenoid 40
    lines = [t for t, _ in ok.render(c)]
    text = "\n".join(lines)
    assert "Accompainment   3 solenoids   <-- keys" in text
    assert "*40 1" in text and "B2 b3" in text
    assert "Melody   13 solenoids" in text and " 41  " in text  # other blocks drawn, no keys
    assert "by sections [s]" in lines[0]
    short = [t for t, _ in ok.render(c, 12)]
    assert len(short) == 4 + 7 + 2 + 1 < len(lines)             # head, seven headings, the open one-row block, status
    assert "Melody   13 solenoids" in "\n".join(short) and " 41  " not in "\n".join(short)
    assert "*40 1" in "\n".join(short)                         # the selected block stays open


def test_cli_view_section_needs_an_organ(capsys):
    import pytest
    with pytest.raises(SystemExit):
        ok.main(["--dry-run", "--view", "section"])
    assert "needs --organ" in capsys.readouterr().err
