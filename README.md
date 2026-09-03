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

[`hardware/HARDWARE-NOTES.md`](hardware/HARDWARE-NOTES.md) records how both DIP
switch blocks are actually wired -- including why the small block shows only two
GPIO connections on a multimeter, and why switch 8 of the large block does
nothing -- and how to deal with the schematic's missing symbol cache.

### MIDI input wiring

MIDI arrives on **pin 0 (RXD0)**, the ATmega's hardware UART receive pin — the
same pin upstream used. Feed it a **conventional, non-inverted MIDI signal**:
idle high, standard UART polarity, which is what a correctly-wired optocoupler
front end produces.

If your input idles *low*, the signal is inverted and the UART will never frame
a byte — it sees a permanent break condition. **The symptom is total silence:
the board boots, runs its exercise routine, and ignores every note.** The right
fix is the wiring, but there is an escape hatch for testing:

```cpp
#define MIDI_INPUT_POLARITY MIDI_INPUT_INVERTED
```

The AVR's USART has no receive-invert bit, so inverting in firmware means
abandoning the hardware UART for `SoftwareSerial` in inverse-logic mode.

**Treat that as a bench workaround, not a destination.** `SoftwareSerial`
receives each byte by busy-waiting through it with interrupts disabled — about
320 µs at 31250 baud, which at 8 MHz is a quarter of the CPU's entire budget
for that stretch. A byte arriving during another byte is lost, which is exactly
what dense MIDI produces, and `loop()` stalls meanwhile so the hold PWM gets
rougher too. Costs about 500 bytes of flash. Fine for proving a few notes;
not what you want under a full chest.

#### Fixing it in hardware instead

Invert the signal before it reaches pin 0 and you keep the hardware UART.
Speed is irrelevant here — a bit is 32 µs at 31250 baud, so any logic family
has thousands of times the margin it needs. Choose for signal quality:

- **74HC14**, a Schmitt-trigger hex inverter. The best pick: it inverts *and*
  squares up the lazy edges an optocoupler with a large pull-up tends to
  produce.
- **74HCT04** if you would rather not use a Schmitt — CMOS input currents with
  TTL-compatible thresholds.
- **A single NPN transistor**, which is usually already in the drawer:

```
  opto out ──[4.7k]──┬── B                      +5V
                     │                           │
                   [10k]      2N3904           [1k]
                     │        E ── GND           │
                    GND       C ─────────────────┴──── to pin 0 (RXD0)
```

The 10k from base to ground matters more than it looks: it holds the
transistor off if the input ever floats, so the output idles **high** — which
a UART reads as "no data" rather than as a break condition.

Tie unused gate inputs of a logic IC to GND or Vcc; floating CMOS inputs
oscillate. Power either option from the board's own 5 V rail.

