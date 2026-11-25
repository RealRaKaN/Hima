"use strict";
/* Pages: Overview, Mission, Compare, Review. (Route: route.js.) Each page renders into #view. */

const PAGES = {};
const MCACHE = new Map();               // mission id -> mission JSON (cleared when the archive changes)
let LIST = null;                         // /api/missions

async function missionList(force) { if (!LIST || force) LIST = await getJSON("/api/missions"); return LIST; }
const virtualPill = on => on ? html`<span class="pill virtual" title="Not a real flight: the simulator or the synthetic generator">${icon("virtual", 12)} Virtual</span>` : "";
function shortLabel(row, rows) {
  const same = rows.filter(r => r.flight_date === row.flight_date);
  return fmt.dateShort(row.flight_date) + (same.length > 1 ? ` (${same.indexOf(row) + 1})` : "");
}

// =============================================================================================== Overview
let OV_RANGE = {start: null, end: null};
PAGES.overview = async (v) => {
  const q = [virtualParam(), OV_RANGE.start ? "start=" + OV_RANGE.start : "", OV_RANGE.end ? "end=" + OV_RANGE.end : ""].filter(Boolean).join("&");
  const d = await getJSON("/api/overview?" + q);
  const t = d.totals, counted = new Set(d.counted);
  const rows = d.rows.filter(r => counted.has(r.mission_id));
  const sp = t ? Object.entries(t.tracks_by_species) : [];
  v.innerHTML = String(html`
  <div class="page-head"><div><div class="eyebrow">Ground archive</div><h1>Overview</h1>
    <p>${d.rows.length} flights in the archive${d.real_count ? html` · ${d.real_count} real` : ""}${d.virtual_count ? html` · ${d.virtual_count} virtual` : ""}</p></div>
    ${d.days.length > 1 ? html`<div class="row no-print"><label class="small muted">From <input type="date" id="dS" value="${OV_RANGE.start || d.days[0]}" min="${d.days[0]}" max="${d.days[d.days.length - 1]}" class="btn"></label>
      <label class="small muted">to <input type="date" id="dE" value="${OV_RANGE.end || d.days[d.days.length - 1]}" min="${d.days[0]}" max="${d.days[d.days.length - 1]}" class="btn"></label>
      ${OV_RANGE.start || OV_RANGE.end ? html`<button class="btn ghost" id="dR">All dates</button>` : ""}</div>` : ""}</div>
  <div class="callouts">
    ${Object.entries(d.problems).map(([m, r]) => callout("bad", html`<b>${m}</b> does not verify: ${r.slice(0, 3).join("; ")}`))}
    ${d.virtual_count && d.include_virtual ? callout("virtual", html`The totals count <b>${d.virtual_count} virtual flights</b> (the simulator's flight and synthetic flights; not real). They leave the totals when the first real flight arrives, or switch <b>Count virtual flights</b> off.`) : ""}
    ${d.virtual_count && !d.include_virtual ? callout("virtual", html`${d.virtual_count} virtual flights are not counted here (switch <b>Count virtual flights</b> on to see them).`) : ""}
  </div>
  ${!t ? html`<div class="empty">No flight in these dates${d.virtual_count && !d.include_virtual ? " (virtual flights are not counted)" : ""}.</div>` : html`
  <div class="kpis">
    <div class="kpi"><div class="l">Flights</div><div class="v">${t.missions}</div><div class="s">${Object.keys(d.trend.days).length ? d.trend.days.length + " flight days" : ""}</div></div>
    <div class="kpi"><div class="l">Flight time</div><div class="v">${fmt.dur(t.flight_s)}</div><div class="s">${fmt.n(t.flight_s / 60, 0)} min in the air</div></div>
    <div class="kpi"><div class="l">Animal tracks</div><div class="v">${fmt.n(t.tracks)}</div>
      <div class="mini">${sp.map(([k, n]) => html`<i style="width:${n / Math.max(1, t.tracks) * 100}%;background:${spColor(k)}" data-tip="${tipOf`<b>${spName(k)}</b>: ${n}`}"></i>`)}</div>
      <div class="s">${sp.map(([k, n], i) => html`${i ? " · " : ""}${spName(k)} ${n}`)}</div></div>
    <div class="kpi alert"><div class="l">Tracks in alert</div><div class="v">${t.alert_tracks}</div><div class="s">${fmt.pct(t.alert_tracks / Math.max(1, t.tracks) * 100)} of all tracks</div></div>
    <div class="kpi"><div class="l">Uncertain detections</div><div class="v">${fmt.n(Object.values(t.uncertain).reduce((a, b) => a + b, 0))}</div><div class="s">${t.uncertain_with_image} with an image to review</div></div>
    <div class="kpi"><div class="l">Archive check</div><div class="v" style="color:${Object.keys(d.problems).length ? "var(--bad)" : "var(--ok)"}">${Object.keys(d.problems).length ? Object.keys(d.problems).length + " bad" : "All OK"}</div><div class="s">SHA-256 of every archived file</div></div>
  </div>
  <div class="grid g-21" style="margin-bottom:16px">
    <div class="card"><div class="card-h"><h2>Tracks per flight</h2><span class="sub">bars stacked by species · hover for counts</span></div><div class="chart" id="cFlights"></div></div>
    <div class="card"><div class="card-h"><h2>Status of every track</h2><span class="sub">most serious first</span></div>
      ${hbars(d.status_order.map(s => ({label: statusTag(s), value: t.statuses[s] || 0, color: stColor(s), tip: tipOf`<b>${stName(s)}</b>: ${t.statuses[s] || 0} tracks`})))}</div>
  </div>
  ${d.trend.days.length > 1 ? html`<div class="grid g3" style="margin-bottom:16px">
    <div class="card"><div class="card-h"><h2>Tracks per day</h2><span class="sub">by species</span></div><div class="chart" id="cDayTracks"></div></div>
    <div class="card"><div class="card-h"><h2>Flight time per day</h2><span class="sub">minutes</span></div><div class="chart" id="cDayMin"></div></div>
    <div class="card"><div class="card-h"><h2>Tracks in alert per day</h2><span class="sub">tracks</span></div><div class="chart" id="cDayAlert"></div></div></div>` : ""}
  <div class="card" style="margin-bottom:16px"><div class="card-h"><h2>Flights</h2><span class="sub">newest first · click a flight to open it</span></div><div id="tFlights"></div></div>
  <div class="grid g3">
    <div class="card"><div class="card-h"><h2>Behaviour events</h2><span class="sub">all tracks</span></div>${hbars(Object.entries(t.events).sort((a, b) => b[1] - a[1]).map(([k, n]) => ({label: k, value: n, color: evColor(k)})), {empty: "No events."})}</div>
    <div class="card"><div class="card-h"><h2>Why the detector was unsure</h2><span class="sub">uncertain detections</span></div>${hbars(Object.entries(t.uncertain).sort((a, b) => b[1] - a[1]).map(([k, n]) => ({label: k.replace(/_/g, " "), value: n, color: "var(--info)"})), {empty: "None."})}</div>
    <div class="card"><div class="card-h"><h2>Who wrote the reports</h2><span class="sub">landing reports</span></div>${hbars(Object.entries(t.report_sources).map(([k, n]) => ({label: SOURCE_TXT[k] || k, value: n, color: k === "llm" ? "var(--ok)" : k === "template" ? "var(--muted)" : "var(--warn)"})))}</div>
  </div>
  <p class="small muted" style="margin-top:14px">${d.note}</p>`}`);
  const dS = $("#dS"), dE = $("#dE");
  if (dS) { dS.onchange = () => { OV_RANGE.start = dS.value; route(); }; dE.onchange = () => { OV_RANGE.end = dE.value; route(); }; }
  if ($("#dR")) $("#dR").onclick = () => { OV_RANGE = {start: null, end: null}; route(); };
  if (!t) return;
  const species = Object.keys(t.tracks_by_species);
  const chron = rows.slice().reverse();
  vbars($("#cFlights"), {cats: chron.map(r => ({key: r.mission_id, label: `${r.label}${r.virtual ? " (virtual)" : ""}`, short: shortLabel(r, chron)})),
    series: species.map(s => ({key: s, label: spName(s), color: spColor(s)})), stacked: true, integer: true, labels: true, yLabel: "tracks",
    value: (m, s) => (chron.find(r => r.mission_id === m).tracks_by_species[s] || 0), rotate: chron.length > 8, height: 260});
  if (d.trend.days.length > 1) {
    const days = d.trend.days.map(x => ({key: x.date, label: fmt.date(x.date), short: fmt.dateShort(x.date)}));
    vbars($("#cDayTracks"), {cats: days, series: species.map(s => ({key: s, label: spName(s), color: spColor(s)})), stacked: true, integer: true, yLabel: "tracks",
      value: (day, s) => (d.trend.species.find(x => x.date === day && x.species === s) || {}).tracks || 0});
    vbars($("#cDayMin"), {cats: days, series: [{key: "m", label: "Flight minutes", color: "var(--accent)"}], legend: false, yLabel: "minutes", d: 1,
      value: day => d.trend.days.find(x => x.date === day).flight_min});
    vbars($("#cDayAlert"), {cats: days, series: [{key: "a", label: "Tracks in alert", color: "var(--bad)"}], legend: false, integer: true, yLabel: "tracks",
      value: day => d.trend.days.find(x => x.date === day).alert_tracks});
  }
  const all = d.rows;
  table($("#tFlights"), [
    {key: "flight_date", label: "Date", render: r => fmt.date(r.flight_date)},
    {key: "mission_id", label: "Flight", render: r => html`<span class="mono">${r.mission_id}</span> ${virtualPill(r.virtual)}${counted.has(r.mission_id) ? "" : html` <span class="pill plain" title="Not in the totals above">not counted</span>`}`},
    {key: "source", label: "Source", render: r => SOURCE_TXT[r.source] || r.source},
    {key: "duration_s", label: "Flight time", num: true, render: r => fmt.dur(r.duration_s)},
    {key: "tracks", label: "Tracks", num: true, render: r => html`<div class="cellbar"><span class="stackbar" style="width:110px">${Object.entries(r.tracks_by_species).map(([k, n]) => html`<i style="width:${n / Math.max(1, r.tracks) * 100}%;background:${spColor(k)}" data-tip="${tipOf`${spName(k)}: <b>${n}</b>`}"></i>`)}</span><b>${r.tracks}</b></div>`},
    {key: "alert_tracks", label: "In alert", num: true, render: r => r.alert_tracks ? html`<b style="color:var(--bad)">${r.alert_tracks}</b>` : "0"},
    {key: "uncertain", label: "Uncertain", num: true},
    {key: "report_source", label: "Report", render: r => SOURCE_TXT[r.report_source] || r.report_source},
    {key: "verified", label: "Check", render: r => r.verified ? html`<span class="pill ok">OK</span>` : html`<span class="pill bad" data-tip="${tipOf`${r.problems.join("; ")}`}">changed</span>`},
  ], all, {onRow: r => go(`#/mission/${r.mission_id}`)});
};

// =============================================================================================== Mission
const MTABS = [["report", "Report"], ["animals", "Animals"], ["timeline", "Timeline"], ["map", "Map"], ["media", "Images & clips"], ["log", "Flight log"], ["files", "Files"]];
PAGES.mission = async (v, args) => {
  const list = await missionList();
  if (!list.rows.length) { v.innerHTML = String(html`<div class="page-head"><h1>Mission</h1></div><div class="empty">The archive is empty. Packages arrive from the drone over Wi-Fi, or by hand: <span class="mono">python -m hima ingest &lt;package&gt; --archive …</span></div>`); return; }
  let mid = args[0] || (APP.state && APP.state.newest) || list.rows[0].mission_id;
  if (!list.rows.find(r => r.mission_id === mid)) { await missionList(true); if (!LIST.rows.find(r => r.mission_id === mid)) mid = LIST.rows[0].mission_id; }
  const tab = MTABS.find(t => t[0] === args[1]) ? args[1] : "report";
  if (!MCACHE.has(mid)) { v.innerHTML = String(skeleton()); MCACHE.set(mid, await getJSON(`/api/mission/${encodeURIComponent(mid)}`)); }
  const m = MCACHE.get(mid), rows = LIST.rows, i = rows.findIndex(r => r.mission_id === mid), row = rows[i];
  const rep = m.report, sp = Object.entries(rep.tracks_by_species);
  const counts = {report: rep.narrative ? rep.incidents.length : rep.findings.length, animals: m.animals.length, timeline: m.events.length, media: m.media.clips.length + m.media.stills.length + m.media.images.length, log: m.flight ? m.flight.events.length : 0};
  v.innerHTML = String(html`
  <div class="page-head"><div class="row" style="gap:12px">
      <div class="picker" id="pk"><button class="picker-btn" id="pkB"><div style="flex:1;min-width:0"><div class="t">${fmt.date(row.flight_date)} · <span class="mono">${mid}</span></div>
        <div class="d">${fmt.dur(rep.duration_s)} · ${rep.tracks_seen} tracks · ${rep.alert_track_ids.length} in alert · ${SOURCE_TXT[row.source] || row.source}</div></div>${virtualPill(m.virtual)}${icon("chev", 16)}</button></div>
      <button class="btn icon" id="pkPrev" title="Newer flight" ${i <= 0 ? "disabled" : ""}>${icon("left")}</button><button class="btn icon" id="pkNext" title="Older flight" ${i >= rows.length - 1 ? "disabled" : ""}>${icon("right")}</button>
    </div><div class="row no-print"><button class="btn" id="prt">${icon("print")} Print report</button></div></div>
  ${m.virtual ? html`<div class="callouts">${callout("virtual", m.main_virtual
      ? html`<b>Virtual flight.</b> ArduPilot SITL and the simulator's video: not a real flight. It is always here so the dashboard never opens empty; real flights show first when they arrive.`
      : html`<b>Virtual flight.</b> The synthetic generator (no video, no flight log): not a real flight. It is here so Compare has flights to compare.`)}</div>` : ""}
  ${m.session_error ? html`<div class="callouts">${callout("bad", html`The archived session cannot be read: ${m.session_error}`)}</div>` : ""}
  <div class="kpis">
    <div class="kpi"><div class="l">Flight time</div><div class="v">${fmt.dur(rep.duration_s)}</div><div class="s">${rep.metrics_known ? "with telemetry and camera data" : "no telemetry: nothing placed on the ground"}</div></div>
    <div class="kpi"><div class="l">Animal tracks</div><div class="v">${rep.tracks_seen}</div>
      <div class="mini">${sp.map(([k, n]) => html`<i style="width:${n / Math.max(1, rep.tracks_seen) * 100}%;background:${spColor(k)}"></i>`)}</div>
      <div class="s">${sp.map(([k, n], j) => html`${j ? " · " : ""}<span class="sp"><i style="background:${spColor(k)}"></i>${spName(k)} ${n}</span>`)}</div></div>
    <div class="kpi alert"><div class="l">Tracks in alert</div><div class="v">${rep.alert_track_ids.length}</div><div class="s mono">${rep.alert_track_ids.map(x => "T" + x).join(" ") || "none"}</div></div>
    <div class="kpi"><div class="l">Clips · images</div><div class="v">${m.media.clips.filter(c => c.url).length}<small>clips</small> ${m.media.stills.filter(c => c.url).length + m.media.images.filter(c => c.url).length}<small>images</small></div><div class="s">${m.media.frame ? `${m.media.frame.w}×${m.media.frame.h} px video` : "no video in this flight"}</div></div>
    <div class="kpi"><div class="l">Report written by</div><div class="v" style="font-size:19px;margin-top:9px">${SOURCE_TXT[rep.source] || rep.source}</div><div class="s">${rep.model ? html`model <span class="mono">${rep.model}</span>` : ""}</div></div>
  </div>
  <div class="tabs" id="mtabs">${MTABS.map(([k, l]) => html`<a href="#/mission/${mid}/${k}" class="${k === tab ? "on" : ""}">${l}${counts[k] ? html`<span class="cnt">${counts[k]}</span>` : ""}</a>`)}<span class="ink"></span></div>
  <div id="mbody" class="view-in"></div>`);
  placeInk($("#mtabs")); requestAnimationFrame(() => placeInk($("#mtabs")));
  $("#pkPrev").onclick = () => go(`#/mission/${rows[i - 1].mission_id}/${tab}`);
  $("#pkNext").onclick = () => go(`#/mission/${rows[i + 1].mission_id}/${tab}`);
  $("#prt").onclick = () => { if (tab !== "report") { go(`#/mission/${mid}/report`); setTimeout(() => print(), 500); } else print(); };
  pickerMenu($("#pk"), rows, mid, tab);
  MISSION_TABS[tab]($("#mbody"), m);
};

function pickerMenu(pk, rows, mid, tab) {
  $("#pkB", pk).onclick = e => {
    e.stopPropagation();
    if ($(".picker-menu", pk)) { $(".picker-menu", pk).remove(); return; }
    const menu = document.createElement("div"); menu.className = "picker-menu";
    const draw = f => {
      const fl = rows.filter(r => !f || (r.mission_id + " " + r.flight_date + " " + r.source).toLowerCase().includes(f));
      $("#pmL", menu).innerHTML = String(html`${fl.map(r => html`<div class="pm-item ${r.mission_id === mid ? "on" : ""}" data-id="${r.mission_id}"><span class="t">${fmt.date(r.flight_date)} · <span class="mono">${r.mission_id}</span></span>
        <span class="d">${fmt.dur(r.duration_s)} · ${r.tracks} tracks · ${r.alert_tracks} in alert · ${SOURCE_TXT[r.source] || r.source}</span><span class="p">${virtualPill(r.virtual)}</span></div>`)}`);
      $$(".pm-item", menu).forEach(x => x.onclick = () => go(`#/mission/${x.dataset.id}/${tab}`));
    };
    menu.innerHTML = String(html`<input placeholder="Find a flight (date, id, source)" id="pmF"><div id="pmL"></div>`);
    pk.appendChild(menu); draw(""); $("#pmF", menu).focus();
    $("#pmF", menu).oninput = e2 => draw(e2.target.value.toLowerCase());
    const off = ev => { if (!pk.contains(ev.target)) { menu.remove(); document.removeEventListener("click", off); } };
    document.addEventListener("click", off);
  };
}

const MISSION_TABS = {};

MISSION_TABS.report = (el, m) => {
  const rep = m.report, g = rep.gate || {}, fl = m.flight;
  const sent = fl ? fl.events : [];
  el.innerHTML = String(html`<div class="grid g-21">
    <div class="stack">
      <div class="card"><div class="card-h"><h2>Landing report</h2><span class="row"><span class="pill ${rep.source === "llm" ? "ok" : rep.source === "template" ? "plain" : "warn"}">${SOURCE_TXT[rep.source] || rep.source}</span>${rep.model ? html`<span class="pill plain mono">${rep.model}</span>` : ""}</span></div>
        <p class="headline${rep.narrative ? " lead" : ""}">${rep.headline}</p>
        ${rep.narrative ? html`
          <h3 class="rep-h">The flight</h3><p class="rep-p">${rep.flight.join(" ")}</p>
          <h3 class="rep-h">Incidents</h3>
          ${rep.incidents.length ? html`<div class="incidents">${rep.incidents.map(x => html`<div class="incident"><div class="inc-t">${x.title}${x.at_min != null ? html`<span class="muted"> · minute ${x.at_min}</span>` : ""}</div>
            <p class="rep-p">${x.text}</p><p class="rep-p follow"><b>Follow-up:</b> ${x.follow_up}</p></div>`)}</div>` : html`<p class="rep-p muted">None.</p>`}
          <h3 class="rep-h">Closing</h3><p class="rep-p">${rep.closing}</p>` : ""}
        ${rep.source === "template" ? html`<div style="margin-top:12px">${callout("info", "The fixed template wrote this report: the AI model's text was not used (it failed the checks below) or was not asked.")}</div>` : ""}
        ${rep.source === "llm_repaired" ? html`<div style="margin-top:12px">${callout("warn", html`Template text replaced the model's for: ${(g.repaired || []).join(", ")}`)}</div>` : ""}
      </div>
      ${rep.groups && rep.groups.length ? html`<div class="card"><div class="card-h"><h2>Groups</h2><span class="sub">the data behind the report: tracks found in one place (within ${rep.link_m} m of each other)</span></div>
        <div class="tbl-wrap"><table class="tbl"><thead><tr><th>Group</th><th>From launch</th><th>Tracks</th><th>Seen together</th><th>Statuses</th><th class="n">Minutes</th></tr></thead><tbody>
        ${rep.groups.map(gr => html`<tr><td class="mono">${gr.group}</td><td style="white-space:nowrap">${gr.distance_from_launch_m >= 1000 ? (gr.distance_from_launch_m / 1000).toFixed(1) + " km" : gr.distance_from_launch_m + " m"} ${gr.direction_from_launch}</td>
          <td>${Object.entries(gr.by_species).map(([sp, n]) => html`<div style="white-space:nowrap">${n} ${SPECIES[sp] || sp}</div>`)}</td>
          <td>${Object.entries(gr.seen_together || {}).map(([sp, n]) => html`<div style="white-space:nowrap">≥ ${n} ${SPECIES[sp] || sp}</div>`)}</td>
          <td>${Object.entries(gr.statuses).filter(([st]) => st !== "BASELINE").map(([st, n]) => html`<span style="white-space:nowrap">${n} × ${statusTag(st)}</span> `)}${Object.keys(gr.statuses).every(st => st === "BASELINE") ? html`<span class="muted">all baseline</span>` : ""}</td>
          <td class="n" style="white-space:nowrap">${gr.first_seen_min}–${gr.last_seen_min}</td></tr>`)}
        </tbody></table></div>
        ${rep.vehicle_list && rep.vehicle_list.length ? html`<p class="small muted" style="margin:10px 2px 0">${rep.vehicle_list.map(v => `${v.vehicle}: ${v.kind} at ${v.seen_min} min` + (v.nearest_group ? `, ${v.distance_to_group_m} m from the centre of ${v.nearest_group}` : ", place not known")).join(" · ")}</p>` : ""}
      </div>` : ""}
      ${rep.narrative ? "" : html`<div class="card"><div class="card-h"><h2>Findings</h2><span class="sub">${rep.findings.length} tracks the report names · colour = status</span></div>
        ${rep.findings.length ? html`<div class="findings">${rep.findings.map(f => html`<div class="finding" style="--c:${stColor(f.status)}"><div class="top"><span class="tid">T${f.track_id}</span>${statusTag(f.status)}</div><p>${f.note}</p></div>`)}</div>` : html`<div class="empty">No track needed a finding.</div>`}
      </div>`}
      <div class="card"><div class="card-h"><h2>Messages sent in flight</h2><span class="sub">what the drone sent over the radio, by mission time</span></div>
        ${sent.length ? html`<div class="seg no-print" id="msgSeg" style="margin-bottom:12px"><button data-f="" class="on">All ${sent.length}</button><button data-f="alert">Alerts ${sent.filter(x => x.what.includes("alert")).length}</button><button data-f="periodic">Periodic analyses ${sent.filter(x => x.what === "periodic analysis").length}</button><button data-f="window">AI camera ${sent.filter(x => x.what === "AI camera window").length}</button></div><div class="tl scroll-y" id="msgs"></div>`
          : html`<div class="empty">${m.virtual && !m.main_virtual ? "A synthetic flight has no flight log." : "No flight log in this package."}</div>`}
      </div>
    </div>
    <div class="stack">
      <div class="card"><div class="card-h"><h2>Checks on the AI's text</h2>${g.accepted === undefined ? "" : g.accepted ? html`<span class="pill ok">${icon("ok", 13)} accepted</span>` : html`<span class="pill bad">${icon("bad", 13)} not accepted</span>`}</div>
        <p class="small muted" style="margin-top:0">Every number and status in the model's text is checked against the flight's own facts; a sentence that fails is replaced by the template's.</p>
        ${(g.reasons || []).length ? html`<details ${g.reasons.length <= 4 ? "open" : ""}><summary class="small" style="cursor:pointer;font-weight:600">${g.reasons.length} reasons</summary><ul class="small" style="padding-left:18px;margin:8px 0 0">${g.reasons.map(r => html`<li style="margin-bottom:4px">${r}</li>`)}</ul></details>` : html`<div class="small muted">No problem found.</div>`}
      </div>
      ${rep.judge.length ? html`<div class="card"><div class="card-h"><h2>Second reading</h2><span class="sub">a judge model reads each part against the facts</span></div>
        <div class="verdicts">${rep.judge.map(j => html`<div class="verdict"><span style="color:${j.verdict === "supported" ? "var(--ok)" : j.verdict ? "var(--bad)" : "var(--muted)"}">${icon(j.verdict === "supported" ? "ok" : j.verdict ? "bad" : "q")}</span>
          <span class="mono">${j.part}</span><span>${j.verdict || "no verdict"}${j.problem ? html`<span class="muted"> · ${j.problem}</span>` : ""}</span></div>`)}</div></div>` : ""}
      ${fl ? html`<div class="card"><div class="card-h"><h2>Flight</h2></div><div class="small" style="display:grid;grid-template-columns:auto 1fr;gap:4px 12px">
        <span class="muted">Ended</span><span>${fl.end || "–"}</span>
        ${fl.camera ? html`<span class="muted">Camera</span><span>${fl.camera.device} · ${fl.camera.width_px}×${fl.camera.height_px}</span><span class="muted">Frames</span><span>${fmt.n(fl.camera.frames_taken)} analysed of ${fmt.n(fl.camera.frames_read)}</span>` : ""}
        <span class="muted">Pre-flight</span><span>${fl.preflight.filter(p => p.level === "OK").length} of ${fl.preflight.length} checks OK</span></div></div>` : ""}
    </div></div>`);
  if (sent.length) {
    const draw = f => {
      const list = sent.filter(x => !f || (f === "alert" ? x.what.includes("alert") : f === "periodic" ? x.what === "periodic analysis" : x.what === "AI camera window"));
      $("#msgs", el).innerHTML = String(html`${list.map(x => {
        const col = x.what === "animal alert" ? "var(--bad)" : x.what === "vehicle alert" ? "var(--warn)" : x.what === "AI camera window" ? "var(--virtual)" : "var(--info)";
        const lag = x.delivered_t === null || x.delivered_t === undefined ? html`<span class="pill bad">not delivered</span>` : html`<span class="muted small">delivered +${(x.delivered_t - x.t).toFixed(1)} s</span>`;
        return html`<div class="tl-i" style="--c:${col}"><div class="row" style="gap:8px"><span class="when">${fmt.t(x.t)}</span><span class="pill plain" style="color:${col}">${x.what}</span>${lag}</div><div class="txt"><code>${x.text}</code></div></div>`;
      })}`);
    };
    draw("");
    $$("#msgSeg button", el).forEach(b => b.onclick = () => { $$("#msgSeg button", el).forEach(x => x.classList.toggle("on", x === b)); draw(b.dataset.f); });
  }
};

MISSION_TABS.animals = (el, m) => {
  const counts = {}; m.animals.forEach(a => counts[a.status] = (counts[a.status] || 0) + 1);
  const order = m.status_order.filter(s => counts[s]);
  let filt = null;
  el.innerHTML = String(html`<div class="grid g-12" style="margin-bottom:16px">
    <div class="card"><div class="card-h"><h2>Status</h2><span class="sub">click to filter</span></div><div id="stF">${hbars(order.map(s => ({label: statusTag(s), value: counts[s], color: stColor(s)})))}</div></div>
    <div class="card"><div class="card-h"><h2>Behaviour of each track</h2><span class="sub">seconds resting · walking · running (from the session)</span></div><div class="chart" id="cEtho"></div></div></div>
    <div class="card"><div class="card-h"><h2>Tracks</h2><span class="sub" id="tCnt"></span></div><div id="tAn"></div><p class="small muted">${m.census_note}</p></div>`);
  const withE = m.animals.filter(a => a.ethogram_s).sort((a, b) => b.visible_s - a.visible_s).slice(0, 24);
  if (withE.length) vbars($("#cEtho", el), {cats: withE.map(a => ({key: a.track_id, label: `T${a.track_id} · ${spName(a.species)}`, short: "T" + a.track_id})),
    series: [{key: "resting", label: "Resting", color: "#3f9a6e"}, {key: "walking", label: "Walking", color: "#2f7fc1"}, {key: "running", label: "Running", color: "#e08a2e"}],
    stacked: true, yLabel: "seconds", unit: " s", d: 0, value: (tid, k) => withE.find(a => a.track_id === tid).ethogram_s[k], height: 230, rotate: withE.length > 14});
  else $("#cEtho", el).innerHTML = String(html`<div class="empty">No behaviour data in this session.</div>`);
  const draw = () => {
    const rows = m.animals.filter(a => !filt || a.status === filt);
    $("#tCnt", el).textContent = filt ? `${rows.length} of ${m.animals.length} · ${stName(filt)}` : `${rows.length} tracks`;
    table($("#tAn", el), [
      {key: "track_id", label: "Track", num: true, render: a => html`<b class="mono">T${a.track_id}</b>`},
      {key: "species", label: "Species", render: a => spTag(a.species)},
      {key: "status", label: "Status", render: a => statusTag(a.status), sort: a => m.status_order.indexOf(a.status)},
      {key: "first_seen_s", label: "First seen", num: true, render: a => fmt.t(a.first_seen_s)},
      {key: "visible_s", label: "In view", num: true, render: a => fmt.dur(a.visible_s)},
      {key: "events", label: "Events", render: a => Object.entries(a.events).map(([k, n]) => html`<span class="pill plain" style="color:${evColor(k)};margin-right:4px">${k} ×${n}</span>`), sort: a => Object.values(a.events).reduce((x, y) => x + y, 0)},
      {key: "min_distance_to_drone_m", label: "Closest to drone", num: true, render: a => a.min_distance_to_drone_m === null ? "–" : fmt.n(a.min_distance_to_drone_m, 0) + " m", title: "Slant distance from the drone"},
      {key: "distance_m", label: "Moved", num: true, render: a => a.distance_m === null || a.distance_m === undefined ? "–" : fmt.n(a.distance_m, 0) + " m"},
      {key: "ethogram_s", label: "Behaviour", nosort: true, render: a => a.ethogram_s ? html`<span class="stackbar" data-tip="${tipOf`Resting <b>${fmt.n(a.ethogram_s.resting, 0)} s</b><br>Walking <b>${fmt.n(a.ethogram_s.walking, 0)} s</b><br>Running <b>${fmt.n(a.ethogram_s.running, 0)} s</b>`}">${[["resting", "#3f9a6e"], ["walking", "#2f7fc1"], ["running", "#e08a2e"]].map(([k, c]) => html`<i style="width:${a.ethogram_s[k] / Math.max(1e-9, a.ethogram_s.resting + a.ethogram_s.walking + a.ethogram_s.running) * 100}%;background:${c}"></i>`)}</span>` : "–"},
    ], rows, {sort: "status", dir: 1, scroll: true});
  };
  $$(".hbar", $("#stF", el)).forEach((b, k) => { b.style.cursor = "pointer"; b.onclick = () => { filt = filt === order[k] ? null : order[k]; $$(".hbar", $("#stF", el)).forEach((x, j) => x.style.opacity = !filt || order[j] === filt ? 1 : .4); draw(); }; });
  draw();
};

MISSION_TABS.timeline = (el, m) => {
  const tracks = [...new Set(m.events.map(e => e.track_id))].sort((a, b) => a - b);
  const types = [...new Set(m.events.map(e => e.type))];
  const alerts = m.flight ? m.flight.events.filter(x => x.what === "animal alert" || x.what === "vehicle alert") : [];
  const d = m.report.duration_s, t0 = m.herd.length ? m.herd[0].t : 0;
  el.innerHTML = String(html`<div class="stack">
    <div class="card"><div class="card-h"><h2>Behaviour events by track</h2><span class="sub">mission time (m:ss) · ▼ = alert sent from the drone · hover any bar</span></div>
      ${tracks.length ? html`<div class="chart" id="cG"></div>${legendHTML(types.map(t => ({label: t, color: evColor(t)})).concat(alerts.length ? [{label: "animal alert", color: "var(--bad)"}, {label: "vehicle alert", color: "var(--warn)"}] : []))}` : html`<div class="empty">No behaviour events in this flight.</div>`}</div>
    <div class="card"><div class="card-h"><h2>Animals in view</h2><span class="sub">count on each herd sample · isolated = away from the rest</span></div>
      ${m.herd.length > 1 ? html`<div class="chart" id="cH"></div>` : html`<div class="empty">${m.herd.length ? "Only one herd sample: the flight is too short for a timeline." : "No herd timeline in this flight."}</div>`}</div></div>`);
  if (tracks.length) {
    const st = Object.fromEntries(m.animals.map(a => [a.track_id, a]));
    gantt($("#cG", el), {x0: t0, x1: t0 + d, rows: tracks.map(t => ({key: t, label: `T${t}`, color: st[t] ? stColor(st[t].status) : null})),
      bars: m.events.map(e => ({row: e.track_id, start: e.start_s, end: e.end_s, color: evColor(e.type),
        tip: esc(`<b>T${e.track_id}</b> · ${esc(spName(e.species))}<br>${esc(e.type)}: ${fmt.t(e.start_s)} – ${fmt.t(e.end_s)} (${fmt.dur(e.duration_s)})<br>${esc(stName(e.status))}`)})),
      marks: alerts.map(a => ({t: a.t, color: a.what === "animal alert" ? "#c2342b" : "#c98a04", label: "!", tip: esc(`<b>${fmt.t(a.t)}</b> ${esc(a.what)}<br><span style="font-family:var(--mono)">${esc(a.text)}</span>`)}))});
  }
  if (m.herd.length > 1) lines($("#cH", el), {xs: m.herd.map(h => h.t), yLabel: "animals", height: 230,
    series: [{label: "Animals in view", color: "#2f7fc1", values: m.herd.map(h => h.count)}, {label: "Isolated", color: "#e0672a", values: m.herd.map(h => h.isolated)}]});
};

MISSION_TABS.map = (el, m) => {
  if (!m.map.known) { el.innerHTML = String(html`<div class="empty">No map: this flight has no camera parameters or drone telemetry, so nothing can be placed on the ground.</div>`); return; }
  const species = [...new Set(m.map.points.map(p => p.species))];
  const st = [...new Set(m.map.points.map(p => p.status).filter(s => s && s !== "BASELINE"))];
  el.innerHTML = String(html`<div class="grid g-21"><div class="card"><div class="card-h"><h2>Where each animal was found</h2><span class="sub">east / north metres from take-off · flat ground, camera pointing down</span></div><div id="mp"></div>
      ${legendHTML(species.map(s => ({label: spName(s), color: spColor(s)})))}
      <div class="legend">${st.map(s => html`<span><i style="background:transparent;border:2px solid ${stColor(s)};border-radius:50%"></i>${stName(s)}</span>`)}<span><i class="ln" style="background:var(--fg-2)"></i>drone path</span></div></div>
    <div class="card"><div class="card-h"><h2>Animals on the map</h2><span class="sub">${m.map.points.length} points</span></div><div id="mpT"></div></div></div>`);
  mapView($("#mp", el), {path: m.map.path, points: m.map.points});
  table($("#mpT", el), [
    {key: "track_id", label: "Track", num: true, render: p => html`<b class="mono">T${p.track_id}</b>`},
    {key: "status", label: "Status", render: p => statusTag(p.status || "BASELINE"), sort: p => m.status_order.indexOf(p.status)},
    {key: "t", label: "Found", num: true, render: p => fmt.t(p.t)},
    {key: "lat_deg", label: "GPS", nosort: true, render: p => p.lat_deg === null || p.lat_deg === undefined ? "–" : html`<span class="mono" style="font-size:11.5px">${fmt.ll(p.lat_deg)}, ${fmt.ll(p.lon_deg)}</span>`},
  ], m.map.points, {sort: "status", dir: 1, scroll: true});
};

MISSION_TABS.media = (el, m) => {
  const md = m.media, fr = md.frame || {w: 1280, h: 720};
  const clipTile = c => c.url ? html`<div class="tile" data-clip="${c.id}"><div class="ph"><img loading="lazy" src="/api/clip/${encodeURIComponent(m.mission_id)}/${encodeURIComponent(c.id)}/poster" alt="${c.id}">
        <span class="play"><span>${icon("play", 20)}</span></span><span class="badge-t">${fmt.t(c.t_start)}</span></div>
        <div class="cap"><span class="t"><span class="mono">${c.id}</span><span class="small muted">${fmt.dur(c.t_end - c.t_start)}</span></span>${statusTag(c.why)}<span class="muted">Tracks ${c.tracks.map(t => "T" + t).join(", ")}</span></div></div>`
    : html`<div class="tile nomedia"><div class="ph">${c.state === "no_media" ? "No video: a synthetic flight has times and tracks only" : "Clip not sent (" + c.state + ")"}</div>
        <div class="cap"><span class="t"><span class="mono">${c.id}</span><span class="small muted">${fmt.t(c.t_start)}</span></span>${statusTag(c.why)}<span class="muted">Tracks ${c.tracks.map(t => "T" + t).join(", ")}</span></div></div>`;
  const imgTile = (x, k, kind) => x.url ? html`<div class="tile" data-kind="${kind}" data-k="${k}"><div class="ph"><img loading="lazy" src="${x.url}" alt="${x.id}">${boxOverlay((x.boxes || []).map(b => ({...b, color: kind === "img" ? "#ffb020" : "#ff4d3d"})), fr.w, fr.h)}<span class="badge-t">${fmt.t(x.t)}</span></div>
      <div class="cap"><span class="t"><span class="mono">${x.id}</span>${kind === "img" ? html`<span class="pill warn">${x.purpose.replace(/_/g, " ")}</span>` : html`<span class="pill info">${x.criterion.replace(/_/g, " ")}</span>`}</span>
      <span class="muted">${kind === "img" ? (x.alert || "") : (x.boxes || []).map(b => `${b.label} ${b.conf.toFixed(2)}`).join(", ")}</span></div></div>` : "";
  el.innerHTML = String(html`<div class="stack">
    <div class="card"><div class="card-h"><h2>Event clips</h2><span class="sub">the video around each alert · click to play, with the detector's boxes</span></div>
      ${md.clips.length ? html`<div class="gallery">${md.clips.map(clipTile)}</div>` : html`<div class="empty">No event clip in this package.</div>`}</div>
    <div class="card"><div class="card-h"><h2>Onboard images</h2><span class="sub">taken in flight (vehicle near a herd, AI camera windows) · sent after landing</span></div>
      ${md.images.filter(x => x.url).length ? html`<div class="gallery">${md.images.map((x, k) => imgTile(x, k, "img"))}</div>` : html`<div class="empty">No onboard image in this flight.</div>`}</div>
    <div class="card"><div class="card-h"><h2>Uncertain detections</h2><span class="sub">frames where the detector was unsure · red box = its guess · for labelling on the Review page</span></div>
      ${md.stills.filter(x => x.url).length ? html`<div class="gallery">${md.stills.map((x, k) => imgTile(x, k, "unc"))}</div>` : html`<div class="empty">${md.stills.length ? `${md.stills.length} uncertain detections, without images (no video in this flight).` : "No uncertain detection."}</div>`}</div></div>`);
  $$(".tile[data-clip]", el).forEach(t => t.onclick = () => clipPlayer(m.mission_id, md.clips.find(c => c.id === t.dataset.clip)));
  const items = kind => (kind === "img" ? md.images : md.stills).filter(x => x.url).map(x => ({url: x.url, title: `${x.id} · ${kind === "img" ? x.purpose.replace(/_/g, " ") : x.criterion.replace(/_/g, " ")}`,
    sub: kind === "img" ? (x.alert || "") : x.reason, boxes: (x.boxes || []).map(b => ({...b, color: kind === "img" ? "#ffb020" : "#ff4d3d"})),
    info: [["mission time", fmt.t(x.t)], ["flight", m.mission_id], ...(kind === "unc" ? [["tracks", x.tracks.map(t => "T" + t).join(", ") || "–"]] : x.window ? [["window", x.window]] : [])]}));
  $$(".tile[data-kind]", el).forEach(t => t.onclick = () => {
    const kind = t.dataset.kind, list = (kind === "img" ? md.images : md.stills).filter(x => x.url);
    const id = (kind === "img" ? md.images : md.stills)[+t.dataset.k].id;
    lightbox(items(kind), list.findIndex(x => x.id === id));
  });
};

MISSION_TABS.log = (el, m) => {
  const f = m.flight;
  if (!f) { el.innerHTML = String(html`<div class="empty">${m.virtual && !m.main_virtual ? "A synthetic flight has no onboard flight log." : "No flight log in this package."}${m.log_error ? " " + m.log_error : ""}</div>`); return; }
  el.innerHTML = String(html`<div class="stack">
    <div class="card"><div class="card-h"><h2>Everything the drone sent</h2><span class="sub">by mission time · delivered = when the radio delivered its last part</span></div><div id="lE"></div></div>
    <div class="card"><div class="card-h"><h2>AI camera windows</h2><span class="sub">the AI saw images only inside these windows</span></div><div id="lW"></div></div>
    <div class="card"><div class="card-h"><h2>Where each animal was found</h2><span class="sub">${f.movement_note}</span></div><div id="lM"></div></div>
    <div class="card"><div class="card-h"><h2>Pre-flight checks</h2></div><div id="lP"></div></div></div>`);
  table($("#lE", el), [
    {key: "t", label: "Time", num: true, render: r => fmt.t(r.t)}, {key: "what", label: "What"},
    {key: "text", label: "Message", cls: "wrap", render: r => html`<span class="mono">${r.text}</span>`},
    {key: "delivered_t", label: "Delivered", num: true, render: r => r.delivered_t === null ? html`<span class="pill bad">never</span>` : `+${(r.delivered_t - r.t).toFixed(1)} s`},
  ], f.events, {scroll: true, empty: "Nothing sent."});
  table($("#lW", el), [
    {key: "window", label: "#", num: true}, {key: "trigger", label: "Trigger"}, {key: "alert", label: "For alert", render: r => html`<span class="mono">${r.alert || "–"}</span>`},
    {key: "opened_t", label: "Opened", num: true, render: r => fmt.t(r.opened_t)}, {key: "images", label: "Images", num: true},
    {key: "latency_s", label: "AI time", num: true, render: r => r.latency_s === null ? "–" : r.latency_s.toFixed(1) + " s"},
    {key: "verdict", label: "Verdict", cls: "wrap"}, {key: "sent", label: "Sent", cls: "wrap", render: r => html`<span class="mono">${r.sent}</span>`},
  ], f.windows, {empty: "No AI camera window in this flight."});
  table($("#lM", el), [
    {key: "track_id", label: "Track", num: true, render: r => html`<b class="mono">T${r.track_id}</b>`}, {key: "species", label: "Species", render: r => spTag(r.species)},
    {key: "status", label: "Status", render: r => statusTag(r.status)}, {key: "found", label: "Found"},
    {key: "gps", label: "GPS", render: r => r.gps ? html`<span class="mono">${r.gps}</span>` : "–"},
    {key: "distance_m", label: "Moved", num: true, render: r => r.distance_m === null || r.distance_m === undefined ? "–" : fmt.n(r.distance_m, 0) + " m"},
  ], f.movement, {scroll: true});
  table($("#lP", el), [
    {key: "level", label: "Result", render: r => html`<span class="pill ${r.level === "OK" ? "ok" : r.level === "WARN" ? "warn" : "bad"}">${r.level}</span>`},
    {key: "name", label: "Check"}, {key: "detail", label: "Detail", cls: "wrap", render: r => html`<span class="small">${r.detail}</span>`},
  ], f.preflight);
};

MISSION_TABS.files = (el, m) => {
  const p = m.package, b = p.budget || {};
  el.innerHTML = String(html`<div class="grid g2">
    <div class="card"><div class="card-h"><h2>Package</h2><span class="sub">made ${p.created_at}</span></div>
      ${hbars(Object.entries(p.items).sort((a, x) => x[1] - a[1]).map(([k, n]) => ({label: k.replace(/_/g, " "), value: n, color: "var(--info)"})))}
      <p class="small muted">Size budget: ${b.used_bytes !== undefined ? fmt.bytes(b.used_bytes) : "–"} used${b.dropped_count ? html` · <b style="color:var(--warn)">${b.dropped_count} items dropped</b> to stay in the budget` : " · nothing dropped"}.</p></div>
    <div class="card"><div class="card-h"><h2>Files to open</h2><span class="sub">the archived originals (checked by SHA-256)</span></div>
      <div class="stack" style="gap:8px">${p.files.map(f => html`<a class="btn" style="justify-content:flex-start" href="${f.url}" target="_blank">${icon("file")} <span class="mono">${f.path}</span><span class="spacer"></span><span class="muted small">${fmt.bytes(f.bytes)}</span></a>`)}</div></div></div>`);
};

// =============================================================================================== Compare
PAGES.compare = async (v) => {
  const by = STORE.get("cmpBy", "flight");
  const saved = STORE.get("cmp_" + by, null);
  const d = await getJSON(`/api/compare?by=${by}&${virtualParam()}${saved ? "&keys=" + encodeURIComponent(saved.join(",")) : ""}`);
  const keys = d.keys, r = d.result;
  const col = k => SEL_COL[keys.indexOf(k) % SEL_COL.length];
  v.innerHTML = String(html`<div class="page-head"><div><div class="eyebrow">Side by side</div><h1>Compare</h1><p>Pick 2 to ${d.max} ${by === "flight" ? "flights" : "flight days"}. Each keeps its colour in every chart.</p></div>
    <div class="seg" id="cBy"><button data-b="flight" class="${by === "flight" ? "on" : ""}">Flights</button><button data-b="day" class="${by === "day" ? "on" : ""}">Days</button></div></div>
    <div class="card" style="margin-bottom:16px"><div class="row" id="cOpts">${d.options.map(o => {
      const on = keys.includes(o.key);
      return html`<span class="chip ${on ? "on" : ""}" data-k="${o.key}" ${!on && keys.length >= d.max ? "disabled" : ""}><span class="sw" style="${on ? `background:${col(o.key)}` : ""}"></span>${by === "day" ? fmt.date(o.key) : o.label}${o.virtual ? html` <span class="muted small">virtual</span>` : ""}</span>`;
    })}</div>${d.options.length < 2 ? html`<div class="empty" style="margin-top:10px">The archive needs at least two ${by === "flight" ? "flights" : "flight days"} to compare${d.include_virtual ? "" : " (virtual flights are not counted: switch them on in the side bar)"}.</div>` : ""}</div>
    ${!r ? (d.options.length >= 2 ? html`<div class="empty">Pick at least two.</div>` : "") : html`
    <div class="callouts">${r.notes.map((n, i) => callout(i === 0 ? "info" : "warn", n))}</div>
    <div class="kpis">${keys.map(k => { const c = r.context[k]; const val = name => (r.table.find(x => x.metric === name) || {})[k];
      return html`<div class="kpi" style="border-top:3px solid ${col(k)}"><div class="l"><span class="sp"><i style="background:${col(k)}"></i></span>${by === "day" ? fmt.date(k) : fmt.date(c.dates[0])}</div>
        <div class="v">${val("Tracks")}<small>tracks</small></div><div class="s">${val("Flight time (min)")} min · ${val("Tracks in alert")} in alert${by === "flight" ? "" : ` · ${val("Flights")} flights`}</div>
        <div class="s mono" style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${k}">${by === "flight" ? k : c.sources.join(", ")}</div></div>`; })}</div>
    <div class="grid g2" style="margin-bottom:16px">
      <div class="card"><div class="card-h"><h2>Tracks per flight hour</h2><span class="sub">tracks ÷ flight time · fair across long and short flights</span></div><div class="chart" id="cRate"></div>
        <p class="small muted">Left empty under ${d.min_rate_s / 60} min of flight.</p></div>
      <div class="card"><div class="card-h"><h2>Tracks by species</h2><span class="sub">count</span></div><div class="chart" id="cSp"></div></div>
      <div class="card"><div class="card-h"><h2>Status of the tracks</h2><span class="sub">share of each selection's tracks · hover for counts</span></div><div id="cSt"></div></div>
      <div class="card"><div class="card-h"><h2>Behaviour events</h2><span class="sub">count</span></div><div class="chart" id="cEv"></div></div>
    </div>
    <div class="card" style="margin-bottom:16px"><div class="card-h"><h2>All numbers</h2><span class="sub">${keys.length === 2 ? "change = second minus first (oldest first) · " : ""}bar = share of the row's largest value</span></div><div id="cTbl"></div></div>
    <div class="card"><div class="card-h"><h2>What each selection is</h2></div><div id="cCtx"></div></div>`}`);
  $$("#cBy button").forEach(b => b.onclick = () => { STORE.set("cmpBy", b.dataset.b); route(); });
  $$("#cOpts .chip").forEach(c => c.onclick = () => {
    if (c.hasAttribute("disabled")) { toast(`At most ${d.max} at once.`); return; }
    const k = c.dataset.k, next = keys.includes(k) ? keys.filter(x => x !== k) : keys.concat(k);
    STORE.set("cmp_" + by, next.length ? next : null); route();
  });
  if (!r) return;
  const cats = keys.map(k => ({key: k, label: r.labels[k], short: by === "day" ? fmt.dateShort(k) : fmt.dateShort(r.context[k].dates[0]) + (keys.filter(x => r.context[x].dates[0] === r.context[k].dates[0]).length > 1 ? " " + (k.match(/seed(\d+)/) ? "#" + k.match(/seed(\d+)/)[1] : k.slice(-6)) : "")}));
  // one series per selection so each bar keeps its selection colour
  vbars($("#cRate"), {cats, series: keys.map(k => ({key: k, label: "tracks per hour", color: col(k)})), stacked: true, legend: false, d: 1, labels: true, yLabel: "tracks / hour",
    value: (c, s) => c === s ? (r.per_hour.find(x => x.selection === c) || {}).tracks_per_hour : 0});
  const species = [...new Set(r.species.map(x => x.species))];
  vbars($("#cSp"), {cats, series: species.map(s => ({key: s, label: spName(s), color: spColor(s)})), integer: true, labels: true, yLabel: "tracks",
    value: (c, s) => (r.species.find(x => x.selection === c && x.species === s) || {}).tracks || 0});
  const events = [...new Set(r.events.map(x => x.event))];
  if (events.length) vbars($("#cEv"), {cats, series: events.map(e => ({key: e, label: e, color: evColor(e)})), integer: true, yLabel: "events",
    value: (c, e) => (r.events.find(x => x.selection === c && x.event === e) || {}).count || 0});
  else $("#cEv").innerHTML = String(html`<div class="empty">No events in these selections.</div>`);
  $("#cSt").innerHTML = String(html`<div class="stack" style="gap:12px">${cats.map(c => {
    const rows = r.statuses.filter(x => x.selection === c.key), tot = rows.reduce((a, x) => a + x.tracks, 0);
    return html`<div><div class="row small" style="justify-content:space-between;margin-bottom:4px"><span class="sp"><i style="background:${col(c.key)}"></i>${c.short}</span><span class="muted">${tot} tracks</span></div>
      <div class="stackbar" style="height:16px;border-radius:5px">${r.status_order.map(s => { const n = (rows.find(x => x.status === s) || {}).tracks || 0;
        return n ? html`<i class="hb" style="width:${n / Math.max(1, tot) * 100}%;background:${stColor(s)}" data-tip="${tipOf`<b>${c.label}</b><br>${stName(s)}: <b>${n}</b> (${fmt.pct(n / tot * 100)})`}"></i>` : ""; })}</div></div>`;
  })}${legendHTML(r.status_order.map(s => ({label: stName(s), color: stColor(s)})))}</div>`);
  const tcols = [{key: "metric", label: "Metric", nosort: true, render: x => html`<b style="font-weight:550">${x.metric.replace(/^Tracks: (.*)$/, (q, s) => "Tracks: " + spName(s)).replace(/^Status: (.*)$/, (q, s) => "Status: " + stName(s))}</b>`}]
    .concat(keys.map(k => ({key: k, label: by === "day" ? fmt.dateShort(k) : cats.find(c => c.key === k).short, num: true, nosort: true, title: r.labels[k],
      render: x => { const vals = keys.map(q => x[q]).filter(z => typeof z === "number"), mx = Math.max(...vals, 1e-9);
        return typeof x[k] === "number" ? html`<div class="cellbar">${fmt.n(x[k], x[k] % 1 ? 1 : 0)}<span class="b" style="width:${Math.max(2, x[k] / mx * 60)}px;background:${col(k)}"></span></div>` : (x[k] ?? "–"); }})));
  if (keys.length === 2) tcols.push({key: "change", label: "Change", num: true, nosort: true, render: x => x.change === null || x.change === undefined ? "–" : html`<b style="color:${x.change > 0 ? "var(--ok)" : x.change < 0 ? "var(--bad)" : "var(--muted)"}">${x.change > 0 ? "+" : ""}${fmt.n(x.change, x.change % 1 ? 1 : 0)}</b>`});
  table($("#cTbl"), tcols, r.table);
  table($("#cCtx"), [{key: "k", label: "Selection", nosort: true, render: x => html`<span class="sp"><i style="background:${col(x.k)}"></i><span class="mono">${x.k}</span></span>`},
    {key: "dates", label: "Dates", nosort: true, render: x => x.dates.map(fmt.date).join(", ")}, {key: "sources", label: "Source", nosort: true, render: x => x.sources.map(s => SOURCE_TXT[s] || s).join(", ")},
    {key: "telemetry", label: "Telemetry", nosort: true}, {key: "reports", label: "Report", nosort: true, render: x => x.reports.map(s => SOURCE_TXT[s] || s).join(", ")},
    ...(by === "flight" ? [{key: "headline", label: "Headline", nosort: true, cls: "wrap"}] : [])], keys.map(k => ({k, ...r.context[k]})));
};

// =============================================================================================== Review
let RV = {mission: "", crit: null};
PAGES.review = async (v) => {
  const d = await getJSON("/api/review?" + virtualParam());
  const shown = d.items.filter(x => x.url);
  const mids = [...new Set(shown.map(x => x.mission_id))], crits = [...new Set(shown.map(x => x.criterion))];
  if (!RV.crit) RV.crit = new Set(crits);
  const list = shown.filter(x => (!RV.mission || x.mission_id === RV.mission) && RV.crit.has(x.criterion));
  v.innerHTML = String(html`<div class="page-head"><div><div class="eyebrow">Labelling</div><h1>Review</h1><p>${d.note}</p></div></div>
    <div class="kpis"><div class="kpi"><div class="l">Uncertain detections</div><div class="v">${d.total}</div><div class="s">in the counted flights</div></div>
      <div class="kpi"><div class="l">With an image</div><div class="v">${d.with_image}</div><div class="s">only flights with video have frames</div></div>
      <div class="kpi"><div class="l">Shown</div><div class="v">${list.length}</div><div class="s">with the filters below</div></div></div>
    ${!shown.length ? html`<div class="empty">No uncertain detection with an image${d.include_virtual ? "" : " (virtual flights are not counted: switch them on in the side bar)"}.</div>` : html`
    <div class="card" style="margin-bottom:16px"><div class="row"><select class="btn" id="rvM"><option value="">All flights (${mids.length})</option>${mids.map(x => html`<option value="${x}" ${x === RV.mission ? "selected" : ""}>${x}</option>`)}</select>
      <span class="muted small" style="margin-left:6px">Why unsure:</span>${crits.map(c => html`<span class="chip ${RV.crit.has(c) ? "on" : ""}" data-c="${c}"><span class="sw" style="${RV.crit.has(c) ? "background:var(--info)" : ""}"></span>${c.replace(/_/g, " ")} <span class="muted">${shown.filter(x => x.criterion === c).length}</span></span>`)}</div>
      <p class="small muted" style="margin:10px 0 0">Export for labelling (YOLO format): <span class="mono">python -m hima export-review --archive "${d.archive}" --out review_export</span></p></div>
    <div class="gallery" id="rvG">${list.map((x, k) => html`<div class="tile" data-k="${k}"><div class="ph"><img loading="lazy" src="${x.url}" alt="${x.id}">${x.size ? boxOverlay(x.boxes, x.size[0], x.size[1]) : ""}<span class="badge-t">${fmt.t(x.t)}</span></div>
      <div class="cap"><span class="t"><span class="mono">${x.id}</span><span class="pill info">${x.criterion.replace(/_/g, " ")}</span></span><span class="muted">${x.boxes.map(b => `${b.label} ${b.conf.toFixed(2)}`).join(", ")}</span>
      <span class="muted mono" style="font-size:11px">${x.mission_id}</span></div></div>`)}</div>`}`);
  if (!shown.length) return;
  $("#rvM").onchange = e => { RV.mission = e.target.value; route(); };
  $$(".chip[data-c]").forEach(c => c.onclick = () => { const k = c.dataset.c; RV.crit.has(k) ? RV.crit.delete(k) : RV.crit.add(k); route(); });
  $$("#rvG .tile").forEach(t => t.onclick = () => lightbox(list.map(x => ({url: x.url, title: `${x.id} · ${x.criterion.replace(/_/g, " ")}`, sub: x.reason, boxes: x.boxes,
    info: [["flight", x.mission_id], ["mission time", fmt.t(x.t)], ["frame", x.frame], ["tracks", x.tracks.map(q => "T" + q).join(", ") || "–"], ["boxes", x.boxes.map(b => `${b.label} ${b.conf.toFixed(2)}`).join(", ")]]})), +t.dataset.k));
};
