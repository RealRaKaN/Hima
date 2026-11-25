"""A simple page in the browser for the link tests (launcher/link_tests.py): which test runs now, what to do with
your hands, the buttons that answer it (instead of typing in the console), and each result as it comes.

Served on this computer only (127.0.0.1, a free port), with the standard library. The console keeps working too.
The server thread never prints: the link tests redirect the screen while they read the links."""
from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

# what each test checks, and what the person does (Arabic, for the page)
TESTS_AR = {
    1: ("find", "البحث عن البيكس هوك", "يجرّب كل منفذ ويبحث عن نبضة البيكس هوك: USB، أو سلك TELEM2، أو الراديو.", ""),
    2: ("params", "المعاملات", "يقرأ إعدادات منفذ TELEM2 ورقم النظام، ولا يغيّر شيئًا.", ""),
    3: ("quality", "جودة الاتصال", "يعدّ الحزم الضائعة والتالفة على كل منفذ، وإشارة الراديو.", "لا تلمس شيئًا أثناء القياس."),
    4: ("rates", "معدّلات الرسائل", "هل تصل رسائل الموقع والاتجاه بالسرعة التي تحتاجها الرحلة؟", ""),
    5: ("freshness", "حداثة الموقع", "عمر آخر موقع يصل، وزمن الذهاب والإياب إلى البيكس هوك.", ""),
    6: ("gps", "الـ GPS", "ينتظر قفل الـ GPS ونقطة البداية والبطارية.",
        "داخل المبنى لا يقفل الـ GPS: اضغط «تخطَّ»."),
    7: ("rotate", "التدوير", "يتأكد أن اتجاه الدرون يُقرأ صحيحًا.",
        "ضع الدرون ثابتًا، اضغط «تم»، ثم أدِره ربع لفة مع عقارب الساعة واضغط «تم»."),
    8: ("lift", "الرفع", "يتأكد أن الارتفاع يُقرأ صحيحًا.",
        "أمسك الدرون ثابتًا على الأرض واضغط «تم»، ثم ارفعه نحو متر واضغط «تم»."),
    9: ("arm", "التسليح", "يتأكد أن التسليح وفكّه يصلان (يبدآن التسجيل وينهيانه).",
        "المراوح مفكوكة! سلّح الدرون من Mission Planner أو الريموت، ثم فك التسليح."),
    10: ("texts", "الرسائل إلى المحطة", "يرسل 5 رسائل مرقّمة ويتأكد أنها تصل المحطة الأرضية.",
         "مع الراديو موصولًا يتم وحده. وإلا انظر في Mission Planner: وصلت كلها (نعم)، أو بعضها، أو لا."),
    11: ("cut", "قطع الاتصال", "يتأكد أن الاتصال يعود وحده بعد انقطاعه.",
         "اقطع الاتصال نحو 10 ثوانٍ كما يُطلب أدناه، ثم أعِده."),
    12: ("camera", "الكاميرا", "20 ثانية من الكاميرا: الإطارات في الثانية والدقة.", ""),
    13: ("station", "المحطة الأرضية", "هل ترد المحطة بمفتاح النقل نفسه؟", ""),
    14: ("preflight", "فحص برنامج الطيران", "برنامج الطيران نفسه يفحص المنفذ والـ GPS والكاميرا والأوزان.",
         "إن طال انتظار الـ GPS داخل المبنى اضغط «تخطَّ»."),
}

