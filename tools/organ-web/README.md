# organ-web

The organ's front desk: one web page served by the Pi, for a touchscreen on
the case and for any phone on the same Wi-Fi. It sits on top of the
[player](../player/) and owns everything around the music: the queue, the
library, saved playlists, uploads through the arranger, the pump, and the
service screens. The music itself stays in `grinder`, which this app runs
as a child in `--watch` mode. If the page or this app dies, the song keeps
playing.

```bash
pip install flask mido pyserial pyyaml          # plus gpiozero lgpio on the Pi for the pump and the reset
python organ_web.py --library ~/organ/tunes --organ ../organ-arranger/instrument/organ.yaml
```

Then open `http://<pi>:8080/` from anything on the network. On a laptop
with no organ, `--dry-run` plays to nowhere and the whole page works.

## How it hangs together

```
phone / touchscreen  ──HTTP──▶  organ_web.py  ──writes──▶  queue.m3u  ──read before each song──▶  grinder --watch  ──MIDI──▶  boards
                                     ▲                                                                 │
                                     └──────────────── reads status.json once a second ◀───────────────┘
```

- **The queue file** is the only thing the two processes share. The app
  writes the songs still to come, each with an `id=`; the player plays the
  first id it has not played and idles when none is left. Reordering is
  rewriting the file; the player notices before its next song. The page
  itself keeps the whole programme: what has played stays in the list,
  shaded, the current song is highlighted, and "Clear played" tidies up. A
  tap on any song moves the programme there: a played one plays again and
  the rest follow; one further down is jumped to, and what was passed over
  counts as played.
- **Skip** is a `SIGUSR1` to the player. **Stop** empties the file and skips,
  but keeps the queue in the app with the stopped song still at its head;
  **Play** writes the queue back with fresh ids, so that song starts again
  from the top and the rest follow. Songs added while nothing is playing
  wait the same way: the programme fills up and Play starts it; the ▶ on a
  tune, and "play instead" on a playlist, start at once. **Pause** is a `SIGUSR2`: the player lets
  the sounding notes end where they are written, silences the organ and
  waits; the same signal resumes from that position, registration first.
- **The pump relay and the power relay** belong to this app, not the
  player. A play request while they are off switches the supply on, then
  the pump, and holds the songs back for the warm-up, which also covers the
  boards booting; after the idle time-out with nothing queued the pump goes
  off and then the supply. The Service actions -- keys, reset, board
  settings -- switch the supply on first and ask for a second try a moment
  later, since the boards need a second to boot. Without `--pump` or
  `--power` the wind and the power are your business.
- **State** lives in `--state` (default `~/.local/share/organ-web`):
  `queue.m3u`, `status.json`, `settings.json`, `playlists/*.m3u`. The
  playlists are plain text in the player's own format, so a hand-written
  one works too.

## The pages

| Tab | What it does |
|---|---|
| **Play** | what is playing, with position; a vertical tempo rail (50–150 %, applied as the finger lifts, 100 to reset); pause and resume; skip; stop, which keeps the programme with the stopped song selected, and play, which starts it again from the top; the programme with played songs shaded and the current one highlighted, move up/down and remove for what is to come, queue-again for what has played; shuffle what is to come, clear, clear played; save the programme as a playlist; repeat |
| **Library** | the folders under `--library` as chips, the arranged tunes in each; ＋ queues one, ▶ plays it next; queue or shuffle a whole folder, or everything |
| **Playlists** | the saved lists: open, queue, queue shuffled, "play instead" (replaces the queue), delete |
| **Upload** | drop files that are already arranged into a library folder, existing or new; they are checked to be readable MIDI and stored as `name.organ.mid` |
| **Arrange** | drop a raw `.mid`: the transcriber runs on it, with a plan from the arranger's collection or automatically, then the arranger; the result lands in `library/uploads/` with its reports beside the source |
| **Service** | reset the boards; the 12 V supply and the pump on and off by hand; Shut down, which turns the pump and the supply off and then powers the Pi off, so the panel switch can be turned off safely once the screen is dark; the keys tester, by section or by board, with hold mode and the snare roll, usable when the player is idle |
| **Settings** | this screen's brightness and a screen time-out (per browser: set them on the case screen; the kiosk defaults to ten minutes, a phone to never; with a real backlight the Pi drives it, otherwise the page dims itself and "off" is a black screen the first touch wakes); the pause between songs, the pump's warm-up and idle time-out, the look and the sounds; and the driver boards' solenoid parameters (pull-in duty and window, hold duty, stuck-note watchdog, exercise passes), applied over the MIDI line as `organ_config` does, with save, reload and factory. The card starts locked and relocks after every send and after three minutes; the boards cannot be read back, so the fields show what was last sent from here, or this organ's usual values before anything has been |

