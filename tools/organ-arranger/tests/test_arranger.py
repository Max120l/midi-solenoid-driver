"""
Tests for organ_arranger, against a small synthetic organ.

Source files are built at 480 ticks per beat and the MIDI default of 120 BPM,
so one second is exactly 960 ticks -- which keeps the expected timings legible.
"""

import sys
from pathlib import Path

import mido
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import organ_arranger as oa  # noqa: E402

TPB = 480
SECOND = 960          # ticks per second at 120 BPM, 480 tpb
TOL = 0.002           # output resolution is ~0.52 ms; allow a few ticks


# ----------------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------------

def organ(**timing_overrides) -> oa.Organ:
    timing = {
        "min_note_ms": 50, "min_gap_ms": 30,
        "percussion_pulse_ms": 50, "register_pulse_ms": 100,
        "register_stagger_ms": 60, "lead_in_ms": 1000, "settle_ms": 250,
        "reset_registers_at_start": True, "reset_registers_at_end": True,
    }
    timing.update(timing_overrides)
    return oa.Organ.from_dict({
        "name": "test",
        "output_channel": 1,
        "pitches": {60: 48, 62: 49, 64: 50},
        "percussion": {36: 60, 38: 61},
        "register_track": "Registers",
        "registers": {100: {"name": "Trumpet", "set": 70, "reset": 71}},
        "timing": timing,
    })


def track(name, events, tpb_unused=None):
    """events: iterable of (abs_tick, Message). Offs sort before ons at a tick."""
    ordered = sorted(events, key=lambda e: (e[0], 0 if e[1].type == "note_off" else 1))
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("track_name", name=name, time=0))
    prev = 0
    for tick, msg in ordered:
        tr.append(msg.copy(time=tick - prev))
        prev = tick
    tr.append(mido.MetaMessage("end_of_track", time=0))
    return tr


def note(channel, pitch, start, end):
    return [
        (start, mido.Message("note_on", channel=channel, note=pitch, velocity=90)),
        (end, mido.Message("note_off", channel=channel, note=pitch, velocity=0)),
    ]


def midi(*tracks, tempo_track_events=()):
    mid = mido.MidiFile(type=1, ticks_per_beat=TPB)
    conductor = mido.MidiTrack()
    conductor.append(mido.MetaMessage("track_name", name="Conductor", time=0))
    prev = 0
    for tick, msg in sorted(tempo_track_events, key=lambda e: e[0]):
        conductor.append(msg.copy(time=tick - prev))
        prev = tick
    conductor.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(conductor)
    mid.tracks.extend(tracks)
    return mid


def decode(out: mido.MidiFile):
    """(slot, start_s, end_s) for every note in the arranged file, sorted."""
    tr = out.tracks[0]
    tick = 0
    open_at = {}
    result = []
    for msg in tr:
        tick += msg.time
        s = mido.tick2second(tick, out.ticks_per_beat, oa.OUTPUT_TEMPO)
        if msg.type == "note_on":
            assert msg.note not in open_at, f"slot {msg.note} re-triggered while on"
            open_at[msg.note] = s
        elif msg.type == "note_off":
            result.append((msg.note, open_at.pop(msg.note), s))
    assert not open_at, f"slots left on at end: {sorted(open_at)}"
    return sorted(result)


def on_slot(decoded, slot):
    return [(s, e) for n, s, e in decoded if n == slot]


def music_only(decoded, org):
    """Drop the register preamble/postamble pulses so tests can look at music."""
    reset_slots = {r.reset_slot for r in org.registers.values()}
    return [(n, s, e) for n, s, e in decoded if n not in reset_slots]


# ----------------------------------------------------------------------------
# Mapping
# ----------------------------------------------------------------------------

def test_pitched_note_maps_to_its_slot_and_is_shifted_by_lead_in():
    org = organ()
    mid = midi(track("Melody", note(0, 60, 0, SECOND)))
    out, report = oa.arrange(mid, org)
    notes = on_slot(decode(out), 48)
    assert len(notes) == 1
    start, end = notes[0]
    assert start == pytest.approx(1.0, abs=TOL)
    assert end == pytest.approx(2.0, abs=TOL)
    assert report.notes["pitch"] == 1
    assert report.dropped == 0


