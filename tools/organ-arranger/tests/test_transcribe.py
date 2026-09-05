"""Tests for organ_transcribe against a small synthetic organ and tunes."""

import sys
from pathlib import Path

import mido
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import organ_arranger as oa    # noqa: E402
import organ_transcribe as ot  # noqa: E402

TPB = 480
BEAT = TPB           # 120 BPM default: one beat is 0.5 s


def organ() -> oa.Organ:
    return oa.Organ.from_dict({
        "name": "t",
        "tracks": {
            # low region C2..C4 (C D E F G A), gap, high region C5..C6 (C D E F G A C)
            "Main": {"notes": {36: 48, 41: 49, 43: 50, 48: 51, 50: 52, 52: 53, 53: 54, 55: 55, 57: 56, 60: 57,
                               72: 60, 74: 61, 76: 62, 77: 63, 79: 64, 81: 65, 84: 66}},
            "TenorCM": {"notes": {60: 70, 62: 71, 64: 72, 65: 73, 67: 74, 69: 75, 71: 76, 72: 77}},
            "TrebCM": {"notes": {84: 80, 86: 81, 88: 82, 89: 83, 91: 84, 93: 85, 95: 86, 96: 87}},
            "Drums": {"kind": "pulse", "pulse_ms": 50, "notes": {25: 100, 22: 101, 23: 102, 21: 103},
                      "labels": {25: "Bass", 22: "Snare", 23: "Snare", 21: "Leader"}},
            "Registers": {"kind": "pulse", "pulse_ms": 100,
                          "notes": {100: 110, 99: 111, 103: 112, 102: 113},
                          "labels": {100: "MEL flute on", 99: "MEL flute off",
                                     103: "MEL violin on", 102: "MEL violin off"}},
        },
        "registers": [{"name": "MEL flute", "set": 110, "reset": 111},
                      {"name": "MEL violin", "set": 112, "reset": 113}],
    })


def track(name, events, channel=0):
    ordered = sorted(events, key=lambda e: (e[0], 0 if e[1].type == "note_off" else 1))
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("track_name", name=name, time=0))
    prev = 0
    for tick, msg in ordered:
        tr.append(msg.copy(time=tick - prev))
        prev = tick
    tr.append(mido.MetaMessage("end_of_track", time=0))
    return tr


