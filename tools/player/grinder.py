#!/usr/bin/env python3
"""
grinder -- play an arranged MIDI file to the organ over a serial MIDI line.

The Raspberry Pi's UART, at 31250 baud into a current-loop driver and an
optocoupler, is a MIDI output. This writes a song's note and control-change
bytes down it with mido doing the tempo arithmetic. Deliberately small: it is
the playback engine a playlist and a screen will sit on top of, not the app.

    grinder.py SONG.organ.mid [--device /dev/serial0]

Play files that have been through organ_arranger: single track, channel 1,
every note a driver-board slot. Ctrl+C stops the song and silences the organ.
"""

from __future__ import annotations

import argparse
import sys

import mido
import serial

MIDI_BAUD = 31250
NOTE_CHANNEL = 0          # channel 1, as mido counts; the boards' fixedChannel


def silence(port) -> None:
    """All Notes Off. The boards honour it on any channel; send it on the
    music's own so it also silences anything else on the line."""
    msg = mido.Message("control_change", channel=NOTE_CHANNEL, control=123, value=0)
    port.write(bytes(msg.bytes()))
    port.flush()


def play(song: str, device: str) -> int:
    mid = mido.MidiFile(song)
    with serial.Serial(device, baudrate=MIDI_BAUD) as port:
        print(f"Playing {song} ({mid.length:.0f} s) on {device}. Ctrl+C stops and silences.")
        try:
            # play() yields only channel messages, paced by the tempo map.
            # Notes and control changes go to the organ; anything else -- pitch
            # bend, aftertouch -- means nothing to it and is not sent.
            for msg in mid.play():
                if msg.type in ("note_on", "note_off", "control_change"):
                    port.write(bytes(msg.bytes()))
        except KeyboardInterrupt:
            print("\nStopped.")
            return 130
        finally:
            # A note that was sounding when we left must not stay on. Without
            # this it waits for the boards' stuck-note watchdog, which is a
            # safety net, not a way to end a song.
            silence(port)
    print("Finished.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="grinder", description="Play an arranged MIDI file to the organ.")
    p.add_argument("song", help="an arranged .organ.mid")
    p.add_argument("--device", default="/dev/serial0", help="serial device driving the MIDI line (default /dev/serial0)")
    a = p.parse_args(argv)
    try:
        return play(a.song, a.device)
    except (OSError, ValueError, EOFError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
