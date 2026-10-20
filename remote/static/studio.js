// AI Director Studio: talks to remote/server.py. Everything lives on the GPU machine;
// the browser only keeps the access token and a few UI choices in localStorage.
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("studio_" + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("studio_" + k, JSON.stringify(v)); } catch (e) {} },
};

// ---------------------------------------------------------------- token + api
const params = new URLSearchParams(location.search);
let token = params.get("t") || "";
try {
  if (token) localStorage.setItem("kfa_token", token); else token = localStorage.getItem("kfa_token") || "";
} catch (e) {}
if (params.has("t")) history.replaceState(null, "", location.pathname + location.hash);

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), "X-Token": token } });
  if (res.status === 401) { $("noToken").classList.remove("hidden"); throw new Error("not authorised"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}
const withToken = (url) => url + (url.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(token);
const imgUrl = (id) => withToken(`/api/keyframes/image/${id}`);
const jobImg = (j, kind, name) => withToken(`/api/jobs/${j.id}/image/${kind}/${name}`);
// A video's current version: with its soundtrack once it has one (?v= busts the cache after a remix).
const jobVideo = (j, version, download) => {
  const mixed = j.audio?.mixed;
  version = version || (mixed ? "mix" : "original");
  return withToken(`/api/jobs/${j.id}/video?version=${version}` + (mixed ? `&v=${Math.round(mixed)}` : "") + (download ? "&download=1" : ""));
};

function toast(msg) {
  const t = $("toast"); t.textContent = msg; t.classList.remove("hidden");
  clearTimeout(toast.timer); toast.timer = setTimeout(() => t.classList.add("hidden"), 4000);
}

const ago = (t) => {
  const s = Date.now() / 1000 - t;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(t * 1000).toLocaleDateString();
};
const fmtTime = (s) => s < 60 ? `${Math.round(s)}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
const licenseClass = (text) => /commercial OK/i.test(text) ? "ok" : /non-commercial|not licensed/i.test(text) ? "bad" : "warn";

// ---------------------------------------------------------------- state
const S = {
  info: null, images: [], jobs: [],
  pending: [],                       // image generations in flight: {task, prompt, error}
  imgMode: "text", source: null,     // edit mode: library image id to edit
  aspect: store.get("aspect", "16:9"), count: 1,
  imageModel: store.get("image_model", "klein4b"),
  tray: store.get("tray", []),       // library image ids for the next video, in order
  motion: store.get("motion", []),   // one motion prompt per gap
  style: "photoreal", restyleTouched: false,
  libFilter: "all",
  characters: [],
  cast: store.get("cast", []),       // character ids tagged for the next images
};
const imageById = (id) => S.images.find((m) => m.id === id);
const charById = (id) => S.characters.find((c) => c.id === id);
const modelLabel = (id) => (S.info?.image_models.find((m) => m.id === id) || {}).label || id || "";

// ---------------------------------------------------------------- router
const TITLES = { home: "Home", image: "Create image", video: "Create video", library: "Library", director: "Director", characters: "Characters" };
function route() {
  const name = (location.hash.match(/^#\/(\w+)/) || [])[1];
  const page = TITLES[name] ? name : "home";
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("hidden", p.dataset.page !== page));
  document.querySelectorAll("[data-route]").forEach((a) => a.classList.toggle("on", a.dataset.route === page));
  $("pageTitle").textContent = TITLES[page];
  document.title = `${TITLES[page]} · AI Director Studio`;
  S.page = page;
  renderAll();
  window.scrollTo(0, 0);
}
window.addEventListener("hashchange", route);
const go = (page) => { if (location.hash === "#/" + page) route(); else location.hash = "#/" + page; };

// ---------------------------------------------------------------- tiles
function imageTile(m, acts = true) {
  const tag = m.kind === "upload" ? "Upload" : m.kind === "edit" ? "Edit" : "";
  const ratio = m.width && m.height ? ` style="aspect-ratio:${m.width}/${m.height}"` : "";
  return `<div class="tile" data-img="${m.id}" tabindex="0">
    <img loading="lazy" alt="${esc(m.prompt || "image")}" src="${imgUrl(m.id)}"${ratio}>
    ${tag ? `<span class="tag">${tag}</span>` : ""}
    ${acts ? `<div class="acts"><button type="button" data-act="edit">Edit</button><button type="button" data-act="animate">Animate</button></div>` : ""}
  </div>`;
}
function videoTile(j) {
  const poster = j.styled.length ? jobImg(j, "styled", j.styled[0]) : jobImg(j, "input", j.inputs[0]);
  return `<div class="tile" data-job="${j.id}" tabindex="0">
    <video muted loop playsinline preload="none" poster="${poster}" src="${jobVideo(j)}"></video>
    <span class="tag"><svg><use href="#i-video"/></svg>Video</span>
  </div>`;
}
function pendingTile(p) {
  if (p.error) return `<div class="tile failed" data-pending="${p.key}" title="Click to dismiss"><b>Failed</b><br>${esc(p.error)}</div>`;
  return `<div class="tile pending"><div class="p"><div class="spinner"></div>${esc(p.task?.progress || "starting")}</div></div>`;
}
function wireHoverPlay(box) {
  box.querySelectorAll("video").forEach((v) => {
    const tile = v.closest(".tile");
    tile.onmouseenter = () => v.play().catch(() => {});
    tile.onmouseleave = () => { v.pause(); };
  });
}
// Re-render a container only when its content changed (keeps scroll position and playing videos).
function setHtml(el, html) { if (el.dataset.sig !== html) { el.innerHTML = html; el.dataset.sig = html; return true; } return false; }

function emptyState(text, href, label) {
  return `<div class="empty" style="column-span:all">${text}${href ? `<br><a class="btn" href="${href}">${label}</a>` : ""}</div>`;
}

// ---------------------------------------------------------------- home
function renderHome() {
  const videos = S.jobs.filter((j) => j.status === "done").map((j) => ({ t: j.finished || j.created, html: videoTile(j) }));
  const imgs = S.images.map((m) => ({ t: m.created, html: imageTile(m) }));
  const items = [...videos, ...imgs].sort((a, b) => b.t - a.t).slice(0, 20);
  const box = $("homeGrid");
  if (setHtml(box, items.length ? items.map((i) => i.html).join("") : emptyState("Nothing here yet. Describe a first frame above to start.")))
    wireHoverPlay(box);
}
$("heroForm").onsubmit = (e) => {
  e.preventDefault();
  const text = $("heroPrompt").value.trim();
  setImgMode("text");
  if (text) $("imgPrompt").value = text;
  go("image");
  if (text) generateImages();
};
$("heroPrompt").onkeydown = (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("heroForm").requestSubmit(); } };

// ---------------------------------------------------------------- create image
const ASPECTS = [["16:9", 32, 18], ["9:16", 14, 25], ["1:1", 22, 22], ["4:3", 28, 21], ["3:4", 21, 28]];
function renderAspects() {
  $("aspects").innerHTML = ASPECTS.map(([id, w, h]) =>
    `<button type="button" class="aspect${S.aspect === id ? " on" : ""}" data-aspect="${id}"><i style="width:${w}px;height:${h}px"></i>${id}</button>`).join("");
}
$("aspects").onclick = (e) => {
  const b = e.target.closest("[data-aspect]"); if (!b) return;
  S.aspect = b.dataset.aspect; store.set("aspect", S.aspect); renderAspects();
};
function renderCounts() {
  $("counts").innerHTML = [1, 2, 3, 4].map((n) => `<button type="button" class="chip${S.count === n ? " on" : ""}" data-n="${n}">${n}</button>`).join("");
}
$("counts").onclick = (e) => { const b = e.target.closest("[data-n]"); if (b) { S.count = +b.dataset.n; renderCounts(); } };

function setImgMode(mode, source) {
  S.imgMode = mode;
  if (source !== undefined) S.source = source;
  document.querySelectorAll("#imgMode button").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  const edit = mode === "edit";
  $("editSourceBox").classList.toggle("hidden", !edit);
  $("aspectBox").classList.toggle("hidden", edit);
  $("imgPrompt").placeholder = edit
    ? "What changes? e.g. she turns toward the camera and smiles"
    : "Describe the scene and its style, e.g. a red fox in a snowy forest at dusk, side view, anime style";
  $("imgPromptHint").textContent = edit
    ? "Describe only the change: one pose or camera change per edit. Everything else is kept, so a chain of edits gives consistent keyframes."
    : "Put the style in the prompt. Generate a few, then use Edit on the best one to make the next keyframe.";
  renderSource();
}
$("imgMode").onclick = (e) => { const b = e.target.closest("[data-mode]"); if (b) setImgMode(b.dataset.mode); };
function renderSource() {
  const m = S.source && imageById(S.source);
  $("editSource").innerHTML = m
    ? `<img alt="" src="${imgUrl(m.id)}"><div class="sp-text"><b>${m.kind === "upload" ? "Your image" : "Generated image"}</b>${esc((m.prompt || m.name || "").slice(0, 90))}</div>
       <button type="button" class="btn small" data-pick-source>Change</button>`
    : `<div class="sp-text"><b>No image chosen</b>Pick one from your library or upload a photo.</div><button type="button" class="btn small" data-pick-source>Choose</button>`;
}
$("editSource").onclick = (e) => { if (e.target.closest("[data-pick-source]")) openPicker("source"); };

function renderImageModels() {
  const models = S.info.image_models;
  if (!models.some((m) => m.id === S.imageModel && !m.missing.length)) S.imageModel = "klein4b";
  document.querySelectorAll("select.imageModel").forEach((sel) => {
    sel.innerHTML = models.map((m) => `<option value="${m.id}"${m.missing.length ? " disabled" : ""}>${esc(m.label)}${m.missing.length ? " (not installed)" : ""}</option>`).join("");
    sel.value = S.imageModel;
    sel.onchange = () => {
      S.imageModel = sel.value; store.set("image_model", S.imageModel);
      document.querySelectorAll("select.imageModel").forEach((o) => { o.value = S.imageModel; });
      renderModelLicense();
    };
  });
  renderModelLicense();
}
function renderModelLicense() {
  const m = S.info.image_models.find((x) => x.id === S.imageModel);
  const el = $("imgModelLicense");
  el.className = "license " + licenseClass(m.license); el.textContent = "License: " + m.license;
}

async function generateImages() {
  const prompt = $("imgPrompt").value.trim();
  const edit = S.imgMode === "edit";
  if (!prompt) return toast(edit ? "Describe what changes" : "Describe the image");
  if (edit && !S.source) return toast("Choose an image to edit");
  const seed0 = $("imgSeed").value.trim();
  for (let i = 0; i < S.count; i++) {
    const p = { key: Math.random().toString(36).slice(2), prompt };
    S.pending.unshift(p);
    try {
      p.task = await api("/api/keyframes/generate", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, source: edit ? S.source : null, aspect: S.aspect, model: S.imageModel,
                               seed: seed0 ? String(+seed0 + i) : null, characters: S.cast }) });
    } catch (e) { p.error = e.message; }
    renderImagePage();
  }
  pollTasks();
}
$("imgGo").onclick = generateImages;
$("imgPrompt").onkeydown = (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) generateImages(); };

let polling = false;
async function pollTasks() {
  if (polling) return;
  polling = true;
  try {
    while (S.pending.some((p) => p.task && p.task.status === "running")) {
      await new Promise((r) => setTimeout(r, 1500));
      let finished = false;
      for (const p of S.pending.filter((x) => x.task && x.task.status === "running")) {
        try { p.task = await api(`/api/keyframes/tasks/${p.task.id}`); } catch (e) { p.error = e.message; p.task.status = "failed"; }
        if (p.task.status === "failed") p.error = p.error || p.task.error || "failed";
        if (p.task.status === "done") finished = true;
      }
      if (finished) {
        await refreshImages();
        S.pending = S.pending.filter((p) => !(p.task && p.task.status === "done"));
      }
      renderImagePage();
    }
  } finally { polling = false; }
}

// ---------------------------------------------------------------- characters on Create image
function renderCastPick() {
  S.cast = S.cast.filter((id) => charById(id) || !S.charactersLoaded);
  setHtml($("castPick"), S.characters.length
    ? S.characters.map((c) => `<button type="button" class="cast-chip${S.cast.includes(c.id) ? " on" : ""}" data-cast="${c.id}">
        ${c.refs[0] ? `<img alt="" src="${imgUrl(c.refs[0])}">` : ""}${esc(c.name)}</button>`).join("")
    : `<p class="hint" style="margin:0">No characters yet. <a href="#/characters">Create one</a> from a reference image to keep them consistent.</p>`);
}
$("castPick").onclick = (e) => {
  const b = e.target.closest("[data-cast]"); if (!b) return;
  const id = b.dataset.cast, i = S.cast.indexOf(id);
  if (i >= 0) S.cast.splice(i, 1);
  else if (S.cast.length >= 3) return toast("Up to 3 characters per image");
  else S.cast.push(id);
  store.set("cast", S.cast); renderCastPick();
};

function renderImagePage() {
  renderCastPick();
  setHtml($("imgPending"), S.pending.map(pendingTile).join(""));
  const html = S.images.map((m) => imageTile(m)).join("");
  setHtml($("imgGrid"), html || (S.pending.length ? "" : emptyState("Your generated and uploaded images appear here.")));
  $("imgCount").textContent = S.images.length ? `${S.images.length} image${S.images.length === 1 ? "" : "s"}` : "";
  const busy = S.pending.filter((p) => !p.error).length;
  $("imgGo").innerHTML = `<svg><use href="#i-spark"/></svg>` + (busy ? `Generate (${busy} running)` : "Generate");
}
$("imgPending").addEventListener("click", (e) => {
  const failed = e.target.closest("[data-pending]");
  if (failed) { S.pending = S.pending.filter((p) => p.key !== failed.dataset.pending); renderImagePage(); }
});

// ---------------------------------------------------------------- image actions (all grids)
function editImage(id) {
  setImgMode("edit", id);
  $("imgPrompt").value = "";
  go("image");
  setTimeout(() => $("imgPrompt").focus(), 50);
}
function addToTray(ids) {
  const room = 12 - S.tray.length;
  if (room <= 0) return toast("Up to 12 keyframes per video");
  S.tray.push(...ids.slice(0, room)); store.set("tray", S.tray);
  if (ids.length > room) toast("Up to 12 keyframes per video");
  renderVideoPage();
}
async function deleteImage(id) {
  if (!confirm("Delete this image from the library?")) return false;
  try {
    await api(`/api/images/${id}`, { method: "DELETE" });
    S.tray = S.tray.filter((x) => x !== id); store.set("tray", S.tray);
    if (S.source === id) S.source = null;
    await refreshImages(); renderAll(); return true;
  } catch (e) { toast(e.message); return false; }
}
document.addEventListener("click", (e) => {
  const tile = e.target.closest(".tile[data-img], .tile[data-job]");
  if (!tile || tile.closest("#picker")) return;
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (tile.dataset.img) {
    const id = tile.dataset.img;
    if (act === "edit") editImage(id);
    else if (act === "animate") { addToTray([id]); toast("Added to Create video"); }
    else openImage(id);
  } else openVideo(tile.dataset.job);
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {  // the picker can sit on top of the character sheet: close only the top one
    if (!$("picker").classList.contains("hidden")) return closeOverlay("picker");
    closeOverlay("lightbox"); closeOverlay("charSheet"); closeOverlay("soundSheet");
  }
  if (e.key === "Enter" && e.target.matches?.(".tile")) e.target.click();
});

// ---------------------------------------------------------------- lightbox
function closeOverlay(id) {
  const o = $(id); if (o.classList.contains("hidden")) return;
  o.classList.add("hidden"); o.querySelectorAll("video").forEach((v) => v.pause());
  if (id === "lightbox") $("lbMedia").innerHTML = "";
}
document.querySelectorAll(".overlay").forEach((o) => o.addEventListener("click", (e) => {
  if (e.target === o || e.target.closest("[data-close]")) closeOverlay(o.id);
}));

function openImage(id) {
  const m = imageById(id); if (!m) return;
  $("lbMedia").innerHTML = `<img alt="" src="${imgUrl(m.id)}">`;
  const kind = { text: "Text to image", edit: "Edit", upload: "Uploaded" }[m.kind] || "Image";
  const cast = (m.characters || []).map((c) => c.name).join(", ");
  const rows = [["Type", kind], ["Model", modelLabel(m.model)], ["Characters", cast], ["Size", m.width ? `${m.width} × ${m.height}` : ""],
                ["Seed", m.seed ?? ""], ["Created", ago(m.created)]].filter(([, v]) => v !== "" && v !== undefined);
  $("lbInfo").innerHTML = `<h3>${kind}</h3>
    ${m.prompt ? `<div class="prompt">${esc(m.prompt)}</div>` : ""}
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <div class="acts">
      <button type="button" class="btn" data-lb="edit">Edit</button>
      <button type="button" class="btn" data-lb="animate">Animate</button>
      ${m.prompt ? `<button type="button" class="btn" data-lb="reuse">Reuse prompt</button>` : ""}
      <button type="button" class="btn" data-lb="character">New character</button>
      <a class="btn" href="${imgUrl(m.id)}" download="image-${m.id}.png">Download</a>
      <button type="button" class="btn danger" data-lb="delete">Delete</button>
    </div>`;
  $("lbInfo").onclick = async (e) => {
    const a = e.target.closest("[data-lb]")?.dataset.lb; if (!a) return;
    if (a === "edit") { closeOverlay("lightbox"); editImage(id); }
    else if (a === "animate") { addToTray([id]); toast("Added to Create video"); }
    else if (a === "reuse") {
      closeOverlay("lightbox");
      setImgMode(m.kind === "edit" && m.source ? "edit" : "text", m.kind === "edit" ? m.source : undefined);
      $("imgPrompt").value = m.prompt; if (m.model) { S.imageModel = m.model; renderImageModels(); }
      S.cast = (m.characters || []).map((c) => c.id).filter(charById); store.set("cast", S.cast);
      go("image");
    } else if (a === "character") { closeOverlay("lightbox"); openCharacter(null, [id]); }
    else if (a === "delete" && await deleteImage(id)) closeOverlay("lightbox");
  };
  $("lightbox").classList.remove("hidden");
}

function jobSummary(j) {
  const s = j.settings;
  const style = s.custom_style || (S.info?.styles.find((x) => x.id === s.style) || {}).label || s.style;
  const engine = (S.info?.engines.find((e) => e.id === s.engine) || {}).label || s.engine;
  return { style, engine, text: `${engine} · ${style} · ${j.inputs.length} keyframes${s.loop ? " · loop" : ""} · ${s.fast ? "fast" : "quality"}` };
}
function openVideo(id) {
  const j = S.jobs.find((x) => x.id === id); if (!j) return;
  const s = j.settings, sum = jobSummary(j);
  $("lbMedia").innerHTML = `<video controls autoplay loop playsinline src="${jobVideo(j)}"></video>`;
  const rows = [["Engine", sum.engine], ["Style", sum.style], ["Keyframes", j.inputs.length], ["Mode", s.fast ? "Fast" : "Quality"],
                ["Seed", s.seed ?? "random"], ["Render time", j.started && j.finished ? fmtTime(j.finished - j.started) : ""], ["Created", ago(j.created)]]
    .filter(([, v]) => v !== "");
  $("lbInfo").innerHTML = `<h3>Video</h3>
    <div class="prompt">${s.prompts.map((p, i) => `${i + 1} → ${i + 2}: ${esc(p)}`).join("\n")}</div>
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <div class="acts">
      <button type="button" class="btn" data-lb="sound">Sound</button>
      <a class="btn" href="${jobVideo(j, null, true)}">Download</a>
      <button type="button" class="btn danger" data-lb="delete">Delete</button>
    </div>`;
  $("lbInfo").onclick = async (e) => {
    const a = e.target.closest("[data-lb]")?.dataset.lb;
    if (a === "sound") openSound(id);
    else if (a === "delete" && await deleteJob(id)) closeOverlay("lightbox");
  };
  $("lightbox").classList.remove("hidden");
}

// ---------------------------------------------------------------- sound (ElevenLabs layers, opt-in)
const SND = { job: null, kind: "voice", version: "original", lines: [], sig: "", video: "" };
const SND_KINDS = { voice: "Voice-over", dialogue: "Dialogue", sfx: "Effect", music: "Music" };
const sndJob = () => S.jobs.find((x) => x.id === SND.job);
const sndAudio = (j) => j.audio || { tracks: [], dubs: {}, video_volume: 1, mixed: null };

async function loadVoices() {
  if (S.voices || !S.info?.elevenlabs?.enabled) return;
  try { S.voices = await api("/api/voices"); } catch (e) { toast(e.message); }
}
const voiceOptions = (sel) => (S.voices || []).map((v) =>
  `<option value="${esc(v.id)}"${v.id === sel ? " selected" : ""}>${esc(v.name)}${v.labels ? ` · ${esc(v.labels)}` : ""}</option>`).join("")
  || `<option value="">No voices loaded</option>`;

async function openSound(id) {
  const j = S.jobs.find((x) => x.id === id); if (!j) return;
  const el = S.info.elevenlabs;
  SND.job = id; SND.sig = ""; SND.video = ""; SND.version = sndAudio(j).mixed ? "mix" : "original";
  $("sndLicense").textContent = "ElevenLabs: " + el.license;
  $("sndOff").classList.toggle("hidden", el.enabled);
  $("sndGo").disabled = $("sndDub").disabled = !el.enabled;
  $("sndLang").innerHTML = Object.entries(el.languages).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("");
  $("sndLang").value = store.get("dub_lang", "es");
  closeOverlay("lightbox");
  $("soundSheet").classList.remove("hidden");
  renderSound();
  await loadVoices();
  $("sndVoice").innerHTML = voiceOptions(store.get("voice"));
  if (!SND.lines.length) SND.lines = [{ voice_id: store.get("voice"), text: "" }, { voice_id: "", text: "" }];
  renderLines();
}

function renderLines() {
  $("sndLines").innerHTML = SND.lines.map((l, i) => `<div class="line-row" data-line="${i}">
    <select data-f="voice_id" aria-label="Voice for line ${i + 1}">${voiceOptions(l.voice_id)}</select>
    <textarea data-f="text" placeholder="Line ${i + 1}">${esc(l.text)}</textarea>
    <button type="button" class="btn small ghost" data-del-line aria-label="Remove line">✕</button></div>`).join("");
}
function readLines() {
  SND.lines = [...$("sndLines").querySelectorAll(".line-row")].map((r) =>
    ({ voice_id: r.querySelector("select").value, text: r.querySelector("textarea").value }));
}
$("sndAddLine").onclick = () => { readLines(); SND.lines.push({ voice_id: SND.lines.at(-2)?.voice_id || "", text: "" }); renderLines(); };
$("sndLines").onclick = (e) => {
  const row = e.target.closest("[data-del-line]")?.parentElement; if (!row) return;
  readLines(); SND.lines.splice(+row.dataset.line, 1); renderLines();
};
$("sndKinds").onclick = (e) => {
  const b = e.target.closest("[data-kind]"); if (!b) return;
  SND.kind = b.dataset.kind;
  $("sndKinds").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  document.querySelectorAll(".snd-form").forEach((f) => f.classList.toggle("hidden", f.dataset.form !== SND.kind));
};

function trackRow(j, t) {
  const state = t.status === "generating" ? `<span class="status running">generating</span>` :
                t.status === "failed" ? `<span class="status failed">failed</span>` :
                `<span class="status done">${t.length ? t.length.toFixed(1) + " s" : "ready"}</span>`;
  return `<div class="snd-track" data-track="${t.id}">
    <div class="head"><b>${SND_KINDS[t.kind]}</b><span class="txt" title="${esc(t.label)}">${esc(t.label)}</span>${state}</div>
    ${t.error ? `<div class="error">${esc(t.error)}</div>` : ""}
    ${t.status === "done" ? `<audio controls preload="none" src="${withToken(`/api/jobs/${j.id}/audio/${t.file}`)}"></audio>` : ""}
    <div class="ctl"><label>Start (s)<input type="number" data-f="start" min="0" step="0.1" value="${t.start}"></label>
      <label>Volume ${Math.round(t.volume * 100)}%<input type="range" data-f="volume" min="0" max="2" step="0.05" value="${t.volume}"></label>
      <button type="button" class="btn small danger" data-del>Remove</button></div>
  </div>`;
}

function renderSound() {
  const j = sndJob(); if (!j) { closeOverlay("soundSheet"); return; }
  const a = sndAudio(j), langs = S.info.elevenlabs.languages;
  const dubs = Object.entries(a.dubs);
  if (SND.version !== "original" && SND.version !== "mix" && a.dubs[SND.version]?.status !== "done") SND.version = a.mixed ? "mix" : "original";
  if (SND.version === "mix" && !a.mixed) SND.version = "original";
  const src = jobVideo(j, SND.version);
  if (src !== SND.video) {  // only reload the player when the file changed
    SND.video = src;
    $("sndVideo").innerHTML = `<video controls playsinline loop src="${src}"></video>`;
  }
  const sig = JSON.stringify([a, SND.version]);
  if (sig === SND.sig || document.activeElement?.closest?.("#sndTracks")) return;  // don't clobber a field being edited
  SND.sig = sig;
  const versions = [["original", "Original"], ...(a.mixed ? [["mix", "With sound"]] : []),
                    ...dubs.filter(([, d]) => d.status === "done").map(([k]) => [k, langs[k] || k])];
  $("sndVersions").innerHTML = versions.map(([k, label]) =>
    `<button type="button" class="chip${k === SND.version ? " on" : ""}" data-version="${k}">${esc(label)}</button>`).join("");
  $("sndDownload").innerHTML = `<a class="btn small" href="${jobVideo(j, SND.version, true)}">Download this version</a>`;
  const hasOwnSound = j.settings.engine === "minimax-h3";
  $("sndTracks").innerHTML = (hasOwnSound ? `<div class="snd-track" data-track="video">
      <div class="head"><b>Render</b><span class="txt">the video's own generated sound</span></div>
      <div class="ctl"><span></span><label>Volume ${Math.round(a.video_volume * 100)}%<input type="range" data-f="volume" min="0" max="2" step="0.05" value="${a.video_volume}"></label><span></span></div>
    </div>` : "") + (a.tracks.map((t) => trackRow(j, t)).join("") || `<div class="snd-empty">No layers yet. Add a voice-over, dialogue, sound effect or music below.</div>`);
  $("sndDubs").innerHTML = dubs.map(([k, d]) => `<div class="snd-track" data-dub="${k}">
      <div class="head"><b>${esc(langs[k] || k)}</b><span class="txt">${d.status === "working" ? esc(d.progress || "working") : d.stale ? "out of date: the speech layers changed since, dub again" : ""}</span>
        <span class="status ${d.status === "working" ? "running" : d.status}">${d.status === "working" ? "dubbing" : d.status}</span></div>
      ${d.error ? `<div class="error">${esc(d.error)}</div>` : ""}
      <div class="row"><button type="button" class="btn small danger" data-del-dub>Remove</button></div>
    </div>`).join("");
}

$("sndVersions").onclick = (e) => {
  const b = e.target.closest("[data-version]"); if (!b) return;
  SND.version = b.dataset.version; SND.sig = ""; renderSound();
};
async function soundChanged() { await refreshJobs(); SND.sig = ""; renderSound(); }
$("sndTracks").onchange = async (e) => {
  const f = e.target.dataset.f, id = e.target.closest("[data-track]")?.dataset.track; if (!f || !id) return;
  try {
    await api(`/api/jobs/${SND.job}/audio/${id}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [f]: +e.target.value }) });
    e.target.blur(); await soundChanged();
  } catch (err) { toast(err.message); }
};
$("sndTracks").onclick = async (e) => {
  const id = e.target.closest("[data-del]") && e.target.closest("[data-track]").dataset.track; if (!id) return;
  try { await api(`/api/jobs/${SND.job}/audio/${id}`, { method: "DELETE" }); await soundChanged(); } catch (err) { toast(err.message); }
};
$("sndDubs").onclick = async (e) => {
  const lang = e.target.closest("[data-del-dub]") && e.target.closest("[data-dub]").dataset.dub; if (!lang) return;
  try { await api(`/api/jobs/${SND.job}/dubs/${lang}`, { method: "DELETE" }); await soundChanged(); } catch (err) { toast(err.message); }
};

