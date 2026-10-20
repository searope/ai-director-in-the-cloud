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
  imageModel: store.get("image_model", null),  // null: the server's default (FLUX 3 Klein when installed)
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
const TITLES = { home: "Home", image: "Create image", video: "Create video", library: "Library", director: "Director", characters: "Characters", styles: "Styles" };
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
  if (page === "director" && token) refreshDirector().then(renderAll).catch((e) => toast(e.message));
}
window.addEventListener("hashchange", route);
const go = (page) => { if (location.hash === "#/" + page) route(); else location.hash = "#/" + page; };

// ---------------------------------------------------------------- tiles
function imageTile(m, acts = true) {
  const tag = m.kind === "upload" ? "Upload" : m.kind === "edit" ? "Edit" : m.kind === "style" ? styleLabel(m.style) : "";
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
    <span class="tag"><svg><use href="#i-${j.kind === "film" ? "director" : "video"}"/></svg>${j.kind === "film" ? "Film" : "Video"}</span>
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
  if (!models.some((m) => m.id === S.imageModel && !m.missing.length)) S.imageModel = S.info.default_image_model;
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
  // the owner chose (Oct 2026) not to show license text here: it is a tooltip on the menu
  $("imgModelLicense").classList.add("hidden");
  document.querySelectorAll("select.imageModel").forEach((sel) => { sel.title = m ? "License: " + m.license : ""; });
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
    if (!$("ssZoom").classList.contains("hidden")) return closeOverlay("ssZoom");
    closeOverlay("lightbox"); closeOverlay("charSheet"); closeOverlay("soundSheet"); closeOverlay("styleSheet");
  }
  if (e.key === "Enter" && e.target.matches?.(".tile")) e.target.click();
  if ((e.key === "ArrowLeft" || e.key === "ArrowRight") && !$("lightbox").classList.contains("hidden") && $("lbMedia").querySelector(".lb-paddle")
      && !e.target.matches?.("input, textarea")) S.lbStep?.(e.key === "ArrowLeft" ? -1 : 1);
});

// ---------------------------------------------------------------- lightbox
// the page behind an open overlay doesn't scroll
new MutationObserver(() => document.body.classList.toggle("no-scroll", !!document.querySelector(".overlay:not(.hidden)")))
  .observe(document.body, { subtree: true, attributes: true, attributeFilter: ["class"] });
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
  const kind = m.kind === "style" ? `Style: ${styleLabel(m.style)}` : { text: "Text to image", edit: "Edit", upload: "Uploaded" }[m.kind] || "Image";
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
      <button type="button" class="btn" data-lb="styles">Try styles</button>
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
    else if (a === "styles") { closeOverlay("lightbox"); STY.image = id; STY.mode = "browse"; go("styles"); }
    else if (a === "delete" && await deleteImage(id)) closeOverlay("lightbox");
  };
  $("lightbox").classList.remove("hidden");
}

const SOUND_ENGINES = ["minimax-h3", "ltx23"];  // engines that render their own soundtrack
const modeLabel = (s) => s.engine === "ltx23" ? "Distilled, 8 steps" : !s.fast ? "Quality"
  : s.engine === "minimax-h3" ? (s.turbo_steps === 4 ? "Fast, 4 steps" : "Balanced, 8 steps") : "Fast, 4 steps";
function jobSummary(j) {
  const s = j.settings;
  if (j.kind === "film") return { style: "", engine: "", text: `Film · ${j.title} · ${s.prompts.length} shots` };
  const style = s.custom_style || (S.info?.styles.find((x) => x.id === s.style) || {}).label || s.style;
  const engine = (S.info?.engines.find((e) => e.id === s.engine) || {}).label || s.engine;
  return { style, engine, text: `${engine} · ${style} · ${j.inputs.length} keyframes${s.loop ? " · loop" : ""} · ${modeLabel(s).toLowerCase()}` };
}
// ---------------------------------------------------------------- keyframes of a render
// The keyframes a render was made from: the originals, and the redrawn ones when the style redraws them
// (shown as soon as the redraw finishes). Tapping one opens it full size.
function keyframeStrip(j) {
  const film = j.kind === "film", redrawn = j.styled.length > 0;
  const row = (kind, names, label) => !names.length ? "" : `<div class="kf-row"><span class="kf-label">${label}</span>
    ${names.map((n, i) => `<button type="button" class="kf-thumb" data-kf="${kind}/${n}" data-kf-job="${j.id}" aria-label="${label} ${i + 1}">
      <img loading="lazy" alt="" src="${jobImg(j, kind, n)}"><span>${film ? "Shot " : ""}${i + 1}</span></button>`).join("")}</div>`;
  const redrawing = j.status === "running" && j.settings.restyle && !redrawn;
  const checks = j.keyframe_checks || [], last = checks[checks.length - 1];
  const checkLine = !last ? "" : last.ok
    ? `<div class="kf-check ok">Keyframes checked: OK${checks.length > 1 ? ` after ${checks.length} tries` : ""}</div>`
    : `<div class="kf-check warn" title="${esc((last.problems || []).join(" | "))}">Keyframe check: ${esc((last.problems || [])[0] || "problems")}${checks.length > 1 ? ` (best of ${checks.length} tries used)` : ""}</div>`;
  return `<div class="kf-strip">${row("input", j.inputs, film ? "Shots" : redrawn || j.settings.restyle ? "Original" : "Keyframes")}
    ${row("styled", j.styled, "Redrawn")}${checkLine}${redrawing ? `<div class="kf-row"><span class="kf-label">Redrawn</span><span class="hint" style="margin:0">being redrawn in the style…</span></div>` : ""}</div>`;
}
function openKeyframe(jobId, ref, back) {
  const j = jobById(jobId); if (!j) return;
  const [kind, name] = ref.split("/"), n = parseInt(name, 10);
  const label = j.kind === "film" ? `Shot ${n}, start frame` : `Keyframe ${n}${kind === "styled" ? ", redrawn" : j.styled.length ? ", original" : ""}`;
  // every keyframe of the render in strip order (originals, then redrawn), for the ‹ › paddles and the mouse wheel
  const all = [...j.inputs.map((x) => `input/${x}`), ...j.styled.map((x) => `styled/${x}`)], at = all.indexOf(ref);
  const step = (d) => all.length > 1 && openKeyframe(jobId, all[(at + d + all.length) % all.length], back);
  $("lbMedia").innerHTML = `<img alt="" src="${jobImg(j, kind, name)}">` + (all.length > 1
    ? `<button type="button" class="lb-paddle prev" data-step="-1" aria-label="Previous keyframe">‹</button>
       <button type="button" class="lb-paddle next" data-step="1" aria-label="Next keyframe">›</button>
       <span class="lb-count">${at + 1} / ${all.length}</span>` : "");
  $("lbMedia").onclick = (e) => { const b = e.target.closest("[data-step]"); if (b) step(+b.dataset.step); };
  $("lbMedia").onwheel = (e) => {  // the wheel moves through the keyframes, not the page behind
    e.preventDefault();
    if (Date.now() - (S.wheelAt || 0) < 250 || Math.abs(e.deltaY) + Math.abs(e.deltaX) < 4) return;
    S.wheelAt = Date.now(); step((e.deltaY || e.deltaX) > 0 ? 1 : -1);
  };
  S.lbStep = step;
  $("lbInfo").innerHTML = `<h3>${label}</h3>
    <p class="hint" style="margin:0">${esc(jobSummary(j).text)}</p>
    ${keyframeStrip(j)}
    <div class="acts">
      ${back ? `<button type="button" class="btn" data-lb="video">Back to the video</button>` : ""}
      <a class="btn" href="${jobImg(j, kind, name)}" download="keyframe-${j.id}-${kind}-${name}">Download</a>
    </div>`;
  $("lbInfo").onclick = (e) => {
    const kf = e.target.closest("[data-kf]");
    if (kf) return openKeyframe(jobId, kf.dataset.kf, back);
    if (e.target.closest('[data-lb="video"]')) openVideo(jobId);
  };
  $("lightbox").classList.remove("hidden");
}

function openVideo(id) {
  const j = S.jobs.find((x) => x.id === id); if (!j) return;
  const s = j.settings, sum = jobSummary(j);
  $("lbMedia").innerHTML = `<video controls autoplay loop playsinline src="${jobVideo(j)}"></video>`;
  const rows = [["Keyframes", j.inputs.length], ["Render time", j.started && j.finished ? fmtTime(j.finished - j.started) : ""], ["Created", ago(j.created)]]
    .filter(([, v]) => v !== "");
  $("lbInfo").innerHTML = `<h3>${j.kind === "film" ? esc(j.title) : "Video"}</h3>
    ${j.kind === "film" ? `<div class="prompt">${s.prompts.map((p, i) => `Shot ${i + 1}: ${esc(p)}`).join("\n")}</div>` : renderFacts(j)}
    <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    ${keyframeStrip(j)}
    <div class="acts">
      <button type="button" class="btn" data-lb="sound">Sound</button>
      <a class="btn" href="${jobVideo(j, null, true)}">Download</a>
      <button type="button" class="btn danger" data-lb="delete">Delete</button>
    </div>`;
  $("lbInfo").onclick = async (e) => {
    const kf = e.target.closest("[data-kf]");
    if (kf) return openKeyframe(id, kf.dataset.kf, true);
    const a = e.target.closest("[data-lb]")?.dataset.lb;
    if (a === "sound") openSound(id);
    else if (a === "delete" && await deleteJob(id)) closeOverlay("lightbox");
  };
  $("lightbox").classList.remove("hidden");
}

// ---------------------------------------------------------------- sound (ElevenLabs layers, opt-in)
const SND = { job: null, kind: "voice", version: "original", lines: [], sig: "", video: "" };
const SND_KINDS = { voice: "Voice-over", dialogue: "Dialogue", sfx: "Effect", music: "Music" };
// An older film may be outside the newest 50 jobs: the open Director project carries its job.
const jobById = (id) => S.jobs.find((x) => x.id === id) || (D.project?.film?.job?.id === id ? D.project.film.job : null);
const sndJob = () => jobById(SND.job);
const sndAudio = (j) => j.audio || { tracks: [], dubs: {}, video_volume: 1, mixed: null };

async function loadVoices() {
  if (S.voices || !S.info?.elevenlabs?.enabled) return;
  try { S.voices = await api("/api/voices"); } catch (e) { toast(e.message); }
}
const voiceOptions = (sel) => (S.voices || []).map((v) =>
  `<option value="${esc(v.id)}"${v.id === sel ? " selected" : ""}>${esc(v.name)}${v.labels ? ` · ${esc(v.labels)}` : ""}</option>`).join("")
  || `<option value="">No voices loaded</option>`;

