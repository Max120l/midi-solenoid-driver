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
//   2. Peak-and-hold drive. Each solenoid is driven hard for a short pull-in
//      window, then PWM'd down to a lower holding current, so coils survive
//      sustained notes. The pull-in itself can also be PWM'd, for solenoids
//      overspecced for what they move -- see peakDutyPercent. Both phases are
//      phase-staggered across the 16 channels to spread the load on the
//      supply, and daisy-chained boards offset their stagger from each other.
//
//   3. Selectable MIDI channel: fixed in firmware, read from the small DIP
//      switch block as upstream did, or omni (respond on every channel).
//
//   4. Optional velocity response, scaling the pull-in time.
//
//   5. Defensive handling of badly-formed MIDI. See "Robustness" below.
//
//   6. Runtime configuration over MIDI. The tuning parameters -- pull-in duty
//      and duration, hold duty, watchdog, exercise passes -- are set by control
//      changes on a dedicated channel and can be saved to EEPROM, so a board
//      built into an instrument never needs reflashing to retune it.
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
#include <EEPROM.h>

// ===================== Configuration =====================

// --- Where the lowest-note offset comes from ---
//
// OFFSET_FULL7   uses all seven switches of the large DIP block, in 1-note
//                steps, exactly as upstream did. This is the correct setting
//                for undamaged hardware, and the one to pick if you are
//                building this from scratch.
// OFFSET_COARSE3 uses only switches 1-3, in 16-note steps, for a board whose
//                remaining four switches are dead. One board's worth of notes
//                per step, so a chain still addresses the full keyboard, just
//                not at arbitrary offsets.
#define OFFSET_FULL7   0
#define OFFSET_COARSE3 1

// Defaults to COARSE3 because the boards this fork is maintained against have
// a damaged large DIP block with only three working switches. If your switches
// are all good, change this to OFFSET_FULL7 -- nothing else needs to change.
#define NOTE_OFFSET_MODE OFFSET_COARSE3

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

// --- MIDI input polarity ---
//
// MIDI_INPUT_NORMAL   Hardware UART. Expects a conventional non-inverted MIDI
//                     signal: idle high, standard UART polarity. This is what
//                     a correctly wired optocoupler front end produces, and it
//                     is the setting you want in the end.
//
// MIDI_INPUT_INVERTED SoftwareSerial in inverse-logic mode, for a front end
//                     that delivers the signal upside down. The AVR's USART
//                     has no receive-invert bit, so inverting in firmware
//                     means giving up the hardware UART entirely.
//
// Treat INVERTED as a bench workaround, not a destination. SoftwareSerial
// receives each byte by busy-waiting through it with interrupts disabled --
// about 320 us at 31250 baud, and at 8 MHz that is a quarter of the CPU's
// entire budget for that period. A byte arriving during another byte is lost,
// which is exactly what dense MIDI produces, and loop() stalls meanwhile so
// the hold PWM gets rougher too. Fine for testing a few notes. Not what you
// want under a whole chest.
//
// If MIDI does not work at all and the wiring is otherwise sound, this is the
// first thing to try.
#define MIDI_INPUT_NORMAL   0
#define MIDI_INPUT_INVERTED 1

#define MIDI_INPUT_POLARITY MIDI_INPUT_NORMAL

// --- Diagnostics ---
//
// For when nothing happens and you need to know which of three filters is
// eating the input: the framing, the channel, or the note range.
//
// DIAGNOSTIC_RAW_BYTES  Every byte arriving on the MIDI port clicks output 1,
//                       bypassing the MIDI parser, the channel filter and the
//                       note range completely. Nothing else runs.
//                         clicks  -> bytes arrive and frame correctly, so the
//                                    fault is downstream: channel or note range
//                         silent  -> baud, polarity, or wiring. Nothing is
//                                    getting in at all.
//
// DIAGNOSTIC_ANY_NOTE   Listens on every channel, and any note-on clicks
//                       output 1 whatever its pitch. Normal playing still
//                       works alongside it.
//                         clicks but no valve -> note range. Check the DIP
//                                    switches: with all of them open baseNote
//                                    is 0, so the board answers only notes
//                                    0-15 and a keyboard sending 60 is ignored.
//                         nothing -> channel filter was not the problem either
//
// Work downwards: RAW_BYTES first, since it makes the fewest assumptions.
#define DIAGNOSTIC_OFF       0
#define DIAGNOSTIC_RAW_BYTES 1
#define DIAGNOSTIC_ANY_NOTE  2

