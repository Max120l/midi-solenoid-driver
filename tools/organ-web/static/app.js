/* The organ's front desk. Plain JS: polls /api/state once a second and
   renders; every button is one POST. Nothing here holds state the server
   does not have, so a phone and the touchscreen always agree. */

const $ = (id) => document.getElementById(id);
const fmt = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let state = null;
let build = null;                 // the server's page-files token: when it changes, the page reloads itself
let library = { tunes: [], folders: [] };
let folder = "";
let playlistOpen = null;
let toastTimer = null;
const keys = { layout: null, view: "section", hold: false, sounding: new Set(), rolling: [] };

function toast(text, error) {
  const t = $("toast");
  t.textContent = text; t.hidden = false; t.className = error ? "error" : "";
  if (error && typeof chirp === "function") chirp("deny");
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), error ? 5000 : 2200);
}

async function api(path, method = "GET", body) {
  const opts = { method, headers: {} };
  if (body instanceof FormData) opts.body = body;
  else if (body !== undefined) { opts.headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(body); }
  const r = await fetch(path, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* no body */ }
  if (!r.ok) throw new Error(data.error || `${r.status} ${r.statusText}`);
  return data;
}
async function act(path, body, okText) {
  try { const d = await api(path, "POST", body); if (okText) toast(okText); await refresh(); return d; }
  catch (e) { toast(e.message, true); }
}

/* ---- tabs ------------------------------------------------------------- */
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(name) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  document.querySelectorAll(".tab").forEach((s) => s.classList.toggle("active", s.id === `tab-${name}`));
  try { localStorage.setItem("organ-tab", name); } catch (e) { /* fine */ }
  if (name === "library") loadLibrary();
  if (name === "playlists") loadPlaylists();
  if (name === "upload") loadUploadFolders();
  if (name === "arrange") { loadPlans(); loadJobs(); }
  if (name === "service") loadKeys();
}

/* ---- state ------------------------------------------------------------ */
async function refresh() {
  try { state = await api("/api/state"); } catch (e) { $("now-line").textContent = "no connection"; return; }
  if (build !== null && state.build !== build) { location.reload(); return; }
  build = state.build;
  renderNow(); renderQueue(); renderSettings(); renderBoards(); renderService(); renderNav(); renderScreen();
}

/* the sidebar's small numbers: readings, not decoration */
function renderNav() {
  const c = state.counts || {}, st = state.status || {};
  const left = state.queue.filter((e) => !e.done);
  const secs = left.reduce((a, e) => a + (e.length_s || 0), 0);
  const code = {
    play: state.held ? `ready · ${left.length} to play` : left.length ? `${left.length} to play · ${fmt(secs)}` : (state.pending.length ? `${state.pending.length} waiting` : "idle"),
    library: `${c.tunes ?? "–"} tunes · ${c.folders ?? "–"} folders`,
    playlists: `${c.playlists ?? "–"} lists`,
    upload: `${(c.by_folder || {}).uploads || 0} in uploads`,
    arrange: c.jobs_running ? `${c.jobs_running} arranging` : (c.jobs_done ? `${c.jobs_done} done` : "ready"),
    service: [state.power && state.power.on ? "power on" : "", state.pump.on ? "pump on" : ""].filter(Boolean).join(" · ") || (state.idle ? "idle" : "busy"),
    settings: `tempo ${Math.round((state.settings.tempo || 1) * 100)} %` + (state.settings.repeat ? " · repeat" : ""),
  };
  document.querySelectorAll("#tabs button").forEach((b) => { if (code[b.dataset.tab]) b.dataset.code = code[b.dataset.tab]; });
}

