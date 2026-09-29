#!/usr/bin/env python3
"""persona.py - build a persona and its mirror anti-persona from local traces.

Sources: Chrome history, Safari history, Claude Code chats, ChatGPT exports.
Everything is aggregated locally; only the aggregate summary goes to a local
Ollama model. Nothing leaves the machine.

    python3 persona.py                # asks for consent, then runs
    python3 persona.py --yes          # skip the consent prompt
    python3 persona.py --extra ~/chatgpt-export --model qwen3:14b
"""
import argparse
import collections
import datetime as dt
import json
import random
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

HOME = Path.home()
CHROME = {
    "darwin": HOME / "Library/Application Support/Google/Chrome/Default/History",
    "linux": HOME / ".config/google-chrome/Default/History",
    "win32": HOME / "AppData/Local/Google/Chrome/User Data/Default/History",
}.get(sys.platform, HOME / "Chrome-History-not-found")
SAFARI = HOME / "Library/Safari/History.db"  # macOS only; missing elsewhere and skipped
CLAUDE = HOME / ".claude/projects"

STOP = set(
    """the a an and or of to in on for is it this that with as at by be are was
    i you my me we our your from not no if do does can how what why when where
    which will would should could have has had about into than then there here
    just like get use using also more some any all one new need want make add
    и в не на что с как я это по но а для от из у же о то ты мы вы он она они
    бы ли если или так его её их там тут нет да ну вот еще ещё только уже
    """.split()
)
CYR = re.compile("[а-яА-ЯёЁ]")
LAT = re.compile("[a-zA-Z]")
WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ][a-zA-Zа-яА-ЯёЁ\-]{2,}")
TAG = re.compile(r"</?[\w-]+[^>]*>")


# ---------------------------------------------------------------- helpers
def top(counter, n):
    return [{"key": k, "n": v} for k, v in counter.most_common(n)]


def hours_hist(counter):
    return [counter.get(h, 0) for h in range(24)]


def domain(url):
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host


def text_stats(msgs, hours, projects):
    """Shared aggregate for any chat source. msgs: list[str]."""
    cyr = sum(len(CYR.findall(m)) for m in msgs)
    lat = sum(len(LAT.findall(m)) for m in msgs)
    words = collections.Counter(
        w.lower() for m in msgs for w in WORD.findall(m) if w.lower() not in STOP
    )
    pool = sorted({m.strip() for m in msgs if 20 <= len(m.strip()) <= 400})
    random.Random(0).shuffle(pool)
    return {
        "messages": len(msgs),
        "hours": hours_hist(hours),
        "projects": top(projects, 15),
        "cyrillic_ratio": round(cyr / (cyr + lat), 2) if cyr + lat else 0,
        "keywords": top(words, 40),
        "samples": pool[:60],
    }


# ---------------------------------------------------------------- browsers
def _sqlite_copy(path):
    """Browsers lock their DB while running; read from a copy."""
    tmp = Path(tempfile.mkdtemp()) / "h.db"
    shutil.copy(path, tmp)
    return sqlite3.connect(tmp)


def chrome_time(t):  # microseconds since 1601-01-01
    return dt.datetime.fromtimestamp(t / 1e6 - 11644473600)


def safari_time(t):  # seconds since 2001-01-01
    return dt.datetime.fromtimestamp(t + 978307200)


def collect_chrome(path=CHROME):
    con = _sqlite_copy(path)
    urls = con.execute("select url,title,visit_count from urls where visit_count>0").fetchall()
    visits = con.execute("select visit_time from visits").fetchall()
    terms = con.execute("select term from keyword_search_terms").fetchall()
    domains = collections.Counter()
    titles = collections.Counter()
    for url, title, n in urls:
        domains[domain(url)] += n
        if title:
            titles[title[:80]] += n
    hours = collections.Counter(chrome_time(t).hour for (t,) in visits if t)
    return {
        "urls": len(urls),
        "visits": len(visits),
        "domains": top(domains, 40),
        "titles": top(titles, 30),
        "hours": hours_hist(hours),
        "searches": top(collections.Counter(t for (t,) in terms), 50),
    }


def collect_safari(path=SAFARI):
    con = _sqlite_copy(path)
    items = con.execute("select url,visit_count from history_items").fetchall()
    visits = con.execute("select visit_time from history_visits").fetchall()
    domains = collections.Counter()
    for url, n in items:
        domains[domain(url)] += n or 1
    hours = collections.Counter(safari_time(t).hour for (t,) in visits if t)
    return {
        "urls": len(items),
        "visits": len(visits),
        "domains": top(domains, 40),
        "hours": hours_hist(hours),
    }


