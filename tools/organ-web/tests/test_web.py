"""organ_web: the desk (queue, pump, settings, playlists, jobs, keys) and the HTTP routes,
with a fake player process, a fake pump, a fake clock and a fake arranger."""
import io
import json
import sys
from pathlib import Path

import mido
import pytest

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import organ_web as w  # noqa: E402
import grinder as g  # noqa: E402

ORGAN = HERE.parent / "organ-arranger" / "instrument" / "organ.yaml"


def song_file(path: Path, seconds=2.0, note=40):
    path.parent.mkdir(parents=True, exist_ok=True)
    mid = mido.MidiFile(type=0, ticks_per_beat=480)
    tr = mido.MidiTrack()
    tr.append(mido.MetaMessage("set_tempo", tempo=500_000, time=0))
    tr.append(mido.Message("note_on", note=note, velocity=100, time=0))
    tr.append(mido.Message("note_off", note=note, velocity=0, time=int(seconds * 2 * 480)))
    tr.append(mido.MetaMessage("end_of_track", time=0))
    mid.tracks.append(tr)
    mid.save(str(path))
    return path


class FakeProc:
    def __init__(self, cmd):
        self.cmd = cmd
        self.pid = 4242
        self.signals = []
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def send_signal(self, s):
        self.signals.append(s)

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def desk(tmp_path):
    lib = tmp_path / "lib"
    song_file(lib / "waltzes" / "skaters.organ.mid", 3.0)
    song_file(lib / "waltzes" / "danube.organ.mid", 2.0)
    song_file(lib / "marches" / "bogey.organ.mid", 1.0)
    song_file(lib / "waltzes" / "skaters.mid", 1.0)                 # a source file, not organ format
    cfg = w.Config(library=lib, organ=ORGAN, state=tmp_path / "state", dry_run=True)
    clock = Clock()
    procs = []

    def spawn(cmd):
        p = FakeProc(cmd)
        procs.append(p)
        return p

    runs = []

    def run_cmd(cmd):
        runs.append(cmd)
        # the fake transcriber and arranger: write the output named after -o
        out = Path(cmd[cmd.index("-o") + 1])
        if "fail" in out.name:
            return 1, "boom"
        song_file(out, 1.0)
        return 0, f"ok {out.name}"

    d = w.Desk(cfg, now=clock, spawn=spawn, pump=w.FakePump(), run_cmd=run_cmd, open_port=lambda: g.NullPort())
    d.player.skip_signal = 10
    d.clock, d.procs, d.runs = clock, procs, runs
    return d


def status(d, **fields):
    """What the player would write -- dated after the desk's last queue write, as a real report is."""
    import time
    d.cfg.status_file.write_text(json.dumps({"time": time.time() + 1, "pid": 4242, **fields}), encoding="utf-8")


def test_a_stale_idle_report_does_not_clear_a_freshly_written_queue(desk):
    desk.pump = None
    desk.add(["marches/bogey.organ.mid"])
    desk.cfg.status_file.write_text(json.dumps({"time": desk.last_write - 0.5, "state": "idle"}), encoding="utf-8")
    desk.housekeep()
    assert [e["name"] for e in desk.queue] == ["bogey"]           # the player has not seen the file yet
    status(desk, state="idle")
    desk.housekeep()
    assert desk.queue == []                                       # this one it wrote after reading it


def queue_lines(d):
    return [ln for ln in d.cfg.queue_file.read_text(encoding="utf-8").splitlines() if ln and not ln.startswith("#")]


# ----------------------------------------------------------------------------
# library and queue
# ----------------------------------------------------------------------------

def test_library_lists_arranged_tunes_by_folder_with_lengths(desk):
    tunes = desk.library.tunes()
    assert [(t["folder"], t["name"]) for t in tunes] == [("marches", "bogey"), ("waltzes", "danube"), ("waltzes", "skaters")]
    assert tunes[2]["length_s"] == 3.0 and tunes[0]["path"] == "marches/bogey.organ.mid"
    assert desk.library.folders() == ["marches", "waltzes"]
    with pytest.raises(ValueError):
        desk.library.resolve("../outside.mid")


