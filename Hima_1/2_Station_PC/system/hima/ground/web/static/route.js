"use strict";
/* Route before take-off. The map is route_map/index.html, the same editor as before: this page talks to it
   with the messages it already knows (render in, value out), so the editor itself is unchanged. */

let RT = {timer: 0, frame: null, onMsg: null};
function stopRoute() {
  clearInterval(RT.timer); RT.timer = 0;
  if (RT.onMsg) window.removeEventListener("message", RT.onMsg);
  RT.onMsg = null; RT.frame = null;
}

PAGES.route = async (v, args) => {
  stopRoute();
  const list = await getJSON("/api/routes");
  if (!list.offers.length) {
    v.innerHTML = String(html`<div class="page-head"><div><div class="eyebrow">Before take-off</div><h1>Route</h1></div></div>
      <div class="empty">No route yet. When the drone has planned today's route at its launch point it sends it here (Wi-Fi, or the radio bridge), and waits ${list.wait_s} s for you to change it.<br><span class="mono small">${list.dir}</span></div>`);
    return;
  }
  const pid = list.offers.find(o => o.plan_id === args[0]) ? args[0] : (list.pending || list.offers[0].plan_id);
  const r = await getJSON(`/api/route/${pid}`);
  v.innerHTML = String(html`<div class="page-head"><div><div class="eyebrow">Before take-off</div><h1>Route</h1>
      <p>The drone's own plan. Move, add or delete points, then send it, or let the drone fly its own. It waits ${r.wait_s} s.</p></div></div>
    <div class="grid" style="grid-template-columns:minmax(0,260px) minmax(0,1fr)" id="rtGrid">
      <div class="card"><div class="card-h"><h2>Offers</h2><span class="sub">newest first</span></div><div class="offer-list">${list.offers.map(o => html`<div class="offer ${o.plan_id === pid ? "on" : ""}" data-p="${o.plan_id}">
        <div class="t">${o.plan_id}</div><div class="d">${o.points ? o.points + " points · " + (o.route_km ?? "–") + " km" : ""}<span class="pill ${o.state === "waiting" ? "warn" : o.state === "demo" ? "virtual" : o.state.startsWith("flown") ? "ok" : "plain"}">${o.state}</span></div></div>`)}</div></div>
      <div class="stack">
        <div id="rtState"></div>
        <div class="kpis" style="margin-bottom:0">
          <div class="kpi"><div class="l">Points</div><div class="v">${r.points}</div></div>
          <div class="kpi"><div class="l">Route</div><div class="v">${r.route_km ?? "–"}<small>km</small></div><div class="s">battery allows ${r.route_budget_km ?? "–"} km</div></div>
          <div class="kpi"><div class="l">Came by</div><div class="v" style="font-size:19px;margin-top:9px">${r.seen_via.join(" + ")}</div><div class="s">${r.received_utc || ""}</div></div>
          <div class="kpi" id="rtLeftK"><div class="l">Drone waits</div><div class="v countdown" id="rtLeft">–</div><div class="s">then flies its own route</div></div></div>
        <div class="card" style="padding:10px"><iframe class="routemap" id="rtMap" src="/route_map/index.html" title="Route map"></iframe>
          <p class="small muted" style="margin:8px 4px 0">${r.images.length ? html`Images: ${r.images.map(im => `${im.name} (${im.source}, ${im.licence})`).join("; ")}. If an image does not sit right on the route, switch it off on the map.` : html`No satellite image covers this route (add one: <span class="mono">python -m hima imagery add …</span>). The map shows the fence and the route only.`}</p></div>
        <div class="card"><div class="card-h"><h2>Points (GPS)</h2><span class="sub">${r.answer && r.answer.targets ? "as sent" : "the drone's plan"}</span></div><div id="rtPts"></div></div>
      </div></div>`);
  if (innerWidth < 900) $("#rtGrid").style.gridTemplateColumns = "minmax(0,1fr)";
  $$(".offer").forEach(o => o.onclick = () => go(`#/route/${o.dataset.p}`));
  table($("#rtPts"), [{key: "i", label: "Point", num: true}, {key: "lat", label: "Latitude", num: true, render: p => html`<span class="mono">${fmt.ll(p.lat)}</span>`},
    {key: "lon", label: "Longitude", num: true, render: p => html`<span class="mono">${fmt.ll(p.lon)}</span>`},
    {key: "c", label: "", nosort: true, render: p => html`<button class="btn ghost" style="padding:3px 8px" data-ll="${fmt.ll(p.lat)}, ${fmt.ll(p.lon)}">${icon("copy", 13)}</button>`}],
    r.targets.map((p, i) => ({i: i + 1, lat: p[0], lon: p[1]})), {scroll: true});
  $$("#rtPts [data-ll]").forEach(b => b.onclick = () => copyText(b.dataset.ll, b));

  const stateBox = $("#rtState");
  function showState(extra) {
    let s;
    if (r.demo) s = callout("virtual", "Demo route: a real plan of the reserve from the drone's planner, always here to try the map. Nothing waits on it, and nothing is sent.");
    else if (r.result) s = callout("ok", html`The drone flies the <b>${r.result.used}</b> route: ${r.result.reason}`);
    else if (r.answer) s = callout("info", html`Answer sent: <b>${r.answer.decision}</b> at ${r.answer.utc} (waiting for the drone to confirm).`);
    else if (r.left_s !== null && r.left_s > 0) s = callout("warn", "The drone is waiting for your answer. Without one it flies its own route.");
    else s = callout("info", "The wait is over: without an answer the drone flew its own route.");
    stateBox.innerHTML = String(html`<div class="callouts" style="margin:0">${s}${extra || ""}</div>`);
  }
  showState();
  const deadline = r.map.deadline_ms;
  const tick = () => {
    const left = deadline ? (deadline - Date.now()) / 1000 : null;
    $("#rtLeft").textContent = r.demo ? "demo" : r.answer || r.result ? "answered" : left !== null && left > 0 ? Math.ceil(left) + " s" : "over";
    $("#rtLeftK").style.borderColor = left !== null && left > 0 && !r.answer && !r.demo ? "var(--warn)" : "";
  };
  tick(); RT.timer = setInterval(tick, 500);

  // the editor's protocol: it says it is ready, we render it; it sends {action, targets, nonce, plan_id}
  const frame = $("#rtMap"); RT.frame = frame;
  const render = () => frame.contentWindow.postMessage({isStreamlitMessage: true, type: "streamlit:render", args: r.map}, "*");
  let busy = false;
  RT.onMsg = async ev => {
    if (ev.source !== frame.contentWindow || !ev.data) return;
    const d = ev.data;
    if (d.type === "streamlit:componentReady") render();
    else if (d.type === "streamlit:setFrameHeight") frame.style.height = Math.max(420, Math.min(900, d.height)) + "px";
    else if (d.type === "streamlit:setComponentValue" && d.value && !busy) {
      const val = d.value; if (val.plan_id !== pid) return;
      busy = true;
      try {
        const res = await postJSON(`/api/route/${pid}`, {action: val.action, targets: val.targets});
        showState(callout(res.ok ? "ok" : "bad", res.message));
        if (res.ok && val.action !== "check") { toast(res.message); setTimeout(() => route(), 1200); }
      } catch (e) { showState(callout("bad", "Not sent: " + e.message)); }
      busy = false;
    }
  };
  window.addEventListener("message", RT.onMsg);
};