# ---------------------------------------------------------------- AI chats
def clean_prompt(c):
    if isinstance(c, list):
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
    if not isinstance(c, str) or "<local-command" in c or "tool_use_id" in c or "toolu_" in c:
        return ""
    return re.sub(r"\s+", " ", TAG.sub(" ", c)).strip()


def collect_claude(root=CLAUDE):
    msgs, hours, projects = [], collections.Counter(), collections.Counter()
    files = list(root.rglob("*.jsonl"))
    for f in files:
        with open(f, errors="ignore") as fh:
            for line in fh:
                if '"type":"user"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("type") != "user" or d.get("isMeta"):
                    continue
                text = clean_prompt(d.get("message", {}).get("content"))
                if len(text) < 8 or text.startswith("[Request interrupted"):
                    continue
                msgs.append(text)
                if d.get("timestamp"):
                    hours[dt.datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).astimezone().hour] += 1
                projects[Path(d.get("cwd", "?")).name] += 1
    return {"files": len(files), **text_stats(msgs, hours, projects)}


def collect_chatgpt(root):
    """ChatGPT data export: <root>/**/conversations.json."""
    # ponytail: only the official ChatGPT export format; other AI exports need their own parser
    msgs, hours, titles = [], collections.Counter(), collections.Counter()
    files = list(Path(root).rglob("conversations.json"))
    for f in files:
        for conv in json.load(open(f, errors="ignore")):
            titles[conv.get("title") or "?"] += 1
            for node in conv.get("mapping", {}).values():
                m = node.get("message") or {}
                if m.get("author", {}).get("role") != "user":
                    continue
                text = " ".join(p for p in m.get("content", {}).get("parts", []) if isinstance(p, str)).strip()
                if len(text) < 8:
                    continue
                msgs.append(text)
                if m.get("create_time"):
                    hours[dt.datetime.fromtimestamp(m["create_time"]).hour] += 1
    return {"files": len(files), **text_stats(msgs, hours, titles)}


# ---------------------------------------------------------------- LLM
PROMPT = """You are a behavioral analyst. Below is an aggregated, anonymized summary of one
person's local digital traces: browser history statistics, search terms, and
prompts they wrote to AI assistants. Infer who this person is, then build the
exact mirror image: an ANTI-PERSONA whose every trait is the opposite.

Rules:
- Browser history describes the WHOLE person; AI-chat prompts describe only
  their job. Weigh the browser at least as much as the chats. Do not derive
  personality traits (anxious, controlling, etc.) from work prompts alone.
- "activity" gives peak hours numerically. Use those numbers for any claim
  about when the person is active; do not guess from vibes.
- "interests_outside_work" must come from non-work domains, searches and
  titles (shopping, hobbies, health, travel, sport, culture, finance, home).
- Ground every persona claim in the data; cite the source in "evidence".
- The anti-persona must invert each trait one-to-one (same list lengths, same order).
- Be specific and vivid, not generic. Give both characters a plausible name.
- Write in {lang}.
- Answer with ONLY this JSON, no prose:
{{
 "persona": {{
  "name": "", "tagline": "", "summary": "2-3 sentences",
  "traits": ["6 items"], "interests": ["6 items"], "interests_outside_work": ["6 items"],
  "values": ["5 items"], "habits": ["5 items"], "communication_style": "", "typical_day": "",
  "evidence": [{{"claim": "", "source": "which data supports it"}}]
 }},
 "anti_persona": {{
  "name": "", "tagline": "", "summary": "2-3 sentences",
  "traits": ["6 items"], "interests": ["6 items"], "interests_outside_work": ["6 items"],
  "values": ["5 items"], "habits": ["5 items"], "communication_style": "", "typical_day": "",
  "why_opposite": ["one line per inverted trait"]
 }}
}}

DATA:
{data}
"""


def peaks(hours):
    total = sum(hours) or 1
    order = sorted(range(24), key=lambda h: -hours[h])
    return {
        "peak_hours": order[:3],
        "share_08_18": round(sum(hours[8:18]) / total, 2),
        "share_22_02": round((sum(hours[22:]) + sum(hours[:2])) / total, 2),
    }


def llm_input(summary):
    """Trim the summary to what the model needs (no raw history)."""
    s = {}
    for name in ("chrome", "safari"):
        b = summary.get(name)
        if b and "error" not in b:
            s[name] = {k: b[k] for k in ("domains", "titles", "searches") if k in b}
            s[name]["activity"] = peaks(b["hours"])
    for name in ("claude", "chatgpt"):
        b = summary.get(name)
        if b and "error" not in b:
            # ponytail: chats are work-only, so they get fewer samples/keywords than the browser
            s[name] = {k: b[k] for k in ("messages", "projects", "cyrillic_ratio")}
            s[name]["keywords"] = b["keywords"][:25]
            s[name]["samples"] = b["samples"][:30]
            s[name]["activity"] = peaks(b["hours"])
    return json.dumps(s, ensure_ascii=False, indent=0)