async function openSound(id) {
  const j = jobById(id); if (!j) return;
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
  // A Director film carries its script's narration with each shot's start time: one tap fills the form.
  const lines = j.narration || [];
  $("sndNarration").classList.toggle("hidden", !lines.length);
  $("sndNarration").innerHTML = lines.length ? `<b>From the script</b>` + lines.map((n, i) =>
    `<button type="button" data-narr="${i}"><span>Shot ${n.shot} · ${n.start.toFixed(1)} s</span>${esc(n.text)}</button>`).join("") : "";
}
$("sndNarration").onclick = (e) => {
  const b = e.target.closest("[data-narr]"); if (!b) return;
  const n = (sndJob()?.narration || [])[+b.dataset.narr]; if (!n) return;
  $("sndVoiceText").value = n.text; $("sndStart").value = n.start;
  toast("Filled in: choose a voice and generate the layer");
};

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
  const hasOwnSound = SOUND_ENGINES.includes(j.settings.engine);
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
const pickOne = () => ["source", "shotstart", "trystyle", "sstry"].includes(picker.mode);  // single choice, not a list
function openPicker(mode) {
  picker.mode = mode; picker.sel = [];
  $("pickerTitle").textContent = { source: "Choose the image to edit", refs: "Add reference images",
                                   shotstart: "Choose the shot's start frame", trystyle: "Choose the image to try the styles on",
                                   sstry: "Choose a picture to try this style on" }[mode] || "Add keyframes, in order";
  $("pickerDone").textContent = pickOne() ? "Use image" : "Add";
  renderPicker();
  $("picker").classList.remove("hidden");
}
function renderPicker() {
  $("pickerGrid").innerHTML = S.images.length ? S.images.map((m) => {
    const n = picker.sel.indexOf(m.id);
    return `<div class="tile${n >= 0 ? " sel" : ""}" data-pick="${m.id}"><img loading="lazy" alt="" src="${imgUrl(m.id)}">
      ${n >= 0 && !pickOne() ? `<span class="num">${n + 1}</span>` : ""}</div>`;
  }).join("") : `<div class="empty" style="grid-column:1/-1">No images yet. Upload some, or make them in Create image.</div>`;
  $("pickerDone").disabled = !picker.sel.length;
}
$("pickerGrid").onclick = (e) => {
  const t = e.target.closest("[data-pick]"); if (!t) return;
  const id = t.dataset.pick, i = picker.sel.indexOf(id);
  if (pickOne()) picker.sel = [id];
  else if (i >= 0) picker.sel.splice(i, 1); else picker.sel.push(id);
  renderPicker();
};
$("pickerDone").onclick = () => {
  if (picker.mode === "source") { S.source = picker.sel[0]; renderSource(); }
  else if (picker.mode === "refs") addRefs(picker.sel);
  else if (picker.mode === "shotstart") setShotStart(picker.shot, picker.sel[0]);
  else if (picker.mode === "trystyle") tryStyles(picker.sel[0]);
  else if (picker.mode === "sstry") tryOnPicture(picker.sel[0]);
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
  picker.sel = pickOne() ? ids.slice(-1) : [...picker.sel, ...ids];
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
  $("trayCount").textContent = S.tray.length ? `${S.tray.length} of 12` : "2–12 images, in order";
  renderPanelSummary();
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
    return `<div class="gap"><div class="pair"><img alt="Keyframe ${i + 1}" src="${imgUrl(a)}" data-open-img="${a}"><span>${i + 1} → ${(i + 1) % S.tray.length + 1}</span><img alt="Keyframe ${(i + 1) % S.tray.length + 1}" src="${imgUrl(b)}" data-open-img="${b}"></div>
      <textarea data-gap="${i}" placeholder="${i ? "Same as above" : "What moves, and the camera, e.g. she turns and smiles, slow push-in"}">${esc(S.motion[i] || "")}</textarea></div>`;
  }).join("");
}
$("motion").onclick = async (e) => {  // a keyframe full size
  const img = e.target.closest("[data-open-img]"); if (img) { await refreshImages(); openImage(img.dataset.openImg); }
};
$("motion").oninput = (e) => {
  const t = e.target.closest("[data-gap]"); if (!t) return;
  S.motion[+t.dataset.gap] = t.value; store.set("motion", S.motion);
};
$("loop").onchange = renderMotion;

// ---------------------------------------------------------------- styles (Create video's style, and the Styles page)
const styleById = (id) => (S.info?.styles || []).find((s) => s.id === id);
const styleLabel = (id) => id === "__custom" ? "Custom" : styleById(id)?.label || id;
const exampleUrl = (url) => url.startsWith("/api/") ? withToken(url) : url;
// the card picture: the style's example in the scene chosen on the Styles page (city street, face, landscape, indoor)
const sceneExample = (st) => st?.examples?.[STY.scene] || null;
const styleThumb = (st, cls = "") => sceneExample(st) ? `<img class="${cls}" loading="lazy" alt="" src="${exampleUrl(sceneExample(st))}">`
  : `<span class="${cls} style-ph">${esc((st?.label || "?").slice(0, 2))}</span>`;
function setVideoStyle(id) {
  S.style = id;
  if (id !== "__custom") store.set("recent_styles", [id, ...store.get("recent_styles", []).filter((x) => x !== id)].slice(0, 8));
  const st = styleById(id);
  $("customStyle").classList.toggle("hidden", id !== "__custom");
  if (!S.restyleTouched) $("restyle").checked = id === "__custom" || !!st?.restyle;
  $("restyleModelBox").classList.toggle("hidden", !$("restyle").checked);
  renderStyles(); renderPanelSummary();
}
// Create video shows only recently used styles (the current one highlighted) and "More styles…" (the Styles page).
const DEFAULT_RECENT = ["photoreal", "anime", "watercolor", "oil-painting"];
function renderStyles() {
  // recently used first, topped up with a few basics until there are four
  let recent = [...new Set([...store.get("recent_styles", []), ...DEFAULT_RECENT])].filter(styleById).slice(0, Math.max(4, store.get("recent_styles", []).length));
  if (S.style !== "__custom" && !recent.includes(S.style)) recent = [S.style, ...recent];
  // the chosen style large enough to judge (tap it for all its examples), then recent styles as chips that wrap
  const st = styleById(S.style), custom = S.style === "__custom";
  const chip = (id) => `<button type="button" class="chip${S.style === id ? " on" : ""}" data-style="${id}">${esc(styleLabel(id))}</button>`;
  $("styleCurrent").innerHTML = `<button type="button" class="sp-main" ${custom ? "data-style-change" : "data-style-open"}>
      ${custom ? `<span class="sp-img style-ph">✎</span>` : styleThumb(st, "sp-img")}
      <span class="sp-cap"><b>${esc(styleLabel(S.style))}</b><span>${custom ? "described below" : `${esc(st?.group || "")} · tap for examples`}</span></span></button>
    <div class="chips sp-chips">${recent.slice(0, 6).map(chip).join("")}${custom ? chip("__custom") : ""}
      <button type="button" class="chip more" data-style-change>More styles…</button></div>`;
}
$("styleCurrent").onclick = (e) => {
  if (e.target.closest("[data-style-change]")) { STY.mode = "video"; go("styles"); return; }
  if (e.target.closest("[data-style-open]")) return openStyleSheet(S.style);
  const b = e.target.closest("[data-style]"); if (b) setVideoStyle(b.dataset.style);
};
$("restyle").onchange = () => { S.restyleTouched = true; $("restyleModelBox").classList.toggle("hidden", !$("restyle").checked); };

