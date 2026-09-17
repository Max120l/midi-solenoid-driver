#!/usr/bin/env python3
"""
grinder -- play arranged MIDI files to the organ over a serial MIDI line.

The Raspberry Pi's UART, at 31250 baud into an optocoupler, is a MIDI
output. This writes songs' note and control-change bytes down it, paced by
the tempo map, one after another. It is the playback engine a screen or a
phone sits on top of, not the app: no state but the queue, nothing to crash
but this process.

    grinder.py SONG.organ.mid
    grinder.py tunes/                          # every *.organ.mid under tunes/, folder by folder
    grinder.py tunes/waltzes --shuffle         # those, in random order
    grinder.py tunes --shuffle --repeat        # everything, reshuffled each time round
    grinder.py evening.m3u --gap 5 --tempo 95%
    grinder.py skaters.organ.mid --start 62 --organ ../organ-arranger/instrument/organ.yaml
    grinder.py tunes --shuffle --list          # show the order, play nothing
    grinder.py tunes --status /tmp/grinder.json --pump 24 --warm-up 8
    grinder.py queue.m3u --watch --status /dev/shm/grinder.json     # under a front end

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

Keys while playing: Ctrl+C skips the song -- the notes sounding at that
moment end where they are written, within half a second, and then the organ
is silenced; Ctrl+C again within two seconds, or during the pause between
songs, quits. From another process, SIGUSR1 skips and SIGTERM quits the
same clean way.

--watch turns one playlist into a live queue: the file is read again before
every song, the first entry not yet played is next (entries carry `id=N`
after the pipe; a line without one is known by its line number), and when
nothing is left the player idles, checking the file every second, until
something is added. --repeat starts the file over instead of idling. This
is how a front end drives the player: it edits the file, and sends the
signals.

--status FILE rewrites a one-line JSON document about once a second: what
is playing, where in it, what the queue is doing. --pump GPIO drives a relay
for the bellows pump through a Pi GPIO: on before the first song, off at
the end or on quit; --warm-up SECONDS waits for wind before the first note,
with or without a pump pin.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import mido

__version__ = "0.4.0"

MIDI_BAUD = 31250
NOTE_CHANNEL = 0                    # channel 1, as mido counts; the boards' fixedChannel
DEFAULT_DEVICE = "/dev/serial0"
DEFAULT_PATTERN = "*.organ.mid"
DEFAULT_GAP_S = 3.0
TEMPO_MIN, TEMPO_MAX = 0.5, 1.5
MIN_NOTE_S, MIN_GAP_S = 0.050, 0.030            # organ.yaml timing defaults; --organ reads the real ones
REGISTER_PULSE_S, REGISTER_STAGGER_S, SETTLE_S = 0.100, 0.060, 0.250
QUIT_WINDOW_S = 2.0
FADE_MAX_S = 0.5                                # a skip lets what is sounding end where written, within this
STATUS_PERIOD_S = 1.0
WATCH_POLL_S = 1.0
PLAYLIST_SUFFIXES = (".m3u", ".m3u8", ".txt", ".playlist")
ORGAN_MESSAGES = ("note_on", "note_off", "control_change")


class Quit(BaseException):
    """Asked to stop from outside (SIGTERM): finish cleanly, silenced."""


# ----------------------------------------------------------------------------
# The queue: songs from files, folders and playlists
# ----------------------------------------------------------------------------

@dataclass
class Song:
    path: Path
    tempo: float | None = None          # a playlist line's own tempo, else the run's
    gap: float | None = None            # the pause after this song, else the run's
    id: int | None = None               # a playlist line's id=, for --watch

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
        ident = lineno
        for opt in opts.split():
            key, _, value = opt.partition("=")
            try:
                if key == "tempo":
                    tempo = parse_tempo(value)
                elif key == "gap":
                    gap = float(value)
                    if gap < 0:
                        raise ValueError("gap must be 0 or more")
                elif key == "id":
                    ident = int(value)
                else:
                    raise ValueError(f"unknown setting {key!r} (tempo=, gap=, id=)")
            except ValueError as e:
                raise ValueError(f"{path}:{lineno}: {e}") from None
        p = Path(target.strip())
        if not p.is_absolute():
            p = path.parent / p
        for song in expand([p], pattern):
            song.tempo, song.gap, song.id = tempo, gap, ident
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


class Playback:
    """One song on the wire, knowing where it is and what is sounding, so a
    skip can end cleanly."""

    def __init__(self, events: list[tuple[float, mido.Message]], min_note_s: float = MIN_NOTE_S) -> None:
        self.events = events
        self.min_note_s = min_note_s
        self.pos = 0
        self.t0: float | None = None
        self.sounding: dict[int, float] = {}          # note -> clock time it went on

    @property
    def length(self) -> float:
        return self.events[-1][0] if self.events else 0.0

    def position(self, clock) -> float:
        return 0.0 if self.t0 is None else min(clock() - self.t0, self.length)

    def _write(self, port, msg: mido.Message, clock) -> None:
        # bookkeeping first: an interrupt that lands in the write must still
        # find the note counted as sounding, or release() would leave it on
        if msg.type == "note_on" and msg.velocity > 0:
            self.sounding[msg.note] = clock()
        elif is_off(msg):
            self.sounding.pop(msg.note, None)
        port.write(bytes(msg.bytes()))

    def play(self, port, sleep=time.sleep, clock=time.monotonic, tick=None, period: float = STATUS_PERIOD_S) -> None:
        """Write each message at its time, measured from a fixed origin so
        delays never accumulate. With `tick`, long waits are cut into
        `period` slices and tick() is called between them."""
        self.t0 = clock()
        next_tick = self.t0 + period
        while self.pos < len(self.events):
            t, msg = self.events[self.pos]
            while True:
                now = clock()
                wait = self.t0 + t - now
                if wait <= 0:
                    break
                if tick is None:
                    sleep(wait)
                    break
                if now >= next_tick:                      # dense music: the tick falls between events
                    tick()
                    next_tick += period
                slice_ = min(wait, next_tick - now)
                if slice_ > 0:
                    sleep(slice_)
                if wait <= slice_:
                    break
            self._write(port, msg, clock)
            self.pos += 1
        port.flush()

    def release(self, port, sleep=time.sleep, clock=time.monotonic) -> None:
        """After a skip: the notes sounding now end where they are written,
        as long as that is within FADE_MAX_S; anything longer is cut, but
        never before its minimum length. Then All Notes Off."""
        if self.t0 is None:
            silence(port)
            return
        deadline = clock() + FADE_MAX_S
        for t, msg in self.events[self.pos:]:
            if self.t0 + t > deadline or not self.sounding:
                break
            if is_off(msg) and msg.note in self.sounding:
                wait = self.t0 + t - clock()
                if wait > 0:
                    sleep(wait)
                self._write(port, msg, clock)
        remaining = max((on + self.min_note_s for on in self.sounding.values()), default=0.0) - clock()
        if remaining > 0:
            sleep(remaining)
        silence(port)
        self.sounding.clear()


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


class Status:
    """A one-line JSON file, rewritten in place, for whatever wants to show
    what the organ is doing: a screen, a phone page, a log."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.fields: dict = {"pid": os.getpid()}

    def write(self, **fields) -> None:
        self.fields = {**self.fields, **fields}
        data = {"time": round(time.time(), 1), **self.fields}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.path)


