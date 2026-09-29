#!/usr/bin/env python3
"""app.py - a click-through wizard for anti-persona, in your browser.

    python3 app.py                 (or double-click Start.command / Start.bat, or the packaged app)
    python3 app.py --no-browser    print the address instead of opening it
    python3 app.py --selftest

The packaged app is this file frozen with PyInstaller. It has no Python and no
.py files, so it runs the other scripts through itself:
    anti-persona --script anti selftest

The page is served on 127.0.0.1 only and every request must carry a token that
is new on each launch, so other websites and other users of this computer
cannot drive it. Every button runs the same persona.py / anti.py command you
could type yourself; the command and its output are shown on the page.
"""
import argparse
import hmac
import http.server
import importlib.util
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import anti
import persona

ROOT = Path(__file__).resolve().parent
DATA = persona.DATA
FROZEN = persona.FROZEN
TOKEN = secrets.token_urlsafe(24)
SERVER = None
CHANNELS = ("google", "youtube", "chatgpt", "claude")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,80}$")
TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
WIN_TASK = "anti-persona"


# ---------------------------------------------------------------- jobs
class Job:
    """One command at a time, its output kept for the page to poll."""

    def __init__(self):
        self.lock = threading.Lock()
        self.proc, self.name, self.code, self.lines = None, None, None, []

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, name, args, stdin_text=None):
        with self.lock:
            if self.running():
                raise RuntimeError(f"'{self.name}' is still running; wait for it or press Stop")
            shown = [("anti-persona" if FROZEN else "python3") if a == sys.executable else a for a in args]
            self.name, self.code, self.lines = name, None, ["$ " + " ".join(shown)]
            env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
            DATA.mkdir(parents=True, exist_ok=True)
            self.proc = subprocess.Popen(
                args, cwd=DATA, env=env, text=True, encoding="utf-8", errors="replace", bufsize=1,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )
        if stdin_text is not None:
            self.send(stdin_text)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            self.lines.append(line.rstrip("\n"))
        self.code = proc.wait()
        self.lines.append(f"[finished, exit code {self.code}]")

    def send(self, text):
        if self.running():
            try:
                self.proc.stdin.write(text + "\n")
                self.proc.stdin.flush()
            except OSError:
                pass

    def stop(self):
        if self.running():
            self.proc.terminate()
            self.lines.append("[stopped by you]")

    def view(self, since):
        return {"name": self.name, "running": self.running(), "code": self.code,
                "lines": self.lines[since:], "next": len(self.lines)}


JOB = Job()


# ---------------------------------------------------------------- commands
def _int(p, name, lo, hi, default):
    try:
        return min(max(int(p.get(name, default)), lo), hi)
    except (TypeError, ValueError):
        return default


def _model(p):
    m = str(p.get("model") or anti.MODEL)
    if not MODEL_RE.match(m):
        raise ValueError("invalid model name")
    return m


def _only(p):
    chosen = [c for c in CHANNELS if c in (p.get("channels") or CHANNELS)]
    if not chosen:
        raise ValueError("pick at least one site")
    return [] if len(chosen) == len(CHANNELS) else ["--only", ",".join(chosen)]