// The Styles page. mode "browse": select styles to try on an image (or use one for videos); mode "video": tap one to
// use it in Create video. STY.image: an image chosen to try styles on (from the image viewer).
var STY = { mode: "browse", group: "Recent", q: "", sel: new Set(), image: null, scene: 0 };
try { STY.scene = Math.max(0, Math.min(3, +store.get("style_scene", 0) || 0)); } catch { /* no storage */ }
function styleGroups() {
  const groups = [...new Set((S.info?.styles || []).map((s) => s.group))];
  return [...(store.get("recent_styles", []).length ? ["Recent"] : []), "All", ...groups];
}
function renderStylesPage() {
  if (!S.info) return;
  setHtml($("styleScenes"), SCENES.map((name, i) => `<button type="button" data-scene="${i}"${STY.scene === i ? ' class="on"' : ""}>${name}</button>`).join(""));
  const groups = styleGroups();
  if (!groups.includes(STY.group)) STY.group = groups.includes("Basics") ? "Basics" : "All";
  setHtml($("styleGroups"), groups.map((g) => `<button type="button" class="chip${STY.group === g && !STY.q ? " on" : ""}" data-group="${esc(g)}">${esc(g)}</button>`).join(""));
  const q = STY.q.trim().toLowerCase();
  let list = S.info.styles;
  if (q) list = list.filter((s) => `${s.label} ${s.group} ${s.id} ${s.look || ""}`.toLowerCase().includes(q));
  else if (STY.group === "Recent") list = store.get("recent_styles", []).map(styleById).filter(Boolean);
  else if (STY.group !== "All") list = list.filter((s) => s.group === STY.group);
  const pick = STY.mode === "video";
  const cards = list.map((s) => `<div class="style-card${STY.sel.has(s.id) ? " sel" : ""}${S.style === s.id ? " current" : ""}" data-sid="${s.id}" tabindex="0">
      ${styleThumb(s, "sc-img")}${!pick && STY.sel.has(s.id) ? `<span class="num">✓</span>` : ""}
      <button type="button" class="sc-info" data-info="${s.id}" aria-label="Prompts and examples of ${esc(s.label)}" title="Prompts and examples">✎</button>
      <div class="sc-label"><b>${esc(s.label)}</b>${S.style === s.id ? `<span class="in-use">in use</span>` : s.edited ? `<span class="sc-edited">edited</span>` : ""}</div>
      ${!pick && s.restyle === false ? `<span class="sc-note">no redraw</span>` : ""}
    </div>`).join("");
  const custom = pick && !q ? `<div class="style-card" data-sid="__custom" tabindex="0"><span class="sc-img style-ph">✎</span><div class="sc-label"><b>Custom…</b></div></div>` : "";
  setHtml($("styleGrid"), cards + custom || emptyState("No style matches that search."));
  const tool = $("styleTool"), img = STY.image && imageById(STY.image);
  tool.classList.toggle("hidden", !pick && !STY.sel.size && !img);
  tool.innerHTML = pick ? `<span>Tap a style to use it in Create video.</span><button type="button" class="btn small ghost" data-st="cancel">Back</button>`
    : `${img ? `<img alt="" src="${imgUrl(img.id)}">` : ""}<span>${STY.sel.size ? `${STY.sel.size} selected` : "Select the styles to try"}${img ? " on this image" : ""}</span>
       ${STY.sel.size ? `<button type="button" class="btn small primary" data-st="try">Try ${STY.sel.size} on ${img ? "it" : "an image"}</button>
       ${STY.sel.size === 1 ? `<button type="button" class="btn small" data-st="use">Use for videos</button>` : ""}
       <button type="button" class="btn small ghost" data-st="clear">Clear</button>` : ""}`;
}
$("styleScenes").onclick = (e) => {
  const b = e.target.closest("[data-scene]"); if (!b) return;
  STY.scene = +b.dataset.scene; store.set("style_scene", STY.scene); renderStylesPage(); renderStyles();
};
$("styleSearch").oninput = () => { STY.q = $("styleSearch").value; renderStylesPage(); };
$("styleGroups").onclick = (e) => {
  const b = e.target.closest("[data-group]"); if (!b) return;
  STY.group = b.dataset.group; STY.q = ""; $("styleSearch").value = ""; renderStylesPage();
};
$("styleGrid").onclick = (e) => {
  const info = e.target.closest("[data-info]");
  if (info) return openStyleSheet(info.dataset.info);
  const card = e.target.closest("[data-sid]"); if (!card) return;
  const id = card.dataset.sid;
  if (STY.mode === "video") { setVideoStyle(id); STY.mode = "browse"; go("video"); return; }
  if (styleById(id)?.restyle === false) return toast(`${styleLabel(id)} keeps images as they are: nothing to redraw`);
  STY.sel.has(id) ? STY.sel.delete(id) : STY.sel.add(id);
  renderStylesPage();
};
$("styleTool").onclick = async (e) => {
  const a = e.target.closest("[data-st]")?.dataset.st; if (!a) return;
  if (a === "cancel") { STY.mode = "browse"; go("video"); }
  else if (a === "clear") { STY.sel.clear(); STY.image = null; renderStylesPage(); }
  else if (a === "use") { setVideoStyle([...STY.sel][0]); toast(`${styleLabel(S.style)} is now the style for videos`); renderStylesPage(); }
  else if (a === "try") { if (STY.image) tryStyles(STY.image); else openPicker("trystyle"); }
};
// A style's details: its example pictures (city street, close-up face, landscape, indoor scene) and its two
// prompts, editable. "Redraw examples" redoes the pictures with the edited redraw prompt on the GPU.
const SCENES = ["City street", "Close-up face", "Landscape", "Indoor scene"];
const SS = { id: null, redrawing: {}, tries: [] };  // tries: pictures tried in a style here {style, source, task, image}  // redrawing: style id -> tasks still running
// A new style of the user's own: the panel opens empty (seed: a style to copy from).
const NEW_STYLE = { id: null, label: "", group: "My styles", look: "", restyle_prompt: "", restyle: true, custom: true, examples: [] };
function openStyleSheet(id, seed = null) {
  const st = id ? styleById(id) : { ...NEW_STYLE, ...(seed || {}) }; if (!st) return;
  SS.id = id; SS.draft = id ? null : st;
  $("ssTitle").textContent = id ? st.label : "New style";
  $("ssName").value = id ? st.label : st.label || "";
  $("ssRestyle").value = st.restyle_prompt || "";
  $("ssLook").value = st.look || "";
  renderStyleSheet();
  $("styleSheet").classList.remove("hidden");
  if (!id) $("ssName").focus();
}
function renderStyleSheet() {
  const st = SS.id ? styleById(SS.id) : SS.draft; if (!st) return;
  $("ssNameBlock").classList.toggle("hidden", !st.custom);
  $("ssRestyleHint").textContent = st.custom ? "turns pictures into this style; leave empty to make it from the look"
    : "turns keyframes and pictures into this style";
  $("ssCopy").classList.toggle("hidden", !!st.custom || !SS.id);
  $("ssReset").textContent = st.custom ? "Delete style" : "Reset to default";
  const running = SS.redrawing[st.id] || [];
  $("ssSub").textContent = `${st.group}${st.edited ? " · edited" : ""}`;
  $("ssExamples").innerHTML = SCENES.map((name, i) => `<figure>${st.examples?.[i] ? `<img alt="${name}, tap to enlarge" src="${exampleUrl(st.examples[i])}" data-zoom-scene="${i}">`
    : `<span class="style-ph"></span>`}<figcaption>${name}</figcaption></figure>`).join("");
  $("ssRestyleBlock").classList.toggle("hidden", !st.restyle);
  $("ssRedraw").classList.toggle("hidden", !st.restyle);
  $("ssRedraw").disabled = running.length > 0;
  $("ssRedrawing").classList.toggle("hidden", !running.length);
  $("ssRedrawing").textContent = running.length ? `Redrawing the examples on the GPU: ${running.length} left…` : "";
  $("ssReset").classList.toggle("hidden", st.custom ? !SS.id : !st.edited);
  $("ssRedraw").disabled = $("ssRedraw").disabled || !SS.id && !st.custom;
  $("ssTryBlock").classList.toggle("hidden", !st.restyle);
  const tries = SS.tries.filter((t) => SS.id && t.style === SS.id);
  $("ssTries").innerHTML = tries.map((t, i) => `<div class="ss-try">
      <img alt="Before" src="${imgUrl(t.source)}" data-try="${SS.tries.indexOf(t)}" data-side="before"><span>→</span>
      ${t.image ? `<img alt="After" src="${imgUrl(t.image)}" data-try="${SS.tries.indexOf(t)}" data-side="after">`
        : `<div class="ph">${t.error ? esc(t.error.slice(0, 80)) : `<div class="spinner"></div>`}</div>`}</div>`).join("");
  const r = st.review;
  $("ssReview").classList.toggle("hidden", !r);
  $("ssReview").innerHTML = !r ? "" : `<div class="ss-cov"><b>${r.coverage}% styled</b> · ${esc(r.strength)} · <span class="hint">AI check of the examples</span></div>
    <p>${esc(r.summary)}</p>
    ${r.unstyled.length ? `<p><b>May stay unstyled:</b> ${r.unstyled.map(esc).join(", ")}. Similar things in your pictures may not take the style well.</p>` : ""}
    ${r.replaced.length ? `<p class="warn-text"><b>Changed content:</b> ${r.replaced.map(esc).join("; ")}.</p>` : ""}`;
}
// Try the style on the user's own picture, from the panel: drop, upload or pick from the Library.
async function tryOnPicture(imageId) {
  if (!SS.id) { try { await saveStyle(); } catch (e) { return toast(e.message); } }  // a new style: save it first
  const id = SS.id, st = styleById(id); if (!st || !imageId) return;
  try {
    const [t] = await api("/api/styles/try", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image_id: imageId, styles: [id], model: S.imageModel, prompt: $("ssRestyle").value }) });
    const entry = { style: id, source: imageId, task: t.task, image: null, error: null };
    SS.tries.unshift(entry);
    renderStyleSheet();
    while (entry.task.status === "running") {
      await new Promise((r) => setTimeout(r, 2500));
      try { entry.task = await api(`/api/keyframes/tasks/${entry.task.id}`); } catch { entry.task = { status: "failed", error: "the task was lost" }; }
    }
    if (entry.task.status === "done") entry.image = entry.task.image_id; else entry.error = entry.task.error || "failed";
    refreshImages();
  } catch (e) { toast(e.message); }
  renderStyleSheet();
}
async function tryFiles(files) {
  for (const id of await uploadImages([...files].filter((f) => f.type.startsWith("image/")))) tryOnPicture(id);
}
$("ssDrop").addEventListener("click", (e) => {
  const a = e.target.closest("[data-sstry]")?.dataset.sstry;
  if (a === "upload") $("ssFile").click();
  else if (a === "library") openPicker("sstry");
});
$("ssFile").onchange = () => { tryFiles($("ssFile").files); $("ssFile").value = ""; };
["dragenter", "dragover"].forEach((ev) => $("ssDrop").addEventListener(ev, (e) => { e.preventDefault(); $("ssDrop").classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => $("ssDrop").addEventListener(ev, () => $("ssDrop").classList.remove("over")));
$("ssDrop").addEventListener("drop", (e) => { e.preventDefault(); tryFiles(e.dataTransfer.files); });
$("ssTries").onclick = (e) => {
  const img = e.target.closest("[data-try]"); if (!img) return;
  const t = SS.tries[+img.dataset.try];
  ZOOM.custom = { before: imgUrl(t.source), after: t.image ? imgUrl(t.image) : null };
  ZOOM.before = img.dataset.side === "before" || !t.image;
  renderZoom(); $("ssZoom").classList.remove("hidden");
};

// An example full size, over the style panel: After (the redraw) or Before (the original photo), ‹ › for the scenes.
const ZOOM = { scene: 0, before: false, custom: null };  // custom: {before, after} of a picture tried here
function renderZoom() {
  const st = styleById(SS.id); if (!st) return;
  const c = ZOOM.custom;
  const url = c ? (ZOOM.before ? c.before : c.after) : ZOOM.before ? `/static/styles/_base-${ZOOM.scene + 1}.jpg` : st.examples?.[ZOOM.scene];
  $("ssZoomImg").src = url ? exampleUrl(url) : "";
  $("ssZoomCap").textContent = `${st.label} · ${c ? "your picture" : SCENES[ZOOM.scene]} · ${ZOOM.before ? "original" : "redrawn"}`;
  document.querySelectorAll("#ssZoom [data-zoom=prev], #ssZoom [data-zoom=next]").forEach((b) => b.classList.toggle("hidden", !!c));
  document.querySelectorAll("#ssZoom [data-zoom=after], #ssZoom [data-zoom=before]").forEach((b) =>
    b.classList.toggle("on", (b.dataset.zoom === "before") === ZOOM.before));
}
function zoomStep(d) {
  const st = styleById(SS.id), have = SCENES.map((_, i) => i).filter((i) => st?.examples?.[i]);
  if (!have.length) return;
  ZOOM.scene = have[(have.indexOf(ZOOM.scene) + d + have.length) % have.length];
  renderZoom();
}
$("ssExamples").onclick = (e) => {
  const img = e.target.closest("[data-zoom-scene]"); if (!img) return;
  ZOOM.scene = +img.dataset.zoomScene; ZOOM.before = false; ZOOM.custom = null;
  renderZoom(); $("ssZoom").classList.remove("hidden");
};
$("ssZoom").addEventListener("click", (e) => {
  const a = e.target.closest("[data-zoom]")?.dataset.zoom;
  if (a === "prev" || a === "next") zoomStep(a === "prev" ? -1 : 1);
  else if (a) { ZOOM.before = a === "before"; renderZoom(); }
  else if (e.target.id === "ssZoomImg") { ZOOM.before = !ZOOM.before; renderZoom(); }  // tap the picture to compare
});
document.addEventListener("keydown", (e) => {
  if ($("ssZoom").classList.contains("hidden")) return;
  if (e.key === "ArrowLeft" || e.key === "ArrowRight") zoomStep(e.key === "ArrowLeft" ? -1 : 1);
  else if (e.key === " ") { e.preventDefault(); ZOOM.before = !ZOOM.before; renderZoom(); }
});

async function saveStyle() {
  const st = SS.id ? styleById(SS.id) : SS.draft;
  const body = { look: $("ssLook").value };
  if (st.restyle) body.restyle_prompt = $("ssRestyle").value;
  if (st.custom) body.label = $("ssName").value;
  if (!SS.id) {  // a new style: create it, then the panel shows it like any other
    SS.id = (await api("/api/styles", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).id;
    SS.draft = null;
  } else {
    await api(`/api/styles/${SS.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  }
  await refreshInfo(); renderStylesPage(); renderStyles();
  const now = styleById(SS.id);
  if (now) { $("ssTitle").textContent = now.label; $("ssRestyle").value = now.restyle_prompt || ""; }
  renderStyleSheet();
}
$("styleNew").onclick = () => openStyleSheet(null);
$("ssCopy").onclick = () => {
  const st = styleById(SS.id); if (!st) return;
  openStyleSheet(null, { label: `${st.label} (mine)`, look: st.look, restyle_prompt: st.restyle_prompt || "" });
};
$("ssSave").onclick = async () => { try { await saveStyle(); toast("Saved: renders in this style use the new prompts"); } catch (e) { toast(e.message); } };
$("ssReset").onclick = async () => {
  const st = styleById(SS.id); if (!st) return;
  if (!confirm(st.custom ? `Delete your style "${st.label}"? Videos already made keep their look.`
    : "Go back to the original prompts and example pictures for this style?")) return;
  try {
    await api(`/api/styles/${SS.id}`, { method: "DELETE" });
    await refreshInfo();
    if (st.custom) { closeOverlay("styleSheet"); if (S.style === st.id) setVideoStyle("photoreal"); } else openStyleSheet(SS.id);
    renderStylesPage(); renderStyles();
  } catch (e) { toast(e.message); }
};
$("ssRedraw").onclick = async () => {
  const id = SS.id;
  try {
    await saveStyle();
    const tasks = await api(`/api/styles/${id}/examples`, { method: "POST" });
    SS.redrawing[id] = tasks.map((t) => t.task);
    renderStyleSheet();
    pollStyleRedraw(id);
  } catch (e) { toast(e.message); }
};
async function pollStyleRedraw(id) {
  while ((SS.redrawing[id] || []).length) {
    await new Promise((r) => setTimeout(r, 3000));
    const left = [];
    for (const t of SS.redrawing[id]) {
      try {
        const now = await api(`/api/keyframes/tasks/${t.id}`);
        if (now.status === "running") left.push(now);
        else if (now.status === "failed") toast(`Example failed: ${now.error || "unknown error"}`);
      } catch { /* the task is gone (app restarted): stop waiting for it */ }
    }
    SS.redrawing[id] = left;
    if (SS.id === id) renderStyleSheet();
  }
  delete SS.redrawing[id];
  await refreshInfo(); renderStylesPage();
  if (SS.id === id) renderStyleSheet();
  toast(`${styleLabel(id)}: examples redrawn`);
}

async function tryStyles(imageId) {
  const ids = [...STY.sel];
  try {
    const tasks = await api("/api/styles/try", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ image_id: imageId, styles: ids, model: S.imageModel }) });
    for (const t of tasks) S.pending.unshift({ key: Math.random().toString(36).slice(2), prompt: `Style: ${styleLabel(t.style)}`, task: t.task });
    STY.sel.clear(); STY.image = null;
    toast(`Trying ${tasks.length} style${tasks.length === 1 ? "" : "s"}; results appear in Create image`);
    go("image"); pollTasks();
  } catch (err) { toast(err.message); }
}

function renderCheckOption() {
  const w = reviewWriter();
  $("checkKeyframes").disabled = !w;
  $("checkKeyframesLabel").textContent = !w ? "Check the redrawn keyframes (set up an AI model first)"
    : `Check the redrawn keyframes${w.paid ? ` (${w.label}, paid per use)` : ""}`;
}
function renderEngines() {
  const sel = $("engine");
  sel.innerHTML = S.info.engines.map((e) =>
    `<option value="${e.id}"${e.ready ? "" : " disabled"}>${esc(e.label)}${e.ready ? "" : " (not installed)"}</option>`).join("");
  // Keep the current choice if it's still available; otherwise MiniMax H3 (the default) when installed, else Wan.
  const ok = (id) => S.info.engines.some((e) => e.id === id && e.ready);
  sel.value = ok(sel.dataset.chosen) ? sel.dataset.chosen : ok("minimax-h3") ? "minimax-h3" : "wan22";
  sel.onchange();
}
$("engine").onchange = () => {
  const e = S.info.engines.find((x) => x.id === $("engine").value);
  const h3 = e.id === "minimax-h3", ltx = e.id === "ltx23";
  $("soundBox").classList.toggle("hidden", !SOUND_ENGINES.includes(e.id));
  $("smooth").checked = !SOUND_ENGINES.includes(e.id);  // H3 and LTX already render 24-25 fps
  // H3 has two turbo LoRAs (4 and 8 steps); Wan's fast mode is its 4-step LoRA; LTX-2.3 runs its distilled model.
  const keep = $("vmode").value;
  $("vmode").innerHTML = (ltx ? [["fast", "Distilled (8 steps)"]] : h3
    ? [["fast4", "Fast (4 steps)" + (e.turbo4 ? "" : " (not installed)"), !e.turbo4], ["fast", "Balanced (8 steps)"], ["quality", "Quality (20 steps)"]]
    : [["fast", "Fast (4 steps)"], ["quality", "Quality (20 steps)"]])
    .map(([v, label, off]) => `<option value="${v}"${off ? " disabled" : ""}>${label}</option>`).join("");
  $("vmode").value = [...$("vmode").options].some((o) => o.value === keep && !o.disabled) ? keep : "fast";
  $("vmode").onchange(); renderPanelSummary();
};
// Remember only the user's own choice (renderEngines calls onchange itself when it picks a default).
$("engine").addEventListener("change", () => { $("engine").dataset.chosen = $("engine").value; });
$("vmode").onchange = () => {
  const e = S.info.engines.find((x) => x.id === $("engine").value), h3 = e.id === "minimax-h3";
  // What the engine gives; its license notes only on hover (owner's choice, Oct 2026; details in the README).
  const el = $("engineLicense"), turbo = h3 && $("vmode").value !== "quality";
  el.className = "hint";
  el.textContent = h3 ? "24 fps with generated sound" : e.id === "ltx23" ? "25 fps with generated sound" : "16 fps, silent";
  el.title = "License: " + e.license + (turbo ? `. ${e.turbo_license}; Quality mode doesn't use it.` : "");
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
    restyle: $("restyle").checked, fast: $("vmode").value !== "quality", turbo_steps: $("vmode").value === "fast4" ? 4 : 8,
    check_keyframes: $("restyle").checked && $("checkKeyframes").checked && !!reviewWriter(), writer: reviewWriter()?.id || "",
    approve_keyframes: $("restyle").checked && $("approveKeyframes").checked,
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

// ---------------------------------------------------------------- clip check (an AI model reviews a render)
const STYLE_RATINGS = { strong: ["strong", "ok"], good: ["good", "ok"], weak: ["weak", "warn"], lost: ["lost", "bad"] };
const VERDICTS = { good: ["Looks good", "ok"], minor: ["Small flaws", "warn"], redo: ["Needs a redo", "bad"] };
function reviewWriter() {
  const ws = (S.info?.writers || []).filter((w) => w.ready);
  return ws.find((w) => w.id === store.get("writer")) || ws[0];
}
async function checkClip(jobId) {
  const w = reviewWriter();
  if (!w) return toast("Set up an AI model first (docs/director.md)");
  if (w.paid && !confirm(`${w.label} is billed per use. Check this clip with it?`)) return false;
  try { await api(`/api/jobs/${jobId}/review`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ writer: w.id }) }); }
  catch (e) { toast(e.message); return false; }
  await refreshJobs(); return true;
}
// action: the button under the suggested prompt, e.g. ["rerender", "Render again with this prompt"]
// redrawn: the render restyled its keyframes, so offer to keep the redrawn ones (default: start from the originals)
function reviewBlock(r, action, redrawn = false) {
  if (!r) return "";
  const who = esc(writerById(r.writer)?.label || r.writer || "");
  if (r.status === "working") return `<div class="ai-status"><div class="spinner"></div>Checking the clip with ${who}…</div>`;
  if (r.status === "failed") return `<div class="ai-status bad"><b>The check didn't finish.</b> ${esc(r.error || "")}</div>`;
  const [label, cls] = VERDICTS[r.verdict] || ["Checked", ""];
  return `<div class="clip-review">
    <div class="cr-head"><span class="pill ${cls}">${label}</span><span class="hint">${who}</span></div>
    <p>${esc(r.summary)}</p>
    ${r.style_rating ? `<div class="cr-style"><span class="pill ${STYLE_RATINGS[r.style_rating]?.[1] || ""}">Style: ${STYLE_RATINGS[r.style_rating]?.[0] || r.style_rating}</span> ${esc(r.style_notes || "")}</div>` : ""}
    ${r.issues?.length ? `<details><summary>${r.issues.length} problem${r.issues.length === 1 ? "" : "s"}</summary><ul>${r.issues.map((i) =>
      `<li class="${i.severity}"><b>${esc(i.when)}</b> ${esc(i.what)}</li>`).join("")}</ul></details>` : ""}
    ${r.keyframe_advice ? `<div class="note">${esc(r.keyframe_advice)}</div>` : ""}
    ${r.verdict !== "good" && r.motion_prompt ? `<label class="sf">Suggested prompt<textarea data-fix-prompt rows="3">${esc(r.motion_prompt)}</textarea></label>
      ${redrawn ? `<label class="toggle"><input type="checkbox" data-keep-styled><span>Keep the redrawn keyframes (otherwise your original keyframes are redrawn again)</span></label>` : ""}
      <button type="button" class="btn small" data-fix="${action[0]}">${action[1]}</button>` : ""}
  </div>`;
}

// Which cards' Details are open (by job id): kept across the regular re-renders. Unset = the default
// (open while queued or rendering, so the keyframes show during the render; closed once done).
const detailsOpen = new Map();
// Everything about a render: engine and its settings, size, length, seed, style with its prompts, motion prompts.
// j.render holds what the renderer really used (set once it starts); before that, the requested settings.
// "1 010 499 991": grouped with thin spaces, so browsers and phone add-ons don't take a seed for a phone number
const groupDigits = (n) => n == null ? "" : String(n).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
function renderFacts(j) {
  const s = j.settings, r = j.render || {}, sum = jobSummary(j);
  const engine = (S.info?.engines || []).find((e) => e.id === s.engine);
  const size = r.width ? `${r.width}×${r.height}` : { standard: "standard", large: "large", small: "small" }[s.size] || s.size;
  const length = r.frames_per_segment ? `${r.segments} × ${r.frames_per_segment} frames at ${r.fps} fps (≈ ${r.seconds} s)`
    + (r.output_fps && r.output_fps !== r.fps ? `, smoothed to ${r.output_fps} fps` : "") : `${s.length} frames per gap (Wan scale)`;
  const style = s.custom_style ? `Custom: ${s.custom_style}` : (r.style || sum.style);
  const rows = [
    ["Engine", engine?.label || s.engine], ["Mode", modeLabel(s)],
    ["Size", size], ["Length", length], ["Seed", groupDigits(r.seed ?? s.seed) || "random"],
    ["Sound", r.audio ?? (SOUND_ENGINES.includes(s.engine) && s.audio) ? `generated${s.sound ? `: ${s.sound}` : ""}` : "none"],
    ["Style", style], ["Style look", r.look || ""],
    ["Redraw keyframes", (r.restyle ?? s.restyle) ? `yes, with ${r.image_model || s.image_model}` : "no"],
    ["Redraw prompt", r.restyle_prompt || ""], ["Your redraw note", r.restyle_note || ""],
    ["Keyframe check", s.check_keyframes ? "on" : ""], ["Approval stop", s.approve_keyframes ? "on" : ""],
    ["Style LoRAs", (r.loras || []).join(", ")], ["Negative prompt", r.negative || ""],
    ["Loop", s.loop ? "yes" : ""], ["Retry of", j.retry_of || ""],
  ].filter(([, v]) => v !== "" && v != null);
  const prompts = (r.prompts || s.prompts).map((p, i) => `<li><b>${j.kind === "film" ? `Shot ${i + 1}` : `${i + 1} → ${i + 2}`}</b> ${esc(p || "(smooth natural motion)")}</li>`).join("");
  return `<dl class="facts">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(String(v))}</dd>`).join("")}</dl>
    <div class="facts-prompts"><span class="kf-label">Motion prompts${r.prompts ? " (as sent, with the style)" : ""}</span><ol>${prompts}</ol></div>`;
}
function jobCard(j) {
  const sum = jobSummary(j);
  const end = j.finished || Date.now() / 1000, took = j.started ? fmtTime(end - j.started) : "";
  const active = ["queued", "running"].includes(j.status);
  const when = new Date(j.created * 1000).toLocaleString([], { dateStyle: "short", timeStyle: "short" });
  const progress = j.status === "queued" ? `Waiting · position ${j.queue_position || "?"}` : j.status === "running" ? `${j.progress} · ${took}` : "";
  // badges for the collapsed Details row
  const checks = j.keyframe_checks || [], kc = checks[checks.length - 1], r = j.review;
  const badges = [j.styled.length ? "redrawn" : j.settings.restyle && active ? "redrawing" : "",
    kc ? (kc.ok ? "keyframes OK" : "keyframe issues") : "",
    r?.status === "working" ? "checking clip…" : r?.status === "done" ? `clip: ${(VERDICTS[r.verdict] || ["checked"])[0].toLowerCase()}` : r?.status === "failed" ? "check failed" : ""]
    .filter(Boolean).map((b) => `<span class="badge-s${/issues|redo|failed/.test(b) ? " warn" : / OK|good/.test(b) ? " ok" : ""}">${esc(b)}</span>`).join("");
  const open = detailsOpen.has(j.id) ? detailsOpen.get(j.id) : active;
  const flash = S.flashJob?.id === j.id && Date.now() < S.flashJob.until;
  const cancelling = j.status === "running" && j.progress === "cancelling…";
  const statusLabel = cancelling ? "cancelling…" : { approve: "waiting for you", running: "rendering" }[j.status] || j.status;
  return `<div class="job${flash ? " flash" : ""}" data-id="${j.id}">
    <div class="job-head"><span class="status ${j.status}${cancelling ? " cancelling" : ""}">${statusLabel}</span><span class="job-meta">${esc(sum.text)}</span>
      <span class="job-time">${took && !active ? `${took} · ` : ""}${when}</span></div>
    ${progress ? `<div class="progress">${esc(progress)}</div><div class="bar"><span></span></div>` : ""}
    ${j.error ? `<div class="error">${esc(j.error)}</div>` : ""}
    ${j.status === "done" ? `<video controls playsinline loop preload="metadata" src="${jobVideo(j)}"></video>` : ""}
    ${j.status === "approve" ? `<div class="approve">
      <b>Paused: check the redrawn keyframes, then render the video or redraw them.</b>
      ${keyframeStrip(j)}
      <input type="text" data-redraw-note maxlength="600" placeholder="What to change if you redraw (optional), e.g. keep his eyes looking up">
      <div class="row"><button type="button" class="btn small primary" data-approve="render">Render with these</button>
        <button type="button" class="btn small" data-approve="redraw">Redraw again</button></div>
    </div>` : ""}
    <details class="job-details"${open ? " open" : ""}><summary>Details ${badges}</summary>
      ${renderFacts(j)}
      ${keyframeStrip(j)}
      ${reviewBlock(r, ["rerender", "Render again with this prompt"], j.styled.length > 0)}
    </details>
    <div class="row">
      ${j.status === "done" && j.kind !== "film" ? (r?.status === "working"
        ? `<button type="button" class="btn small busy" disabled><span class="spinner"></span>Checking clip…</button>`
        : `<button type="button" class="btn small" data-job-act="check">${r ? "Check again" : "Check clip"}</button>`) : ""}
      ${j.status === "done" ? `<button type="button" class="btn small" data-job-act="sound">Sound</button>
        <a class="btn small" href="${jobVideo(j, null, true)}">Download</a>` : ""}
      ${active || j.status === "approve" ? `<button type="button" class="btn small" data-job-act="cancel">Cancel</button>` : ""}
      ${["failed", "cancelled"].includes(j.status) && j.kind !== "film" ? `<button type="button" class="btn small primary" data-job-act="retry">Try again</button>` : ""}
      ${j.status !== "running" ? `<button type="button" class="btn small ghost danger icon-only" data-job-act="delete" aria-label="Delete this render" title="Delete this render"><svg><use href="#i-trash"/></svg></button>` : ""}
    </div></div>`;
}
// A button that starts work shows it at once (spinner, no double clicks); returns a function that puts it back.
function busyButton(btn, text) {
  if (!btn) return () => {};
  const html = btn.innerHTML;
  btn.disabled = true; btn.classList.add("busy"); btn.innerHTML = `<span class="spinner"></span>${esc(text)}`;
  return () => { if (btn.isConnected) { btn.disabled = false; btn.classList.remove("busy"); btn.innerHTML = html; } };
}
// After queueing a render: bring the list to it and flash it, so it's clear where it went.
function showNewJob(id) {
  S.flashJob = { id, until: Date.now() + 3000 };  // kept in the state: the card is redrawn when its status changes
  (S.seenJobs ||= new Set()).add(id);
  renderJobs();
  setTimeout(() => {  // one jump after the redraw (a smooth scroll is cancelled when the card is redrawn mid-way)
    const el = document.querySelector(`#jobList .job[data-id="${id}"]`); if (!el || !el.offsetParent) return;
    window.scrollTo({ top: Math.max(0, el.getBoundingClientRect().top + scrollY - 80) });
  }, 50);
  setTimeout(() => renderJobs(), 3100);
}
function renderJobs() {
  const list = $("jobList");
  // A render that wasn't in the list before (queued here, from the Director or another device): bring it into view.
  if (S.seenJobs) {
    const fresh = S.jobs.find((j) => !S.seenJobs.has(j.id));
    if (fresh && S.flashJob?.id !== fresh.id) { S.jobs.forEach((j) => S.seenJobs.add(j.id)); return showNewJob(fresh.id); }
  }
  S.seenJobs = new Set([...(S.seenJobs || []), ...S.jobs.map((j) => j.id)]);
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
    if ((!el || el.dataset.html !== html) && !(el && el.contains(document.activeElement))) {  // keep a card being edited
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
// Remember a Details row the user opened or closed (a click on its summary; "toggle" also fires on re-renders).
$("jobList").addEventListener("click", (e) => {
  const summary = e.target.closest(".job-details > summary"); if (!summary) return;
  detailsOpen.set(summary.closest(".job").dataset.id, !summary.parentElement.open);
}, true);
$("jobList").onclick = async (e) => {
  const card = e.target.closest(".job"); if (!card) return;
  const kf = e.target.closest("[data-kf]");
  if (kf) return openKeyframe(card.dataset.id, kf.dataset.kf, false);
  const ap = e.target.closest("[data-approve]");
  if (ap) {
    const note = card.querySelector("[data-redraw-note]")?.value || "";
    const restore = busyButton(ap, ap.dataset.approve === "render" ? "Starting the video…" : "Starting the redraw…");
    try {
      await api(`/api/jobs/${card.dataset.id}/approve`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: ap.dataset.approve, note }) });
      toast(ap.dataset.approve === "render" ? "Rendering the video from these keyframes" : "Redrawing the keyframes again");
      detailsOpen.delete(card.dataset.id); await refreshJobs(); renderJobs();
    } catch (err) { toast(err.message); restore(); }
    return;
  }
  if (e.target.closest('[data-fix="rerender"]')) {
    const prompt = card.querySelector("[data-fix-prompt]").value.trim();
    const restore = busyButton(e.target.closest('[data-fix="rerender"]'), "Queueing the new render…");
    try {
      const nj = await api(`/api/jobs/${card.dataset.id}/rerender`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ prompts: [prompt], keep_styled: !!card.querySelector("[data-keep-styled]")?.checked }) });
      toast("Queued with the new prompt: it's at the top of the list"); await refreshJobs(); renderJobs(); showNewJob(nj.id);
    } catch (err) { toast(err.message); restore(); }
    return;
  }
  const act = e.target.closest("[data-job-act]")?.dataset.jobAct; if (!act) return;
  const id = card.dataset.id;
  if (act === "check") {
    const btn = e.target.closest("[data-job-act]");  // react at once: the request takes a moment
    btn.disabled = true; btn.classList.add("busy"); btn.innerHTML = `<span class="spinner"></span>Starting the check…`;
    detailsOpen.set(id, true);
    if (await checkClip(id)) { toast(`Checking the clip with ${reviewWriter()?.label || "the AI model"}: the result appears under Details`); renderJobs(); }
    else { btn.disabled = false; btn.classList.remove("busy"); btn.textContent = "Check clip"; }
    return;
  }
  if (act === "delete") return deleteJob(id);
  if (act === "sound") return openSound(id);
  if (act === "retry") {  // the same render again: same keyframes, settings and prompts (redrawn keyframes are reused)
    const j = jobById(id);
    const restore = busyButton(e.target.closest("[data-job-act]"), "Queueing…");
    try {
      const nj = await api(`/api/jobs/${id}/rerender`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompts: j.settings.prompts.map((p) => p || "smooth natural motion"), keep_styled: j.styled.length > 0 }) });
      toast("Queued again: it's at the top of the list"); await refreshJobs(); renderJobs(); showNewJob(nj.id);
    } catch (err) { toast(err.message); restore(); }
    return;
  }
  const restore = busyButton(e.target.closest("[data-job-act]"), "Cancelling…");
  try { await api(`/api/jobs/${id}/cancel`, { method: "POST" }); await refreshJobs(); renderJobs(); }
  catch (err) { toast(err.message); restore(); }
};
function renderVideoPage() { renderTray(); renderMotion(); renderJobs(); renderPanelSummary(); }

