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