def notes(channel, seq, start=0, length=BEAT // 2, step=BEAT // 2):
    """seq: pitches, or lists of pitches for chords, played one after another."""
    ev = []
    t = start
    for item in seq:
        for p in (item if isinstance(item, (list, tuple)) else [item]):
            ev.append((t, mido.Message("note_on", channel=channel, note=p, velocity=100)))
            ev.append((t + length, mido.Message("note_off", channel=channel, note=p, velocity=0)))
        t += step
    return ev


def tune(*tracks):
    mid = mido.MidiFile(type=1, ticks_per_beat=TPB)
    cond = mido.MidiTrack()
    cond.append(mido.MetaMessage("track_name", name="TUNE", time=0))
    cond.append(mido.MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    cond.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    cond.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(cond)
    mid.tracks.extend(tracks)
    return mid


def out_notes(mid, track_name):
    for tr in mid.tracks:
        if tr.name == track_name:
            t = 0
            opens, res = {}, []
            for m in tr:
                t += m.time
                if m.type == "note_on":
                    opens.setdefault(m.note, []).append(t)
                elif m.type == "note_off":
                    res.append((opens[m.note].pop(0), t, m.note))
            return sorted(res)
    return []


# A tune in D major: melody D E F# G A, bass D A. The organ is C-major shaped,
# so a shift of -2 puts everything on pipes.
def d_major_tune():
    return tune(
        track("Lead", notes(0, [74, 76, 78, 79, 81, 79, 78, 76] * 4)),
        track("Bass", notes(1, [38, 45] * 16, length=BEAT, step=BEAT)),
        track("Pad", notes(2, [[50, 54, 57]] * 8, length=2 * BEAT, step=2 * BEAT)),
        track("Drums", notes(9, [36, 38, 36, 38] * 8, length=10), channel=9),
    )


# ----------------------------------------------------------------------------

def test_ranks_split_a_track_at_its_gap():
    ranks = ot.derive_ranks(organ())
    assert set(ranks) == {"Main:low", "Main:high", "TenorCM", "TrebCM"}
    assert ranks["Main:low"].lo == 36 and ranks["Main:low"].hi == 60
    assert ranks["Main:high"].lo == 72 and ranks["Main:high"].hi == 84
    assert ranks["Main:high"].pcs == {0, 2, 4, 5, 7, 9}


def test_duplicate_tracks_are_detected():
    mid = tune(track("A", notes(0, [60, 62, 64])), track("A copy", notes(3, [60, 62, 64])))
    sources, _, _ = ot.read_source(mid)
    assert sources[1].duplicate_of == sources[0].key


def test_auto_plan_finds_bass_melody_accomp_and_drums():
    org = organ()
    sources, _, _ = ot.read_source(d_major_tune())
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    roles = {v.source.split("#")[0]: (v.role, v.rank) for v in plan.voices}
    assert roles["Lead"] == ("melody", "Main:high")
    assert roles["Bass"] == ("bass", "Main:low")
    assert roles["Pad"][0] == "accomp"
    assert plan.drums_source.startswith("Drums#")


def test_transposition_search_finds_the_key_that_fits():
    r = ot.transcribe(d_major_tune(), organ())
    assert r.shift == -2
    assert r.shifts[0][1] == pytest.approx(1.0)


def test_melody_lands_in_its_rank_in_scale():
    r = ot.transcribe(d_major_tune(), organ())
    main = [n for _, _, n in out_notes(r.mid, "Main")]
    high = [n for n in main if n >= 72]
    assert high and set(high) <= {72, 74, 76, 77, 79, 81, 84}
    assert 72 <= min(high) and max(high) <= 84


def test_bass_lands_low_and_in_scale():
    r = ot.transcribe(d_major_tune(), organ())
    low = [n for _, _, n in out_notes(r.mid, "Main") if n < 72]
    assert low and set(low) <= {36, 41, 43, 48, 50, 52, 53, 55, 57, 60}


def test_out_of_scale_notes_are_snapped_or_dropped_as_asked():
    # Force no transposition: F# (78) has no pipe on Main:high.
    org = organ()
    mid = tune(track("Lead", notes(0, [72, 78, 72, 78] * 4)))
    sources, _, _ = ot.read_source(mid)
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    plan.transpose = 0
    snapped = ot.transcribe(mid, org, plan, snap=True)
    dropped = ot.transcribe(mid, org, plan, snap=False)
    s_stats = list(snapped.voice_stats.values())[0]
    d_stats = list(dropped.voice_stats.values())[0]
    assert s_stats.snapped == 8 and s_stats.dropped == 0
    assert d_stats.dropped == 8 and d_stats.snapped == 0
    assert snapped.check_dropped == 0 and dropped.check_dropped == 0


def test_chords_are_thinned_to_the_voice_polyphony():
    r = ot.transcribe(d_major_tune(), organ())
    pad = next(v for v in r.plan.voices if v.source.startswith("Pad"))
    assert pad.max_poly == 3
    lead = next(v for v in r.plan.voices if v.source.startswith("Lead"))
    org = organ()
    mid = tune(track("Lead", notes(0, [[72, 76, 79]] * 4)))
    sources, _, _ = ot.read_source(mid)
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    r2 = ot.transcribe(mid, org, plan)
    st = list(r2.voice_stats.values())[0]
    assert st.thinned == 8 and st.kept == 4         # melody keeps one note per chord


def test_drums_map_to_organ_percussion_and_snares_alternate():
    r = ot.transcribe(d_major_tune(), organ())
    drums = out_notes(r.mid, "Drums")
    kinds = [n for _, _, n in drums]
    assert kinds.count(25) == 16                     # kicks -> Bass
    assert kinds.count(22) == 8 and kinds.count(23) == 8   # snares alternate
    assert r.drum_counts["bass"] == 16 and r.drum_counts["snare"] == 16


def test_leader_beats_every_downbeat_while_music_plays():
    r = ot.transcribe(d_major_tune(), organ())
    leader = [(s, e) for s, e, n in out_notes(r.mid, "Drums") if n == 21]
    # The bass plays [38, 45] * 16 = 32 one-beat notes = 8 bars of 4/4 at
    # 120 BPM (2 s per bar): downbeats at 0, 2, ... 14 s, and none at 16 s,
    # which is where the music ends.
    assert len(leader) == 8
    starts = [mido.tick2second(s, r.mid.ticks_per_beat, 500_000) for s, _ in leader]
    assert starts == pytest.approx([2.0 * i for i in range(8)], abs=0.01)


def test_melodic_percussion_names_are_not_treated_as_a_drum_kit():
    org = organ()
    mid = tune(track("Steel Drums", notes(0, [74, 76, 78, 79] * 8)),
               track("Drums", notes(9, [36, 38] * 8, length=10), channel=9))
    sources, _, _ = ot.read_source(mid)
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    roles = {v.source.split("#")[0]: v.role for v in plan.voices}
    assert roles["Steel Drums"] == "melody"
    assert plan.drums_source.startswith("Drums#")


def test_folding_prefers_an_octave_with_a_pipe_over_snapping():
    # Main:low has A only at A3 (57), not A2 (45). A bass line around A2 must go
    # up an octave to the pipe, not be snapped to a neighbouring semitone.
    org = organ()
    mid = tune(track("Bass", notes(1, [45, 43, 45, 41] * 4, length=BEAT, step=BEAT)))
    sources, _, _ = ot.read_source(mid)
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    plan.transpose = 0
    r = ot.transcribe(mid, org, plan)
    st = list(r.voice_stats.values())[0]
    assert st.snapped == 0
    low = [n for _, _, n in out_notes(r.mid, "Main")]
    assert 57 in low and 45 not in low and 46 not in low


def test_registration_soft_at_start_loud_at_melody_and_off_at_end():
    org = organ()
    # melody enters two bars in
    mid = tune(track("Lead", notes(0, [74, 76, 78, 79] * 4, start=8 * BEAT)),
               track("Bass", notes(1, [38] * 24, length=BEAT, step=BEAT)))
    r = ot.transcribe(mid, org)
    regs = out_notes(r.mid, "Registers")
    by_note = {}
    for s, e, n in regs:
        by_note.setdefault(n, []).append(mido.tick2second(s, r.mid.ticks_per_beat, 500_000))
    assert by_note[100][0] < 0.5                      # MEL flute on, before anything
    assert by_note[103][0] == pytest.approx(4.0 - 0.25, abs=0.01)   # MEL violin on at melody entry
    assert 99 in by_note and 102 in by_note           # both switched off at the end
    assert max(by_note[99] + by_note[102]) > 12.0


def test_output_is_organ_format_and_passes_the_arranger():
    org = organ()
    r = ot.transcribe(d_major_tune(), org)
    names = [t.name for t in r.mid.tracks]
    assert names[0] == "TUNE" and names[1:4] == ["Main", "TenorCM", "TrebCM"]
    assert "Drums" in names and "Registers" in names
    chans = {t.name: {m.channel for m in t if m.type == "note_on"} for t in r.mid.tracks[1:]}
    assert chans["Main"] <= {0} and chans["TenorCM"] <= {1} and chans["TrebCM"] <= {2}
    assert r.check_dropped == 0
    _, check = oa.arrange(r.mid, org)
    assert check.dropped == 0


def test_plan_override_moves_a_voice_and_can_drop_one():
    org = organ()
    mid = d_major_tune()
    sources, _, _ = ot.read_source(mid)
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    for v in plan.voices:
        if v.source.startswith("Lead"):
            v.rank = "TrebCM"
        if v.source.startswith("Pad"):
            v.rank = "drop"
    r = ot.transcribe(mid, org, plan)
    assert out_notes(r.mid, "TrebCM")
    assert not any(72 <= n <= 84 for _, _, n in out_notes(r.mid, "Main"))
    assert not any(v.source.startswith("Pad") for v in r.plan.voices if v.source in r.voice_stats)


def test_plan_round_trips_through_yaml():
    org = organ()
    sources, _, _ = ot.read_source(d_major_tune())
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    again = ot.Plan.from_dict(yaml.safe_load(yaml.safe_dump(plan.to_dict())))
    assert [v.rank for v in again.voices] == [v.rank for v in plan.voices]
    assert again.drum_map == plan.drum_map


# ----------------------------------------------------------------------------
# Sections and spill-over
# ----------------------------------------------------------------------------

def sectioned_organ() -> oa.Organ:
    # Main as this instrument really is: a four-pipe bass (C F G Bb), an
    # eight-pipe accompaniment, a melody section.
    d = organ_dict_sectioned()
    return oa.Organ.from_dict(d)


def organ_dict_sectioned() -> dict:
    base = {36: 0, 41: 1, 43: 2, 46: 3}
    acc = {48: 4, 50: 5, 52: 6, 53: 7, 55: 8, 57: 9, 58: 10, 60: 11}
    mel = {72: 20, 74: 21, 76: 22, 77: 23, 79: 24, 81: 25, 84: 26}
    return {
        "name": "sectioned",
        "tracks": {
            "Main": {"notes": {**base, **acc, **mel},
                     "sections": {"Base": sorted(base), "Accompainment": sorted(acc), "Melody": sorted(mel)}},
            "TenorCM": {"notes": {60: 30, 62: 31, 64: 32, 65: 33, 67: 34, 69: 35, 71: 36, 72: 37}},
        },
    }


def test_sections_become_ranks_named_by_the_sheet():
    ranks = ot.derive_ranks(sectioned_organ())
    assert set(ranks) == {"Main:Base", "Main:Accompainment", "Main:Melody", "TenorCM"}
    assert ranks["Main:Base"].pcs == {0, 5, 7, 10}


def test_bass_goes_to_the_base_rank_with_the_accompaniment_as_fallback():
    org = sectioned_organ()
    sources, _, _ = ot.read_source(d_major_tune())
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    bass = next(v for v in plan.voices if v.role == "bass")
    assert bass.rank == "Main:Base" and bass.fallback == "Main:Accompainment"
    melody = next(v for v in plan.voices if v.role == "melody")
    assert melody.rank == "Main:Melody"


def test_bass_notes_the_base_rank_lacks_spill_to_the_accompaniment_an_octave_up():
    org = sectioned_organ()
    # C major bass line: C G D A. Base has C and G; D and A are not there in any
    # octave, but the accompaniment has D3 (50) and A3 (57).
    mid = tune(track("Bass", notes(1, [36, 43, 38, 45] * 4, length=BEAT, step=BEAT)))
    sources, _, _ = ot.read_source(mid)
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    plan.transpose = 0
    r = ot.transcribe(mid, org, plan)
    st = list(r.voice_stats.values())[0]
    assert st.spilled == 8 and st.snapped == 0 and st.dropped == 0
    main = [n for _, _, n in out_notes(r.mid, "Main")]
    assert set(main) == {36, 43, 50, 57}


def test_narrow_rank_without_fallback_snaps_by_pitch_class_and_reports_it():
    d = organ_dict_sectioned()
    del d["tracks"]["Main"]["sections"]["Accompainment"]
    for n in (48, 50, 52, 53, 55, 57, 58, 60):
        del d["tracks"]["Main"]["notes"][n]
    org = oa.Organ.from_dict(d)
    mid = tune(track("Bass", notes(1, [38] * 4, length=BEAT, step=BEAT)))       # D: no pipe at all
    sources, _, _ = ot.read_source(mid)
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    plan.transpose = 0
    r = ot.transcribe(mid, org, plan)
    st = list(r.voice_stats.values())[0]
    assert st.snapped == 4 and st.kept == 4
    assert all(n in (36, 41, 43, 46) for _, _, n in out_notes(r.mid, "Main"))
    assert any("no pipe" in l or "nearest pipe" in l for l in r.lines)


def test_plan_fallback_round_trips_and_is_validated():
    org = sectioned_organ()
    sources, _, _ = ot.read_source(d_major_tune())
    plan = ot.auto_plan(sources, ot.derive_ranks(org), org)
    again = ot.Plan.from_dict(yaml.safe_load(yaml.safe_dump(plan.to_dict())))
    assert [v.fallback for v in again.voices] == [v.fallback for v in plan.voices]
    bad = ot.Plan.from_dict(plan.to_dict())
    bad.voices[0].fallback = "Nope"
    with pytest.raises(ot.TranscribeError, match="fallback rank"):
        ot.transcribe(d_major_tune(), org, bad)


def test_cli_writes_output_report_and_plan(tmp_path):
    org_path = tmp_path / "organ.yaml"
    org_path.write_text(yaml.safe_dump({
        "name": "t",
        "tracks": {"Main": {"notes": {72: 60, 74: 61, 76: 62, 77: 63, 79: 64, 81: 65, 84: 66}}},
    }), encoding="utf-8")
    src = tmp_path / "tune.mid"
    tune(track("Lead", notes(0, [74, 76, 78, 79]))).save(str(src))
    rc = ot.main([str(src), "--organ", str(org_path), "--write-plan", str(tmp_path / "plan.yaml"), "-q"])
    assert rc == 0
    assert (tmp_path / "tune.fororgan.mid").exists()
    assert (tmp_path / "tune.fororgan.txt").exists()
    plan = yaml.safe_load((tmp_path / "plan.yaml").read_text(encoding="utf-8"))
    assert plan["voices"][0]["role"] == "melody"
    text = (tmp_path / "tune.fororgan.txt").read_text(encoding="utf-8")
    assert "Transposition: -2 semitones" in text
    assert "Arranger check: 0 dropped" in text
