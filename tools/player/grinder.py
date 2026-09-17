#!/usr/bin/env python3
"""
grinder -- play arranged MIDI files to the organ over a serial MIDI line.

The Raspberry Pi's UART, at 31250 baud into an optocoupler, is a MIDI
output. This writes songs' note and control-change bytes down it, paced by
the tempo map, one after another. It is the playback engine a screen or a
phone will sit on top of, not the app: no state but the queue, nothing to
crash but this process.

    grinder.py SONG.organ.mid
    grinder.py tunes/                          # every *.organ.mid under tunes/, folder by folder
    grinder.py tunes/waltzes --shuffle         # those, in random order
    grinder.py tunes --shuffle --repeat        # everything, reshuffled each time round
    grinder.py evening.m3u --gap 5 --tempo 95%
    grinder.py skaters.organ.mid --start 62 --organ ../organ-arranger/instrument/organ.yaml
    grinder.py tunes --shuffle --list          # show the order, play nothing

What plays: files that have been through organ_arranger -- single track,
channel 1, every note a driver-board slot. A folder contributes every file
matching --pattern (default *.organ.mid) under it, subfolders included, in
name order. A playlist (.m3u, .txt) is one entry per line: a file or a
folder, relative to the playlist, `#` comments; a line may carry its own
settings after a pipe:

    skaters-waltz.organ.mid | tempo=90%
    colonel-bogey.organ.mid | tempo=1.05 gap=6

Tempo scales the whole song, 50 %-150 %. A note is still kept on for at
least the organ's minimum note (50 ms) and off for at least the minimum gap
(30 ms) before it sounds again, so a faster tempo does not ask a solenoid
for what it cannot do. Where the same pipe is struck faster than that
allows -- a roll at 150 % -- the note gives way first, then the gap; the
arrangement already ran at the limit, and no player can add headroom.

Keys while playing: Ctrl+C skips the song; Ctrl+C again within two seconds,
or during the pause between songs, quits. The organ is silenced either way.
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import mido

__version__ = "0.2.0"

MIDI_BAUD = 31250
NOTE_CHANNEL = 0                    # channel 1, as mido counts; the boards' fixedChannel
DEFAULT_DEVICE = "/dev/serial0"
DEFAULT_PATTERN = "*.organ.mid"
DEFAULT_GAP_S = 3.0
TEMPO_MIN, TEMPO_MAX = 0.5, 1.5
MIN_NOTE_S, MIN_GAP_S = 0.050, 0.030            # organ.yaml timing defaults; --organ reads the real ones
REGISTER_PULSE_S, REGISTER_STAGGER_S, SETTLE_S = 0.100, 0.060, 0.250
QUIT_WINDOW_S = 2.0
PLAYLIST_SUFFIXES = (".m3u", ".m3u8", ".txt", ".playlist")
ORGAN_MESSAGES = ("note_on", "note_off", "control_change")


# ----------------------------------------------------------------------------
# The queue: songs from files, folders and playlists
# ----------------------------------------------------------------------------

@dataclass
class Song:
    path: Path
    tempo: float | None = None          # a playlist line's own tempo, else the run's
    gap: float | None = None            # the pause after this song, else the run's

    @property
    def name(self) -> str:
        n = self.path.name
        for suffix in (".mid", ".midi"):
            if n.lower().endswith(suffix):
                n = n[: -len(suffix)]
        if n.lower().endswith(".organ"):
            n = n[: -len(".organ")]
        return n


def parse_tempo(text: str | float) -> float:
    """'90%', '0.9' and '90' all mean nine tenths of the written tempo."""
    s = str(text).strip()
    try:
        if s.endswith("%"):
            value = float(s[:-1]) / 100
        else:
            value = float(s)
            if value > 5:                       # nobody wants five times faster: it is a percentage
                value /= 100
    except ValueError:
        raise ValueError(f"tempo {text!r} is not a number or a percentage") from None
    if not TEMPO_MIN <= value <= TEMPO_MAX:
        raise ValueError(f"tempo {text!r} is outside {TEMPO_MIN:.0%}-{TEMPO_MAX:.0%}")
    return value


def read_playlist(path: Path, pattern: str = DEFAULT_PATTERN) -> list[Song]:
    songs: list[Song] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        target, _, opts = line.partition("|")
        tempo = gap = None
        for opt in opts.split():
            key, _, value = opt.partition("=")
            try:
                if key == "tempo":
                    tempo = parse_tempo(value)
                elif key == "gap":
                    gap = float(value)
                    if gap < 0:
                        raise ValueError("gap must be 0 or more")
                else:
                    raise ValueError(f"unknown setting {key!r} (tempo=, gap=)")
            except ValueError as e:
                raise ValueError(f"{path}:{lineno}: {e}") from None
        p = Path(target.strip())
        if not p.is_absolute():
            p = path.parent / p
        for song in expand([p], pattern):
            song.tempo, song.gap = tempo, gap
            songs.append(song)
    return songs


def expand(items: list[str | Path], pattern: str = DEFAULT_PATTERN) -> list[Song]:
    """Files as given; folders as every matching file under them in name
    order, folder by folder; playlists as their lines."""
    songs: list[Song] = []
    for item in items:
        p = Path(item)
        if p.is_dir():
            found = sorted(q for q in p.rglob(pattern) if q.is_file())
            if not found:
                raise FileNotFoundError(f"no {pattern} under {p}")
            songs += [Song(q) for q in found]
        elif p.suffix.lower() in PLAYLIST_SUFFIXES:
            if not p.is_file():
                raise FileNotFoundError(f"{p}: no such playlist")
            songs += read_playlist(p, pattern)
        elif p.is_file():
            songs.append(Song(p))
        else:
            raise FileNotFoundError(f"{p}: no such file or folder")
    return songs


# ----------------------------------------------------------------------------
# One song as a timeline the organ can follow
# ----------------------------------------------------------------------------

def is_off(msg: mido.Message) -> bool:
    return msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0)


def timeline(mid: mido.MidiFile, speed: float = 1.0, start: float = 0.0,
             min_note_s: float = MIN_NOTE_S, min_gap_s: float = MIN_GAP_S) -> list[tuple[float, mido.Message]]:
    """(seconds from the start, message) for what the organ understands, at
    `speed` times the written tempo, from `start` seconds (at the new speed)
    onwards. Every note stays on at least min_note_s and is off at least
    min_gap_s before it sounds again, as far as the strikes allow: when the
    same pipe is struck faster than note + gap, the note gives way first,
    and when even the gap cannot fit, the interval is split in two."""
    events: list[list] = []
    t = 0.0
    for msg in mid:                                  # merged tracks, msg.time in seconds
        t += msg.time
        if msg.type in ORGAN_MESSAGES:
            events.append([t / speed, msg])
    # every note's on/off pairs, in order
    open_at: dict[int, int] = {}
    pairs: dict[int, list[tuple[int, int]]] = {}
    for i, (_, msg) in enumerate(events):
        if msg.type == "note_on" and msg.velocity > 0:
            open_at[msg.note] = i
        elif is_off(msg):
            j = open_at.pop(msg.note, None)
            if j is not None:
                pairs.setdefault(msg.note, []).append((j, i))
    for note_pairs in pairs.values():
        for k, (j, i) in enumerate(note_pairs):
            on = events[j][0]
            off = max(events[i][0], on + min_note_s)
            if k + 1 < len(note_pairs):
                next_on = events[note_pairs[k + 1][0]][0]
                off = min(off, max(next_on - min_gap_s, on + (next_on - on) / 2))
            events[i][0] = off
    events.sort(key=lambda e: (e[0], 0 if is_off(e[1]) else 1))     # offs first at the same instant
    if start > 0:
        events = [[t - start, m] for t, m in events if t >= start]
    return [(t, m) for t, m in events]


def registration_at(mid: mido.MidiFile, speed: float, start: float,
                    registers: list[tuple[int, int]]) -> list[int]:
    """The register notes to pulse before playing from `start`: for each
    set/reset pair, whichever of its two notes was pulsed last before then."""
    last: dict[int, tuple[float, int]] = {}
    which = {n: i for i, pair in enumerate(registers) for n in pair}
    t = 0.0
    for msg in mid:
        t += msg.time
        if t / speed >= start:
            break
        if msg.type == "note_on" and msg.velocity > 0 and msg.note in which:
            last[which[msg.note]] = (t, msg.note)
    return [note for _, note in sorted(last.values())]


def read_organ(path: str) -> tuple[float, float, list[tuple[int, int]]]:
    """(min_note_s, min_gap_s, [(set note, reset note), ...]) from organ.yaml."""
    import yaml
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    timing = raw.get("timing") or {}
    first = int(raw.get("solenoid_1_note", 0))
    regs = [(first + int(r["set"]) - 1, first + int(r["reset"]) - 1) for r in raw.get("registers") or []]
    return (float(timing.get("min_note_ms", MIN_NOTE_S * 1000)) / 1000,
            float(timing.get("min_gap_ms", MIN_GAP_S * 1000)) / 1000, regs)


# ----------------------------------------------------------------------------
# Playing
# ----------------------------------------------------------------------------

class NullPort:
    """--dry-run: the bytes go nowhere, the time still passes."""

    def __init__(self) -> None:
        self.written = 0

    def write(self, data: bytes) -> None:
        self.written += len(data)

    def flush(self) -> None:
        pass


def silence(port) -> None:
    """All Notes Off. The boards honour it on any channel; send it on the
    music's own so it also silences anything else on the line."""
    port.write(bytes(mido.Message("control_change", channel=NOTE_CHANNEL, control=123, value=0).bytes()))
    port.flush()