class Pump:
    """A relay on a Pi GPIO for the bellows pump. Most relay modules pull in
    on a low input: --pump-active-low."""

    def __init__(self, pin: int, active_low: bool = False) -> None:
        from gpiozero import OutputDevice                  # imported here so nothing else needs it
        self.dev = OutputDevice(pin, active_high=not active_low, initial_value=False)

    def on(self) -> None:
        self.dev.on()

    def off(self) -> None:
        self.dev.off()

    def close(self) -> None:
        self.dev.close()


@dataclass
class Settings:
    tempo: float = 1.0
    gap: float = DEFAULT_GAP_S
    start: float = 0.0
    shuffle: bool = False
    repeat: bool = False
    warm_up: float = 0.0
    min_note_s: float = MIN_NOTE_S
    min_gap_s: float = MIN_GAP_S
    registers: list[tuple[int, int]] | None = None
    pattern: str = DEFAULT_PATTERN


class Player:
    """The run loop's shared parts: one song at a time, the status file, the
    pump, the console line."""

    def __init__(self, port, cfg: Settings, sleep=time.sleep, clock=time.monotonic,
                 out=None, status: Status | None = None, pump=None) -> None:
        self.port, self.cfg, self.sleep, self.clock = port, cfg, sleep, clock
        self.out = out or sys.stdout
        self.status, self.pump = status, pump

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def report(self, **fields) -> None:
        if self.status is not None:
            self.status.write(**fields)

    def stopped(self) -> int:
        silence(self.port)
        self.say("Stopped.")
        self.report(state="stopped")
        return 130

    def warm_up(self, total: int) -> bool:
        """Pump on and the wait for wind. False when interrupted."""
        if self.pump is not None:
            self.pump.on()
            self.say("pump on")
        if self.cfg.warm_up > 0:
            self.report(state="warming up", seconds=self.cfg.warm_up, index=0, total=total)
            self.say(f"waiting {self.cfg.warm_up:g} s for wind")
            try:
                self.sleep(self.cfg.warm_up)
            except KeyboardInterrupt:
                return False
        return True

    def play_song(self, song: Song, index: int, total: int, start: float = 0.0, extra: dict | None = None) -> str:
        """Returns 'played', 'skipped', 'quit' or 'unreadable'."""
        cfg = self.cfg
        speed = song.tempo if song.tempo is not None else cfg.tempo
        try:
            mid = mido.MidiFile(str(song.path))
            events = timeline(mid, speed, start, cfg.min_note_s, cfg.min_gap_s)
        except (OSError, ValueError, EOFError, KeyError, IndexError) as e:
            self.say(f"[{index}/{total}] {song.name}: cannot play: {e}")
            return "unreadable"
        pb = Playback(events, cfg.min_note_s)
        note = f"  from {clock_text(start)}" if start else ""
        self.say(f"[{index}/{total}] {song.name}  {clock_text(pb.length)}  tempo {speed:.0%}{note}")
        info = dict(song=song.name, path=str(song.path), id=song.id, index=index, total=total,
                    length_s=round(pb.length, 1), tempo=speed, start_s=start, **(extra or {}))
        self.report(state="playing", position_s=0.0, **info)
        silence(self.port)
        try:
            if start and cfg.registers:
                pulse_registers(registration_at(mid, speed, start, cfg.registers), self.port, self.sleep)
            tick = (lambda: self.report(state="playing", position_s=round(pb.position(self.clock), 1))) \
                if self.status else None
            pb.play(self.port, self.sleep, self.clock, tick)
        except KeyboardInterrupt:
            at = pb.position(self.clock)
            try:
                pb.release(self.port, self.sleep, self.clock)
            except KeyboardInterrupt:              # hammered: quit now, silenced
                return "quit"
            self.say(f"      skipped at {clock_text(at)}  (Ctrl+C again within {QUIT_WINDOW_S:.0f} s quits)")
            self.report(state="skipped", position_s=round(at, 1))
            try:
                self.sleep(QUIT_WINDOW_S)
            except KeyboardInterrupt:
                return "quit"
            return "skipped"
        silence(self.port)
        self.report(state="played", position_s=round(pb.length, 1))
        return "played"

    def pause(self, seconds: float) -> bool:
        """The silence between songs. False when interrupted."""
        self.report(state="pause", seconds=seconds)
        try:
            self.sleep(seconds)
        except KeyboardInterrupt:
            return False
        return True

    def finished(self) -> int:
        self.say("Finished.")
        self.report(state="finished")
        return 0

    def pump_off(self) -> None:
        if self.pump is not None:
            self.pump.off()
            self.say("pump off")