function renderNow() {
  const st = state.status || {};
  const playing = st.state === "playing" || st.state === "paused" || st.state === "pause" || st.state === "skipped";
  const paused = st.state === "paused";
  $("lamp-player").className = "lamp" + (state.player.running ? " on" : "");
  const pump = state.pump;
  const powered = state.power && state.power.configured ? state.power.on : true;
  $("lamp-pump").className = "lamp" + ((pump.on || (state.power && state.power.configured && state.power.on)) ? (state.warming_up_s > 0 ? " warm" : " on") : "");
  $("lamp-pump").textContent = state.warming_up_s > 0 ? `wind in ${Math.ceil(state.warming_up_s)} s`
    : pump.configured ? (pump.on ? "pump" : (powered ? "pump off" : "organ off")) : (state.power && state.power.configured ? (powered ? "power" : "organ off") : "pump (manual)");
  let line;
  if (st.state === "playing") line = `${st.song}  ${fmt(st.position_s)} / ${fmt(st.length_s)}`;
  else if (paused) line = `paused  ${st.song}  ${fmt(st.position_s)} / ${fmt(st.length_s)}`;
  else if (st.state === "pause") line = `pause ${st.seconds ? st.seconds + " s" : ""}`;
  else if (state.warming_up_s > 0) line = "waiting for wind";
  else if (state.queue.some((e) => !e.done) && !state.held) line = "starting…";
  else line = state.player.running ? "idle" : "player not running";
  $("now-line").textContent = line;
  if (st.state === "playing" || st.state === "pause" || paused) {
    $("now-title").textContent = st.song || "";
    $("now-sub").textContent = `${st.index}/${st.total || st.queue || ""}  ·  tempo ${Math.round((st.tempo || 1) * 100)} %`
      + (st.state === "pause" ? "  ·  pause" : paused ? "  ·  PAUSED" : "");
    const p = st.length_s ? Math.min(100, 100 * (st.position_s || 0) / st.length_s) : 0;
    $("now-bar").style.width = p + "%";
    $("now-pos").textContent = fmt(st.position_s); $("now-len").textContent = fmt(st.length_s);
  } else {
    $("now-title").textContent = state.queue.some((e) => !e.done) ? (state.held ? "Ready" : "Starting…") : (state.pending.length ? "Waiting for wind" : "Nothing");
    $("now-sub").textContent = ""; $("now-bar").style.width = "0"; $("now-pos").textContent = "0:00"; $("now-len").textContent = "0:00";
  }
  $("btn-skip").disabled = !playing;
  $("btn-pause").hidden = state.held;
  $("btn-pause").disabled = !(st.state === "playing" || paused);
  $("btn-pause").textContent = paused ? "Resume" : "Pause";
  $("btn-pause").classList.toggle("active", paused);
  $("btn-play").hidden = !state.held;
  const selected = state.queue.find((e) => e.now);
  $("btn-play").disabled = !selected;
  $("btn-stop").hidden = state.held;
  if (state.held && selected) {
    $("now-title").textContent = selected.name;
    $("now-sub").textContent = st.state === "skipped" || st.state === "paused" ? "stopped · Play starts it from the top" : "ready · press Play";
  }
  $("chk-repeat").checked = !!state.settings.repeat;
}

function renderQueue() {
  const ul = $("queue");
  const items = state.queue;
  const toCome = items.filter((e) => !e.done && !e.now).length;
  const played = items.filter((e) => e.done).length;
  $("queue-label").textContent = !items.length ? "Queue is empty"
    : state.held ? `Ready · ${toCome + 1} to play` : `${toCome} to play${played ? ` · ${played} played` : ""}`;
  ul.innerHTML = items.map((e, i) => `
    <li class="${e.now ? "now" : e.done ? "done" : ""}">
      <span class="meta">${e.now ? (state.held ? "■" : "▶") : e.done ? "✓" : i + 1}</span>
      <span class="name">${esc(e.name)}<br><span class="meta">${esc(e.path.split("/").slice(0, -1).join("/"))}</span></span>
      <span class="meta">${fmt(e.length_s)}${e.tempo ? " · " + Math.round(e.tempo * 100) + " %" : ""}</span>
      ${e.done ? `<button data-again="${esc(e.path)}" title="queue again">↻</button><button data-remove="${e.id}" class="danger" title="remove">✕</button>`
        : e.now && !state.held ? ""
        : `<button data-move="${e.id}" data-dir="-1" title="earlier">▲</button>
      <button data-move="${e.id}" data-dir="1" title="later">▼</button>
      <button data-remove="${e.id}" class="danger" title="remove">✕</button>`}
    </li>`).join("");
  ul.querySelectorAll("[data-remove]").forEach((b) => b.onclick = () => act("/api/queue/remove", { id: +b.dataset.remove }));
  ul.querySelectorAll("[data-again]").forEach((b) => b.onclick = () => act("/api/queue/add", { path: b.dataset.again }, "queued again"));
  // a tap on the song itself moves the programme there: play it (again) and carry on from it
  ul.querySelectorAll("li").forEach((li, i) => {
    const e = items[i];
    li.querySelector(".name").classList.add("tappable");
    li.querySelector(".name").onclick = () => {
      if (e.now && !state.held) { act("/api/queue/jump", { id: e.id }, `${e.name} from the top`); return; }
      act("/api/queue/jump", { id: e.id }, state.held ? `${e.name} selected` : `jumping to ${e.name}`);
    };
  });
  ul.querySelectorAll("[data-move]").forEach((b) => b.onclick = () => {
    const id = +b.dataset.move, idx = items.findIndex((e) => e.id === id);
    act("/api/queue/move", { id, to: idx + (+b.dataset.dir) });
  });
  $("btn-clear-played").hidden = !played;
  $("pending-box").hidden = !state.pending.length;
  $("pending").innerHTML = state.pending.map((e) => `<li><span class="name">${esc(e.name)}</span><span class="meta">${fmt(e.length_s)}</span></li>`).join("");
}