The keys tester shares the serial line with the player, so it only answers
while the player is idle; the moment something is queued it closes.

Two looks, chosen under Settings and remembered per browser: **LCARS**, the
Enterprise-D panel (the default: black field, the elbow, coloured blocks
with their codes, pill controls, a stardate that means nothing), and
**brass and walnut**, the plain dark one. Same page, same buttons. In the
LCARS look every tap chirps, a lower note on the red buttons and a flat buzz
when a request is refused; the sounds are synthesised in the browser, so
there is no file to license, and Settings has a switch to silence them.

Tempo and pause changes apply to the songs not yet played. A playlist line
can still carry its own `tempo=` and `gap=`, which win over the settings.

## The API

Everything the page does is a plain HTTP call, so a different front end,
a script or a home-automation box can do the same.

| Method and path | Body | Does |
|---|---|---|
| `GET /api/state` | | status, queue, pending, pump, settings, player |
| `GET /api/library` | | tunes with folder and length |
| `POST /api/queue/add` | `{paths:[…], shuffle?, play_now?}` or `{path}` | queue tunes (library-relative paths) |
| `POST /api/queue/remove` `/move` `/clear` `/clear-played` `/shuffle` `/jump` | `{id}`, `{id,to}` | edit the programme; jump moves the cursor to a song |
| `POST /api/queue/save` | `{name}` | save the queue as a playlist |
| `POST /api/player/skip` `/stop` `/play` `/pause` | | transport: stop keeps the queue, play restarts it from its head, pause toggles |
| `POST /api/settings` | `{tempo?, gap?, repeat?, warm_up?, idle_off?, power_switch?}` | change settings |
| `GET /api/playlists`, `GET /PUT /DELETE /api/playlists/<name>` | `{entries:[{path,tempo?,gap?}]}` | playlists |
| `POST /api/playlists/<name>/queue` | `{shuffle?, replace?}` | queue a playlist |
| `GET /api/plans` | | the arranger's plans |
| `POST /api/upload` | multipart `file` (repeatable), `folder` | store arranged files in a library folder |
| `POST /api/arrange` | multipart `file`, `plan`, `transpose` | start an arrange job |
| `GET /api/jobs`, `GET /api/jobs/<id>` | | job state, output path, log |
| `GET /api/keys/layout`, `POST /api/keys/pulse` `/hold` `/roll` `/off` | `{solenoid, ms?}`, `{solenoid,on}`, `{solenoids?,interval_ms?,on}` | the keys tester |
| `POST /api/screen` | `{brightness?: 0-100, power?: on\|off}` | the display's backlight, where the Pi has one (`/sys/class/backlight`) |
| `POST /api/boards` | `{peak?, hold?, peak_ms?, max_note?, exercise?, command?: save\|reload\|factory, board?}` | solenoid parameters to the boards (idle only) |
| `POST /api/service/reset`, `POST /api/service/pump`, `POST /api/service/power` | , `{on}`, `{on}` | the boards' reset line; the pump and the supply by hand |
| `POST /api/service/shutdown` | | pump off, 12 V off, then `systemctl poweroff` |

Errors come back as `{"error": "…"}` with 400 (bad request), 404 (no such
tune, playlist or job) or 409 (the player is busy; no pump configured).

## Options