To verify before connecting: with MIDI idle and nothing playing, the inverter
output should sit **high**, near 5 V. If it idles low, it is still inverted.

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
#define NOTE_OFFSET_MODE OFFSET_COARSE3
```

| Mode | Switches used | Steps | Use when |
|---|---|---|---|
| `OFFSET_FULL7` | all 7 of the large DIP block | 1 note | Normal, undamaged hardware |
| `OFFSET_COARSE3` | switches 1-3 only | 16 notes | The other four switches are dead |

**This fork defaults to `OFFSET_COARSE3`**, because the boards it is maintained
against have a damaged large DIP block with only three working switches. If
your switches are all good — which they should be on a board you have just
built — set `OFFSET_FULL7` instead. Nothing else needs to change.

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

| Mode | Behaviour | Needs |
|---|---|---|
| `VELOCITY_OFF` | Every note gets the full pull-in time | — |
| `VELOCITY_ON` | Pull-in time scales with velocity | — |
| `VELOCITY_SMALLDIP` | Read one bit of the small DIP block at boot | Channel not from the small DIP |
| `VELOCITY_SWITCH` | Read large-DIP switch 4 (pin 18) at boot | `OFFSET_COARSE3` |

When enabled, velocity scales the pull-in time between `peakDurationMin`
(15 ms) and `peakDurationMax` (40 ms). Harder notes pull in for longer and
therefore hit harder — meaningful on a struck instrument, meaningless on an
organ valve, which is simply open or shut. Hence the default of off.

**If you want velocity on a physical switch, `VELOCITY_SMALLDIP` is normally
the one to use.** Under `OFFSET_FULL7` every switch of the large DIP block is
part of the note offset, so there is no spare one there — but if the MIDI
channel is fixed in firmware, the whole small DIP block is idle, and
`velocitySmallDipBit` picks which of its four switches to read.

`VELOCITY_SWITCH` is the alternative for a board running `OFFSET_COARSE3`,
where large-DIP switches 4-7 are unused anyway.

These constraints are enforced at compile time. Asking for `VELOCITY_SWITCH`
alongside `OFFSET_FULL7`, or `VELOCITY_SMALLDIP` alongside `CHANNEL_DIP`, fails
the build with a message telling you which switches collided — rather than
flashing cleanly and behaving strangely on the bench.

### Stuck-note watchdog

```cpp
const unsigned long maxNoteDuration = 30000UL;
```

Any solenoid held longer than this is force-released. Set to `0` to disable —
but understand what you are turning off: with the watchdog disabled, one
missing note-off leaves a valve open until you power-cycle the board. 30
seconds is longer than almost any real note. Raise it if your music genuinely
holds notes longer than that.

### Power-up valve exercise

```cpp
const int exerciseCycles  = 2;   // 0 disables
const int exerciseOnTime  = 60;  // ms energised
const int exerciseOffTime = 60;  // ms released
```

Fires every output in turn at boot, at full power, before wind is applied.

The point is **pluck** — the adhesion between a valve and its seat after the
instrument has sat closed. On a lightly loaded valve this can be the largest
single force the solenoid has to overcome, and it is worst on the very first
cycle after a rest, which is exactly the note you least want to fail. Working
the valves loose while the chest is unpressurised costs nothing and no pipe
speaks.

It is sequential rather than simultaneous on purpose. One solenoid at a time
draws a sixteenth of the current, and you hear each output fire in order — so a
dead channel, a swapped connector or a stuck valve announces itself at every
power-up. It doubles as a power-on self test.

Full power is used regardless of `peakDutyPercent`, since breaking stiction is
the one job that genuinely wants maximum force, and one-at-a-time makes the
current draw trivial.

The defaults take `2 × 16 × 120 ms` ≈ **3.8 seconds**. The UART is unattended
during that time, so the receive buffer is discarded afterwards to stop a
part-received message sounding a note the instant the loop opens. Set
`exerciseCycles` to 0 to disable, in which case the shorter boot heartbeat runs
instead and the whole routine is optimised out of the build.

## Daisy-chaining several boards

Boards chain over RJ12 and all see the same MIDI stream. Each one is told which
16 notes are *its* by its DIP switches. All boards run identical firmware.

A four-board chain under the default `OFFSET_COARSE3`, covering MIDI notes
48-111 (C3 to D#8). Base notes are limited to multiples of 16, which is exactly
one board's worth, so four boards tile without gaps:

| Board | Base note | Notes covered | Switches closed |
|---|---|---|---|
| 1 | 48 | 48-63 | 1, 2 |
| 2 | 64 | 64-79 | 3 |
| 3 | 80 | 80-95 | 1, 3 |
| 4 | 96 | 96-111 | 2, 3 |

The same chain under `OFFSET_FULL7`, where any base note is reachable — here
placed at 36-99 (C2 to D#7) instead:

| Board | Base note | Notes covered | Switches closed |
|---|---|---|---|
| 1 | 36 | 36-51 | 6, 3 |
| 2 | 52 | 52-67 | 6, 5, 3 |
| 3 | 68 | 68-83 | 7, 3 |
| 4 | 84 | 84-99 | 7, 5, 3 |

**Verified with two boards**, at base notes set by their DIP switches, sharing
one MIDI signal down the chain. The same binary runs on every board — the note
offset is read from the switches at boot, so nothing in the firmware needs to
differ between them.

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
arduino-cli compile --fqbn MiniCore:avr:328:variant=modelPB,clock=8MHz_internal \
  firmware/midiSolenoidDriverDirectMIDI
```

Which yields, comfortably inside the 328PB:

```
Sketch uses 5896 bytes (18%) of program storage space. Maximum is 32384 bytes.
Global variables use 569 bytes (27%) of dynamic memory, leaving 1479 for locals.
```

