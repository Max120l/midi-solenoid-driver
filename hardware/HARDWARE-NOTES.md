# Hardware notes

Findings from tracing the driver board's KiCad files, recorded because several
of them are easy to misdiagnose on the bench.

Everything here describes Willem Hillier's original board. This fork changes no
copper, no footprint geometry and no connectivity — only the library
*references* the files use to find their symbols, which had rotted with age.

## DIP switches, as actually wired

### SW2 — the large block: 8 positions, 7 of them usable

Traced from the pad-to-net assignments in `midi-solenoid-driver.kicad_pcb`.

| Switch | Pad | Net | MCU pin | Weight in firmware |
|---|---|---|---|---|
| 1 | 1 | `MIDI_NOTE_BIT0` | 15 | +1 |
| 2 | 2 | `MIDI_NOTE_BIT1` | 16 | +2 |
| 3 | 3 | `MIDI_NOTE_BIT2` | 17 | +4 |
| 4 | 4 | `MIDI_NOTE_BIT3` | 18 | +8 |
| 5 | 5 | `MIDI_NOTE_BIT4` | 19 | +16 |
| 6 | 6 | `MIDI_NOTE_BIT5` | 1 | +32 |
| 7 | 7 | `MIDI_NOTE_BIT6` | 2 | +64 |
| 8 | 8 | GND | — | **none** |

**Switch 8 does nothing.** Pad 8 and its opposite pad 16 are both on GND, so
closing it connects ground to ground. There is no spare switch on this block —
worth knowing before planning a feature around one.

Switch 7 sits on pin 2, and switch 6 on pin 1, which is the UART transmit pin.
That is why the firmware disables the transmitter before reading the switches.

### SW1 — the small block: 4 positions, all wired, only 2 pins

Labelled `MIDI_CHANNEl` in the schematic (upstream's typo, kept here so the
name stays searchable).

| Switch | Pad | Goes to | Reaches the MCU as |
|---|---|---|---|
| 1 | 1 | `RN3` pad 2 | `MIDI_CH_BIT0-1` |
| 2 | 2 | `RN3` pad 5 | `MIDI_CH_BIT0-1` |
| 3 | 3 | `RN4` pad 2 | `MIDI_CH_BIT2-3` |
| 4 | 4 | `RN4` pad 5 | `MIDI_CH_BIT2-3` |

The other side of every switch is GND.

**Continuity-testing this block finds only two connections to GPIO pins, and
that is correct.** Four switches are summed through resistor networks onto two
analog nets, and the firmware separates them by voltage threshold —
`analogRead(A0)` and `analogRead(A7)` in upstream's `readSmallDip()`. A
multimeter cannot see four connections because there are only two wires.

Practical consequence: this block cannot be read as digital inputs. Anything
wanting a physical switch has to go through `readSmallDip()`, and only when the
MIDI channel is not already using the block.

## The gate indicator LEDs cannot work as drawn

D17-D32, the small LEDs on the MOSFET gates, are wired anode to the gate and
cathode to ground, **with no current-limiting resistor of their own**:

```
U1 pin --- RN7 (1k) --- GATE --- Q1 gate
                          |
                        D17 (A)
                          |
                         GND
```

`RN7` is the gate resistor and simultaneously the LED's only current limit.
Once the LED forward-biases it **clamps the gate at its forward voltage** —
around 2 V for a red part. The MOSFET sees 2 V of Vgs instead of 5 and cannot
pass any useful current.

Fitted the "correct" way round, the LED lights and the solenoid does not fire.
Fitted backwards it is reverse-biased and inert, so the gate reaches 5 V and
everything works — which is presumably why boards shipped this way went
unnoticed. Removing the LED entirely also works.

This is independent of how the outputs are driven. The clamp is a DC level, so
it applies equally to upstream's plain on/off firmware and to peak-and-hold
PWM — the gate never exceeds the LED's forward voltage either way.