$("btn-skip").onclick = async () => { const d = await act("/api/player/skip"); if (d && !d.skipped) toast("nothing to skip, or no signal on this platform", true); };
$("btn-stop").onclick = () => act("/api/player/stop", {}, "stopped");
$("btn-play").onclick = () => act("/api/player/play", {}, "playing");
$("btn-pause").onclick = async () => { const d = await act("/api/player/pause"); if (d && !d.paused) toast("nothing playing, or no signal on this platform", true); };
$("btn-shuffle").onclick = () => act("/api/queue/shuffle", {}, "shuffled");
$("btn-clear").onclick = () => act("/api/queue/clear", {}, "queue cleared");
$("btn-clear-played").onclick = () => act("/api/queue/clear-played", {}, "played songs cleared");
$("btn-save").onclick = () => { const name = prompt("Playlist name"); if (name) act("/api/queue/save", { name }, `saved "${name}"`); };
$("chk-repeat").onchange = (e) => act("/api/settings", { repeat: e.target.checked });
$("set-power-switch").onchange = (e) => act("/api/settings", { power_switch: e.target.checked }, e.target.checked ? "the switch shuts the organ down" : "the switch is ignored");

/* ---- library ---------------------------------------------------------- */
async function loadLibrary() {
  try { library = await api("/api/library"); } catch (e) { toast(e.message, true); return; }
  if (folder && !library.folders.includes(folder)) folder = "";
  renderLibrary();
}
function renderLibrary() {
  const chips = [""].concat(library.folders);
  const inFolder = (f) => library.tunes.filter((t) => !f || t.folder === f || t.folder.startsWith(f + "/")).length;
  $("folders").innerHTML = chips.map((f) => `<button data-folder="${esc(f)}" class="${f === folder ? "active" : ""}">${f ? esc(f) : "everything"} <span class="count">${inFolder(f)}</span></button>`).join("");
  $("folders").querySelectorAll("button").forEach((b) => b.onclick = () => { folder = b.dataset.folder; renderLibrary(); });
  const q = $("search").value.trim().toLowerCase();
  const tunes = library.tunes.filter((t) => (!folder || t.folder === folder || t.folder.startsWith(folder + "/")) && (!q || t.name.toLowerCase().includes(q)));
  $("tunes").innerHTML = tunes.map((t) => `
    <li>
      <span class="name">${esc(t.name)}<br><span class="meta">${esc(t.folder)}</span></span>
      <span class="meta">${fmt(t.length_s)}</span>
      <button data-play="${esc(t.path)}" title="play now">▶</button>
      <button data-queue="${esc(t.path)}" title="add to queue">＋</button>
    </li>`).join("") || `<li><span class="name hint">No arranged tunes here. Arranged files end in .organ.mid.</span></li>`;
  $("tunes").querySelectorAll("[data-queue]").forEach((b) => b.onclick = () => act("/api/queue/add", { path: b.dataset.queue }, "queued"));
  $("tunes").querySelectorAll("[data-play]").forEach((b) => b.onclick = () => act("/api/queue/add", { path: b.dataset.play, play_now: true }, "playing next"));
  $("btn-queue-folder").textContent = folder ? `Queue ${folder}` : "Queue all";
  $("btn-shuffle-folder").textContent = folder ? `Shuffle ${folder}` : "Shuffle all";
  $("btn-queue-folder").onclick = () => act("/api/queue/add", { paths: tunes.map((t) => t.path) }, `${tunes.length} queued`);
  $("btn-shuffle-folder").onclick = () => act("/api/queue/add", { paths: tunes.map((t) => t.path), shuffle: true }, `${tunes.length} queued, shuffled`);
}
$("search").oninput = renderLibrary;

/* ---- playlists -------------------------------------------------------- */
async function loadPlaylists() {
  let d; try { d = await api("/api/playlists"); } catch (e) { toast(e.message, true); return; }
  $("playlists").innerHTML = d.playlists.map((p) => `
    <li><span class="name">${esc(p.name)}</span><span class="meta">${p.count} tunes</span>
      <button data-open="${esc(p.name)}">Open</button><button data-q="${esc(p.name)}">＋</button></li>`).join("")
    || `<li><span class="name hint">No playlists yet. Build a queue and press "Save as playlist".</span></li>`;
  $("playlists").querySelectorAll("[data-open]").forEach((b) => b.onclick = () => openPlaylist(b.dataset.open));
  $("playlists").querySelectorAll("[data-q]").forEach((b) => b.onclick = () => act(`/api/playlists/${encodeURIComponent(b.dataset.q)}/queue`, {}, "queued"));
  if (playlistOpen && !d.playlists.some((p) => p.name === playlistOpen)) { playlistOpen = null; $("playlist-view").hidden = true; }
}
async function openPlaylist(name) {
  let d; try { d = await api(`/api/playlists/${encodeURIComponent(name)}`); } catch (e) { toast(e.message, true); return; }
  playlistOpen = name;
  $("playlist-view").hidden = false;
  $("playlist-name").textContent = name;
  $("playlist-entries").innerHTML = d.entries.map((e, i) => `
    <li class="${e.missing ? "missing" : ""}"><span class="meta">${i + 1}</span><span class="name">${esc(e.name)}${e.missing ? " (missing)" : ""}</span>
      <span class="meta">${e.tempo ? Math.round(e.tempo * 100) + " %" : ""}${e.gap != null ? " · gap " + e.gap + " s" : ""}</span></li>`).join("");
}
$("pl-queue").onclick = () => act(`/api/playlists/${encodeURIComponent(playlistOpen)}/queue`, {}, "queued");
$("pl-shuffle").onclick = () => act(`/api/playlists/${encodeURIComponent(playlistOpen)}/queue`, { shuffle: true }, "queued, shuffled");
$("pl-replace").onclick = () => act(`/api/playlists/${encodeURIComponent(playlistOpen)}/queue`, { replace: true }, "playing this instead");
$("pl-delete").onclick = async () => {
  if (!confirm(`Delete playlist "${playlistOpen}"?`)) return;
  try { await api(`/api/playlists/${encodeURIComponent(playlistOpen)}`, "DELETE"); toast("deleted"); loadPlaylists(); }
  catch (e) { toast(e.message, true); }
};