// Create video's folded sections show a one-line summary; the footer, what the render will make.
function renderPanelSummary() {
  if (!S.info) return;
  const opt = (id) => $(id).selectedOptions?.[0]?.textContent?.replace(/\s*\(.*\)$/, "") || "";
  const restyle = $("restyle").checked;
  $("sumLook").textContent = [styleLabel(S.style), restyle ? `redraw on${$("approveKeyframes").checked ? ", approval stop" : ""}` : ""].filter(Boolean).join(" · ");
  const sound = !$("soundBox").classList.contains("hidden") && $("audio").checked;
  $("sumVideo").textContent = [opt("engine").replace(/ \+ sound$/, ""), opt("vmode"), `${opt("length")} per gap`,
    $("size").value === "preview" ? "preview" : "480p", sound ? "sound" : "", $("loop").checked ? "loop" : ""].filter(Boolean).join(" · ");
  const gaps = Math.max(0, S.tray.length - ($("loop").checked ? 0 : 1));
  $("sumRender").textContent = gaps ? `${S.tray.length} keyframes → about ${gaps * ((+$("length").value - 1) / 16)} s of video` : "Add at least two keyframes";
}
document.querySelector(".video-panel").addEventListener("change", renderPanelSummary);
document.querySelector(".video-panel").addEventListener("input", renderPanelSummary);
document.querySelectorAll(".video-panel details.sec").forEach((d) => {  // remember which sections are open
  try { d.open = store.get(`sec_${d.dataset.sec}`, false); } catch { /* no storage */ }
  d.addEventListener("toggle", () => store.set(`sec_${d.dataset.sec}`, d.open));
});

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

