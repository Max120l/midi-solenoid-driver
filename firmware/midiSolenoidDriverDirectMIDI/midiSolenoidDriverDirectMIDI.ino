// Firmware for the MIDI solenoid driver board, direct-MIDI variant.
//
// Upstream project: https://github.com/willemcvu/midi-solenoid-driver
// Original firmware and all hardware by Willem Hillier (MIT, 2019).
//
// What this variant changes, relative to firmware/midiSolenoidDriver:
//
//   1. Direct MIDI input. The original board expects 38400-baud MIDI-over-serial
//      from the companion USB interface board. That interface was never finished
//      upstream, so this firmware decodes standard 31250-baud MIDI on the same
//      RJ12 pin using the Arduino MIDI Library. No interface board needed.
//
//   2. Peak-and-hold drive. Each solenoid is driven at full current for a short
//      pull-in window, then PWM'd down to a lower holding current, so coils
//      survive sustained notes. Each channel's hold PWM is phase-staggered to
//      spread the load on the supply, and daisy-chained boards offset their
//      stagger from each other.
//
//   3. Selectable MIDI channel: fixed in firmware, read from the small DIP
//      switch block as upstream did, or omni (respond on every channel).
//
//   4. Optional velocity response, scaling the pull-in time.
//
//   5. Defensive handling of badly-formed MIDI. See "Robustness" below.
//
// Target: ATmega328PB. MIDI arrives on the hardware UART (pin 0, RXD0), which
// is the same pin upstream used. The transmitter is disabled so this board can
// never drive the shared RJ12 bus.
//
// Robustness
// ----------
// Sequencer output is not always clean, and a solenoid left energised is worse
// than a missed note: it drones, it heats the coil, and it heats the driver.
// Everything here fails toward "off".
//
//   - Note-on with velocity 0 is treated as note-off, per the MIDI spec. This
//     is how most sequencers express note-off, and how a note "removed" by
//     zeroing its velocity arrives.
//   - A repeated note-on for a note already sounding re-articulates it rather
//     than stacking. A single note-off then releases it. This is deliberately
//     NOT reference-counted: if a file sends two note-ons and only one
//     note-off, counting would hold the valve open forever, whereas
//     last-off-wins simply releases it.
//   - CC 120 (All Sound Off) and CC 123 (All Notes Off) release everything.
//     Sequencers emit these on stop, on panic, and at end of file.
//   - MIDI Stop and System Reset release everything.
//   - A watchdog force-releases any solenoid held longer than maxNoteDuration,
//     which catches a note-off that was never sent, or a cable pulled mid-note.
//   - Running status, interleaved real-time bytes and unknown message types are
//     handled by the MIDI library rather than by hand. Upstream's byte-by-byte
//     parser mishandled running status, which is common in tightly-packed files.

#include <MIDI.h>

// ===================== Configuration =====================

// --- Where the lowest-note offset comes from ---
//
// OFFSET_FULL7   uses all seven switches of the large DIP block, in 1-note
//                steps, exactly as upstream did. This is the correct setting
//                for undamaged hardware.
// OFFSET_COARSE3 uses only switches 1-3, in 16-note steps, for a board whose
//                remaining four switches are dead. One board's worth of notes
//                per step, so a chain still addresses the full keyboard.
#define OFFSET_FULL7   0
#define OFFSET_COARSE3 1

#define NOTE_OFFSET_MODE OFFSET_FULL7

// --- Where the MIDI channel comes from ---
#define CHANNEL_FIXED 0  // use fixedChannel below
#define CHANNEL_DIP   1  // read the 4-bit small DIP block, as upstream did
#define CHANNEL_OMNI  2  // respond on every channel

#define CHANNEL_SOURCE CHANNEL_FIXED

// Used only when CHANNEL_SOURCE is CHANNEL_FIXED. 1-16.
const byte fixedChannel = 1;

// --- Velocity response ---
//
// VELOCITY_OFF      every note gets the full pull-in time. Correct for organ
//                   valves, which are open or shut and have no dynamics.
// VELOCITY_ON       pull-in time scales with velocity, for struck instruments.
// VELOCITY_SMALLDIP read one switch of the small DIP block at boot. Available
//                   whenever the channel is not also coming from that block,
//                   which makes it the usual way to put velocity on a switch.
// VELOCITY_SWITCH   read velocitySwitchPin on the large DIP block at boot.
//                   Needs a switch the note offset is not using, so it
//                   requires OFFSET_COARSE3.
//
// Both switch modes are active-low: closed enables velocity.
#define VELOCITY_OFF      0
#define VELOCITY_ON       1
#define VELOCITY_SWITCH   2
#define VELOCITY_SMALLDIP 3

