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

## Resetting boards from the Pi

`organ_reset.py` pulses each board's RESET through an optocoupler wired to a
Pi GPIO — see the hardware notes for the circuit. The Pi and the organ share
no ground; the reset line is isolated for the same reason the MIDI line is.

```bash
pip install gpiozero lgpio
python organ_reset.py --all
python organ_reset.py --board 2
```

Default GPIOs are 17, 27, 22 and 23 (physical pins 11, 13, 15, 16), one per
board in order; `--pins` changes that. A reset reloads the board's saved
settings and runs its exercise routine, so under wind it plays a scale — set
`--exercise 0 --save` with `organ_config` first if that matters.

## Playing solenoids from the keyboard

`organ_keys.py` is the commissioning screen: the 64 solenoids drawn as four
boards, numbered as on the layout sheet, with the key that fires each one and,
given the organ definition, what it is.

```bash
python organ_keys.py --serial /dev/serial0 --organ ../organ-arranger/instrument/organ.yaml
```

```
 board 2   solenoids 17-32   <-- keys
 17 1     18 2     19 3     20 4     21 5     22 6     23 7     24 8
 Tn E4    Tn F#4   Tn A4    Tn B4    Tr C6    Tr E6    Tr F#6   Tr A6
*25 q     26 w     27 e     28 r     29 t     30 y     31 u     32 i
 Tr B6    F2 Bas   C2 Bas   C4 Acc   E6 Mel   A#3 Acc  C6 Mel   G3 Acc
```

| Key | Does |
|---|---|
| `1`–`8`, `q`–`i` | fire solenoids 1–8 and 9–16 of the selected board |
| `z` `x` `c` `v`, `Tab` | select board 1–4 |
| `h` | hold mode: a key opens a valve until pressed again (leaving hold mode closes everything) |
| `a` | play the selected board's sixteen in a row |
| `-` `=` | tap length down / up (default 150 ms) |
| `space`, `0` | everything off, plus an all-notes-off on the channel |
| `Q`, `Esc` | quit, everything off first |

A tap is a pulse, because a terminal cannot see a key being released. Hold
mode is for tuning a pipe or finding a tube that goes nowhere. `--dry-run`
shows the screen with no output; `--port` uses a MIDI interface instead of
the UART; `--solenoid-1-note` overrides what the definition says about the
wire. Labels are `Tn`/`Tr` for TenorCM and TrebCM, the note, and `Bas`/`Acc`/
`Mel` for Main's sections; registers show as `Trom B+` (Trombone Base on).

## Tests

```bash
pip install pytest
pytest tests/
```

Message construction, port matching and the keyboard console's model are
tested; nothing opens a real port or a screen.
