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
    base = {36: 1, 41: 2, 43: 3, 46: 4}   # solenoids are 1-based
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


def test_registration_never_lands_before_zero_when_the_melody_starts_on_tick_zero():
    # Regression: a tune whose melody starts on the first tick put the "at
    # melody" register pulse at -0.25 s, and mido refused to save the file.
    org = organ()
    mid = tune(track("Lead", notes(0, [74, 76, 78, 79] * 4, start=0)),
               track("Bass", notes(1, [38] * 16, length=BEAT, step=BEAT)))
    r = ot.transcribe(mid, org)
    assert all(m.time >= 0 for t in r.mid.tracks for m in t)
    regs = out_notes(r.mid, "Registers")
    assert min(s for s, _, _ in regs) == 0
    import io
    r.mid.save(file=io.BytesIO())                      # what actually failed before


def test_a_voice_window_lets_one_source_play_two_roles():
    # A synth that is the hook for the first 4 s and a low ostinato after:
    # windowed twice, the hook goes to the melody rank and the rest is dropped.
    org = organ()
    hook = notes(0, [74, 76, 78, 79] * 4, start=0)                     # 0-4 s
    ostinato = notes(0, [38, 45] * 8, start=8 * BEAT)                  # 4-8 s
    mid = tune(track("Synth", hook + ostinato), track("Bass", notes(1, [38] * 16, length=BEAT, step=BEAT)))
    sources, _, _ = ot.read_source(mid)
    ranks = ot.derive_ranks(org)
    plan = ot.auto_plan(sources, ranks, org)
    synth = next(v for v in plan.voices if v.source.startswith("Synth"))
    plan.voices.remove(synth)
    plan.voices.insert(0, ot.Voice(synth.source, "Main:high", "melody", 1, 3.0, None, None, 4.0))
    plan.voices.insert(1, ot.Voice(synth.source, "drop", "counter", 1, 1.0, None, 4.0, None))
    plan.transpose = 0
    r = ot.transcribe(mid, org, plan)
    main = out_notes(r.mid, "Main")
    high = [s for s, e, n in main if n >= 72]
    assert high and max(high) < mido.second2tick(4.0, r.mid.ticks_per_beat, 500_000)   # nothing after 4 s
    assert r.voice_stats[plan.voices[0].key].kept == 16
    text = ot.render_report(r, org, ranks, "s", "d")
    assert "[..4s]" in text and "[4s..]" in text
    again = ot.Plan.from_dict(yaml.safe_load(yaml.safe_dump(plan.to_dict())))
    assert (again.voices[0].start, again.voices[0].end) == (None, 4.0)
    assert (again.voices[1].start, again.voices[1].end) == (4.0, None)
    with pytest.raises(ot.TranscribeError):
        ot.Plan.from_dict({"voices": [{"source": "x", "rank": "drop", "from": 5, "until": 2}]})