// ---------------------------------------------------------------- director
const D = { list: [], project: null, filled: "", tab: "shots" };
const dirId = () => (location.hash.match(/^#\/director\/([0-9a-f]{12})/) || [])[1];
const SHOT_FIELDS = ["scene", "action", "start_frame", "end_change", "motion", "narration", "seconds", "characters"];
const writerById = (id) => (S.info?.writers || []).find((w) => w.id === id);

async function refreshDirector() {
  const id = dirId();
  if (id) D.project = await api(`/api/director/${id}`);
  else D.list = await api("/api/director");
}
async function saveProject(fields) {
  try { D.project = await api(`/api/director/${D.project.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(fields) }); }
  catch (e) { toast(e.message); }
  renderDirector();
}
const shotsForSave = (shots) => shots.map((s) => Object.fromEntries([["id", s.id], ...SHOT_FIELDS.map((f) => [f, s[f]])]));

function renderDirector() {
  const id = dirId();
  $("dirHome").classList.toggle("hidden", !!id);
  $("dirWork").classList.toggle("hidden", !id);
  if (!id) {
    setHtml($("dirProjects"), D.list.length ? D.list.map((p) => `<a class="char-card" href="#/director/${p.id}">
      <div class="char-art">${p.cover ? `<img loading="lazy" alt="" src="${imgUrl(p.cover)}">` : `<span>${p.shots ? `${p.shots} shots` : "No shots yet"}</span>`}</div>
      <div class="char-text"><b>${esc(p.title)}</b><span>${esc(p.logline || "")}</span></div>
      <div class="row hint" style="margin:0">${p.shots} shot${p.shots === 1 ? "" : "s"} · ${ago(p.updated)}</div></a>`).join("")
      : emptyState("No films yet. Start one with a short idea; the AI model writes the shot list."));
    return;
  }
  const p = D.project;
  if (!p || p.id !== id) return;
  renderDirPanel(p);
  $("dirHeadTitle").textContent = p.title;
  $("dirLogline").textContent = p.logline || (p.shots.length ? "" : "Fill in the brief on the left, choose an AI model and write the script.");
  $("dirFast").checked = p.fast !== false;
  const rendered = p.shots.filter((s) => s.render?.status === "done").length;
  $("dirFilm").disabled = !p.shots.length || rendered < p.shots.length || p.film?.status === "working";
  $("dirFilm").title = rendered < p.shots.length ? `${rendered} of ${p.shots.length} shots rendered` : "";
  renderFilm(p);
  document.querySelectorAll("#dirTabs [data-dtab]").forEach((b) => b.classList.toggle("on", b.dataset.dtab === D.tab));
  const board = D.tab === "board";
  $("dirShots").classList.toggle("hidden", board); $("dirAddShot").classList.toggle("hidden", board);
  $("dirBoard").classList.toggle("hidden", !board);
  if (board) renderBoard(p); else renderShots(p);
}

// ---------------------------------------------------------------- storyboard board
// A production sheet for the film: references, sets and camera plan, the storyboard panels (each shot's start
// keyframe with lens, camera and framing) and lighting/mood notes. The AI writer fills in the text, the GPU draws
// the pictures; Print saves it as a PDF.
const KIND = { character: "Character", vehicle: "Vehicle", prop: "Prop", creature: "Creature" };
function boardPic(item, extra = "") {
  const busy = item.img_status === "working";
  return `<div class="bitem">${item.image ? `<img alt="" src="${imgUrl(item.image)}" data-open-img="${item.image}">`
      : `<div class="bph">${busy ? `<div class="spinner"></div>` : item.img_status === "failed" ? "Not drawn (failed)" : "Not drawn yet"}</div>`}
    ${item.prompt && !busy ? `<button type="button" class="bredo" data-bredraw="${item.key}" title="${item.image ? "Draw again" : "Draw"}" aria-label="Draw ${esc(item.name)}">↻</button>` : ""}
    <div class="bcap">${esc(item.name)}${extra}</div>${item.note ? `<div class="bnote">${esc(item.note)}</div>` : ""}
    ${item.img_status === "failed" ? `<div class="berr">${esc(item.img_error || "")}</div>` : ""}</div>`;
}
function renderBoard(p) {
  const b = p.board, w = writerById(p.writer), ai = p.ai || {};
  const writing = ai.status === "working" && ai.task === "board";
  const items = b ? [...b.references, ...b.environments, ...(b.camera_plan ? [b.camera_plan] : []), ...b.lighting] : [];
  const undrawn = items.filter((i) => i.prompt && !i.image && i.img_status !== "working").length;
  const drawing = items.some((i) => i.img_status === "working");
  const tools = `<div class="board-tools">
    <button type="button" class="btn small${b ? "" : " primary"}" data-board="write"${!p.shots.length || ai.status === "working" || !w?.ready ? " disabled" : ""}>
      ${writing ? "Writing the storyboard…" : b ? "Rewrite storyboard" : "Write storyboard"}</button>
    ${b ? `<button type="button" class="btn small${undrawn ? " primary" : ""}" data-board="draw"${!undrawn || drawing ? " disabled" : ""}>
      ${drawing ? "Drawing pictures…" : undrawn ? `Draw ${undrawn} picture${undrawn === 1 ? "" : "s"}` : "All pictures drawn"}</button>
      <button type="button" class="btn small" data-board="print">Print / PDF</button>` : ""}
    <span class="hint">${!p.shots.length ? "Write the script first." : w?.paid ? `${esc(w.label)}: paid per use.` : ""}
      ${b ? " Pictures are drawn on the GPU; storyboard panels are the shots' start keyframes." : ""}</span></div>`;
  if (!b) {
    setHtml($("dirBoard"), tools + emptyState("The storyboard sheet: character and prop references, sets and a camera plan, every shot with its lens, camera move and framing, and the lighting and mood. The AI model writes it from the shot list."));
    return;
  }
  const spec = Object.fromEntries(b.shots.map((x) => [x.shot_id, x]));
  const setup = Object.fromEntries((b.camera_plan?.cuts || []).map((c) => [c.shot_id, c.setup]));
  const num = Object.fromEntries(p.shots.map((s, i) => [s.id, i + 1]));
  const html = `${tools}<div class="board">
    <div class="board-head"><h2>${esc(p.title)}</h2>
      <span class="meta"><b>Cut count:</b> ${p.shots.length}</span>
      ${b.palette.length ? `<span class="meta"><b>Colour palette:</b> <span class="swatches">${b.palette.map((c) => `<span class="swatch"><i style="background:${c.hex}"></i>${esc(c.name)}</span>`).join("")}</span></span>` : ""}
      ${b.fingerprint ? `<span class="meta"><b>Environment fingerprint:</b> ${esc(b.fingerprint)}</span>` : ""}</div>
    ${b.references.length ? `<div class="bsec"><h3>1. Characters, vehicles and props</h3><div class="bgrid">${b.references.map((r) => boardPic(r, ` · ${KIND[r.kind] || r.kind}`)).join("")}</div></div>` : ""}
    <div class="bsec"><h3>2. Environment / set design</h3><div class="bgrid wide">${b.environments.map((e) => boardPic(e)).join("")}
      ${b.camera_plan?.prompt ? `<div>${boardPic(b.camera_plan)}${b.camera_plan.cuts.length ? `<ol class="cuts">${b.camera_plan.cuts.filter((c) => num[c.shot_id]).sort((x, y) => num[x.shot_id] - num[y.shot_id])
        .map((c) => `<li value="${num[c.shot_id]}">${esc(c.setup)}</li>`).join("")}</ol>` : ""}</div>` : ""}</div></div>
    <div class="bsec"><h3>3. Storyboard</h3><div class="panels">${p.shots.map((s, i) => {
      const x = spec[s.id], img = s.start_image || s.end_image;
      return `<div class="bitem panel-b">${img ? `<img alt="" src="${imgUrl(img)}" data-open-img="${img}">` : `<div class="bph">No keyframe yet: make it on the Shots tab</div>`}
        <span class="pnum">${i + 1}</span>
        <div class="spec">${x ? `<b>${x.lens_mm}mm</b> | ${s.seconds}s | <b>${esc(x.camera)}</b> | <b>${esc(x.framing)}</b> — ${esc(x.note)}` : `${s.seconds}s — ${esc(s.action)}`}
        ${setup[s.id] ? `<div class="bnote">Camera: ${esc(setup[s.id])}</div>` : ""}</div></div>`;
    }).join("")}</div></div>
    <div class="bsec"><h3>4. Lighting / mood / style notes</h3><div class="bfoot">
      <div class="bgrid">${b.lighting.map((l) => boardPic(l)).join("")}</div>
      <div><h3>Mood keywords</h3><div class="mood">${b.mood.map((m) => `<span>${esc(m)}</span>`).join("")}</div></div>
      <div><h3>Cinematography notes</h3><ul>${b.cinematography.map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div></div></div>
  </div>`;
  if ($("dirBoard").dataset.html !== html) { $("dirBoard").innerHTML = html; $("dirBoard").dataset.html = html; }
}
$("dirTabs").onclick = (e) => {
  const b = e.target.closest("[data-dtab]"); if (!b) return;
  D.tab = b.dataset.dtab; store.set("dir_tab", D.tab); renderDirector();
};
D.tab = store.get("dir_tab", "shots");
async function drawBoard(keys) {
  try {
    D.project = await api(`/api/director/${D.project.id}/board/draw`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ keys }) });
  } catch (e) { toast(e.message); }
  renderDirector();
}
$("dirBoard").addEventListener("click", async (e) => {
  const img = e.target.closest("[data-open-img]");
  if (img) { await refreshImages(); return openImage(img.dataset.openImg); }
  const redo = e.target.closest("[data-bredraw]");
  if (redo) return drawBoard([redo.dataset.bredraw]);
  const a = e.target.closest("[data-board]")?.dataset.board, p = D.project;
  if (!a || !p) return;
  if (a === "print") return window.print();
  if (a === "draw") return drawBoard(null);
  if (a === "write") {
    const w = writerById($("dirWriter").value);
    if (w?.paid && !confirm(`${w.label} is billed per use. Write the storyboard with it?`)) return;
    try {
      D.project = await api(`/api/director/${p.id}/write`, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: "board", writer: $("dirWriter").value }) });
    } catch (err) { toast(err.message); }
    renderDirector();
  }
});

function renderDirPanel(p) {
  const fill = `${p.id}|${p.ai?.finished || ""}`;
  if (D.filled !== fill && !document.activeElement?.closest?.("#dirWork .panel")) {  // don't overwrite what is being typed
    D.filled = fill;
    $("dirTitle").value = p.title; $("dirIdea").value = p.brief.idea; $("dirStyle").value = p.brief.style;
    $("dirSeconds").value = p.brief.seconds; $("dirAspect").value = p.brief.aspect; $("dirNotes").value = p.brief.notes;
  }
  setHtml($("dirCast"), S.characters.length ? S.characters.map((c) => `<button type="button" class="cast-chip${p.cast.includes(c.id) ? " on" : ""}" data-dcast="${c.id}">
      ${c.refs[0] ? `<img alt="" src="${imgUrl(c.refs[0])}">` : ""}${esc(c.name)}</button>`).join("")
    : `<p class="hint" style="margin:0">No characters yet. <a href="#/characters">Create them</a> to keep faces consistent; without a cast the AI describes everyone in each prompt.</p>`);
  setHtml($("dirEngine"), S.info.engines.map((e) => `<option value="${e.id}"${e.ready ? "" : " disabled"}>${esc(e.label)}${e.ready ? "" : " (not installed)"}</option>`).join(""));
  $("dirEngine").value = p.engine || "wan22";
  const writers = S.info.writers || [];
  setHtml($("dirWriter"), writers.length ? writers.map((w) => `<option value="${w.id}"${w.ready ? "" : " disabled"}>${esc(w.label)}${w.model ? ` · ${esc(w.model)}` : ""}${w.ready ? "" : " (not set up)"}</option>`).join("")
    : `<option value="">No AI model set up</option>`);
  $("dirWriter").value = p.writer || "";
  const w = writerById(p.writer);
  const note = $("dirWriterNote");
  note.className = "license " + (!w ? "bad" : w.paid ? "warn" : "ok");
  note.textContent = !w ? "Set up an AI model in remote/config.json (see docs/director.md)." :
    w.paid ? "Paid per use: each script or revision is billed by the provider." : "No per-use charge (your subscription or your own model).";
  const ai = p.ai || {}, box = $("dirAi"), working = ai.status === "working";
  box.classList.toggle("hidden", !working && ai.status !== "failed");
  box.className = "ai-status" + (working ? "" : ai.status === "failed" ? " bad" : " hidden");
  const checking = ai.task === "check";
  box.innerHTML = working ? `<div class="spinner"></div>${checking ? "Checking" : ai.task === "board" ? "Writing the storyboard" : "Writing"} with ${esc(writerById(ai.writer)?.label || ai.writer)} · ${fmtTime(Date.now() / 1000 - ai.started)}`
    : ai.status === "failed" ? `<b>${checking ? "The check didn't finish." : ai.task === "board" ? "The storyboard wasn't written." : "The script wasn't written."}</b> ${esc(ai.error || "")}` : "";
  const btn = $("dirWrite");
  btn.disabled = working || !w?.ready;
  btn.innerHTML = `<svg><use href="#i-spark"/></svg>` + (working && ai.task === "write" ? "Writing…" : p.shots.length ? "Revise script" : "Write script");
  const check = $("dirCheck");
  check.classList.toggle("hidden", !p.shots.length);
  check.disabled = working || !w?.ready;
  check.textContent = working && checking ? "Checking…" : "Check continuity";
  const r = p.review;
  setHtml($("dirReview"), r ? `<b>Continuity check</b> <span class="hint">${esc(writerById(r.writer)?.label || r.writer)} · ${ago(r.finished)}</span>
    <p>${esc(r.summary || "")}</p>${r.notes.length ? `<p class="hint">${r.notes.length} note${r.notes.length === 1 ? "" : "s"} on the shots below.</p>
    <button type="button" class="btn small" id="dirFixNotes"${working ? " disabled" : ""}>Fix these with the AI</button>` : `<p class="hint">No problems found.</p>`}` : "");
  $("dirReview").classList.toggle("hidden", !r);
}

function frame(id, label, busy) {
  return id ? `<img alt="${label}" src="${imgUrl(id)}" data-open-img="${id}">`
    : `<div class="ph">${busy ? `<div class="spinner"></div>` : ""}${label}</div>`;
}
function shotCard(p, s, i) {
  const kfBusy = s.kf_status === "working", r = s.render;
  const status = kfBusy ? (s.start_image ? "Making the end keyframe…" : "Making the start keyframe…")
    : s.kf_status === "failed" ? `Keyframes failed: ${s.kf_error || ""}`
    : r?.status === "queued" ? "Render queued" : r?.status === "running" ? `Rendering: ${r.progress || ""}`
    : r?.status === "failed" ? `Render failed: ${r.error || ""}` : r?.status === "done" ? "Rendered"
    : s.start_image && s.end_image ? "Keyframes ready" : "";
  const bad = s.kf_status === "failed" || r?.status === "failed";
  const cast = p.cast.map(charById).filter(Boolean);
  const ta = (f, label, rows, ph) => `<label class="sf">${label}<textarea data-f="${f}" rows="${rows}" placeholder="${ph}">${esc(s[f] || "")}</textarea></label>`;
  return `<div class="shot" data-shot="${s.id}">
    <div class="shot-head"><span class="num">${i + 1}</span>
      <input data-f="scene" value="${esc(s.scene)}" placeholder="Scene, e.g. Lighthouse lamp room, night" aria-label="Scene">
      <select data-f="seconds" aria-label="Length">${[2, 3, 4, 5].map((n) => `<option value="${n}"${s.seconds === n ? " selected" : ""}>${n} s</option>`).join("")}</select>
      <span class="shot-tools"><button type="button" data-sa="up" aria-label="Move up">↑</button><button type="button" data-sa="down" aria-label="Move down">↓</button><button type="button" data-sa="del" aria-label="Delete shot">✕</button></span>
    </div>
    <div class="shot-body">
      <div class="frames">
        ${r?.status === "done" ? `<video controls playsinline loop preload="metadata" src="${jobVideo({ id: s.job_id, audio: null })}"></video>`
          : `<div class="pair">${frame(s.start_image, "Start", kfBusy && !s.start_image)}<span>→</span>${frame(s.end_image, "End", kfBusy && !!s.start_image)}</div>`}
        <button type="button" class="linkish" data-sa="pickstart"${kfBusy ? " disabled" : ""}>Use a library image as the start frame</button>
      </div>
      <div class="shot-fields">
        ${ta("action", "Action", 2, "What happens in this shot")}
        ${ta("start_frame", "Start frame", 3, "Image prompt for the first frame: who, where, framing, light, style")}
        ${ta("end_change", "End change", 2, "One change that makes the last frame, e.g. she turns and smiles")}
        ${ta("motion", "Motion", 2, "Movement and camera, e.g. slow push-in")}
        ${ta("narration", "Narration", 1, "Voice-over or dialogue (optional)")}
        ${(p.review?.notes || []).filter((n) => n.shot_id === s.id).map((n) => `<div class="note">${esc(n.note)}</div>`).join("")}
        ${r?.status === "done" ? reviewBlock(r.review, ["usefix", "Use this prompt and render again"]) : ""}
        ${cast.length ? `<div class="cast-pick small">${cast.map((c) => `<button type="button" class="cast-chip${s.characters.includes(c.id) ? " on" : ""}" data-sc="${c.id}">
          ${c.refs[0] ? `<img alt="" src="${imgUrl(c.refs[0])}">` : ""}${esc(c.name)}</button>`).join("")}</div>` : ""}
      </div>
    </div>
    <div class="shot-foot"><span class="shot-status${bad ? " bad" : ""}">${esc(status)}</span>
      ${s.start_image && !s.end_image && !kfBusy ? `<button type="button" class="btn small" data-sa="kfend">Make end keyframe</button>` : ""}
      <button type="button" class="btn small" data-sa="kf"${kfBusy ? " disabled" : ""}>${s.start_image ? "Remake both keyframes" : "Make keyframes"}</button>
      ${r?.status === "done" && r.review?.status !== "working" ? `<button type="button" class="btn small" data-sa="check">${r.review ? "Check again" : "Check clip"}</button>` : ""}
      <button type="button" class="btn small" data-sa="render"${s.start_image && s.end_image && !["queued", "running"].includes(r?.status) && !kfBusy ? "" : " disabled"}>${r?.status === "done" ? "Render again" : "Render shot"}</button>
    </div></div>`;
}
function renderShots(p) {
  const list = $("dirShots");
  if (!p.shots.length) { setHtml(list, ""); return; }
  if (list.querySelector(".empty")) list.innerHTML = "";
  const existing = new Map([...list.children].map((c) => [c.dataset.shot, c]));
  const ids = new Set(p.shots.map((s) => s.id));
  for (const [id, el] of existing) if (!ids.has(id)) el.remove();
  let prev = null;
  p.shots.forEach((s, i) => {
    const html = shotCard(p, s, i);
    let el = existing.get(s.id);
    if ((!el || el.dataset.html !== html) && !(el && el.contains(document.activeElement))) {  // keep a card being edited
      const tmp = document.createElement("div"); tmp.innerHTML = html;
      const fresh = tmp.firstElementChild; fresh.dataset.html = html;
      if (el) el.replaceWith(fresh);
      el = fresh;
    }
    const want = prev ? prev.nextElementSibling : list.firstElementChild;
    if (want !== el) list.insertBefore(el, want);
    prev = el;
  });
  list.dataset.sig = "";
}
function renderFilm(p) {
  const f = p.film || {}, job = f.job;  // sent with the project: the job list only holds the newest 50
  setHtml($("dirFilmBox"), f.status === "done" && job ? `<div class="film"><video controls playsinline preload="metadata" src="${jobVideo(job)}"></video>
      <div class="row"><b>Film</b><span class="hint" style="margin:0">${f.shots} shots · made ${ago(f.finished)}${job.audio?.mixed ? " · with sound" : ""}</span>
      <button type="button" class="btn small" data-film-sound="${job.id}">Sound</button>
      <a class="btn small" href="${jobVideo(job, null, true)}">Download</a></div></div>`
    : f.status === "working" ? `<div class="ai-status"><div class="spinner"></div>Joining the shots into one film…</div>`
    : f.status === "failed" ? `<div class="ai-status bad"><b>The film wasn't made.</b> ${esc(f.error || "")}</div>` : "");
}

async function newFilm() {
  try {
    const p = await api("/api/director", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: "Untitled film", writer: store.get("writer"), cast: [] }) });
    location.hash = `#/director/${p.id}`;
  } catch (e) { toast(e.message); }
}
$("dirNew").onclick = newFilm;
function briefFields() {
  return { title: $("dirTitle").value, writer: $("dirWriter").value, brief: { idea: $("dirIdea").value, style: $("dirStyle").value,
    seconds: $("dirSeconds").value, aspect: $("dirAspect").value, notes: $("dirNotes").value } };
}
["dirTitle", "dirIdea", "dirStyle", "dirSeconds", "dirAspect", "dirNotes"].forEach((id) => { $(id).onchange = () => saveProject(briefFields()); });
$("dirWriter").onchange = () => { store.set("writer", $("dirWriter").value); saveProject({ writer: $("dirWriter").value }); };
$("dirCast").onclick = (e) => {
  const b = e.target.closest("[data-dcast]"); if (!b || !D.project) return;
  const cast = D.project.cast.includes(b.dataset.dcast) ? D.project.cast.filter((x) => x !== b.dataset.dcast) : [...D.project.cast, b.dataset.dcast];
  saveProject({ cast });
};
$("dirFast").onchange = () => saveProject({ fast: $("dirFast").checked });
$("dirEngine").onchange = () => saveProject({ engine: $("dirEngine").value });
$("dirWrite").onclick = async () => {
  const p = D.project; if (!p) return;
  const w = writerById($("dirWriter").value);
  if (w?.paid && !confirm(`${w.label} is billed per use. Write the script with it?`)) return;
  await saveProject(briefFields());
  try {
    D.project = await api(`/api/director/${p.id}/write`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ instruction: $("dirInstruction").value, writer: $("dirWriter").value }) });
    $("dirInstruction").value = "";
  } catch (e) { toast(e.message); }
  renderDirector();
};