#define VELOCITY_SOURCE VELOCITY_OFF

// Switch 4 of the large DIP block, for VELOCITY_SWITCH. Unused when
// NOTE_OFFSET_MODE is OFFSET_COARSE3; part of the note offset when it is
// OFFSET_FULL7.
const int velocitySwitchPin = 18;

// Which bit of the small DIP block to read for VELOCITY_SMALLDIP, 0-3.
const int velocitySmallDipBit = 0;

// --- Configuration sanity checks ---
// Catch switch assignments that collide, at compile time rather than by
// mystifying behaviour on the bench.
#if (VELOCITY_SOURCE == VELOCITY_SWITCH) && (NOTE_OFFSET_MODE == OFFSET_FULL7)
#error "VELOCITY_SWITCH needs large-DIP switch 4, which OFFSET_FULL7 uses for the note offset. Use VELOCITY_SMALLDIP, or switch to OFFSET_COARSE3."
#endif
#if (VELOCITY_SOURCE == VELOCITY_SMALLDIP) && (CHANNEL_SOURCE == CHANNEL_DIP)
#error "VELOCITY_SMALLDIP and CHANNEL_DIP both want the small DIP block. Pick one: use a fixed channel, or put velocity on VELOCITY_SWITCH."
#endif

// --- Stuck-note watchdog ---
// Force-release any solenoid held longer than this, in milliseconds. Guards
// against a note-off that never arrives. Set to 0 to disable, but be aware
// that disabling it means a malformed file can leave a valve open until the
// board is power-cycled. 30 s is longer than almost any real note; raise it if
// your music genuinely holds notes longer than that.
const unsigned long maxNoteDuration = 30000UL;

// ===================== MIDI input =====================
//
// The hardware UART, not SoftwareSerial. SoftwareSerial receives a byte by
// busy-waiting through it with interrupts disabled, roughly 320 us at 31250
// baud, during which nothing else runs -- so a byte arriving during another
// byte is simply lost. That is exactly the failure mode dense MIDI provokes.
// The hardware UART is interrupt-driven and buffered, so back-to-back bytes
// are fine.
MIDI_CREATE_INSTANCE(HardwareSerial, Serial, MIDI);

// ===================== Hardware pin mapping =====================

// Digital output pins in sequential order, "OUTPUTS 1-16" on the silkscreen.
// Unchanged from the original firmware.
const int solenoidPins[16] = {
  3, 4, 23, 24, 20, 21, 5, 6,
  7, 8, 9, 10, 11, 12, 13, 25
};

// Large DIP block, switch 1 first. Note that switches 6 and 7 sit on pins 1
// and 2; pin 1 is the UART transmit pin, which is why the transmitter must be
// disabled before these are read.
#if NOTE_OFFSET_MODE == OFFSET_FULL7
const int noteOffsetPins[7] = { 15, 16, 17, 18, 19, 1, 2 };
const int noteOffsetWeights[7] = { 1, 2, 4, 8, 16, 32, 64 };
const int numOffsetSwitches = 7;
#else
const int noteOffsetPins[3] = { 15, 16, 17 };
const int noteOffsetWeights[3] = { 16, 32, 64 };
const int numOffsetSwitches = 3;
#endif

const int numSolenoids = 16;

// Lowest MIDI note this board responds to. Set from the DIP switches in setup().
int baseNote = 0;

// Whether velocity scaling is active. Resolved once at boot.
bool velocityEnabled = false;

// ===================== Peak-and-hold engine =====================
//
// solenoidState[i] is one of:
//   0 = off
//   1 = peak   (full current, pulling in)
//   2 = hold   (PWM'd down to holding current)

byte solenoidState[numSolenoids];
unsigned long noteOnTime[numSolenoids];
byte notePeakDuration[numSolenoids];

const int peakDurationMax = 40; // ms at full current, and at full velocity
const int peakDurationMin = 15; // ms at the lowest velocity, when scaling
const int pwmPeriod       = 2000; // us, hold PWM period (500 Hz)
const int pwmOnTime       = 800;  // us, hold PWM on-time (40% duty)