#define DIAGNOSTIC_MODE DIAGNOSTIC_OFF

// --- Tunable settings: compiled defaults, adjustable at runtime over MIDI ---
//
// These are the values you tune against the instrument. They start at the
// compiled defaults below, can be changed while running with control-change
// messages (see "Runtime configuration over MIDI" next), and can be saved to
// EEPROM so a board comes up with its tuned values after a power cycle.
// Nothing here needs a reflash to change.
//
//   peakDutyPercent  How hard to drive the pull-in, 1-100. 100 is solid DC.
//                    Lower it when the solenoids are overspecced for their
//                    load, which is common: a 22 W actuator seating a small
//                    valve does not need 22 W, and at 100% it announces the
//                    fact by slamming. The single most effective lever on
//                    supply current, since pull-in is the only time every
//                    energised channel draws full current at once. Too low
//                    and notes silently fail to sound under load.
//   holdDutyPercent  PWM duty once pulled in, 0-HOLD_DUTY_MAX_PERCENT. What
//                    keeps a duty-limited coil inside its thermal budget on a
//                    long note. 25% is 0.87 W on a 7 ohm coil.
//   peakDurationMs   Pull-in window, 1-PEAK_DURATION_MAX_MS. Long enough for
//                    the plunger to seat under real load, and no longer.
//   maxNoteSeconds   Stuck-note watchdog, 0-127 s: force-releases anything
//                    held longer. 0 disables it, after which a malformed file
//                    can leave a valve open until power-cycle. 30 s outlasts
//                    almost any real note.
//   exerciseCycles   Power-up valve exercise passes, 0-EXERCISE_CYCLES_MAX.
//                    See exerciseValves() for why. 0 disables it and the
//                    short boot heartbeat runs instead.
struct Settings {
  byte peakDutyPercent;
  byte holdDutyPercent;
  byte peakDurationMs;
  byte maxNoteSeconds;
  byte exerciseCycles;
};

const Settings factoryDefaults = {
  100,   // peakDutyPercent
  25,    // holdDutyPercent   (500 us of a 2000 us period)
  40,    // peakDurationMs
  30,    // maxNoteSeconds
  2,     // exerciseCycles
};

// Hard ceilings the runtime path cannot exceed, whatever it is sent. These
// protect the hardware from a fat-fingered value: the boards cannot transmit,
// so they cannot say "that would cook the coils" -- they can only refuse.
// Raise them only for solenoids rated for it.
const byte HOLD_DUTY_MAX_PERCENT = 40;   // 2.74 W on a 7 ohm coil: 133% of a 10% ED budget
const byte PEAK_DURATION_MAX_MS  = 127;  // a CC value is 7 bits; a larger ceiling would be unreachable over MIDI
const byte EXERCISE_CYCLES_MAX   = 10;

const int exerciseOnTime  = 60;  // ms energised, per output, per pass
const int exerciseOffTime = 60;  // ms released, so the valve can reseat

