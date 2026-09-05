# organ-config

Retune the driver boards over MIDI. No reflashing.

The firmware's tuning parameters — pull-in duty and duration, hold duty, the
stuck-note watchdog, and the power-up exercise — are adjustable while the
boards run, by control changes on a dedicated configuration channel, and can
be saved to each board's EEPROM so they survive a power cycle. This tool sends
those messages from the Pi (or anything with a MIDI output).

## Install

```bash
pip install mido python-rtmidi
```

## Use

```bash
python organ_config.py --list-ports
python organ_config.py --port "USB MIDI" --peak 60 --hold 25
python organ_config.py --port "USB MIDI" --peak 60 --hold 25 --save
```

`--port` is a case-insensitive substring; it can be omitted if there is only
one MIDI output. `--dry-run` prints what would be sent and sends nothing.

### From a Raspberry Pi UART instead of a MIDI interface

If the Pi drives the optocoupler straight from a UART pin, there is no MIDI
port for `mido` to find — the output is a serial device. Send to it directly:

```bash
pip install pyserial
python organ_config.py --serial /dev/ttyAMA1 --peak 60 --hold 25 --save
```

That writes exactly the bytes a MIDI interface would, at 31250 baud. Two
things to get right on the Pi side:

- **Use a PL011 UART, not the mini-UART.** On a Pi 4, GPIO14 (pin 8) is
  UART0's transmit and GPIO4 (pin 7) is UART3's, enabled with
  `dtoverlay=uart3` in `config.txt`; they appear as `/dev/ttyAMA*` and do
  31250 baud exactly. `/dev/ttyS0` is the mini-UART, whose baud rate follows
  the core clock, and it is not to be trusted at this rate.
- Whatever device your player already writes to is the one to use here.

| Option | Firmware setting | Range |
|---|---|---|
| `--peak PCT` | `peakDutyPercent` | 1–100 |
| `--hold PCT` | `holdDutyPercent` | 0–40 |
| `--peak-ms MS` | `peakDurationMs` | 1–127 |
| `--max-note S` | `maxNoteSeconds` | 0–127, 0 disables the watchdog |
| `--exercise N` | `exerciseCycles` | 0–10 |
| `--board BASE` | address one board by its base note | 0–112; default is every board |
| `--save` | write current settings to EEPROM | |
| `--reload` | reload settings from EEPROM | |
| `--factory` | compiled defaults, in RAM (add `--save` to keep) | |

Changes take effect **immediately, including on notes already sounding** —
which is what makes tuning by ear work. Hold a chord and step the hold duty
down until a valve drops out; play an attack and step the peak duty down until
one seats sluggishly; then back off and `--save`.

## Things worth knowing

**The boards cannot answer.** There is no read-back. What a board currently
believes is whatever it was last sent, or its EEPROM contents if it has been
power-cycled since. The tool prints exactly what it sent, and a `--save` is
acknowledged by **a click on each board's output 1** — under wind, a chirp
from each board's lowest pipe. Four chirps means four boards saved.

**Everything is clamped, twice.** The firmware enforces hard ceilings —
hold duty 40%, pull-in 127 ms, ten exercise passes — that no message can
exceed, because a fat-fingered hold duty on a 10%-duty solenoid is a thermal
problem. The tool applies the same clamps client-side and *warns*, so you
learn what actually took effect rather than wondering. Raise the ceilings in
the firmware only for solenoids rated for it.

**Configuration lives on channel 16; music on channel 1.** A stray CC in a
song cannot retune the organ, and the arranger's output contains no CCs at all
anyway. Panic messages (CC 120/123) are honoured on every channel regardless.

**A board selection times out.** `--board` makes only that board listen to
what follows; the tool always sends the selection first, and the firmware
reverts to "all boards" after a quiet minute so a forgotten selection cannot
strand one. Since the four boards run identical solenoids, you will almost
always be addressing all of them.

**Save deliberately, not continuously.** Settings sent without `--save` live in
RAM and vanish at power-off; that is the right mode for tuning. EEPROM has a
finite number of writes, so save when you have settled on values, not on every
tweak.

## Tests

```bash
pip install pytest
pytest tests/
```

Message construction and port matching are tested; nothing opens a real port.
