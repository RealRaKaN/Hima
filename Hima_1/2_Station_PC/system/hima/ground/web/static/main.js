"use strict";
/* Router, live updates (a new flight or a new route shows by itself), theme, the virtual-flights switch. */

const APP = {state: null, seenStamp: null, seenIds: null, seenRoute: undefined, page: null, busy: 0};

function go(hash) { if (location.hash === hash) route(); else location.hash = hash; }
function parse() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean).map(decodeURIComponent);
  return {page: PAGES[parts[0]] ? parts[0] : null, args: parts.slice(1)};
}

async function route() {
  let {page, args} = parse();
  if (!page) {                                    // first open: Route while a route waits, else the newest flight
    page = APP.state && APP.state.route_pending ? "route" : "mission";
    history.replaceState(null, "", "#/" + page);
  }
  if (page !== "route") stopRoute();
  $$("#nav a").forEach(a => a.classList.toggle("on", a.dataset.page === page));
  const v = $("#view"), my = ++APP.busy, samePage = APP.page === page;
  APP.page = page; closeModal(); tip.classList.remove("on");
  if (!samePage) { v.classList.remove("view-in"); v.innerHTML = String(skeleton()); } else v.classList.add("loading");
  // the page draws into a hidden box placed first (so its ids are found before the old page's), then swaps in at once
  const holder = document.createElement("div"); holder.style.display = "none"; v.prepend(holder);
  try {
    await PAGES[page](holder, args);
    if (my !== APP.busy) { holder.remove(); return; }
    v.replaceChildren(...holder.childNodes);
    v.classList.remove("loading", "view-in"); void v.offsetWidth;
    if (!samePage) { v.classList.add("view-in"); window.scrollTo({top: 0}); }
    redrawCharts(v);
  } catch (e) {
    holder.remove(); v.classList.remove("loading");
    if (my !== APP.busy) return;
    v.innerHTML = String(errorBox(e));
  }
}

// charts drawn while their box was hidden have width 0: draw them again once shown
function redrawCharts(root) {
  requestAnimationFrame(() => {
    $$(".tabs", root).forEach(placeInk);
    window.dispatchEvent(new Event("resize"));
    $$(".chart, #mp", root).forEach(el => { const ro = _obs.get(el); if (ro) { ro.disconnect(); ro.observe(el); } });
  });
}

// ---------------------------------------------------------------- live: poll the station every 3 s
async function poll(first) {
  let s;
  try { s = await getJSON("/api/state"); }
  catch (e) { $("#liveDot").classList.add("off"); $("#liveTxt").textContent = "Station not answering"; return; }
  $("#liveDot").classList.remove("off"); $("#liveTxt").textContent = "Live";
  $("#missionCount").textContent = `${s.missions} flights`;
  $("#archPath").textContent = s.archive;
  $("#routeBadge").hidden = !s.route_pending;
  const prev = APP.state; APP.state = s;
  if (first) { APP.seenStamp = s.stamp; APP.seenIds = new Set(s.ids); APP.seenRoute = s.route_pending; return; }
  if (s.route_pending && s.route_pending !== APP.seenRoute) {      // the drone waits only wait_s: open the Route page
    APP.seenRoute = s.route_pending; toast("New route from the drone: it waits for your answer.", 6000); go(`#/route/${s.route_pending}`); return;
  }
  APP.seenRoute = s.route_pending;
  if (s.stamp !== APP.seenStamp) {
    APP.seenStamp = s.stamp; MCACHE.clear(); LIST = null;
    const fresh = s.ids.filter(id => !APP.seenIds.has(id)); APP.seenIds = new Set(s.ids);
    fresh.forEach(id => toast(`New flight received: ${id}`, 6000));
    if (APP.page && APP.page !== "route" && !$("#modal")) route();
  }
  void prev;
}

// ---------------------------------------------------------------- side bar controls
function setTheme(t) {
  if (t === "system") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = t;
  STORE.set("theme", t); try { localStorage.setItem("hima.theme", t); } catch (e) { /* */ }
  $$("#themeSeg button").forEach(b => b.classList.toggle("on", b.dataset.t === t));
  if (APP.page) setTimeout(() => route(), 50);      // charts read the theme's colours
}
$$("#themeSeg button").forEach(b => b.onclick = () => setTheme(b.dataset.t));
(() => { let t = "system"; try { t = localStorage.getItem("hima.theme") || "system"; } catch (e) { /* */ } $$("#themeSeg button").forEach(b => b.classList.toggle("on", b.dataset.t === t)); })();

const vt = $("#virtualToggle");
vt.onchange = () => { STORE.set("virtual", vt.checked); toast(vt.checked ? "Virtual flights are counted in the totals." : "Virtual flights are left out of the totals.", 2600); route(); };

window.addEventListener("hashchange", route);
document.addEventListener("click", e => { const a = e.target.closest && e.target.closest('a[href^="#/"]'); if (a && a.getAttribute("href") === location.hash) { e.preventDefault(); route(); } });

(async () => {
  await poll(true);
  const pref = STORE.get("virtual", null);
  vt.checked = pref === null ? !(APP.state && APP.state.real) : pref;   // default: counted only while no real flight
  route();
  setInterval(() => poll(false), 3000);
})();