// Each channel's hold PWM is offset in time from the one before it, so that
// simultaneously-held solenoids do not all switch on at the same instant.
// Spreading 16 channels at 40% duty across the period means the supply sees
// roughly 7 coils' worth of hold current at any moment instead of all 16,
// without changing any individual coil's duty cycle or holding force.
const int pwmPhaseStep = pwmPeriod / numSolenoids; // us, 125 us per channel

// Boards in a chain run identical firmware, so they all stagger to the same
// pattern. Derived from the note offset, which is different on every board in
// the chain, this shifts each board's pattern relative to the others.
//
// Be realistic about what it buys you: the boards run from independent
// crystals and boot at different moments, so their PWM cycles already drift
// relative to one another and would rarely be in lockstep anyway. The offset
// is also smaller than one pass of loop(). Treat it as a free nudge, not as
// load management -- for a four-board chain, size the supply for the worst
// case rather than trusting this.
int boardPhaseOffset = 0;

// ===================== Solenoid control =====================

void releaseSolenoid(int i) {
  solenoidState[i] = 0;
  digitalWrite(solenoidPins[i], LOW);
}

void releaseAll() {
  for (int i = 0; i < numSolenoids; i++) {
    releaseSolenoid(i);
  }
}

// ===================== MIDI handlers =====================

void handleNoteOff(byte channel, byte pitch, byte velocity);

void handleNoteOn(byte channel, byte pitch, byte velocity) {
  // Note-on with zero velocity is note-off by another name.
  if (velocity == 0) {
    handleNoteOff(channel, pitch, velocity);
    return;
  }

  if (pitch >= baseNote && pitch < baseNote + numSolenoids) {
    int i = pitch - baseNote;

    // Louder notes get a longer pull-in, which hits harder on a struck
    // instrument. Valves have no dynamics, so this is off by default.
    if (velocityEnabled) {
      notePeakDuration[i] = map(velocity, 1, 127, peakDurationMin, peakDurationMax);
    } else {
      notePeakDuration[i] = peakDurationMax;
    }

    // Restart the peak window even if this note is already sounding, so a
    // repeated note re-articulates rather than being ignored.
    solenoidState[i] = 1;
    noteOnTime[i] = millis();
    digitalWrite(solenoidPins[i], HIGH);
  }
}

void handleNoteOff(byte channel, byte pitch, byte velocity) {
  if (pitch >= baseNote && pitch < baseNote + numSolenoids) {
    releaseSolenoid(pitch - baseNote);
  }
}

void handleControlChange(byte channel, byte number, byte value) {
  // 120 = All Sound Off, 123 = All Notes Off. Both mean "release everything",
  // and honouring them is the difference between a clean stop and a stuck
  // valve when a sequencer stops mid-phrase.
  if (number == 120 || number == 123) {
    releaseAll();
  }
}

void handleStop() {
  releaseAll();
}

void handleSystemReset() {
  releaseAll();
}

// ===================== DIP switch reading =====================

#if (CHANNEL_SOURCE == CHANNEL_DIP) || (VELOCITY_SOURCE == VELOCITY_SMALLDIP)
// The small DIP block is not wired to four digital pins; it is read as two
// analog voltages, two switches per pin. Ported unchanged from upstream's
// firmware, which is the authority on these thresholds.
boolean readSmallDip(int bitNo) {
  boolean bit0 = false, bit1 = false, bit2 = false, bit3 = false;

  int adc_bit01_val = analogRead(A0);
  int adc_bit23_val = analogRead(A7);

  if (adc_bit01_val > 750) {
    bit0 = false; bit1 = false;
  } else if (adc_bit01_val > 550) {
    bit0 = false; bit1 = true;
  } else if (adc_bit01_val > 480) {
    bit0 = true;  bit1 = false;
  } else {
    bit0 = true;  bit1 = true;
  }

  if (adc_bit23_val > 750) {
    bit2 = false; bit3 = false;
  } else if (adc_bit23_val > 550) {
    bit2 = false; bit3 = true;
  } else if (adc_bit23_val > 480) {
    bit2 = true;  bit3 = false;
  } else {
    bit2 = true;  bit3 = true;
  }

  if (bitNo == 0) return bit0;
  if (bitNo == 1) return bit1;
  if (bitNo == 2) return bit2;
  if (bitNo == 3) return bit3;
  return false;
}
#endif