$("sndGo").onclick = async () => {
  const k = SND.kind, body = { kind: k, start: +$("sndStart").value || 0 };
  if (k === "voice") { body.voice_id = $("sndVoice").value; body.text = $("sndVoiceText").value; store.set("voice", body.voice_id); }
  if (k === "dialogue") { readLines(); body.lines = SND.lines; }
  if (k === "sfx") { body.text = $("sndSfxText").value; body.duration = $("sndSfxLen").value || null; body.loop = $("sndSfxLoop").checked; }
  if (k === "music") { body.prompt = $("sndMusicText").value; body.seconds = $("sndMusicLen").value || null; body.instrumental = $("sndMusicInst").checked; }
  const btn = $("sndGo"); btn.disabled = true;
  try {
    await api(`/api/jobs/${SND.job}/audio`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    toast(`${SND_KINDS[k]} is generating (uses ElevenLabs credits).`);
    await soundChanged();
  } catch (e) { toast(e.message); } finally { btn.disabled = false; }
};
$("sndDub").onclick = async () => {
  const lang = $("sndLang").value; store.set("dub_lang", lang);
  if (!confirm(`Dub the voice-over and dialogue into ${S.info.elevenlabs.languages[lang]}? Dubbing uses ElevenLabs credits per minute of audio.`)) return;
  try {
    await api(`/api/jobs/${SND.job}/dubs`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ language: lang }) });
    await soundChanged();
  } catch (e) { toast(e.message); }
};