def play_events(events: list[tuple[float, mido.Message]], port, sleep=time.sleep, clock=time.monotonic) -> None:
    """Write each message at its time, measured from now, so delays never accumulate."""
    t0 = clock()
    for t, msg in events:
        wait = t0 + t - clock()
        if wait > 0:
            sleep(wait)
        port.write(bytes(msg.bytes()))
    port.flush()


def pulse_registers(notes: list[int], port, sleep=time.sleep) -> None:
    for note in notes:
        port.write(bytes(mido.Message("note_on", channel=NOTE_CHANNEL, note=note, velocity=100).bytes()))
        sleep(REGISTER_PULSE_S)
        port.write(bytes(mido.Message("note_off", channel=NOTE_CHANNEL, note=note, velocity=0).bytes()))
        sleep(REGISTER_STAGGER_S)
    if notes:
        sleep(SETTLE_S)


def clock_text(seconds: float) -> str:
    m, s = divmod(int(seconds + 0.5), 60)
    return f"{m}:{s:02d}"


@dataclass
class Settings:
    tempo: float = 1.0
    gap: float = DEFAULT_GAP_S
    start: float = 0.0
    shuffle: bool = False
    repeat: bool = False
    min_note_s: float = MIN_NOTE_S
    min_gap_s: float = MIN_GAP_S
    registers: list[tuple[int, int]] | None = None


