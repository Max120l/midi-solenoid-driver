#!/usr/bin/env python3
"""
organ_config -- retune the driver boards over MIDI, without reflashing.

Sends control changes on the boards' configuration channel. The CC map below
mirrors firmware/midiSolenoidDriverDirectMIDI/midiSolenoidDriverDirectMIDI.ino;
if one changes, change the other.

    organ_config.py --list-ports
    organ_config.py --port "USB MIDI" --peak 60 --hold 25
    organ_config.py --port "USB MIDI" --board 64 --peak 55        # one board only
    organ_config.py --port "USB MIDI" --peak 60 --hold 25 --save  # persist to EEPROM
    organ_config.py --port "USB MIDI" --factory --save            # back to compiled defaults
    organ_config.py --dry-run --peak 60                           # show what would be sent

The boards cannot transmit, so there is no read-back: this tool tells you what
it sent and what the firmware will clamp it to, and a save is acknowledged by
a click on each board's output 1. What a board currently believes is whatever
you last sent it, or what is in its EEPROM if it has been power-cycled since.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

import mido

__version__ = "0.1.0"

# ---- Mirror of the firmware's CC map ---------------------------------------

CONFIG_CHANNEL = 16          # 1-16, as MIDI numbers it; the firmware's CONFIG_CHANNEL

CC_SELECT_BOARD = 20
CC_PEAK_DUTY = 21
CC_HOLD_DUTY = 22
CC_PEAK_DURATION = 23
CC_MAX_NOTE = 24
CC_EXERCISE_CYCLES = 25
CC_COMMAND = 26

CMD_SAVE = 1
CMD_RELOAD = 2
CMD_FACTORY = 3

SELECT_ALL = 127             # any value 113-127 means every board

# The firmware's hard ceilings. Values above these are clamped on the board;
# the tool warns so you are not surprised by what actually took effect.
HOLD_DUTY_MAX_PERCENT = 40
PEAK_DURATION_MAX_MS = 127       # a CC value is 7 bits, so this is also the wire limit
EXERCISE_CYCLES_MAX = 10

INTER_MESSAGE_GAP_S = 0.02   # politeness between CCs; a save also takes the board ~30 ms
MIDI_BAUD = 31250            # a UART at this rate, into a current-loop driver, is a MIDI out


@dataclass
class Request:
    board: int | None = None          # base note 0-112, or None for all boards
    peak: int | None = None
    hold: int | None = None
    peak_ms: int | None = None
    max_note: int | None = None
    exercise: int | None = None
    command: int | None = None        # CMD_*


def clamp(value: int, lo: int, hi: int, name: str, warnings: list[str]) -> int:
    clamped = max(lo, min(hi, value))
    if clamped != value:
        warnings.append(f"{name} {value} is outside {lo}-{hi}; the board will use {clamped}")
    return clamped


def build_messages(req: Request) -> tuple[list[mido.Message], list[str]]:
    """The exact CCs to send, in order, plus any clamping warnings.

    Order matters: the board selection goes first so everything after it is
    addressed correctly, and a command goes last so a save captures the values
    sent just before it.
    """
    ch = CONFIG_CHANNEL - 1          # mido numbers channels 0-15
    warnings: list[str] = []
    out: list[mido.Message] = []

    def cc(number: int, value: int) -> None:
        out.append(mido.Message("control_change", channel=ch, control=number, value=value))

    if req.board is None:
        cc(CC_SELECT_BOARD, SELECT_ALL)
    else:
        cc(CC_SELECT_BOARD, clamp(req.board, 0, 112, "board base note", warnings))

    if req.peak is not None:
        cc(CC_PEAK_DUTY, clamp(req.peak, 1, 100, "peak duty", warnings))
    if req.hold is not None:
        cc(CC_HOLD_DUTY, clamp(req.hold, 0, HOLD_DUTY_MAX_PERCENT, "hold duty", warnings))
    if req.peak_ms is not None:
        cc(CC_PEAK_DURATION, clamp(req.peak_ms, 1, PEAK_DURATION_MAX_MS, "peak duration", warnings))
    if req.max_note is not None:
        cc(CC_MAX_NOTE, clamp(req.max_note, 0, 127, "max note seconds", warnings))
    if req.exercise is not None:
        cc(CC_EXERCISE_CYCLES, clamp(req.exercise, 0, EXERCISE_CYCLES_MAX, "exercise cycles", warnings))
    if req.command is not None:
        cc(CC_COMMAND, req.command)

    return out, warnings


def describe(msg: mido.Message) -> str:
    names = {
        CC_SELECT_BOARD: "select board", CC_PEAK_DUTY: "peak duty %",
        CC_HOLD_DUTY: "hold duty %", CC_PEAK_DURATION: "peak duration ms",
        CC_MAX_NOTE: "max note s", CC_EXERCISE_CYCLES: "exercise cycles",
        CC_COMMAND: "command",
    }
    label = names.get(msg.control, f"CC {msg.control}")
    value = msg.value
    if msg.control == CC_SELECT_BOARD:
        value = "all boards" if msg.value >= 113 else f"base note {msg.value}"
    elif msg.control == CC_COMMAND:
        value = {CMD_SAVE: "save", CMD_RELOAD: "reload", CMD_FACTORY: "factory"}.get(msg.value, msg.value)
    return f"ch {msg.channel + 1}  {label:<18} {value}"


def send_serial(messages: list[mido.Message], port, gap_s: float = INTER_MESSAGE_GAP_S,
                sleep=time.sleep) -> None:
    """Write the messages as raw MIDI bytes to anything with a write().

    For a Raspberry Pi driving the optocoupler straight from a UART pin, this
    is the whole transport: the same three bytes per control change that a
    MIDI interface would send, at 31250 baud.
    """
    for m in messages:
        port.write(bytes(m.bytes()))
        print(describe(m))
        sleep(gap_s)
    flush = getattr(port, "flush", None)
    if flush:
        flush()


def match_port(names: list[str], wanted: str | None) -> str:
    """Pick an output port: the one containing `wanted`, or the only one there is."""
    if wanted:
        hits = [n for n in names if wanted.lower() in n.lower()]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise LookupError(f"no MIDI output matches '{wanted}'. Available: {names or 'none'}")
        raise LookupError(f"'{wanted}' is ambiguous: {hits}")
    if len(names) == 1:
        return names[0]
    if not names:
        raise LookupError("no MIDI output ports found")
    raise LookupError(f"several MIDI outputs; choose one with --port: {names}")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="organ_config",
                                description="Retune the solenoid driver boards over MIDI.")
    where = p.add_mutually_exclusive_group()
    where.add_argument("--port", help="MIDI output port (substring match); optional if there is only one")
    where.add_argument("--serial", metavar="DEVICE",
                       help=f"send over a serial device at {MIDI_BAUD} baud instead, e.g. /dev/ttyAMA1 "
                            f"on a Pi driving MIDI from a UART pin")
    p.add_argument("--list-ports", action="store_true", help="list MIDI output ports and exit")
    p.add_argument("--board", type=int, metavar="BASE",
                   help="address only the board with this base note (default: all boards)")
    p.add_argument("--peak", type=int, metavar="PCT", help="pull-in duty, 1-100")
    p.add_argument("--hold", type=int, metavar="PCT", help=f"hold duty, 0-{HOLD_DUTY_MAX_PERCENT}")
    p.add_argument("--peak-ms", type=int, metavar="MS", help=f"pull-in window, 1-{PEAK_DURATION_MAX_MS}")
    p.add_argument("--max-note", type=int, metavar="S", help="stuck-note watchdog in seconds, 0 = off")
    p.add_argument("--exercise", type=int, metavar="N", help=f"power-up exercise passes, 0-{EXERCISE_CYCLES_MAX}")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--save", action="store_true", help="save the boards' current settings to EEPROM")
    g.add_argument("--reload", action="store_true", help="reload settings from EEPROM")
    g.add_argument("--factory", action="store_true", help="return to compiled defaults (in RAM; add --save to keep)")
    p.add_argument("--dry-run", action="store_true", help="print what would be sent, send nothing")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    if a.list_ports:
        for name in mido.get_output_names():
            print(name)
        return 0

    req = Request(
        board=a.board, peak=a.peak, hold=a.hold, peak_ms=a.peak_ms,
        max_note=a.max_note, exercise=a.exercise,
        command=CMD_SAVE if a.save else CMD_RELOAD if a.reload else CMD_FACTORY if a.factory else None,
    )
    if all(v is None for v in (req.peak, req.hold, req.peak_ms, req.max_note, req.exercise, req.command)):
        p.error("nothing to do: give at least one setting or a command")

    messages, warnings = build_messages(req)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    if a.dry_run:
        for m in messages:
            print(describe(m))
        return 0

    if a.serial:
        try:
            import serial  # pyserial; only needed for this path
        except ImportError:
            print("error: --serial needs pyserial: pip install pyserial", file=sys.stderr)
            return 2
        try:
            with serial.Serial(a.serial, baudrate=MIDI_BAUD, timeout=1) as port:
                send_serial(messages, port)
        except (serial.SerialException, OSError, ValueError) as e:
            print(f"error: cannot send on {a.serial}: {e}", file=sys.stderr)
            return 2
    else:
        try:
            port_name = match_port(mido.get_output_names(), a.port)
        except LookupError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        with mido.open_output(port_name) as port:
            for m in messages:
                port.send(m)
                print(describe(m))
                time.sleep(INTER_MESSAGE_GAP_S)
    if req.command == CMD_SAVE:
        print("saved -- each board should have clicked its output 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