/* ---- upload: finished files into a folder --------------------------------- */
const uploaded = [];
async function loadUploadFolders() {
  try { library = await api("/api/library"); } catch (e) { return; }
  const current = $("upload-folder").value;
  const folders = library.folders.filter((f) => f).concat(library.folders.includes("uploads") ? [] : ["uploads"]);
  $("upload-folder").innerHTML = folders.map((f) => `<option ${f === (current || "uploads") ? "selected" : ""}>${esc(f)}</option>`).join("");
}
function renderUploaded() {
  $("uploaded").innerHTML = uploaded.map((u) => `
    <li><span class="name">${esc(u.name)}<br><span class="meta">${esc(u.folder)}</span></span><span class="meta">${fmt(u.length_s)}</span>
      <button data-play="${esc(u.path)}" title="play now">▶</button><button data-queue="${esc(u.path)}" title="add to queue">＋</button></li>`).join("")
    || `<li><span class="name hint">Nothing uploaded in this session.</span></li>`;
  $("uploaded").querySelectorAll("[data-queue]").forEach((b) => b.onclick = () => act("/api/queue/add", { path: b.dataset.queue }, "queued"));
  $("uploaded").querySelectorAll("[data-play]").forEach((b) => b.onclick = () => act("/api/queue/add", { path: b.dataset.play, play_now: true }, "playing next"));
}
$("upload-files-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const files = $("upload-files").files;
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("file", f);
  fd.append("folder", $("upload-new-folder").value.trim() || $("upload-folder").value);
  try {
    const d = await api("/api/upload", "POST", fd);
    uploaded.unshift(...d.uploaded); renderUploaded();
    toast(`${d.uploaded.length} file${d.uploaded.length === 1 ? "" : "s"} added`);
    $("upload-files").value = ""; $("upload-new-folder").value = "";
    loadUploadFolders();
  } catch (e) { toast(e.message, true); }
};
renderUploaded();

/* ---- arrange: raw MIDI through the transcriber and the arranger ---------- */
async function loadPlans() {
  try {
    const d = await api("/api/plans");
    $("upload-plan").innerHTML = `<option value="">automatic</option>` + d.plans.map((p) => `<option>${esc(p)}</option>`).join("");
  } catch (e) { /* the list is a convenience */ }
}
async function loadJobs() {
  let d; try { d = await api("/api/jobs"); } catch (e) { return; }
  $("jobs").innerHTML = d.jobs.map((j) => `
    <li class="job">
      <div class="row"><span class="name">${esc(j.name)}</span>
        <span class="state-${j.state}">${j.state}</span>
        ${j.plan ? `<span class="meta">plan ${esc(j.plan)}</span>` : ""}
        ${j.output ? `<button data-queue="${esc(j.output)}">＋ queue</button><button data-play="${esc(j.output)}">▶</button>` : ""}
        <button data-log="${j.id}">log</button></div>
      <pre id="log-${j.id}" hidden>${esc(j.log)}</pre>
    </li>`).join("") || `<li><span class="name hint">Nothing arranged yet.</span></li>`;
  $("jobs").querySelectorAll("[data-log]").forEach((b) => b.onclick = () => { const p = $(`log-${b.dataset.log}`); p.hidden = !p.hidden; });
  $("jobs").querySelectorAll("[data-queue]").forEach((b) => b.onclick = () => act("/api/queue/add", { path: b.dataset.queue }, "queued"));
  $("jobs").querySelectorAll("[data-play]").forEach((b) => b.onclick = () => act("/api/queue/add", { path: b.dataset.play, play_now: true }, "playing next"));
  if (d.jobs.some((j) => j.state === "running")) setTimeout(loadJobs, 1500);
}
$("upload-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const f = $("upload-file").files[0];
  if (!f) return;
  const fd = new FormData();
  fd.append("file", f); fd.append("plan", $("upload-plan").value); fd.append("transpose", $("upload-transpose").value.trim() || "auto");
  try { await api("/api/arrange", "POST", fd); toast("arranging…"); $("upload-file").value = ""; loadJobs(); }
  catch (e) { toast(e.message, true); }
};

