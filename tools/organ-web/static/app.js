/* The organ's front desk. Plain JS: polls /api/state once a second and
   renders; every button is one POST. Nothing here holds state the server
   does not have, so a phone and the touchscreen always agree. */

const $ = (id) => document.getElementById(id);
const fmt = (s) => { s = Math.max(0, Math.round(s || 0)); return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`; };
const esc = (t) => String(t ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let state = null;
let library = { tunes: [], folders: [] };
let folder = "";
let playlistOpen = null;
let toastTimer = null;
const keys = { layout: null, view: "section", hold: false, sounding: new Set(), rolling: [] };

function toast(text, error) {
  const t = $("toast");
  t.textContent = text; t.hidden = false; t.className = error ? "error" : "";
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
  if (name === "upload") { loadPlans(); loadJobs(); }
  if (name === "service") loadKeys();
}

/* ---- state ------------------------------------------------------------ */
async function refresh() {
  try { state = await api("/api/state"); } catch (e) { $("now-line").textContent = "no connection"; return; }
  renderNow(); renderQueue(); renderSettings(); renderService();
}

function renderNow() {
  const st = state.status || {};
  const playing = st.state === "playing" || st.state === "pause" || st.state === "skipped";
  $("lamp-player").className = "lamp" + (state.player.running ? " on" : "");
  const pump = state.pump;
  $("lamp-pump").className = "lamp" + (pump.on ? (state.warming_up_s > 0 ? " warm" : " on") : "");
  $("lamp-pump").textContent = pump.configured ? (state.warming_up_s > 0 ? `wind in ${Math.ceil(state.warming_up_s)} s` : "pump") : "pump (manual)";
  let line;
  if (st.state === "playing") line = `${st.song}  ${fmt(st.position_s)} / ${fmt(st.length_s)}`;
  else if (st.state === "pause") line = `pause ${st.seconds ? st.seconds + " s" : ""}`;
  else if (state.warming_up_s > 0) line = "waiting for wind";
  else if (state.queue.length) line = "starting…";
  else line = state.player.running ? "idle" : "player not running";
  $("now-line").textContent = line;
  if (st.state === "playing" || st.state === "pause") {
    $("now-title").textContent = st.song || "";
    $("now-sub").textContent = `${st.index}/${st.total || st.queue || ""}  ·  tempo ${Math.round((st.tempo || 1) * 100)} %` + (st.state === "pause" ? "  ·  pause" : "");
    const p = st.length_s ? Math.min(100, 100 * (st.position_s || 0) / st.length_s) : 0;
    $("now-bar").style.width = p + "%";
    $("now-pos").textContent = fmt(st.position_s); $("now-len").textContent = fmt(st.length_s);
  } else {
    $("now-title").textContent = state.queue.length ? "Starting…" : (state.pending.length ? "Waiting for wind" : "Nothing");
    $("now-sub").textContent = ""; $("now-bar").style.width = "0"; $("now-pos").textContent = "0:00"; $("now-len").textContent = "0:00";
  }
  $("btn-skip").disabled = !playing;
  $("chk-repeat").checked = !!state.settings.repeat;
}

function renderQueue() {
  const ul = $("queue");
  const items = state.queue;
  $("queue-label").textContent = items.length ? `Queue · ${items.length}` : "Queue is empty";
  ul.innerHTML = items.map((e, i) => `
    <li class="${e.now ? "now" : ""}">
      <span class="meta">${e.now ? "▶" : i + 1}</span>
      <span class="name">${esc(e.name)}<br><span class="meta">${esc(e.path.split("/").slice(0, -1).join("/"))}</span></span>
      <span class="meta">${fmt(e.length_s)}${e.tempo ? " · " + Math.round(e.tempo * 100) + " %" : ""}</span>
      ${e.now ? "" : `<button data-move="${e.id}" data-dir="-1" title="earlier">▲</button>
      <button data-move="${e.id}" data-dir="1" title="later">▼</button>
      <button data-remove="${e.id}" class="danger" title="remove">✕</button>`}
    </li>`).join("");
  ul.querySelectorAll("[data-remove]").forEach((b) => b.onclick = () => act("/api/queue/remove", { id: +b.dataset.remove }));
  ul.querySelectorAll("[data-move]").forEach((b) => b.onclick = () => {
    const id = +b.dataset.move, idx = items.findIndex((e) => e.id === id);
    act("/api/queue/move", { id, to: idx + (+b.dataset.dir) });
  });
  $("pending-box").hidden = !state.pending.length;
  $("pending").innerHTML = state.pending.map((e) => `<li><span class="name">${esc(e.name)}</span><span class="meta">${fmt(e.length_s)}</span></li>`).join("");
}

$("btn-skip").onclick = async () => { const d = await act("/api/player/skip"); if (d && !d.skipped) toast("nothing to skip, or no signal on this platform", true); };
$("btn-stop").onclick = () => act("/api/player/stop", {}, "stopped");
$("btn-shuffle").onclick = () => act("/api/queue/shuffle", {}, "shuffled");
$("btn-clear").onclick = () => act("/api/queue/clear", {}, "queue cleared");
$("btn-save").onclick = () => { const name = prompt("Playlist name"); if (name) act("/api/queue/save", { name }, `saved "${name}"`); };
$("chk-repeat").onchange = (e) => act("/api/settings", { repeat: e.target.checked });

/* ---- library ---------------------------------------------------------- */
async function loadLibrary() {
  try { library = await api("/api/library"); } catch (e) { toast(e.message, true); return; }
  if (folder && !library.folders.includes(folder)) folder = "";
  renderLibrary();
}
function renderLibrary() {
  const chips = [""].concat(library.folders);
  $("folders").innerHTML = chips.map((f) => `<button data-folder="${esc(f)}" class="${f === folder ? "active" : ""}">${f ? esc(f) : "everything"}</button>`).join("");
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

/* ---- upload ----------------------------------------------------------- */
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
  $("about").textContent = `organ_web ${state.version}${state.dry_run ? " · dry run: nothing reaches the organ" : ""}`;
}
["set-tempo", "set-gap", "set-warm", "set-idle"].forEach((id) => $(id).oninput = () => { settingsDirty = true; $("set-tempo-v").textContent = `${$("set-tempo").value} %`; });
$("btn-save-settings").onclick = async () => {
  const body = { tempo: `${$("set-tempo").value}%`, gap: +$("set-gap").value, warm_up: +$("set-warm").value, idle_off: +$("set-idle").value };
  settingsDirty = false;
  await act("/api/settings", body, "settings saved");
};

/* ---- service ---------------------------------------------------------- */
$("btn-reset").onclick = () => { if (confirm("Reset all driver boards? Every note drops and the boards run their exercise routine.")) act("/api/service/reset", {}, "boards reset"); };
$("btn-pump-on").onclick = () => act("/api/service/pump", { on: true }, "pump on");
$("btn-pump-off").onclick = () => act("/api/service/pump", { on: false }, "pump off");

function renderService() {
  const pumpless = !state.pump.configured;
  $("btn-pump-on").disabled = pumpless; $("btn-pump-off").disabled = pumpless;
  $("service-note").textContent = pumpless ? "No pump relay configured (--pump GPIO)." : "";
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

/* ---- go --------------------------------------------------------------- */
let startTab = "play";
try { startTab = localStorage.getItem("organ-tab") || "play"; } catch (e) { /* fine */ }
showTab(startTab);
refresh();
setInterval(refresh, 1000);
