"use strict";
/* Hand-made SVG charts: axes with units, gridlines, legends, a tooltip on every mark. Redrawn on resize. */

const _obs = new WeakMap();
function responsive(el, draw) {
  const run = () => { const w = el.clientWidth; if (w > 0) draw(w); };
  run();
  if (_obs.has(el)) _obs.get(el).disconnect();
  let pending = 0, lastW = el.clientWidth;
  const ro = new ResizeObserver(() => { if (Math.abs(el.clientWidth - lastW) < 4) return; lastW = el.clientWidth; cancelAnimationFrame(pending); pending = requestAnimationFrame(run); });
  ro.observe(el); _obs.set(el, ro);
}
function niceStep(max, n = 5) {
  if (max <= 0) return 1;
  const raw = max / n, p = Math.pow(10, Math.floor(Math.log10(raw))), f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * p;
}
function ticks(max, n = 5, integer = false) {
  let s = niceStep(max, n); if (integer) s = Math.max(1, Math.round(s));
  const out = []; for (let v = 0; v <= max + s * 0.001; v += s) out.push(+v.toFixed(6));
  if (out[out.length - 1] < max) out.push(+(out[out.length - 1] + s).toFixed(6));
  return out;
}
const legendHTML = items => html`<div class="legend">${items.map(i => html`<span><i class="${i.line ? "ln" : ""}" style="background:${i.color}"></i>${i.label}</span>`)}</div>`;

// ---------------------------------------------------------------- vertical bars (stacked or grouped)
// opt: {cats: [{key, label}], series: [{key, label, color}], value(catKey, seriesKey), stacked, yLabel, height, integer, unit, legend}
function vbars(el, opt) {
  responsive(el, W => {
    const H = opt.height || 240, ml = 46, mr = 10, mt = 12, mb = opt.rotate ? 58 : 34;
    const cats = opt.cats, ser = opt.series, iw = W - ml - mr, ih = H - mt - mb;
    const tot = c => opt.stacked ? ser.reduce((a, s) => a + (opt.value(c.key, s.key) || 0), 0) : Math.max(0, ...ser.map(s => opt.value(c.key, s.key) || 0));
    const max = Math.max(1e-9, ...cats.map(tot));
    const tk = ticks(max, 4, opt.integer), top = tk[tk.length - 1];
    const y = v => mt + ih - v / top * ih, band = iw / Math.max(1, cats.length);
    const bw = Math.min(opt.stacked ? 56 : 64, band * 0.72), gw = opt.stacked ? bw : bw / ser.length;
    let s = `<svg width="${W}" height="${H}" role="img">`;
    for (const t of tk) s += `<line class="gridl" x1="${ml}" x2="${W - mr}" y1="${y(t)}" y2="${y(t)}"/><text x="${ml - 8}" y="${y(t) + 4}" text-anchor="end">${fmt.n(t, t % 1 ? 1 : 0)}</text>`;
    if (opt.yLabel) s += `<text transform="translate(12 ${mt + ih / 2}) rotate(-90)" text-anchor="middle" style="font-weight:600">${esc(opt.yLabel)}</text>`;
    cats.forEach((c, i) => {
      const x0 = ml + band * i + (band - bw) / 2;
      let acc = 0;
      ser.forEach((sr, j) => {
        const v = opt.value(c.key, sr.key) || 0;
        if (v <= 0 && opt.stacked) return;
        const x = opt.stacked ? x0 : x0 + gw * j, yy = opt.stacked ? y(acc + v) : y(v), h = opt.stacked ? y(acc) - y(acc + v) : y(0) - y(v);
        const t = esc(`<b>${esc(c.label)}</b><br>${esc(sr.label)}: <b>${fmt.n(v, opt.d || 0)}${opt.unit || ""}</b>`);
        s += `<rect class="bar" data-tip="${t}" x="${x.toFixed(1)}" y="${yy.toFixed(1)}" width="${Math.max(1, gw - (opt.stacked ? 0 : 2)).toFixed(1)}" height="${Math.max(0, h).toFixed(1)}" rx="${opt.stacked ? 2 : 3}" fill="${sr.color}" style="animation-delay:${i * 30}ms"/>`;
        if (!opt.stacked && opt.labels && ser.length <= 3) s += `<text x="${(x + gw / 2).toFixed(1)}" y="${(yy - 4).toFixed(1)}" text-anchor="middle" style="fill:var(--fg-2);font-weight:600">${fmt.n(v, opt.d || 0)}</text>`;
        acc += v;
      });
      if (opt.stacked && opt.labels) s += `<text x="${(x0 + bw / 2).toFixed(1)}" y="${(y(acc) - 5).toFixed(1)}" text-anchor="middle" style="fill:var(--fg-2);font-weight:600">${fmt.n(acc, opt.d || 0)}</text>`;
      const lx = ml + band * i + band / 2;
      s += opt.rotate ? `<text transform="translate(${lx} ${H - mb + 14}) rotate(-35)" text-anchor="end">${esc(c.short || c.label)}</text>`
                      : `<text x="${lx}" y="${H - mb + 18}" text-anchor="middle">${esc(c.short || c.label)}</text>`;
    });
    s += `<line class="axis" x1="${ml}" x2="${W - mr}" y1="${y(0)}" y2="${y(0)}" stroke="var(--axis)"/></svg>`;
    el.innerHTML = s + (opt.legend === false ? "" : String(legendHTML(ser)));
  });
}

