#!/usr/bin/env python3
"""
organ_web -- the organ's front desk: a web page served by the Pi, for a
touchscreen on the case and for any phone on the same Wi-Fi.

It owns everything around the music and nothing of the music itself. The
player (tools/player/grinder.py, run in --watch mode as a child of this
process) reads a queue file before every song and writes a status file
once a second; this app edits the one and reads the other, and sends the
player a signal to skip. If this app dies, the song keeps playing.

    organ_web.py --library ~/organ/tunes --organ ../organ-arranger/instrument/organ.yaml
    organ_web.py --library ~/organ/tunes --organ ... --pump 24 --pump-active-low --warm-up 8
    organ_web.py --library ./tunes --organ ... --dry-run --port 8080     # on a laptop: no serial, no GPIO

What it does:

  Play        the queue: what is playing, what is next; skip, stop, reorder
  Library     the tune folders under --library; tap to queue or play now
  Playlists   saved lists in the state folder; load, save the queue as one
  Upload      drop arranged .organ.mid files straight into a library folder
  Arrange     drop a raw .mid: the transcriber and the arranger run on it,
              with a plan if one is chosen; the result lands in library/uploads
  Service     reset the boards, the pump by hand, and a touch version of
              organ_keys for when the player is idle
  Settings    tempo, the pause between songs, repeat, the pump's warm-up
              and idle time-out

The pump, and the organ's power: this app owns both relays, not the player.
A play request while they are off switches the supply on, then the pump, and
holds the songs back for the warm-up; when the player has been idle for the
idle time-out the pump goes off and the supply after it.

State lives in --state (default ~/.local/share/organ-web): queue.m3u,
status.json, settings.json, playlists/. Nothing here needs root.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


def static_build(folder: Path = HERE / "static") -> int:
    """A token that changes whenever a page file changes on disk: the newest mtime.
    The page compares it every second and reloads itself, so a git pull reaches a
    kiosk that has been showing the page for a week."""
    try:
        return max(int(f.stat().st_mtime) for f in folder.iterdir() if f.is_file())
    except (OSError, ValueError):
        return 0
TOOLS = HERE.parent
sys.path.insert(0, str(TOOLS / "player"))
sys.path.insert(0, str(TOOLS / "organ-config"))

import grinder  # noqa: E402
import organ_config  # noqa: E402
import organ_keys  # noqa: E402

__version__ = "0.1.0"

DEFAULT_PORT = 8080
DEFAULT_SETTINGS = {"tempo": 1.0, "gap": 3.0, "repeat": False, "warm_up": 8.0, "idle_off": 180.0,
                    "power_switch": True,   # the physical on-off switch may shut the organ down; off if it misbehaves
                    "boards": {}}       # what was last sent to the driver boards: they cannot be read back
BOARD_FIELDS = {"peak": (1, 100), "hold": (0, organ_config.HOLD_DUTY_MAX_PERCENT),
                "peak_ms": (1, organ_config.PEAK_DURATION_MAX_MS), "max_note": (0, 127),
                "exercise": (0, organ_config.EXERCISE_CYCLES_MAX)}
BOARD_COMMANDS = {"save": organ_config.CMD_SAVE, "reload": organ_config.CMD_RELOAD, "factory": organ_config.CMD_FACTORY}
# what this organ runs at (the bench values in the README; no exercise passes,
# since its solenoids start cleanly under wind and the supply is switched
# through the day) and what the firmware compiles in; the page fills its
# fields with the first and shows the second as hints
BOARD_DEFAULTS = {"peak": 60, "hold": 25, "peak_ms": 40, "max_note": 30, "exercise": 0}
FIRMWARE_DEFAULTS = {"peak": 100, "hold": 25, "peak_ms": 40, "max_note": 30, "exercise": 2}
PLAYER_RESTART_S = 2.0
HOUSEKEEP_S = 1.0
KEYS_TICK_S = 0.01
SOURCE_SUFFIXES = (".mid", ".midi")


# ----------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------

@dataclass
class Config:
    library: Path
    organ: Path
    state: Path
    plans: Path = TOOLS / "organ-arranger" / "tunes"
    device: str = grinder.DEFAULT_DEVICE
    python: str = sys.executable
    pump_pin: int | None = None
    pump_active_low: bool = False
    power_pin: int | None = None            # the organ's 12 V supply: an ATX PS_ON through an opto
    power_active_low: bool = False
    reset_pins: list[int] = field(default_factory=lambda: [17])
    dry_run: bool = False
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    backlight: Path = Path("/sys/class/backlight")     # where the kernel exposes a display's backlight, if any
    shm: Path = Path("/dev/shm")                        # RAM-backed folder, when the system has one

    @property
    def queue_file(self) -> Path:
        return self.state / "queue.m3u"

    @property
    def status_file(self) -> Path:
        """Rewritten every second by the player: in RAM when the system offers it, so an
        SD card is not written once a second for the life of the organ."""
        if self.shm.is_dir():
            return self.shm / "organ-web-status.json"
        return self.state / "status.json"

    @property
    def settings_file(self) -> Path:
        return self.state / "settings.json"

    @property
    def playlists(self) -> Path:
        return self.state / "playlists"

    @property
    def uploads(self) -> Path:
        return self.library / "uploads"


# ----------------------------------------------------------------------------
# The library: what can be played
# ----------------------------------------------------------------------------

class Library:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lengths: dict[str, tuple[float, float]] = {}      # rel path -> (mtime, seconds)

    def length(self, path: Path) -> float | None:
        rel = str(path.relative_to(self.root))
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return None
        hit = self._lengths.get(rel)
        if hit and hit[0] == mtime:
            return hit[1]
        try:
            import mido
            seconds = round(mido.MidiFile(str(path)).length, 1)
        except Exception:
            seconds = None
        if seconds is not None:
            self._lengths[rel] = (mtime, seconds)
        return seconds

    def tunes(self, pattern: str = grinder.DEFAULT_PATTERN) -> list[dict]:
        out = []
        if not self.root.is_dir():
            return out
        for p in sorted(self.root.rglob(pattern)):
            if not p.is_file():
                continue
            rel = p.relative_to(self.root)
            out.append({"path": rel.as_posix(), "name": grinder.Song(p).name,
                        "folder": rel.parent.as_posix() if rel.parent != Path(".") else "",
                        "length_s": self.length(p)})
        return out

    def folders(self) -> list[str]:
        seen = []
        for t in self.tunes():
            if t["folder"] not in seen:
                seen.append(t["folder"])
        return seen

    def resolve(self, rel: str) -> Path:
        """A library-relative path, refused if it points outside the library."""
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"{rel}: outside the library")
        return p


# ----------------------------------------------------------------------------
# The player process
# ----------------------------------------------------------------------------

class PlayerProcess:
    """grinder --watch as a child, restarted if it dies."""

    def __init__(self, cfg: Config, spawn=None) -> None:
        self.cfg = cfg
        self.spawn = spawn or self._spawn
        self.proc = None
        self.started_at = 0.0
        self.closing = False
        self.skip_signal = getattr(signal, "SIGUSR1", None)      # None where the platform has no such thing
        self.pause_signal = getattr(signal, "SIGUSR2", None)

    def command(self) -> list[str]:
        cmd = [self.cfg.python, str(TOOLS / "player" / "grinder.py"), str(self.cfg.queue_file), "--watch",
               "--status", str(self.cfg.status_file), "--organ", str(self.cfg.organ)]
        if self.cfg.dry_run:
            cmd.append("--dry-run")
        else:
            cmd += ["--device", self.cfg.device]
        return cmd

    def _spawn(self, cmd: list[str]):
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def ensure(self, now: float) -> None:
        if self.closing or self.running():
            return
        if now - self.started_at < PLAYER_RESTART_S:
            return
        self.proc = self.spawn(self.command())
        self.started_at = now

    def skip(self) -> bool:
        if not self.running() or self.skip_signal is None:
            return False
        self.proc.send_signal(self.skip_signal)
        return True

    def pause(self) -> bool:
        """Pauses a playing song, resumes a paused one; the player decides which."""
        if not self.running() or self.pause_signal is None:
            return False
        self.proc.send_signal(self.pause_signal)
        return True

    def close(self) -> None:
        self.closing = True
        if self.running():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


# ----------------------------------------------------------------------------
# The pump
# ----------------------------------------------------------------------------

class FakePump:
    def __init__(self) -> None:
        self.state = False

    def on(self) -> None:
        self.state = True

    def off(self) -> None:
        self.state = False

    def close(self) -> None:
        pass


# ----------------------------------------------------------------------------
# The keys tester over the wire, when the player is idle
# ----------------------------------------------------------------------------

class KeysDesk:
    """organ_keys' Console driven from HTTP: pulses, holds and rolls, ticked
    by a thread. Opened on demand, closed when the player has work."""

    def __init__(self, cfg: Config, labels: dict[int, str], groups, section_labels, open_port) -> None:
        self.cfg = cfg
        self.open_port = open_port
        self.port = None
        self.console = None
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.labels, self.groups, self.section_labels = labels, groups, section_labels

    def layout(self) -> dict:
        boards = [{"name": f"board {b + 1}", "solenoids": list(range(b * 16 + 1, b * 16 + 17))} for b in range(4)]
        return {"boards": boards, "sections": [{"name": n, "solenoids": s} for n, s in self.groups],
                "labels": {str(k): v for k, v in self.labels.items()},
                "section_labels": {str(k): v for k, v in self.section_labels.items()},
                "snare": self.console.snare_pair() if self.console else organ_keys.Console(
                    send=lambda m: None, labels=self.labels).snare_pair()}

    def open(self) -> None:
        with self.lock:
            if self.console is not None:
                return
            self.port = self.open_port()
            first = int(self._solenoid_1_note)
            self.console = organ_keys.Console(send=lambda m: self.port.write(bytes(m.bytes())),
                                              solenoid_1_note=first, labels=self.labels,
                                              sections=self.groups, section_labels=self.section_labels)
            self.thread = threading.Thread(target=self._run, name="keys", daemon=True)
            self.thread.start()

    _solenoid_1_note = 0

    def _run(self) -> None:
        while True:
            with self.lock:
                c = self.console
                if c is None:
                    return
                c.tick(time.monotonic())
            time.sleep(KEYS_TICK_S)

    def close(self) -> None:
        with self.lock:
            if self.console is None:
                return
            self.console.all_off()
            self.console = None
            try:
                self.port.close()
            except Exception:
                pass
            self.port = None

    def act(self, what: str, body: dict) -> dict:
        self.open()
        now = time.monotonic()
        with self.lock:
            c = self.console
            if what == "pulse":
                s = int(body["solenoid"])
                c.pulse(s, now, int(body.get("ms", c.pulse_ms)))
            elif what == "hold":
                s = int(body["solenoid"])
                if body.get("on", True):
                    c.on(s, now)
                else:
                    c.off(s)
            elif what == "roll":
                if body.get("on", True):
                    targets = [int(x) for x in body.get("solenoids", [])] or c.snare_pair()
                    c.roll_interval_ms = max(organ_keys.ROLL_MIN_MS, min(organ_keys.ROLL_MAX_MS,
                                                                        int(body.get("interval_ms", c.roll_interval_ms))))
                    c.start_roll(targets, now)
                else:
                    c.roll_targets = []
            elif what == "off":
                c.all_off()
            else:
                raise ValueError(f"unknown keys action {what!r}")
            return {"sounding": sorted(c.sounding), "rolling": list(c.roll_targets)}


# ----------------------------------------------------------------------------
# The desk: queue, settings, pump, jobs
# ----------------------------------------------------------------------------

class Desk:
    def __init__(self, cfg: Config, now=time.monotonic, spawn=None, pump=None, run_cmd=None, open_port=None,
                 power=None) -> None:
        self.cfg = cfg
        self.now = now
        cfg.state.mkdir(parents=True, exist_ok=True)
        cfg.playlists.mkdir(parents=True, exist_ok=True)
        cfg.uploads.mkdir(parents=True, exist_ok=True)
        self.library = Library(cfg.library)
        self.settings = dict(DEFAULT_SETTINGS)
        self.next_id = 1
        self._load_settings()
        self.queue: list[dict] = []
        self.pending: list[dict] = []           # held back for the warm-up
        self.held = False                       # stopped: the queue is kept, but the player gets none of it
        self.wind_ready_at: float | None = None
        self.round: list[dict] = []             # what was added since the last idle, for repeat
        self.player = PlayerProcess(cfg, spawn)
        self.pump = pump
        self.pump_on = False
        self.power = power                      # the solenoid supply's relay, if the Pi has one
        self.power_on = False
        self.power_switch = None                # the PowerSwitch, when one is configured
        self.idle_since: float | None = None
        self.status: dict = {}
        self.jobs: dict[str, dict] = {}
        self.run_cmd = run_cmd or self._run_cmd
        self.lock = threading.RLock()
        import yaml
        with open(cfg.organ, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        labels, first = organ_keys.labels_from_organ(raw)
        groups, section_labels = organ_keys.groups_from_organ(raw)
        self.keys = KeysDesk(cfg, labels, groups, section_labels, open_port or self._open_port)
        self.keys._solenoid_1_note = first
        self.registers = [r.get("name") for r in raw.get("registers") or []]
        self._written: list[dict] = []
        try:
            cfg.status_file.unlink()            # a status left by an earlier run says nothing about now
        except OSError:
            pass
        self.write_queue()

    # -- persistence ---------------------------------------------------------

    def _load_settings(self) -> None:
        try:
            data = json.loads(self.cfg.settings_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for k in DEFAULT_SETTINGS:
            if k in data:
                self.settings[k] = data[k]
        self.next_id = int(data.get("next_id", 1))

    def _save_settings(self) -> None:
        data = {**self.settings, "next_id": self.next_id}
        tmp = self.cfg.settings_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.cfg.settings_file)

    def write_queue(self) -> None:
        """What the player should play: the entries not yet played, none while
        stopped. Rewritten only when the text changes."""
        lines = ["# organ_web queue: edited by the app, read by grinder --watch before every song"]
        for e in ([] if self.held else [e for e in self.queue if not e.get("done")]):
            opts = [f"id={e['id']}", f"tempo={e.get('tempo') or self.settings['tempo']:.3f}",
                    f"gap={e.get('gap') if e.get('gap') is not None else self.settings['gap']:g}"]
            lines.append(f"{e['abs']} | {' '.join(opts)}")
            e["sent"] = True                    # the player may know this id from now on
        text = "\n".join(lines) + "\n"
        if text == getattr(self, "_queue_text", None) and self.cfg.queue_file.is_file():
            return
        tmp = self.cfg.queue_file.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.cfg.queue_file)
        self._queue_text = text
        self.last_write = time.time()

    def read_status(self) -> dict:
        try:
            return json.loads(self.cfg.status_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    # -- the queue: the whole programme, with a cursor --------------------------

    def entry(self, rel: str, tempo=None, gap=None) -> dict:
        p = self.library.resolve(rel)
        if not p.is_file():
            raise FileNotFoundError(f"{rel}: not in the library")
        e = {"id": self.next_id, "path": rel, "abs": str(p), "name": grinder.Song(p).name,
             "tempo": tempo, "gap": gap, "length_s": self.library.length(p), "done": False}
        self.next_id += 1
        return e

    def current_id(self):
        """The id the player is on, or was on a moment ago."""
        if self.status.get("state") in ("playing", "paused", "pause", "skipped", "played"):
            return self.status.get("id")
        return None

    def cursor(self) -> int:
        """Index of the first entry not yet played: the one playing, or the next up."""
        for i, e in enumerate(self.queue):
            if not e.get("done"):
                return i
        return len(self.queue)

    def floor(self) -> int:
        """The first index a song may be moved to: after the one that is playing."""
        c = self.cursor()
        playing = (self.status.get("state") in ("playing", "paused") and c < len(self.queue)
                   and self.queue[c]["id"] == self.status.get("id") and not self.held)
        return c + 1 if playing else c

    def add(self, rels: list[str], shuffle: bool = False, play_now: bool = False,
            tempos: list | None = None, gaps: list | None = None) -> list[dict]:
        with self.lock:
            entries = [self.entry(r, (tempos or [None] * len(rels))[i], (gaps or [None] * len(rels))[i])
                       for i, r in enumerate(rels)]
            if shuffle:
                random.shuffle(entries)
            self._save_settings()
            if self.held and play_now:
                # the new song first, then what was still to come, back to life
                self.held = False
                upcoming = [e for e in self.queue if not e.get("done")]
                self.queue = [e for e in self.queue if e.get("done")]
                self._renumber(upcoming)
                entries = entries + upcoming
            flowing = (self.status.get("state") in ("playing", "paused", "pause", "skipped")
                       or bool(self.to_come()) or bool(self.pending))
            if not play_now and (self.held or not flowing):
                # nothing is playing: the songs take their place and wait for Play
                self.held = True
                self.queue += entries
                self.write_queue()
                return entries
            self.wind_up()
            if self.wind_ready_at is not None and self.now() < self.wind_ready_at:
                self.pending = entries + self.pending if play_now else self.pending + entries
                return entries
            if play_now:
                at = self.floor()
                playing = at > self.cursor()
                self.queue[at:at] = entries
                self.write_queue()
                if playing:
                    self.player.skip()
            else:
                self.queue += entries
                self.write_queue()
            return entries

    def remove(self, ident: int) -> None:
        with self.lock:
            self.queue = [e for e in self.queue if e["id"] != ident]
            self.pending = [e for e in self.pending if e["id"] != ident]
            self.write_queue()

    def move(self, ident: int, to: int) -> None:
        with self.lock:
            e = next((x for x in self.queue if x["id"] == ident), None)
            playing_now = (not self.held and e is not None and e["id"] == self.status.get("id")
                           and self.status.get("state") in ("playing", "paused"))
            if e is None or e.get("done") or playing_now:
                return                              # what has played, and what is playing, stays where it is
            rest = [x for x in self.queue if x["id"] != ident]
            self.queue = rest
            at = max(self.floor(), min(to, len(rest)))
            rest.insert(at, e)
            self.queue = rest
            self.write_queue()

    def clear(self) -> None:
        """Everything but the song that is playing, played ones included."""
        with self.lock:
            current = self.status.get("id") if self.status.get("state") in ("playing", "paused") else None
            self.queue = [e for e in self.queue if e["id"] == current and not self.held]
            self.pending = []
            self.held = False
            self.write_queue()

    def clear_played(self) -> None:
        with self.lock:
            self.queue = [e for e in self.queue if not e.get("done")]
            self.write_queue()

    def jump(self, ident: int) -> None:
        """Move the cursor to this song: everything before it counts as played,
        it and everything after are to come, and it starts now -- or, while
        stopped, it becomes the selected one for Play."""
        with self.lock:
            ids = [e["id"] for e in self.queue]
            if ident not in ids:
                raise FileNotFoundError(f"no entry {ident} in the queue")
            i = ids.index(ident)
            playing = self.status.get("state") in ("playing", "paused") and not self.held
            for e in self.queue[:i]:
                e["done"] = True
            self._renumber(self.queue[i:])
            self.write_queue()
            if playing:
                self.player.skip()

    def _renumber(self, entries: list[dict]) -> None:
        """Marked as still to come, and, for any the player may already know,
        a fresh id so it takes them as new -- including one it has played or
        skipped. Entries it has never been given keep theirs."""
        for e in entries:
            if e.get("sent"):
                e["id"] = self.next_id
                self.next_id += 1
                e["sent"] = False
            e["done"] = False
        self._save_settings()

    def shuffle(self) -> None:
        """What is still to come, in a new order; what has played stays put."""
        with self.lock:
            at = self.floor()
            head, rest = self.queue[:at], self.queue[at:]
            random.shuffle(rest)
            self.queue = head + rest
            self.write_queue()

    def skip(self) -> bool:
        return self.player.skip()

    def pause(self) -> bool:
        """Pause or resume, whichever applies; nothing between songs."""
        if self.status.get("state") not in ("playing", "paused"):
            return False
        return self.player.pause()

    def stop(self) -> None:
        """End what is playing and hold: the song that was playing stays the
        selected one, and Play starts it from the top."""
        with self.lock:
            self.held = True
            self.queue += self.pending          # anything still waiting for wind is kept too
            self.pending = []
            self.wind_ready_at = None
            self.write_queue()                  # nothing for the player; it idles after the skip
            self.player.skip()

    def play(self) -> None:
        """After a stop: from the selected song on."""
        with self.lock:
            if not self.held:
                return
            self.held = False
            upcoming = [e for e in self.queue if not e.get("done")]
            self._renumber(upcoming)
            self.wind_up()
            if self.wind_ready_at is not None and self.now() < self.wind_ready_at:
                self.queue = [e for e in self.queue if e.get("done")]
                self.pending = upcoming + self.pending
            self.write_queue()

    def upcoming(self) -> list[dict]:
        """The programme: each entry with done (already played), now (playing,
        or selected while stopped)."""
        current = self.current_id()
        c = self.cursor()
        out = []
        for i, e in enumerate(self.queue):
            now = (i == c) if self.held else (e["id"] == current and not e.get("done"))
            out.append(dict(e, now=now, done=bool(e.get("done"))))
        return out

    def to_come(self) -> list[dict]:
        return [e for e in self.queue if not e.get("done")]

    # -- settings -----------------------------------------------------------

    def set_settings(self, changes: dict) -> dict:
        with self.lock:
            if "tempo" in changes:
                self.settings["tempo"] = grinder.parse_tempo(changes["tempo"])
            for k in ("gap", "warm_up", "idle_off"):
                if k in changes:
                    v = float(changes[k])
                    if v < 0:
                        raise ValueError(f"{k} must be 0 or more")
                    self.settings[k] = v
            if "repeat" in changes:
                self.settings["repeat"] = bool(changes["repeat"])
            if "power_switch" in changes:
                self.settings["power_switch"] = bool(changes["power_switch"])
            self._save_settings()
            self.write_queue()                  # tempo and gap reach the lines not yet played
            return dict(self.settings)

    # -- power and wind --------------------------------------------------------

    def pump_set(self, on: bool) -> None:
        if self.pump is None:
            return
        if on:
            self.pump.on()
        else:
            self.pump.off()
        self.pump_on = on
        if not on:
            self.wind_ready_at = None

    def power_set(self, on: bool) -> None:
        """The organ's 12 V supply. Off takes the pump with it: no supply, no boards."""
        if self.power is None:
            return
        if on:
            self.power.on()
        else:
            if self.pump_on:
                self.pump_set(False)
            self.power.off()
        self.power_on = on

    def wind_up(self) -> None:
        """Before music: the supply on, then the pump, and if anything had to
        be switched on, the songs wait out the warm-up -- for the boards to
        boot and the reservoir to fill."""
        switched = False
        if self.power is not None and not self.power_on:
            self.power_set(True)
            switched = True
        if self.pump is not None and not self.pump_on:
            self.pump_set(True)
            switched = True
        if switched and self.settings["warm_up"] > 0:
            self.wind_ready_at = self.now() + float(self.settings["warm_up"])

    def ensure_power(self, what: str) -> None:
        """Service actions need the boards alive. Switch the supply on and ask
        the user to come back in a moment rather than talk to dead boards."""
        if self.power is not None and not self.power_on:
            self.power_set(True)
            self.idle_since = self.now()
            raise RuntimeError(f"the organ was off: powering up for {what}, try again in a few seconds")

    # -- housekeeping, once a second ------------------------------------------

    def housekeep(self) -> None:
        now = self.now()
        with self.lock:
            self.player.ensure(now)
            st = self.read_status()
            self.status = st
            state = st.get("state")
            current = st.get("id")
            fresh = st.get("time", 0) > getattr(self, "last_write", 0)
            if self.held:
                pass                                # the programme is frozen; the player's reports are about the past
            elif state in ("playing", "paused") and current is not None:
                ids = [e["id"] for e in self.queue]
                if current in ids:
                    for e in self.queue[:ids.index(current)]:
                        e["done"] = True
            elif state in ("played", "skipped") and current is not None:
                for e in self.queue:
                    if e["id"] == current:
                        e["done"] = True
            elif state == "idle" and fresh and not self.pending:
                # an idle reported after our last write means the player saw the
                # file and found nothing left; an older one is just late
                for e in self.queue:
                    e["done"] = True
            # the warm-up is over: release what was held back
            if self.pending and (self.wind_ready_at is None or now >= self.wind_ready_at):
                self.queue += self.pending
                self.pending = []
            # repeat: when the whole programme has played, start it again
            if (state == "idle" and fresh and self.queue and not self.to_come() and not self.pending
                    and not self.held and self.settings["repeat"]):
                self._renumber(self.queue)
            self.write_queue()
            # the pump: off after idling long enough
            busy = self.busy()
            if busy:
                self.idle_since = None
            elif self.idle_since is None:
                self.idle_since = now
            if ((self.pump_on or self.power_on) and self.idle_since is not None and self.settings["idle_off"] > 0
                    and now - self.idle_since >= self.settings["idle_off"]):
                self.pump_set(False)
                self.power_set(False)
            # the keys tester must not share the wire with a song
            if self.keys.console is not None and busy:
                self.keys.close()

    def counts(self) -> dict:
        """Library and playlist sizes for the sidebar, rescanned at most every few seconds."""
        now = self.now()
        cached = getattr(self, "_counts", None)
        if cached and now - cached[0] < 5.0:
            return cached[1]
        tunes = self.library.tunes()
        by_folder: dict[str, int] = {}
        for tune in tunes:
            by_folder[tune["folder"]] = by_folder.get(tune["folder"], 0) + 1
        data = {"tunes": len(tunes), "folders": len(by_folder), "by_folder": by_folder,
                "playlists": len(list(self.cfg.playlists.glob("*.m3u")))}
        self._counts = (now, data)
        return data

    def snapshot(self) -> dict:
        with self.lock:
            st = dict(self.status)
            jobs = list(self.jobs.values())
            return {
                "status": st,
                "counts": {**self.counts(), "jobs_running": sum(1 for j in jobs if j["state"] == "running"),
                           "jobs_done": sum(1 for j in jobs if j["state"] == "done")},
                "queue": self.upcoming(),
                "pending": [dict(e) for e in self.pending],
                "warming_up_s": (max(0.0, self.wind_ready_at - self.now())
                                 if self.wind_ready_at is not None and self.now() < self.wind_ready_at else 0.0),
                "pump": {"configured": self.pump is not None, "on": self.pump_on},
                "power": {"configured": self.power is not None, "on": self.power_on},
                "switch": ({"configured": True, "closed": bool(self.power_switch.button.is_pressed),
                            "enabled": bool(self.settings.get("power_switch", True))}
                           if self.power_switch is not None else {"configured": False}),
                "player": {"running": self.player.running(), "pid": st.get("pid")},
                "settings": dict(self.settings),
                "board_defaults": dict(BOARD_DEFAULTS),
                "firmware_defaults": dict(FIRMWARE_DEFAULTS),
                "keys_open": self.keys.console is not None,
                "backlight": self.backlight_dir() is not None,
                "idle": not self.busy(),
                "held": self.held,
                "dry_run": self.cfg.dry_run,
                "version": __version__,
                "build": static_build(),
            }

    # -- playlists ------------------------------------------------------------

    def playlist_path(self, name: str) -> Path:
        safe = "".join(c for c in name.strip() if c.isalnum() or c in " -_.,()'&").strip()
        if not safe:
            raise ValueError("a playlist needs a name")
        return self.cfg.playlists / f"{safe}.m3u"

    def playlists(self) -> list[dict]:
        out = []
        for p in sorted(self.cfg.playlists.glob("*.m3u")):
            try:
                entries = self.read_playlist(p.stem)
            except (OSError, ValueError):
                entries = []
            out.append({"name": p.stem, "count": len(entries)})
        return out

    def read_playlist(self, name: str) -> list[dict]:
        p = self.playlist_path(name)
        if not p.is_file():
            raise FileNotFoundError(f"no playlist {name!r}")
        out = []
        for raw in p.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            target, _, opts = line.partition("|")
            e = {"path": target.strip(), "tempo": None, "gap": None}
            for opt in opts.split():
                k, _, v = opt.partition("=")
                if k == "tempo":
                    e["tempo"] = grinder.parse_tempo(v)
                elif k == "gap":
                    e["gap"] = float(v)
            try:
                full = self.library.resolve(e["path"])
                e["name"] = grinder.Song(full).name
                e["missing"] = not full.is_file()
            except ValueError:
                e["name"], e["missing"] = e["path"], True
            out.append(e)
        return out

    def write_playlist(self, name: str, entries: list[dict]) -> None:
        lines = [f"# {name}"]
        for e in entries:
            self.library.resolve(e["path"])
            opts = []
            if e.get("tempo") is not None:
                opts.append(f"tempo={float(e['tempo']):.3f}")
            if e.get("gap") is not None:
                opts.append(f"gap={float(e['gap']):g}")
            lines.append(e["path"] + (f" | {' '.join(opts)}" if opts else ""))
        self.playlist_path(name).write_text("\n".join(lines) + "\n", encoding="utf-8")

    def delete_playlist(self, name: str) -> None:
        p = self.playlist_path(name)
        if p.is_file():
            p.unlink()
        self._counts = None

    def queue_playlist(self, name: str, shuffle: bool = False, replace: bool = False) -> list[dict]:
        entries = [e for e in self.read_playlist(name) if not e.get("missing")]
        if replace:
            self.clear()
        added = self.add([e["path"] for e in entries], shuffle,
                         tempos=[e["tempo"] for e in entries], gaps=[e["gap"] for e in entries])
        if replace:
            self.play()                         # "play instead" means now
        return added

    # -- upload and arrange ------------------------------------------------------

    def plans(self) -> list[str]:
        return sorted(p.name[: -len(".plan.yaml")] for p in self.cfg.plans.glob("*.plan.yaml"))

    def _run_cmd(self, cmd: list[str]) -> tuple[int, str]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    def start_arrange(self, filename: str, data: bytes, plan: str | None, transpose: str | None,
                      background: bool = True) -> str:
        stem = grinder.Song(Path(filename)).name              # 'x.organ.mid' and 'x.mid' both give 'x'
        safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in stem).strip("-").lower() or "upload"
        job_id = uuid.uuid4().hex[:8]
        job = {"id": job_id, "state": "running", "name": safe, "log": "", "output": None,
               "started": time.time(), "plan": plan or "", "transpose": transpose or "auto"}
        self.jobs[job_id] = job
        srcdir = self.cfg.uploads / "src"
        srcdir.mkdir(parents=True, exist_ok=True)
        suffix = Path(filename).suffix.lower()
        src = srcdir / f"{safe}{suffix if suffix in SOURCE_SUFFIXES else '.mid'}"
        src.write_bytes(data)
        if filename.lower().endswith(".organ.mid"):
            out = self.cfg.uploads / f"{safe}.organ.mid"
            out.write_bytes(data)
            job.update(state="done", output=out.relative_to(self.cfg.library).as_posix(),
                       log="already arranged: added to uploads")
            return job_id
        if background:
            threading.Thread(target=self._arrange, args=(job, src, plan, transpose), daemon=True).start()
        else:
            self._arrange(job, src, plan, transpose)
        return job_id

    def _arrange(self, job: dict, src: Path, plan: str | None, transpose: str | None) -> None:
        arranger = TOOLS / "organ-arranger"
        fororgan = src.with_name(f"{job['name']}.fororgan.mid")
        out = self.cfg.uploads / f"{job['name']}.organ.mid"
        cmd = [self.cfg.python, str(arranger / "organ_transcribe.py"), str(src), "--organ", str(self.cfg.organ),
               "-o", str(fororgan), "--report", str(fororgan.with_suffix(".txt"))]
        if plan:
            planfile = self.cfg.plans / f"{plan}.plan.yaml"
            if not planfile.is_file():
                job.update(state="failed", log=f"no plan {plan!r}")
                return
            cmd += ["--plan", str(planfile)]
        else:
            cmd += ["--write-plan", str(src.with_name(f"{job['name']}.plan.yaml"))]
        if transpose and transpose != "auto":
            cmd += ["--transpose", str(transpose)]
        rc, log = self.run_cmd(cmd)
        job["log"] = log
        if rc != 0 or not fororgan.is_file():
            job.update(state="failed", log=log or f"transcriber exited {rc}")
            return
        cmd = [self.cfg.python, str(arranger / "organ_arranger.py"), str(fororgan), "--organ", str(self.cfg.organ),
               "-o", str(out), "--report", str(out.with_suffix(".txt"))]
        rc, log2 = self.run_cmd(cmd)
        job["log"] = log + "\n" + log2
        if rc != 0 or not out.is_file():
            job.update(state="failed")
            return
        job.update(state="done", output=out.relative_to(self.cfg.library).as_posix())
        self._counts = None

    def upload(self, filename: str, data: bytes, folder: str = "uploads") -> dict:
        """An already arranged file into a library folder, named STEM.organ.mid.
        Refused if it is not a MIDI file the player could read."""
        import mido
        stem = grinder.Song(Path(filename)).name
        safe = "".join(c if c.isalnum() or c in "-_ ()" else "-" for c in stem).strip("- ") or "tune"
        folder = folder.strip().strip("/") or "uploads"
        target_dir = self.library.resolve(folder)
        target_dir.mkdir(parents=True, exist_ok=True)
        out = target_dir / f"{safe}.organ.mid"
        out.write_bytes(data)
        try:
            length = round(mido.MidiFile(str(out)).length, 1)
        except Exception as e:
            out.unlink(missing_ok=True)
            raise ValueError(f"{filename}: not a MIDI file the player can read ({e})")
        self._counts = None
        return {"path": out.relative_to(self.cfg.library).as_posix(), "name": safe, "folder": folder, "length_s": length}

    # -- service ----------------------------------------------------------------

    def busy(self) -> bool:
        """Is the wire spoken for: a song on it, one about to be, or one waiting for wind."""
        return (self.status.get("state") in ("playing", "paused", "pause", "skipped", "warming up")
                or (bool(self.to_come()) and not self.held) or bool(self.pending))

    def apply_boards(self, values: dict, command: str | None = None, board: int | None = None) -> dict:
        """Send solenoid parameters and/or a command to the driver boards over
        the MIDI line, as organ_config does. Only while the player is idle: the
        line is shared. Remembers what was sent, since the boards cannot answer."""
        with self.lock:
            if self.busy():
                raise RuntimeError("the player is busy: stop it before retuning the boards")
            self.ensure_power("the boards")
        req = organ_config.Request(board=board)
        sent: dict = {}
        for name, (lo, hi) in BOARD_FIELDS.items():
            if values.get(name) is None or values.get(name) == "":
                continue
            v = int(values[name])
            if not lo <= v <= hi:
                raise ValueError(f"{name} must be {lo}-{hi}")
            setattr(req, name, v)
            sent[name] = v
        if command is not None:
            if command not in BOARD_COMMANDS:
                raise ValueError(f"unknown command {command!r} (save, reload, factory)")
            req.command = BOARD_COMMANDS[command]
        if not sent and command is None:
            raise ValueError("nothing to send")
        messages, warnings = organ_config.build_messages(req)
        port = self._open_port()
        try:
            organ_config.send_serial(messages, port)
        finally:
            close = getattr(port, "close", None)
            if close:
                close()
        with self.lock:
            boards = dict(self.settings.get("boards") or {})
            if command == "factory":
                boards = {}
            boards.update(sent)
            boards["sent_at"] = time.time()
            if command == "save":
                boards["saved_at"] = boards["sent_at"]
            self.settings["boards"] = boards
            self._save_settings()
        return {"sent": [organ_config.describe(m) for m in messages], "warnings": warnings, "boards": boards}

    # -- the screen: a real backlight when the Pi has one ----------------------------

    def backlight_dir(self) -> Path | None:
        try:
            for d in sorted(self.cfg.backlight.iterdir()):
                if (d / "brightness").is_file() and (d / "max_brightness").is_file():
                    return d
        except OSError:
            pass
        return None

    def screen(self, brightness: int | None = None, power: str | None = None) -> dict:
        """Set the backlight's brightness (0-100 %) and/or power ('on'/'off').
        Without a backlight, reports so; the page then dims itself."""
        d = self.backlight_dir()
        if d is None:
            return {"backlight": False}
        try:
            maximum = int((d / "max_brightness").read_text().strip() or 255)
            if brightness is not None:
                b = max(0, min(100, int(brightness)))
                (d / "brightness").write_text(str(round(maximum * b / 100)))
            if power is not None:
                if power not in ("on", "off"):
                    raise ValueError("power must be on or off")
                (d / "bl_power").write_text("0" if power == "on" else "1")
            current = int((d / "brightness").read_text().strip() or 0)
            powered = (d / "bl_power").read_text().strip() == "0" if (d / "bl_power").is_file() else True
            return {"backlight": True, "device": d.name, "brightness": round(100 * current / maximum), "power": "on" if powered else "off"}
        except OSError as e:
            raise RuntimeError(f"cannot drive the backlight at {d}: {e}")

    def shutdown(self, run=None) -> dict:
        """Power the organ down in order: pump off, 12 V off, then the Pi
        itself, so the panel switch can be turned off without cutting a
        running system. Needs `systemctl poweroff` allowed without a password
        for the service's user (see the README)."""
        self.stop()
        self.pump_set(False)
        self.power_set(False)
        if self.cfg.dry_run:
            return {"shutdown": "dry run: the organ is off, the Pi stays up"}
        run = run or subprocess.run
        r = run(["sudo", "-n", "systemctl", "poweroff"], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError("the Pi refused to power off: " + (r.stderr or r.stdout or "").strip()
                               + " -- allow it with a sudoers line, see the README")
        return {"shutdown": "powering off"}

    def reset_boards(self) -> dict:
        self.ensure_power("a reset")
        if self.cfg.dry_run:
            return {"reset": "dry run", "pins": self.cfg.reset_pins}
        try:
            sys.path.insert(0, str(TOOLS / "organ-config"))
            import organ_reset
        except ImportError as e:
            raise RuntimeError(f"organ_reset not available: {e}")
        for pin in self.cfg.reset_pins:
            organ_reset.pulse(pin, 0.1)
        return {"reset": "pulsed", "pins": self.cfg.reset_pins}

    def _open_port(self):
        if self.cfg.dry_run:
            return grinder.NullPort()
        import serial
        return serial.Serial(self.cfg.device, baudrate=grinder.MIDI_BAUD)

    def keys_act(self, what: str, body: dict) -> dict:
        with self.lock:
            if self.busy() and what != "off":
                raise RuntimeError("the player is busy: stop it before using the keys")
            if what != "off":
                self.ensure_power("the keys")
                self.idle_since = self.now()          # a hand on the keys is not idling
        return self.keys.act(what, body)

    def close(self) -> None:
        self.keys.close()
        self.player.close()
        self.pump_set(False)
        self.power_set(False)
        for dev in (self.pump, self.power):
            if dev is not None:
                try:
                    dev.close()
                except Exception:
                    pass


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def create_app(desk: Desk):
    import logging
    from flask import Flask, jsonify, request, send_from_directory

    app = Flask(__name__, static_folder=str(HERE / "static"), static_url_path="/static")
    logging.getLogger("werkzeug").setLevel(logging.WARNING)      # the page polls once a second; the journal need not hear it

    def body() -> dict:
        return request.get_json(silent=True) or {}

    def fail(e, code=400):
        return jsonify({"error": str(e)}), code

    @app.errorhandler(Exception)
    def on_error(e):
        code = 404 if isinstance(e, FileNotFoundError) else 409 if isinstance(e, RuntimeError) else 400
        if not isinstance(e, (ValueError, FileNotFoundError, RuntimeError, KeyError, TypeError)):
            code = 500
        return fail(e, code)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.after_request
    def revalidate(resp):
        """Page files may be cached but must be asked about on every load, so a reload
        after a git pull always gets the new stylesheet and script."""
        if request.path == "/" or request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.get("/api/state")
    def state():
        return jsonify(desk.snapshot())

    @app.get("/api/library")
    def library():
        return jsonify({"root": str(desk.cfg.library), "tunes": desk.library.tunes(), "folders": desk.library.folders()})

    @app.post("/api/queue/add")
    def queue_add():
        b = body()
        paths = b.get("paths") or ([b["path"]] if b.get("path") else [])
        if not paths:
            return fail("nothing to add")
        return jsonify({"added": desk.add(paths, bool(b.get("shuffle")), bool(b.get("play_now")))})

    @app.post("/api/queue/remove")
    def queue_remove():
        desk.remove(int(body()["id"]))
        return jsonify(desk.snapshot())

    @app.post("/api/queue/move")
    def queue_move():
        b = body()
        desk.move(int(b["id"]), int(b["to"]))
        return jsonify(desk.snapshot())

    @app.post("/api/queue/clear")
    def queue_clear():
        desk.clear()
        return jsonify(desk.snapshot())

    @app.post("/api/queue/shuffle")
    def queue_shuffle():
        desk.shuffle()
        return jsonify(desk.snapshot())

    @app.post("/api/queue/clear-played")
    def queue_clear_played():
        desk.clear_played()
        return jsonify(desk.snapshot())

    @app.post("/api/queue/jump")
    def queue_jump():
        desk.jump(int(body()["id"]))
        return jsonify(desk.snapshot())

    @app.post("/api/queue/save")
    def queue_save():
        name = body().get("name", "")
        desk.write_playlist(name, [{"path": e["path"], "tempo": e.get("tempo"), "gap": e.get("gap")}
                                   for e in desk.queue + desk.pending])          # the whole programme, played or not
        return jsonify({"saved": name, "playlists": desk.playlists()})

    @app.post("/api/player/skip")
    def player_skip():
        return jsonify({"skipped": desk.skip()})

    @app.post("/api/player/stop")
    def player_stop():
        desk.stop()
        return jsonify(desk.snapshot())

    @app.post("/api/player/play")
    def player_play():
        desk.play()
        return jsonify(desk.snapshot())

    @app.post("/api/player/pause")
    def player_pause():
        return jsonify({"paused": desk.pause(), "state": desk.status.get("state")})

    @app.post("/api/settings")
    def settings():
        return jsonify(desk.set_settings(body()))

    @app.get("/api/playlists")
    def playlists():
        return jsonify({"playlists": desk.playlists()})

    @app.get("/api/playlists/<name>")
    def playlist(name):
        return jsonify({"name": name, "entries": desk.read_playlist(name)})

    @app.put("/api/playlists/<name>")
    def playlist_put(name):
        desk.write_playlist(name, body().get("entries") or [])
        return jsonify({"saved": name, "playlists": desk.playlists()})

    @app.delete("/api/playlists/<name>")
    def playlist_delete(name):
        desk.delete_playlist(name)
        return jsonify({"deleted": name, "playlists": desk.playlists()})

    @app.post("/api/playlists/<name>/queue")
    def playlist_queue(name):
        b = body()
        added = desk.queue_playlist(name, bool(b.get("shuffle")), bool(b.get("replace")))
        return jsonify({"added": added})

    @app.post("/api/upload")
    def upload():
        files = request.files.getlist("file")
        if not files or not any(f.filename for f in files):
            return fail("no file")
        folder = request.form.get("folder") or "uploads"
        done = [desk.upload(f.filename, f.read(), folder) for f in files if f.filename]
        return jsonify({"uploaded": done})

    @app.get("/api/plans")
    def plans():
        return jsonify({"plans": desk.plans()})

    @app.post("/api/arrange")
    def arrange():
        f = request.files.get("file")
        if f is None or not f.filename:
            return fail("no file")
        plan = request.form.get("plan") or None
        transpose = request.form.get("transpose") or None
        job_id = desk.start_arrange(f.filename, f.read(), plan, transpose)
        return jsonify({"job": desk.jobs[job_id]})

    @app.get("/api/jobs/<job_id>")
    def job(job_id):
        if job_id not in desk.jobs:
            raise FileNotFoundError(f"no job {job_id}")
        return jsonify({"job": desk.jobs[job_id]})

    @app.get("/api/jobs")
    def jobs():
        return jsonify({"jobs": sorted(desk.jobs.values(), key=lambda j: -j["started"])[:20]})

    @app.get("/api/keys/layout")
    def keys_layout():
        return jsonify(desk.keys.layout())

    @app.post("/api/keys/<what>")
    def keys_act(what):
        return jsonify(desk.keys_act(what, body()))

    @app.post("/api/boards")
    def boards():
        b = body()
        board = b.get("board")
        return jsonify(desk.apply_boards(b, b.get("command"), int(board) if board not in (None, "") else None))

    @app.post("/api/screen")
    def screen():
        b = body()
        return jsonify(desk.screen(b.get("brightness"), b.get("power")))

    @app.post("/api/service/reset")
    def service_reset():
        return jsonify(desk.reset_boards())

    @app.post("/api/service/pump")
    def service_pump():
        if desk.pump is None:
            return fail("no pump relay configured (--pump GPIO)", 409)
        on = bool(body().get("on"))
        if on:
            desk.power_set(True)                 # a pump wants a supply to live in
        desk.pump_set(on)
        desk.idle_since = desk.now() if on else desk.idle_since
        return jsonify(desk.snapshot()["pump"])

    @app.post("/api/service/shutdown")
    def service_shutdown():
        return jsonify(desk.shutdown())

    @app.post("/api/service/power")
    def service_power():
        if desk.power is None:
            return fail("no power relay configured (--power GPIO)", 409)
        desk.power_set(bool(body().get("on")))
        desk.idle_since = desk.now()
        return jsonify(desk.snapshot()["power"])

    return app


class PowerSwitch:
    """A toggle switch between a GPIO and ground as the organ's on-off switch.
    Closed is on. When it opens, the desk shuts the organ down and the Pi
    powers off; a halted Pi wakes when GPIO 3 is pulled low, so on that pin
    the same switch turns it back on with no software at all."""

    def __init__(self, desk: Desk, button, out=None) -> None:
        self.desk = desk
        self.button = button
        self.out = out or sys.stderr
        self.fired = False
        button.when_released = self.off
        button.when_pressed = lambda: print("power switch closed", file=self.out, flush=True)
        print(f"power switch watched: contact {'closed' if button.is_pressed else 'open'} at start",
              file=self.out, flush=True)
        if not button.is_pressed:               # already in the off position when we come up
            self.off()

    def off(self) -> None:
        if self.fired:
            return
        if not self.desk.settings.get("power_switch", True):
            print("power switch opened, ignored: disabled in Settings", file=self.out, flush=True)
            return
        self.fired = True
        print("power switch off: shutting down", file=self.out, flush=True)
        threading.Thread(target=self._shutdown, name="power-switch", daemon=True).start()

    def _shutdown(self) -> None:
        try:
            self.desk.shutdown()
        except Exception as e:
            print(f"power switch: {e}", file=self.out, flush=True)
            self.fired = False                  # let the switch be tried again


def housekeeping(desk: Desk, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            desk.housekeep()
        except Exception as e:                      # keep the loop alive; the next tick may do better
            print(f"housekeeping: {e}", file=sys.stderr, flush=True)
        stop.wait(HOUSEKEEP_S)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="organ_web", description="The organ's front desk: a web page served by the Pi.")
    p.add_argument("--library", required=True, help="folder of arranged tunes (subfolders are sections of the library)")
    p.add_argument("--organ", required=True, help="organ.yaml")
    p.add_argument("--state", default=str(Path.home() / ".local" / "share" / "organ-web"),
                   help="where the queue, status, settings and playlists live")
    p.add_argument("--plans", default=str(TOOLS / "organ-arranger" / "tunes"), help="folder of *.plan.yaml for uploads")
    p.add_argument("--device", default=grinder.DEFAULT_DEVICE, help="serial device driving the MIDI line")
    p.add_argument("--python", default=sys.executable, help="interpreter for the player and the arranger (default: this one)")
    p.add_argument("--pump", type=int, metavar="GPIO", help="BCM GPIO of the bellows pump relay")
    p.add_argument("--pump-active-low", action="store_true")
    p.add_argument("--power", type=int, metavar="GPIO", help="BCM GPIO switching the organ's 12 V supply (an ATX PS_ON through an opto)")
    p.add_argument("--power-active-low", action="store_true")
    p.add_argument("--power-switch", type=int, metavar="GPIO",
                   help="a toggle switch to ground on this GPIO is the organ's on-off switch; on GPIO 3 it also wakes a halted Pi")
    p.add_argument("--reset-pins", default="17", help="BCM GPIOs of the boards' reset line(s), comma-separated (default 17)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--dry-run", action="store_true", help="no serial port, no GPIO: the player plays to nowhere")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    try:
        import flask  # noqa: F401
    except ImportError:
        print("error: needs flask: pip install flask", file=sys.stderr)
        return 2
    cfg = Config(library=Path(a.library).expanduser(), organ=Path(a.organ).expanduser(), state=Path(a.state).expanduser(),
                 plans=Path(a.plans).expanduser(), device=a.device, python=a.python, pump_pin=a.pump,
                 pump_active_low=a.pump_active_low, power_pin=a.power, power_active_low=a.power_active_low,
                 dry_run=a.dry_run, host=a.host, port=a.port,
                 reset_pins=[int(x) for x in a.reset_pins.split(",") if x.strip()])
    if not cfg.organ.is_file():
        print(f"error: no organ definition at {cfg.organ}", file=sys.stderr)
        return 2
    cfg.library.mkdir(parents=True, exist_ok=True)
    def relay(pin, active_low, flag):
        if pin is None:
            return None
        if cfg.dry_run:
            return FakePump()
        try:
            return grinder.Pump(pin, active_low)
        except ImportError:
            print(f"error: {flag} needs gpiozero: pip install gpiozero lgpio", file=sys.stderr)
            raise SystemExit(2)

    pump = relay(cfg.pump_pin, cfg.pump_active_low, "--pump")
    power = relay(cfg.power_pin, cfg.power_active_low, "--power")
    desk = Desk(cfg, pump=pump, power=power)
    switch = None
    if a.power_switch is not None and not cfg.dry_run:
        try:
            from gpiozero import Button
            switch = PowerSwitch(desk, Button(a.power_switch, pull_up=True, bounce_time=0.2))
            desk.power_switch = switch
        except ImportError:
            print("error: --power-switch needs gpiozero: pip install gpiozero lgpio", file=sys.stderr)
            return 2
    # systemd stops us with SIGTERM: leave the way the Shut down button does, pump and 12 V off in order
    def on_term(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, on_term)
    app = create_app(desk)
    stop = threading.Event()
    threading.Thread(target=housekeeping, args=(desk, stop), name="housekeeping", daemon=True).start()
    print(f"organ_web {__version__}: library {cfg.library}, state {cfg.state}, http://{cfg.host}:{cfg.port}/"
          + ("  (dry run)" if cfg.dry_run else ""), flush=True)
    try:
        app.run(host=cfg.host, port=cfg.port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        desk.close()
        if switch is not None:
            try:
                switch.button.close()
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