def test_a_pitch_window_splits_one_line_into_bass_and_tune():
    # A single track that alternates a low riff and a high tune, as type-0
    # files often do: below C4 is the bass, C4 and up is the melody.
    org = organ()
    line = []
    for i in range(8):
        line += notes(0, [36], start=i * BEAT, length=BEAT // 2, step=BEAT // 2)
        line += notes(0, [79], start=i * BEAT + BEAT // 2, length=BEAT // 2, step=BEAT // 2)
    mid = tune(track("Piano", line))
    plan = ot.Plan.from_dict({
        "transpose": 0,
        "voices": [
            {"source": "Piano#1", "rank": "Main:high", "role": "melody", "lowest": 60},
            {"source": "Piano#1", "rank": "Main:low", "role": "bass", "highest": 59},
        ],
        "drums": {"source": None, "map": {}}, "registration": [],
    })
    r = ot.transcribe(mid, org, plan)
    main = out_notes(r.mid, "Main")
    pitches = sorted({n for _, _, n in main})
    assert len(pitches) == 2 and pitches[0] % 12 == 0 and pitches[0] < 72 and pitches[1] == 79   # C folded within the bass rank, G on the tune rank
    assert r.voice_stats["Piano#1[C4..]"].kept == 8 and r.voice_stats["Piano#1[..B3]"].kept == 8
    with pytest.raises(ot.TranscribeError):
        ot.Plan.from_dict({"voices": [{"source": "x", "rank": "drop", "lowest": 70, "highest": 60}]})


def test_a_track_carrying_several_channels_becomes_one_source_per_channel():
    # Type 0 files put every instrument on one track, told apart by channel.
    org = organ()
    line = (notes(0, [79, 81, 83, 84] * 4, start=0)                       # a tune on channel 1
            + notes(1, [36] * 8, start=0, length=BEAT, step=BEAT)          # a bass on channel 2
            + notes(9, [38] * 8, start=0, length=10, step=BEAT))           # drums on channel 10
    mid = tune(track("Everything", line))
    sources, _, _ = ot.read_source(mid)
    assert [s.key for s in sources] == ["Everything#1/ch1", "Everything#1/ch2", "Everything#1/ch10"]
    assert [s.channel for s in sources] == [0, 1, 9]
    ranks = ot.derive_ranks(org)
    plan = ot.auto_plan(sources, ranks, org)
    roles = {v.source: v.role for v in plan.voices}
    assert roles["Everything#1/ch1"] == "melody" and roles["Everything#1/ch2"] == "bass"
    assert plan.drums_source == "Everything#1/ch10"
    r = ot.transcribe(mid, org, plan)
    assert r.check_dropped == 0 and r.drum_counts["snare"] == 8
    # a single-channel track keeps its plain key, so existing plans still match
    single, _, _ = ot.read_source(tune(track("Lead", notes(0, [79] * 4))))
    assert single[0].key == "Lead#1"


def test_track_names_are_cleaned_of_nuls_and_padding():
    tr = track("vocals \x00", notes(0, [79] * 4))
    sources, _, _ = ot.read_source(tune(tr))
    assert sources[0].key == "vocals#1" and sources[0].name == "vocals"


def test_several_drum_tracks_merge_into_one_drum_source():
    # Kick and snare on separate tracks, as some files do.
    org = organ()
    mid = tune(track("Lead", notes(0, [79] * 8, length=BEAT, step=BEAT)),
               track("Kick", notes(9, [36] * 8, length=10, step=BEAT)),
               track("Snare", notes(9, [38] * 4, start=BEAT // 2, length=10, step=2 * BEAT)))
    plan = ot.Plan.from_dict({
        "transpose": 0,
        "voices": [{"source": "Lead#1", "rank": "Main:high", "role": "melody"}],
        "drums": {"source": ["Kick#2", "Snare#3", "Nope#9"], "map": {36: "bass", 38: "snare"}},
        "registration": [],
    })
    r = ot.transcribe(mid, org, plan)
    assert r.drum_counts["bass"] == 8 and r.drum_counts["snare"] == 4
    assert any("Nope#9" in line for line in r.lines)
    again = ot.Plan.from_dict(yaml.safe_load(yaml.safe_dump(plan.to_dict())))
    assert again.drums_source == ["Kick#2", "Snare#3", "Nope#9"]


def test_a_humanised_octave_double_counts_as_part_of_the_chord():
    # Sequencers offset doubled voices by 10-15 ms; that is still one chord.
    late = int(round(0.012 * TPB * 2))                      # 12 ms in ticks at 120 BPM
    line = []
    for i in range(4):
        line += notes(0, [79], start=i * BEAT, length=BEAT // 2)
        line += notes(0, [67], start=i * BEAT + late, length=BEAT // 2)
    thinned, removed = ot.thin_chords(ot.read_source(tune(track("Lead", line)))[0][0].notes, 1, "melody")
    assert removed == 4 and [n.pitch for n in thinned] == [79] * 4


def test_a_one_voice_line_is_clipped_to_legato_so_folded_overlaps_do_not_merge():
    # A pedalled piano bass: each note held across the next. Folded onto one
    # rank, the overlaps would land on one pipe; clipped, they re-articulate.
    held = [ot.Note(i * 0.5, i * 0.5 + 1.2, 36 + 12 * (i % 2)) for i in range(6)]   # C2, C3, C2, ... each 1.2 s
    clipped = ot.clip_legato(held)
    assert [round(n.end - n.start, 3) for n in clipped] == [0.5] * 5 + [1.2]
    assert [n.pitch for n in clipped] == [n.pitch for n in held]
    org = organ()
    line = []
    for i in range(6):
        line += notes(0, [36 + 12 * (i % 2)], start=i * BEAT, length=int(2.4 * BEAT))
    mid = tune(track("Piano", line))
    plan = ot.Plan.from_dict({"transpose": 0,
                              "voices": [{"source": "Piano#1", "rank": "Main:low", "role": "bass", "max_poly": 1}],
                              "drums": {"source": None, "map": {}}, "registration": []})
    r = ot.transcribe(mid, org, plan)
    _, check = oa.arrange(r.mid, org)
    assert check.counts["Merged: overlapping notes on one solenoid"] == 0
    assert check.notes[oa.KIND_PITCHED] == 6


def test_a_plan_whose_melody_matches_no_source_is_refused_not_silently_emptied():
    org = organ()
    mid = d_major_tune()
    good = ot.Plan.from_dict({"transpose": 0, "voices": [{"source": "Lead#1", "rank": "Main:high", "role": "melody"}],
                              "drums": {"source": None, "map": {}}, "registration": []})
    assert ot.transcribe(mid, org, good).check_dropped == 0
    renamed = ot.Plan.from_dict({"transpose": 0,
                                 "voices": [{"source": "Lead#1/ch1", "rank": "Main:high", "role": "melody"},
                                            {"source": "Bass#2", "rank": "Main:low", "role": "bass"}],
                                 "drums": {"source": None, "map": {}}, "registration": []})
    with pytest.raises(ot.TranscribeError, match="melody voice 'Lead#1/ch1'"):
        ot.transcribe(mid, org, renamed)
    nothing = ot.Plan.from_dict({"transpose": 0, "voices": [{"source": "Nope#7", "rank": "Main:low", "role": "bass"}],
                                 "drums": {"source": None, "map": {}}, "registration": []})
    with pytest.raises(ot.TranscribeError, match="none of the plan's voices"):
        ot.transcribe(mid, org, nothing)


def test_preview_gm_assigns_sounds_and_moves_drums_to_channel_10():
    import preview_gm as pg
    org = organ()
    r = ot.transcribe(d_major_tune(), org)
    pv = pg.preview(r.mid, {25: "Bass", 22: "Snare", 23: "Snare", 21: "Leader"})
    names = [t.name for t in pv.tracks]
    assert "Registers" not in names and "Drums" in names and "Main" in names
    main = next(t for t in pv.tracks if t.name == "Main")
    assert main[0].type == "program_change" and main[0].program == pg.PROGRAMS["Main"]
    drums = next(t for t in pv.tracks if t.name == "Drums")
    ons = [m for m in drums if m.type == "note_on"]
    assert ons and all(m.channel == 9 for m in ons)
    assert {m.note for m in ons} <= {36, 38, 76}
    import io
    pv.save(file=io.BytesIO())


def test_a_tremolo_between_two_pitches_becomes_the_pair_held():
    # E5-G5 alternating every 80 ms for two seconds, then a plain C6.
    trem = [ot.Note(i * 0.08, i * 0.08 + 0.06, 76 if i % 2 == 0 else 79) for i in range(25)]
    tail = [ot.Note(2.5, 3.0, 84)]
    out, runs = ot.sustain_tremolos(trem + tail, 0.1)
    assert runs == 1
    assert [(n.pitch, round(n.start, 2), round(n.end, 2)) for n in out] == [(76, 0.0, 1.98), (79, 0.08, 1.98), (84, 2.5, 3.0)]
    # a scale at the same speed is not a tremolo: more than two pitches
    scale = [ot.Note(i * 0.08, i * 0.08 + 0.06, 72 + i) for i in range(8)]
    assert ot.sustain_tremolos(scale, 0.1) == (sorted(scale, key=lambda n: (n.start, n.pitch)), 0)
    # and a slow alternation is left alone
    slow = [ot.Note(i * 0.3, i * 0.3 + 0.2, 76 if i % 2 == 0 else 79) for i in range(8)]
    assert ot.sustain_tremolos(slow, 0.1)[1] == 0


def test_tremolo_option_round_trips_through_the_plan_and_reaches_the_arrangement():
    org = organ()
    line = []
    for i in range(24):
        line += notes(0, [76 if i % 2 == 0 else 79], start=i * (BEAT // 6), length=BEAT // 8)   # 83 ms apart
    mid = tune(track("Lead", line))
    plan = ot.Plan.from_dict({"transpose": 0,
                              "voices": [{"source": "Lead#1", "rank": "Main:high", "role": "melody", "tremolo": 100}],
                              "drums": {"source": None, "map": {}}, "registration": []})
    r = ot.transcribe(mid, org, plan)
    main = out_notes(r.mid, "Main")
    assert len(main) == 2 and {n for _, _, n in main} == {76, 79}
    assert any("tremolo passage" in line for line in r.lines)
    again = ot.Plan.from_dict(yaml.safe_load(yaml.safe_dump(plan.to_dict())))
    assert again.voices[0].tremolo_ms == 100


def test_snare_hits_alternate_between_the_two_beaters():
    # The organ's two "Snare" notes are two beaters on one drum; alternating
    # them is how a roll gets faster than one solenoid can re-articulate.
    org = organ()
    roll = notes(9, [38] * 16, start=0, length=5, step=BEAT // 8)        # 62 ms apart
    r = ot.transcribe(tune(track("Lead", notes(0, [79] * 4, length=BEAT, step=BEAT)), track("Drums", roll)), org)
    hits = [n for _, _, n in out_notes(r.mid, "Drums")]
    snares = [n for n in hits if n in (22, 23)]
    assert len(snares) == 16 and snares[:4] == [22, 23, 22, 23]
    _, check = oa.arrange(r.mid, org)
    assert check.counts["Merged: re-articulation too fast to play, joined into one note"] == 0