// --- Runtime configuration over MIDI ---
//
// Control changes on CONFIG_CHANNEL adjust the settings above while the board
// runs. Music arrives on the note channel; configuration lives on a channel of
// its own so a stray CC inside a song cannot retune the organ. (Files from
// organ-arranger contain no CCs at all.)
//
//   CC 20  select board     0-112: only the board whose base note equals the
//                           value listens to what follows. 113-127: all
//                           boards. Reverts to all after a quiet
//                           CONFIG_SELECT_TIMEOUT_MS, so a forgotten selection
//                           cannot strand a board.
//   CC 21  peakDutyPercent  clamped to 1-100
//   CC 22  holdDutyPercent  clamped to 0-HOLD_DUTY_MAX_PERCENT
//   CC 23  peakDurationMs   clamped to 1-PEAK_DURATION_MAX_MS
//   CC 24  maxNoteSeconds   0 disables the watchdog
//   CC 25  exerciseCycles   clamped to 0-EXERCISE_CYCLES_MAX
//   CC 26  command          1 = save to EEPROM, acknowledged by a click on
//                               output 1 if that output is idle
//                           2 = reload from EEPROM
//                           3 = factory defaults, in RAM; save to keep them
//
// Changes take effect immediately, including on notes already held, which is
// what makes tuning by ear work: drag the hold duty while a chord sounds.
// Every value, from MIDI or from EEPROM, passes through the same clamps.
//
// The Pi-side tool for this is tools/organ-config. Set CONFIG_ENABLED to 0 to
// compile all of it out.
#define CONFIG_ENABLED 1
#define CONFIG_CHANNEL 16
#define CONFIG_ACK     1     // click output 1 on save; 0 for silence

#define CC_SELECT_BOARD    20
#define CC_PEAK_DUTY       21
#define CC_HOLD_DUTY       22
#define CC_PEAK_DURATION   23
#define CC_MAX_NOTE        24
#define CC_EXERCISE_CYCLES 25
#define CC_COMMAND         26

#define CMD_SAVE    1
#define CMD_RELOAD  2
#define CMD_FACTORY 3

const unsigned long CONFIG_SELECT_TIMEOUT_MS = 60000UL;

// ===================== MIDI input =====================
//
// Normally the hardware UART: interrupt-driven and buffered, so back-to-back
// bytes are fine. See MIDI_INPUT_POLARITY above for why the inverted variant
// has to give that up.
#if MIDI_INPUT_POLARITY == MIDI_INPUT_INVERTED
  #include <SoftwareSerial.h>
  // (rxPin, txPin, inverseLogic) -- RX on pin 0, no transmit, inverted.
  SoftwareSerial invertedSerial(0, 255, true);
  MIDI_CREATE_INSTANCE(SoftwareSerial, invertedSerial, MIDI);
  #define MIDI_PORT invertedSerial
#else
  MIDI_CREATE_INSTANCE(HardwareSerial, Serial, MIDI);
  #define MIDI_PORT Serial
#endif

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

const int peakDurationMin = 15;   // ms at the lowest velocity, when scaling
const int pwmPeriod       = 2000; // us, PWM period for both phases (500 Hz)

// Live values, derived from `settings` by applySettings(). The hot path reads
// these rather than the struct so nothing is recomputed per pass. The
// initialisers only have to be sane; applySettings() runs before loop() does.
Settings      settings        = factoryDefaults;
byte          peakDurationMax = 40;        // ms of pull-in drive, and at full velocity
unsigned int  peakOnTime      = pwmPeriod; // us; equal to pwmPeriod means solid DC
unsigned int  pwmOnTime       = 500;       // us, hold PWM on-time
unsigned long maxNoteDuration = 30000UL;   // ms; 0 means the watchdog is off
byte          exerciseCycles  = 2;

void clampSettings(Settings &s) {
  if (s.peakDutyPercent < 1)                   s.peakDutyPercent = 1;
  if (s.peakDutyPercent > 100)                 s.peakDutyPercent = 100;
  if (s.holdDutyPercent > HOLD_DUTY_MAX_PERCENT) s.holdDutyPercent = HOLD_DUTY_MAX_PERCENT;
  if (s.peakDurationMs < 1)                    s.peakDurationMs = 1;
  if (s.peakDurationMs > PEAK_DURATION_MAX_MS) s.peakDurationMs = PEAK_DURATION_MAX_MS;
  if (s.exerciseCycles > EXERCISE_CYCLES_MAX)  s.exerciseCycles = EXERCISE_CYCLES_MAX;
  // maxNoteSeconds: anything 0-127 is legitimate, 0 meaning off.
}

void applySettings() {
  clampSettings(settings);
  peakDurationMax = settings.peakDurationMs;
  peakOnTime      = (unsigned int)(((unsigned long)pwmPeriod * settings.peakDutyPercent) / 100);
  pwmOnTime       = (unsigned int)(((unsigned long)pwmPeriod * settings.holdDutyPercent) / 100);
  maxNoteDuration = (unsigned long)settings.maxNoteSeconds * 1000UL;
  exerciseCycles  = settings.exerciseCycles;
}