// Returns the channel to pass to MIDI.begin(): 1-16, or MIDI_CHANNEL_OMNI.
byte selectedChannel() {
#if CHANNEL_SOURCE == CHANNEL_OMNI
  return MIDI_CHANNEL_OMNI;
#elif CHANNEL_SOURCE == CHANNEL_DIP
  // Upstream's small-DIP mapping produces a 0-indexed channel; the MIDI
  // library wants 1-16, hence the +1.
  byte channel = 0;
  if (readSmallDip(3)) channel += 1;
  if (readSmallDip(2)) channel += 2;
  if (readSmallDip(1)) channel += 4;
  if (readSmallDip(0)) channel += 8;
  return channel + 1;
#else
  return fixedChannel;
#endif
}

// ===================== Setup and main loop =====================

void setup() {
  for (int i = 0; i < numSolenoids; i++) {
    pinMode(solenoidPins[i], OUTPUT);
    digitalWrite(solenoidPins[i], LOW);
    solenoidState[i] = 0;
    notePeakDuration[i] = peakDurationMax;
  }

  MIDI.begin(selectedChannel());
  MIDI.setHandleNoteOn(handleNoteOn);
  MIDI.setHandleNoteOff(handleNoteOff);
  MIDI.setHandleControlChange(handleControlChange);
  MIDI.setHandleStop(handleStop);
  MIDI.setHandleSystemReset(handleSystemReset);

  // MIDI Thru is on by default in the library, which would echo everything
  // straight back out. This board is a leaf node on the bus, so turn it off.
  MIDI.turnThruOff();

  // Disable only the UART transmitter, keeping the receiver. Several boards
  // share the RJ12 line and none of them should be able to drive it. Upstream
  // does the same thing for the same reason. This also frees pin 1 to be read
  // as a DIP input below, so it has to happen before the switches are set up.
  UCSR0B &= ~(1 << TXEN0);

  // Switches are active-low.
  for (int i = 0; i < numOffsetSwitches; i++) {
    pinMode(noteOffsetPins[i], INPUT_PULLUP);
  }
  for (int i = 0; i < numOffsetSwitches; i++) {
    if (!digitalRead(noteOffsetPins[i])) baseNote += noteOffsetWeights[i];
  }

#if VELOCITY_SOURCE == VELOCITY_ON
  velocityEnabled = true;
#elif VELOCITY_SOURCE == VELOCITY_SWITCH
  pinMode(velocitySwitchPin, INPUT_PULLUP);
  velocityEnabled = !digitalRead(velocitySwitchPin);
#elif VELOCITY_SOURCE == VELOCITY_SMALLDIP
  velocityEnabled = readSmallDip(velocitySmallDipBit);
#else
  velocityEnabled = false;
#endif

  // Give each board in a chain a different starting phase, a quarter of a
  // channel step apart, so four boards interleave rather than align.
  boardPhaseOffset = (baseNote / numSolenoids) * (pwmPhaseStep / 4);

  // Boot heartbeat: one short click on output 1, so you can hear that the
  // board came up without needing to send it anything.
  digitalWrite(solenoidPins[0], HIGH);
  delay(150);
  digitalWrite(solenoidPins[0], LOW);
}

void loop() {
  MIDI.read();

  unsigned long currentMillis = millis();
  unsigned long currentMicros = micros();
  unsigned long cycleBase = currentMicros % pwmPeriod;

  for (int i = 0; i < numSolenoids; i++) {
    if (solenoidState[i] == 0) continue;

    // Stuck-note watchdog. Unsigned subtraction, so millis() rollover after
    // ~49 days is handled correctly.
    if (maxNoteDuration > 0 && currentMillis - noteOnTime[i] >= maxNoteDuration) {
      releaseSolenoid(i);
      continue;
    }

    if (solenoidState[i] == 1) {
      if (currentMillis - noteOnTime[i] >= notePeakDuration[i]) {
        solenoidState[i] = 2;
      }
    }
    // Deliberately not an "else if": a solenoid that just finished its peak
    // window above should start holding on this same pass, not the next one.
    if (solenoidState[i] == 2) {
      // Shift this channel's place in the PWM cycle by its index, staggering
      // the 16 channels' on-times across the period rather than stacking them.
      unsigned long cyclePosition =
        (cycleBase + boardPhaseOffset + (unsigned long)i * pwmPhaseStep) % pwmPeriod;
      if (cyclePosition < pwmOnTime) {
        digitalWrite(solenoidPins[i], HIGH);
      } else {
        digitalWrite(solenoidPins[i], LOW);
      }
    }
  }
}