def test_pitch_the_organ_lacks_is_dropped_and_reported():
    org = organ()
    mid = midi(track("Melody", note(0, 61, 0, SECOND)))
    out, report = oa.arrange(mid, org)
    assert music_only(decode(out), org) == []
    assert report.dropped == 1
    assert any("C#4" in line for _, line in report.sections["Dropped: pitch this organ does not have"])


def test_pulses_of_exactly_the_minimum_are_not_reported_as_stretched():
    # Regression: (start + 0.05) - start can land a hair under 0.05 in floating
    # point, which used to report every drum hit and register pulse as stretched.
    org = organ()
    events = []
    for i in range(20):
        events += note(9, 36, i * SECOND, i * SECOND + 1)
    mid = midi(track("Drums", events), track("Registers", note(0, 100, 0, 5 * SECOND)))
    _, report = oa.arrange(mid, org)
    assert report.counts["Stretched: note shorter than the solenoid can play"] == 0
    assert report.counts["Trimmed: shortened a note to leave a re-articulation gap"] == 0


def test_note_starting_exactly_where_the_last_ended_is_a_re_articulation_not_an_overlap():
    org = organ()
    mid = midi(track("Melody", note(0, 60, 0, SECOND) + note(0, 60, SECOND, 2 * SECOND)))
    _, report = oa.arrange(mid, org)
    assert report.counts["Merged: overlapping notes on one slot"] == 0
    assert report.counts["Trimmed: shortened a note to leave a re-articulation gap"] == 1


def test_report_uses_source_time_and_lists_chronologically():
    org = organ()   # 1 s lead-in, which must NOT appear in reported times
    mid = midi(
        track("Late", note(0, 61, 2 * SECOND, 3 * SECOND)),     # dropped, at 2 s
        track("Early", note(0, 61, 1 * SECOND, 2 * SECOND)),    # dropped, at 1 s
        track("Blip", note(0, 60, 3 * SECOND, 3 * SECOND + 5)), # stretched, at 3 s
    )
    _, report = oa.arrange(mid, org)
    text = report.render(org, "src", "dst")
    # Match the dropped-note lines ("<time>  <track>: C#4 (61)"), not the
    # Tracks summary lines that also mention the track names.
    dropped = [line for line in text.splitlines() if "Early: C#4" in line or "Late: C#4" in line]
    assert dropped[0].strip().startswith("0:01.000") and "Early" in dropped[0]
    assert dropped[1].strip().startswith("0:02.000") and "Late" in dropped[1]
    stretched = [line for line in text.splitlines() if "slot 48" in line]
    assert stretched and stretched[0].strip().startswith("0:03.000")   # not 0:04.000


def test_percussion_becomes_a_fixed_pulse_whatever_its_written_length():
    org = organ()
    mid = midi(track("Drums", note(9, 36, 0, 2 * SECOND)))   # two-second drum "note"
    out, _ = oa.arrange(mid, org)
    hits = on_slot(decode(out), 60)
    assert len(hits) == 1
    start, end = hits[0]
    assert end - start == pytest.approx(0.050, abs=TOL)


def test_unmapped_percussion_is_dropped():
    org = organ()
    mid = midi(track("Drums", note(9, 44, 0, 10)))
    out, report = oa.arrange(mid, org)
    assert music_only(decode(out), org) == []
    assert report.counts["Dropped: percussion this organ does not have"] == 1


def test_velocity_is_normalised_and_channel_is_the_output_channel():
    org = organ()
    mid = midi(track("Melody", note(5, 60, 0, SECOND)))      # authored on channel 6
    out, _ = oa.arrange(mid, org)
    ons = [m for m in out.tracks[0] if m.type == "note_on"]
    assert ons and all(m.channel == 0 and m.velocity == oa.OUTPUT_VELOCITY for m in ons)


# ----------------------------------------------------------------------------
# Registers
# ----------------------------------------------------------------------------