/* ---- settings --------------------------------------------------------- */
let settingsDirty = false;
function renderSettings() {
  if (settingsDirty) return;
  const s = state.settings;
  $("set-tempo").value = Math.round(s.tempo * 100); $("set-tempo-v").textContent = `${Math.round(s.tempo * 100)} %`;
  $("set-gap").value = s.gap; $("set-warm").value = s.warm_up; $("set-idle").value = s.idle_off;
  $("set-power-switch").checked = s.power_switch !== false;
  $("about").textContent = `organ_web ${state.version}${state.dry_run ? " · dry run: nothing reaches the organ" : ""}`;
}
["set-tempo", "set-gap", "set-warm", "set-idle"].forEach((id) => $(id).oninput = () => { settingsDirty = true; $("set-tempo-v").textContent = `${$("set-tempo").value} %`; });
/* the tempo rail on the Play screen applies as soon as the finger lifts */
$("set-tempo").onchange = async () => { settingsDirty = false; await act("/api/settings", { tempo: `${$("set-tempo").value}%` }, `tempo ${$("set-tempo").value} %`); };
$("tempo-reset").onclick = async () => { $("set-tempo").value = 100; $("set-tempo-v").textContent = "100 %"; settingsDirty = false; await act("/api/settings", { tempo: "100%" }, "tempo 100 %"); };
$("btn-save-settings").onclick = async () => {
  const body = { gap: +$("set-gap").value, warm_up: +$("set-warm").value, idle_off: +$("set-idle").value };
  settingsDirty = false;
  await act("/api/settings", body, "settings saved");
};

/* ---- this screen: brightness and a time-out, per browser ----------------- */
/* With a real backlight (the Pi's own touch display) the Pi drives it; on an
   HDMI panel the page dims itself with a dark layer, and "off" is a black
   screen. The first touch on a dark screen only wakes it. */
const screen = { bright: 100, timeout: 0, off: false, timer: null, hw: false };
try {
  screen.bright = +(localStorage.getItem("organ-bright") || 100);
  const kiosk = new URLSearchParams(location.search).get("kiosk");
  if (kiosk) localStorage.setItem("organ-kiosk", "1");
  const isKiosk = localStorage.getItem("organ-kiosk") === "1";
  screen.timeout = localStorage.getItem("organ-timeout") != null ? +localStorage.getItem("organ-timeout") : (isKiosk ? 10 : 0);
} catch (e) { /* fine */ }
$("scr-bright").value = screen.bright; $("scr-bright-v").textContent = `${screen.bright} %`;
$("scr-timeout").value = screen.timeout;
async function applyBrightness(save) {
  $("scr-bright-v").textContent = `${screen.bright} %`;
  if (save) { try { localStorage.setItem("organ-bright", String(screen.bright)); } catch (e) { /* fine */ } }
  if (screen.hw) {
    try { await api("/api/screen", "POST", { brightness: screen.bright }); $("dim").style.opacity = 0; return; } catch (e) { /* fall back to the layer */ }
  }
  $("dim").style.opacity = String((100 - screen.bright) / 100 * 0.85);
}
$("scr-bright").oninput = () => { screen.bright = +$("scr-bright").value; applyBrightness(false); };
$("scr-bright").onchange = () => applyBrightness(true);
$("scr-timeout").onchange = () => {
  screen.timeout = Math.max(0, +$("scr-timeout").value || 0);
  try { localStorage.setItem("organ-timeout", String(screen.timeout)); } catch (e) { /* fine */ }
  armScreenTimer();
  toast(screen.timeout ? `screen off after ${screen.timeout} min` : "screen stays on");
};
function armScreenTimer() {
  clearTimeout(screen.timer);
  if (screen.timeout > 0 && !screen.off) screen.timer = setTimeout(screenOff, screen.timeout * 60 * 1000);
}
async function screenOff() {
  screen.off = true;
  $("screen-off").hidden = false;
  if (screen.hw) { try { await api("/api/screen", "POST", { power: "off" }); } catch (e) { /* the black layer stands */ } }
}
async function screenOn() {
  if (!screen.off) return;
  screen.off = false;
  $("screen-off").hidden = true;
  if (screen.hw) { try { await api("/api/screen", "POST", { power: "on", brightness: screen.bright }); } catch (e) { /* fine */ } }
  armScreenTimer();
}
$("scr-off-now").onclick = () => setTimeout(screenOff, 300);
// any touch wakes; a touch on a dark screen does nothing else
["pointerdown", "keydown", "touchstart"].forEach((ev) => document.addEventListener(ev, (e) => {
  if (screen.off) { e.stopPropagation(); e.preventDefault(); screenOn(); return; }
  armScreenTimer();
}, { capture: true, passive: false }));
["pointermove", "wheel"].forEach((ev) => document.addEventListener(ev, () => { if (!screen.off) armScreenTimer(); }, { passive: true }));
$("screen-off").addEventListener("click", (e) => { e.stopPropagation(); e.preventDefault(); }, true);
function renderScreen() {
  const hw = !!state.backlight;
  if (hw !== screen.hw) { screen.hw = hw; applyBrightness(false); }
  $("scr-note").textContent = hw ? "This display's backlight is under the Pi's control."
                                 : "No backlight control on this display: the page dims itself, and off is a black screen.";
}
applyBrightness(false);
armScreenTimer();