def llm_json(prompt, model, host, temperature=0.7):
    """One prompt in, parsed JSON out. Shared by persona.py and anti.py."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"num_ctx": 20000, "temperature": temperature},
    }
    req = urllib.request.Request(
        f"{host}/api/chat", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=3600) as r:
        return json.loads(json.load(r)["message"]["content"])


def ask_ollama(summary, model, host, lang):
    return llm_json(PROMPT.format(lang=lang, data=llm_input(summary)), model, host)


# ---------------------------------------------------------------- report
HTML = r"""<!doctype html><html><head><meta charset="utf-8"><title>Persona vs Anti-Persona</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0f1115;--card:#181b22;--fg:#e6e6e6;--mut:#8b90a0;--a:#5eead4;--b:#f97316}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,Inter,sans-serif;padding:24px}
h1{margin:0 0 4px;font-size:28px}h2{font-size:18px;margin:0 0 12px}.mut{color:var(--mut)}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.card{background:var(--card);border-radius:12px;padding:20px}
.who{border-top:4px solid var(--a)}.anti{border-top:4px solid var(--b)}
.name{font-size:22px;font-weight:700}.tag{font-style:italic;color:var(--mut);margin-bottom:12px}
ul{padding-left:18px;margin:4px 0 12px}.k{font-weight:600;color:var(--mut);text-transform:uppercase;font-size:12px;letter-spacing:.06em;margin-top:10px}
.chips span{display:inline-block;background:#262a35;border-radius:999px;padding:2px 10px;margin:3px;font-size:13px}
table{width:100%;border-collapse:collapse;font-size:14px}td{padding:6px 8px;border-top:1px solid #262a35;vertical-align:top}
canvas{max-height:260px}
</style></head><body>
<h1>Persona vs Anti-Persona</h1><div class="mut" id="meta"></div>
<div class="grid" style="margin-top:20px">
 <div class="card who" id="persona"></div>
 <div class="card anti" id="anti"></div>
</div>
<div class="grid" style="margin-top:16px">
 <div class="card"><h2>Top domains</h2><canvas id="dom"></canvas></div>
 <div class="card"><h2>Activity by hour</h2><canvas id="hrs"></canvas></div>
 <div class="card"><h2>AI chats by project</h2><canvas id="prj"></canvas></div>
 <div class="card"><h2>Search terms</h2><div class="chips" id="srch"></div></div>
 <div class="card"><h2>Prompt keywords</h2><div class="chips" id="kw"></div></div>
 <div class="card"><h2>Evidence</h2><table id="ev"></table></div>
</div>
<script>
const D=__DATA__;const S=D.summary,P=D.llm.persona||{},A=D.llm.anti_persona||{};
const li=a=>'<ul>'+(a||[]).map(x=>'<li>'+x+'</li>').join('')+'</ul>';
const card=(p,extra)=>`<div class="name">${p.name||'?'}</div><div class="tag">${p.tagline||''}</div><p>${p.summary||''}</p>
<div class="k">Traits</div>${li(p.traits)}<div class="k">Interests</div>${li(p.interests)}<div class="k">Outside work</div>${li(p.interests_outside_work)}<div class="k">Values</div>${li(p.values)}
<div class="k">Habits</div>${li(p.habits)}<div class="k">Communication</div><p>${p.communication_style||''}</p>
<div class="k">Typical day</div><p>${p.typical_day||''}</p>${extra||''}`;
document.getElementById('persona').innerHTML=card(P);
document.getElementById('anti').innerHTML=card(A,'<div class="k">Why opposite</div>'+li(A.why_opposite));
document.getElementById('meta').textContent=`${D.generated} · model ${D.model} · sources: ${Object.keys(S).filter(k=>!S[k].error).join(', ')}`;
const bar=(id,labels,data,color,horiz)=>new Chart(document.getElementById(id),{type:'bar',data:{labels,datasets:[{data,backgroundColor:color}]},
 options:{indexAxis:horiz?'y':'x',plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#8b90a0'}},y:{ticks:{color:'#8b90a0'}}}}});
const dom=(S.chrome&&S.chrome.domains||S.safari&&S.safari.domains||[]).slice(0,15);
bar('dom',dom.map(d=>d.key),dom.map(d=>d.n),'#5eead4',true);
const hrs=Array(24).fill(0);for(const k of ['chrome','safari','claude','chatgpt'])(S[k]&&S[k].hours||[]).forEach((v,i)=>hrs[i]+=v);
bar('hrs',hrs.map((_,i)=>i+':00'),hrs,'#f97316');
const prj=(S.claude&&S.claude.projects||[]).concat(S.chatgpt&&S.chatgpt.projects||[]).slice(0,12);
bar('prj',prj.map(d=>d.key),prj.map(d=>d.n),'#a78bfa',true);
document.getElementById('srch').innerHTML=(S.chrome&&S.chrome.searches||[]).map(s=>`<span>${s.key}</span>`).join('');
document.getElementById('kw').innerHTML=(S.claude&&S.claude.keywords||[]).map(s=>`<span>${s.key} · ${s.n}</span>`).join('');
document.getElementById('ev').innerHTML=(P.evidence||[]).map(e=>`<tr><td>${e.claim}</td><td class="mut">${e.source}</td></tr>`).join('');
</script></body></html>"""


# ---------------------------------------------------------------- main
def size(p):
    try:
        return f"{sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) / 1e6:.0f} MB" if p.is_dir() else f"{p.stat().st_size / 1e6:.1f} MB"
    except OSError:
        return "?"


def consent(sources, out):
    print("This program will READ (never modify) these local sources:")
    for name, path in sources:
        mark = "ok" if path and path.exists() else "--"
        print(f"  [{mark}] {name:<8} {path} ({size(path) if path and path.exists() else 'missing'})")
    print(
        "\nIt aggregates everything locally and sends ONLY a statistical summary\n"
        "(top domains, search terms, keywords, ~60 short prompt samples) to an\n"
        f"Ollama model running on this machine. Output goes to {out.resolve()}/\n"
    )
    return input("Proceed? [y/N] ").strip().lower() == "y"


def run(step, fn, *a):
    print(f"* {step} ...", end=" ", flush=True)
    try:
        r = fn(*a)
        print("ok")
        return r
    except Exception as e:  # noqa: BLE001 - one bad source must not kill the run
        print(f"skipped: {e}")
        return {"error": str(e)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true", help="skip consent prompt")
    ap.add_argument("--extra", type=Path, help="folder with ChatGPT export (conversations.json)")
    ap.add_argument("--model", default="qwen3.8:latest")
    ap.add_argument("--host", default="http://localhost:11434")
    ap.add_argument("--lang", default="English", help="language of the generated personas")
    ap.add_argument("--out", type=Path, default=Path("out"))
    ap.add_argument("--no-llm", action="store_true", help="only collect + charts, skip Ollama")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    sources = [("chrome", CHROME), ("safari", SAFARI), ("claude", CLAUDE), ("chatgpt", args.extra)]
    if not args.yes and not consent(sources, args.out):
        sys.exit("aborted")

    summary = {
        "chrome": run("chrome", collect_chrome),
        "safari": run("safari (needs Full Disk Access for your terminal)", collect_safari),
        "claude": run("claude code chats", collect_claude),
    }
    if args.extra:
        summary["chatgpt"] = run("chatgpt export", collect_chatgpt, args.extra)

    args.out.mkdir(exist_ok=True)
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))

    llm = {}
    if not args.no_llm:
        llm = run(f"ollama {args.model} (minutes, be patient)", ask_ollama, summary, args.model, args.host, args.lang)
        if "error" in llm:
            llm = {}
        else:
            (args.out / "persona.json").write_text(json.dumps(llm, ensure_ascii=False, indent=1))

    report = {"generated": dt.datetime.now().isoformat(timespec="minutes"), "model": args.model, "summary": summary, "llm": llm}
    html = args.out / "report.html"
    html.write_text(HTML.replace("__DATA__", json.dumps(report, ensure_ascii=False)))
    print(f"\nreport: {html.resolve()}")
    webbrowser.open(html.resolve().as_uri())


def selftest():
    assert chrome_time(13400000000000000).year == 2025
    assert safari_time(780000000).year == 2025
    assert clean_prompt("<command-name>/x</command-name><command-args>hello world</command-args>") == "/x hello world"
    assert clean_prompt([{"type": "tool_result", "tool_use_id": "1"}, {"type": "text", "text": "hi"}]) == "hi"
    assert clean_prompt([{"type": "text", "text": "tool_use_id leaked"}]) == ""
    st = text_stats(["привет мир " * 3, "hello there friend " * 2], collections.Counter({9: 2}), collections.Counter())
    assert 0 < st["cyrillic_ratio"] < 1 and st["hours"][9] == 2 and st["keywords"][0]["key"] in ("привет", "мир")
    assert domain("https://www.GitHub.com/x") == "github.com"
    pk = peaks([10] * 8 + [50, 90, 70] + [10] * 11 + [30, 30])
    assert pk["peak_hours"] == [9, 10, 8] and pk["share_22_02"] > 0
    print("selftest ok")


if __name__ == "__main__":
    main()