// ---------------------------------------------------------------- picker (library images)
const picker = { mode: "tray", sel: [] };
function openPicker(mode) {
  picker.mode = mode; picker.sel = [];
  $("pickerTitle").textContent = { source: "Choose the image to edit", refs: "Add reference images" }[mode] || "Add keyframes, in order";
  $("pickerDone").textContent = mode === "source" ? "Use image" : "Add";
  renderPicker();
  $("picker").classList.remove("hidden");
}
function renderPicker() {
  $("pickerGrid").innerHTML = S.images.length ? S.images.map((m) => {
    const n = picker.sel.indexOf(m.id);
    return `<div class="tile${n >= 0 ? " sel" : ""}" data-pick="${m.id}"><img loading="lazy" alt="" src="${imgUrl(m.id)}">
      ${n >= 0 && picker.mode !== "source" ? `<span class="num">${n + 1}</span>` : ""}</div>`;
  }).join("") : `<div class="empty" style="grid-column:1/-1">No images yet. Upload some, or make them in Create image.</div>`;
  $("pickerDone").disabled = !picker.sel.length;
}
$("pickerGrid").onclick = (e) => {
  const t = e.target.closest("[data-pick]"); if (!t) return;
  const id = t.dataset.pick, i = picker.sel.indexOf(id);
  if (picker.mode === "source") picker.sel = [id];
  else if (i >= 0) picker.sel.splice(i, 1); else picker.sel.push(id);
  renderPicker();
};
$("pickerDone").onclick = () => {
  if (picker.mode === "source") { S.source = picker.sel[0]; renderSource(); }
  else if (picker.mode === "refs") addRefs(picker.sel);
  else addToTray(picker.sel);
  closeOverlay("picker");
};
async function uploadImages(files) {
  const ids = [];
  for (const file of files) {
    const form = new FormData(); form.append("image", file, file.name);
    try { ids.push((await api("/api/keyframes/upload", { method: "POST", body: form })).image_id); }
    catch (e) { toast(`Upload of ${file.name} failed: ${e.message}`); }
  }
  await refreshImages();
  return ids;
}
$("pickerUpload").onchange = async (e) => {
  const files = [...e.target.files]; e.target.value = "";
  toast(`Uploading ${files.length} image${files.length > 1 ? "s" : ""}…`);
  const ids = await uploadImages(files);
  picker.sel = picker.mode === "source" ? ids.slice(-1) : [...picker.sel, ...ids];
  renderPicker(); renderAll();
};
$("libUpload").onchange = async (e) => {
  const files = [...e.target.files]; e.target.value = "";
  toast(`Uploading ${files.length} image${files.length > 1 ? "s" : ""}…`);
  await uploadImages(files); renderAll();
};