def run(songs: list[Song], port, cfg: Settings, sleep=time.sleep, clock=time.monotonic,
        out=None, rng: random.Random | None = None) -> int:
    """Play the queue. Returns 0 at the end, 130 when quit with Ctrl+C."""
    out = out or sys.stdout
    rng = rng or random.Random()
    total = len(songs)
    time_round = 0
    while True:
        order = list(range(total))
        if cfg.shuffle:
            rng.shuffle(order)
        for k, i in enumerate(order):
            song = songs[i]
            speed = song.tempo if song.tempo is not None else cfg.tempo
            start = cfg.start if time_round == 0 and k == 0 else 0.0
            try:
                mid = mido.MidiFile(str(song.path))
                events = timeline(mid, speed, start, cfg.min_note_s, cfg.min_gap_s)
            except (OSError, ValueError, EOFError, KeyError, IndexError) as e:
                print(f"[{k + 1}/{total}] {song.name}: cannot play: {e}", file=out, flush=True)
                continue
            length = events[-1][0] if events else 0.0
            note = f"  from {clock_text(start)}" if start else ""
            print(f"[{k + 1}/{total}] {song.name}  {clock_text(length)}  tempo {speed:.0%}{note}", file=out, flush=True)
            silence(port)
            try:
                if start and cfg.registers:
                    pulse_registers(registration_at(mid, speed, start, cfg.registers), port, sleep)
                began = clock()
                play_events(events, port, sleep, clock)
            except KeyboardInterrupt:
                silence(port)
                print(f"      skipped at {clock_text(clock() - began)}  (Ctrl+C again within {QUIT_WINDOW_S:.0f} s quits)",
                      file=out, flush=True)
                try:
                    sleep(QUIT_WINDOW_S)
                except KeyboardInterrupt:
                    print("Stopped.", file=out, flush=True)
                    return 130
                continue
            silence(port)
            last = k + 1 == total and not cfg.repeat
            if not last:
                gap = song.gap if song.gap is not None else cfg.gap
                try:
                    sleep(gap)
                except KeyboardInterrupt:
                    print("Stopped.", file=out, flush=True)
                    return 130
        if not cfg.repeat:
            break
        time_round += 1
    print("Finished.", file=out, flush=True)
    return 0


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="grinder", description="Play arranged MIDI files to the organ.",
                                formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog="Ctrl+C skips the song; again within two seconds, or during a pause, quits.")
    p.add_argument("items", nargs="+", metavar="SONG|FOLDER|PLAYLIST",
                   help="arranged .organ.mid files, folders of them, or .m3u/.txt playlists")
    p.add_argument("--device", default=DEFAULT_DEVICE, help=f"serial device driving the MIDI line (default {DEFAULT_DEVICE})")
    p.add_argument("--dry-run", action="store_true", help="no serial port: play in real time to nowhere")
    p.add_argument("--tempo", default="100%", help="speed for every song, 50%%-150%% (e.g. 90%%, 1.1); a playlist line can override")
    p.add_argument("--gap", type=float, default=DEFAULT_GAP_S, help=f"seconds of silence between songs (default {DEFAULT_GAP_S:g})")
    p.add_argument("--shuffle", action="store_true", help="random order (reshuffled on every repeat)")
    p.add_argument("--repeat", action="store_true", help="start again when the queue ends")
    p.add_argument("--start", type=float, default=0.0, metavar="SECONDS", help="begin the first song this far in")
    p.add_argument("--pattern", default=DEFAULT_PATTERN, help=f"which files a folder contributes (default {DEFAULT_PATTERN})")
    p.add_argument("--organ", help="organ.yaml: the minimum note and gap, and the registers to restore for --start")
    p.add_argument("--list", action="store_true", help="print the queue in play order and exit")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    cfg = Settings(gap=a.gap, start=a.start, shuffle=a.shuffle, repeat=a.repeat)
    try:
        cfg.tempo = parse_tempo(a.tempo)
        if a.gap < 0 or a.start < 0:
            raise ValueError("--gap and --start must be 0 or more")
        if a.organ:
            cfg.min_note_s, cfg.min_gap_s, cfg.registers = read_organ(a.organ)
        songs = expand(a.items, a.pattern)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if a.list:
        order = list(range(len(songs)))
        if a.shuffle:
            random.shuffle(order)
        for n, i in enumerate(order, 1):
            s = songs[i]
            extra = f"  tempo {s.tempo:.0%}" if s.tempo is not None else ""
            extra += f"  gap {s.gap:g}" if s.gap is not None else ""
            print(f"{n:3d}  {s.path}{extra}")
        return 0

    if a.dry_run:
        print(f"dry run: {len(songs)} song{'s' if len(songs) != 1 else ''}, nothing on the wire")
        return run(songs, NullPort(), cfg)

    try:
        import serial
    except ImportError:
        print("error: needs pyserial: pip install pyserial (or use --dry-run)", file=sys.stderr)
        return 2
    try:
        with serial.Serial(a.device, baudrate=MIDI_BAUD) as port:
            return run(songs, port, cfg)
    except (serial.SerialException, OSError, ValueError) as e:
        print(f"error: cannot open {a.device}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
