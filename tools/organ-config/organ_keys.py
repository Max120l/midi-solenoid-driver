#!/usr/bin/env python3
"""
organ_keys -- play the organ's solenoids from the keyboard, one block at a time.

A screen for commissioning: the 64 solenoids drawn in blocks of up to two
rows of eight, numbered as on the layout sheet, with the key that fires each
one and, if the organ definition is given, what it is (C3 Acc, Snare, Trmb
on). Two views of the same solenoids: by **board**, four blocks of sixteen
as wired; and by **section**, given the organ definition -- Melody,
Accompainment, Base, TenorCM, TrebCM, Drums, Registers -- each block in
pitch order, so the keys 1-8 q-i walk up the rank and `a` plays its scale.
Tap a key and the solenoid pulses; toggle hold mode and a key opens a valve
until you press it again -- for tuning a pipe, or for finding the tube that
goes nowhere.

    organ_keys.py --serial /dev/serial0 --organ ../organ-arranger/instrument/organ.yaml
    organ_keys.py --port "USB MIDI"
    organ_keys.py --organ instrument/organ.yaml --view section
    organ_keys.py --dry-run                          # no hardware, screen only

Keys
    1 2 3 4 5 6 7 8      the first eight solenoids of the selected block
    q w e r t y u i      the next eight
    z x c v b n m  Tab   select a block: board 1-4, or section 1-7
    s                    switch view: boards / sections (sections need --organ)
    h                    hold mode on/off (keys toggle instead of pulse)
    a                    play the selected block in a row (a section: its scale)
    d                    roll the snare, alternating its two beaters (d again stops)
    D                    roll the last tapped solenoid alone
    up / down arrows     roll faster / slower
    -  =                 pulse length down / up
    space  or  0         everything off
    Q  or  Esc           quit (everything off first)

The wire is the same as the arranger's output: solenoid N is MIDI note
solenoid_1_note + N - 1 (0 for this instrument), on the output channel.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field

import mido

from organ_config import MIDI_BAUD, match_port

__version__ = "0.2.0"

BOARDS = 4
PER_BOARD = 16
ROW_KEYS = ("12345678", "qwertyui")          # two rows of eight, as the cells are drawn
GROUP_KEYS = "zxcvbnm"           # select a block: boards 1-4, or sections 1-7
BOARD_KEYS = GROUP_KEYS[:BOARDS]
SECTION_ORDER = ("melody", "accompainment", "accompaniment", "base", "tenorcm", "trebcm", "drums", "registers")
DEFAULT_PULSE_MS = 150
PULSE_STEP_MS = 50
PULSE_MIN_MS, PULSE_MAX_MS = 50, 2000
SCALE_STEP_S = 0.35
DEFAULT_ROLL_MS = 120            # interval between roll hits; arrows change it
ROLL_STEP_MS = 5
ROLL_MIN_MS, ROLL_MAX_MS = 20, 500
ROLL_HIT_MIN_MS = 10

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n: int) -> str:
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


# ----------------------------------------------------------------------------
# What each solenoid is, from organ.yaml (optional)
# ----------------------------------------------------------------------------

TRACK_ABBREV = {"main": "", "melody": "", "accompainment": "", "accompaniment": "",
                "tenorcm": "Tn", "trebcm": "Tr", "drums": "", "registers": ""}
SECTION_ABBREV = {"base": "Bas", "accompainment": "Acc", "accompaniment": "Acc", "melody": "Mel"}


def labels_from_organ(raw: dict) -> tuple[dict[int, str], int]:
    """solenoid -> short label (<= 8 chars), and the note that fires solenoid 1."""
    labels: dict[int, str] = {}
    for tname, tdef in (raw.get("tracks") or {}).items():
        tdef = tdef or {}
        kind = str(tdef.get("kind", "pitched")).lower()
        tlabels = {int(k): str(v) for k, v in (tdef.get("labels") or {}).items()}
        section_of: dict[int, str] = {}
        for sec, notes in (tdef.get("sections") or {}).items():
            for n in notes or []:
                section_of[int(n)] = SECTION_ABBREV.get(str(sec).lower(), str(sec)[:3])
        abbrev = TRACK_ABBREV.get(tname.lower(), tname[:2])
        for k, v in (tdef.get("notes") or {}).items():
            note = int(k)
            sols = v if isinstance(v, (list, tuple)) else [v]
            if kind == "pulse":
                text = tlabels.get(note) or f"{tname[:4]} {note}"
                text = shorten_pulse_label(text)
            else:
                parts = [p for p in (abbrev, note_name(note), section_of.get(note, "")) if p]
                text = " ".join(parts)
            for s in sols:
                labels[int(s)] = text[:8]
    return labels, int(raw.get("solenoid_1_note", 0))


def groups_from_organ(raw: dict) -> tuple[list[tuple[str, list[int]]], dict[int, str]]:
    """The organ by section: ([(name, [solenoids in pitch order]), ...], solenoid -> cell label).

    A track with `sections` gives one group per section, any other track one
    group, ordered as the organ is thought of -- Melody, Accompainment,
    Base, TenorCM, TrebCM, Drums, Registers -- then anything else in file
    order. Cell labels are the written note and the board ('C5 b3'), or the
    pulse label ('Snare', 'Trmb B+'): the section is the block's heading.
    """
    found: dict[str, list[tuple[int, int]]] = {}          # name -> [(written note, solenoid)]
    labels: dict[int, str] = {}
    for tname, tdef in (raw.get("tracks") or {}).items():
        tdef = tdef or {}
        kind = str(tdef.get("kind", "pitched")).lower()
        tlabels = {int(k): str(v) for k, v in (tdef.get("labels") or {}).items()}
        section_of: dict[int, str] = {}
        for sec, notes in (tdef.get("sections") or {}).items():
            for n in notes or []:
                section_of[int(n)] = str(sec)
        for k, v in (tdef.get("notes") or {}).items():
            note = int(k)
            sols = v if isinstance(v, (list, tuple)) else [v]
            name = section_of.get(note, tname)
            for s in sols:
                s = int(s)
                found.setdefault(name, []).append((note, s))
                if kind == "pulse":
                    labels[s] = shorten_pulse_label(tlabels.get(note) or f"{tname[:4]} {note}")
                else:
                    labels[s] = f"{note_name(note)} b{(s - 1) // PER_BOARD + 1}"
    order = list(found)

    def rank(name: str) -> tuple[int, int]:
        key = name.lower()
        return (SECTION_ORDER.index(key) if key in SECTION_ORDER else len(SECTION_ORDER), order.index(name))

    groups = []
    for name in sorted(found, key=rank):
        seen: list[int] = []
        for _, s in sorted(found[name]):
            if s not in seen:
                seen.append(s)
        groups.append((name, seen))
    return groups, labels


def shorten_pulse_label(text: str) -> str:
    """'Violin Accompainment on' -> 'Viol A+', 'Trombone Base off' -> 'Trmb B-', 'Snare' -> 'Snare'."""
    words = text.split()
    state = ""
    if words and words[-1].lower() in ("on", "off"):
        state = "+" if words.pop().lower() == "on" else "-"
    if not words:
        return state
    head = words[0]
    head = head[:4] if len(words) > 1 or state else head[:8]
    tail = words[1][0].upper() if len(words) > 1 else ""
    return (head + (" " + tail if tail else "") + state).strip()


# ----------------------------------------------------------------------------
# The model: keys, notes, pending note-offs. No screen in here, so it is testable.
# ----------------------------------------------------------------------------

@dataclass
class Console:
    send: object                                  # callable(mido.Message)
    channel: int = 0                              # zero-based
    solenoid_1_note: int = 0
    pulse_ms: int = DEFAULT_PULSE_MS
    labels: dict[int, str] = field(default_factory=dict)           # board view: 'C3 Acc', 'Tn E4'
    sections: list[tuple[str, list[int]]] = field(default_factory=list)   # from groups_from_organ
    section_labels: dict[int, str] = field(default_factory=dict)   # section view: 'C3 b3'
    view: str = "board"                           # "board" | "section"
    group: int = 0                                # selected block in the current view, zero-based
    hold: bool = False
    sounding: set[int] = field(default_factory=set)        # solenoids currently on
    pending_off: dict[int, float] = field(default_factory=dict)   # solenoid -> time to switch off
    pending_scale: list[tuple[float, int]] = field(default_factory=list)   # (when, solenoid) for 'a'
    # A roll: hits alternating over roll_targets every roll_interval_ms. Two
    # entries are the snare's two beaters (how organ books roll faster than one
    # beater can re-articulate); one entry measures a single solenoid's limit.
    roll_targets: list[int] = field(default_factory=list)
    roll_interval_ms: int = DEFAULT_ROLL_MS
    roll_next: float = 0.0
    roll_index: int = 0
    last_tap: int | None = None
    status: str = ""

    @property
    def board(self) -> int:
        return self.group

    @board.setter
    def board(self, value: int) -> None:
        self.group = value

    def snare_pair(self) -> list[int]:
        return sorted(s for s, label in self.labels.items() if "snare" in label.lower())[:2]

    def note_of(self, solenoid: int) -> int:
        return self.solenoid_1_note + solenoid - 1

    def groups(self) -> list[tuple[str, list[int]]]:
        """The blocks of the current view: (name, solenoids in key order)."""
        if self.view == "section" and self.sections:
            return self.sections
        return [(f"board {b + 1}", list(range(b * PER_BOARD + 1, (b + 1) * PER_BOARD + 1))) for b in range(BOARDS)]

    def current(self) -> tuple[str, list[int]]:
        return self.groups()[self.group]

    def label(self, solenoid: int) -> str:
        if self.view == "section" and self.sections:
            return self.section_labels.get(solenoid, "")
        return self.labels.get(solenoid, "")

    def solenoid_for_key(self, key: str) -> int | None:
        for row, keys in enumerate(ROW_KEYS):
            if key in keys:
                i = row * 8 + keys.index(key)
                sols = self.current()[1]
                return sols[i] if i < len(sols) else None
        return None

    def key_for_solenoid(self, solenoid: int) -> str:
        for _, sols in self.groups():
            if solenoid in sols:
                i = sols.index(solenoid)
                return ROW_KEYS[i // 8][i % 8]
        return ""

    def on(self, solenoid: int, now: float) -> None:
        if solenoid not in self.sounding:
            self.send(mido.Message("note_on", channel=self.channel, note=self.note_of(solenoid), velocity=100))
            self.sounding.add(solenoid)
        self.pending_off.pop(solenoid, None)

    def off(self, solenoid: int) -> None:
        if solenoid in self.sounding:
            self.send(mido.Message("note_off", channel=self.channel, note=self.note_of(solenoid), velocity=0))
            self.sounding.discard(solenoid)
        self.pending_off.pop(solenoid, None)

    def pulse(self, solenoid: int, now: float, length_ms: int | None = None) -> None:
        self.on(solenoid, now)
        self.pending_off[solenoid] = now + (self.pulse_ms if length_ms is None else length_ms) / 1000

    def all_off(self) -> None:
        self.roll_targets = []
        for s in sorted(self.sounding):
            self.off(s)
        self.pending_off.clear()
        self.send(mido.Message("control_change", channel=self.channel, control=123, value=0))

    def roll_hit_ms(self) -> int:
        """Each hit is on for a bit over half the interval, never longer than a tap."""
        return max(ROLL_HIT_MIN_MS, min(self.pulse_ms, int(self.roll_interval_ms * 0.6)))

    def start_roll(self, targets: list[int], now: float) -> None:
        self.roll_targets = list(targets)
        self.roll_index = 0
        self.roll_next = now

    def tick(self, now: float) -> None:
        """Switch off whatever pulse has run its course; keep a roll going."""
        while self.roll_targets and now >= self.roll_next:
            s = self.roll_targets[self.roll_index % len(self.roll_targets)]
            self.roll_index += 1
            self.pulse(s, self.roll_next, self.roll_hit_ms())
            self.roll_next += self.roll_interval_ms / 1000
        for s, due in list(self.pending_off.items()):
            if now >= due:
                self.off(s)

    def handle_key(self, key: str, now: float) -> bool:
        """Apply one keypress. Returns False when the user wants to quit."""
        if key in ("Q", "\x1b"):
            self.all_off()
            return False
        if key in (" ", "0"):
            self.all_off()
            self.status = "all off"
            return True
        if self.handle_roll_key(key, now):
            return True
        if key in GROUP_KEYS:
            i = GROUP_KEYS.index(key)
            if i < len(self.groups()):
                self.group = i
                self.status = self.current()[0]
            else:
                self.status = f"only {len(self.groups())} blocks in this view"
            return True
        if key == "\t":
            self.group = (self.group + 1) % len(self.groups())
            self.status = self.current()[0]
            return True
        if key == "s":
            if not self.sections:
                self.status = "no organ definition (need --organ) for the section view"
            else:
                self.view = "board" if self.view == "section" else "section"
                self.group = 0
                self.status = ("by section: blocks are ranks, keys in pitch order" if self.view == "section"
                               else "by board: blocks are boards, keys in solenoid order")
            return True
        if key == "h":
            self.hold = not self.hold
            if not self.hold:
                self.all_off()
            self.status = "hold: keys toggle valves" if self.hold else "pulse mode"
            return True
        if key == "-":
            self.pulse_ms = max(PULSE_MIN_MS, self.pulse_ms - PULSE_STEP_MS)
            self.status = f"pulse {self.pulse_ms} ms"
            return True
        if key in ("=", "+"):
            self.pulse_ms = min(PULSE_MAX_MS, self.pulse_ms + PULSE_STEP_MS)
            self.status = f"pulse {self.pulse_ms} ms"
            return True
        if key == "a":
            name, sols = self.current()
            self.pending_scale = [(now + i * SCALE_STEP_S, s) for i, s in enumerate(sols)]
            self.status = f"{name}: {span(sols)} in a row"
            return True
        solenoid = self.solenoid_for_key(key.lower())
        if solenoid is None:
            return True
        self.last_tap = solenoid
        if self.hold:
            if solenoid in self.sounding:
                self.off(solenoid)
                self.status = f"solenoid {solenoid} off"
            else:
                self.on(solenoid, now)
                self.status = f"solenoid {solenoid} ON (held)"
        else:
            self.pulse(solenoid, now)
            self.status = f"solenoid {solenoid}  {self.label(solenoid)}".rstrip()
        return True

    def handle_roll_key(self, key: str, now: float) -> bool:
        """r / R / UP / DOWN. Returns True if the key was one of those."""
        if key == "d":
            if self.roll_targets:
                self.all_off()
                self.status = "roll stopped"
            else:
                pair = self.snare_pair()
                if len(pair) < 2:
                    self.status = "no snare pair in the organ definition (need --organ); D rolls the last tapped solenoid"
                else:
                    self.start_roll(pair, now)
                    self.status = f"rolling snare, beaters {pair[0]} and {pair[1]}"
            return True
        if key == "D":
            if self.roll_targets:
                self.all_off()
                self.status = "roll stopped"
            elif self.last_tap is None:
                self.status = "tap a solenoid first, then D rolls it alone"
            else:
                self.start_roll([self.last_tap], now)
                self.status = f"rolling solenoid {self.last_tap} alone"
            return True
        if key in ("UP", "DOWN"):
            step = -ROLL_STEP_MS if key == "UP" else ROLL_STEP_MS
            self.roll_interval_ms = max(ROLL_MIN_MS, min(ROLL_MAX_MS, self.roll_interval_ms + step))
            self.status = f"roll interval {self.roll_interval_ms} ms"
            return True
        return False

    def tick_scale(self, now: float) -> None:
        while self.pending_scale and now >= self.pending_scale[0][0]:
            _, s = self.pending_scale.pop(0)
            self.pulse(s, now)


# ----------------------------------------------------------------------------
# Screen
# ----------------------------------------------------------------------------

CELL_W = 9


def span(sols: list[int]) -> str:
    """'solenoids 17-32' when contiguous, else '13 solenoids'."""
    if sols and sols == list(range(sols[0], sols[0] + len(sols))):
        return f"solenoids {sols[0]}-{sols[-1]}"
    return f"{len(sols)} solenoid{'s' if len(sols) != 1 else ''}"


def render(c: Console, height: int | None = None) -> list[tuple[str, str]]:
    """The screen as (text, style) lines; style is 'normal' | 'active' | 'on' | 'dim'.

    A cell is two lines: '36 q' (solenoid, key) and its label. Rows are
    styled per cell by the drawer; here each line is one string so tests
    can read it. Given the terminal height, a section view that does not
    fit folds the blocks that are not selected down to their heading.
    """
    groups = c.groups()
    by_section = c.view == "section" and bool(c.sections)
    mode = "HOLD" if c.hold else f"pulse {c.pulse_ms} ms"
    per_s = 1000 / c.roll_interval_ms
    roll = (f"ROLLING {'+'.join(str(s) for s in c.roll_targets)} " if c.roll_targets else "roll ")
    roll += f"{c.roll_interval_ms} ms/hit = {per_s:.1f}/s [d snare, D last tap, arrows]"
    view = "sections" if by_section else "boards"
    head = [
        (f" organ_keys {__version__}   {c.current()[0]} [{' '.join(GROUP_KEYS[:len(groups)])}, Tab]"
         f"   by {view} [s]   {mode} [h, - =]", "normal"),
        (" keys 1-8 and q-i fire the marked block   a: it in a row   space: all off   Q: quit", "dim"),
        (" " + roll, "active" if c.roll_targets else "dim"),
        ("", "normal"),
    ]

    def block(gi: int, folded: bool) -> list[tuple[str, str]]:
        name, sols = groups[gi]
        active = gi == c.group
        style = "active" if active else "dim"
        out = [(f" {name}   {span(sols)}" + ("   <-- keys" if active else ""), style)]
        if folded:
            return out
        for row in range(max(1, (len(sols) + 7) // 8)):
            top = ""
            bottom = ""
            for col in range(8):
                i = row * 8 + col
                if i >= len(sols):
                    break
                s = sols[i]
                key = ROW_KEYS[row][col] if active else " "
                mark = "*" if s in c.sounding else " "
                top += f"{mark}{s:2d} {key}".ljust(CELL_W)
                bottom += f" {c.label(s)[:7]}".ljust(CELL_W)
            out += [(top, style), (bottom, style)]
        if not by_section:
            out.append(("", "normal"))
        return out

    def body(fold_others: bool) -> list[tuple[str, str]]:
        lines = list(head)
        for gi in range(len(groups)):
            lines += block(gi, fold_others and gi != c.group)
        lines.append((f" {c.status}", "normal"))
        return lines

    lines = body(False)
    if height is not None and len(lines) > height - 1:
        lines = body(True)
    return lines


def run_screen(c: Console) -> None:
    import curses

    def main(stdscr):
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.timeout(20)
        has_color = curses.has_colors()
        if has_color:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(1, curses.COLOR_YELLOW, -1)
            curses.init_pair(2, curses.COLOR_GREEN, -1)
        styles = {
            "normal": curses.A_NORMAL,
            "active": curses.color_pair(1) | curses.A_BOLD if has_color else curses.A_BOLD,
            "dim": curses.A_DIM,
            "on": curses.color_pair(2) | curses.A_BOLD if has_color else curses.A_REVERSE,
        }
        running = True
        while running:
            now = time.monotonic()
            c.tick_scale(now)
            c.tick(now)
            stdscr.erase()
            h, w = stdscr.getmaxyx()
            for y, (text, style) in enumerate(render(c, h)):
                if y >= h - 1:
                    break
                try:
                    stdscr.addnstr(y, 0, text, w - 1, styles[style])
                    # light up sounding solenoids
                    if "*" in text and style != "normal":
                        for col in range(8):
                            x = col * CELL_W
                            if text[x:x + 1] == "*":
                                stdscr.addnstr(y, x, text[x:x + CELL_W], min(CELL_W, w - 1 - x), styles["on"])
                except curses.error:
                    pass
            stdscr.refresh()
            try:
                ch = stdscr.get_wch()
            except curses.error:
                continue
            if ch == curses.KEY_UP:
                ch = "UP"
            elif ch == curses.KEY_DOWN:
                ch = "DOWN"
            if isinstance(ch, str):
                running = c.handle_key(ch, time.monotonic())

    curses.wrapper(main)


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="organ_keys", description="Play the organ's solenoids from the keyboard.")
    out = p.add_mutually_exclusive_group()
    out.add_argument("--serial", metavar="DEVICE", help="UART device driving the MIDI line, e.g. /dev/serial0")
    out.add_argument("--port", metavar="NAME", help="MIDI output port (substring)")
    out.add_argument("--dry-run", action="store_true", help="no output; just the screen")
    p.add_argument("--organ", help="organ.yaml, for the labels, the sections and solenoid_1_note")
    p.add_argument("--view", choices=("board", "section"), default="board",
                   help="start by board (default) or by section (needs --organ); s switches on screen")
    p.add_argument("--solenoid-1-note", type=int, help="MIDI note that fires solenoid 1 (default: organ.yaml, else 0)")
    p.add_argument("--channel", type=int, default=1, help="MIDI channel 1-16 (default 1)")
    p.add_argument("--pulse-ms", type=int, default=DEFAULT_PULSE_MS, help=f"tap length (default {DEFAULT_PULSE_MS})")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    a = p.parse_args(argv)

    labels: dict[int, str] = {}
    sections: list[tuple[str, list[int]]] = []
    section_labels: dict[int, str] = {}
    first = 0
    if a.organ:
        import yaml
        try:
            with open(a.organ, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            labels, first = labels_from_organ(raw)
            sections, section_labels = groups_from_organ(raw)
        except (OSError, yaml.YAMLError, ValueError, TypeError) as e:
            print(f"error: cannot read {a.organ}: {e}", file=sys.stderr)
            return 2
    if a.solenoid_1_note is not None:
        first = a.solenoid_1_note
    if not 1 <= a.channel <= 16:
        p.error("--channel must be 1-16")
    if a.view == "section" and not sections:
        p.error("--view section needs --organ with tracks in it")

    def make_console(send) -> Console:
        return Console(send=send, channel=a.channel - 1, solenoid_1_note=first,
                       pulse_ms=max(PULSE_MIN_MS, min(PULSE_MAX_MS, a.pulse_ms)), labels=labels,
                       sections=sections, section_labels=section_labels, view=a.view)

    if a.dry_run or not (a.serial or a.port):
        if not a.dry_run:
            print("note: no --serial or --port given; running with no output (--dry-run)", file=sys.stderr)
            time.sleep(1.0)
        run_screen(make_console(lambda m: None))
        return 0

    if a.serial:
        try:
            import serial
        except ImportError:
            print("error: --serial needs pyserial: pip install pyserial", file=sys.stderr)
            return 2
        try:
            with serial.Serial(a.serial, baudrate=MIDI_BAUD, timeout=1) as port:
                run_screen(make_console(lambda m: port.write(bytes(m.bytes()))))
        except (serial.SerialException, OSError, ValueError) as e:
            print(f"error: cannot open {a.serial}: {e}", file=sys.stderr)
            return 2
        return 0

    try:
        port_name = match_port(mido.get_output_names(), a.port)
    except LookupError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    with mido.open_output(port_name) as port:
        run_screen(make_console(port.send))
    return 0


if __name__ == "__main__":
    sys.exit(main())
