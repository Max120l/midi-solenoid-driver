// Firmware for the MIDI solenoid driver board, direct-MIDI variant.
//
// Upstream project: https://github.com/willemcvu/midi-solenoid-driver
// Original firmware and all hardware by Willem Hillier (MIT, 2019).
//
// What this variant changes, relative to firmware/midiSolenoidDriver:
//
//   1. Direct MIDI input. The original board expects 38400-baud MIDI-over-serial
//      from the companion USB interface board. That interface was never finished
//      upstream, so this firmware speaks standard 31250-baud MIDI on the same
//      RJ12 pin instead, using the Arduino MIDI Library. No interface board needed.
//
//   2. Peak-and-hold drive. Each solenoid is driven at full current for a short
//      pull-in window, then PWM'd down to a lower holding current. This lets the
//      solenoid pull in hard without cooking itself (or the driver) on long notes.
//
//   3. Three-switch note offset. The original reads a 7-bit DIP switch for the
//      lowest note and a separate analog DIP for MIDI channel. This board has
//      3 working switches, so the offset is coarse (16-note steps) and the
//      channel is fixed in software.
//
// Target: ATmega328PB. Verified working on hardware.

#include <MIDI.h>
#include <SoftwareSerial.h>

// --- MIDI input ---------------------------------------------------------
//
// SoftwareSerial in inverse-logic mode is used rather than the hardware UART.
// Arguments are (rxPin, txPin, inverseLogic): RX on pin 0, no TX (255), and
// inverted so the incoming MIDI signal is read the right way up. See README
// for the input wiring this expects.
SoftwareSerial invertedSerial(0, 255, true);
MIDI_CREATE_INSTANCE(SoftwareSerial, invertedSerial, MIDI);

// --- Hardware pin mapping -----------------------------------------------

// Digital output pins in sequential order, "OUTPUTS 1-16" on the silkscreen.
// Unchanged from the original firmware.
const int solenoidPins[16] = {
  3, 4, 23, 24, 20, 21, 5, 6,
  7, 8, 9, 10, 11, 12, 13, 25
};

// The three working switches of the large DIP block.
const int dipSwitchPins[3] = {
  15, 16, 17
};

const int numSolenoids = 16;
const int midiChannel = 1;   // 1-indexed, as the MIDI library expects

// Lowest MIDI note this board responds to. Set from the DIP switches in setup().
int baseNote = 0;

// --- Peak-and-hold engine ------------------------------------------------
//
// solenoidState[i] is one of:
//   0 = off
//   1 = peak   (full current, pulling in)
//   2 = hold   (PWM'd down to holding current)

byte solenoidState[numSolenoids];
unsigned long noteOnTime[numSolenoids];

const int peakDuration = 40;   // ms at full current before dropping to hold
const int pwmPeriod    = 2000; // us, hold PWM period (500 Hz)
const int pwmOnTime    = 800;  // us, hold PWM on-time (40% duty)

void handleNoteOff(byte channel, byte pitch, byte velocity);

void handleNoteOn(byte channel, byte pitch, byte velocity) {
  // Note-on with zero velocity is note-off by another name.
  if (velocity == 0) {
    handleNoteOff(channel, pitch, velocity);
    return;
  }

  if (pitch >= baseNote && pitch < baseNote + numSolenoids) {
    int i = pitch - baseNote;
    solenoidState[i] = 1;
    noteOnTime[i] = millis();
    digitalWrite(solenoidPins[i], HIGH);
  }
}

void handleNoteOff(byte channel, byte pitch, byte velocity) {
  if (pitch >= baseNote && pitch < baseNote + numSolenoids) {
    int i = pitch - baseNote;
    solenoidState[i] = 0;
    digitalWrite(solenoidPins[i], LOW);
  }
}

void setup() {
  for (int i = 0; i < numSolenoids; i++) {
    pinMode(solenoidPins[i], OUTPUT);
    digitalWrite(solenoidPins[i], LOW);
    solenoidState[i] = 0;
  }

  for (int i = 0; i < 3; i++) {
    pinMode(dipSwitchPins[i], INPUT_PULLUP);
  }

  // Switches are active-low. Three switches give a base note of 0 to 112
  // in steps of 16, i.e. one full board's worth of notes per step.
  if (!digitalRead(dipSwitchPins[0])) baseNote += 16;
  if (!digitalRead(dipSwitchPins[1])) baseNote += 32;
  if (!digitalRead(dipSwitchPins[2])) baseNote += 64;

  MIDI.begin(midiChannel);
  MIDI.setHandleNoteOn(handleNoteOn);
  MIDI.setHandleNoteOff(handleNoteOff);

  // Release the hardware UART entirely; MIDI comes in on SoftwareSerial and
  // an active hardware RX/TX on the same pin would fight it.
  UCSR0B &= ~((1 << RXEN0) | (1 << TXEN0));

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
  unsigned long cyclePosition = currentMicros % pwmPeriod;
  bool isPwmHigh = (cyclePosition < pwmOnTime);

  for (int i = 0; i < numSolenoids; i++) {
    if (solenoidState[i] == 1) {
      if (currentMillis - noteOnTime[i] >= peakDuration) {
        solenoidState[i] = 2;
      }
    }
    // Deliberately not an "else if": a solenoid that just finished its peak
    // window above should start holding on this same pass, not the next one.
    if (solenoidState[i] == 2) {
      if (isPwmHigh) {
        digitalWrite(solenoidPins[i], HIGH);
      } else {
        digitalWrite(solenoidPins[i], LOW);
      }
    }
  }
}