// ---------------------------------------------------------------- create video
function renderTray() {
  S.tray = S.tray.filter((id) => imageById(id) || !S.imagesLoaded);
  const tiles = S.tray.map((id, i) => `<div class="kf"><img alt="" src="${imgUrl(id)}"><span class="n">${i + 1}</span>
    <div class="kt"><button type="button" data-k="left" data-i="${i}" aria-label="Move earlier">◀</button>
    <button type="button" data-k="del" data-i="${i}" aria-label="Remove">✕</button>
    <button type="button" data-k="right" data-i="${i}" aria-label="Move later">▶</button></div></div>`).join("");
  setHtml($("tray"), tiles + (S.tray.length < 12 ? `<button type="button" class="add" data-k="add"><span><svg><use href="#i-plus"/></svg><br>Add</span></button>` : ""));
  $("trayCount").textContent = S.tray.length ? `${S.tray.length} of 12` : "";
}
$("tray").onclick = (e) => {
  const b = e.target.closest("[data-k]"); if (!b) return;
  const k = b.dataset.k, i = +b.dataset.i;
  if (k === "add") return openPicker("tray");
  if (k === "del") { S.tray.splice(i, 1); S.motion.splice(Math.min(i, S.motion.length), 1); }
  else {
    const j = k === "left" ? i - 1 : i + 1;
    if (j >= 0 && j < S.tray.length) [S.tray[i], S.tray[j]] = [S.tray[j], S.tray[i]];
  }
  store.set("tray", S.tray); store.set("motion", S.motion); renderVideoPage();
};
function renderMotion() {
  const n = Math.max(0, S.tray.length - ($("loop").checked ? 0 : 1));
  const box = $("motion");
  const sig = `${n}|${S.tray.join(",")}`;
  if (box.dataset.sig === sig) return;  // don't wipe what is being typed
  box.dataset.sig = sig;
  if (!n) { box.innerHTML = `<p class="hint" style="margin:0">Add at least two keyframes.</p>`; return; }
  box.innerHTML = Array.from({ length: n }, (_, i) => {
    const a = S.tray[i], b = S.tray[(i + 1) % S.tray.length];
    return `<div class="gap"><div class="pair"><img alt="" src="${imgUrl(a)}">${i + 1} → ${(i + 1) % S.tray.length + 1}<img alt="" src="${imgUrl(b)}"></div>
      <textarea data-gap="${i}" placeholder="${i ? "Same as above" : "What moves, and the camera, e.g. she turns and smiles, slow push-in"}">${esc(S.motion[i] || "")}</textarea></div>`;
  }).join("");
}
$("motion").oninput = (e) => {
  const t = e.target.closest("[data-gap]"); if (!t) return;
  S.motion[+t.dataset.gap] = t.value; store.set("motion", S.motion);
};
$("loop").onchange = renderMotion;