def test_adding_starts_the_pump_and_holds_the_songs_for_the_warm_up(desk):
    desk.set_settings({"warm_up": 8, "tempo": "90%", "gap": 4})
    added = desk.add(["waltzes/skaters.organ.mid", "marches/bogey.organ.mid"])
    assert [e["id"] for e in added] == [1, 2] and desk.pump_on
    assert desk.pending and not desk.queue and queue_lines(desk) == []
    desk.housekeep()
    assert desk.pending                                          # still warming up
    desk.clock.t += 8
    desk.housekeep()
    assert not desk.pending and [e["id"] for e in desk.queue] == [1, 2]
    lines = queue_lines(desk)
    assert lines[0].endswith("skaters.organ.mid | id=1 tempo=0.900 gap=4") and "bogey" in lines[1]
    assert desk.procs and "--watch" in desk.procs[0].cmd and "--dry-run" in desk.procs[0].cmd


def test_without_a_pump_songs_go_straight_to_the_file(desk):
    desk.pump = None
    desk.add(["marches/bogey.organ.mid"])
    assert queue_lines(desk) == [f"{desk.library.root / 'marches' / 'bogey.organ.mid'} | id=1 tempo=1.000 gap=3"]


def test_housekeeping_prunes_what_the_player_reports_as_done(desk):
    desk.pump = None
    desk.add(["marches/bogey.organ.mid", "waltzes/danube.organ.mid", "waltzes/skaters.organ.mid"])
    status(desk, state="playing", id=2, song="danube")
    desk.housekeep()
    assert [e["id"] for e in desk.queue] == [2, 3] and desk.upcoming()[0]["now"] is True
    status(desk, state="played", id=2)
    desk.housekeep()
    assert [e["id"] for e in desk.queue] == [3]
    status(desk, state="idle", queue=1)
    desk.housekeep()
    assert desk.queue == [] and desk.snapshot()["idle"] is True


def test_play_now_goes_right_after_the_current_song_and_skips_it(desk):
    desk.pump = None
    desk.add(["marches/bogey.organ.mid", "waltzes/danube.organ.mid"])
    status(desk, state="playing", id=1)
    desk.housekeep()
    desk.add(["waltzes/skaters.organ.mid"], play_now=True)
    assert [e["name"] for e in desk.queue] == ["bogey", "skaters", "danube"]
    assert desk.procs[0].signals == [10]
    desk.move(2, 5)
    assert [e["name"] for e in desk.queue] == ["bogey", "skaters", "danube"] or [e["name"] for e in desk.queue][0] == "bogey"
    desk.move(3, 0)                                              # cannot go above the playing song
    assert [e["name"] for e in desk.queue][0] == "bogey"
    desk.remove(3)
    assert [e["name"] for e in desk.queue] == ["bogey", "danube"]
    # stop: the song ends, the queue stays with it at the head, the player gets an empty file
    desk.stop()
    assert desk.held and [e["name"] for e in desk.queue] == ["bogey", "danube"] and desk.procs[0].signals == [10, 10]
    assert queue_lines(desk) == [] and desk.upcoming()[0]["now"] is True
    status(desk, state="skipped", id=1)
    desk.housekeep()
    status(desk, state="idle", queue=0)
    desk.housekeep()
    assert [e["name"] for e in desk.queue] == ["bogey", "danube"]         # the player's reports do not touch a held queue
    assert desk.snapshot()["idle"] is True and desk.snapshot()["held"] is True
    desk.keys_act("pulse", {"solenoid": 1}); desk.keys.close()          # the wire is free while stopped
    # play: fresh ids, from the head
    desk.play()
    assert not desk.held and [e["id"] for e in desk.queue] == [4, 5]
    assert [ln.split("| ")[1].split()[0] for ln in queue_lines(desk)] == ["id=4", "id=5"]
    # pause is a signal, only while something plays
    desk.player.pause_signal = 12
    assert desk.pause() is False
    status(desk, state="playing", id=4)
    desk.housekeep()
    assert desk.pause() is True and desk.procs[0].signals[-1] == 12
    status(desk, state="paused", id=4, position_s=12.0)
    desk.housekeep()
    assert desk.busy() and desk.pause() is True


def test_settings_reach_the_lines_not_yet_played_and_bad_values_are_refused(desk):
    desk.pump = None
    desk.add(["marches/bogey.organ.mid"])
    desk.set_settings({"tempo": 1.2, "gap": 1, "repeat": True})
    assert queue_lines(desk)[0].endswith("id=1 tempo=1.200 gap=1")
    assert json.loads(desk.cfg.settings_file.read_text())["repeat"] is True
    with pytest.raises(ValueError):
        desk.set_settings({"tempo": "300%"})
    with pytest.raises(ValueError):
        desk.set_settings({"gap": -2})