**Confirmed running on hardware**, not merely compiling: an ATmega328PB at
8 MHz internal, MIDI decoded on the hardware UART, boot exercise routine and
note handling both working. The optocoupler feeding it was inverting, corrected
with an external NPN inverter as described under
[MIDI input wiring](#midi-input-wiring) — that is the sanctioned fix, in
preference to the firmware workaround.

### ⚠️ The clock setting is not optional

**This board has no crystal.** `XTAL1`/`PB6` and `XTAL2`/`PB7` are wired as
solenoid outputs CTRL5 and CTRL6, so the oscillator pins are unavailable and
the ATmega runs from its internal RC oscillator.

MiniCore defaults to `clock=16MHz_external`. Build with that default and flash
it to a chip fused for 8 MHz internal, and `F_CPU` is wrong by a factor of two:

- `Serial.begin(31250)` actually produces **15625 baud** — no MIDI is received
  at all
- `millis()` and `micros()` run at half speed, so the 40 ms peak becomes 80 ms,
  the hold PWM drops to 250 Hz, and the watchdog stretches to 60 s
- The boot heartbeat still clicks, just slowly — so the board looks alive while
  being completely deaf, which is a miserable thing to debug

Set the clock explicitly, and make it match the fuses actually programmed into
your chips. In the Arduino IDE that is Tools → Clock → "Internal 8 MHz".

31250 baud divides exactly at both 8 and 16 MHz (`UBRR` = 15 and 31
respectively), so there is no divisor rounding error at either. The whole error
budget belongs to the RC oscillator, which is factory-calibrated to about ±1%
but drifts with temperature and supply. UART framing tolerates roughly ±2%, so
this works — it worked for upstream too — but if you ever see occasional
garbled MIDI in a cold or hot organ chamber, oscillator drift is the first
suspect, not the firmware.

Running at 8 MHz also makes `loop()` timing tight enough to matter: see
[Phase staggering](#phase-staggering).

On boot the valve exercise routine runs, or if disabled, output 1 clicks once
for 150 ms as a heartbeat.

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

### Driving the pull-in less hard

```cpp
const int peakDutyPercent = 100;
```

100 means solid DC for the whole peak window. Lower values PWM the pull-in as
well, at the same 500 Hz and with the same per-channel stagger.

**This is the single most effective lever on supply current**, because the
pull-in is the only time every energised channel draws full current
simultaneously — the hold phase is already staggered and duty-limited. Reducing
it also brings the stagger to bear on the attack, which at 100% it cannot
touch.

Reduce it when the solenoids are overspecced for what they move, which is
common: a 22 W actuator shifting a small valve does not need 22 W to seat it.
Tune on the bench under real load — reduce until a solenoid seats sluggishly or
not at all, then back off generously. Too low is worse than too high: notes
fail silently under load, which is far harder to diagnose later than a supply
that runs warm.

Bounds are checked at compile time; 0 or 101 fails the build.

### Worked example: an overspecced, duty-limited solenoid

A U0530S tubular solenoid driving a small valve. Datasheet says 12 V, 6.5 Ω,
1.8 A, 22 W, **rated 10% duty cycle**; **measured coil resistance is closer to
7 Ω**, which is what the figures below use — 1.71 A and 20.6 W at 12 V.

Force falls roughly with the square of coil current, so the numbers move very
differently from the current:

| `peakDutyPercent` | Coil current | Pull-in force | 16-ch attack | 20-ch | 64-ch |
|---|---|---|---|---|---|
| 100 | 1.71 A | 100% | 27.4 A | 34.3 A | 110 A |
| 80 | 1.35 A | 62% | 17.3 A | 21.6 A | 69 A |
| 70 | 1.17 A | 47% | 13.1 A | 16.4 A | 52 A |
| 60 | 0.99 A | 33% | 9.5 A | 11.9 A | 38 A |
| 50 | 0.81 A | 22% | 6.5 A | 8.1 A | 26 A |

The force column is independent of coil resistance — it depends only on the
ratio of held current to full current, which the duty cycle sets. Only the
amperages moved.

A solenoid rated to pull 700 g moving a valve needing perhaps 50 g still has
roughly a 4:1 margin at 60% duty. Three times less supply current for margin
you were never using.

Note how much peak-and-hold is already doing before you touch this: at 100%,
sixteen channels attack at 27.4 A and settle to **4.0 A** holding.

**Capacitors cannot rescue the attack.** C = I·t/ΔV, so covering 34 A for a
40 ms peak within 1 V of droop needs about 1.4 farads. Bulk capacitance tames
switching edges only — the supply must genuinely deliver the attack, or the
attack must come down.

### Solenoids with a duty-cycle rating

Many cheap actuators are rated for intermittent use — "10% ED", "duty cycle
10%" — meaning they are not built to stay energised. Treat the rating as a
thermal budget: **rated wattage × rated duty** is roughly the average
dissipation that produces the quoted temperature rise. For the measured 7 Ω
coil that is 20.6 W at 12 V, so the budget is about **2.1 W**.

Hold dissipation, allowing for the flyback diode clamping the coil during the
off portion:

| `pwmOnTime` | Duty | Coil current | Dissipation | vs a 2.1 W budget |
|---|---|---|---|---|
| 800 | 40% | 0.63 A | 2.74 W | 133% — over |
| 700 | 35% | 0.54 A | 2.00 W | 97% — at the limit |
| 600 | 30% | 0.44 A | 1.38 W | 67% |
| 500 | 25% | 0.35 A | 0.87 W | 42% |

Measuring 7 Ω rather than 6.5 Ω barely moves these proportions, since both the
dissipation and the budget scale the same way with resistance.

What saves you is thermal mass: the coil integrates over minutes, so what
matters is the average across a passage, not any single note. Ordinary playing
with notes down a third of the time is comfortable even at 40%.

The real hazards are a **drone or pedal point held for minutes**, and a **stuck
note**. On duty-limited solenoids the stuck-note watchdog stops being a
convenience and becomes a thermal safety feature — consider shortening
`maxNoteDuration` well below its 30 s default, and note that it will also cut
off any legitimately long note, which is a genuine trade-off rather than a free
win.

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

There is also a small per-board offset derived from the note offset, so chained
boards don't stagger identically. Don't lean on it: each board runs from its
own internal RC oscillator, which varies by a percent or two part to part, so
chained boards drift relative to one another continuously and would never stay
in lockstep anyway. It is a free nudge, not load management.

### Why the offsets are precomputed

At 8 MHz, `loop()` speed is not a detail. A 32-bit modulo per channel per pass
— sixteen of them — costs more than everything else in the loop combined, and
would push the loop period past the 125 µs phase step it is trying to resolve.
A stagger finer than the loop that implements it is no stagger at all.

So each channel's offset is computed once in `setup()` into `channelPhase[]`,
and the hot path uses 16-bit arithmetic with a single conditional subtraction:

```cpp
unsigned int cyclePosition = cycleBase + channelPhase[i];
if (cyclePosition >= pwmPeriod) cyclePosition -= pwmPeriod;
```

Exactly one 32-bit modulo survives, computing `cycleBase` once per pass. The
result is arithmetically identical to the obvious version — verified across all
32000 (position, channel) combinations — at 32 bytes of RAM.

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
- **Tested with two daisy-chained boards, not four.** Two boards sharing one
  single-ended signal down the RJ12 chain, with distinct note offsets, work
  correctly. Longer chains under real solenoid load are still unproven — see
  the note on ground reference under
  [Daisy-chaining several boards](#daisy-chaining-several-boards).
- **Four `unused parameter` warnings** under `-Wall -Wextra`, from MIDI library
  callback signatures that require arguments this firmware doesn't read. They
  do not appear in a default build.

## Ideas and open questions

[`IDEAS.md`](IDEAS.md) parks work that is thought about but not decided: the
Raspberry Pi player, configuring boards over the MIDI bus rather than by DIP
switch, and the interaction between the boot exercise routine and blower
timing.

## Credit and licence

Original project, hardware and firmware: **Willem Hillier**
(<https://github.com/willemcvu/midi-solenoid-driver>), MIT licensed, © 2019.

MIT licence retained — see [LICENSE](LICENSE). Upstream's copyright notice
stays in place; this fork's modifications are offered under the same terms.