/* ---- the driver boards' solenoid parameters ---------------------------- */
const BOARD_FIELDS = { peak: "bd-peak", hold: "bd-hold", peak_ms: "bd-peak-ms", max_note: "bd-max-note", exercise: "bd-exercise" };
let boardsDirty = false;
let boardsLocked = true;                 // always starts locked; unlock is a deliberate tap, and it relocks after a while
let relockTimer = null;
const BOARD_BUTTONS = ["bd-apply", "bd-apply-save", "bd-reload", "bd-factory", "bd-defaults"];
Object.values(BOARD_FIELDS).forEach((id) => $(id).oninput = () => (boardsDirty = true));
function setBoardsLock(locked) {
  boardsLocked = locked;
  clearTimeout(relockTimer);
  if (!locked) relockTimer = setTimeout(() => setBoardsLock(true), 3 * 60 * 1000);
  $("bd-lock").textContent = locked ? "Unlock" : "Lock";
  $("bd-lock").classList.toggle("danger", locked);
  $("bd-lock-note").textContent = locked ? "locked" : "unlocked: relocks in 3 minutes";
  Object.values(BOARD_FIELDS).forEach((id) => ($(id).disabled = locked));
  if (state) renderBoards();
}
$("bd-lock").onclick = () => setBoardsLock(!boardsLocked);
function renderBoards() {
  const b = state.settings.boards || {}, ours = state.board_defaults || {}, fw = state.firmware_defaults || {};
  if (!boardsDirty) Object.entries(BOARD_FIELDS).forEach(([k, id]) => { $(id).value = b[k] ?? ours[k] ?? ""; });
  Object.entries(BOARD_FIELDS).forEach(([k, id]) => { $(id).placeholder = fw[k] != null ? `firmware ${fw[k]}` : ""; });
  const when = (t) => new Date(t * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
  let note = "The boards cannot be read back. ";
  if (b.sent_at) note += `These are the values last sent from here, ${when(b.sent_at)}${b.saved_at ? `, saved to the boards ${when(b.saved_at)}` : ", not yet saved to the boards"}. `;
  else note += "Nothing has been sent from here yet; the fields show this organ's usual values, and the hints the firmware's own. ";
  note += state.idle ? "Apply changes them until power-off; save writes them to each board's memory, and every board clicks once to say so."
                     : "The player is busy: stop it before retuning.";
  $("bd-note").textContent = note;
  BOARD_BUTTONS.forEach((id) => $(id).disabled = boardsLocked || !state.idle);
}
$("bd-defaults").onclick = () => {
  Object.entries(BOARD_FIELDS).forEach(([k, id]) => { $(id).value = (state.board_defaults || {})[k] ?? ""; });
  boardsDirty = true;
  toast("this organ's usual values filled in; Apply sends them");
};
function boardValues() {
  const out = {};
  Object.entries(BOARD_FIELDS).forEach(([k, id]) => { if ($(id).value !== "") out[k] = +$(id).value; });
  return out;
}
async function sendBoards(body, okText) {
  try {
    const d = await api("/api/boards", "POST", body);
    boardsDirty = false;
    setBoardsLock(true);                 // one change per unlock
    toast(d.warnings && d.warnings.length ? d.warnings.join("; ") : okText, !!(d.warnings && d.warnings.length));
    await refresh();
  } catch (e) { toast(e.message, true); }
}
$("bd-apply").onclick = () => sendBoards(boardValues(), "sent to the boards");
$("bd-apply-save").onclick = () => { if (confirm("Send these values and save them to every board's memory?")) sendBoards({ ...boardValues(), command: "save" }, "sent and saved"); };
$("bd-reload").onclick = () => sendBoards({ command: "reload" }, "boards reloaded their saved settings");
$("bd-factory").onclick = () => { if (confirm("Return every board to its compiled defaults? (In memory only; save afterwards to keep.)")) sendBoards({ command: "factory" }, "boards at factory defaults"); };

/* ---- service ---------------------------------------------------------- */
$("btn-reset").onclick = () => { if (confirm("Reset all driver boards? Every note drops and the boards run their exercise routine.")) act("/api/service/reset", {}, "boards reset"); };
$("btn-power-on").onclick = () => act("/api/service/power", { on: true }, "organ power on");
$("btn-power-off").onclick = () => act("/api/service/power", { on: false }, "organ power off");
$("btn-shutdown").onclick = () => {
  if (!confirm("Shut the organ down? The pump and the 12 V go off, then the Pi powers itself off. Turn the panel switch off once the screen is dark.")) return;
  act("/api/service/shutdown", {}, "powering off -- turn the switch off when the screen is dark");
};
$("btn-pump-on").onclick = () => act("/api/service/pump", { on: true }, "pump on");
$("btn-pump-off").onclick = () => act("/api/service/pump", { on: false }, "pump off");

function renderService() {
  const pumpless = !state.pump.configured, powerless = !(state.power && state.power.configured);
  $("btn-pump-on").disabled = pumpless; $("btn-pump-off").disabled = pumpless;
  $("btn-power-on").disabled = powerless; $("btn-power-off").disabled = powerless;
  $("btn-power-on").classList.toggle("active", !powerless && state.power.on);
  $("btn-pump-on").classList.toggle("active", !pumpless && state.pump.on);
  const sw = state.switch || {};
  const swText = !sw.configured ? "no on/off switch (--power-switch GPIO)" : `on/off switch: contact ${sw.closed ? "closed (on)" : "OPEN (off)"}${sw.enabled ? "" : ", ignored"}`;
  $("service-note").textContent = [swText, powerless ? "no power relay (--power GPIO)" : "", pumpless ? "no pump relay (--pump GPIO)" : ""].filter(Boolean).join(" · ");
  const busy = !state.idle;
  $("keys-busy").hidden = !busy;
  $("keys").classList.toggle("disabled", busy);
  if (!state.keys_open) { keys.sounding.clear(); keys.rolling = []; paintKeys(); }
}
async function loadKeys() {
  if (keys.layout) { renderKeys(); return; }
  try { keys.layout = await api("/api/keys/layout"); renderKeys(); } catch (e) { toast(e.message, true); }
}
function renderKeys() {
  const L = keys.layout, bySection = keys.view === "section";
  const blocks = bySection ? L.sections : L.boards;
  const labels = bySection ? L.section_labels : L.labels;
  $("keys").innerHTML = blocks.map((b) => `
    <div class="block"><div class="block-title">${esc(b.name)} · ${b.solenoids.length}</div>
      <div class="grid">${b.solenoids.map((s) => `<button class="key" data-s="${s}"><span class="n">${s}</span><span class="l">${esc(labels[s] || "")}</span></button>`).join("")}</div>
    </div>`).join("");
  $("keys").querySelectorAll(".key").forEach((b) => b.onclick = () => tapKey(+b.dataset.s));
  $("keys-view").textContent = bySection ? "By section" : "By board";
  paintKeys();
}
function paintKeys() {
  $("keys").querySelectorAll(".key").forEach((b) => b.classList.toggle("on", keys.sounding.has(+b.dataset.s)));
  $("keys-roll").setAttribute("aria-pressed", keys.rolling.length ? "true" : "false");
}
async function keysAct(what, body) {
  try {
    const r = await api(`/api/keys/${what}`, "POST", body || {});
    keys.sounding = new Set(r.sounding); keys.rolling = r.rolling; paintKeys();
    if (r.sounding.length && !keys.hold) setTimeout(() => { keys.sounding.clear(); paintKeys(); }, 250);
  } catch (e) { toast(e.message, true); }
}
function tapKey(s) {
  if (keys.hold) keysAct("hold", { solenoid: s, on: !keys.sounding.has(s) });
  else keysAct("pulse", { solenoid: s });
}
$("keys-view").onclick = () => { keys.view = keys.view === "section" ? "board" : "section"; renderKeys(); };
$("keys-hold").onchange = (e) => { keys.hold = e.target.checked; if (!keys.hold) keysAct("off"); };
$("keys-roll").onclick = () => keysAct("roll", { on: !keys.rolling.length, interval_ms: +$("keys-interval").value });
$("keys-interval").oninput = () => { $("keys-interval-v").textContent = $("keys-interval").value; if (keys.rolling.length) keysAct("roll", { on: true, interval_ms: +$("keys-interval").value }); };
$("keys-off").onclick = () => keysAct("off");

/* ---- the look ---------------------------------------------------------- */
$("set-theme").onchange = (e) => {
  document.documentElement.dataset.theme = e.target.value;
  try { localStorage.setItem("organ-theme", e.target.value); } catch (err) { /* fine */ }
};
$("set-theme").value = document.documentElement.dataset.theme || "lcars";
function stardate() {
  // TNG-style: four digits and a tenth, ticking through the day. Purely decorative.
  const now = new Date(), start = new Date(now.getFullYear(), 0, 1);
  const frac = (now - start) / (365.25 * 864e5);
  return (1000 * (now.getFullYear() - 1987) + frac * 1000).toFixed(1);
}
setInterval(() => { $("stardate").textContent = "Stardate " + stardate(); }, 6000);
$("stardate").textContent = "Stardate " + stardate();

/* ---- panel sounds ------------------------------------------------------ */
/* Synthesised, not sampled: a short two-tone chirp on a tap, lower on the red
   buttons, a flat buzz on a refusal. The browser only lets a page make sound
   after a gesture, so the audio context is made on the first tap. */
const sound = { ctx: null, on: true };
try { sound.on = localStorage.getItem("organ-sound") !== "off"; } catch (e) { /* fine */ }
$("set-sound").checked = sound.on;
$("set-sound").onchange = (e) => {
  sound.on = e.target.checked;
  try { localStorage.setItem("organ-sound", sound.on ? "on" : "off"); } catch (err) { /* fine */ }
  if (sound.on) chirp("tap");
};
function chirp(kind) {
  if (!sound.on || document.documentElement.dataset.theme !== "lcars") return;
  try {
    if (!sound.ctx) sound.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const ctx = sound.ctx;
    if (ctx.state === "suspended") ctx.resume();
    const t0 = ctx.currentTime;
    const tone = (freq1, freq2, start, len, gain, type) => {
      const o = ctx.createOscillator(), g = ctx.createGain();
      o.type = type || "sine";
      o.frequency.setValueAtTime(freq1, t0 + start);
      if (freq2 !== freq1) o.frequency.exponentialRampToValueAtTime(freq2, t0 + start + len);
      g.gain.setValueAtTime(0.0001, t0 + start);
      g.gain.exponentialRampToValueAtTime(gain, t0 + start + 0.006);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + start + len);
      o.connect(g).connect(ctx.destination);
      o.start(t0 + start); o.stop(t0 + start + len + 0.02);
    };
    if (kind === "danger") { tone(740, 740, 0, 0.07, 0.18); tone(560, 560, 0.08, 0.09, 0.18); }
    else if (kind === "deny") { tone(220, 200, 0, 0.16, 0.16, "square"); }
    else if (kind === "nav") { tone(1480, 1480, 0, 0.05, 0.14); tone(1960, 1960, 0.055, 0.06, 0.14); }
    else { tone(1250, 1850, 0, 0.055, 0.16); tone(2200, 2200, 0.06, 0.05, 0.12); }
  } catch (e) { /* no audio here; the button still works */ }
}
/* on release, not on touch-down: a finger that starts a scroll makes no sound */
const scrollDrag = { swallow: false };
document.addEventListener("click", (ev) => {
  if (scrollDrag.swallow) return;
  const b = ev.target.closest("button, input[type=checkbox], select, .key");
  if (!b) return;
  if (b.closest("#tabs")) chirp("nav");
  else if (b.classList.contains("danger")) chirp("danger");
  else chirp("tap");
}, true);
document.addEventListener("change", (ev) => { if (ev.target.matches("input[type=range]")) chirp("tap"); });