def test_repeat_queues_the_round_again_when_the_player_goes_idle(desk):
    desk.pump = None
    desk.set_settings({"repeat": True})
    desk.add(["marches/bogey.organ.mid", "waltzes/danube.organ.mid"])
    status(desk, state="idle", queue=2)
    desk.housekeep()
    assert [(e["id"], e["name"]) for e in desk.queue] == [(3, "bogey"), (4, "danube")]
    desk.set_settings({"repeat": False})
    status(desk, state="idle", queue=2)
    desk.housekeep()
    assert desk.queue == []


def test_the_pump_goes_off_after_the_idle_time_out(desk):
    desk.set_settings({"warm_up": 1, "idle_off": 60})
    desk.add(["marches/bogey.organ.mid"])
    desk.clock.t += 1
    desk.housekeep()
    status(desk, state="playing", id=1)
    desk.housekeep()
    status(desk, state="idle")
    desk.housekeep()
    assert desk.pump_on
    desk.clock.t += 59
    desk.housekeep()
    assert desk.pump_on
    desk.clock.t += 2
    desk.housekeep()
    assert not desk.pump_on
    desk.pump_set(True)
    assert desk.snapshot()["pump"] == {"configured": True, "on": True}


# ----------------------------------------------------------------------------
# playlists, jobs, keys
# ----------------------------------------------------------------------------

def test_playlists_round_trip_with_per_entry_settings(desk):
    desk.pump = None
    desk.write_playlist("Sunday", [{"path": "waltzes/skaters.organ.mid", "tempo": 0.9},
                                   {"path": "marches/bogey.organ.mid", "gap": 6},
                                   {"path": "waltzes/gone.organ.mid"}])
    assert desk.playlists() == [{"name": "Sunday", "count": 3}]
    entries = desk.read_playlist("Sunday")
    assert entries[0]["tempo"] == 0.9 and entries[1]["gap"] == 6 and entries[2]["missing"] is True
    added = desk.queue_playlist("Sunday")
    assert [e["name"] for e in added] == ["skaters", "bogey"]
    assert queue_lines(desk)[0].endswith("tempo=0.900 gap=3") and queue_lines(desk)[1].endswith("tempo=1.000 gap=6")
    desk.write_playlist("From queue", [{"path": e["path"], "tempo": e["tempo"], "gap": e["gap"]} for e in desk.queue])
    assert [p["name"] for p in desk.playlists()] == ["From queue", "Sunday"]
    desk.delete_playlist("Sunday")
    assert [p["name"] for p in desk.playlists()] == ["From queue"]
    with pytest.raises(FileNotFoundError):
        desk.read_playlist("Sunday")
    assert desk.playlist_path("../evil").parent == desk.cfg.playlists      # names cannot leave the folder
    with pytest.raises(ValueError):
        desk.write_playlist("   ", [])


def test_arrange_runs_the_transcriber_then_the_arranger_into_uploads(desk):
    job_id = desk.start_arrange("My Tune.mid", b"MThd", plan="skaters-waltz", transpose="auto", background=False)
    job = desk.jobs[job_id]
    assert job["state"] == "done" and job["output"] == "uploads/my-tune.organ.mid"
    assert (desk.cfg.uploads / "my-tune.organ.mid").is_file()
    t, a = desk.runs
    assert t[1].endswith("organ_transcribe.py") and "--plan" in t and t[t.index("--plan") + 1].endswith("skaters-waltz.plan.yaml")
    assert a[1].endswith("organ_arranger.py") and a[2].endswith("my-tune.fororgan.mid")
    assert "my-tune" in [x["name"] for x in desk.library.tunes()]
    # no plan: the transcriber writes one; a transposition is passed through
    job2 = desk.jobs[desk.start_arrange("other.mid", b"MThd", plan=None, transpose="-2", background=False)]
    t2 = desk.runs[2]
    assert job2["state"] == "done" and "--write-plan" in t2 and t2[t2.index("--transpose") + 1] == "-2"
    # a failure is reported with the log
    job3 = desk.jobs[desk.start_arrange("fail.mid", b"MThd", plan=None, transpose=None, background=False)]
    assert job3["state"] == "failed" and "boom" in job3["log"]
    # an already arranged file just lands in uploads
    job4 = desk.jobs[desk.start_arrange("done.organ.mid", b"MThd", plan=None, transpose=None, background=False)]
    assert job4["state"] == "done" and job4["output"] == "uploads/done.organ.mid"
    assert "skaters-waltz" in desk.plans()


