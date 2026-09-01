# midi-solenoid-driver — direct MIDI fork

A fork of [willemcvu/midi-solenoid-driver](https://github.com/willemcvu/midi-solenoid-driver)
that lets the solenoid driver board accept **standard MIDI directly**, with no
interface board in between, and adds **peak-and-hold** solenoid drive.

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
driver board itself. The ATmega328PB now decodes 31250-baud MIDI on the same
RJ12 input pin using the Arduino MIDI Library, so a MIDI source connects
straight to the driver board.

## What's different

| | Upstream `midiSolenoidDriver` | This fork's `midiSolenoidDriverDirectMIDI` |
|---|---|---|
| Input | 38400 baud MIDI-over-serial, needs the interface board | 31250 baud standard MIDI, direct |
| Parsing | Hand-rolled byte-by-byte state machine | Arduino MIDI Library, on inverted SoftwareSerial |
| Solenoid drive | Full current for the whole note | Peak-and-hold: full current to pull in, then phase-staggered PWM to hold |
| Note offset | 7-bit DIP switch, 1-note steps | 3 switches, 16-note steps |
| MIDI channel | 4-bit analog DIP switch | Fixed in firmware |

The last two rows are a downgrade, not an improvement — they're a concession to
a board whose large DIP switch block has only three working switches. See
[Known limitations](#known-limitations).

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

The firmware reads MIDI on **pin 0** (the RJ12 serial input) using
`SoftwareSerial` in **inverse-logic mode**:

```cpp
SoftwareSerial invertedSerial(0, 255, true);
```

That third argument matters. It means the firmware expects the MIDI signal
already inverted at the pin — the polarity you get straight off a MIDI current
loop, without an inverting optocoupler stage in front of it. If you feed it a
conventional non-inverted MIDI signal, you will get nothing. Change the third
argument to `false` in that case.

### DIP switches

Three switches set the lowest MIDI note the board responds to, in steps of 16
(one board's worth of notes per step). Switches are active-low.

| SW1 (pin 15) | SW2 (pin 16) | SW3 (pin 17) | Base note |
|---|---|---|---|
| off | off | off | 0 |
| **on** | off | off | 16 |
| off | **on** | off | 32 |
| **on** | **on** | off | 48 |
| off | off | **on** | 64 |
| **on** | off | **on** | 80 |
| off | **on** | **on** | 96 |
| **on** | **on** | **on** | 112 |

The board responds to notes `baseNote` through `baseNote + 15`. Daisy-chained
boards each get a different switch setting so they cover different octaves.

MIDI channel is fixed at **1** in firmware (`const int midiChannel = 1;`).

## Building and flashing

Arduino IDE, or `arduino-cli`:

- **Board:** ATmega328PB. Requires a core with 328PB support — e.g.
  [MiniCore](https://github.com/MCUdude/MiniCore).
- **Libraries:** [MIDI Library](https://github.com/FortySevenEffects/arduino_midi_library)
  by Forty Seven Effects; `SoftwareSerial` ships with the Arduino core.

Open [`firmware/midiSolenoidDriverDirectMIDI/midiSolenoidDriverDirectMIDI.ino`](firmware/midiSolenoidDriverDirectMIDI/midiSolenoidDriverDirectMIDI.ino)
and upload.

On boot, output 1 clicks once for 150 ms. That's the heartbeat — if you hear it,
the board came up.

## Tuning peak-and-hold

Three constants at the top of the sketch:

```cpp
const int peakDuration = 40;   // ms at full current before dropping to hold
const int pwmPeriod    = 2000; // us, hold PWM period (500 Hz)
const int pwmOnTime    = 800;  // us, hold PWM on-time (40% duty)
```

A fourth is derived rather than set by hand:

```cpp
const int pwmPhaseStep = pwmPeriod / numSolenoids; // us, 125 us per channel
```

Each channel's hold PWM is offset by `i * pwmPhaseStep`, so the 16 channels'
on-times are spread evenly across the 2 ms period instead of all starting
together. Every coil still gets the same 40% duty and the same holding force;
what changes is that the supply sees roughly 7 coils' worth of hold current at
any given moment rather than all 16 at once. On a full chord that is a
substantially gentler load, and it takes some of the growl out of the hold.
There is normally no reason to change it, but setting it to `0` restores the
original all-in-phase behaviour if you want to compare.

`peakDuration` needs to be long enough for the solenoid to physically pull in —
too short and it will buzz or fail to seat under load. `pwmOnTime` needs to be
high enough to *keep* it seated but low enough that the coil doesn't overheat on
sustained notes. Start conservative (longer peak, higher duty), then reduce
while listening for dropouts.

These values are a working starting point for one particular set of solenoids,
not a universal answer. Yours will differ.

## Known limitations

Things this fork does not yet do. Contributions welcome.

- **MIDI channel is not selectable.** Upstream read it from a 4-bit analog DIP
  switch (`readSmallDip()`); that code is still in the original firmware and
  could be ported back in.
- **Note offset is coarse.** 16-note steps rather than upstream's 1-note steps,
  because only three switches survived on this board. A board with the full
  7-switch block can restore fine offsets.
- **Hold PWM timing jitters while MIDI is arriving.** `SoftwareSerial` receives a
  byte by busy-waiting through it with interrupts disabled, roughly 320 us at
  31250 baud, during which `loop()` does not run and PWM edges land late. It is
  audible only as a slight roughness in the hold under dense MIDI. Moving to the
  hardware UART, or to timer-driven PWM, would remove it.
- **No velocity response.** Velocity is used only as an on/off test. The
  hardware could plausibly vary `peakDuration` or hold duty with velocity.
- **No aftertouch or polypressure.** Stubbed but unimplemented upstream too.
- **Untested with daisy-chained boards** in this direct-MIDI configuration.

## Credit and licence

Original project, hardware and firmware: **Willem Hillier**
(<https://github.com/willemcvu/midi-solenoid-driver>), MIT licensed, © 2019.

MIT licence retained — see [LICENSE](LICENSE). Upstream's copyright notice
stays in place; this fork's modifications are offered under the same terms.
