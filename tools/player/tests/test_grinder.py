"""grinder: the queue, the timeline, and the run loop, without a serial port or a clock."""
import io
import random
import sys
from pathlib import Path

import mido
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import grinder as g  # noqa: E402


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------

def song_file(path: Path, notes, tempo_us=500_000, tpb=480):
    """notes: [(start_beats, length_beats, note), ...] -> a one-track file like the arranger's."""
    mid = mido.MidiFile(type=0, ticks_per_beat=tpb)
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("set_tempo", tempo=tempo_us, time=0))
    evs = []
    for start, length, note in notes:
        evs.append((int(start * tpb), 1, note))
        evs.append((int((start + length) * tpb), 0, note))
    evs.sort(key=lambda e: (e[0], e[1]))
    prev = 0
    for tick, on, note in evs:
        tr.append(mido.Message("note_on" if on else "note_off", channel=0, note=note,
                               velocity=100 if on else 0, time=tick - prev))
        prev = tick
    tr.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tr)
    mid.save(str(path))
    return path


class FakePort:
    def __init__(self):
        self.msgs = []
        self.interrupt_at = None          # raise KeyboardInterrupt on the nth write of a note_on

    def write(self, data):
        m = mido.Message.from_bytes(data)
        self.msgs.append(m)
        if self.interrupt_at is not None and m.type == "note_on" and m.velocity > 0:
            self.interrupt_at -= 1
            if self.interrupt_at == 0:
                raise KeyboardInterrupt

    def flush(self):
        pass

    def notes(self):
        return [(m.type, m.note) for m in self.msgs if m.type in ("note_on", "note_off")]