def test_register_note_becomes_set_pulse_at_on_and_reset_pulse_at_off():
    org = organ()
    mid = midi(track("Registers", note(0, 100, SECOND, 3 * SECOND)))
    out, report = oa.arrange(mid, org)
    d = decode(out)
    sets = on_slot(d, 70)
    assert len(sets) == 1
    assert sets[0][0] == pytest.approx(2.0, abs=TOL)          # 1 s + 1 s lead-in
    assert sets[0][1] - sets[0][0] == pytest.approx(0.100, abs=TOL)
    # reset slot: preamble at 0, the music's off at 3+1 = 4 s, postamble later
    resets = on_slot(d, 71)
    assert any(abs(s - 4.0) < TOL for s, _ in resets)
    assert report.notes["register"] >= 2


def test_register_track_is_matched_by_name_case_insensitively():
    org = organ()
    mid = midi(track("my REGISTERS here", note(0, 100, 0, SECOND)))
    out, report = oa.arrange(mid, org)
    assert on_slot(decode(out), 70)


def test_unknown_note_on_register_track_is_dropped_not_played_as_pitch():
    org = organ()
    # 60 is a valid pitch elsewhere, but on the register track it is not a register
    mid = midi(track("Registers", note(0, 60, 0, SECOND)))
    out, report = oa.arrange(mid, org)
    assert on_slot(decode(out), 48) == []
    assert report.counts["Dropped: note on the register track that is not a register"] == 1


def test_preamble_resets_every_register_before_the_music_starts():
    org = organ()
    mid = midi(track("Melody", note(0, 60, 0, SECOND)))
    out, report = oa.arrange(mid, org)
    d = decode(out)
    resets = on_slot(d, 71)
    assert resets[0][0] == pytest.approx(0.0, abs=TOL)
    assert resets[0][1] == pytest.approx(0.100, abs=TOL)
    first_music = min(s for n, s, e in music_only(d, org))
    assert first_music >= resets[0][1] + 0.030 - TOL
    assert report.lead_in_s == pytest.approx(1.0)


def test_postamble_resets_after_the_last_note_plus_settle():
    org = organ()
    mid = midi(track("Melody", note(0, 60, 0, SECOND)))
    out, _ = oa.arrange(mid, org)
    d = decode(out)
    last_music_end = max(e for n, s, e in music_only(d, org))
    post = [s for s, e in on_slot(d, 71) if s > last_music_end]
    assert len(post) == 1
    assert post[0] == pytest.approx(last_music_end + 0.250, abs=TOL)


def test_lead_in_grows_if_the_preamble_needs_more_room():
    # Seven registers at 60 ms stagger + 100 ms pulse = 460 ms, plus 30 ms gap,
    # is more than a 200 ms lead-in allows.
    regs = {100 + i: {"name": f"R{i}", "set": 70 + 2 * i, "reset": 71 + 2 * i} for i in range(7)}
    org = oa.Organ.from_dict({
        "name": "t", "pitches": {60: 48}, "register_track": "Registers",
        "registers": regs, "timing": {"lead_in_ms": 200},
    })
    mid = midi(track("Melody", note(0, 60, 0, SECOND)))
    out, report = oa.arrange(mid, org)
    assert report.lead_in_s == pytest.approx(0.36 + 0.100 + 0.030)
    first_music = min(s for n, s, e in music_only(decode(out), org))
    assert first_music == pytest.approx(report.lead_in_s, abs=TOL)


def test_resets_can_be_switched_off():
    org = organ(reset_registers_at_start=False, reset_registers_at_end=False)
    mid = midi(track("Melody", note(0, 60, 0, SECOND)))
    out, _ = oa.arrange(mid, org)
    assert on_slot(decode(out), 71) == []


# ----------------------------------------------------------------------------
# Making slots playable
# ----------------------------------------------------------------------------