Whether it *appears* to work depends on the load. At 2 V of Vgs a logic-level
MOSFET is weakly on rather than off, passing perhaps tens to a couple of
hundred milliamps in its linear region. A small valve magnet drawing ~120 mA
might actuate on that: hot, inefficient, and cooking the MOSFET, but working
well enough that nobody looks closer. Anything approaching an amp will not
move at all. Since the board is fused for 2.5 A per channel, the fault would
have broken the hardware's intended load too — so the LEDs were most likely
never populated, or always fitted backwards.

### If you want working indicators

Give each LED a series resistor so it stops clamping the gate. Assuming a 2 V
red part and a 5 V drive:

| Series R | Gate voltage | LED current |
|---|---|---|
| none (as drawn) | 2.0 V | 3.0 mA — MOSFET will not switch |
| 2.2k | 4.1 V | 0.94 mA |
| **4.7k** | **4.5 V** | **0.53 mA** |
| 10k | 4.7 V | 0.27 mA |

4.7k is the reasonable compromise: 4.5 V is ample for a logic-level MOSFET and
half a milliamp is visible on a modern 0603 indoors. In practice this means
lifting a pad and inserting an 0603 in series with each of sixteen LEDs, which
is unpleasant enough that **not fitting them at all is a defensible choice.**

A higher-forward-voltage LED (blue or white, ~3.2 V) raises the clamp rather
than removing it. Whether 3.2 V of Vgs is enough depends entirely on the
MOSFET, and the schematic specifies only a generic `Q_NMOS_GDS` with no part
number — check what is actually populated before relying on it.

**For a board respin**, move the indicator to the drain: LED plus its own
resistor from +12 V to the output node. That reports the output actually
switching rather than the gate merely going high, is fully decoupled from the
gate, and about 4.7k off the 12 V rail gives a properly bright 2 mA.

## Fusing: 20 A per bank is not protection

The board carries two blade fuses, each feeding a bank of eight outputs, and
the schematic specifies **20 A** for each -- 2.5 A per channel, 40 A per
board. For solenoids sized to an actual load that is far too much, and it is
worse than merely too much.

A fuse protects the **wire**, not the solenoid: it exists so that a short
downstream -- a chafed lead, a coil failed to its frame -- opens the fuse and
not the loom. Two things follow:

- **Rating at or below the wire's ampacity.** Typical solenoid hookup wire is
  22 AWG (roughly 5-7 A) or 20 AWG (roughly 10 A). A 20 A fuse behind 22 AWG
  makes the wire the fuse.
- **The supply must be able to open it.** A switch-mode supply current-limits
  at perhaps 120-150% of its rating, then hiccups. A 10 A supply delivers
  maybe 13-15 A into a dead short, which never opens a 20 A fuse. It would
  hiccup into the fault indefinitely, protecting nothing.

What a bank of eight 7 ohm coils actually draws at this fork's settings (60%
pull-in, 25% hold): about 0.7 A with all eight holding, about 4.7 A with all
eight attacking at once for 40 ms, and 1.7 A during the boot exercise, which
fires one coil at a time. A blade fuse carries 135% of its rating for minutes
and about 200% for a second, so a 40 ms attack is invisible to it. The music
does not size the fuse; the wire and the supply do.

**5 A per bank**, and on this instrument that is a ceiling rather than a
preference: the solenoid leads are **22 AWG**, good for about 5-7 A, so
anything larger stops protecting them. 5 A is still comfortably above
anything the music does and opens fast on a real short from a modest
supply. A bank could only go to 7.5 A after rewiring it in 20 AWG. Add one
main fuse at the supply output sized to the supply, so the feeds and the
star ground are covered as well.

No fuse protects a coil that is stuck **on**: 1.7 A continuous through a
10%-duty solenoid trips nothing and simply cooks. That is the firmware
watchdog's job, and a supply relay's.

## Supply wiring: sized for voltage drop, not heat

What sizes the supply wiring is not the current's heating but the voltage it
drops -- and above all the drop on the **ground** leg, since that is the
reference the single-ended MIDI line is measured against on each board.