def build(action, p):
    """Button -> argument list. Whitelisted actions, validated values, never a shell."""
    a, pe = (lambda *x: persona.script_cmd("anti", *x)), (lambda *x: persona.script_cmd("persona", *x))
    if action == "setup":
        if FROZEN:
            raise ValueError("the packaged app already includes the browser helper")
        return [sys.executable, "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], None
    if action == "pull":
        return a("pull", "--model", _model(p)), None  # Ollama's HTTP API: works without the CLI on PATH
    if action == "research":
        return pe("--yes", "--model", _model(p)), None
    if action == "plan":
        args = a("plan", "--model", _model(p), "--stages", str(_int(p, "stages", 3, 20, 10)),
                 "--days-per-stage", str(_int(p, "days", 1, 30, anti.DAYS_PER_STAGE)))
        goal = " ".join(str(p.get("goal") or "").split())[:300]
        return args + (["--goal", goal] if goal else []), None
    if action == "pace":
        return a("plan", "--check", "--days-per-stage", str(_int(p, "days", 1, 30, anti.DAYS_PER_STAGE))), None
    if action == "approve":
        return a("approve"), "y"  # the page showed the plan and the notice; the click is the "y"
    if action == "login":
        return a("login"), None
    if action == "preview":
        return a("run", "--dry-run", *_only(p)), None
    if action == "run":
        return a("run", "--yes", "--model", _model(p), *_only(p), *(["--force"] if p.get("force") else [])), None
    if action == "metrics":
        return a("metrics", "--judge", "--model", _model(p)), None
    if action == "schedule":
        at = str(p.get("at") or "20:00")
        if not TIME_RE.match(at):
            raise ValueError("time must look like 20:00")
        return a("schedule", "--at", at), None
    if action == "unschedule":
        return a("schedule", "--remove"), None
    raise ValueError(f"unknown action {action!r}")


# ---------------------------------------------------------------- status
def chrome_found():
    if sys.platform == "darwin":
        paths = [Path("/Applications/Google Chrome.app"), Path.home() / "Applications/Google Chrome.app"]
    elif sys.platform == "win32":
        paths = [Path(os.environ.get(v, "")) / "Google/Chrome/Application/chrome.exe"
                 for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if os.environ.get(v)]
    else:
        return any(shutil.which(n) for n in ("google-chrome", "google-chrome-stable"))
    return any(p.exists() for p in paths)


def ollama_models():
    try:
        with urllib.request.urlopen(f"{anti.HOST}/api/tags", timeout=2) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except (OSError, ValueError):
        return None


def scheduled():
    if sys.platform == "darwin":
        return anti.PLIST.exists()
    if sys.platform == "win32":
        return subprocess.run(["schtasks", "/Query", "/TN", WIN_TASK], capture_output=True).returncode == 0
    return None


def sources():
    """Sizes walk big folders, so this is asked for once, not on every poll."""
    out = []
    for name, path in (("Chrome history", persona.CHROME), ("Safari history", persona.SAFARI), ("Claude Code chats", persona.CLAUDE)):
        exists = path.exists()
        out.append({"name": name, "path": str(path), "exists": exists, "size": persona.size(path) if exists else "missing"})
    return out


def status():
    models = ollama_models()
    s = {
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "playwright": importlib.util.find_spec("playwright") is not None,
        "chrome": chrome_found(),
        "frozen": FROZEN,
        "data": str(DATA),
        "ollama": models is not None,
        "models": models or [],
        "default_model": anti.MODEL,
        "embed_model": anti.EMBED,
        "research": (anti.OUT / "persona.json").exists(),
        "reports": {n: (anti.OUT / n).exists() for n in ("report.html", "transition.html")},
        "scheduled": scheduled(),
        "roadmap": None,
    }
    if anti.ROADMAP_F.exists():
        try:
            rm = json.loads(anti.ROADMAP_F.read_text())
            r = anti.rid(rm)
            log = anti.read_log(r)
            left = anti.open_slots(rm, log, anti.load_variants(), every=True)
            total = len(anti.slots(rm))
            sc = rm.get("scores", {})
            s["roadmap"] = {
                "id": r, "approved": rm.get("approved") == r, "days_per_stage": anti.days(rm),
                "stages": [{"k": x["k"], "theme": x["theme"], "bridge": x["bridge"]} for x in rm["stages"]],
                "scores": {k: sc.get(k) for k in ("pos", "min_cos", "cos_ends", "max_step", "step_limit", "backslides", "coverage", "valid")},
                "days_done": total - len(left), "days_total": total,
                "next": {"stage": left[0][0], "day": left[0][1], "due_in_h": round(anti.too_soon(log, *left[0]), 1)} if left else None,
            }
        except (ValueError, KeyError, OSError) as e:
            s["roadmap_error"] = str(e)
    return s


# ---------------------------------------------------------------- http
class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "anti-persona"

    def log_message(self, *a):  # keep the terminal quiet
        pass

    def _allowed(self, token):
        """Host check blocks DNS rebinding; the token blocks other sites and other local users."""
        port = self.server.server_address[1]
        if self.headers.get("Host") not in (f"127.0.0.1:{port}", f"localhost:{port}"):
            return False
        return hmac.compare_digest(str(token or ""), TOKEN)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        token = (q.get("t") or [None])[0] or self.headers.get("X-Token")
        if not self._allowed(token):
            return self._send(403, {"error": "forbidden"})
        if u.path == "/":
            return self._send(200, PAGE.replace("__TOKEN__", TOKEN).encode(), "text/html; charset=utf-8")
        if u.path == "/api/status":
            return self._send(200, status())
        if u.path == "/api/sources":
            return self._send(200, sources())
        if u.path == "/api/job":
            return self._send(200, JOB.view(int((q.get("since") or ["0"])[0] or 0)))
        if u.path in ("/report/report.html", "/report/transition.html"):
            f = anti.OUT / u.path.rsplit("/", 1)[1]
            return self._send(200, f.read_bytes(), "text/html; charset=utf-8") if f.exists() else self._send(404, {"error": "not made yet"})
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._allowed(self.headers.get("X-Token")):
            return self._send(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        if n > 10_000:
            return self._send(413, {"error": "too large"})
        try:
            p = json.loads(self.rfile.read(n) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/start":
                args, stdin_text = build(str(p.get("action")), p)
                JOB.start(str(p.get("action")), args, stdin_text)
            elif path == "/api/input":
                JOB.send("")  # only ever "press Enter" (login finished)
            elif path == "/api/stop":
                JOB.stop()
            elif path == "/api/quit":
                JOB.stop()
                threading.Thread(target=SERVER.shutdown, daemon=True).start()
            else:
                return self._send(404, {"error": "not found"})
            return self._send(200, {"ok": True})
        except (ValueError, RuntimeError) as e:
            return self._send(400, {"error": str(e)})


def serve(port, open_browser):
    global SERVER
    try:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # port busy: take any free one
    SERVER = srv
    url = f"http://127.0.0.1:{srv.server_address[1]}/?t={TOKEN}"
    print(f"anti-persona is running at\n  {url}\nKeep this window open. Close it (or press Ctrl+C) to quit.", flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        JOB.stop()
        srv.server_close()


# ---------------------------------------------------------------- page
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>anti-persona</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#16181d;--mut:#667085;--line:#e4e7ec;--acc:#0f766e;--acc2:#115e59;--ok:#15803d;--bad:#b42318;--warn:#b54708;--chip:#f2f4f7;--log:#0b0d12;--logfg:#d0d5dd}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--fg:#e6e8ec;--mut:#98a2b3;--line:#262a35;--acc:#2dd4bf;--acc2:#5eead4;--ok:#4ade80;--bad:#f87171;--warn:#fbbf24;--chip:#20242d;--log:#07080b;--logfg:#c8ccd4}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,sans-serif;padding:24px 16px 260px}
main{max-width:860px;margin:0 auto}h1{font-size:26px;margin:0}h2{font-size:17px;margin:0}.mut{color:var(--mut)}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;margin-top:14px}
.head{display:flex;align-items:center;gap:10px}.num{width:28px;height:28px;border-radius:50%;background:var(--chip);display:grid;place-items:center;font-weight:600;font-size:14px;flex:none}
.badge{margin-left:auto;font-size:12px;padding:2px 10px;border-radius:999px;background:var(--chip);color:var(--mut);white-space:nowrap}
.badge.ok{color:var(--ok)}.badge.bad{color:var(--bad)}.badge.warn{color:var(--warn)}
.body{margin-top:10px}.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-top:10px}
button{font:inherit;border:1px solid var(--line);background:var(--chip);color:var(--fg);padding:7px 14px;border-radius:9px;cursor:pointer}
button.main{background:var(--acc);border-color:var(--acc);color:#fff}button.main:hover{background:var(--acc2)}
button:disabled{opacity:.45;cursor:not-allowed}
input,select{font:inherit;padding:6px 10px;border:1px solid var(--line);border-radius:8px;background:var(--card);color:var(--fg)}
input[type=text]{flex:1;min-width:220px}input[type=number]{width:74px}
label{display:inline-flex;gap:6px;align-items:center}
ul.checks{list-style:none;padding:0;margin:0}ul.checks li{padding:4px 0;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.y{color:var(--ok)}.n{color:var(--bad)}
table{width:100%;border-collapse:collapse;font-size:14px;margin-top:8px}td,th{padding:6px 8px;border-top:1px solid var(--line);text-align:left;vertical-align:top}
.note{font-size:13px;color:var(--mut);margin-top:8px}.warnbox{border-left:3px solid var(--warn);padding:8px 12px;background:var(--chip);border-radius:6px;font-size:14px;margin-top:10px}
.bar{height:8px;background:var(--chip);border-radius:99px;overflow:hidden;margin-top:8px}.bar>div{height:100%;background:var(--acc)}
#logbox{position:fixed;left:0;right:0;bottom:0;background:var(--log);color:var(--logfg);border-top:1px solid var(--line)}
#loghead{display:flex;gap:10px;align-items:center;padding:8px 16px;font-size:13px}#loghead b{color:#fff}
#log{margin:0;padding:0 16px 12px;height:190px;overflow:auto;font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
#logbox button{padding:3px 10px;font-size:12px;background:#20242d;color:#fff;border-color:#333}
a{color:var(--acc)}
</style></head><body><main>
<div class="head"><h1>anti-persona</h1><button id="quit" style="margin-left:auto">Quit</button></div>
<div class="mut">Go step by step. Each button runs one command; you can see it and its output at the bottom.</div>
<div class="note" id="datanote"></div>

<section class="card"><div class="head"><div class="num">0</div><h2>Get ready</h2><span class="badge" id="b0"></span></div>
<div class="body"><ul class="checks" id="checks"></ul>
<div class="row"><label>Model <select id="model"></select></label><span class="note" id="modelnote"></span></div></div></section>

<section class="card"><div class="head"><div class="num">1</div><h2>Learn who you are</h2><span class="badge" id="b1"></span></div>
<div class="body"><div class="mut">The tool will read these files on this computer. It never changes them, and nothing is uploaded.</div>
<ul class="checks" id="sources"><li class="mut">checking…</li></ul>
<div class="row"><label><input type="checkbox" id="consent"> I agree to let it read these files</label></div>
<div class="row"><button class="main" id="research" data-act="research">Start research</button>
<button data-open="report.html" id="openreport">Open report</button><span class="note">Takes a few minutes.</span></div></div></section>

<section class="card"><div class="head"><div class="num">2</div><h2>Make the plan</h2><span class="badge" id="b2"></span></div>
<div class="body"><div class="row"><input type="text" id="goal" placeholder="Optional goal, e.g. house plants, gardening, living in nature"></div>
<div class="row"><label>Steps <input type="number" id="stages" value="10" min="3" max="20"></label>
<label>Days per step <input type="number" id="days" value="5" min="1" max="30"></label>
<button class="main" data-act="plan" id="plan">Make plan</button>
<button data-act="pace" id="pace">Change pace only</button></div>
<div class="note">Making a plan is slow: about 20 minutes per attempt with the default model, up to 3 attempts.</div>
<div id="plantable"></div></div></section>

<section class="card"><div class="head"><div class="num">3</div><h2>Approve the plan</h2><span class="badge" id="b3"></span></div>
<div class="body"><div class="warnbox">Before you approve:<br>
• Only use this on your own computer and your own accounts.<br>
• ChatGPT and Claude do not allow bots on their websites. Your accounts could be flagged. You can leave them out in step 5.<br>
• If a site shows a CAPTCHA or a login page, the run stops. It never tries to get past it.<br>
• Everything runs in a separate Chrome profile. Your normal Chrome is untouched.</div>
<div class="row"><label><input type="checkbox" id="understood"> I read the plan above and the notes</label>
<button class="main" data-act="approve" id="approve">Approve</button></div></div></section>

<section class="card"><div class="head"><div class="num">4</div><h2>Log in once</h2><span class="badge" id="b4"></span></div>
<div class="body"><div class="mut">A separate Chrome window opens. Log in to Google, ChatGPT and Claude there yourself. The tool never sees your passwords.</div>
<div class="row"><button class="main" data-act="login" id="login">Open login window</button>
<button id="loggedin" disabled>I'm logged in, save it</button></div></div></section>

<section class="card"><div class="head"><div class="num">5</div><h2>Run a day</h2><span class="badge" id="b5"></span></div>
<div class="body"><div id="progress" class="mut"></div><div class="bar"><div id="bar" style="width:0"></div></div>
<div class="row">Sites: <label><input type="checkbox" class="ch" value="google" checked> Google</label>
<label><input type="checkbox" class="ch" value="youtube" checked> YouTube</label>
<label><input type="checkbox" class="ch" value="chatgpt" checked> ChatGPT</label>
<label><input type="checkbox" class="ch" value="claude" checked> Claude</label></div>
<div class="row"><button data-act="preview" id="preview">Preview today</button>
<button class="main" data-act="run" id="run">Run today</button>
<label class="note"><input type="checkbox" id="force"> ignore the 12-hour wait</label></div>
<div class="note">A day is about 11 actions and takes 10–15 minutes. Chrome opens; you can watch.</div></div></section>

<section class="card"><div class="head"><div class="num">6</div><h2>See progress</h2><span class="badge" id="b6"></span></div>
<div class="body"><div class="row"><button class="main" data-act="metrics" id="metrics">Measure now</button>
<button data-open="transition.html" id="opendash">Open dashboard</button><span class="note">Takes 1–2 minutes.</span></div></div></section>

<section class="card"><div class="head"><div class="num">7</div><h2>Run it every day</h2><span class="badge" id="b7"></span></div>
<div class="body"><div class="row"><label>Every day at <input type="time" id="at" value="20:00"></label>
<button class="main" data-act="schedule" id="schedule">Turn on</button><button data-act="unschedule" id="unschedule">Turn off</button></div>
<div class="note" id="schednote">The computer must be on (a Mac that was asleep runs it on wake).</div></div></section>
</main>

<div id="logbox"><div id="loghead"><b id="jobname">Output</b><span id="jobstate"></span><span style="margin-left:auto"></span>
<button id="stop" disabled>Stop</button><button id="clear">Clear</button></div><pre id="log">Nothing has run yet.</pre></div>

<script>
const T="__TOKEN__",$=id=>document.getElementById(id);
const e=x=>String(x??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const api=(p,body)=>fetch(p,{method:body?'POST':'GET',headers:{'X-Token':T,'Content-Type':'application/json'},body:body&&JSON.stringify(body)}).then(r=>r.json());
let S=null,since=0,running=false;
const badge=(id,txt,cls)=>{const b=$(id);b.textContent=txt;b.className='badge '+(cls||'')};
const chk=(ok,txt,extra)=>`<li><span class="${ok?'y':'n'}">${ok?'✓':'✗'}</span> ${txt} ${extra||''}</li>`;

function params(act){return {action:act,model:$('model').value,goal:$('goal').value,stages:+$('stages').value,days:+$('days').value,
 channels:[...document.querySelectorAll('.ch:checked')].map(x=>x.value),force:$('force').checked,at:$('at').value}}
async function start(act){
 const r=await api('/api/start',params(act));
 if(r.error){alert(r.error);return}
 since=0;$('log').textContent='';poll();refresh();
}
document.querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>start(b.dataset.act));
document.querySelectorAll('[data-open]').forEach(b=>b.onclick=()=>window.open('/report/'+b.dataset.open+'?t='+T,'_blank','noopener'));
$('loggedin').onclick=()=>api('/api/input',{});
$('stop').onclick=()=>api('/api/stop',{});
$('clear').onclick=()=>{$('log').textContent=''};
$('quit').onclick=async()=>{if(running&&!confirm('A command is still running. Stop it and quit?'))return;await api('/api/quit',{});document.body.innerHTML='<main><h1>anti-persona has stopped.</h1><p class="mut">You can close this tab.</p></main>'};
['consent','understood'].forEach(id=>$(id).onchange=()=>render());

async function poll(){
 const j=await api('/api/job?since='+since);
 if(j.lines.length){const L=$('log'),atEnd=L.scrollTop+L.clientHeight>=L.scrollHeight-20;L.textContent+=(L.textContent?'\n':'')+j.lines.join('\n');if(atEnd)L.scrollTop=L.scrollHeight}
 since=j.next;running=j.running;
 $('jobname').textContent=j.name?('Output · '+j.name):'Output';
 $('jobstate').textContent=j.running?'running…':(j.code==null?'':(j.code===0?'done':'finished with errors (code '+j.code+')'));
 $('stop').disabled=!j.running;$('loggedin').disabled=!(j.running&&j.name==='login');
 render();
 if(j.running)setTimeout(poll,1000);else refresh();
}

function render(){
 if(!S)return;const R=S.roadmap,busy=running;
 const models=S.models,hasModel=m=>models.some(x=>x===m||x===m+':latest'||x.split(':')[0]===m.split(':')[0]&&m.indexOf(':')<0);
 const ready=S.playwright&&S.chrome&&S.ollama&&hasModel($('model').value||S.default_model)&&hasModel(S.embed_model);
 $('checks').innerHTML=
  chk(true,S.frozen?'App (Python '+e(S.python)+' built in)':'Python '+e(S.python))+
  chk(S.playwright,'Browser helper (Playwright)',S.playwright||S.frozen?'':'<button data-act2="setup">Install</button>')+
  chk(S.chrome,'Google Chrome',S.chrome?'':'<a href="https://www.google.com/chrome/" target="_blank" rel="noopener">Download Chrome</a>')+
  chk(S.ollama,'Ollama is running',S.ollama?'':'<span class="mut">open the Ollama app, or</span> <a href="https://ollama.com/download" target="_blank" rel="noopener">download Ollama</a>')+
  (S.ollama?chk(hasModel($('model').value||S.default_model),'Model '+e($('model').value||S.default_model),hasModel($('model').value||S.default_model)?'':'<button data-act2="pull" data-m="'+e($('model').value||S.default_model)+'">Download</button>'):'')+
  (S.ollama?chk(hasModel(S.embed_model),'Embedding model '+e(S.embed_model),hasModel(S.embed_model)?'':'<button data-act2="pull" data-m="'+e(S.embed_model)+'">Download</button>'):'');
 document.querySelectorAll('[data-act2]').forEach(b=>{b.disabled=busy;b.onclick=async()=>{const p=params(b.dataset.act2);if(b.dataset.m)p.model=b.dataset.m;const r=await api('/api/start',p);if(r.error)alert(r.error);else{since=0;$('log').textContent='';poll()}}});
 badge('b0',ready?'ready':'needs attention',ready?'ok':'warn');
 $('modelnote').textContent=S.ollama?'':'Start Ollama to see your models.';

 badge('b1',S.research?'done':'to do',S.research?'ok':'');
 $('research').disabled=busy||!ready||!$('consent').checked;$('openreport').disabled=!S.reports['report.html'];

 $('plan').disabled=busy||!ready||!S.research;$('pace').disabled=busy||!R;
 if(R){const sc=R.scores||{},P=sc.pos||[],f=x=>x==null?'–':(+x).toFixed(2);
  badge('b2',sc.valid?'plan passes all checks':'plan made, some checks missed',sc.valid?'ok':'warn');
  $('plantable').innerHTML=`<table><tr><th>#</th><th>step</th><th>how it connects</th><th title="0 = you, 1 = your opposite">position</th></tr>`+
   R.stages.map((s,i)=>`<tr><td>${e(s.k)}</td><td>${e(s.theme)}</td><td class="mut">${e(s.bridge)}</td><td>${f(P[i])}</td></tr>`).join('')+'</table>'+
   `<div class="note">${e(R.stages.length)} steps × ${e(R.days_per_stage)} days = ${e(R.stages.length*R.days_per_stage)} days. `+
   `Smoothness ${f(sc.min_cos)} (needs ≥ ${f((sc.cos_ends||0)+0.05)}), biggest jump ${f(sc.max_step)} (≤ ${f(sc.step_limit)}), steps back ${e(sc.backslides)}, goal reached ${Math.round((sc.coverage||0)*100)}%.</div>`;
 } else {badge('b2',S.research?'to do':'do step 1 first');$('plantable').innerHTML=''}

 badge('b3',R?(R.approved?'approved':'not approved'):'no plan yet',R&&R.approved?'ok':'');
 $('approve').disabled=busy||!R||R.approved||!$('understood').checked;

 badge('b4','do once, and again if a run stops at a login page');
 $('login').disabled=busy||!S.playwright||!S.chrome;

 if(R){const pct=R.days_total?Math.round(100*R.days_done/R.days_total):0;$('bar').style.width=pct+'%';
  $('progress').textContent=R.next?`Day ${R.days_done+1} of ${R.days_total}: step ${R.next.stage}, day ${R.next.day}.`+(R.next.due_in_h>0?` Next day is due in ${R.next.due_in_h} h.`:' Ready to run.'):'All days are done.';
  badge('b5',`${R.days_done}/${R.days_total} days`,R.next?'':'ok');
 } else {$('progress').textContent='Make and approve a plan first.';badge('b5','waiting')}
 $('preview').disabled=busy||!R;$('run').disabled=busy||!R||!R.approved||!R.next||!S.playwright||!S.chrome;
 $('metrics').disabled=busy||!R||!S.ollama;$('opendash').disabled=!S.reports['transition.html'];badge('b6',S.reports['transition.html']?'dashboard ready':'');

 const on=S.scheduled;badge('b7',on==null?'not available here':(on?'on':'off'),on?'ok':'');
 $('schedule').disabled=busy||!R||!R.approved||on==null;$('unschedule').disabled=busy||!on;
 if(on==null)$('schednote').textContent='On Linux, run "python3 anti.py schedule" in a terminal to get a cron line.';
}

async function refresh(){
 S=await api('/api/status');$('datanote').textContent='Your data is kept in '+S.data;
 const sel=$('model'),cur=sel.value||S.default_model,opts=[...new Set([S.default_model,'qwen3:14b','qwen3:8b',...S.models.filter(m=>!m.startsWith(S.embed_model))])];
 sel.innerHTML=opts.map(m=>`<option ${m===cur?'selected':''}>${e(m)}</option>`).join('');sel.onchange=render;
 render();
}
api('/api/sources').then(list=>{$('sources').innerHTML=list.map(s=>chk(s.exists,`<b>${e(s.name)}</b> <span class="mut">${e(s.path)} · ${e(s.size)}</span>`)).join('')});
refresh().then(poll);setInterval(()=>{if(!running)refresh()},5000);
</script></body></html>"""


# ---------------------------------------------------------------- main
def selftest():
    args, stdin_text = build("plan", {"model": "qwen3:8b", "stages": "99", "days": "0", "goal": "  plants,\n gardening; rm -rf /  "})
    assert args[args.index("--stages") + 1] == "20" and args[args.index("--days-per-stage") + 1] == "1"
    assert args[-2:] == ["--goal", "plants, gardening; rm -rf /"] and stdin_text is None  # one argv item, never a shell
    for bad in ({"model": "x; rm -rf /"}, {"model": "--help"}):
        try:
            build("research", bad)
            raise AssertionError(f"accepted {bad}")
        except ValueError:
            pass
    assert build("run", {"channels": ["google", "evil"]})[0][-2:] == ["--only", "google"]
    assert "--only" not in build("run", {"channels": list(CHANNELS)})[0]
    assert build("approve", {})[1] == "y"
    for bad_time in ("25:00", "8pm", "20:00; ls"):
        try:
            build("schedule", {"at": bad_time})
            raise AssertionError(bad_time)
        except ValueError:
            pass
    try:
        build("shell", {})
        raise AssertionError("unknown action accepted")
    except ValueError:
        pass

    class Fake:  # the request guard, without a socket
        server = type("S", (), {"server_address": ("127.0.0.1", 8765)})()

        def __init__(self, host):
            self.headers = {"Host": host}

    ok = Handler._allowed
    assert ok(Fake("127.0.0.1:8765"), TOKEN) and ok(Fake("localhost:8765"), TOKEN)
    assert not ok(Fake("127.0.0.1:8765"), "wrong") and not ok(Fake("127.0.0.1:8765"), None)
    assert not ok(Fake("evil.example:8765"), TOKEN)  # DNS rebinding
    j = Job()
    if FROZEN:  # no "python -c" inside the app: exercise the --script dispatch instead
        j.start("dispatch", persona.script_cmd("persona", "--selftest"))
    else:
        j.start("echo", [sys.executable, "-c", "print(input())"], "hello")
    j.proc.wait()
    for _ in range(50):
        if j.code is not None:
            break
        threading.Event().wait(0.05)
    assert ("selftest ok" if FROZEN else "hello") in j.lines and j.code == 0, j.lines
    assert build("research", {})[0][:len(persona.script_cmd("persona"))] == persona.script_cmd("persona")
    print("selftest ok")
    return 0


def dispatch(argv):
    """`anti-persona --script NAME ...` runs one of our scripts inside the packaged app."""
    name, rest = argv[2], argv[3:]
    if name not in ("persona", "anti", "browse"):
        sys.exit(f"unknown script {name!r}")
    sys.argv = [f"{name}.py", *rest]
    if name == "browse":
        import browse

        return browse.main()
    return {"persona": persona, "anti": anti}[name].main()


def ensure_streams():
    """A windowed app may start with no stdout/stderr; print() would then crash."""
    if sys.stdout is None or sys.stderr is None:
        DATA.mkdir(parents=True, exist_ok=True)
        log = open(DATA / "app.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - lives as long as the app
        sys.stdout = sys.stdout or log
        sys.stderr = sys.stderr or log


def main():
    ensure_streams()
    if len(sys.argv) > 2 and sys.argv[1] == "--script":
        return dispatch(sys.argv)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a, _ = ap.parse_known_args()  # macOS may add its own arguments when an .app is opened
    if a.selftest:
        return selftest()
    serve(a.port, not a.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