$("dirReview").onclick = (e) => {
  if (!e.target.closest("#dirFixNotes") || !D.project?.review) return;
  const ids = D.project.shots.map((s) => s.id);
  $("dirInstruction").value = "Fix these continuity notes:\n" + D.project.review.notes
    .map((n) => `- Shot ${ids.indexOf(n.shot_id) + 1}: ${n.note}`).join("\n");
  $("dirWrite").click();
};
$("dirCheck").onclick = async () => {
  const p = D.project; if (!p) return;
  const w = writerById($("dirWriter").value);
  if (w?.paid && !confirm(`${w.label} is billed per use. Check the shot list with it?`)) return;
  try {
    D.project = await api(`/api/director/${p.id}/write`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: "check", writer: $("dirWriter").value }) });
  } catch (e) { toast(e.message); }
  renderDirector();
};

async function shotAction(path) {
  try { D.project = await api(`/api/director/${D.project.id}/${path}`, { method: "POST" }); } catch (e) { toast(e.message); return false; }
  renderDirector(); return true;
}
$("dirShots").addEventListener("change", (e) => {
  const f = e.target.dataset.f, card = e.target.closest("[data-shot]"); if (!f || !card) return;
  const shots = D.project.shots.map((s) => s.id === card.dataset.shot ? { ...s, [f]: f === "seconds" ? +e.target.value : e.target.value } : s);
  saveProject({ shots: shotsForSave(shots) });
});
$("dirShots").addEventListener("click", async (e) => {
  const img = e.target.closest("[data-open-img]");
  if (img) { await refreshImages(); return openImage(img.dataset.openImg); }
  const card = e.target.closest("[data-shot]"); if (!card) return;
  const id = card.dataset.shot, shots = [...D.project.shots], i = shots.findIndex((s) => s.id === id);
  const sc = e.target.closest("[data-sc]");
  if (sc) {
    const s = shots[i], has = s.characters.includes(sc.dataset.sc);
    if (!has && s.characters.length >= 3) return toast("Up to 3 characters per shot");
    shots[i] = { ...s, characters: has ? s.characters.filter((x) => x !== sc.dataset.sc) : [...s.characters, sc.dataset.sc] };
    return saveProject({ shots: shotsForSave(shots) });
  }
  if (e.target.closest('[data-fix="usefix"]')) {
    const motion = card.querySelector("[data-fix-prompt]").value.trim(); if (!motion) return;
    busyButton(e.target.closest('[data-fix="usefix"]'), "Queueing…");  // the card redraws when the project updates
    await saveProject({ shots: shotsForSave(shots.map((s) => s.id === id ? { ...s, motion } : s)) });
    if (await shotAction(`shots/${id}/render`)) { await refreshJobs(); toast("New prompt saved; the shot is rendering again"); }
    return;
  }
  const a = e.target.closest("[data-sa]")?.dataset.sa; if (!a) return;
  const busyText = { check: "Starting the check…", kf: "Starting…", kfend: "Starting…", render: "Queueing…" }[a];
  const restore = busyText ? busyButton(e.target.closest("[data-sa]"), busyText) : () => {};
  if (a === "check") { if (await checkClip(shots[i].job_id)) await refreshDirector(); else restore(); return renderDirector(); }
  if (a === "kf") return (await shotAction(`shots/${id}/keyframes`)) || restore();
  if (a === "kfend") return (await shotAction(`shots/${id}/keyframes?keep_start=1`)) || restore();
  if (a === "pickstart") { picker.shot = id; return openPicker("shotstart"); }
  if (a === "render") { if (await shotAction(`shots/${id}/render`)) { await refreshJobs(); toast("Shot queued on the GPU"); } else restore(); return; }
  if (a === "del") { if (!confirm(`Delete shot ${i + 1}?`)) return; shots.splice(i, 1); }
  else { const j = a === "up" ? i - 1 : i + 1; if (j < 0 || j >= shots.length) return; [shots[i], shots[j]] = [shots[j], shots[i]]; }
  saveProject({ shots: shotsForSave(shots) });
});
function setShotStart(shotId, imageId) {
  const shots = shotsForSave(D.project.shots).map((s) => s.id === shotId ? { ...s, start_image: imageId } : s);
  saveProject({ shots });
  toast("Start frame set. Now make the end keyframe from it.");
}
$("dirAddShot").onclick = () => saveProject({ shots: [...shotsForSave(D.project.shots), { scene: "", action: "", start_frame: "", end_change: "", motion: "", narration: "", seconds: 4, characters: [] }] });
$("dirAllKf").onclick = async () => {
  const todo = D.project.shots.filter((s) => !(s.start_image && s.end_image) && s.kf_status !== "working" && s.start_frame && s.end_change);
  if (!todo.length) return toast("Every shot has its keyframes");
  for (const s of todo) if (!await shotAction(`shots/${s.id}/keyframes`)) break;
  toast(`Making keyframes for ${todo.length} shot${todo.length === 1 ? "" : "s"}`);
};
$("dirAllRender").onclick = async () => {
  const todo = D.project.shots.filter((s) => s.start_image && s.end_image && (!s.render || s.render.status === "failed" || s.render.status === "cancelled"));
  if (!todo.length) return toast("No shots are waiting for a render (make the keyframes first)");
  for (const s of todo) if (!await shotAction(`shots/${s.id}/render`)) break;
  await refreshJobs(); toast(`${todo.length} shot${todo.length === 1 ? "" : "s"} queued on the GPU`);
};
$("dirFilm").onclick = async () => { if (await shotAction("film")) refreshJobs(); };
$("dirFilmBox").onclick = (e) => { const b = e.target.closest("[data-film-sound]"); if (b) openSound(b.dataset.filmSound); };
$("dirDelete").onclick = async () => {
  if (!D.project || !confirm(`Delete "${D.project.title}"? Its images and renders stay in the library.`)) return;
  try { await api(`/api/director/${D.project.id}`, { method: "DELETE" }); D.project = null; location.hash = "#/director"; } catch (e) { toast(e.message); }
};