class FakeTime:
    """sleep() advances the clock; sleeps can be told to raise (Ctrl+C in a
    pause) or to run a hook (someone edits the queue while we sleep)."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []
        self.interrupt_sleeps = set()     # indexes of sleep calls that raise KeyboardInterrupt
        self.quit_sleeps = set()          # indexes of sleep calls that raise Quit (SIGTERM)
        self.hooks = {}                   # index -> callable run after that sleep

    def clock(self):
        return self.now

    def sleep(self, s):
        idx = len(self.slept)
        self.slept.append(round(s, 6))
        self.now += s
        if idx in self.hooks:
            self.hooks[idx]()
        if idx in self.interrupt_sleeps:
            raise KeyboardInterrupt
        if idx in self.quit_sleeps:
            raise g.Quit()
        if idx in getattr(self, "pause_sleeps", set()):
            raise g.Pause()


# ----------------------------------------------------------------------------
# tempo and the queue
# ----------------------------------------------------------------------------

def test_tempo_accepts_percent_factor_and_bare_percent_and_rejects_the_absurd():
    assert g.parse_tempo("90%") == pytest.approx(0.9)
    assert g.parse_tempo("1.1") == pytest.approx(1.1)
    assert g.parse_tempo("110") == pytest.approx(1.1)
    assert g.parse_tempo(1.0) == 1.0
    for bad in ("40%", "2", "fast", "160"):
        with pytest.raises(ValueError):
            g.parse_tempo(bad)


def test_a_folder_contributes_matching_files_under_it_in_name_order(tmp_path):
    (tmp_path / "waltzes").mkdir()
    (tmp_path / "marches").mkdir()
    for name in ("waltzes/skaters.organ.mid", "waltzes/blue-danube.organ.mid", "marches/bogey.organ.mid"):
        song_file(tmp_path / name, [(0, 1, 40)])
    song_file(tmp_path / "waltzes/skaters.mid", [(0, 1, 40)])          # a source file: not organ format
    songs = g.expand([tmp_path])
    assert [s.path.relative_to(tmp_path).as_posix() for s in songs] == [
        "marches/bogey.organ.mid", "waltzes/blue-danube.organ.mid", "waltzes/skaters.organ.mid"]
    assert [s.name for s in songs] == ["bogey", "blue-danube", "skaters"]
    only_waltzes = g.expand([tmp_path / "waltzes"])
    assert [s.name for s in only_waltzes] == ["blue-danube", "skaters"]
    everything = g.expand([tmp_path / "waltzes"], pattern="*.mid")
    assert len(everything) == 3
    with pytest.raises(FileNotFoundError):
        g.expand([tmp_path / "empty-or-missing"])
    (tmp_path / "nothing").mkdir()
    with pytest.raises(FileNotFoundError):
        g.expand([tmp_path / "nothing"])


def test_a_playlist_lists_files_and_folders_relative_to_itself_with_per_line_settings(tmp_path):
    song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    (tmp_path / "set").mkdir()
    song_file(tmp_path / "set/b.organ.mid", [(0, 1, 40)])
    song_file(tmp_path / "set/c.organ.mid", [(0, 1, 40)])
    pl = tmp_path / "evening.m3u"
    pl.write_text("# an evening\n\na.organ.mid | tempo=90% gap=6\nset\n" + str(tmp_path / "a.organ.mid") + "\n",
                  encoding="utf-8")
    songs = g.expand([pl])
    assert [s.name for s in songs] == ["a", "b", "c", "a"]
    assert songs[0].tempo == pytest.approx(0.9) and songs[0].gap == 6
    assert songs[1].tempo is None and songs[1].gap is None
    bad = tmp_path / "bad.txt"
    bad.write_text("a.organ.mid | speed=2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="bad.txt:1"):
        g.expand([bad])
    with pytest.raises(FileNotFoundError):
        g.expand([tmp_path / "missing.m3u"])


# ----------------------------------------------------------------------------
# the timeline
# ----------------------------------------------------------------------------

def test_timeline_scales_time_and_keeps_only_what_the_organ_understands(tmp_path):
    mid = mido.MidiFile(str(song_file(tmp_path / "s.organ.mid", [(0, 1, 40), (2, 1, 41)])))
    mid.tracks[0].insert(1, mido.Message("pitchwheel", channel=0, pitch=100, time=0))
    plain = g.timeline(mid)
    assert [(round(t, 3), m.type, m.note) for t, m in plain] == [
        (0.0, "note_on", 40), (0.5, "note_off", 40), (1.0, "note_on", 41), (1.5, "note_off", 41)]
    faster = g.timeline(mid, speed=1.25)
    assert [round(t, 3) for t, _ in faster] == [0.0, 0.4, 0.8, 1.2]


def test_a_faster_tempo_never_shortens_a_note_below_the_minimum_or_closes_the_gap(tmp_path):
    # 60 ms notes repeating every 100 ms, written; at 150 % they would be 40 ms with 27 ms gaps
    notes = [(i * 0.2, 0.12, 40) for i in range(4)]            # 0.2 beat = 100 ms, 0.12 beat = 60 ms
    mid = mido.MidiFile(str(song_file(tmp_path / "r.organ.mid", notes)))
    evs = g.timeline(mid, speed=1.5)
    ons = [t for t, m in evs if m.type == "note_on"]
    offs = [t for t, m in evs if m.type == "note_off"]
    assert [round(t, 4) for t in ons] == pytest.approx([0.0, 0.0667, 0.1333, 0.2], abs=1e-3)
    # strikes come every 66.7 ms, less than minimum note + minimum gap (80 ms), so
    # the note gives way: the minimum gap before the next strike is kept and the
    # note is as long as that leaves (36.7 ms), never shorter than half the interval
    for on, off, next_on in zip(ons, offs, ons[1:]):
        assert off == pytest.approx(max(next_on - g.MIN_GAP_S, on + (next_on - on) / 2), abs=1e-6)
        assert off < next_on                                   # never on top of the next strike
    last_on, last_off = ons[-1], offs[-1]
    assert last_off - last_on == pytest.approx(g.MIN_NOTE_S)   # the last note: the minimum, no next strike
    # with room to spare, the note keeps the minimum and the gap is whatever is left
    roomy = g.timeline(mido.MidiFile(str(song_file(tmp_path / "q.organ.mid", [(0, 0.12, 40), (0.4, 0.12, 40)]))),
                       speed=1.5)
    assert [round(t, 4) for t, _ in roomy] == pytest.approx([0.0, 0.05, 0.1333, 0.1833], abs=1e-3)
    # offs sort before ons at the same instant
    same = g.timeline(mido.MidiFile(str(song_file(tmp_path / "t.organ.mid", [(0, 1, 40), (1, 1, 40)]))))
    assert [m.type for _, m in same] == ["note_on", "note_off", "note_on", "note_off"]


def test_start_drops_what_came_before_and_restarts_the_clock(tmp_path):
    mid = mido.MidiFile(str(song_file(tmp_path / "s.organ.mid", [(0, 1, 40), (2, 1, 41), (4, 1, 42)])))
    # written: 40 at 0-0.5 s, 41 at 1.0-1.5 s, 42 at 2.0-2.5 s
    evs = g.timeline(mid, start=0.75)
    assert [(round(t, 3), m.note) for t, m in evs] == [(0.25, 41), (0.75, 41), (1.25, 42), (1.75, 42)]
    faster = g.timeline(mid, speed=4 / 3, start=0.75)          # start is measured at the new speed: 41 begins right there
    assert [(round(t, 3), m.note) for t, m in faster] == [(0.0, 41), (0.375, 41), (0.75, 42), (1.125, 42)]


def test_registration_before_start_is_the_last_pulse_of_each_pair(tmp_path):
    # registers: pair A = notes 0 (set) / 1 (reset); pair B = 2 / 3. Set A, set B, then reset A, all before the music
    notes = [(0, 0.1, 0), (0.2, 0.1, 2), (0.4, 0.1, 1), (4, 1, 40)]
    mid = mido.MidiFile(str(song_file(tmp_path / "s.organ.mid", notes)))
    assert g.registration_at(mid, 1.0, 1.5, [(0, 1), (2, 3)]) == [2, 1]     # in the order they were last touched
    assert g.registration_at(mid, 1.0, 0.0, [(0, 1), (2, 3)]) == []


def test_read_organ_gives_the_timing_and_the_register_notes(tmp_path):
    y = tmp_path / "organ.yaml"
    y.write_text("solenoid_1_note: 0\nregisters:\n- {name: T, set: 1, reset: 2}\n"
                 "timing: {min_note_ms: 60, min_gap_ms: 40}\n", encoding="utf-8")
    assert g.read_organ(str(y)) == (0.06, 0.04, [(0, 1)])


# ----------------------------------------------------------------------------
# playing
# ----------------------------------------------------------------------------

def test_play_events_waits_from_a_fixed_origin_so_delays_do_not_accumulate():
    ft = FakeTime()
    port = FakePort()
    evs = [(0.0, mido.Message("note_on", note=1)), (0.5, mido.Message("note_off", note=1)),
           (0.5, mido.Message("note_on", note=2)), (1.0, mido.Message("note_off", note=2))]
    ft.now = 1000.0
    real_sleep = ft.sleep

    def slow_sleep(s):                    # the machine oversleeps by 100 ms every time
        real_sleep(s + 0.1)
    g.Playback(evs).play(port, slow_sleep, ft.clock)
    assert [m.note for m in port.msgs] == [1, 1, 2, 2]
    # the first message is due at once (no sleep); the oversleep before the second
    # leaves the third already due (no sleep), and the last wait is shortened to 0.4
    assert ft.slept == pytest.approx([0.6, 0.5])


def test_run_plays_the_queue_with_gaps_and_per_song_tempo(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    b = song_file(tmp_path / "b.organ.mid", [(0, 1, 41)])
    songs = [g.Song(a), g.Song(b, tempo=0.5, gap=7)]
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    rc = g.run(songs, port, g.Settings(gap=3.0), ft.sleep, ft.clock, out)
    assert rc == 0
    assert port.notes() == [("note_on", 40), ("note_off", 40), ("note_on", 41), ("note_off", 41)]
    assert ft.slept == pytest.approx([0.5, 3.0, 1.0])            # a: 0.5 s note; gap 3; b at half speed: 1 s; no gap after the last
    text = out.getvalue()
    assert "[1/2] a  0:01  tempo 100%" in text and "[2/2] b  0:01  tempo 50%" in text and "Finished." in text
    assert sum(1 for m in port.msgs if m.type == "control_change" and m.control == 123) == 4   # silence around each song


def test_ctrl_c_in_a_song_skips_it_and_a_second_within_the_window_quits(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40), (2, 1, 40)])
    b = song_file(tmp_path / "b.organ.mid", [(0, 1, 41)])
    songs = [g.Song(a), g.Song(b)]
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    port.interrupt_at = 2                                        # Ctrl+C on a's second note
    rc = g.run(songs, port, g.Settings(gap=3.0), ft.sleep, ft.clock, out)
    assert rc == 0
    assert ("note_on", 41) in port.notes() and port.notes().count(("note_on", 40)) == 2
    assert "skipped at 0:01" in out.getvalue()
    assert g.QUIT_WINDOW_S in ft.slept and 3.0 not in ft.slept      # the quit window replaces the gap
    # again, but the second Ctrl+C lands in the window
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    port.interrupt_at = 2
    ft.interrupt_sleeps = {3}                # sleeps: 0.5 (note), 0.5 (to the 2nd note), 0.5 (its written end), then the window
    rc = g.run(songs, port, g.Settings(gap=3.0), ft.sleep, ft.clock, out)
    assert rc == 130 and ("note_on", 41) not in port.notes() and "Stopped." in out.getvalue()
    assert port.msgs[-1].type == "control_change" and port.msgs[-1].control == 123
    # hammered: the second Ctrl+C lands in the fade itself
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    port.interrupt_at = 2
    ft.interrupt_sleeps = {2}
    rc = g.run(songs, port, g.Settings(gap=3.0), ft.sleep, ft.clock, out)
    assert rc == 130 and "skipped" not in out.getvalue()
    assert port.msgs[-1].type == "control_change" and port.msgs[-1].control == 123


def test_ctrl_c_in_the_pause_quits(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    songs = [g.Song(a), g.Song(a)]
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    ft.interrupt_sleeps = {1}                                    # the gap after the first song
    assert g.run(songs, port, g.Settings(gap=3.0), ft.sleep, ft.clock, out) == 130
    assert port.notes().count(("note_on", 40)) == 1


def test_shuffle_reorders_per_round_and_repeat_goes_round_again(tmp_path):
    files = [song_file(tmp_path / f"{n}.organ.mid", [(0, 1, 40 + n)]) for n in range(5)]
    songs = [g.Song(f) for f in files]
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    ft.interrupt_sleeps = {19}                                   # quit during a pause in the second round
    rc = g.run(songs, port, g.Settings(gap=1.0, shuffle=True, repeat=True), ft.sleep, ft.clock, out,
               rng=random.Random(7))
    assert rc == 130
    played = [n for t, n in port.notes() if t == "note_on"]
    assert sorted(played[:5]) == [40, 41, 42, 43, 44]            # a round is a permutation
    assert len(played) == 10 and sorted(played[5:]) == [40, 41, 42, 43, 44]
    assert played[:5] != [40, 41, 42, 43, 44] or played[5:] != [40, 41, 42, 43, 44]


def test_an_unreadable_song_is_reported_and_the_queue_goes_on(tmp_path):
    bad = tmp_path / "bad.organ.mid"
    bad.write_bytes(b"not midi")
    good = song_file(tmp_path / "good.organ.mid", [(0, 1, 40)])
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    assert g.run([g.Song(bad), g.Song(good)], port, g.Settings(), ft.sleep, ft.clock, out) == 0
    assert "cannot play" in out.getvalue() and ("note_on", 40) in port.notes()


def test_a_skip_leaves_every_register_off_when_the_organ_is_known(tmp_path):
    notes = [(0, 0.1, 0), (1, 1, 40), (3, 1, 40)]                    # register 1 set, then two notes
    s = song_file(tmp_path / "s.organ.mid", notes)
    b = song_file(tmp_path / "b.organ.mid", [(0, 1, 41)])
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    port.interrupt_at = 3                                            # Ctrl+C on the second note
    cfg = g.Settings(gap=0.0, registers=[(0, 1), (2, 3)])
    assert g.run([g.Song(s), g.Song(b)], port, cfg, ft.sleep, ft.clock, out) == 0
    ons = [n for t, n in port.notes() if t == "note_on"]
    i = ons.index(41)
    assert ons[i - 2:i] == [1, 3]                                    # both reset coils before the next song
    assert "skipped" in out.getvalue()


def test_start_restores_the_registration_when_the_organ_is_known(tmp_path):
    notes = [(0, 0.1, 0), (0.4, 0.1, 3), (4, 1, 40), (6, 1, 41)]
    s = song_file(tmp_path / "s.organ.mid", notes)
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    cfg = g.Settings(start=2.5, registers=[(0, 1), (2, 3)])
    assert g.run([g.Song(s)], port, cfg, ft.sleep, ft.clock, out) == 0
    ons = [n for t, n in port.notes() if t == "note_on"]
    assert ons == [0, 3, 41]                                     # both registers re-pulsed, then the music from 2.5 s
    assert "from 0:03" in out.getvalue()


# ----------------------------------------------------------------------------
# skipping cleanly, the status file, the pump
# ----------------------------------------------------------------------------

def test_a_skip_lets_sounding_notes_end_where_written_within_the_fade_and_cuts_the_rest(tmp_path):
    # written: 40 at 0-0.2 s, 41 at 0-3 s, 42 at 0.5-1.0 s; Ctrl+C lands on 41's note_on
    s = song_file(tmp_path / "s.organ.mid", [(0, 0.4, 40), (0, 6, 41), (1, 1, 42)])
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    port.interrupt_at = 2
    assert g.run([g.Song(s)], port, g.Settings(), ft.sleep, ft.clock, out) == 0
    notes = port.notes()
    assert notes == [("note_on", 40), ("note_on", 41), ("note_off", 40)]     # 40 ends where written; 41 is cut; 42 never starts
    assert port.msgs[-1].type == "control_change" and port.msgs[-1].control == 123   # All Notes Off is the last thing
    assert 0.2 in ft.slept and g.QUIT_WINDOW_S in ft.slept
    assert "skipped at 0:00" in out.getvalue()


def test_a_skip_never_cuts_a_note_before_its_minimum_length(tmp_path):
    s = song_file(tmp_path / "s.organ.mid", [(0, 6, 41)])
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    port.interrupt_at = 1
    assert g.run([g.Song(s)], port, g.Settings(), ft.sleep, ft.clock, out) == 0
    assert ft.slept[0] == pytest.approx(g.MIN_NOTE_S)                          # the note gets its 50 ms first
    assert port.msgs[-1].type == "control_change" and port.msgs[-1].control == 123


class Recorder(g.Status):
    def __init__(self, path):
        super().__init__(path)
        self.seen = []

    def write(self, **fields):
        super().write(**fields)
        self.seen.append(dict(self.fields))


def test_status_file_follows_the_run_and_is_valid_json(tmp_path):
    import json
    a = song_file(tmp_path / "a.organ.mid", [(0, 5, 40)])                      # 2.5 s
    b = song_file(tmp_path / "b.organ.mid", [(0, 1, 41)])                      # 0.5 s
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    st = Recorder(tmp_path / "status.json")
    assert g.run([g.Song(a), g.Song(b)], port, g.Settings(gap=2.0), ft.sleep, ft.clock, out, status=st) == 0
    assert [(f["state"], f.get("position_s")) for f in st.seen] == [
        ("playing", 0.0), ("playing", 1.0), ("playing", 2.0), ("played", 2.5), ("pause", 2.5),
        ("playing", 0.0), ("played", 0.5), ("finished", 0.5)]
    assert ft.slept == pytest.approx([1.0, 1.0, 0.5, 2.0, 0.5])               # waits sliced for the ticks; the wire timing is unchanged
    last = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert last["state"] == "finished" and last["song"] == "b" and last["index"] == 2 and last["total"] == 2
    assert last["tempo"] == 1.0 and last["length_s"] == 0.5 and "time" in last
    assert not (tmp_path / "status.json.tmp").exists()


def test_status_ticks_keep_coming_when_the_music_is_dense(tmp_path):
    # sixteenth notes for 3 s: no single wait is ever as long as the status period
    notes = [(i * 0.25, 0.2, 40 + (i % 3)) for i in range(24)]
    s = song_file(tmp_path / "dense.organ.mid", notes)
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    st = Recorder(tmp_path / "status.json")
    assert g.run([g.Song(s)], port, g.Settings(), ft.sleep, ft.clock, out, status=st) == 0
    positions = [f["position_s"] for f in st.seen if f["state"] == "playing"]
    assert positions == [0.0, 1.0, 2.0]
    assert len([n for t, n in port.notes() if t == "note_on"]) == 24     # and nothing on the wire moved


class FakePump:
    def __init__(self):
        self.log = []

    def on(self):
        self.log.append("on")

    def off(self):
        self.log.append("off")


def test_the_pump_goes_on_before_the_warm_up_and_off_at_the_end_or_on_quit(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    ft, port, out, pump = FakeTime(), FakePort(), io.StringIO(), FakePump()
    assert g.run([g.Song(a)], port, g.Settings(warm_up=8.0), ft.sleep, ft.clock, out, pump=pump) == 0
    assert pump.log == ["on", "off"] and ft.slept[0] == 8.0
    assert "pump on" in out.getvalue() and "waiting 8 s for wind" in out.getvalue()
    # Ctrl+C during the warm-up: nothing plays, the pump still goes off
    ft, port, out, pump = FakeTime(), FakePort(), io.StringIO(), FakePump()
    ft.interrupt_sleeps = {0}
    assert g.run([g.Song(a)], port, g.Settings(warm_up=8.0), ft.sleep, ft.clock, out, pump=pump) == 130
    assert pump.log == ["on", "off"] and port.notes() == []
    # a warm-up with no pump pin is just the wait
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    assert g.run([g.Song(a)], port, g.Settings(warm_up=3.0), ft.sleep, ft.clock, out) == 0
    assert ft.slept == pytest.approx([3.0, 0.5]) and "pump" not in out.getvalue()


def test_cli_pump_without_gpiozero_explains_itself(tmp_path, monkeypatch, capsys):
    song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    monkeypatch.setitem(sys.modules, "gpiozero", None)
    assert g.main([str(tmp_path), "--dry-run", "--pump", "24"]) == 2
    assert "gpiozero" in capsys.readouterr().err
    assert g.main([str(tmp_path), "--dry-run", "--pump", "40"]) == 2
    assert g.main([str(tmp_path), "--dry-run", "--warm-up", "-1"]) == 2


# ----------------------------------------------------------------------------
# the live queue
# ----------------------------------------------------------------------------

def write_queue(path, entries):
    path.write_text("".join(f"{p} | id={i}\n" for i, p in entries), encoding="utf-8")


def test_watch_plays_what_is_added_while_it_idles_and_never_replays_an_id(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    b = song_file(tmp_path / "b.organ.mid", [(0, 1, 41)])
    c = song_file(tmp_path / "c.organ.mid", [(0, 1, 42)])
    q = tmp_path / "queue.m3u"
    write_queue(q, [(1, a), (2, b)])
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    st = Recorder(tmp_path / "status.json")
    # sleeps: a 0.5, gap 3 (b waiting), b 0.5, idle poll -> the front end appends c and drops the played lines
    ft.hooks[3] = lambda: write_queue(q, [(3, c)])
    ft.interrupt_sleeps = {5}                                    # Ctrl+C while idle again: quit
    rc = g.run_watch(q, port, g.Settings(gap=3.0), ft.sleep, ft.clock, out, status=st)
    assert rc == 130
    assert [n for t, n in port.notes() if t == "note_on"] == [40, 41, 42]
    assert ft.slept == pytest.approx([0.5, 3.0, 0.5, 1.0, 0.5, 1.0])   # no gap after b: nothing was waiting yet
    states = [f["state"] for f in st.seen]
    assert states.count("idle") == 2 and states[-1] == "stopped"
    assert [f.get("id") for f in st.seen if f["state"] == "playing"] == [1, 2, 3]
    assert "idle: waiting for the queue" in out.getvalue()
    assert port.msgs[-1].type == "control_change" and port.msgs[-1].control == 123


def test_watch_survives_a_missing_or_broken_file_and_repeat_goes_round(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    q = tmp_path / "queue.m3u"
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    ft.hooks[0] = lambda: q.write_text("a.organ.mid | speed=2\n", encoding="utf-8")   # broken line
    ft.hooks[1] = lambda: write_queue(q, [(1, a)])
    ft.quit_sleeps = {6}                                          # SIGTERM during a later pause
    rc = g.run_watch(q, port, g.Settings(gap=2.0, repeat=True), ft.sleep, ft.clock, out)
    assert rc == 130
    assert "queue:" in out.getvalue() and "speed" in out.getvalue()
    # idle (missing), idle (broken), then a plays; with repeat the same id plays again after each gap
    assert [n for t, n in port.notes() if t == "note_on"] == [40, 40, 40]
    assert ft.slept == pytest.approx([1.0, 1.0, 0.5, 2.0, 0.5, 2.0, 0.5])
    assert port.msgs[-1].type == "control_change" and port.msgs[-1].control == 123


def test_pause_lets_the_sounding_notes_end_and_resume_continues_from_there(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40), (2, 1, 41), (4, 1, 42), (6, 1, 43)])   # a note every second
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    st = Recorder(tmp_path / "status.json")
    # sleeps: 0.5 (40 off), 0.5 (41 on), 0.5 -> the pause lands as 41 is about to end
    ft.pause_sleeps = {2, 4}                                     # sleep 3 is the first idle poll of the pause; 4 resumes
    assert g.run([g.Song(a)], port, g.Settings(), ft.sleep, ft.clock, out, status=st) == 0
    assert [n for t, n in port.notes() if t == "note_on"] == [40, 41, 42, 43]     # nothing replayed
    states = [(f["state"], f["position_s"]) for f in st.seen]
    assert ("paused", 1.5) in states
    assert ("played", 3.5) in states and states[-1][0] == "finished"
    i = states.index(("paused", 1.5))
    assert states[i + 1] == ("playing", 1.5)                     # resumed where it stopped
    assert "paused at 0:02" in out.getvalue() and "resuming" in out.getvalue()
    # the silence after the pause: All Notes Off went out before the wait
    cc = [k for k, m in enumerate(port.msgs) if m.type == "control_change" and m.control == 123]
    on42 = next(k for k, m in enumerate(port.msgs) if m.type == "note_on" and m.note == 42)
    assert any(k < on42 for k in cc[1:])


def test_a_skip_while_paused_ends_the_song(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40), (2, 1, 41), (4, 1, 42)])
    b = song_file(tmp_path / "b.organ.mid", [(0, 1, 44)])
    ft, port, out = FakeTime(), FakePort(), io.StringIO()
    ft.pause_sleeps = {1}
    ft.interrupt_sleeps = {3}                                    # Ctrl+C / SIGUSR1 during the pause
    assert g.run([g.Song(a), g.Song(b)], port, g.Settings(gap=1.0), ft.sleep, ft.clock, out) == 0
    # the pause landed just before the second note would have started, so it never did; the skip moves on to b
    assert [n for t, n in port.notes() if t == "note_on"] == [40, 44]
    assert "skipped while paused" in out.getvalue()


def test_quit_from_outside_stops_cleanly_with_the_pump_off(tmp_path):
    a = song_file(tmp_path / "a.organ.mid", [(0, 4, 40)])
    ft, port, out, pump = FakeTime(), FakePort(), io.StringIO(), FakePump()
    ft.quit_sleeps = {0}                                          # SIGTERM in the middle of the note
    assert g.run([g.Song(a)], port, g.Settings(), ft.sleep, ft.clock, out, pump=pump) == 130
    assert pump.log == ["on", "off"] and "Stopped." in out.getvalue()
    assert port.msgs[-1].type == "control_change" and port.msgs[-1].control == 123


def test_cli_watch_wants_one_playlist_and_no_ordering_flags(tmp_path, capsys):
    a = song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    assert g.main([str(a), "--watch", "--dry-run"]) == 2
    assert "one playlist" in capsys.readouterr().err
    assert g.main([str(tmp_path / "q.m3u"), str(a), "--watch", "--dry-run"]) == 2
    assert g.main([str(tmp_path / "q.m3u"), "--watch", "--shuffle", "--dry-run"]) == 2
    assert "writer decides" in capsys.readouterr().err


# ----------------------------------------------------------------------------
# command line
# ----------------------------------------------------------------------------

def test_cli_lists_the_queue_and_rejects_bad_settings(tmp_path, capsys):
    song_file(tmp_path / "a.organ.mid", [(0, 1, 40)])
    song_file(tmp_path / "b.organ.mid", [(0, 1, 41)])
    assert g.main([str(tmp_path), "--list"]) == 0
    out = capsys.readouterr().out
    assert "1  " in out and "a.organ.mid" in out and "b.organ.mid" in out
    assert g.main([str(tmp_path), "--tempo", "300%"]) == 2
    assert "tempo" in capsys.readouterr().err
    assert g.main([str(tmp_path / "nope"), "--dry-run"]) == 2
    assert g.main([str(tmp_path), "--gap", "-1", "--dry-run"]) == 2


def test_cli_dry_run_plays_to_nowhere(tmp_path, monkeypatch, capsys):
    song_file(tmp_path / "a.organ.mid", [(0, 0.01, 40)])
    monkeypatch.setattr(g.time, "sleep", lambda s: None)
    assert g.main([str(tmp_path), "--dry-run", "--gap", "0"]) == 0
    out = capsys.readouterr().out
    assert "dry run: 1 song" in out and "Finished." in out