One board's worst case is all sixteen coils attacking at once: about 9.4 A
for 40 ms at 60% pull-in on 7 ohm coils. Sustained, it holds under 1.5 A, so
thermally even thin wire survives. Drop over a 2 m loop at that peak:

| Gauge | Loop drop | Ground bounce on that board |
|---|---|---|
| 22 AWG | 1.0 V | ~0.5 V |
| 20 AWG | 0.62 V | ~0.3 V |
| **18 AWG** | **0.39 V** | **~0.2 V** |
| 16 AWG | 0.24 V | ~0.12 V |
| 14 AWG | 0.16 V | ~0.08 V |

The UART input reads under about 1.5 V as low and over 3 V as high; 0.2 V of
bounce is comfortable, 0.5 V starts eating the margin during exactly the
moments -- big attacks -- when a corrupted byte hurts most.

- **18 AWG per board**, 12 V and ground as a twisted or bundled pair back to
  the star point; 16 AWG for any run over about 1.5 m.
- **14 AWG for the trunk** from the supply to the star point. A full 64-coil
  attack never happens musically, but a big chord is 10-15 A.
- **Main fuse in the trunk's positive leg**, at the supply end.
- **Star point at the supply**, or a bus bar beside it. Never board to board:
  a chained ground puts every upstream board's drop under the last board's
  reference.

The 12 V terminal block accepts up to about 12 AWG. Tin or ferrule stranded
ends; a loose strand in a screw terminal is a future intermittent.

## There is no crystal

`XTAL1`/`PB6` (pin 7) and `XTAL2`/`PB7` (pin 8) are wired as solenoid outputs
CTRL5 and CTRL6. The oscillator pins are unavailable, there is no crystal or
resonator anywhere in the design, and the ATmega runs from its internal RC
oscillator.

**Firmware must be built for the internal oscillator**, matching the fuses in
your chips — 8 MHz internal on the boards this fork is maintained against.
MiniCore defaults to `clock=16MHz_external`, and building with that default
produces a binary that flashes cleanly, clicks its boot heartbeat, and receives
no MIDI whatsoever, because `Serial.begin(31250)` lands on 15625 baud. See the
README's build section.

31250 divides exactly at 8 MHz (`UBRR` = 15), so all of the baud error budget
belongs to the RC oscillator itself — around ±1% calibrated, drifting with
temperature and supply, against a UART tolerance of roughly ±2%. It works, and
worked for upstream, but it is the first thing to suspect if MIDI ever garbles
in a cold or overheated chamber.

## Programming over ISP fires solenoids

The ISP header shares its SPI lines with three outputs: MOSI is `CTRL13`,
MISO is `CTRL14`, SCK is `CTRL15` (pins 12, 14 and 17 of the MCU). While a
programmer is clocking data in, MOSFETs 13, 14 and 15 are switching. When
programming finishes and reset releases, the board boots and runs the
exercise routine: every output, at full power, in sequence.

**This happens even with the 12 V supply switched off** if the programmer is
supplying 5 V through the header. A buck regulator conducts backwards through
its high-side body diode when its output is held above its input, so the
board's 5 V rail back-feeds the "12 V" node to roughly 4 V — 0.6 A through a
7 Ω coil, weak but enough to click an unloaded plunger. Confirm with a meter
on the 12 V rail with only the programmer connected.

The procedure that is safe regardless:

1. 12 V supply **off**, **both fuses out** — that opens the fused rail and
   nothing can fire whatever the 12 V node is doing.
2. Programmer connected, supplying 5 V and ground through the header.
3. Program.
4. Programmer **disconnected**, fuses **in**, 12 V **on**.

Do not run the programmer's 5 V and the board's own supply at the same time:
that is two regulators contending for one rail.

Since tuning is done over MIDI (see the README), reflashing is rare, which is
the best mitigation of all.

## MIDI input: the isolated link from the Pi