function renderStyles() {
  $("styles").innerHTML = [...S.info.styles, { id: "__custom", label: "Custom…" }].map((s) =>
    `<button type="button" class="chip${S.style === s.id ? " on" : ""}" data-style="${s.id}">${esc(s.label)}</button>`).join("");
}
$("styles").onclick = (e) => {
  const b = e.target.closest("[data-style]"); if (!b) return;
  S.style = b.dataset.style;
  const st = S.info.styles.find((x) => x.id === S.style);
  $("customStyle").classList.toggle("hidden", S.style !== "__custom");
  if (!S.restyleTouched) $("restyle").checked = S.style === "__custom" || !!st?.restyle;
  $("restyleModelBox").classList.toggle("hidden", !$("restyle").checked);
  renderStyles();
};
$("restyle").onchange = () => { S.restyleTouched = true; $("restyleModelBox").classList.toggle("hidden", !$("restyle").checked); };

function renderEngines() {
  const sel = $("engine");
  sel.innerHTML = S.info.engines.map((e) =>
    `<option value="${e.id}"${e.ready ? "" : " disabled"}>${esc(e.label)}${e.ready ? "" : " (not installed)"}</option>`).join("");
  sel.value = "wan22";  // commercial-safe default; MiniMax H3 only by explicit choice
  sel.onchange();
}
$("engine").onchange = () => {
  const e = S.info.engines.find((x) => x.id === $("engine").value);
  const h3 = e.id === "minimax-h3";
  $("soundBox").classList.toggle("hidden", !h3);
  $("smooth").checked = !h3;  // H3 already renders 24 fps
  const el = $("engineLicense");
  el.className = "license " + licenseClass(e.license);
  el.textContent = (h3 ? "24 fps with generated sound. " : "16 fps, silent. ") + "License: " + e.license;
};

