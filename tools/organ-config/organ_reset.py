#!/usr/bin/env python3
"""
organ_reset -- pulse the driver boards' RESET lines from Raspberry Pi GPIO.

Each board's RESET is reached through an optocoupler (see hardware/HARDWARE-
NOTES.md, "Remote reset from the Pi"): a GPIO drives the opto's LED through
330 ohm, and the opto's output pulls the board's RESET low. The Pi and the
organ share no ground, which is the point.

    organ_reset.py --all
    organ_reset.py --board 2
    organ_reset.py --board 1 --board 4 --pins 17,27,22,23 --hold-ms 100

A reset reloads the board's saved EEPROM settings and runs its power-up
exercise routine -- every coil in sequence, about 3.8 s. Under wind that plays
a scale. For silent remote resets, set exerciseCycles to 0 over MIDI first.

Needs gpiozero: `pip install gpiozero lgpio` in the Pi's venv.
"""

from __future__ import annotations

import argparse
import sys
import time

__version__ = "0.1.0"

DEFAULT_PINS = "17,27,22,23"      # BCM numbers: physical pins 11, 13, 15, 16; all pull-down at boot
STAGGER_S = 0.25                  # between boards, so four exercise routines do not start together


def parse_pins(text: str) -> list[int]:
    try:
        pins = [int(x) for x in text.split(",") if x.strip()]
    except ValueError as e:
        raise ValueError(f"--pins must be comma-separated BCM numbers, got {text!r}") from e
    if not pins or len(set(pins)) != len(pins) or any(p < 0 or p > 27 for p in pins):
        raise ValueError(f"--pins must be distinct BCM numbers 0-27, got {text!r}")
    return pins


def select_targets(pins: list[int], boards: list[int] | None, all_boards: bool) -> list[int]:
    """Board numbers (1-based) to reset, validated against how many pins exist."""
    if all_boards or not boards:
        return list(range(1, len(pins) + 1))
    for b in boards:
        if not 1 <= b <= len(pins):
            raise ValueError(f"board {b} is out of range; there are {len(pins)} pins configured")
    return list(dict.fromkeys(boards))


def pulse(pin: int, hold_s: float, device_factory=None) -> None:
    """Drive the pin high for hold_s, then low, and release it."""
    if device_factory is None:
        from gpiozero import DigitalOutputDevice  # imported here so tests need no GPIO
        device_factory = DigitalOutputDevice
    dev = device_factory(pin, active_high=True, initial_value=False)
    try:
        dev.on()
        time.sleep(hold_s)
        dev.off()
    finally:
        dev.close()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="organ_reset", description="Reset the solenoid driver boards from the Pi.")
    p.add_argument("--pins", default=DEFAULT_PINS, help=f"BCM GPIO per board, in board order (default {DEFAULT_PINS})")
    p.add_argument("--board", type=int, action="append", help="board to reset, 1-based; repeatable")
    p.add_argument("--all", action="store_true", help="reset every board (default when no --board is given)")
    p.add_argument("--hold-ms", type=int, default=100, help="how long to hold RESET low (default 100)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    try:
        pins = parse_pins(a.pins)
        targets = select_targets(pins, a.board, a.all)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    try:
        import gpiozero  # noqa: F401
    except ImportError:
        print("error: needs gpiozero: pip install gpiozero lgpio", file=sys.stderr)
        return 2

    for i, b in enumerate(targets):
        pulse(pins[b - 1], a.hold_ms / 1000)
        print(f"board {b}: reset pulsed on GPIO {pins[b - 1]}")
        if i + 1 < len(targets):
            time.sleep(STAGGER_S)
    print("boards will reload their saved settings and run the exercise routine")
    return 0


if __name__ == "__main__":
    sys.exit(main())
