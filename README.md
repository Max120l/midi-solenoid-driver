# midi-solenoid-driver — direct MIDI fork

A fork of [willemcvu/midi-solenoid-driver](https://github.com/willemcvu/midi-solenoid-driver)
that lets the solenoid driver board accept **standard MIDI directly**, with no
interface board in between, and adds **peak-and-hold** solenoid drive,
**selectable MIDI channel**, **optional velocity response**, and **defensive
handling of badly-formed MIDI**.

All hardware in this repository — the driver PCB, the schematics, the gerbers,
the photographs — is Willem Hillier's original work, unchanged. This fork only
replaces the firmware that runs on it.

---

## Why this fork exists

The upstream project is a driver board plus a companion USB-to-serial interface
board. The driver board doesn't speak MIDI; it speaks 38400-baud MIDI-*like*
serial, and the interface board is what turns real MIDI into that. Upstream's
interface work stalled — the `wifi_native_midi` firmware is committed as "not
working" and the repository has had no commits since October 2020. Attempts to
reach the original author about it went unanswered.

Rather than finish the missing interface board, this fork moves the job onto the
driver board itself. The ATmega328PB decodes 31250-baud MIDI on the same RJ12
input pin using the Arduino MIDI Library, so a MIDI source connects straight to
the driver board.

## What's different

| | Upstream `midiSolenoidDriver` | This fork's `midiSolenoidDriverDirectMIDI` |
|---|---|---|
| Input | 38400 baud MIDI-over-serial, needs the interface board | 31250 baud standard MIDI, direct |
| Parsing | Hand-rolled byte-by-byte state machine | Arduino MIDI Library on the hardware UART |
| Running status | Mishandled | Handled |
| Solenoid drive | Full current for the whole note | Peak-and-hold, phase-staggered across channels |
| Velocity | Ignored | Optionally scales pull-in time |
| Note offset | 7-bit DIP switch, 1-note steps | Same, plus a 3-switch fallback mode |
| MIDI channel | 4-bit analog DIP switch | Fixed, DIP-read, or omni |
| Malformed MIDI | Notes can stick on | All-notes-off, stop, reset, and a stuck-note watchdog |

Upstream's original firmware is still here, untouched, in
[`firmware/midiSolenoidDriver/`](firmware/midiSolenoidDriver/). If you have the
USB interface board and it works for you, use that one.

## Hardware

Unchanged from upstream. See [`README-upstream.md`](README-upstream.md) for the
original project description, board photos, and daisy-chaining details.

- Driver board: [`hardware/midi-solenoid-driver/`](hardware/midi-solenoid-driver/)
- MCU: ATmega328PB
- 16 solenoid outputs, "OUTPUTS 1-16" on the silkscreen

### MIDI input wiring

MIDI arrives on **pin 0 (RXD0)**, the ATmega's hardware UART receive pin — the
same pin upstream used. Feed it a **conventional, non-inverted MIDI signal**:
idle high, standard UART polarity, which is what a correctly-wired optocoupler
front end produces. If your input idles *low*, the optocoupler is wired
backwards; fix the wiring rather than the firmware.

The firmware disables the UART **transmitter** while keeping the receiver:

```cpp
UCSR0B &= ~(1 << TXEN0);
```

Several boards share the RJ12 line, and none of them should be able to drive
it. Upstream does the same thing for the same reason. Two consequences worth
knowing:

- **There is no serial debug output.** If you want `Serial.print` while
  bench-testing, comment that line out — but do not do so with the board
  connected to a shared bus, because it will fight every other board on it.
- It also frees pin 1 to be read as a DIP switch input, which is why the
  switches are configured *after* the UART in `setup()`.

Flashing over the bootloader still works normally.

## Configuration

Everything configurable sits in one block at the top of the sketch.

### Note offset — which 16 notes this board plays

```cpp
#define NOTE_OFFSET_MODE OFFSET_FULL7
```

| Mode | Switches used | Steps | Use when |
|---|---|---|---|
| `OFFSET_FULL7` | all 7 of the large DIP block | 1 note | Normal, undamaged hardware |
| `OFFSET_COARSE3` | switches 1-3 only | 16 notes | The other four switches are dead |

`OFFSET_FULL7` is upstream's mapping, restored: switches 1-7 are worth
+1, +2, +4, +8, +16, +32, +64 respectively, active-low, so any base note from
0 to 127 is reachable. The board responds to `baseNote` through
`baseNote + 15`.

`OFFSET_COARSE3` re-weights the three surviving switches to +16, +32 and +64,
giving base notes 0, 16, 32 … 112. One board's worth of notes per step, so a
chain can still address the whole keyboard — just not at arbitrary offsets.

### MIDI channel

```cpp
#define CHANNEL_SOURCE CHANNEL_FIXED
const byte fixedChannel = 1;
```

| Mode | Behaviour |
|---|---|
| `CHANNEL_FIXED` | Uses `fixedChannel`, 1-16 |
| `CHANNEL_DIP` | Reads the 4-bit small DIP block, upstream's `readSmallDip()` ported back |
| `CHANNEL_OMNI` | Responds on every channel |

`CHANNEL_DIP` reads two analog pins, two switches each, using upstream's
voltage thresholds — that block isn't wired to four digital pins. Upstream's
mapping is 0-indexed; this firmware adds 1, so the switches read as the channel
number your DAW displays.

### Velocity response

```cpp
#define VELOCITY_SOURCE VELOCITY_OFF
```

| Mode | Behaviour |
|---|---|
| `VELOCITY_OFF` | Every note gets the full pull-in time |
| `VELOCITY_ON` | Pull-in time scales with velocity |
| `VELOCITY_SWITCH` | Read `velocitySwitchPin` at boot; closed enables it |

When enabled, velocity scales `peakDuration` between `peakDurationMin` (15 ms)
and `peakDurationMax` (40 ms). Harder notes pull in for longer and therefore
hit harder — which is meaningful on a struck instrument and meaningless on an
organ valve, where the valve is simply open or shut. Hence the default of off.

`VELOCITY_SWITCH` uses switch 4 of the large DIP block (pin 18), which is only
free under `OFFSET_COARSE3`. Under `OFFSET_FULL7` that switch is part of the
note offset, so use `VELOCITY_ON` or `VELOCITY_OFF` instead.

### Stuck-note watchdog

```cpp
const unsigned long maxNoteDuration = 30000UL;
```

Any solenoid held longer than this is force-released. Set to `0` to disable —
but understand what you are turning off: with the watchdog disabled, one
missing note-off leaves a valve open until you power-cycle the board. 30
seconds is longer than almost any real note. Raise it if your music genuinely
holds notes longer than that.

## Daisy-chaining several boards

Boards chain over RJ12 and all see the same MIDI stream. Each one is told which
16 notes are *its* by its DIP switches. All boards run identical firmware.

A four-board chain covering MIDI notes 36-99 (C2 to D#7), under
`OFFSET_FULL7`:

| Board | Base note | Notes covered | Switches closed |
|---|---|---|---|
| 1 | 36 | 36-51 | 6, 3 |
| 2 | 52 | 52-67 | 6, 5, 3 |
| 3 | 68 | 68-83 | 7, 3 |
| 4 | 84 | 84-99 | 7, 5, 3 |

Under `OFFSET_COARSE3` you are limited to multiples of 16, so a four-board
chain would sit at 48, 64, 80 and 96, covering notes 48-111.

Points that matter with more than one board:

- **Every board must have its transmitter disabled.** The firmware does this,
  but if you comment it out for debugging on one board, take that board off the
  bus first.
- **Channel.** With `CHANNEL_FIXED` every board listens on the same channel,
  which is what you usually want — they're separated by note range, not by
  channel. Use `CHANNEL_DIP` if you want per-board channels instead.
- **Power.** Four boards is up to 64 solenoids. The phase staggering below
  reduces the peak draw *within* one board; it does not coordinate boards with
  each other. Size the supply for the realistic worst case.

## Building and flashing

Arduino IDE, or `arduino-cli`:

- **Board:** ATmega328PB. Requires a core with 328PB support — e.g.
  [MiniCore](https://github.com/MCUdude/MiniCore).
- **Libraries:** [MIDI Library](https://github.com/FortySevenEffects/arduino_midi_library)
  by Forty Seven Effects.

Open [`firmware/midiSolenoidDriverDirectMIDI/midiSolenoidDriverDirectMIDI.ino`](firmware/midiSolenoidDriverDirectMIDI/midiSolenoidDriverDirectMIDI.ino)
and upload.

Verified building with MiniCore 3.1.3 and MIDI Library 5.0.2, in every
combination of the configuration options above:

```
arduino-cli compile --fqbn MiniCore:avr:328:variant=modelPB \
  firmware/midiSolenoidDriverDirectMIDI
```

Which yields, comfortably inside the 328PB:

```
Sketch uses 5732 bytes (17%) of program storage space. Maximum is 32384 bytes.
Global variables use 555 bytes (27%) of dynamic memory, leaving 1493 for locals.
```

On boot, output 1 clicks once for 150 ms. That's the heartbeat — if you hear
it, the board came up.

## Tuning peak-and-hold

```cpp
const int peakDurationMax = 40;   // ms at full current
const int peakDurationMin = 15;   // ms at lowest velocity, when scaling
const int pwmPeriod       = 2000; // us, hold PWM period (500 Hz)
const int pwmOnTime       = 800;  // us, hold PWM on-time (40% duty)
```

`peakDurationMax` needs to be long enough for the solenoid to physically pull
in — too short and it will buzz or fail to seat under load. `pwmOnTime` needs
to be high enough to *keep* it seated but low enough that the coil doesn't
overheat on sustained notes.

Tune in that order: raise the peak until every solenoid reliably pulls in under
real load, then lower `pwmOnTime` in steps until one drops out, then back off
by a comfortable margin. Small valve solenoids will often hold far below 40%.

These values are a working starting point, not a universal answer.

### Phase staggering

```cpp
const int pwmPhaseStep = pwmPeriod / numSolenoids; // us, 125 us per channel
```

Each channel's hold PWM is offset by `i * pwmPhaseStep`, so the 16 channels'
on-times are spread evenly across the 2 ms period instead of all starting
together. Every coil still gets the same duty and the same holding force; what
changes is the peak the supply has to deliver.

Simulating one full 2 ms period with all 16 channels held, at 40% duty:

| | In phase (`pwmPhaseStep = 0`) | Staggered (125 us) |
|---|---|---|
| Duty per coil | 40% | 40% |
| Mean coils on | 6.40 | 6.40 |
| **Peak coils on** | **16** | **7** |
| Load over the period | 0 coils for 60% of it, then all 16 for 40% | 6 coils for 60% of it, 7 for 40% |

Same average power, same holding force, less than half the peak draw, and a
nearly flat load instead of a hard square wave at 500 Hz. Setting
`pwmPhaseStep` to `0` restores the original behaviour if you want to compare.

There is also a small per-board offset derived from the note offset, so
chained boards don't stagger identically. Don't lean on it: the boards run from
independent crystals and boot at different moments, so their cycles drift
relative to each other regardless, and the offset is smaller than one pass of
`loop()` anyway. It is a free nudge, not load management.

## Robustness with badly-formed MIDI

A solenoid left energised is worse than a missed note: it drones, it heats the
coil, and it heats the driver. Everything here fails toward "off".

**Note-off expressed as velocity 0.** Handled — a note-on with zero velocity is
treated as note-off, per the spec. This is how most sequencers express note-off
in the first place, and how a note "removed" by zeroing its velocity arrives.

**Overlapping and duplicate notes.** A second note-on for a note already
sounding restarts its pull-in, so the note re-articulates rather than being
ignored. A single note-off then releases it, regardless of how many note-ons
arrived. This is deliberately *not* reference-counted: if a file sends two
note-ons and only one note-off, counting would hold the valve open forever,
whereas last-off-wins simply releases it. The failure mode is a note that stops
slightly early, not a valve that never closes.

**Panic messages.** CC 120 (All Sound Off), CC 123 (All Notes Off), MIDI Stop
and System Reset all release every solenoid. Sequencers emit these on stop, on
panic, and at the end of a file, and honouring them is the difference between a
clean stop and a chord left droning.

**Note-offs that never arrive.** The watchdog force-releases anything held past
`maxNoteDuration`. This is the backstop for a truncated file, a crashed
sequencer, or a cable pulled mid-note.

**Running status and dense streams.** Upstream's hand-rolled parser mishandled
running status, which tightly-packed files use heavily. The MIDI library
handles it correctly, along with interleaved real-time bytes and message types
this board ignores. Reception is on the hardware UART, which is interrupt-driven
and buffered — the previous `SoftwareSerial` implementation busy-waited through
each byte with interrupts disabled for roughly 320 us, during which a second
byte arriving was simply lost. That was the real hazard at high note density.

**A note on tempo.** Tempo changes are a Standard MIDI File concept: meta-events
resolved by whatever plays the file. They never reach this board, which only
ever sees note and control messages already placed in time by the player. There
is nothing for the firmware to do about them, and any firmware claiming to
handle tempo would be doing nothing. What tempo *does* affect is how densely
events arrive — and that is exactly what moving to the hardware UART addresses.

## Known limitations

Things this fork does not yet do. Contributions welcome.

- **No aftertouch or polypressure.** Stubbed but unimplemented upstream too.
- **Velocity affects pull-in time only.** It does not vary holding force, and
  on a valve it has no acoustic effect at all.
- **The per-board PWM offset is below `loop()` resolution**, as described above.
- **Untested with daisy-chained boards** in this direct-MIDI configuration.
- **Four `unused parameter` warnings** under `-Wall -Wextra`, from MIDI library
  callback signatures that require arguments this firmware doesn't read. They
  do not appear in a default build.

## Credit and licence

Original project, hardware and firmware: **Willem Hillier**
(<https://github.com/willemcvu/midi-solenoid-driver>), MIT licensed, © 2019.

MIT licence retained — see [LICENSE](LICENSE). Upstream's copyright notice
stays in place; this fork's modifications are offered under the same terms.