// --- EEPROM persistence ---
//
// A small record with a magic number, a layout version and a checksum, so a
// blank or stale EEPROM is recognised and ignored rather than trusted. Written
// only on an explicit save, never on every change: EEPROM wears out, and live
// tuning would otherwise chew through it.
struct StoredSettings {
  uint16_t magic;
  uint8_t  version;
  Settings s;
  uint8_t  checksum;
};
const uint16_t SETTINGS_MAGIC   = 0x0C9A;
const uint8_t  SETTINGS_VERSION = 1;
const int      SETTINGS_ADDRESS = 0;

uint8_t settingsChecksum(const Settings &s) {
  const uint8_t *p = (const uint8_t *)&s;
  uint8_t sum = 0x5A;
  for (size_t i = 0; i < sizeof(Settings); i++) sum = (uint8_t)(sum * 31 + p[i]);
  return sum;
}

// Applies the stored settings and returns true if EEPROM holds a valid record.
bool loadSettings() {
  StoredSettings st;
  EEPROM.get(SETTINGS_ADDRESS, st);
  if (st.magic != SETTINGS_MAGIC || st.version != SETTINGS_VERSION) return false;
  if (settingsChecksum(st.s) != st.checksum) return false;
  settings = st.s;
  applySettings();
  return true;
}

void saveSettings() {
  StoredSettings st;
  st.magic    = SETTINGS_MAGIC;
  st.version  = SETTINGS_VERSION;
  st.s        = settings;
  st.checksum = settingsChecksum(settings);
  EEPROM.put(SETTINGS_ADDRESS, st);   // put() rewrites only the bytes that changed
}

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

// Each channel's fixed offset into the PWM cycle, precomputed in setup().
//
// This exists for speed, not clarity. The board runs from the internal 8 MHz
// oscillator, and a 32-bit modulo per channel per pass -- sixteen of them --
// costs more than everything else in loop() put together. Precomputing the
// offsets lets the hot path use 16-bit arithmetic and a single conditional
// subtraction instead, which keeps the loop period well under the 125 us phase
// step it is trying to resolve. Without it the stagger is washed out by the
// loop being slower than the thing it is staggering.
unsigned int channelPhase[numSolenoids];

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

// Power-up valve exercise: fire every output in turn, at full power.
//
// The point is to break any adhesion between valve and seat that built up
// while the instrument sat closed -- "pluck" -- so the first note of the
// session behaves like the hundredth. On a lightly loaded valve, pluck can be
// the largest single force the solenoid has to overcome, and it is worst on
// the first cycle after a rest. Run before wind is applied: unpressurised, the
// valves move freely and no pipe speaks.
//
// Sequential rather than all at once, deliberately. One solenoid at a time
// draws a sixteenth of the current, and you hear each output fire in order, so
// a dead channel or a swapped connector announces itself. It doubles as a
// power-on self test. Full power regardless of peakDutyPercent, since breaking
// stiction is precisely the job that wants maximum force.
//
// Total time is cycles x 16 x (on + off): about 3.8 s at the defaults.
// Blocking on purpose: this runs once in setup(), before anything else.
void exerciseValves() {
  for (int c = 0; c < exerciseCycles; c++) {
    for (int i = 0; i < numSolenoids; i++) {
      digitalWrite(solenoidPins[i], HIGH);
      delay(exerciseOnTime);
      digitalWrite(solenoidPins[i], LOW);
      delay(exerciseOffTime);
    }
  }
}

// ===================== MIDI handlers =====================

void handleNoteOff(byte channel, byte pitch, byte velocity);

// The library is opened omni so that configuration can arrive on a channel of
// its own; notes are filtered here against the channel this board is set to.
byte noteChannel = 1;   // 1-16, or MIDI_CHANNEL_OMNI; assigned in setup()

bool forThisBoard(byte channel) {
  return noteChannel == MIDI_CHANNEL_OMNI || channel == noteChannel;
}

