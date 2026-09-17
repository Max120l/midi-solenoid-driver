# player

The playback engine: the Raspberry Pi's UART, at 31250 baud into an
optocoupler, is a MIDI output, and `grinder.py` writes arranged songs down
it, one after another, paced by their tempo maps.

```bash
pip install mido pyserial
python grinder.py song.organ.mid
python grinder.py tunes/ --shuffle --repeat --gap 5
```

It plays files that have been through [`organ_arranger`](../organ-arranger/):
single track, channel 1, every note a driver-board slot. Around every song
it sends All Notes Off, so a note sounding when a song is skipped does not
hang until the boards' stuck-note watchdog reaches it.

This is still deliberately an engine, not the app: no state but the queue,
nothing to crash but this process. A screen, a phone across the room and
uploads that run the arranger belong in a layer that starts this one, so
the music keeps playing if the UI does not.

## The queue

Every argument is a song, a folder or a playlist, and the queue is their
contents in order:

| Argument | Contributes |
|---|---|
| `song.organ.mid` | that file |
| `tunes/` | every `*.organ.mid` under it, subfolders included, in name order, folder by folder (`--pattern` changes the match) |
| `evening.m3u`, `set.txt` | its lines: files or folders, relative to the playlist; `#` comments |

So `tunes/waltzes --shuffle` is a random order within one folder, and
`tunes --shuffle` a random order across all of them. `--repeat` starts
again when the queue ends, reshuffled each time round. `--list` prints
the queue in play order and plays nothing, which is how to see what a
shuffle would do.

A playlist line can carry its own settings after a pipe:

```
# Saturday afternoon
skaters-waltz.organ.mid | tempo=90%
colonel-bogey.organ.mid | tempo=1.05 gap=6
marches/
```

## Settings

| Option | Does |
|---|---|
| `--tempo 90%`, `--tempo 1.1` | speed for every song, 50 %–150 %; a playlist line overrides it for that song |
| `--gap 5` | seconds of silence between songs (default 3); a playlist line overrides it for the pause after that song |
| `--shuffle`, `--repeat` | as above |
| `--start 62` | begin the first song this far in, in seconds at the chosen tempo |
| `--organ organ.yaml` | read the organ's minimum note and gap, and its registers, from the definition |
| `--pattern '*.mid'` | what a folder contributes (default `*.organ.mid`, so source files in the same folder are not played) |
| `--status FILE` | rewrite a one-line JSON file about once a second with what is playing (below) |
| `--pump GPIO`, `--pump-active-low` | drive the bellows pump relay from a Pi GPIO: on before the music, off after (below) |
| `--warm-up 8` | wait this many seconds for wind before the first note, with or without a pump pin |
| `--device /dev/ttyUSB0` | the serial device (default `/dev/serial0`) |
| `--dry-run` | no serial port: play in real time to nowhere |

**Tempo and the solenoids.** The arranger already holds every note on for
the organ's minimum (50 ms) and off for the minimum gap (30 ms) before the
same pipe sounds again. Scaling time would break that, so the player
enforces the same limits after scaling: a note is never shortened below the
minimum, and never left on into the next strike. Where a pipe is struck
faster than minimum note plus gap allows, a snare roll at 150 % say, the
note gives way first and then the gap. Slower tempos have no such problem.

**Rehearsing a passage.** `--start` skips to a point in the first song. The
registers are set by pulses at the top of the file, which a plain skip would
lose, so with `--organ` the player looks at what was pulsed before the start
point and re-pulses the last state of every register pair before playing.
Notes already sounding at the start point are not restarted.

## Keys while playing

| Key | Does |
|---|---|
| `Ctrl+C` during a song | skip it |
| `Ctrl+C` again within two seconds | quit |
| `Ctrl+C` during the pause or the warm-up | quit |

A skip does not cut the organ dead. The notes sounding at that moment end
where they are written, as long as that is within half a second; anything
held longer is cut then, but never before its minimum length. Then All Notes
Off, which is the last thing on the wire whichever way the player leaves.

## The status file

With `--status FILE` the player rewrites one line of JSON about once a
second, replacing the file atomically so a reader never sees half of it:

```json
{"time": 1789756123.4, "state": "playing", "song": "skaters-waltz", "path": "tunes/skaters-waltz.organ.mid",
 "index": 3, "total": 12, "round": 1, "length_s": 172.1, "tempo": 0.9, "start_s": 0, "position_s": 41.0}
```

`state` runs through `warming up`, `playing`, `played`, `pause`, `skipped`,
`finished` and `stopped`; `seconds` accompanies a warm-up or a pause. It is
what a phone page or a screen reads, and a log if you keep the lines. Put it
somewhere in RAM on the Pi, `/run/user/1000/grinder.json` or `/dev/shm`, so
the SD card is not written every second.

## The bellows pump

`--pump 24` drives a relay module from BCM GPIO 24 (physical pin 18): the
relay pulls in before the warm-up, stays in across the pauses between songs,
and drops out when the queue ends or the player quits, however it quits.
Most cheap relay modules pull in on a *low* input; add `--pump-active-low`
for those. Only the relay's coil side touches the Pi; its contacts switch the
pump's own supply, so the isolation between the Pi and the organ is
untouched. The Pi's GPIOs default to pull-down at boot, so a booting Pi
leaves an active-high relay off; with an active-low module put the pump on
the relay's normally-open contact for the same reason.

`--warm-up 8` waits eight seconds after the relay pulls in before the first
note, for the reservoir to fill. It works without `--pump` too, for a pump
switched by hand. Neither is needed until the relay exists; the player behaves
exactly as before when they are not given.

## The Pi's serial port

`/dev/serial0` transmits on GPIO14, **physical pin 8**. On a Pi 4 with
Bluetooth at its default it is the mini-UART, which works at 31250 because
`enable_uart=1` pins the core clock its baud rate is derived from. For an
appliance with no use for onboard Bluetooth, give it the real PL011 on the
same pin instead — exact baud, bigger FIFO — with, in
`/boot/firmware/config.txt`:

```
enable_uart=1
dtoverlay=disable-bt
```

then `sudo systemctl disable hciuart` and a reboot. Nothing here changes.

The same device works for [`organ-config`](../organ-config/):

```bash
python ../organ-config/organ_config.py --serial /dev/serial0 --peak 60 --hold 25 --save
```

## Tests

```bash
python -m pytest tests
```

The tests drive the engine with a fake port and a fake clock, so they need
neither a serial device nor real time.