The Pi's UART reaches all four boards through one optocoupler module, a
generic single-channel board (marked GYJ-0109-B) carrying a **6N137** with a
360 Ω input resistor, a two-transistor output buffer and its own pull-up on
OUT. A 6N137 is fast enough for 31,250 baud with a wide margin; the slow
PC817 class is not, and belongs on the reset lines instead.

The module is wired the MIDI way, with the Pi's TX **sinking** the LED
current rather than sourcing it:

```
Pi 3.3 V (header pin 1 or 17) ── IN+ (anode)          Pi side
Pi TX, GPIO 14 (header pin 8) ── IN- (cathode)
                                 ────────────────────── isolation barrier
organ 5 V (RJ12 bus pin 6) ───── VCC
OUT ─────────────────────────── RX of all four boards
organ GND ───────────────────── GND
```

With TX idle high, both ends of the LED sit at 3.3 V, nothing flows and the
LED is dark; the 6N137 is inverting, so OUT idles at the 5 V the boards
expect. Each zero bit pulls TX low, the pin sinks about 5 mA through the
360 Ω, and OUT drops. Measured on the bench: OUT idles at 5 V and dips on
every note. No Pi ground goes to the module at all -- the TX pin is the
return -- and nothing links the two grounds.

The two ways this was got wrong first, kept here so they are not repeated:

- **IN+ to TX, IN- to Pi GND** lights the LED at *idle* and leaves it dark on
  zero bits. OUT then sits low (about 0.9 V here, a solid low for the AVR's
  1.5 V threshold) and the boards see a permanent break. Pulling the input
  wires and watching OUT rise to 5 V is the quick way to recognise it.
- **Swapping the two input wires** reverse-biases the LED, which then never
  lights: OUT stays at 5 V for ever. The module's IN+/IN- polarity is fixed;
  only the Pi ends of the wires move.

Feed the LED from 3.3 V, not 5 V: with TX resting at 3.3 V, a 5 V anode would
leave 1.7 V across the LED and the chip might never switch fully off. The
360 Ω gives about 5 mA from 3.3 V, right at the 6N137's minimum; if a module
of this type proves marginal, a 1 k across R1 brings it to about 7 mA. Check
the board's indicator LEDs too: on some of these modules the signal LED is in
series with the input and eats 2 V of the 3.3 V drive, in which case bridge
it. This one did not need either change.