// ---------------------------------------------------------------- lines over mission time, with a hover crosshair
// opt: {xs: [t...], series: [{label, color, values: []}], yLabel, height, xFmt}
function lines(el, opt) {
  responsive(el, W => {
    const H = opt.height || 220, ml = 46, mr = 12, mt = 12, mb = 32, iw = W - ml - mr, ih = H - mt - mb;
    const xs = opt.xs, x0 = xs[0], x1 = xs[xs.length - 1] || 1;
    const max = Math.max(1, ...opt.series.flatMap(s => s.values.filter(v => v !== null)));
    const tk = ticks(max, 4, true), top = tk[tk.length - 1];
    const X = t => ml + (t - x0) / Math.max(1e-9, x1 - x0) * iw, Y = v => mt + ih - v / top * ih;
    let s = `<svg width="${W}" height="${H}">`;
    for (const t of tk) s += `<line class="gridl" x1="${ml}" x2="${W - mr}" y1="${Y(t)}" y2="${Y(t)}"/><text x="${ml - 8}" y="${Y(t) + 4}" text-anchor="end">${t}</text>`;
    const xt = ticks(x1 - x0, Math.max(2, Math.floor(iw / 90)));
    for (const t of xt) if (x0 + t <= x1) s += `<text x="${X(x0 + t)}" y="${H - 10}" text-anchor="middle">${(opt.xFmt || fmt.t)(x0 + t)}</text>`;
    if (opt.yLabel) s += `<text transform="translate(12 ${mt + ih / 2}) rotate(-90)" text-anchor="middle" style="font-weight:600">${esc(opt.yLabel)}</text>`;
    opt.series.forEach((sr, j) => {
      const pts = xs.map((t, i) => sr.values[i] === null ? null : `${X(t).toFixed(1)},${Y(sr.values[i]).toFixed(1)}`).filter(Boolean);
      if (j === 0) s += `<path d="M${pts[0]} L${pts.join(" L")} L${X(x1).toFixed(1)},${Y(0)} L${X(x0).toFixed(1)},${Y(0)} Z" fill="${sr.color}" opacity=".12"/>`;
      s += `<polyline points="${pts.join(" ")}" fill="none" stroke="${sr.color}" stroke-width="2" stroke-linejoin="round"/>`;
    });
    s += `<line class="axis" x1="${ml}" x2="${W - mr}" y1="${Y(0)}" y2="${Y(0)}" stroke="var(--axis)"/>`;
    s += `<line id="xh" x1="0" x2="0" y1="${mt}" y2="${mt + ih}" stroke="var(--fg-2)" stroke-dasharray="3 3" opacity="0"/>`;
    s += `<rect id="hov" x="${ml}" y="${mt}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
    el.innerHTML = s + String(legendHTML(opt.series.map(sr => ({...sr, line: true}))));
    const hov = el.querySelector("#hov"), xh = el.querySelector("#xh");
    hov.addEventListener("mousemove", e => {
      const r = hov.getBoundingClientRect(), t = x0 + (e.clientX - r.left) / r.width * (x1 - x0);
      let i = 0, best = Infinity; xs.forEach((v, k) => { const d = Math.abs(v - t); if (d < best) { best = d; i = k; } });
      xh.setAttribute("x1", X(xs[i])); xh.setAttribute("x2", X(xs[i])); xh.setAttribute("opacity", 1);
      hov.setAttribute("data-tip", esc(`<b>${fmt.t(xs[i])}</b> (${fmt.n(xs[i])} s)<br>` + opt.series.map(sr => `${esc(sr.label)}: <b>${sr.values[i] ?? "–"}</b>`).join("<br>")));
    });
    hov.addEventListener("mouseleave", () => xh.setAttribute("opacity", 0));
  });
}

// ---------------------------------------------------------------- timeline: one row per track, a bar per event, alert markers on top
// opt: {rows: [{key, label, color}], bars: [{row, start, end, color, tip}], marks: [{t, color, tip, label}], x0, x1}
function gantt(el, opt) {
  responsive(el, W => {
    const rowH = 22, ml = 92, mr = 12, mt = opt.marks && opt.marks.length ? 30 : 10, mb = 30;
    const H = mt + opt.rows.length * rowH + mb, iw = W - ml - mr;
    const x0 = opt.x0, x1 = opt.x1 > x0 ? opt.x1 : x0 + 1, X = t => ml + (t - x0) / (x1 - x0) * iw;
    let s = `<svg width="${W}" height="${H}">`;
    const xt = ticks(x1 - x0, Math.max(2, Math.floor(iw / 90)));
    for (const t of xt) if (x0 + t <= x1) s += `<line class="gridl" x1="${X(x0 + t)}" x2="${X(x0 + t)}" y1="${mt - 4}" y2="${H - mb}"/><text x="${X(x0 + t)}" y="${H - 10}" text-anchor="middle">${fmt.t(x0 + t)}</text>`;
    opt.rows.forEach((r, i) => {
      const yy = mt + i * rowH;
      if (i % 2 === 0) s += `<rect x="${ml}" y="${yy}" width="${iw}" height="${rowH}" fill="var(--surface-2)"/>`;
      s += `<circle cx="${ml - 80}" cy="${yy + rowH / 2}" r="4" fill="${r.color || "var(--muted)"}"/><text x="${ml - 72}" y="${yy + rowH / 2 + 4}" style="fill:var(--fg-2);font-family:var(--mono);font-size:11.5px">${esc(r.label)}</text>`;
    });
    const idx = Object.fromEntries(opt.rows.map((r, i) => [r.key, i]));
    for (const b of opt.bars) {
      const i = idx[b.row]; if (i === undefined) continue;
      const xa = X(b.start), xb = Math.max(xa + 3, X(b.end));
      s += `<rect class="hb" data-tip="${b.tip}" x="${xa.toFixed(1)}" y="${mt + i * rowH + 4}" width="${(xb - xa).toFixed(1)}" height="${rowH - 8}" rx="3" fill="${b.color}"/>`;
    }
    for (const m of opt.marks || []) {
      const x = X(m.t);
      s += `<g data-tip="${m.tip}" style="cursor:default"><line x1="${x}" x2="${x}" y1="${mt - 6}" y2="${H - mb}" stroke="${m.color}" stroke-width="1.5" stroke-dasharray="2 3" opacity=".75"/>
        <path d="M${x - 6},${mt - 22} h12 v9 l-6,6 l-6,-6 z" fill="${m.color}"/><text x="${x}" y="${mt - 13}" text-anchor="middle" style="fill:#fff;font-size:9px;font-weight:700">${esc(m.label || "")}</text></g>`;
    }
    el.innerHTML = s + "</svg>";
  });
}

// ---------------------------------------------------------------- map: drone path and where each animal was found (east/north metres from home)
function mapView(el, opt) {
  const path = opt.path, pts = opt.points;
  const xs = path.map(p => p[1]).concat(pts.map(p => p.x_m)), ys = path.map(p => p[2]).concat(pts.map(p => p.y_m));
  const minx = Math.min(...xs), maxx = Math.max(...xs), miny = Math.min(...ys), maxy = Math.max(...ys);
  const span = Math.max(maxx - minx, maxy - miny, 20) * 1.12, cx = (minx + maxx) / 2, cy = (miny + maxy) / 2;
  let view = {x: cx - span / 2, y: -(cy + span / 2), w: span, h: span}, sel = null;
  el.innerHTML = String(html`<div class="mapbox" id="mb"><svg id="msv" preserveAspectRatio="xMidYMid meet"></svg>
    <div class="map-ctl no-print"><button class="btn icon" id="mzi" title="Zoom in">${icon("plus")}</button><button class="btn icon" id="mzo" title="Zoom out">${icon("minus")}</button><button class="btn icon" id="mfit" title="Fit">${icon("fit")}</button></div>
    <div class="map-info" id="minfo">Drag to pan, scroll to zoom. Click an animal for its GPS position.</div></div>`);
  const box = el.querySelector("#mb"), sv = el.querySelector("#msv"), info = el.querySelector("#minfo");
  const ar = () => box.clientWidth / Math.max(1, box.clientHeight);
  function fitView() { if (!box.clientWidth || !box.clientHeight) return; const a = ar(); view = a >= 1 ? {x: cx - span * a / 2, y: -(cy + span / 2), w: span * a, h: span} : {x: cx - span / 2, y: -(cy + span / a / 2), w: span, h: span / a}; }
  function draw() {
    if (!box.clientWidth || !box.clientHeight || !isFinite(view.w) || !isFinite(view.h)) return;   // not shown yet
    const k = view.w / Math.max(1, box.clientWidth);           // metres per screen pixel
    let s = `<defs><marker id="arr" viewBox="0 0 10 10" refX="5" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0,0 L10,5 L0,10 z" fill="var(--muted)"/></marker></defs>`;
    // grid every nice step
    const step = niceStep(view.w, 6);
    for (let gx = Math.ceil(view.x / step) * step; gx < view.x + view.w; gx += step) s += `<line x1="${gx}" x2="${gx}" y1="${view.y}" y2="${view.y + view.h}" stroke="var(--grid)" stroke-width="${k}"/>`;
    for (let gy = Math.ceil(view.y / step) * step; gy < view.y + view.h; gy += step) s += `<line y1="${gy}" y2="${gy}" x1="${view.x}" x2="${view.x + view.w}" stroke="var(--grid)" stroke-width="${k}"/>`;
    const d = path.map((p, i) => `${i ? "L" : "M"}${p[1].toFixed(1)},${(-p[2]).toFixed(1)}`).join("");
    s += `<path d="${d}" fill="none" stroke="var(--fg-2)" stroke-opacity=".55" stroke-width="${1.6 * k}" stroke-dasharray="${5 * k} ${4 * k}"/>`;
    if (path.length) {
      s += `<circle cx="${path[0][1]}" cy="${-path[0][2]}" r="${6 * k}" fill="var(--surface)" stroke="var(--fg)" stroke-width="${2 * k}" data-tip="${esc("<b>Take-off / home</b>")}"/>`;
      const L = path[path.length - 1];
      s += `<rect x="${L[1] - 5 * k}" y="${-L[2] - 5 * k}" width="${10 * k}" height="${10 * k}" fill="var(--fg)" data-tip="${esc(`<b>Last position</b><br>${fmt.t(L[0])}`)}"/>`;
    }
    pts.forEach((p, i) => {
      const alert = p.status && p.status !== "BASELINE", r = (alert ? 7 : 5.5) * k, col = spColor(p.species);
      const tipS = esc(`<b>T${p.track_id}</b> · ${esc(spName(p.species))}<br>${esc(stName(p.status || "–"))}<br>found at ${fmt.t(p.t)} · (${p.x_m.toFixed(0)}, ${p.y_m.toFixed(0)}) m` + (p.lat_deg !== null && p.lat_deg !== undefined ? `<br>GPS ${fmt.ll(p.lat_deg)}, ${fmt.ll(p.lon_deg)}` : ""));
      if (alert) s += `<circle cx="${p.x_m}" cy="${-p.y_m}" r="${r + 4 * k}" fill="none" stroke="${stColor(p.status)}" stroke-width="${2.2 * k}"/>`;
      s += `<circle class="apt" data-i="${i}" data-tip="${tipS}" cx="${p.x_m}" cy="${-p.y_m}" r="${r}" fill="${col}" stroke="${sel === i ? "var(--fg)" : "var(--surface)"}" stroke-width="${(sel === i ? 3 : 1.5) * k}" style="cursor:pointer"/>`;
    });
    // scale bar and north arrow in screen units
    const sb = niceStep(view.w / 4, 1), sbpx = sb / k, bx = view.x + 14 * k, by = view.y + view.h - 60 * k;
    s += `<g><rect x="${bx}" y="${by}" width="${sb}" height="${5 * k}" fill="var(--fg)"/><text x="${bx}" y="${by - 6 * k}" style="font-size:${12 * k}px;fill:var(--fg-2)">${sb >= 1000 ? sb / 1000 + " km" : sb + " m"}</text></g>`;
    const nx = view.x + view.w - 70 * k, ny = view.y + 20 * k;
    s += `<g><path d="M${nx},${ny + 22 * k} L${nx},${ny}" stroke="var(--fg-2)" stroke-width="${2 * k}" marker-end="url(#arr)"/><text x="${nx}" y="${ny + 36 * k}" text-anchor="middle" style="font-size:${12 * k}px;fill:var(--fg-2);font-weight:700">N</text></g>`;
    sv.setAttribute("viewBox", `${view.x} ${view.y} ${view.w} ${view.h}`);
    sv.innerHTML = s;
    void sbpx;
  }
  function zoomAt(f, px, py) {
    const r = box.getBoundingClientRect(), mx = view.x + (px - r.left) / r.width * view.w, my = view.y + (py - r.top) / r.height * view.h;
    view = {x: mx - (mx - view.x) * f, y: my - (my - view.y) * f, w: view.w * f, h: view.h * f}; draw();
  }
  box.addEventListener("wheel", e => { e.preventDefault(); zoomAt(Math.exp(e.deltaY * 0.0015), e.clientX, e.clientY); }, {passive: false});
  let drag = null;
  box.addEventListener("pointerdown", e => { if (e.target.closest(".map-ctl, .map-info")) return; drag = {x: e.clientX, y: e.clientY, v: {...view}, moved: false}; box.setPointerCapture(e.pointerId); });
  box.addEventListener("pointermove", e => {
    if (!drag) return; const r = box.getBoundingClientRect(), dx = (e.clientX - drag.x) / r.width * view.w, dy = (e.clientY - drag.y) / r.height * view.h;
    if (Math.abs(e.clientX - drag.x) + Math.abs(e.clientY - drag.y) > 3) { drag.moved = true; box.classList.add("drag"); }
    view = {...view, x: drag.v.x - dx, y: drag.v.y - dy}; draw();
  });
  box.addEventListener("pointerup", e => {
    box.classList.remove("drag");
    if (drag && !drag.moved) {
      const t = document.elementFromPoint(e.clientX, e.clientY), c = t && t.closest && t.closest(".apt");
      if (c) {
        sel = +c.dataset.i; const p = pts[sel];
        const gps = p.lat_deg !== null && p.lat_deg !== undefined ? `${fmt.ll(p.lat_deg)}, ${fmt.ll(p.lon_deg)}` : null;
        info.innerHTML = String(html`<b>T${p.track_id}</b> · ${spTag(p.species)} · ${statusTag(p.status || "BASELINE")}<br>found at ${fmt.t(p.t)}, (${p.x_m.toFixed(0)}, ${p.y_m.toFixed(0)}) m from home
          ${gps ? html`<br>GPS <span class="mono">${gps}</span> <button class="btn" style="padding:2px 8px;margin-left:4px" id="mcp">${icon("copy", 13)} Copy</button>` : ""}`);
        const b = info.querySelector("#mcp"); if (b) b.onclick = () => copyText(gps, b);
        if (opt.onPick) opt.onPick(p);
        draw();
      }
    }
    drag = null;
  });
  el.querySelector("#mzi").onclick = () => { const r = box.getBoundingClientRect(); zoomAt(1 / 1.5, r.left + r.width / 2, r.top + r.height / 2); };
  el.querySelector("#mzo").onclick = () => { const r = box.getBoundingClientRect(); zoomAt(1.5, r.left + r.width / 2, r.top + r.height / 2); };
  el.querySelector("#mfit").onclick = () => { fitView(); draw(); };
  let size = "";
  fitView(); draw();
  new ResizeObserver(() => { const now = box.clientWidth + "x" + box.clientHeight; if (now !== size) { size = now; fitView(); draw(); } }).observe(box);
}