def run(songs: list[Song], port, cfg: Settings, sleep=time.sleep, clock=time.monotonic,
        out=None, rng: random.Random | None = None, status: Status | None = None, pump=None) -> int:
    """Play a fixed queue. Returns 0 at the end, 130 when quit with Ctrl+C."""
    rng = rng or random.Random()
    pl = Player(port, cfg, sleep, clock, out, status, pump)
    total = len(songs)
    try:
        if not pl.warm_up(total):
            return pl.stopped()
        time_round = 0
        while True:
            order = list(range(total))
            if cfg.shuffle:
                rng.shuffle(order)
            for k, i in enumerate(order):
                start = cfg.start if time_round == 0 and k == 0 else 0.0
                outcome = pl.play_song(songs[i], k + 1, total, start, {"round": time_round + 1})
                if outcome == "quit":
                    return pl.stopped()
                if outcome != "played":
                    continue
                last = k + 1 == total and not cfg.repeat
                if not last:
                    gap = songs[i].gap if songs[i].gap is not None else cfg.gap
                    if not pl.pause(gap):
                        return pl.stopped()
            if not cfg.repeat:
                break
            time_round += 1
        return pl.finished()
    except Quit:
        return pl.stopped()
    finally:
        pl.pump_off()


def read_queue(path: Path, pattern: str, say) -> list[Song]:
    """The live queue file, or nothing while it is missing or broken."""
    try:
        return read_playlist(path, pattern) if path.is_file() else []
    except (OSError, ValueError, FileNotFoundError) as e:
        say(f"queue: {e}")
        return []


