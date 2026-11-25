"use strict";
/* Hima ground station: shared helpers. No libraries (the station in the field has no internet). */

// ---------------------------------------------------------------- safe HTML
class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
const raw = s => new Raw(String(s));
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
function _v(v) {
  if (v instanceof Raw) return v.s;
  if (Array.isArray(v)) return v.map(_v).join("");
  if (v === null || v === undefined || v === false) return "";
  return esc(v);
}
function html(strings, ...vals) { let s = ""; strings.forEach((p, i) => { s += p + (i < vals.length ? _v(vals[i]) : ""); }); return new Raw(s); }
const $ = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

// ---------------------------------------------------------------- data
const STORE = {
  get(k, d) { try { const v = localStorage.getItem("hima." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("hima." + k, JSON.stringify(v)); } catch (e) { /* private window */ } },
};
async function getJSON(url) {
  const r = await fetch(url, {cache: "no-store"});
  let doc = null;
  try { doc = await r.json(); } catch (e) { /* not JSON */ }
  if (!r.ok) throw new Error((doc && doc.error) || `${r.status} ${r.statusText}`);
  return doc;
}
async function postJSON(url, body) {
  const r = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json", "X-Hima": "1"}, body: JSON.stringify(body)});
  const doc = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(doc.error || `${r.status}`);
  return doc;
}
function virtualParam() { const v = STORE.get("virtual", null); return v === null ? "" : (v ? "virtual=1" : "virtual=0"); }

// ---------------------------------------------------------------- formatting
const fmt = {
  n(v, d = 0) { return v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toLocaleString("en-US", {maximumFractionDigits: d, minimumFractionDigits: d}); },
  min(s) { return s === null || s === undefined ? "–" : (s / 60).toLocaleString("en-US", {maximumFractionDigits: 1, minimumFractionDigits: 1}); },
  dur(s) {
    if (s === null || s === undefined) return "–";
    if (s < 60) return `${Math.round(s)} s`;
    if (s < 3600) return `${(s / 60).toFixed(1)} min`;
    const h = Math.floor(s / 3600), m = Math.round((s % 3600) / 60);
    return `${h} h ${String(m).padStart(2, "0")} min`;
  },
  t(s) {                                  // mission time m:ss (h:mm:ss over an hour)
    if (s === null || s === undefined) return "–";
    s = Math.max(0, s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = Math.floor(s % 60);
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(x).padStart(2, "0")}` : `${m}:${String(x).padStart(2, "0")}`;
  },
  bytes(b) { if (b < 1024) return `${b} B`; if (b < 1048576) return `${(b / 1024).toFixed(0)} KB`; return `${(b / 1048576).toFixed(1)} MB`; },
  date(d) { if (!d) return "–"; const x = new Date(d + "T00:00:00Z"); return x.toLocaleDateString("en-GB", {day: "numeric", month: "short", year: "numeric", timeZone: "UTC"}); },
  dateShort(d) { const x = new Date(d + "T00:00:00Z"); return x.toLocaleDateString("en-GB", {day: "numeric", month: "short", timeZone: "UTC"}); },
  pct(v) { return v === null || v === undefined ? "–" : `${Math.round(v)}%`; },
  ll(v) { return v === null || v === undefined ? "–" : Number(v).toFixed(6); },
};
const SPECIES = {arabian_oryx: "Arabian oryx", reem_gazelle: "Reem gazelle"};
const spName = s => SPECIES[s] || String(s).replace(/_/g, " ");
const STATUS_TXT = {
  SEVERE_DRONE_DISTURBANCE: "Severe drone disturbance", SUSPECTED_INJURY_OR_ILLNESS: "Suspected injury or illness",
  POSSIBLE_HIDDEN_YOUNG: "Possible hidden young", RAPID_CORRIDOR_TRANSIT: "Rapid corridor transit",
  PROXIMITY_PACING_ALERTNESS: "Proximity pacing / alert", INTERMITTENT_OCCLUSION: "Intermittent occlusion", BASELINE: "Baseline (normal)",
};
const stName = s => STATUS_TXT[s] || String(s).replace(/_/g, " ").toLowerCase().replace(/^./, c => c.toUpperCase());
const SOURCE_TXT = {template: "Fixed template", llm: "AI model", llm_repaired: "AI model, repaired", synthetic: "Synthetic", real: "Camera", video: "Video"};

// ---------------------------------------------------------------- colours (one meaning, one colour, everywhere)
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
const STATUS_COL = {
  SEVERE_DRONE_DISTURBANCE: "#c2342b", SUSPECTED_INJURY_OR_ILLNESS: "#e0672a", POSSIBLE_HIDDEN_YOUNG: "#c99a06",
  RAPID_CORRIDOR_TRANSIT: "#7c5ce6", PROXIMITY_PACING_ALERTNESS: "#d6417f", INTERMITTENT_OCCLUSION: "#6b7c8c", BASELINE: "#3f9a6e",
};
const PALETTE = ["#2f7fc1", "#e08a2e", "#3f9a6e", "#c2342b", "#7c5ce6", "#d6417f", "#1aa3a3", "#8c6d3f", "#6b7c8c", "#b4a21d"];
const SEL_COL = ["#2f7fc1", "#e08a2e", "#3f9a6e", "#7c5ce6", "#d6417f", "#1aa3a3"];
const _catMap = {};
function catColor(group, key) {
  const m = _catMap[group] || (_catMap[group] = {});
  if (!(key in m)) m[key] = PALETTE[Object.keys(m).length % PALETTE.length];
  return m[key];
}
const spColor = s => s === "arabian_oryx" ? cssVar("--oryx") : s === "reem_gazelle" ? cssVar("--gazelle") : catColor("sp", s);
const stColor = s => STATUS_COL[s] || catColor("st", s);
const EVENT_COL = {stress: "#c2342b", proximity: "#d6417f", isolation: "#7c5ce6", running: "#e08a2e", resting: "#3f9a6e", hidden: "#6b7c8c", occlusion: "#6b7c8c"};
const evColor = e => EVENT_COL[e] || catColor("ev", e);
const statusTag = s => html`<span class="status"><i style="background:${stColor(s)}"></i>${stName(s)}</span>`;
const spTag = s => html`<span class="sp"><i style="background:${spColor(s)}"></i>${spName(s)}</span>`;

// ---------------------------------------------------------------- icons (inline SVG paths)
const ICON = {
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  warn: '<path d="M12 3l9.5 17h-19z"/><path d="M12 10v4M12 17h.01"/>',
  ok: '<circle cx="12" cy="12" r="9"/><path d="M8 12.5l2.7 2.7L16 9.8"/>',
  bad: '<circle cx="12" cy="12" r="9"/><path d="M9 9l6 6M15 9l-6 6"/>',
  q: '<circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.6.3-1 .8-1 1.5v.4M12 17h.01"/>',
  virtual: '<path d="M12 2l8.5 5v10L12 22l-8.5-5V7z"/><path d="M12 22V12M20.5 7L12 12 3.5 7"/>',
  left: '<path d="M15 18l-6-6 6-6"/>', right: '<path d="M9 18l6-6-6-6"/>', x: '<path d="M6 6l12 12M18 6L6 18"/>',
  play: '<path d="M7 4.5v15l12-7.5z" fill="currentColor"/>', pause: '<path d="M7 4h3.5v16H7zM13.5 4H17v16h-3.5z" fill="currentColor"/>',
  plus: '<path d="M12 5v14M5 12h14"/>', minus: '<path d="M5 12h14"/>', fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
  print: '<path d="M6 9V3h12v6"/><rect x="3" y="9" width="18" height="8" rx="2"/><path d="M6 14h12v7H6z"/>',
  file: '<path d="M14 3H6v18h12V7z"/><path d="M14 3v4h4"/>', copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/>',
  box: '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 9h16"/>', chev: '<path d="M6 9l6 6 6-6"/>',
  zoom: '<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3M11 8v6M8 11h6"/>',
};
const icon = (n, size) => raw(`<svg viewBox="0 0 24 24" ${size ? `width="${size}" height="${size}"` : ""} fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICON[n] || ""}</svg>`);
const callout = (kind, body) => html`<div class="callout ${kind}">${icon(kind === "virtual" ? "virtual" : kind === "bad" ? "bad" : kind === "ok" ? "ok" : kind === "warn" ? "warn" : "info")}<div>${body}</div></div>`;

// ---------------------------------------------------------------- tooltip (any element with data-tip; its value is HTML we built and escaped)
const tip = $("#tip");
document.addEventListener("mouseover", e => {
  const t = e.target.closest && e.target.closest("[data-tip]");
  if (!t) { tip.classList.remove("on"); return; }
  tip.innerHTML = t.getAttribute("data-tip"); tip.classList.add("on");
});
document.addEventListener("mousemove", e => {
  if (!tip.classList.contains("on")) return;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = e.clientX + 14, y = e.clientY + 14;
  if (x + w > innerWidth - 8) x = e.clientX - w - 14;
  if (y + h > innerHeight - 8) y = e.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
});
document.addEventListener("scroll", () => tip.classList.remove("on"), true);
const tipAttr = parts => esc(String(html(...parts)));   // build a data-tip value: html`` then escape for the attribute
function tipOf(strings, ...vals) { return raw(esc(String(html(strings, ...vals)))); }

// ---------------------------------------------------------------- toasts
function toast(msg, ms = 4200) {
  const t = document.createElement("div"); t.className = "toast"; t.innerHTML = String(html`${icon("info", 16)}<span>${msg}</span>`);
  $("#toasts").appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 260); }, ms);
}

// ---------------------------------------------------------------- copy
async function copyText(text, btn) {
  try { await navigator.clipboard.writeText(text); toast(`Copied ${text}`, 2000); }
  catch (e) { const r = document.createRange(); if (btn) { r.selectNodeContents(btn); getSelection().removeAllRanges(); getSelection().addRange(r); } }
}

// ---------------------------------------------------------------- sortable table
// cols: [{key, label, num, render(row) -> html, sort(row) -> value, cls}]
function table(el, cols, rows, opt = {}) {
  let sortKey = opt.sort || null, dir = opt.dir || -1;
  function draw() {
    const c = cols.find(c => c.key === sortKey);
    const data = c ? rows.slice().sort((a, b) => {
      const va = c.sort ? c.sort(a) : a[c.key], vb = c.sort ? c.sort(b) : b[c.key];
      if (va === vb) return 0; if (va === null || va === undefined) return 1; if (vb === null || vb === undefined) return -1;
      return (va > vb ? 1 : -1) * dir;
    }) : rows;
    el.innerHTML = String(html`<div class="tbl-wrap ${opt.scroll ? "scroll-y" : ""}"><table class="tbl"><thead><tr>${cols.map(c => html`<th class="${c.num ? "n" : ""} ${c.nosort ? "" : "sort"}" data-k="${c.key}" ${c.title ? raw(`title="${esc(c.title)}"`) : ""}>${c.label}${c.key === sortKey ? html`<span class="ar">${dir > 0 ? "▲" : "▼"}</span>` : ""}</th>`)}</tr></thead>
      <tbody>${data.length ? data.map((r, i) => html`<tr class="${opt.onRow ? "click" : ""}" data-i="${rows.indexOf(r)}">${cols.map(c => html`<td class="${c.num ? "n" : ""} ${c.cls || ""}">${c.render ? c.render(r) : (c.num ? fmt.n(r[c.key], c.d || 0) : r[c.key])}</td>`)}</tr>`)
        : html`<tr><td colspan="${cols.length}" class="muted" style="text-align:center;padding:18px">${opt.empty || "Nothing here."}</td></tr>`}</tbody></table></div>`);
    $$("th.sort", el).forEach(th => th.onclick = () => { const k = th.dataset.k; if (sortKey === k) dir = -dir; else { sortKey = k; dir = cols.find(c => c.key === k).num ? -1 : 1; } draw(); });
    if (opt.onRow) $$("tbody tr.click", el).forEach(tr => tr.onclick = () => opt.onRow(rows[+tr.dataset.i]));
  }
  draw();
}

// ---------------------------------------------------------------- horizontal bars (HTML: readable labels, values on the bar)
function hbars(items, opt = {}) {          // items: [{label (html ok), value, color, tip}]
  if (!items.length) return html`<div class="empty">${opt.empty || "None."}</div>`;
  const max = Math.max(...items.map(i => i.value), 1);
  return html`<div class="hbars">${items.map((it, k) => html`<div class="hbar" ${it.tip ? raw(`data-tip="${it.tip}"`) : ""}><span class="lab">${it.label}</span>
    <span class="track"><span class="fill" style="display:block;width:${(it.value / max * 100).toFixed(1)}%;background:${it.color || "var(--accent)"};animation-delay:${k * 40}ms"></span></span>
    <span class="val">${fmt.n(it.value, opt.d || 0)}${opt.unit || ""}</span></div>`)}</div>`;
}

// ---------------------------------------------------------------- box overlay (bbox_xywh_px: centre x, centre y, width, height in frame pixels)
function boxOverlay(boxes, w, h, labelFn) {
  return html`<svg class="ov" viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">${boxes.map(b => {
    const [cx, cy, bw, bh] = b.bbox_xywh_px, col = b.color || "#ff4d3d";
    const lab = labelFn ? labelFn(b) : "";
    return html`<rect x="${cx - bw / 2}" y="${cy - bh / 2}" width="${bw}" height="${bh}" rx="3" style="stroke:${col}"></rect>${lab ? html`<text x="${cx - bw / 2}" y="${Math.max(14, cy - bh / 2 - 5)}" style="font-size:${Math.max(12, w / 70)}px">${lab}</text>` : ""}`;
  })}</svg>`;
}
const boxLabel = b => [b.label || (b.species ? spName(b.species) : ""), b.track_id !== undefined && b.track_id !== null ? `T${b.track_id}` : "", b.conf !== undefined && b.conf !== null ? b.conf.toFixed(2) : ""].filter(Boolean).join(" · ");

// ---------------------------------------------------------------- modal
let _modalKeys = null;
function openModal(content, onKey) {
  closeModal();
  const m = document.createElement("div"); m.id = "modal"; m.innerHTML = String(content);
  m.addEventListener("click", e => { if (e.target === m || e.target.closest("[data-close]")) closeModal(); });
  document.body.appendChild(m); document.body.style.overflow = "hidden";
  _modalKeys = e => { if (e.key === "Escape") closeModal(); else if (onKey) onKey(e); };
  document.addEventListener("keydown", _modalKeys);
  return m;
}
function closeModal() {
  const m = $("#modal"); if (!m) return;
  if (m._stop) m._stop();
  m.remove(); document.body.style.overflow = ""; document.removeEventListener("keydown", _modalKeys); _modalKeys = null;
}

// ---------------------------------------------------------------- image viewer: [{url, title, sub, boxes, info: [[label, value]]}]
function lightbox(items, start) {
  let i = start, showBoxes = true, zoom = 1;
  const m = openModal(html`<div class="lb"><div class="lb-top"><div><div class="t" id="lbT"></div><div class="d" id="lbD"></div></div><span class="spacer"></span>
      <span class="d" id="lbN"></span>
      <button class="btn" id="lbBox">${icon("box")} Boxes</button><button class="btn" id="lbZoom">${icon("zoom")} Zoom</button>
      <a class="btn" id="lbOpen" target="_blank">${icon("file")} Original</a><button class="btn icon" data-close title="Close (Esc)">${icon("x")}</button></div>
    <div class="lb-stage" id="lbS"></div><div class="lb-info" id="lbI"></div></div>`, e => {
    if (e.key === "ArrowLeft") go(-1); else if (e.key === "ArrowRight") go(1); else if (e.key === "b") toggle();
  });
  function render() {
    const it = items[i];
    $("#lbT", m).textContent = it.title; $("#lbD", m).textContent = it.sub || ""; $("#lbN", m).textContent = items.length > 1 ? `${i + 1} / ${items.length}` : "";
    $("#lbOpen", m).href = it.url;
    $("#lbS", m).innerHTML = String(html`<div class="ovwrap" id="lbW" style="transform:scale(${zoom})"><img id="lbImg" src="${it.url}" alt="${it.title}"><span id="lbOv"></span></div>
      ${items.length > 1 ? html`<button class="lb-nav l" id="lbL" title="Previous (←)">${icon("left")}</button><button class="lb-nav r" id="lbR" title="Next (→)">${icon("right")}</button>` : ""}`);
    const img = $("#lbImg", m);
    const ov = () => { $("#lbOv", m).innerHTML = showBoxes && it.boxes && it.boxes.length ? String(boxOverlay(it.boxes, img.naturalWidth, img.naturalHeight, boxLabel)) : ""; };
    img.complete ? ov() : img.addEventListener("load", ov);
    $("#lbI", m).innerHTML = String(html`${(it.info || []).map(([k, v]) => html`<span>${k} <b>${v}</b></span>`)}`);
    if ($("#lbL", m)) { $("#lbL", m).onclick = () => go(-1); $("#lbR", m).onclick = () => go(1); }
    $("#lbBox", m).classList.toggle("primary", showBoxes);
  }
  function go(d) { i = (i + d + items.length) % items.length; render(); }
  function toggle() { showBoxes = !showBoxes; render(); }
  $("#lbBox", m).onclick = toggle;
  $("#lbZoom", m).onclick = () => { zoom = zoom === 1 ? 1.8 : 1; const w = $("#lbW", m); if (w) w.style.transform = `scale(${zoom})`; $("#lbZoom", m).classList.toggle("primary", zoom > 1); };
  render();
}

// ---------------------------------------------------------------- clip player: the clip's frames (JPEG from the server) and the session's boxes on each
async function clipPlayer(mid, clip, list) {
  let k = 0, playing = false, speed = 1, showBoxes = true, raf = 0, last = 0, info = null;
  const imgs = [];
  const m = openModal(html`<div class="lb"><div class="lb-top"><div><div class="t">${clip.id} · ${stName(clip.why)}</div>
      <div class="d">Tracks ${clip.tracks.map(t => "T" + t).join(", ") || "–"} · mission time ${fmt.t(clip.t_start)} – ${fmt.t(clip.t_end)}${clip.events.length ? " · events: " + clip.events.join(", ") : ""}</div></div>
      <span class="spacer"></span><button class="btn primary" id="cpBox">${icon("box")} Boxes</button><button class="btn icon" data-close title="Close (Esc)">${icon("x")}</button></div>
    <div class="lb-stage"><canvas id="cpC" width="960" height="540"></canvas><div class="loadbar" id="cpL" style="width:0"></div></div>
    <div class="player-bar"><button class="btn" id="cpP">${icon("play")}</button><input type="range" id="cpR" min="0" max="0" value="0">
      <span class="tc" id="cpT">loading…</span><div class="seg" id="cpS"><button data-s="0.5">½×</button><button data-s="1" class="on">1×</button><button data-s="2">2×</button><button data-s="4">4×</button></div></div>
    <div class="lb-info small">Boxes: the detector's boxes on each frame, from the archived session (colour = species, label = track). Space plays and pauses; ← → step one frame.</div></div>`, e => {
    if (e.key === " ") { e.preventDefault(); toggle(); } else if (e.key === "ArrowRight") step(1); else if (e.key === "ArrowLeft") step(-1);
  });
  const cv = $("#cpC", m), cx = cv.getContext("2d");
  m._stop = () => { playing = false; cancelAnimationFrame(raf); };
  try { info = await getJSON(`/api/clip/${encodeURIComponent(mid)}/${encodeURIComponent(clip.id)}/info`); }
  catch (e) { $("#cpT", m).textContent = "The clip cannot be read: " + e.message; return; }
  const n = info.n, aspect = info.h / info.w;
  cv.width = 1280; cv.height = Math.round(1280 * aspect);
  $("#cpR", m).max = n - 1;
  let loaded = 0;
  for (let j = 0; j < n; j++) {
    const im = new Image(); im.onload = () => { loaded++; $("#cpL", m).style.width = (loaded / n * 100) + "%"; if (loaded === n) $("#cpL", m).style.opacity = 0; if (j === k) draw(); };
    im.src = `/api/clip/${encodeURIComponent(mid)}/${encodeURIComponent(clip.id)}/${j}`; imgs.push(im);
  }
  function draw() {
    const im = imgs[k];
    if (im && im.complete && im.naturalWidth) cx.drawImage(im, 0, 0, cv.width, cv.height);
    if (showBoxes && info.boxes[k]) {
      const sx = cv.width / info.w, sy = cv.height / info.h;
      cx.lineWidth = 3; cx.font = "600 20px Segoe UI, system-ui, sans-serif";
      for (const b of info.boxes[k]) {
        const [bx, by, bw, bh] = b.bbox_xywh_px, col = spColor(b.species);
        cx.strokeStyle = col; cx.strokeRect((bx - bw / 2) * sx, (by - bh / 2) * sy, bw * sx, bh * sy);
        const lab = `T${b.track_id}`; const tw = cx.measureText(lab).width + 10;
        cx.fillStyle = col; cx.fillRect((bx - bw / 2) * sx, (by - bh / 2) * sy - 24, tw, 22);
        cx.fillStyle = "#fff"; cx.fillText(lab, (bx - bw / 2) * sx + 5, (by - bh / 2) * sy - 7);
      }
    }
    $("#cpR", m).value = k;
    $("#cpT", m).textContent = `frame ${k + 1}/${n} · t ${fmt.t(info.t_start + k / info.fps)} (${(info.t_start + k / info.fps).toFixed(1)} s)`;
  }
  function loop(ts) {
    if (!playing) return;
    if (ts - last >= 1000 / (info.fps * speed)) { last = ts; k++; if (k >= n) { k = n - 1; playing = false; setBtn(); } draw(); }
    raf = requestAnimationFrame(loop);
  }
  function setBtn() { $("#cpP", m).innerHTML = String(icon(playing ? "pause" : "play")); }
  function toggle() { if (k >= n - 1) k = 0; playing = !playing; setBtn(); if (playing) { last = 0; raf = requestAnimationFrame(loop); } }
  function step(d) { playing = false; setBtn(); k = Math.max(0, Math.min(n - 1, k + d)); draw(); }
  $("#cpP", m).onclick = toggle;
  $("#cpR", m).oninput = e => { k = +e.target.value; draw(); };
  $("#cpBox", m).onclick = () => { showBoxes = !showBoxes; $("#cpBox", m).classList.toggle("primary", showBoxes); draw(); };
  $$("#cpS button", m).forEach(b => b.onclick = () => { speed = +b.dataset.s; $$("#cpS button", m).forEach(x => x.classList.toggle("on", x === b)); });
  draw(); toggle();
}

// ---------------------------------------------------------------- tabs with a sliding underline
function placeInk(tabs) {
  const on = $("a.on", tabs), ink = $(".ink", tabs);
  if (!on || !ink) return;
  ink.style.left = on.offsetLeft + "px"; ink.style.width = on.offsetWidth + "px";
}
function skeleton() { return html`<div class="skel-page"><div class="skel a"></div><div class="skel b"></div><div class="skel c"></div></div>`; }
function errorBox(e) { return html`<div class="err"><b>Something went wrong.</b><div class="small" style="margin-top:6px">${e.message || e}</div></div>`; }