void handleNoteOn(byte channel, byte pitch, byte velocity) {
  if (!forThisBoard(channel)) return;

  // Note-on with zero velocity is note-off by another name.
  if (velocity == 0) {
    handleNoteOff(channel, pitch, velocity);
    return;
  }

#if DIAGNOSTIC_MODE == DIAGNOSTIC_ANY_NOTE
  // Any note at all, on any channel, whatever its pitch.
  digitalWrite(solenoidPins[0], HIGH);
  delay(15);
  digitalWrite(solenoidPins[0], LOW);
#endif

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
  if (!forThisBoard(channel)) return;
  if (pitch >= baseNote && pitch < baseNote + numSolenoids) {
    releaseSolenoid(pitch - baseNote);
  }
}

#if CONFIG_ENABLED
// -1: every board listens. Otherwise only the board with this base note does.
int selectedBase = -1;
unsigned long lastConfigMs = 0;

void acknowledgeSave() {
#if CONFIG_ACK
  // One short click on output 1, provided nothing is playing on it. Under
  // wind that is a brief chirp from the board's lowest pipe -- and four boards
  // saving together answer as a four-note chord, which is as good a "done" as
  // hardware that cannot transmit can give you.
  if (solenoidState[0] != 0) return;
  digitalWrite(solenoidPins[0], HIGH);
  delay(60);
  digitalWrite(solenoidPins[0], LOW);
#endif
}

void handleConfig(byte number, byte value) {
  lastConfigMs = millis();

  if (number == CC_SELECT_BOARD) {
    selectedBase = (value <= 112) ? (int)value : -1;
    return;
  }
  if (selectedBase >= 0 && selectedBase != baseNote) return;   // addressed to another board

  switch (number) {
    case CC_PEAK_DUTY:       settings.peakDutyPercent = value; break;
    case CC_HOLD_DUTY:       settings.holdDutyPercent = value; break;
    case CC_PEAK_DURATION:   settings.peakDurationMs  = value; break;
    case CC_MAX_NOTE:        settings.maxNoteSeconds  = value; break;
    case CC_EXERCISE_CYCLES: settings.exerciseCycles  = value; break;
    case CC_COMMAND:
      if (value == CMD_SAVE) {
        applySettings();            // clamp first, so what is saved is what runs
        saveSettings();
        acknowledgeSave();
      } else if (value == CMD_RELOAD) {
        if (!loadSettings()) settings = factoryDefaults;
      } else if (value == CMD_FACTORY) {
        settings = factoryDefaults;
      }
      break;
    default:
      return;                       // not a configuration CC
  }
  applySettings();
}
#endif

void handleControlChange(byte channel, byte number, byte value) {
  // 120 = All Sound Off, 123 = All Notes Off. Both mean "release everything",
  // and honouring them is the difference between a clean stop and a stuck
  // valve when a sequencer stops mid-phrase. Accepted on any channel at all:
  // a panic must never be filtered out.
  if (number == 120 || number == 123) {
    releaseAll();
    return;
  }
#if CONFIG_ENABLED
  if (channel == CONFIG_CHANNEL) handleConfig(number, value);
#endif
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
#if DIAGNOSTIC_MODE == DIAGNOSTIC_ANY_NOTE
  return MIDI_CHANNEL_OMNI;   // diagnostics override the configured channel
#elif CHANNEL_SOURCE == CHANNEL_OMNI
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

  // Opened omni so configuration can arrive on CONFIG_CHANNEL; the note
  // handlers filter against noteChannel themselves.
  noteChannel = selectedChannel();
  MIDI.begin(MIDI_CHANNEL_OMNI);
  MIDI.setHandleNoteOn(handleNoteOn);
  MIDI.setHandleNoteOff(handleNoteOff);
  MIDI.setHandleControlChange(handleControlChange);
  MIDI.setHandleStop(handleStop);
  MIDI.setHandleSystemReset(handleSystemReset);

  // MIDI Thru is on by default in the library, which would echo everything
  // straight back out. This board is a leaf node on the bus, so turn it off.
  MIDI.turnThruOff();

  // Several boards share the RJ12 line and none of them should be able to
  // drive it, so the UART transmitter is always disabled. Upstream does the
  // same thing for the same reason. This also frees pin 1 to be read as a DIP
  // input below, so it has to happen before the switches are set up.
#if MIDI_INPUT_POLARITY == MIDI_INPUT_INVERTED
  // SoftwareSerial owns pin 0 in this mode, so the hardware receiver has to go
  // too or the two fight over the same pin.
  UCSR0B &= ~((1 << RXEN0) | (1 << TXEN0));
#else
  UCSR0B &= ~(1 << TXEN0);
#endif

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

  for (int i = 0; i < numSolenoids; i++) {
    channelPhase[i] =
      (unsigned int)(((unsigned long)boardPhaseOffset
                      + (unsigned long)i * pwmPhaseStep) % pwmPeriod);
  }

  // Tuned values from EEPROM where a valid record exists, else the compiled
  // defaults. Before the exercise routine, since exerciseCycles is one of them.
  settings = factoryDefaults;
  if (!loadSettings()) applySettings();

  if (exerciseCycles > 0) {
    exerciseValves();
    // The UART has been unattended for several seconds. Discard whatever
    // arrived, so a fragment of a message that began before we started cannot
    // be parsed as a note the moment the loop opens.
    while (MIDI_PORT.available()) MIDI_PORT.read();
  } else {
    // Boot heartbeat: one short click on output 1, so you can hear that the
    // board came up without needing to send it anything.
    digitalWrite(solenoidPins[0], HIGH);
    delay(150);
    digitalWrite(solenoidPins[0], LOW);
  }
}