def test_keys_layout_and_actions_and_the_busy_guard(desk):
    lay = desk.keys.layout()
    assert [s["name"] for s in lay["sections"]] == ["Melody", "Accompainment", "Base", "TenorCM", "TrebCM", "Drums", "Registers"]
    assert len(lay["boards"]) == 4 and lay["labels"]["1"] == "Trom B+" and lay["snare"] == [55, 56]
    r = desk.keys_act("pulse", {"solenoid": 41, "ms": 5000})
    assert r["sounding"] == [41] and desk.snapshot()["keys_open"] is True
    r = desk.keys_act("roll", {"interval_ms": 100})
    assert r["rolling"] == [55, 56]
    r = desk.keys_act("off", {})
    assert r == {"sounding": [], "rolling": []}
    desk.pump = None
    desk.add(["marches/bogey.organ.mid"])
    with pytest.raises(RuntimeError):
        desk.keys_act("pulse", {"solenoid": 1})
    desk.housekeep()
    assert desk.snapshot()["keys_open"] is False               # closed because the player has work
    desk.keys.close()


def test_the_player_process_is_restarted_after_it_dies(desk):
    desk.housekeep()
    assert len(desk.procs) == 1
    desk.procs[0].alive = False
    desk.housekeep()
    assert len(desk.procs) == 1                                  # not within the restart delay
    desk.clock.t += 3
    desk.housekeep()
    assert len(desk.procs) == 2 and desk.snapshot()["player"]["running"]
    desk.close()
    assert not desk.procs[1].alive and not desk.pump_on


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

@pytest.fixture
def client(desk):
    desk.pump = None
    app = w.create_app(desk)
    app.config["TESTING"] = True
    return app.test_client()


def test_routes_cover_the_desk(client, desk):
    r = client.get("/api/state")
    assert r.status_code == 200 and r.get_json()["settings"]["gap"] == 3.0
    assert r.get_json()["counts"] == {"tunes": 3, "folders": 2, "by_folder": {"marches": 1, "waltzes": 2},
                                      "playlists": 0, "jobs_running": 0, "jobs_done": 0}
    lib = client.get("/api/library").get_json()
    assert len(lib["tunes"]) == 3 and lib["folders"] == ["marches", "waltzes"]
    r = client.post("/api/queue/add", json={"paths": ["marches/bogey.organ.mid", "waltzes/danube.organ.mid"]})
    assert r.status_code == 200 and [e["name"] for e in r.get_json()["added"]] == ["bogey", "danube"]
    assert client.post("/api/queue/add", json={}).status_code == 400
    assert client.post("/api/queue/add", json={"path": "nope.mid"}).status_code == 404
    r = client.post("/api/queue/move", json={"id": 2, "to": 0})
    assert [e["name"] for e in r.get_json()["queue"]] == ["danube", "bogey"]
    r = client.post("/api/settings", json={"tempo": "80%"})
    assert r.get_json()["tempo"] == 0.8
    assert client.post("/api/settings", json={"tempo": "0"}).status_code == 400
    r = client.post("/api/queue/save", json={"name": "Tonight"})
    assert r.get_json()["playlists"] == [{"name": "Tonight", "count": 2}]
    r = client.get("/api/playlists/Tonight")
    assert [e["name"] for e in r.get_json()["entries"]] == ["danube", "bogey"]
    assert client.get("/api/playlists/Nope").status_code == 404
    r = client.put("/api/playlists/Two", json={"entries": [{"path": "marches/bogey.organ.mid"}]})
    assert r.status_code == 200
    r = client.post("/api/playlists/Two/queue", json={"replace": True})
    assert [e["name"] for e in r.get_json()["added"]] == ["bogey"] and [e["name"] for e in desk.queue] == ["bogey"]
    assert client.delete("/api/playlists/Two").get_json()["playlists"] == [{"name": "Tonight", "count": 2}]
    r = client.post("/api/queue/clear")
    assert r.get_json()["queue"] == []
    assert client.post("/api/player/skip").get_json() == {"skipped": False}     # nothing running yet
    r = client.post("/api/service/pump", json={"on": True})
    assert r.status_code == 409
    assert client.post("/api/service/reset").get_json()["reset"] == "dry run"
    assert client.get("/api/plans").get_json()["plans"][:2] == ["africa", "bourree"]
    lay = client.get("/api/keys/layout").get_json()
    assert len(lay["sections"]) == 7
    r = client.post("/api/keys/pulse", json={"solenoid": 3, "ms": 20})
    assert r.get_json()["sounding"] == [3]
    client.post("/api/keys/off")
    desk.keys.close()
    assert client.get("/").status_code == 200 and b"<title>" in client.get("/").data