PAGE = """<!doctype html>
<html lang="ar" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>اختبارات الاتصال</title>
<style>
:root{--bg:#f6f5f1;--card:#fff;--ink:#1f2328;--mute:#6b6f76;--line:#e3e1db;--ok:#1a7f37;--warn:#b26a00;
--fail:#c4252c;--skip:#8a8f98;--run:#1f5fbf}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--card:#20232a;--ink:#e8e6e1;--mute:#9aa0a8;--line:#30343c;
--ok:#4cc26b;--warn:#e0a03a;--fail:#ff6b6b;--skip:#8a8f98;--run:#6ea8ff}}
[hidden]{display:none!important}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.6 system-ui,"Segoe UI",Tahoma,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin-bottom:14px}
.now h2{margin:0 0 4px;font-size:22px}.what{color:var(--mute);margin:0 0 8px}
.do{font-size:18px;font-weight:600;margin:8px 0}
.prompt{direction:ltr;text-align:left;background:var(--bg);border-radius:8px;padding:8px 10px;font:14px/1.5 ui-monospace,Consolas,monospace;white-space:pre-wrap}
.btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px}
button{font:inherit;font-size:17px;padding:10px 18px;border-radius:10px;border:1px solid var(--line);background:var(--card);color:var(--ink);cursor:pointer}
button.main{background:var(--run);border-color:var(--run);color:#fff}
button:disabled{opacity:.35;cursor:default}
table{width:100%;border-collapse:collapse}td{padding:7px 6px;border-top:1px solid var(--line);vertical-align:top}
td.n{width:28px;color:var(--mute)}td.lv{width:70px}
.b{display:inline-block;min-width:54px;text-align:center;border-radius:6px;padding:1px 6px;font-size:13px;font-weight:700;color:#fff;background:var(--skip)}
.OK{background:var(--ok)}.WARN{background:var(--warn)}.FAIL{background:var(--fail)}.RUN{background:var(--run)}.WAIT{background:transparent;color:var(--mute);border:1px solid var(--line)}
.det{direction:ltr;text-align:left;color:var(--mute);font-size:13px;word-break:break-word}
.verdict{font-size:20px;font-weight:700}
#log{direction:ltr;text-align:left;max-height:220px;overflow:auto;font:12px/1.45 ui-monospace,Consolas,monospace;color:var(--mute);white-space:pre-wrap;margin:0}
.off{color:var(--fail);font-weight:600}
</style></head><body><main>
<h1>اختبارات الاتصال بالبيكس هوك <small id="conn" class="off"></small></h1>
<div class="card now"><h2 id="title">يبدأ…</h2><p class="what" id="what"></p><p class="do" id="do"></p>
<div class="prompt" id="prompt" hidden></div>
<div class="btns" id="btns" hidden>
<button class="main" data-k="">تم (Enter)</button><button data-k="s">تخطَّ</button>
<button data-k="y" class="yn">نعم، وصلت كلها</button><button data-k="p" class="yn">بعضها</button><button data-k="n" class="yn">لا</button>
</div></div>
<div class="card" id="vcard" hidden><div class="verdict" id="verdict"></div><div class="det" id="report"></div></div>
<div class="card"><table id="tbl"></table></div>
<div class="card"><pre id="log"></pre></div>
</main><script>
const $=id=>document.getElementById(id);let busy=false;
document.querySelectorAll("button[data-k]").forEach(b=>b.onclick=async()=>{
  if(busy)return;busy=true;b.disabled=true;
  try{await fetch("/key",{method:"POST",body:JSON.stringify({line:b.dataset.k})});}catch(e){}
  setTimeout(()=>{b.disabled=false;busy=false},600);});
function esc(s){return String(s).replace(/[&<>]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}
async function tick(){
  let s;try{s=await (await fetch("/state",{cache:"no-store"})).json();$("conn").textContent="";}
  catch(e){$("conn").textContent=" (انتهى البرنامج أو أُغلق)";setTimeout(tick,2000);return}
  const cur=s.tests[s.current]||null;
  if(s.done){$("title").textContent="انتهت الاختبارات";$("what").textContent="";$("do").textContent="";}
  else if(cur){$("title").textContent=s.current+". "+cur.ar;$("what").textContent=cur.what;$("do").textContent=s.prompt?cur.todo:"";}
  $("prompt").hidden=!s.prompt;$("prompt").textContent=(s.prompt||"").trim();
  $("btns").hidden=!s.prompt;document.querySelectorAll(".yn").forEach(b=>b.hidden=s.current!=10);
  $("vcard").hidden=!s.done;$("verdict").textContent=s.verdict||"";$("report").textContent=s.report||"";
  let h="";for(const n of Object.keys(s.tests).map(Number).sort((a,b)=>a-b)){const t=s.tests[n];
    for(const r of (t.results.length?t.results:[{level:n==s.current&&!s.done?"RUN":"WAIT",detail:""}]))
      h+=`<tr><td class="n">${n}</td><td class="lv"><span class="b ${r.level}">${r.level=="RUN"?"يعمل":r.level=="WAIT"?"لاحقًا":r.level}</span></td><td>${esc(t.ar)}<div class="det">${esc(r.detail)}</div></td></tr>`}
  $("tbl").innerHTML=h;const lg=$("log"),end=lg.scrollTop+lg.clientHeight>=lg.scrollHeight-8;
  lg.textContent=s.log.join("\\n");if(end)lg.scrollTop=lg.scrollHeight;
  setTimeout(tick,s.done?3000:700)}
tick();
</script></body></html>"""


