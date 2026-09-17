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
  appends lines with an `id=`; the player plays the first id it has not
  played and idles when none is left. Reordering is rewriting the file;
  the player notices before its next song.
- **Skip** is a `SIGUSR1` to the player. **Stop** clears the file and skips.
  There is no pause: an organ has no way to hold its breath.
- **The pump relay** belongs to this app, not the player. A play request
  while the pump is off switches it on and holds the songs back for the
  warm-up; after the idle time-out with nothing queued the pump goes off.
  Without `--pump` the wind is your business and none of this happens.
- **State** lives in `--state` (default `~/.local/share/organ-web`):
  `queue.m3u`, `status.json`, `settings.json`, `playlists/*.m3u`. The
  playlists are plain text in the player's own format, so a hand-written
  one works too.

## The pages

| Tab | What it does |
|---|---|
| **Play** | what is playing, with position; skip, stop; the queue with move up/down and remove; shuffle and clear the queue; save it as a playlist; repeat |
| **Library** | the folders under `--library` as chips, the arranged tunes in each; ＋ queues one, ▶ plays it next; queue or shuffle a whole folder, or everything |
| **Playlists** | the saved lists: open, queue, queue shuffled, "play instead" (replaces the queue), delete |
| **Upload** | drop files that are already arranged into a library folder, existing or new; they are checked to be readable MIDI and stored as `name.organ.mid` |
| **Arrange** | drop a raw `.mid`: the transcriber runs on it, with a plan from the arranger's collection or automatically, then the arranger; the result lands in `library/uploads/` with its reports beside the source |
| **Service** | reset the boards; pump on and off by hand; the keys tester, by section or by board, with hold mode and the snare roll, usable when the player is idle |
| **Settings** | tempo for every song (50–150 %), the pause between songs, the pump's warm-up and idle time-out, the look and the sounds; and the driver boards' solenoid parameters (pull-in duty and window, hold duty, stuck-note watchdog, exercise passes), applied over the MIDI line as `organ_config` does, with save, reload and factory. The boards cannot be read back, so the fields show what was last sent from here |

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
| `POST /api/queue/remove` `/move` `/clear` `/shuffle` | `{id}`, `{id,to}` | edit the queue |
| `POST /api/queue/save` | `{name}` | save the queue as a playlist |
| `POST /api/player/skip` `/stop` | | transport |
| `POST /api/settings` | `{tempo?, gap?, repeat?, warm_up?, idle_off?}` | change settings |
| `GET /api/playlists`, `GET /PUT /DELETE /api/playlists/<name>` | `{entries:[{path,tempo?,gap?}]}` | playlists |
| `POST /api/playlists/<name>/queue` | `{shuffle?, replace?}` | queue a playlist |
| `GET /api/plans` | | the arranger's plans |
| `POST /api/upload` | multipart `file` (repeatable), `folder` | store arranged files in a library folder |
| `POST /api/arrange` | multipart `file`, `plan`, `transpose` | start an arrange job |
| `GET /api/jobs`, `GET /api/jobs/<id>` | | job state, output path, log |
| `GET /api/keys/layout`, `POST /api/keys/pulse` `/hold` `/roll` `/off` | `{solenoid, ms?}`, `{solenoid,on}`, `{solenoids?,interval_ms?,on}` | the keys tester |
| `POST /api/boards` | `{peak?, hold?, peak_ms?, max_note?, exercise?, command?: save\|reload\|factory, board?}` | solenoid parameters to the boards (idle only) |
| `POST /api/service/reset`, `POST /api/service/pump` | , `{on}` | the boards' reset line; the pump by hand |

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
restarts the player on its own if it ever exits. `systemctl stop organ-web`
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