def test_overlapping_notes_from_two_tracks_on_one_slot_are_merged():
    org = organ()
    mid = midi(
        track("A", note(0, 60, 0, SECOND)),
        track("B", note(1, 60, SECOND // 2, SECOND + SECOND // 2)),
    )
    out, report = oa.arrange(mid, org)
    notes = on_slot(decode(out), 48)
    assert len(notes) == 1
    assert notes[0][0] == pytest.approx(1.0, abs=TOL)
    assert notes[0][1] == pytest.approx(2.5, abs=TOL)
    assert report.counts["Merged: overlapping notes on one slot"] == 1


def test_short_note_is_stretched_to_the_minimum():
    org = organ()
    mid = midi(track("Melody", note(0, 60, 0, 10)))            # ~10 ms
    out, report = oa.arrange(mid, org)
    (start, end), = on_slot(decode(out), 48)
    assert end - start == pytest.approx(0.050, abs=TOL)
    assert report.counts["Stretched: note shorter than the solenoid can play"] == 1


def test_too_close_re_articulation_trims_the_earlier_note():
    org = organ()
    # second note starts 10 ms after the first ends; needs 30 ms of silence
    mid = midi(track("Melody", note(0, 60, 0, SECOND) + note(0, 60, SECOND + 10, 2 * SECOND)))
    out, report = oa.arrange(mid, org)
    a, b = on_slot(decode(out), 48)
    assert b[0] - a[1] == pytest.approx(0.030, abs=TOL)
    assert report.counts["Trimmed: shortened a note to leave a re-articulation gap"] == 1


def test_re_articulation_that_cannot_be_trimmed_is_merged():
    org = organ()
    # first note is already at the 50 ms minimum; trimming it is impossible
    mid = midi(track("Melody", note(0, 60, 0, 48) + note(0, 60, 58, SECOND)))
    out, report = oa.arrange(mid, org)
    notes = on_slot(decode(out), 48)
    assert len(notes) == 1
    assert notes[0][0] == pytest.approx(1.0, abs=TOL)
    assert notes[0][1] == pytest.approx(2.0, abs=TOL)
    assert report.counts["Merged: re-articulation too fast to play, joined into one note"] == 1


def test_no_slot_is_ever_retriggered_while_on():
    # decode() asserts this for every note; a dense file exercises it.
    org = organ()
    events = []
    for i in range(40):
        events += note(0, 60, i * 30, i * 30 + 25)
        events += note(1, 60, i * 30 + 12, i * 30 + 40)
    out, _ = oa.arrange(mid=midi(track("Dense", events)), organ=org)
    decode(out)


# ----------------------------------------------------------------------------
# Timing and file shape
# ----------------------------------------------------------------------------

def test_tempo_map_is_applied_when_placing_notes():
    org = organ(reset_registers_at_start=False, reset_registers_at_end=False, lead_in_ms=0)
    # 120 BPM for one beat, then 60 BPM. A note starting at tick 960 (two beats)
    # begins at 0.5 s + 1.0 s = 1.5 s, not 1.0 s.
    tempo_events = [
        (0, mido.MetaMessage("set_tempo", tempo=500_000)),
        (TPB, mido.MetaMessage("set_tempo", tempo=1_000_000)),
    ]
    mid = midi(track("Melody", note(0, 60, 2 * TPB, 3 * TPB)), tempo_track_events=tempo_events)
    out, _ = oa.arrange(mid, org)
    (start, end), = on_slot(decode(out), 48)
    assert start == pytest.approx(1.5, abs=TOL)
    assert end == pytest.approx(2.5, abs=TOL)


def test_output_is_type_0_single_track_on_known_slots_only():
    org = organ()
    mid = midi(
        track("Melody", note(0, 60, 0, SECOND) + note(0, 62, SECOND, 2 * SECOND)),
        track("Drums", note(9, 36, 0, 10)),
        track("Registers", note(0, 100, 0, 2 * SECOND)),
    )
    out, _ = oa.arrange(mid, org)
    assert out.type == 0
    assert len(out.tracks) == 1
    slots = {m.note for m in out.tracks[0] if m.type in ("note_on", "note_off")}
    assert slots <= org.all_slots


def test_stray_note_off_is_ignored_and_reported():
    org = organ()
    events = [(100, mido.Message("note_off", channel=0, note=60, velocity=0))]
    mid = midi(track("Melody", events))
    out, report = oa.arrange(mid, org)
    assert report.counts["Stray note-offs (ignored)"] == 1


def test_unterminated_note_is_closed_at_end_of_track_and_reported():
    org = organ()
    events = [(0, mido.Message("note_on", channel=0, note=60, velocity=90)),
              (SECOND, mido.Message("note_on", channel=0, note=62, velocity=90)),
              (2 * SECOND, mido.Message("note_off", channel=0, note=62, velocity=0))]
    mid = midi(track("Melody", events))
    out, report = oa.arrange(mid, org)
    (start, end), = on_slot(decode(out), 48)
    assert end == pytest.approx(3.0, abs=TOL)      # track ends at 2 s, plus lead-in
    assert report.counts["Unterminated notes (closed at end of track)"] == 1


# ----------------------------------------------------------------------------
# Organ definition validation
# ----------------------------------------------------------------------------

def test_slot_shared_between_categories_is_rejected():
    with pytest.raises(oa.OrganError, match="slot 48 is used by both"):
        oa.Organ.from_dict({"pitches": {60: 48}, "percussion": {36: 48}})


def test_two_pitches_may_share_a_slot():
    org = oa.Organ.from_dict({"pitches": {60: 48, 61: 48}})
    assert org.pitches == {60: 48, 61: 48}


def test_register_with_identical_set_and_reset_is_rejected():
    with pytest.raises(oa.OrganError, match="same slot"):
        oa.Organ.from_dict({"register_track": "R",
                            "registers": {100: {"name": "X", "set": 70, "reset": 70}}})


def test_registers_without_a_way_to_find_them_are_rejected():
    with pytest.raises(oa.OrganError, match="register_track"):
        oa.Organ.from_dict({"registers": {100: {"name": "X", "set": 70, "reset": 71}}})


def test_unknown_timing_key_is_rejected():
    with pytest.raises(oa.OrganError, match="malformed"):
        oa.Organ.from_dict({"pitches": {60: 48}, "timing": {"min_note_mss": 50}})


def test_slot_out_of_midi_range_is_rejected():
    with pytest.raises(oa.OrganError, match="not a MIDI note"):
        oa.Organ.from_dict({"pitches": {60: 128}})


# ----------------------------------------------------------------------------
# Command line, end to end
# ----------------------------------------------------------------------------

def test_cli_writes_arranged_file_and_report(tmp_path):
    import yaml
    org_path = tmp_path / "organ.yaml"
    org_path.write_text(yaml.safe_dump({
        "name": "cli", "pitches": {60: 48}, "register_track": "Registers",
        "registers": {100: {"name": "T", "set": 70, "reset": 71}},
    }), encoding="utf-8")
    song = tmp_path / "song.mid"
    midi(track("Melody", note(0, 60, 0, SECOND))).save(str(song))

    rc = oa.main([str(song), "--organ", str(org_path), "--quiet"])
    assert rc == 0
    out = tmp_path / "song.organ.mid"
    rep = tmp_path / "song.organ.txt"
    assert out.exists() and rep.exists()
    assert on_slot(decode(mido.MidiFile(str(out))), 48)
    text = rep.read_text(encoding="utf-8")
    assert "pitched notes    1" in text
    assert "Melody: 1 notes -> 1 pitch" in text


def test_cli_dry_run_writes_nothing(tmp_path):
    import yaml
    org_path = tmp_path / "organ.yaml"
    org_path.write_text(yaml.safe_dump({"pitches": {60: 48}}), encoding="utf-8")
    song = tmp_path / "song.mid"
    midi(track("Melody", note(0, 60, 0, SECOND))).save(str(song))
    rc = oa.main([str(song), "--organ", str(org_path), "--quiet", "--dry-run"])
    assert rc == 0
    assert not (tmp_path / "song.organ.mid").exists()


def test_cli_warns_when_nothing_survives(tmp_path):
    import yaml
    org_path = tmp_path / "organ.yaml"
    org_path.write_text(yaml.safe_dump({"pitches": {60: 48}}), encoding="utf-8")
    song = tmp_path / "song.mid"
    midi(track("Melody", note(0, 61, 0, SECOND))).save(str(song))   # not on the organ
    rc = oa.main([str(song), "--organ", str(org_path), "--quiet"])
    assert rc == 1


def test_cli_rejects_bad_organ_definition(tmp_path):
    org_path = tmp_path / "organ.yaml"
    org_path.write_text("pitches: {60: 48}\npercussion: {36: 48}\n", encoding="utf-8")
    song = tmp_path / "song.mid"
    midi(track("Melody", note(0, 60, 0, SECOND))).save(str(song))
    assert oa.main([str(song), "--organ", str(org_path), "--quiet"]) == 2
