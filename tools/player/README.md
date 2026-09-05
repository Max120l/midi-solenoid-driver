# player

The playback engine: the Raspberry Pi's UART, at 31250 baud into a
current-loop driver and an optocoupler, is a MIDI output, and `grinder.py`
writes an arranged song down it with `mido` doing the tempo arithmetic.

```bash
pip install mido pyserial
python grinder.py song.organ.mid --device /dev/serial0
```

Play files that have been through [`organ_arranger`](../organ-arranger/):
single track, channel 1, every note a driver-board slot. Ctrl+C stops the
song **and sends All Notes Off**, so a note sounding at that moment does not
hang until the boards' stuck-note watchdog reaches it.

This is deliberately the whole of it. A playlist, a screen, a phone across
the room and uploads that run the arranger — all of that belongs in a layer
that talks to an engine like this one, so the music keeps playing if the UI
does not.

## The Pi's serial port

`/dev/serial0` transmits on GPIO14, **physical pin 8**. On a Pi 4 with
Bluetooth at its default it is the mini-UART, which works at 31250 because
`enable_uart=1` pins the core clock its baud rate is derived from. For an
appliance with no use for onboard Bluetooth, give it the real PL011 on the
same pin instead — exact baud, bigger FIFO — with, in
`/boot/firmware/config.txt`:

```
enable_uart=1
dtoverlay=disable-bt
```

then `sudo systemctl disable hciuart` and a reboot. Nothing here changes.

The same device works for [`organ-config`](../organ-config/):

```bash
python ../organ-config/organ_config.py --serial /dev/serial0 --peak 60 --hold 25 --save
```
