import sys
from pathlib import Path

import mido

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import organ_keys as ok  # noqa: E402


class Sent:
    def __init__(self):
        self.msgs = []

    def __call__(self, m):
        self.msgs.append(m)

    def notes(self):
        return [(m.type, m.note) for m in self.msgs if m.type in ("note_on", "note_off")]


def console(**kw):
    s = Sent()
    return ok.Console(send=s, **kw), s


def test_keys_map_to_the_selected_boards_solenoids_in_two_rows_of_eight():
    c, _ = console()
    assert c.solenoid_for_key("1") == 1 and c.solenoid_for_key("8") == 8
    assert c.solenoid_for_key("q") == 9 and c.solenoid_for_key("i") == 16
    c.handle_key("c", 0.0)                       # board 3
    assert c.board == 2 and c.solenoid_for_key("1") == 33 and c.solenoid_for_key("i") == 48
    c.handle_key("\t", 0.0)
    assert c.board == 3
    c.handle_key("\t", 0.0)
    assert c.board == 0
    assert c.key_for_solenoid(64) == "i" and c.key_for_solenoid(57) == "q" and c.key_for_solenoid(49) == "1"
    assert c.solenoid_for_key("p") is None


def test_a_tap_pulses_the_solenoid_and_the_wire_note_is_solenoid_minus_one():
    c, s = console()
    c.handle_key("1", 10.0)
    assert s.notes() == [("note_on", 0)]                  # solenoid 1 is MIDI note 0
    c.tick(10.1)
    assert s.notes() == [("note_on", 0)]                  # 150 ms not yet up
    c.tick(10.2)
    assert s.notes() == [("note_on", 0), ("note_off", 0)]
    c.handle_key("x", 0.0)
    c.handle_key("q", 20.0)                               # board 2, solenoid 25 -> note 24
    assert s.notes()[-1] == ("note_on", 24)


def test_solenoid_1_note_shifts_the_wire():
    c, s = console(solenoid_1_note=48)
    c.handle_key("1", 0.0)
    assert s.notes() == [("note_on", 48)]


def test_hold_mode_toggles_and_leaving_it_switches_everything_off():
    c, s = console()
    c.handle_key("h", 0.0)
    c.handle_key("1", 0.0)
    c.handle_key("2", 0.0)
    c.tick(5.0)
    assert c.sounding == {1, 2} and ("note_off", 0) not in s.notes()
    c.handle_key("1", 6.0)                                # toggle off
    assert c.sounding == {2}
    c.handle_key("h", 7.0)                                # back to pulse mode: all off
    assert c.sounding == set()
    assert s.msgs[-1].type == "control_change" and s.msgs[-1].control == 123


def test_space_is_panic_and_quit_switches_off_first():
    c, s = console()
    c.handle_key("h", 0.0)
    c.handle_key("3", 0.0)
    c.handle_key(" ", 1.0)
    assert c.sounding == set() and s.notes()[-1] == ("note_off", 2)
    c.handle_key("4", 2.0)
    assert c.handle_key("Q", 3.0) is False
    assert c.sounding == set()


def test_pulse_length_adjusts_within_bounds():
    c, _ = console()
    c.handle_key("-", 0.0)
    assert c.pulse_ms == 100
    for _ in range(5):
        c.handle_key("-", 0.0)
    assert c.pulse_ms == ok.PULSE_MIN_MS
    for _ in range(100):
        c.handle_key("=", 0.0)
    assert c.pulse_ms == ok.PULSE_MAX_MS


def test_a_plays_the_boards_sixteen_in_a_row():
    c, s = console()
    c.handle_key("x", 0.0)
    c.handle_key("a", 100.0)
    for t in range(0, 70):
        now = 100.0 + t * 0.1
        c.tick_scale(now)
        c.tick(now)
    ons = [n for typ, n in s.notes() if typ == "note_on"]
    assert ons == list(range(16, 32))                     # solenoids 17-32 -> notes 16-31
    assert c.sounding == set()


def test_labels_come_from_the_organ_definition():
    raw = {
        "solenoid_1_note": 0,
        "tracks": {
            "Main": {"kind": "pitched", "notes": {36: 27, 48: 36, 72: 41},
                     "sections": {"Base": [36], "Accompainment": [48], "Melody": [72]}},
            "TenorCM": {"kind": "pitched", "notes": {60: 16}},
            "Drums": {"kind": "pulse", "notes": {35: 15, 21: 64}, "labels": {35: "Bass", 21: "Leader"}},
            "Registers": {"kind": "pulse", "notes": {122: 1, 121: 2, 110: 7},
                          "labels": {122: "Trombone Base on", 121: "Trombone Base off",
                                     110: "Violin Accompainment on"}},
        },
    }
    labels, first = ok.labels_from_organ(raw)
    assert first == 0
    assert labels[27] == "C2 Bas" and labels[36] == "C3 Acc" and labels[41] == "C5 Mel"
    assert labels[16] == "Tn C4"
    assert labels[15] == "Bass" and labels[64] == "Leader"
    assert labels[1] == "Trom B+" and labels[2] == "Trom B-" and labels[7] == "Viol A+"
    assert all(len(v) <= 8 for v in labels.values())


def test_render_shows_numbers_keys_labels_and_the_sounding_mark():
    c, _ = console(labels={1: "Trom B+", 36: "C3 Acc"})
    c.handle_key("h", 0.0)
    c.handle_key("1", 0.0)
    text = "\n".join(t for t, _ in ok.render(c))
    assert "board 1   solenoids 1-16   <-- keys" in text
    assert "* 1 1" in text and "Trom B+" in text          # sounding, numbered, keyed, labelled
    assert " 36  " in text and "C3 Acc" in text           # other boards: numbered and labelled, no key
    assert "HOLD" in text


def test_cli_rejects_a_bad_channel_and_unreadable_organ(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        ok.main(["--dry-run", "--channel", "17"])
    assert ok.main(["--dry-run", "--organ", str(tmp_path / "missing.yaml")]) == 2


def test_screen_loop_runs_against_a_fake_curses(monkeypatch):
    # The drawing code is exercised with a stand-in curses: keys "1", then "Q".
    import types
    keys = iter(["1", "Q"])

    class FakeScreen:
        def curs_set(self, *_): pass
        def nodelay(self, *_): pass
        def timeout(self, *_): pass
        def erase(self): pass
        def refresh(self): pass
        def getmaxyx(self): return (40, 100)
        def addnstr(self, y, x, text, n, attr=0): assert y < 40 and len(text[:n]) <= 100
        def get_wch(self):
            try:
                return next(keys)
            except StopIteration:
                raise fake.error

    fake = types.SimpleNamespace()
    fake.error = type("error", (Exception,), {})
    fake.A_NORMAL, fake.A_BOLD, fake.A_DIM, fake.A_REVERSE = 0, 1, 2, 4
    fake.COLOR_YELLOW, fake.COLOR_GREEN = 3, 2
    fake.has_colors = lambda: True
    fake.start_color = lambda: None
    fake.use_default_colors = lambda: None
    fake.init_pair = lambda *a: None
    fake.color_pair = lambda n: n << 8
    fake.curs_set = lambda *_: None
    fake.wrapper = lambda fn: fn(FakeScreen())
    monkeypatch.setitem(sys.modules, "curses", fake)
    c, s = console()
    ok.run_screen(c)
    assert s.notes()[0] == ("note_on", 0)                 # the tap
    assert s.msgs[-1].type == "control_change"            # quit switched everything off