| Option | Default | |
|---|---|---|
| `--library DIR` | required | arranged tunes; subfolders are the library's sections; `uploads/` is created inside |
| `--organ FILE` | required | organ.yaml, for the keys tester's labels and the arranger |
| `--state DIR` | `~/.local/share/organ-web` | queue, status, settings, playlists |
| `--plans DIR` | `tools/organ-arranger/tunes` | the `*.plan.yaml` offered on upload |
| `--device DEV` | `/dev/serial0` | the MIDI line, passed to the player and used by the keys tester |
| `--python EXE` | this interpreter | runs the player and the arranger |
| `--pump GPIO`, `--pump-active-low` | none | the bellows pump relay |
| `--power GPIO`, `--power-active-low` | none | the organ's 12 V supply: an ATX supply's PS_ON pulled low through an opto; on before the pump, off after it, and switched on for the Service actions |
| `--power-switch GPIO` | none | a toggle switch between this GPIO and ground is the organ's on-off switch: opening it does what Shut down does; on GPIO 3 (pin 5) closing it also wakes a halted Pi |
| `--reset-pins 17` | `17` | the boards' reset line(s), for the Service page |
| `--host`, `--port` | `0.0.0.0`, `8080` | where to listen |
| `--dry-run` | | no serial port, no GPIO |

## As a service, and as a kiosk

[`organ-web.service`](organ-web.service) is a systemd unit: copy it to
`/etc/systemd/system/`, edit the user, paths and options at the top, then

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now organ-web
```

The player is a child of the service and restarts with it; the app also
restarts the player on its own if it ever exits.

The Shut down button needs the service's user allowed to power the Pi off
without a password. One line, as root, in a new file `/etc/sudoers.d/organ`:

```
massie ALL=(root) NOPASSWD: /bin/systemctl poweroff
```

Then a shutdown from the page takes the pump and the 12 V down first and
the Pi powers off within a few seconds; the panel switch is turned off once
the screen is dark.

Better than the button is a real switch: `--power-switch 3` watches a
toggle switch wired between GPIO 3 (physical pin 5) and any ground pin.
Opening it is the Shut down button; closing it again wakes the halted Pi,
which is a property of that pin and needs no software. Any switch will do,
since it carries microamps -- an antique 5 A one included. Should the old
contact start opening on its own, Settings has a tick box that makes the
app ignore it; closing it still wakes the Pi, since that is hardware. GPIO 3 is the
I²C clock, so leave I²C off in raspi-config, which it is by default. The
same sudoers line applies. Cutting the mains with the Pi running usually does no
harm, but a shutdown first is the habit that makes "usually" go away. `systemctl stop organ-web`
sends SIGTERM, which the player takes as a clean stop: the organ is silenced
and the pump switched off before anything exits.

For a touchscreen on the organ, the desktop session starts Chromium in kiosk
mode on the page. [`kiosk/organ-kiosk.sh`](kiosk/organ-kiosk.sh) waits until
the front desk answers and then runs the browser full screen, no address bar,
no tabs, no "restore session?" bubble; [`kiosk/organ-kiosk.desktop`](kiosk/organ-kiosk.desktop)
is the autostart entry that launches it. On Pi OS with the desktop (Bookworm,
Wayland or X11 alike):

```bash
mkdir -p ~/.config/autostart && cp kiosk/organ-kiosk.desktop ~/.config/autostart/ && chmod +x kiosk/organ-kiosk.sh
```

Edit the `Exec=` path in the copied `.desktop` file if the repo is not at
`/home/massie/organ`. Then in `sudo raspi-config`: *System
Options → Boot / Auto Login → Desktop Autologin*, and *Display Options →
Screen Blanking → off*, so the page stays lit. Reboot: the Pi comes up on the
Play tab with nothing else on the screen. To get a desktop back for
maintenance, plug in a keyboard and press Alt+F4, or SSH in and
`pkill chromium`. The page remembers its last tab, and every control is at
least 44 px, so it reflows for a phone in portrait and fills a 7-inch
screen in landscape.

A touchscreen on HDMI with USB touch needs no driver on Pi OS. If the
picture is upside down for the way it is mounted, rotate it in the desktop's
*Screen Configuration*; touch follows the rotation there.

## Tests

```bash
python -m pytest tests
```

The tests drive the desk with a fake player process, a fake pump, a fake
clock and a fake arranger, and the HTTP routes through Flask's test client.
No serial port, no GPIO and no real time.