class LinkUI:
    """State shared with the page; header/show/say/prompt are called by the link tests (one thread)."""

    def __init__(self, log_lines: int = 300):
        self.lock = threading.Lock()
        self.keys: queue.Queue[str] = queue.Queue()
        self.tests = {n: {"name": v[0], "ar": v[1], "what": v[2], "todo": v[3], "results": []}
                      for n, v in TESTS_AR.items()}
        self.current = 0
        self.prompt_text = ""
        self.log: list[str] = []
        self.log_lines = log_lines
        self.verdict = self.report = ""
        self.done = False
        self.server: ThreadingHTTPServer | None = None
        self.url = ""

    # -- called by the link tests --------------------------------------------------------------------------
    def header(self, n: int, _name: str) -> None:
        with self.lock:
            self.current, self.prompt_text = n, ""

    def show(self, r: Any) -> None:
        with self.lock:
            t = self.tests.get(r.n)
            if t is not None:
                t["results"].append({"level": r.level, "detail": r.detail})

    def say(self, line: str) -> None:
        with self.lock:
            self.log.extend(str(line).splitlines() or [""])
            del self.log[:-self.log_lines]

    def prompt(self, text: str | None) -> None:
        with self.lock:
            self.prompt_text = text or ""

    def finish(self, verdict: str, report: str) -> None:
        with self.lock:
            self.verdict, self.report, self.done, self.prompt_text = verdict, report, True, ""

    def state(self) -> dict:
        with self.lock:
            return {"current": self.current, "prompt": self.prompt_text, "done": self.done, "verdict": self.verdict,
                    "report": self.report, "log": list(self.log),
                    "tests": {n: {"ar": t["ar"], "what": t["what"], "todo": t["todo"], "results": list(t["results"])}
                              for n, t in self.tests.items()}}

    # -- the server --------------------------------------------------------------------------------------
    def start(self, port: int = 0) -> str:
        ui = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a) -> None:          # silent: the screen belongs to the tests
                pass

            def _send(self, code: int, body: bytes, ctype: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if self.path in ("/", "/index.html"):
                    self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
                elif self.path.startswith("/state"):
                    self._send(200, json.dumps(ui.state(), ensure_ascii=False).encode("utf-8"),
                               "application/json; charset=utf-8")
                else:
                    self._send(404, b"not found", "text/plain")

            def do_POST(self) -> None:
                if self.path != "/key":
                    self._send(404, b"not found", "text/plain")
                    return
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 1000)
                    line = str(json.loads(self.rfile.read(n) or b"{}").get("line", ""))[:20]
                except (ValueError, AttributeError):
                    self._send(400, b"bad request", "text/plain")
                    return
                ui.keys.put(line)
                self._send(200, b"{}", "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/"
        return self.url

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None


class UiConsole:
    """A button on the page counts as a line typed in the console; the console still works."""

    def __init__(self, ui: LinkUI, inner: Any):
        self.ui, self.inner = ui, inner

    def poll(self) -> str | None:
        try:
            line = self.ui.keys.get_nowait()
        except queue.Empty:
            return self.inner.poll()
        self.ui.say(f"  (button: {line or 'Enter'})")
        return line


def open_browser(url: str) -> bool:
    try:
        import webbrowser
        return webbrowser.open(url)
    except Exception:                                    # noqa: BLE001 - no browser (SSH): the URL is printed
        return False

