import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import organ_config as oc  # noqa: E402


def controls(msgs):
    return [(m.control, m.value) for m in msgs]


def test_selection_comes_first_and_defaults_to_all_boards():
    msgs, warnings = oc.build_messages(oc.Request(peak=60))
    assert controls(msgs)[0] == (oc.CC_SELECT_BOARD, oc.SELECT_ALL)
    assert controls(msgs)[1] == (oc.CC_PEAK_DUTY, 60)
    assert warnings == []


def test_specific_board_is_selected_by_base_note():
    msgs, _ = oc.build_messages(oc.Request(board=64, hold=25))
    assert controls(msgs) == [(oc.CC_SELECT_BOARD, 64), (oc.CC_HOLD_DUTY, 25)]


def test_all_messages_are_on_the_configuration_channel():
    msgs, _ = oc.build_messages(oc.Request(peak=60, hold=25, peak_ms=40, max_note=30, exercise=2,
                                           command=oc.CMD_SAVE))
    assert all(m.type == "control_change" and m.channel == oc.CONFIG_CHANNEL - 1 for m in msgs)


def test_command_is_sent_last_so_a_save_captures_the_values_before_it():
    msgs, _ = oc.build_messages(oc.Request(hold=25, peak=60, command=oc.CMD_SAVE))
    assert controls(msgs)[-1] == (oc.CC_COMMAND, oc.CMD_SAVE)
    assert (oc.CC_PEAK_DUTY, 60) in controls(msgs)[:-1]
    assert (oc.CC_HOLD_DUTY, 25) in controls(msgs)[:-1]


def test_values_beyond_the_firmware_ceilings_are_clamped_with_a_warning():
    msgs, warnings = oc.build_messages(oc.Request(hold=90, peak_ms=500, exercise=99, peak=0))
    assert (oc.CC_HOLD_DUTY, oc.HOLD_DUTY_MAX_PERCENT) in controls(msgs)
    assert (oc.CC_PEAK_DURATION, oc.PEAK_DURATION_MAX_MS) in controls(msgs)
    assert (oc.CC_EXERCISE_CYCLES, oc.EXERCISE_CYCLES_MAX) in controls(msgs)
    assert (oc.CC_PEAK_DUTY, 1) in controls(msgs)
    assert len(warnings) == 4
    assert any("hold duty 90" in w and str(oc.HOLD_DUTY_MAX_PERCENT) in w for w in warnings)


def test_every_emitted_value_fits_in_seven_bits_even_for_absurd_input():
    # Regression: a firmware ceiling above 127 once produced a CC value mido
    # refused to build -- and would have been unreachable over MIDI anyway.
    req = oc.Request(board=10_000, peak=10_000, hold=10_000, peak_ms=10_000,
                     max_note=10_000, exercise=10_000, command=oc.CMD_SAVE)
    msgs, _ = oc.build_messages(req)
    assert all(0 <= m.value <= 127 for m in msgs)
    assert all(0 <= m.control <= 127 for m in msgs)
    for ceiling in (oc.HOLD_DUTY_MAX_PERCENT, oc.PEAK_DURATION_MAX_MS, oc.EXERCISE_CYCLES_MAX):
        assert ceiling <= 127


def test_only_requested_settings_are_sent():
    msgs, _ = oc.build_messages(oc.Request(peak=70))
    assert [c for c, _ in controls(msgs)] == [oc.CC_SELECT_BOARD, oc.CC_PEAK_DUTY]


def test_watchdog_can_be_disabled_with_zero():
    msgs, warnings = oc.build_messages(oc.Request(max_note=0))
    assert (oc.CC_MAX_NOTE, 0) in controls(msgs)
    assert warnings == []


def test_describe_is_readable():
    msgs, _ = oc.build_messages(oc.Request(board=48, peak=60, command=oc.CMD_SAVE))
    text = [oc.describe(m) for m in msgs]
    assert "base note 48" in text[0]
    assert "peak duty %" in text[1] and "60" in text[1]
    assert "save" in text[2]


def test_match_port_by_substring_case_insensitive():
    names = ["Midi Through:Midi Through Port-0 14:0", "USB MIDI Interface:USB MIDI Interface MIDI 1 20:0"]
    assert oc.match_port(names, "usb midi") == names[1]


def test_match_port_uses_the_only_port_when_unspecified():
    assert oc.match_port(["Only One"], None) == "Only One"


def test_match_port_errors_are_specific():
    names = ["A", "B"]
    with pytest.raises(LookupError, match="choose one"):
        oc.match_port(names, None)
    with pytest.raises(LookupError, match="no MIDI output matches"):
        oc.match_port(names, "zzz")
    with pytest.raises(LookupError, match="ambiguous"):
        oc.match_port(["USB A", "USB B"], "usb")


def test_dry_run_sends_nothing_and_reports(capsys):
    rc = oc.main(["--dry-run", "--peak", "60", "--hold", "25", "--save"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "all boards" in out and "peak duty %" in out and "save" in out


def test_nothing_to_do_is_an_error():
    with pytest.raises(SystemExit):
        oc.main(["--dry-run"])
