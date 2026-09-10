import sys
from pathlib import Path

import mido
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import make_scale as ms  # noqa: E402
import organ_arranger as oa  # noqa: E402

ORGAN = Path(__file__).resolve().parents[1] / "instrument" / "organ.yaml"


def played(mid):
    """(note, seconds on) in order, from the one non-conductor track."""
    tr = mid.tracks[1]
    out, on_at, t = [], {}, 0
    for m in tr:
        t += m.time
        if m.type == "note_on" and m.velocity > 0:
            on_at[m.note] = t
        elif m.type in ("note_off", "note_on"):
            out.append((m.note, mido.tick2second(t - on_at.pop(m.note), mid.ticks_per_beat, ms.TEMPO)))
    return out


def test_sections_are_merged_and_ascend():
    organ = oa.Organ.load(ORGAN)
    notes = ms.scale_notes(organ, "Accompainment", ["Base", "Accompainment"], descend=False)
    assert notes == [36, 41, 43, 46, 48, 50, 52, 53, 55, 57, 58, 59, 60]
    assert ms.scale_notes(organ, "melody", [], descend=False) == [72, 74, 76, 77, 78, 79, 81, 82, 83, 84, 86, 88, 89]   # case-insensitive


def test_descend_comes_back_down_without_repeating_the_top():
    organ = oa.Organ.load(ORGAN)
    assert ms.scale_notes(organ, "Drums", [], descend=True) == [21, 35, 38, 39, 38, 35, 21]


def test_unknown_track_or_section_is_a_clear_error():
    organ = oa.Organ.load(ORGAN)
    with pytest.raises(ValueError, match="no track 'Nope'"):
        ms.scale_notes(organ, "Nope", [], False)
    with pytest.raises(ValueError, match="no section 'Bass'"):
        ms.scale_notes(organ, "Accompainment", ["Bass"], False)


def test_each_note_lasts_note_s_with_gap_s_between():
    mid = ms.build("Main", [48, 50, 52], note_s=1.0, gap_s=0.1, title="t")
    assert mid.type == 1 and mid.tracks[1][0].name == "Main"
    assert played(mid) == [(48, 1.0), (50, 1.0), (52, 1.0)]
    assert abs(mid.length - (3 * 1.0 + 2 * 0.1)) < 1e-6


def test_output_arranges_onto_the_organ_with_nothing_dropped():
    organ = oa.Organ.load(ORGAN)
    notes = ms.scale_notes(organ, "Melody", [], descend=False)
    out, report = oa.arrange(ms.build("Melody", notes, 1.0, 0.1, "t"), organ)
    assert report.dropped == 0
    assert report.notes["pitched"] == len(notes)


def test_cli_writes_the_file(tmp_path, capsys):
    out = tmp_path / "s.mid"
    assert ms.main(["--organ", str(ORGAN), "--track", "TenorCM", "--note-s", "0.5", "-o", str(out)]) == 0
    assert "10 notes on TenorCM, 0.5 s each" in capsys.readouterr().out
    assert [n for n, _ in played(mido.MidiFile(str(out)))] == [60, 62, 64, 65, 66, 67, 69, 70, 71, 72]
    assert ms.main(["--organ", str(ORGAN), "--track", "Nope", "-o", str(out)]) == 2
