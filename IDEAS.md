# Ideas and open questions

Parked work. Nothing here is committed to; it is written down so the reasoning
survives, not because it is decided.

## Power-up exercise vs. blower timing

The boot exercise routine takes about 3.8 s and is meant to run before wind is
applied. If the blower shares a switch with the electronics, the boards may
still be cycling as pressure comes up, producing a chromatic run through the
pipes. Harmless, but not necessarily wanted.

Options, cheapest first:

- Put the blower on its own switch and power the electronics first. No code.
- Add a configurable pre-delay before the routine, so the boards wait out the
  blower spin-up. Trivial, but guesses at a time constant.
- Trigger the exercise over MIDI instead of at boot, so the player decides when
  it happens. Needs the SysEx work below, but is the only version that is
  actually correct rather than approximately correct.

## Raspberry Pi 4 player with touchscreen

Intended as the MIDI source: playlists, transport, and instrument settings from
a touchscreen rather than from inside the organ.

**Playback engine.** `aplaymidi` already does the hard part — it plays a
Standard MIDI File to a hardware MIDI port with ALSA sequencer timing. A GUI
would be a wrapper around playlist management and transport rather than a MIDI
sequencer written from scratch. Linux is not realtime, but a few milliseconds
of jitter is inaudible on an organ.

**Getting MIDI out of the Pi.** Two routes:

- A USB MIDI interface. Simplest, definitely works, one more box.
- The Pi's own UART TX at 31250 baud, through a driver into the RJ12 chain.
  31250 is reachable on the PL011. Fewer parts, and it suits a permanent
  installation. Note the Pi is 3.3 V and will need a proper MIDI output stage.

**Worth checking before building anything:** whether an existing organ or
player-piano front end already does the playlist-and-transport job. This is a
well-trodden problem and writing a MIDI player GUI from scratch would be the
least interesting part of the project.

## Configuring the boards from the touchscreen

The appeal is obvious: no digging inside the organ for DIP switches.

### Why driving the switch pins from Pi GPIO does not work well

- **Boot ordering kills it.** The firmware reads its switches once, in
  `setup()`, a few milliseconds after power-up. The Pi takes 20-30 s to boot.
  The boards would read the GPIO in its power-on state -- all inputs, so all
  switches apparently open -- long before the Pi could assert anything. It
  would need the Pi to hold the boards in reset, or to switch their power,
  which is a lot of mechanism for the result.
- **It does not scale.** Seven pins per board, four boards, is 28 lines plus
  returns. That is essentially every usable GPIO on a Pi 4, and it means a
  parallel wiring harness alongside the RJ12 chain that already reaches every
  board.
- Levels are survivable but need care: drive the pins open-drain (output-low to
  close, high-impedance to open) and let the ATmega's own pull-ups do the rest.
  Never drive 3.3 V high into a 5 V input expecting a reliable logic 1.

### The better route: configure over the MIDI bus

The Pi already talks to every board over the RJ12 chain. Send configuration as
SysEx and store it in the ATmega's EEPROM. No new wiring, no boot-order
problem, and it persists across power cycles.

The chicken-and-egg is addressing: to send a message to one board, it needs an
identity, which is what the DIP switches currently provide. Options are to keep
the switches solely as a board ID and move everything else to SysEx, or to
assign IDs once during commissioning -- a button or jumper puts one board in
learn mode and it takes the next ID sent -- and store that in EEPROM too.

### What is actually worth making configurable

Worth separating two things that got conflated:

- **Base note and MIDI channel** are set once at installation and never touched
  again. The DIP switches are genuinely fine for this. Replacing them buys
  very little.
- **`peakDutyPercent`, `pwmOnTime`, `peakDuration`, `maxNoteDuration`** are the
  ones that want tuning against real valves, and they currently require
  editing the sketch and reflashing four boards. *That* is the real friction,
  and it is worth fixing whether or not the switches ever move.

So if this gets built, do the tuning parameters first and treat the switches as
a separate question that may not need answering at all.

## Resolved, kept for the record

- **Latching registers.** Worried the 10% duty rating meant registers could not
  be held open for a whole song. Moot: the register mechanism latches
  mechanically, so nothing is held powered. The stuck-note watchdog stays, and
  is now purely protective for those channels.