async function renderVideoJob() {
  if (S.tray.length < 2) return toast("Add at least two keyframes");
  const custom = S.style === "__custom" ? $("customStyle").value.trim() : "";
  if (S.style === "__custom" && !custom) return toast("Describe the custom style");
  const n = S.tray.length - ($("loop").checked ? 0 : 1);
  const settings = {
    prompts: Array.from({ length: n }, (_, i) => S.motion[i] || ""),
    engine: $("engine").value, image_model: S.imageModel,
    audio: $("audio").checked, sound: $("sound").value,
    style: S.style === "__custom" ? "photoreal" : S.style, custom_style: custom,
    restyle: $("restyle").checked, fast: $("vmode").value === "fast",
    length: +$("length").value, size: $("size").value,
    loop: $("loop").checked, smooth: $("smooth").checked, seed: $("vseed").value,
  };
  const form = new FormData();
  S.tray.forEach((id) => form.append("keyframe_id", id));
  form.append("settings", JSON.stringify(settings));
  const btn = $("vidGo"); btn.disabled = true;
  try {
    await api("/api/jobs", { method: "POST", body: form });
    toast("Queued on the GPU");
    await refreshJobs(); renderVideoPage();
    window.scrollTo({ top: $("jobList").offsetTop - 80, behavior: "smooth" });
  } catch (e) { toast("Failed: " + e.message); }
  finally { btn.disabled = false; }
}
$("vidGo").onclick = renderVideoJob;

