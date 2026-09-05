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

**5 A per bank** is the sensible value: comfortably above anything the music
does, fast to open on a real short from a modest supply, and at or under the
ampacity of the thinnest plausible wire. Step a bank to 7.5 A only after a
nuisance blow, which would take all eight coils attacking together at a much
higher pull-in duty than the default. Add one main fuse at the supply output
sized to the supply, so the feeds and the star ground are covered as well.

No fuse protects a coil that is stuck **on**: 1.7 A continuous through a
10%-duty solenoid trips nothing and simply cooks. That is the firmware
watchdog's job, and a supply relay's.

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