void loop() {
#if DIAGNOSTIC_MODE == DIAGNOSTIC_RAW_BYTES
  // Deliberately crude: no parsing, no filtering, nothing else running. If a
  // byte reaches this pin at the right baud, you hear it.
  while (MIDI_PORT.available()) {
    MIDI_PORT.read();
    digitalWrite(solenoidPins[0], HIGH);
    delay(15);
    digitalWrite(solenoidPins[0], LOW);
    delay(35);
  }
  return;
#endif

  MIDI.read();

  unsigned long currentMillis = millis();

#if CONFIG_ENABLED
  // A forgotten board selection must not strand a board: back to "all" after
  // a quiet minute.
  if (selectedBase >= 0 && currentMillis - lastConfigMs >= CONFIG_SELECT_TIMEOUT_MS) {
    selectedBase = -1;
  }
#endif
  // The one 32-bit modulo per pass. Everything below stays 16-bit.
  unsigned int cycleBase = (unsigned int)(micros() % pwmPeriod);

  for (int i = 0; i < numSolenoids; i++) {
    if (solenoidState[i] == 0) continue;

    // Stuck-note watchdog. Unsigned subtraction, so millis() rollover after
    // ~49 days is handled correctly.
    if (maxNoteDuration > 0 && currentMillis - noteOnTime[i] >= maxNoteDuration) {
      releaseSolenoid(i);
      continue;
    }

    // Shift this channel's place in the PWM cycle by its precomputed offset,
    // staggering the 16 channels' on-times across the period rather than
    // stacking them. Both the peak and the hold phase use it.
    //
    // Both terms are below pwmPeriod, so their sum is below twice it and one
    // conditional subtraction replaces a modulo.
    unsigned int cyclePosition = cycleBase + channelPhase[i];
    if (cyclePosition >= pwmPeriod) cyclePosition -= pwmPeriod;

    if (solenoidState[i] == 1) {
      if (currentMillis - noteOnTime[i] >= notePeakDuration[i]) {
        solenoidState[i] = 2;
        // Deliberately no "else" on the state-2 block below: a solenoid that
        // just finished pulling in should start holding on this same pass.
      } else if (peakOnTime >= pwmPeriod) {
        digitalWrite(solenoidPins[i], HIGH);   // 100%, solid DC
      } else {
        digitalWrite(solenoidPins[i], (cyclePosition < peakOnTime) ? HIGH : LOW);
      }
    }
    if (solenoidState[i] == 2) {
      digitalWrite(solenoidPins[i], (cyclePosition < pwmOnTime) ? HIGH : LOW);
    }
  }
}