function jobCard(j) {
  const sum = jobSummary(j);
  const end = j.finished || Date.now() / 1000, took = j.started ? fmtTime(end - j.started) : "";
  const imgs = (j.styled.length ? j.styled.map((n) => ["styled", n]) : j.inputs.map((n) => ["input", n]))
    .map(([k, n]) => `<img loading="lazy" alt="" src="${jobImg(j, k, n)}">`).join("");
  const progress = j.status === "queued" ? `Waiting · position ${j.queue_position || "?"}` :
                   j.status === "running" ? `${j.progress} · ${took}` : (took ? `Took ${took}` : "");
  const active = ["queued", "running"].includes(j.status);
  return `<div class="job" data-id="${j.id}">
    <div class="job-head"><span class="status ${j.status}">${j.status}</span><span class="job-meta">${esc(sum.text)}</span>
      <span class="job-time">${new Date(j.created * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" })}</span></div>
    ${progress ? `<div class="progress">${esc(progress)}</div>` : ""}
    ${active ? `<div class="bar"><span></span></div>` : ""}
    ${j.error ? `<div class="error">${esc(j.error)}</div>` : ""}
    ${j.status === "done" ? `<video controls playsinline loop preload="metadata" src="${jobVideo(j)}"></video>` : `<div class="mini">${imgs}</div>`}
    <div class="row">
      ${j.status === "done" ? `<button type="button" class="btn small" data-job-act="sound">Sound</button>
        <a class="btn small" href="${jobVideo(j, null, true)}">Download</a>` : ""}
      ${active ? `<button type="button" class="btn small" data-job-act="cancel">Cancel</button>` : ""}
      ${j.status !== "running" ? `<button type="button" class="btn small danger" data-job-act="delete">Delete</button>` : ""}
    </div></div>`;
}
function renderJobs() {
  const list = $("jobList");
  if (!S.jobs.length) { setHtml(list, emptyState("No renders yet. Add keyframes on the left, describe the motion, and render.")); return; }
  if (list.querySelector(".empty")) { list.innerHTML = ""; list.dataset.sig = ""; }
  // Only touch cards that changed, and never move unchanged ones: detaching a <video> stops playback.
  const existing = new Map([...list.children].map((c) => [c.dataset.id, c]));
  const ids = new Set(S.jobs.map((j) => j.id));
  for (const [id, el] of existing) if (!ids.has(id)) el.remove();
  let prev = null;
  for (const j of S.jobs) {
    const html = jobCard(j);
    let el = existing.get(j.id);
    if (!el || el.dataset.html !== html) {
      const tmp = document.createElement("div"); tmp.innerHTML = html;
      const fresh = tmp.firstElementChild; fresh.dataset.html = html;
      if (el) el.replaceWith(fresh);
      el = fresh;
    }
    const want = prev ? prev.nextElementSibling : list.firstElementChild;
    if (want !== el) list.insertBefore(el, want);
    prev = el;
  }
  const active = S.jobs.filter((j) => ["queued", "running"].includes(j.status)).length;
  $("jobCount").textContent = active ? `${active} active` : `${S.jobs.length} render${S.jobs.length === 1 ? "" : "s"}`;
}
async function deleteJob(id) {
  if (!confirm("Delete this render?")) return false;
  try { await api(`/api/jobs/${id}`, { method: "DELETE" }); await refreshJobs(); renderAll(); return true; }
  catch (e) { toast(e.message); return false; }
}
$("jobList").onclick = async (e) => {
  const act = e.target.closest("[data-job-act]")?.dataset.jobAct; if (!act) return;
  const id = e.target.closest(".job").dataset.id;
  if (act === "delete") return deleteJob(id);
  if (act === "sound") return openSound(id);
  try { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); await refreshJobs(); renderJobs(); }
  catch (err) { toast(err.message); }
};
function renderVideoPage() { renderTray(); renderMotion(); renderJobs(); }

// ---------------------------------------------------------------- characters
function charCard(c) {
  const more = c.refs.length > 1 ? `<span class="tag">${c.refs.length} images</span>` : "";
  return `<div class="char-card" data-char="${c.id}">
    <div class="char-art">${c.refs[0] ? `<img loading="lazy" alt="" src="${imgUrl(c.refs[0])}">` : `<span>No image</span>`}${more}</div>
    <div class="char-text"><b>${esc(c.name)}</b>${c.description ? `<span>${esc(c.description)}</span>` : ""}</div>
    <div class="row"><button type="button" class="btn small" data-char-act="use">Use in image</button>
      <button type="button" class="btn small ghost" data-char-act="edit">Edit</button></div>
  </div>`;
}
function renderCharacters() {
  setHtml($("charGrid"), S.characters.length ? S.characters.map(charCard).join("")
    : emptyState("No characters yet. Make or upload a clear image of each one, then create the character from it."));
}
$("charGrid").onclick = (e) => {
  const card = e.target.closest("[data-char]"); if (!card) return;
  const id = card.dataset.char;
  if (e.target.closest("[data-char-act]")?.dataset.charAct === "use") {
    if (!S.cast.includes(id)) { if (S.cast.length >= 3) S.cast.shift(); S.cast.push(id); store.set("cast", S.cast); }
    setImgMode("text"); go("image");
    toast(`${charById(id).name} will be in the next images`);
  } else openCharacter(id);
};
$("charNew").onclick = () => openCharacter(null);