def test_upload_puts_a_finished_file_in_a_folder_and_refuses_junk(client, desk, tmp_path):
    good = song_file(tmp_path / "x.mid", 1.5).read_bytes()
    data = {"file": (io.BytesIO(good), "Skaters Waltz.organ.mid"), "folder": "waltzes"}
    r = client.post("/api/upload", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    up = r.get_json()["uploaded"][0]
    assert up["path"] == "waltzes/Skaters Waltz.organ.mid" and up["length_s"] == 1.5
    assert (desk.cfg.library / "waltzes" / "Skaters Waltz.organ.mid").is_file()
    data = {"file": (io.BytesIO(good), "new.mid"), "folder": "christmas"}      # a new folder, a plain .mid name
    up = client.post("/api/upload", data=data, content_type="multipart/form-data").get_json()["uploaded"][0]
    assert up["path"] == "christmas/new.organ.mid" and "christmas" in desk.library.folders()
    r = client.post("/api/upload", data={"file": (io.BytesIO(b"not midi"), "junk.mid")}, content_type="multipart/form-data")
    assert r.status_code == 400 and "not a MIDI file" in r.get_json()["error"]
    assert not (desk.cfg.uploads / "junk.organ.mid").exists()
    r = client.post("/api/upload", data={"file": (io.BytesIO(good), "x.mid"), "folder": "../out"}, content_type="multipart/form-data")
    assert r.status_code == 400
    assert client.post("/api/upload", data={}, content_type="multipart/form-data").status_code == 400


def test_board_parameters_go_out_as_organ_config_ccs_and_are_remembered(client, desk):
    r = client.post("/api/boards", json={"peak": 60, "hold": 25, "peak_ms": 40})
    assert r.status_code == 200
    d = r.get_json()
    assert d["sent"][0].endswith("all boards") and any("peak duty" in s and s.split()[-1] == "60" for s in d["sent"])
    assert d["warnings"] == [] and d["boards"]["peak"] == 60 and "sent_at" in d["boards"] and "saved_at" not in d["boards"]
    assert json.loads(desk.cfg.settings_file.read_text())["boards"]["hold"] == 25
    # save: the command goes last, and the moment is remembered
    d = client.post("/api/boards", json={"hold": 30, "command": "save"}).get_json()
    assert d["sent"][-1].endswith("save") and d["boards"]["hold"] == 30 and d["boards"]["peak"] == 60 and "saved_at" in d["boards"]
    # one board only, by base note
    d = client.post("/api/boards", json={"peak": 55, "board": 16}).get_json()
    assert d["sent"][0].endswith("base note 16")
    # out of range is refused before anything is sent; so is an empty request and a bad command
    assert client.post("/api/boards", json={"hold": 90}).status_code == 400
    assert client.post("/api/boards", json={}).status_code == 400
    assert client.post("/api/boards", json={"command": "explode"}).status_code == 400
    # factory forgets what was remembered
    d = client.post("/api/boards", json={"command": "factory"}).get_json()
    assert set(d["boards"]) == {"sent_at"}
    # not while the player has work
    desk.add(["marches/bogey.organ.mid"])
    assert client.post("/api/boards", json={"peak": 60}).status_code == 409
    st = client.get("/api/state").get_json()
    assert st["settings"]["boards"] == d["boards"]
    assert st["board_defaults"] == {"peak": 60, "hold": 25, "peak_ms": 40, "max_note": 30, "exercise": 2}
    assert st["firmware_defaults"]["peak"] == 100 and st["firmware_defaults"]["hold"] == 25


def test_upload_route_runs_a_job(client, desk):
    data = {"file": (io.BytesIO(b"MThd"), "Uploaded Tune.mid"), "plan": "", "transpose": "auto"}
    r = client.post("/api/arrange", data=data, content_type="multipart/form-data")
    assert r.status_code == 200
    job = r.get_json()["job"]
    import time
    for _ in range(50):
        job = client.get(f"/api/jobs/{job['id']}").get_json()["job"]
        if job["state"] != "running":
            break
        time.sleep(0.05)
    assert job["state"] == "done" and job["output"] == "uploads/uploaded-tune.organ.mid"
    assert client.get("/api/jobs").get_json()["jobs"][0]["id"] == job["id"]
    assert client.get("/api/jobs/zzz").status_code == 404
    assert client.post("/api/arrange", data={}, content_type="multipart/form-data").status_code == 400