Because this is the standard MIDI IN circuit, any ordinary MIDI source drives
the module unchanged: DIN pin 4 to IN+, DIN pin 5 to IN-, pin 2 left
unconnected on this side. A source's two 220 Ω legs plus the 360 Ω put the
loop at about 4.5 mA from a 5 V keyboard, so the 1 k across R1 is worth
fitting before a keyboard is used. A 1N4148 across the input (cathode on
IN+) protects the LED from a reversed source, as every MIDI IN has. To make
the Pi a standard source too, give it a MIDI OUT: 3.3 V through 33 Ω to DIN
pin 4, TX through 10 Ω to pin 5, ground to pin 2 (the spec's 3.3 V values).
Two sources cannot share one opto -- an idle source holds the loop open --
so switch cables, or use a merge box, or send the keyboard to the Pi over
USB and let the Pi forward.

An earlier home-made input had the Pi's TX lighting the LED at idle behind a
non-inverting output stage. It worked with the Pi, which does not care which
state carries the current, but it is upside down for a MIDI source and
would have shown a keyboard as a permanent break. The inverting 6N137 with
TX on the cathode is what makes the input standard.

## Faults found while commissioning four boards

Hand-assembled boards, sixteen channels each. What went wrong, how it
showed, and what found it -- worth running through before blaming the
tubing or the arranger.

| Fault | How it showed | Found by |
|---|---|---|
| Solder bridges between neighbouring gate pads | two solenoids on one key, or one that never released | the row walk in `organ_keys`: a bridge fires a pair |
| Series gate network (RN7-RN10) not soldered on the MCU side | the solenoid dead, or firing on its own: the gate floats and follows its neighbours | continuity from the MCU pin to the network, then about 1 k through to the gate |
| Snubber diode reversed on one channel | the solenoid never fired under power, though every joint tested fine: the diode conducts across the coil the moment the FET turns on, and only the hold PWM saved the FET | ohms across the diode against a working channel; nothing else sees it |

Per channel the parts are Q*n* (FET), D*n* (snubber, across the coil),
D(16+*n*) (gate indicator, see above), and one element each of the series
network and the pull-down network: RN7/RN1 for channels 1-4, RN8/RN5 for
5-8, RN9/RN2 for 9-12, RN10/RN6 for 13-16. On the series networks the gate
for channel *n* of the group is pin *n* and its MCU input is pin 9 - *n*.

## The bellows pump

Wind comes from a bellows driven by a 1/2 HP single-phase mains motor: about
10 A at 120 V running, a starting surge of several times that. The Pi
switches it through a **solid-state relay of the Fotek SSR-xx DA pattern**
(DC input 3-32 V, AC load, zero-crossing), bought as an 80 A unit on
2026-09-17 and treated as a 30 A one, since the clones' labels run high.
The SSR is itself an optocoupler -- LED in, light across, triac out -- so
the Pi drives it directly, no further isolation:

```
Pi GPIO 24 ── SSR terminal 3 (+)        Pi side
Pi GND ────── SSR terminal 4 (-)
              ───────────────────────── isolation, inside the SSR
mains live ── SSR terminal 1 ── terminal 2 ── motor
```

GPIO 24 rests low at boot, so the pump is off until organ_web says otherwise
(`--pump 24`, no active-low flag). organ_web owns the relay: on when songs
are released to the player, held for the warm-up, off after the idle
time-out or when the service stops.

What the rating does not buy: an SSR drops about 1.5 V whatever its label,
so at 10 A it makes ~15 W and lives on its heat sink with thermal paste and
moving air; and an SSR fails *shorted*, so the mains line keeps a switch
within reach, a fuse or breaker sized for the motor, and the motor's own
thermal protector. An MOV across terminals 1 and 2 takes the motor's
switch-off kick. The output leaks a milliamp or two when off: the motor
does not turn, but the wires are live until the switch says otherwise.

### The panel switch: Off / Book / Auto

The organ keeps its book-reading keyframe, and in book mode the motor must
run with every piece of electronics off. One two-pole, three-position,
centre-off, motor-rated switch (marked 1 HP at 125 V, or 20 A) on the panel
is the organ's master control:

```
pole A: the motor                                pole B: the electronics
mains L ─ fuse ─┬─ common                        mains L ─ fuse ─┬─ common
   BOOK      1  ├──────────────────► motor          BOOK      1  ├── (nothing)
   AUTO      2  ├─► SSR 1 ── 2 ───► motor           AUTO      2  ├──► ATX mains in, Pi supply, fan
   OFF       0  ┘                                   OFF       0  ┘
```

Book: the motor runs, nothing else has power. Auto: the box comes alive,
the Pi boots and the ATX waits on standby until the Pi pulls PS_ON; the
motor runs only when the SSR says so. Off: everything isolated, which the
SSR alone cannot do since it leaks a milliamp. Only the live is switched;
neutral and earth go straight through. If the Pi is on and a tune is
played while the switch is in Book, the SSR closes in parallel with the
manual contact, which is harmless.

Turning Auto off cuts the Pi's mains. The front desk's **Shut down** button
takes the pump and the 12 V down and powers the Pi off first; turn the
switch off once the screen is dark. Pi OS usually survives a hard cut, but
the habit removes the "usually".

### The 12 V supply, and switching it from the Pi

The 12 V comes from an **ATX computer supply**, which puts every rail on
one ground: so its 12 V feeds the organ, and its 5 V feeds nothing on the
Pi's side -- the Pi has a supply of its own, double insulated, no earth --
or the isolation would be undone inside the box. Several yellow wires in
parallel for the 12 V, several black for the return; a 10 Ω 10 W resistor
across 5 V to ground if the 12 V regulates badly with nothing else loaded.

An ATX supply starts when its green wire, **PS_ON**, is pulled to its own
ground. That is on the organ's side of the divide, so the Pi switches the
whole solenoid supply through an opto, exactly as it drives the reset line:

```
Pi GPIO ──[330 Ω]──► PC817 pin 1 (anode)          Pi side
Pi GND ────────────── PC817 pin 2 (cathode)
                      ──────────────────────────── isolation barrier
                      PC817 pin 4 (collector) ── ATX PS_ON (green)
                      PC817 pin 3 (emitter)   ── ATX GND (black)
```

The 5 V standby rail keeps the PS_ON logic alive while the rest is dark.
organ_web drives it as `--power GPIO`: on before the pump, off after the
idle time-out, and on again for the Service actions, which wait a moment
for the boards to boot. Every power-up runs the boards' exercise routine,
this organ's solenoids start cleanly under wind, so exerciseCycles is 0 here
(the Settings card's default) and the Service tab's reset plays the scale when
one is wanted.

The SSR lives in a metal box with the organ's 12 V supply, the Pi's 5 V
supply and a fan. Mains earth to the box; **neither DC negative bonded to
it**, and no shared ground strip: the 12 V negative is the organ's ground,
the 5 V negative is the Pi's, and the three optos (MIDI, reset, pump) are
what keep them apart. Mains side and low-voltage side on opposite sides of
a partition; the heat sink in the fan's stream.

## Remote reset from the Pi

`/RESET` is on **pin 5 of the ISP header, with GND on pin 6** beside it,
pulled up on the board by R2 (10k). Pulling it low resets the board; every
MCU pin goes high-impedance, the gate pull-downs turn all sixteen outputs off,
and on release the board reloads its saved settings and runs the exercise
routine. That makes a reset line the right tool for recovering a hung board
without opening the case.

**Do not run a bare wire from a Pi GPIO.** It would tie the Pi's ground to
the organ's -- the ground plane that sinks the solenoid current -- which is
exactly what the optocoupler on the MIDI input exists to prevent. Isolate the
reset line the same way.

### One common line, on the RJ12 bus

Decided 2026-09-14: **one reset line for the whole chain**, not one per
board. The boards cannot talk back, so the Pi never knows which board hung
and would reset all of them anyway; a whole-organ reset costs milliseconds
plus the exercise routine, which exerciseCycles = 0 silences; and a common
line is what a modular chain wants -- a fifth board gets the same bodge and
nothing on the Pi changes. The only loss is diagnosis: a shorted reset
bodge on any board holds all of them in reset.

The line rides the spare pins of the RJ12 chain, together with 5 V for the
opto modules at the head:

| RJ12 pin | Carries | On every board | Once |
|---|---|---|---|
| 1 | `/RESET`, common | jumper J1 pin 1 to J3 pin 1; tap to ISP pin 5; 100 nF from ISP pin 5 to pin 6 | |
| 6 | 5 V for the opto modules | jumper J1 pin 6 to J3 pin 6 | fed from ISP pin 2 (VCC) on the **last** board only |
| the two the boards already use | MIDI signal, GND | as shipped | |

The jumpers are needed because the spare pins have no copper between J1 and
J3 (in the PCB file only pins 1 and 4 are netted, and only for the RS485
revision). The 5 V comes from one board so that four regulators are never
paralleled on one wire; the modules draw about 15 mA, so the drop over the
whole chain, even from the far end, is a few millivolts. 30 AWG wire-wrap
wire is ample for both lines at these currents.

**Cables must be straight-through.** With `/RESET` on pin 1 and 5 V on
pin 6, a reversed ("telephone") RJ12 cable joins the two buses: the reset
line is then tied to 5 V and the opto shorts the rail when it fires. The
existing cables are straight or the MIDI pair would not have worked, but a
new cable needs checking before it goes in.

```
Pi GPIO 17 ──[330 Ω]──► PC817 pin 1 (anode)          Pi side
Pi GND ───────────────── PC817 pin 2 (cathode)
                         ─────────────────────────── isolation barrier
                         PC817 pin 4 (collector) ── RJ12 bus pin 1 (/RESET)
                         PC817 pin 3 (emitter)   ── RJ12 bus GND
```

The opto sits at the head of the chain beside the MIDI module and needs one
channel only: the four 10k pull-ups in parallel are 2.5k, 2 mA, which any
PC817 sinks while saturating near 0.2 V, well under the AVR's reset
threshold of about 1 V. A ready-made module does as well, as long as its
output is a bare open collector or a transistor that pulls low when the LED
is lit -- an "inverting" module in seller language. Whatever the part, the
test that matters is at rest: with the GPIO low, or the Pi off, `/RESET`
must read high on the ISP header, or the organ sits in reset whenever the
Pi is down. The output is open-collector, so it does not interfere with an
ISP programmer -- simply do not assert it while flashing.

The line now runs through the chest next to solenoid wiring, which is why
each board gets the 100 nF: with the 2.5k pull-up it filters about a
millisecond, which no real reset pulse notices and no coupled spike
survives. `/RESET` has no capacitor of its own on the board.

GPIO 17 (physical pin 11) defaults to pull-down at boot, so a booting Pi
cannot hold the organ in reset by accident.
`tools/organ-config/organ_reset.py --pins 17` drives the line; with one pin
configured, "board 1" is the whole chain. The per-board wiring the tool
was written for (GPIOs 17, 27, 22, 23, an opto at each board's ISP header)
still works if independent resets are ever wanted.

A reset with wind on plays a scale, since the exercise routine fires every
coil. Set exerciseCycles to 0 over MIDI first if remote resets need to be
silent.

## The schematic includes an RS485 transceiver that shipped boards may not have

The schematic in this repository has **U3, an SN75LBC176D** RS485 transceiver at
roughly x=98 mm, y=182 mm on the sheet — bottom left, immediately right of the
RJ12 jacks J1 and J3. In it, the RJ12 pins 3 and 4 carry a differential pair
into U3, whose receiver output drives `MIDI_IN` and thence PD0.

Upstream's history contains *"Added RS485 transceiver, need to ship new rev"*,
and **boards in circulation appear not to have that revision.** If your board
has no 8-pin SOIC near the RJ12 jacks, the schematic here does not describe
your hardware's input path, and MIDI presumably reaches PD0 directly from the
connector.

Worth checking the physical board before reasoning from this schematic about
anything on the input side. Note also:

- Only the receiver half is wired even in this revision: `RE` is tied to GND,
  while `DE` (driver enable) and `D` (driver input) are both unconnected. So
  the board cannot transmit in either revision, which rules out any
  configuration scheme that needs acknowledgement or read-back.
- There is no termination and no idle bias on the A/B pair — only J1, J3 and
  U3 sit on those nets.
- There is **no optocoupler anywhere on the driver board**. Any MIDI input
  isolation is external to it.

## Opening the schematic in a modern KiCad

Three separate problems, all now addressed. If you are working from a fresh
clone you should not hit any of them.

### 1. The symbol cache was never committed

`midi-solenoid-driver.sch` opens with `LIBS:midi-solenoid-driver-cache`, but
that file is absent, so KiCad greets you with:

> The project symbol library cache file 'midi-solenoid-driver-cache.lib' was
> not found.

Upstream's `.gitignore` excluded `*-cache.lib` — a sensible rule for source
code and a damaging one for a KiCad 5 project, where the cache is how a
schematic stays self-contained. This fork no longer excludes it.

**Choose "Load Without Cache File".** Nothing is lost by doing so: every symbol
in this design comes from a stock KiCad library, so there are no custom symbols
that only existed in the cache. Then follow KiCad's own advice in that dialog
and save immediately.

### 2. Two symbols moved between KiCad 5 and KiCad 9

These caused the red `?` placeholders where the MOSFETs should be. Both
references have been updated in the schematic:

| Was (KiCad 5) | Now (KiCad 9) | Count |
|---|---|---|
| `Device:Q_NMOS_GDS` | `Transistor_FET:Q_NMOS_GDS` | 16 |
| `MCU_Microchip_ATmega:ATmega328PB-AU` | `MCU_Microchip_ATmega:ATmega328PB-A` | 1 |

`-A` is KiCad's TQFP-32 variant, which matches the board's
`Package_QFP:TQFP-32_7x7mm_P0.8mm` footprint. The *value* field still reads
`ATmega328PB-AU`, deliberately — that is the real Microchip order code, and
only the symbol reference needed changing.

Every other symbol in the design was verified present in KiCad 9 unchanged.

### 3. The project footprint library was never committed

Five footprints referenced the nickname `footprints:`, a project-local library
absent from upstream's repository. Three of them exist in stock KiCad 9
libraries under different nicknames, one has been renamed, and one is genuinely
custom and exists nowhere else:

| Footprint | Stock equivalent in KiCad 9 |
|---|---|
| `Fuseholder_Blade_Mini_Keystone_3568` | `Fuse:` — same name |
| `TerminalBlock_MetzConnect_Type701_RT11L02HGLU_1x02_P6.35mm_Horizontal` | `TerminalBlock_MetzConnect:` — same name |
| `TerminalBlock_Phoenix_PT-1,5-16-3.5-H_1x16_P3.50mm_Horizontal` | `TerminalBlock_Phoenix:` — same name |
| `RJ12_Amphenol_54601` | renamed to `Connector_RJ:RJ12_Amphenol_54601-x06_Horizontal` |
| `willemhillier.wordpress-logo` | **none — custom artwork** |

Rather than remap four references to lookalikes and lose the fifth, all five
were **recovered from `midi-solenoid-driver.kicad_pcb`**, which embeds complete
footprint definitions for everything placed on the board. They now live in
[`footprints.pretty/`](midi-solenoid-driver/footprints.pretty/) with an
`fp-lib-table` pointing at them, so the nickname resolves from a fresh clone
with nothing to install.

The recovered footprints are the ones actually used to manufacture this board,
not substitutes. Placement, net assignments and timestamps were stripped, and
references reset to `REF**`, as a library footprint requires. All five were
verified to parse and render under KiCad 9.

### What the command line cannot do

**KiCad 9's `kicad-cli` cannot read the legacy Eeschema format at all, and
fails silently:**

```
$ kicad-cli sch export pdf --output sch.pdf midi-solenoid-driver.sch
Plotted to 'sch.pdf'.
Done.
```

Exit status 0, a 271 KB file, and a completely blank page. A netlist export
from the same file returns `(components)` and `(nets)` both empty. Do not trust
the exit code — check the output. Use the GUI, which has the legacy importer
the CLI lacks.

The PCB is unaffected: `kicad-cli` reads `midi-solenoid-driver.kicad_pcb`
correctly, warning only that legacy zone fills are converted on a best-effort
basis.

### This is now done

The schematic has been opened in KiCad 9 and saved, producing
`midi-solenoid-driver.kicad_sch`. **That file is now the authoritative
schematic.** Because KiCad 6 and later embed symbol definitions directly in the
schematic, the project now carries its own symbols and this entire class of
problem cannot recur.

Verified after conversion, by exporting a netlist from the new file: 81
components and 82 nets, no unresolved symbols, all 16 MOSFETs resolving to
`Transistor_FET:Q_NMOS_GDS` and the MCU to `ATmega328PB-A`.

[`midi-solenoid-driver-schematic.pdf`](midi-solenoid-driver/midi-solenoid-driver-schematic.pdf)
is a rendering of it, committed so the schematic can be read without KiCad at
all.

The original legacy `midi-solenoid-driver.sch` is kept alongside it for
provenance. It is no longer maintained, and KiCad will not touch it again — if
you edit the schematic, edit the `.kicad_sch`.
