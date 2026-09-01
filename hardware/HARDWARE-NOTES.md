# Hardware notes

Findings from tracing the driver board's KiCad files, recorded because two of
them are easy to misdiagnose on the bench.

Everything here describes Willem Hillier's original board, unchanged. Nothing
in this fork modifies the hardware.

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
name is searchable).

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
that wants a physical switch has to go through `readSmallDip()`, and only when
the MIDI channel is not already using the block.

## The schematic will not open: legacy format, and a missing symbol cache

Two separate problems stacked on top of each other.

**1. The symbol cache was never committed.** `midi-solenoid-driver.sch` opens
with `LIBS:midi-solenoid-driver-cache`, but that file is absent. Upstream's
`.gitignore` excluded `*-cache.lib`, which is a reasonable rule for source
code and a damaging one for a KiCad 5 project, where the cache is how a
schematic stays self-contained. This fork's `.gitignore` no longer excludes it.

**2. The file is legacy Eeschema format** (`Schematic File Version 4`, KiCad 5)
and **KiCad 9's `kicad-cli` cannot read it.** This fails silently, which is the
part worth warning about:

```
$ kicad-cli sch export pdf --output sch.pdf midi-solenoid-driver.sch
Plotted to 'sch.pdf'.
Done.
```

Exit status 0, a 271 KB file, and a completely blank page. A netlist export
from the same file returns `(components)` and `(nets)` both empty. Do not trust
the exit code here; check the output.

**The PCB is unaffected.** KiCad 9 reads `midi-solenoid-driver.kicad_pcb`
correctly, warning only that legacy zone fills will be converted on a
best-effort basis.

### Getting the schematic back

Open the project in the **KiCad 9 GUI**, not the CLI. The GUI has the legacy
importer the command-line tool lacks, and will convert the schematic on open.
Because the cache is missing, it will resolve symbols against your installed
libraries instead — which works here, because every symbol in this design is a
stock KiCad symbol. There are no custom symbols to lose:

```
Connector:6P6C                      Device:R_Pack04
Connector:Screw_Terminal_01x02      Device:R_US
Connector:Screw_Terminal_01x16      Interface_UART:SN75LBC176D
Connector_Generic:Conn_02x03_Odd_Even   MCU_Microchip_ATmega:ATmega328PB-AU
Device:C                            Regulator_Switching:R-78B5.0-2.0
Device:D_Small                      Switch:SW_DIP_x04
Device:Fuse                         Switch:SW_DIP_x08
Device:LED_Small                    power:+12V
Device:Q_NMOS_GDS                   power:+5V
                                    power:GND
```

**Then save and commit the converted `.kicad_sch`.** KiCad 6 and later embed
symbol definitions directly in the schematic file, so once converted, the
project carries its own symbols and this class of problem cannot recur.