/* ---- drag to scroll ---------------------------------------------------- */
/* A real touch digitiser scrolls the content natively. Many HDMI touch panels
   report as a mouse instead, where a drag would select text; so a mouse-type
   drag of more than a few pixels scrolls the content, and the click that would
   follow it is swallowed so a scroll never presses the button under the finger. */
(() => {
  const main = document.querySelector("main");
  let drag = null;
  const swallowFor = (ms) => { scrollDrag.swallow = true; setTimeout(() => (scrollDrag.swallow = false), ms); };
  document.addEventListener("pointerdown", (e) => {
    if (e.pointerType === "touch" || e.button !== 0) return;
    if (e.target.closest("input, select, textarea, pre")) return;
    drag = { x: e.clientX, y: e.clientY, top: main.scrollTop, moved: false };
  });
  document.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dy = e.clientY - drag.y, dx = e.clientX - drag.x;
    if (!drag.moved) {
      if (Math.abs(dy) < 8 && Math.abs(dx) < 8) return;
      drag.moved = true;
      main.classList.add("dragging");
    }
    main.scrollTop = drag.top - dy;
    e.preventDefault();
  });
  const end = () => {
    if (drag && drag.moved) swallowFor(300);
    drag = null;
    main.classList.remove("dragging");
  };
  document.addEventListener("pointerup", end);
  document.addEventListener("pointercancel", end);
  document.addEventListener("click", (e) => { if (scrollDrag.swallow) { e.stopPropagation(); e.preventDefault(); } }, true);
})();

/* ---- go --------------------------------------------------------------- */
let startTab = "play";
try { startTab = localStorage.getItem("organ-tab") || "play"; } catch (e) { /* fine */ }
showTab(startTab);
refresh();
setInterval(refresh, 1000);