// ---------------------------------------------------------------- library
// Masonry grids (Home, Library): each tile spans as many 4 px rows as its height needs, plus the gap, so tiles of
// any shape pack left to right without holes. Re-measured when pictures load and when the grid's width changes.
const MASONRY_ROW = 4;
function masonryFit(tile) {
  const v = tile.querySelector("video");
  if (v && !v.style.aspectRatio && !v.videoWidth && v.poster && !tile.dataset.probe) {  // its shape from the poster frame
    tile.dataset.probe = "1";
    const im = new Image();
    im.onload = () => { v.style.aspectRatio = `${im.naturalWidth}/${im.naturalHeight}`; masonryFit(tile); };
    im.src = v.poster;
  }
  const gap = tile.parentElement && innerWidth <= 760 ? 8 : 12;
  tile.style.gridRowEnd = `span ${Math.max(1, Math.ceil((tile.getBoundingClientRect().height + gap) / MASONRY_ROW))}`;
}
// measured on the next frame, once the layout has settled (a width change resizes every tile)
const masonryLater = (tiles) => requestAnimationFrame(() => tiles.forEach((t) => t.isConnected && masonryFit(t)));
const masonrySizes = new ResizeObserver((entries) => masonryLater(entries.map((e) => e.target)));  // a tile's height changed
function watchMasonry(grid) {
  const watch = () => { [...grid.children].forEach((t) => masonrySizes.observe(t)); masonryLater([...grid.children]); };
  new MutationObserver(watch).observe(grid, { childList: true });
  new ResizeObserver(() => masonryLater([...grid.children])).observe(grid);
  watch();
}
watchMasonry($("libGrid")); watchMasonry($("homeGrid"));
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
  // the network volume's storage; while the GPU is off, its last reading
  const storage = (old) => !g.disk?.used ? "" : ` · storage ${(g.disk.used / 1e9).toFixed(0)}${g.disk.total ? ` / ${(g.disk.total / 1e9).toFixed(0)}` : ""} GB`
    + (old && g.disk.seen ? ` (${ago(g.disk.seen)})` : "");
  btn.classList.toggle("hidden", !pod || g.state === "stopping");
  if (pod) {
    btn.textContent = g.state === "ready" ? "Stop GPU now" : g.state === "starting" ? "Starting…" : g.state === "failed" ? "Try again" : "Start GPU now";
    btn.disabled = g.state === "starting";
    btn.dataset.act = g.state === "ready" ? "stop" : "start";
  }
  if (pod && g.state !== "ready") {
    const wait = fmtTime(Math.max(0, Date.now() / 1000 - g.since));
    const [pill, cls, name, sub] = {
      off: ["GPU off", "", "GPU off", "Starts by itself when you render" + month + storage(true)],
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
      (pod && g.cost_per_hour ? ` · $${g.cost_per_hour}/h` : "") + month +
      storage(false);
  } else {
    st.textContent = "GPU offline"; st.className = "pill bad";
    card.querySelector(".gpu-name").textContent = "ComfyUI offline";
    card.querySelector(".gpu-sub").textContent = "Start it with start_all.sh, or add a \"gpu\" section to remote/config.json";
    card.querySelector(".meter span").style.width = "0";
  }
  const idle = info.idle_stop || (g.seconds_left != null ? { seconds_left: g.seconds_left } : null), ip = $("idlePill");
  ip.classList.toggle("hidden", !idle || idle.seconds_left > 600);
  if (idle) ip.innerHTML = `Stops in ${Math.max(1, Math.ceil(idle.seconds_left / 60))} min<span class="wide-only"> · keep running</span>`;
  const waiting = S.jobs.filter((j) => j.status === "approve").length;  // redrawn keyframes waiting for an OK
  const q = $("queuePill");
  q.classList.toggle("hidden", !active && !waiting);
  q.textContent = [active ? `${active} in queue` : "", waiting ? `${waiting} waiting for you` : ""].filter(Boolean).join(" · ");
  const badge = $("tabBadge"); badge.classList.toggle("hidden", !active && !waiting); badge.textContent = active + waiting;
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
  if (first) { renderStyles(); renderCheckOption(); }
  // Which models are installed is known only once a GPU pod has run: refresh the menus when that changes.
  const models = JSON.stringify([S.info.image_models.map((m) => m.missing), S.info.engines.map((e) => e.ready)]);
  if (models !== S.modelsSig) {
    S.modelsSig = models;
    renderImageModels(); renderEngines();  // a model that turned out missing falls back to the default
  }
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
  if (S.page === "styles") renderStylesPage();
  if (S.page === "director") renderDirector();
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
      await Promise.all([refreshInfo(), refreshJobs(), slow ? refreshImages() : null, slow ? refreshCharacters() : null,
                         S.page === "director" ? refreshDirector() : null]);
      renderAll();
    } catch (e) {}
  };
  tick(); setInterval(tick, 3000);
}