const CH = { id: null, refs: [] };
function openCharacter(id, refs) {
  const c = id && charById(id);
  CH.id = c ? c.id : null; CH.refs = c ? [...c.refs] : [...(refs || [])];
  $("charTitle").textContent = c ? `Edit ${c.name}` : "New character";
  $("charName").value = c ? c.name : ""; $("charDesc").value = c ? c.description : "";
  $("charDelete").classList.toggle("hidden", !c);
  renderRefs();
  $("charSheet").classList.remove("hidden");
  setTimeout(() => $("charName").focus(), 50);
}
function renderRefs() {
  $("charRefs").innerHTML = CH.refs.map((id, i) => `<div class="kf"><img alt="" src="${imgUrl(id)}"><span class="n">${i ? i + 1 : "Main"}</span>
    <div class="kt"><button type="button" data-r="left" data-i="${i}" aria-label="Move earlier">◀</button>
    <button type="button" data-r="del" data-i="${i}" aria-label="Remove">✕</button>
    <button type="button" data-r="right" data-i="${i}" aria-label="Move later">▶</button></div></div>`).join("")
    + (CH.refs.length < 4 ? `<button type="button" class="add" data-r="add"><span><svg><use href="#i-plus"/></svg><br>Add</span></button>` : "");
  $("charRefCount").textContent = `${CH.refs.length} of 4`;
}
function addRefs(ids) {
  const fresh = ids.filter((id) => !CH.refs.includes(id));
  if (CH.refs.length + fresh.length > 4) toast("Up to 4 reference images");
  CH.refs.push(...fresh.slice(0, 4 - CH.refs.length)); renderRefs();
}
$("charRefs").onclick = (e) => {
  const b = e.target.closest("[data-r]"); if (!b) return;
  const r = b.dataset.r, i = +b.dataset.i;
  if (r === "add") return openPicker("refs");
  if (r === "del") CH.refs.splice(i, 1);
  else {
    const j = r === "left" ? i - 1 : i + 1;
    if (j >= 0 && j < CH.refs.length) [CH.refs[i], CH.refs[j]] = [CH.refs[j], CH.refs[i]];
  }
  renderRefs();
};
$("charSave").onclick = async () => {
  const body = { name: $("charName").value.trim(), description: $("charDesc").value.trim(), refs: CH.refs };
  if (!body.name) return toast("Give the character a name");
  if (!body.refs.length) return toast("Add at least one reference image");
  const btn = $("charSave"); btn.disabled = true;
  try {
    await api(CH.id ? `/api/characters/${CH.id}` : "/api/characters", { method: CH.id ? "PUT" : "POST",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    await refreshCharacters(); closeOverlay("charSheet"); renderAll();
    toast(CH.id ? "Saved" : `${body.name} created. Tag them in Create image.`);
  } catch (e) { toast(e.message); } finally { btn.disabled = false; }
};
$("charDelete").onclick = async () => {
  if (!CH.id || !confirm(`Delete ${charById(CH.id)?.name || "this character"}? Their images stay in the library.`)) return;
  try {
    await api(`/api/characters/${CH.id}`, { method: "DELETE" });
    S.cast = S.cast.filter((x) => x !== CH.id); store.set("cast", S.cast);
    await refreshCharacters(); closeOverlay("charSheet"); renderAll();
  } catch (e) { toast(e.message); }
};

// ---------------------------------------------------------------- library
function renderLibrary() {
  const f = S.libFilter;
  const videos = f === "image" ? [] : S.jobs.filter((j) => j.status === "done").map((j) => ({ t: j.finished || j.created, html: videoTile(j) }));
  const imgs = f === "video" ? [] : S.images.map((m) => ({ t: m.created, html: imageTile(m) }));
  const items = [...videos, ...imgs].sort((a, b) => b.t - a.t);
  const box = $("libGrid");
  if (setHtml(box, items.length ? items.map((i) => i.html).join("") : emptyState("Nothing here yet.", "#/image", "Create an image")))
    wireHoverPlay(box);
}
$("libFilter").onclick = (e) => {
  const b = e.target.closest("[data-f]"); if (!b) return;
  S.libFilter = b.dataset.f;
  $("libFilter").querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", c === b));
  renderLibrary();
};

// ---------------------------------------------------------------- status + polling
function renderStatus() {
  const info = S.info; if (!info) return;
  const c = info.comfy, st = $("status"), card = $("gpuCard"), g = info.gpu || {}, btn = $("gpuBtn");
  const active = S.jobs.filter((j) => ["queued", "running"].includes(j.status)).length;
  const pod = g.mode === "runpod", month = pod ? ` · $${(g.month_cost || 0).toFixed(2)} this month` : "";
  btn.classList.toggle("hidden", !pod || g.state === "stopping");
  if (pod) {
    btn.textContent = g.state === "ready" ? "Stop GPU now" : g.state === "starting" ? "Starting…" : g.state === "failed" ? "Try again" : "Start GPU now";
    btn.disabled = g.state === "starting";
    btn.dataset.act = g.state === "ready" ? "stop" : "start";
  }
  if (pod && g.state !== "ready") {
    const wait = fmtTime(Math.max(0, Date.now() / 1000 - g.since));
    const [pill, cls, name, sub] = {
      off: ["GPU off", "", "GPU off", "Starts by itself when you render" + month],
      starting: ["GPU starting", "busy", "GPU starting…", `${wait} · usually 2–5 min`],
      stopping: ["GPU stopping", "", "GPU stopping…", "Removing the pod" + month],
      failed: ["GPU failed", "bad", "GPU didn't start", g.error || "unknown error"],
    }[g.state] || ["GPU", "", "GPU", ""];
    st.textContent = pill; st.className = "pill " + cls;
    card.querySelector(".gpu-name").textContent = name;
    card.querySelector(".gpu-sub").textContent = sub;
    card.querySelector(".meter span").style.width = "0";
  } else if (c.online) {
    const gpu = (c.gpu || "GPU").replace(/^cuda:\d+\s*/, "").replace(/\s*:.*$/, "");
    const total = c.vram_total / 2 ** 30, used = (c.vram_total - c.vram_free) / 2 ** 30;
    st.textContent = info.running ? "Rendering" : "Ready"; st.className = "pill " + (info.running ? "busy" : "ok");
    card.querySelector(".gpu-name").textContent = gpu;
    card.querySelector(".meter span").style.width = total ? `${Math.round(100 * used / total)}%` : "0";
    card.querySelector(".gpu-sub").textContent = (total ? `${used.toFixed(1)} / ${total.toFixed(0)} GB VRAM` : "") +
      (pod && g.cost_per_hour ? ` · $${g.cost_per_hour}/h` : "") + month;
  } else {
    st.textContent = "GPU offline"; st.className = "pill bad";
    card.querySelector(".gpu-name").textContent = "ComfyUI offline";
    card.querySelector(".gpu-sub").textContent = "Start it with start_all.sh, or add a \"gpu\" section to remote/config.json";
    card.querySelector(".meter span").style.width = "0";
  }
  const idle = info.idle_stop || (g.seconds_left != null ? { seconds_left: g.seconds_left } : null), ip = $("idlePill");
  ip.classList.toggle("hidden", !idle || idle.seconds_left > 600);
  if (idle) ip.innerHTML = `Stops in ${Math.max(1, Math.ceil(idle.seconds_left / 60))} min<span class="wide-only"> · keep running</span>`;
  const q = $("queuePill");
  q.classList.toggle("hidden", !active); q.textContent = `${active} in queue`;
  const badge = $("tabBadge"); badge.classList.toggle("hidden", !active); badge.textContent = active;
}

$("gpuBtn").onclick = async () => {
  const act = $("gpuBtn").dataset.act;
  if (act === "stop" && !confirm("Stop the GPU now? It starts again by itself when you render.")) return;
  try { await api(`/api/gpu/${act}`, { method: "POST" }); await refreshInfo(); } catch (e) { toast(e.message); }
};
// Phones have no sidebar: the status pill opens the GPU card (start/stop, spend) as a popover.
$("status").onclick = (e) => { e.stopPropagation(); document.body.classList.toggle("gpu-open"); };
document.addEventListener("click", (e) => {
  if (document.body.classList.contains("gpu-open") && !e.target.closest("#gpuCard")) document.body.classList.remove("gpu-open");
});
$("idlePill").onclick = async () => {
  try { await api("/api/keepalive", { method: "POST" }); await refreshInfo(); toast("The pod keeps running."); } catch (e) { toast(e.message); }
};

async function refreshInfo() {
  const first = !S.info;
  S.info = await api("/api/info");
  if (first) { renderImageModels(); renderEngines(); renderStyles(); }
  renderStatus();
}
async function refreshImages() { S.images = await api("/api/images"); S.imagesLoaded = true; }
async function refreshJobs() { S.jobs = await api("/api/jobs"); }
async function refreshCharacters() { S.characters = await api("/api/characters"); S.charactersLoaded = true; }

function renderAll() {
  if (!S.info) return;
  renderStatus();
  if (S.page === "home") renderHome();
  if (S.page === "image") { renderImagePage(); renderSource(); }
  if (S.page === "video") renderVideoPage();
  if (S.page === "library") renderLibrary();
  if (S.page === "characters") renderCharacters();
  if (!$("soundSheet").classList.contains("hidden")) renderSound();
}

// ---------------------------------------------------------------- start
renderAspects(); renderCounts(); setImgMode("text");
route();
if (!token) $("noToken").classList.remove("hidden");
else {
  let n = 0;
  const tick = async () => {
    try {
      const slow = n++ % 5 === 0;
      await Promise.all([refreshInfo(), refreshJobs(), slow ? refreshImages() : null, slow ? refreshCharacters() : null]);
      renderAll();
    } catch (e) {}
  };
  tick(); setInterval(tick, 3000);
}