def run_watch(queue: Path, port, cfg: Settings, sleep=time.sleep, clock=time.monotonic,
              out=None, status: Status | None = None, pump=None) -> int:
    """Play a live queue file: re-read before every song, the first entry not
    yet played is next; idle when nothing is left. Runs until told to quit."""
    pl = Player(port, cfg, sleep, clock, out, status, pump)
    played: set[int] = set()
    idle_said = False
    try:
        if not pl.warm_up(0):
            return pl.stopped()
        while True:
            songs = read_queue(queue, cfg.pattern, pl.say)
            waiting = [s for s in songs if s.id not in played]
            if not waiting:
                if cfg.repeat and played and songs:
                    played.clear()
                    continue
                if not idle_said:
                    pl.say("idle: waiting for the queue")
                    idle_said = True
                pl.report(state="idle", queue=len(songs), song=None, id=None)
                try:
                    sleep(WATCH_POLL_S)
                except KeyboardInterrupt:
                    return pl.stopped()
                continue
            idle_said = False
            song = waiting[0]
            played.add(song.id)
            index = len(songs) - len(waiting) + 1
            outcome = pl.play_song(song, index, len(songs), 0.0, {"queue": len(songs)})
            if outcome == "quit":
                return pl.stopped()
            if outcome != "played":
                continue
            songs = read_queue(queue, cfg.pattern, pl.say)
            more = any(s.id not in played for s in songs) or (cfg.repeat and bool(songs))
            if more:
                gap = song.gap if song.gap is not None else cfg.gap
                if not pl.pause(gap):
                    return pl.stopped()
    except Quit:
        return pl.stopped()
    finally:
        pl.pump_off()


def install_signals() -> None:
    """SIGTERM quits cleanly; SIGUSR1 (where it exists) skips like Ctrl+C.
    Both arrive as exceptions in the main thread, wherever it is sleeping."""

    def quit_(signum, frame):
        raise Quit()

    def skip(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, quit_)
    if hasattr(signal, "SIGUSR1"):
        signal.signal(signal.SIGUSR1, skip)


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
    p.add_argument("--watch", action="store_true",
                   help="the one playlist given is a live queue: re-read before every song, idle when empty")
    p.add_argument("--status", metavar="FILE", help="rewrite this one-line JSON file about once a second with what is playing")
    p.add_argument("--pump", type=int, metavar="GPIO", help="BCM GPIO of the bellows pump relay: on before the music, off after")
    p.add_argument("--pump-active-low", action="store_true", help="the relay pulls in on a low input (most modules do)")
    p.add_argument("--warm-up", type=float, default=0.0, metavar="SECONDS", help="wait for wind before the first note")
    p.add_argument("--list", action="store_true", help="print the queue in play order and exit")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    cfg = Settings(gap=a.gap, start=a.start, shuffle=a.shuffle, repeat=a.repeat, warm_up=a.warm_up, pattern=a.pattern)
    songs: list[Song] = []
    queue: Path | None = None
    try:
        cfg.tempo = parse_tempo(a.tempo)
        if a.gap < 0 or a.start < 0 or a.warm_up < 0:
            raise ValueError("--gap, --start and --warm-up must be 0 or more")
        if a.pump is not None and not 0 <= a.pump <= 27:
            raise ValueError("--pump must be a BCM GPIO number, 0-27")
        if a.organ:
            cfg.min_note_s, cfg.min_gap_s, cfg.registers = read_organ(a.organ)
        if a.watch:
            if len(a.items) != 1 or Path(a.items[0]).suffix.lower() not in PLAYLIST_SUFFIXES:
                raise ValueError("--watch takes exactly one playlist file (it need not exist yet)")
            if a.shuffle or a.start or a.list:
                raise ValueError("--watch does not go with --shuffle, --start or --list: the file's writer decides the order")
            queue = Path(a.items[0])
        else:
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

    status = Status(a.status) if a.status else None
    pump = None
    if a.pump is not None:
        try:
            pump = Pump(a.pump, a.pump_active_low)
        except ImportError:
            print("error: --pump needs gpiozero: pip install gpiozero lgpio", file=sys.stderr)
            return 2
        except Exception as e:                         # gpiozero's own errors: no such pin, pin in use
            print(f"error: cannot drive GPIO {a.pump}: {e}", file=sys.stderr)
            return 2
    install_signals()

    def go(port) -> int:
        if queue is not None:
            return run_watch(queue, port, cfg, status=status, pump=pump)
        return run(songs, port, cfg, status=status, pump=pump)

    try:
        if a.dry_run:
            what = f"live queue {queue}" if queue is not None else f"{len(songs)} song{'s' if len(songs) != 1 else ''}"
            print(f"dry run: {what}, nothing on the wire")
            return go(NullPort())

        try:
            import serial
        except ImportError:
            print("error: needs pyserial: pip install pyserial (or use --dry-run)", file=sys.stderr)
            return 2
        try:
            with serial.Serial(a.device, baudrate=MIDI_BAUD) as port:
                return go(port)
        except (serial.SerialException, OSError, ValueError) as e:
            print(f"error: cannot open {a.device}: {e}", file=sys.stderr)
            return 2
    finally:
        if pump is not None:
            pump.close()


if __name__ == "__main__":
    sys.exit(main())
