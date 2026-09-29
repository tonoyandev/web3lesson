#!/usr/bin/env python3
"""anti.py - a gradual, measured drift from persona to anti-persona.

    python3 persona.py --yes      research: out/summary.json + out/persona.json
    python3 anti.py plan          roadmap of N bridged stages, validated with embeddings
    python3 anti.py approve       review and approve the roadmap (required for --yes)
    python3 anti.py login         one-time manual login in the automation Chrome profile
    python3 anti.py run           live the next stage: google, youtube, chatgpt, claude
    python3 anti.py metrics       plan quality, execution, observed drift -> out/transition.html
    python3 anti.py schedule      run one day of the roadmap daily via launchd
    python3 anti.py selftest

State is two files: state/roadmap.json (the plan) and state/log.jsonl (what was done).
"""
import argparse
import collections
import datetime as dt
import hashlib
import json
import math
import os
import plistlib
import random
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import persona

ROOT = Path(__file__).resolve().parent
STATE, OUT = ROOT / "state", ROOT / "out"
ROADMAP_F, LOG_F, METRICS_F = STATE / "roadmap.json", STATE / "log.jsonl", STATE / "metrics.jsonl"
VARIANTS_F = STATE / "variants.json"  # day 2+ wording of each stage, generated once, then fixed
PROFILE_HISTORY = Path.home() / ".anti/profile/Default/History"
PLIST = Path.home() / "Library/LaunchAgents/com.anti.run.plist"
HOST, MODEL, EMBED = "http://localhost:11434", "qwen3.8:latest", "nomic-embed-text"
# stage field -> browser channel; the field name is the whole routing table
CHANNELS = {"search_queries": "google", "youtube_queries": "youtube", "chat_prompts": "chatgpt", "claude_prompts": "claude"}
PICK = ("name", "interests", "interests_outside_work", "values", "habits", "typical_day")
SMOOTH = 0.5  # min similarity between neighbouring stages
COVER = 0.55  # an anti-persona interest counts as reached above this similarity
MIN_COVERAGE = 0.7
MAX_TRIES = 2  # a failing action is dropped after this many attempts, so one broken channel can't stall the roadmap
GAP_HOURS = 12  # a new day starts at most once per this many hours (a daily schedule passes, a double manual run doesn't)
DAYS_PER_STAGE = 5  # people drift over weeks, not days: 10 stages x 5 days is about 7 weeks
EST_SECONDS = {"google": 55, "youtube": 75, "chatgpt": 60, "claude": 60}

ROADMAP = """You design a gradual, believable drift of one person's online interests from
PERSONA to ANTI-PERSONA over {n} stages. One stage = one day of browsing.

Chain rule: stage k must share ONE concrete element (an object, activity, place,
feeling or problem) with stage k-1 and introduce ONE new element that moves
toward the anti-persona. Name the shared element in "bridge". Someone who sees
only stages k-1 and k must find the step natural; someone who sees stage 1 and
stage {n} must be surprised.

Example chain for a different person (do not copy it):
software development -> how to relax after a hard day at the keyboard ->
weekend walks, beauty of natural landscapes -> is it possible to step away
from software -> how to live in nature

Stage 1 is what this person searches today (match REAL_SEARCHES in topic and
style). Stage {n} is 100% the anti-persona. Even pace: no stage skips ahead,
none drifts back. How far along each stage should be: {targets}.
Once a stage has left a topic behind, later stages never return to it.

Query style: write searches the way this person types (see REAL_SEARCHES: short,
lowercase, mixing languages in their proportion; cyrillic share of their writing
is {cyr}). "chat_prompts" and "claude_prompts" are full first-person sentences
as typed into an AI chat. Never use personal names that appear in REAL_SEARCHES.

PERSONA: {persona}
ANTI-PERSONA: {anti}
REAL_SEARCHES: {searches}

{feedback}
Answer with ONLY this JSON:
{{"stages":[{{"k":1,"theme":"","bridge":"shared element with stage k-1 ('start' for k=1)",
 "search_queries":["5 items"],"youtube_queries":["2 items"],"chat_prompts":["2 items"],"claude_prompts":["2 items"]}}]}}
"""

JUDGE = """Below are the most visited page titles and search terms of one browser profile.
List the 6 main interests of the person who uses it, as short phrases.
Answer with ONLY this JSON: {{"interests": ["6 items"]}}

SEARCHES: {searches}
TITLES: {titles}
"""

VARY = """The same person keeps exploring "{theme}" on another day (day {day} of {days}).
Rewrite the actions below the way they would do them today: same topic, same point
in their journey, new wording and new angles, never a copy of the originals.
Keep every list the same length, the same language mix and the same typing style
(searches short and lowercase, chat prompts as full first-person sentences).
Answer with ONLY a JSON object with the same keys.

{actions}
"""

TOS = """
Before you approve:
- Automating the ChatGPT and Claude.ai web apps is against OpenAI's and Anthropic's
  terms of use; the accounts logged in to ~/.anti/profile can be flagged.
  Leave them out with: run --only google,youtube
- Google and YouTube may show an "unusual traffic" page. The run stops there and
  never tries to get past it.
- Everything runs in a separate Chrome profile (~/.anti/profile); your main Chrome
  and its history are untouched.
"""


# ---------------------------------------------------------------- vectors
def now_iso():
    return dt.datetime.now().isoformat(timespec="seconds")


def unit(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cos(u, v):
    return sum(a * b for a, b in zip(u, v)) / ((math.sqrt(sum(a * a for a in u)) * math.sqrt(sum(b * b for b in v))) or 1.0)


def centroid(vs):
    return unit([sum(c) / len(vs) for c in zip(*vs)])


def proj(e, p, a):
    """Position of e on the persona -> anti-persona axis: 0 at p, 1 at a."""
    return (cos(e, a) - cos(e, p)) / (2 * max(1 - cos(p, a), 1e-6)) + 0.5


def embed(texts, host=None):
    out = []
    for i in range(0, len(texts), 64):
        body = {"model": EMBED, "input": ["search_query: " + t for t in texts[i : i + 64]]}
        req = urllib.request.Request(
            f"{host or HOST}/api/embed", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            out += json.load(r)["embeddings"]
    return [unit(v) for v in out]


def interests(side):
    return [str(x) for x in (side.get("interests") or []) + (side.get("interests_outside_work") or [])]


def make_axis(rm, emb):
    """Returns pos(vectors) -> 0 (persona) .. 1 (anti-persona), and the anti interest vectors.

    Single texts never sit exactly on a centroid, so the scale is anchored on the
    mean position of the persona's and anti-persona's own interest phrases."""
    P, A = interests(rm["persona"]), interests(rm["anti_persona"])
    vs = emb(P + A)
    vp, va = vs[: len(P)], vs[len(P) :]
    p, a = centroid(vp), centroid(va)
    t0 = sum(proj(v, p, a) for v in vp) / len(vp)
    t1 = sum(proj(v, p, a) for v in va) / len(va)

    def pos(group):
        return (sum(proj(v, p, a) for v in group) / len(group) - t0) / max(t1 - t0, 1e-6) if group else None

    return pos, va


# ---------------------------------------------------------------- roadmap
def stage_texts(s):
    return [t for f in CHANNELS for t in s.get(f, [])]


def normalize(out):
    raw = out.get("stages") if isinstance(out, dict) else out
    stages = []
    for s in raw if isinstance(raw, list) else []:
        if not isinstance(s, dict):
            continue
        st = {"k": len(stages) + 1, "theme": str(s.get("theme", "")).strip(), "bridge": str(s.get("bridge", "")).strip()}
        for f in CHANNELS:
            st[f] = [str(x).strip() for x in (s.get(f) or []) if str(x).strip()][:5]
        if not stage_texts(st) and st["theme"]:
            st["search_queries"] = [st["theme"]]
        if stage_texts(st):
            stages.append(st)
    return stages


def score(rm, emb=embed, n=None):
    pos, va = make_axis(rm, emb)
    st = rm["stages"]
    n = n or len(st)
    flat = [t for s in st for t in stage_texts(s)]
    vs, groups = emb(flat), []
    for s in st:
        groups.append(vs[: len(stage_texts(s))])
        vs = vs[len(stage_texts(s)) :]
    P = [round(pos(g), 3) for g in groups]
    C = [round(cos(centroid(g1), centroid(g2)), 3) for g1, g2 in zip(groups, groups[1:])]
    steps = [b - a for a, b in zip(P, P[1:])]
    late = [v for g in groups[len(groups) // 2 :] for v in g]
    A = interests(rm["anti_persona"])
    uncovered = [name for name, v in zip(A, va) if max((cos(v, w) for w in late), default=0) < COVER]
    sc = {
        "pos": P,
        "cos_next": C,
        "min_cos": min(C, default=1.0),
        "max_step": round(max(steps, default=0.0), 3),
        "step_limit": round(2 / max(n - 1, 1), 3),
        "backslides": sum(d < -0.05 for d in steps),
        "coverage": round(1 - len(uncovered) / max(len(A), 1), 3),
        "uncovered": uncovered,
        "stages": len(st),
        "expected": n,
    }
    sc["valid"] = (
        len(st) == n
        and sc["backslides"] == 0
        and sc["min_cos"] >= SMOOTH
        and sc["max_step"] <= sc["step_limit"]
        and sc["coverage"] >= MIN_COVERAGE
    )
    return sc


def quality(sc):
    """Lower is better; used to keep the best of several attempts."""
    return (sc["stages"] != sc["expected"], sc["backslides"], -sc["coverage"], -sc["min_cos"])


def feedback(sc):
    P, lines = sc["pos"], []
    if sc["stages"] != sc["expected"]:
        lines.append(f"return exactly {sc['expected']} stages, not {sc['stages']}")
    for k, c in enumerate(sc["cos_next"], 1):
        if c < SMOOTH:
            lines.append(f"stage {k}->{k + 1} is too abrupt (similarity {c:.2f}): make them share a concrete element")
    for k in range(1, len(P)):
        d = P[k] - P[k - 1]
        if d < -0.05:
            lines.append(f"stage {k + 1} drifts back toward the persona ({P[k]:.2f} after {P[k - 1]:.2f})")
        if d > sc["step_limit"]:
            lines.append(f"stage {k}->{k + 1} jumps {d:.2f} of the whole way at once: spread the change evenly")
    for k, x in enumerate(P):
        want = k / max(len(P) - 1, 1)
        if abs(x - want) > 0.2:
            side = "closer to the persona" if x > want else "further toward the anti-persona"
            lines.append(f"stage {k + 1} sits at {x:.0%} of the way, target {want:.0%}: make it {side}")
    if P and P[0] > 0.3:
        lines.append(f"stage 1 is already {P[0]:.2f} of the way; start from what this person searches today")
    if P and P[-1] < 0.8:
        lines.append(f"the final stage reaches only {P[-1]:.2f} of the way; it must be fully the anti-persona")
    if sc["uncovered"]:
        lines.append("anti-persona interests never reached in the second half: " + ", ".join(f'"{u}"' for u in sc["uncovered"]))
    return "Your previous attempt had these problems, fix them:\n- " + "\n- ".join(lines) if lines else ""


def print_plan(rm):
    sc = rm.get("scores", {})
    P, C = sc.get("pos", []), sc.get("cos_next", [])
    cell = lambda xs, i: f"{xs[i]:5.2f}" if i < len(xs) else "    -"  # noqa: E731
    d = days(rm)
    print(f"\npace: {len(rm['stages'])} stages x {d} day(s) = {len(rm['stages']) * d} days of browsing")
    print(f"\n{'k':>2} {'pos':>5} {'sim>':>5}  theme  |  bridge")
    for i, s in enumerate(rm["stages"]):
        print(f"{s['k']:>2} {cell(P, i)} {cell(C, i)}  {s['theme'][:58]}  |  {s['bridge'][:48]}")
    if sc:
        ok = lambda b: "ok" if b else "MISS"  # noqa: E731
        print(
            f"\nmin similarity {sc['min_cos']:.2f} (>= {SMOOTH}) {ok(sc['min_cos'] >= SMOOTH)} | "
            f"max step {sc['max_step']:.2f} (<= {sc['step_limit']}) {ok(sc['max_step'] <= sc['step_limit'])} | "
            f"backslides {sc['backslides']} (= 0) {ok(sc['backslides'] == 0)} | "
            f"coverage {sc['coverage']:.0%} (>= {MIN_COVERAGE:.0%}) {ok(sc['coverage'] >= MIN_COVERAGE)}"
        )
        if sc["uncovered"]:
            print("not reached:", ", ".join(sc["uncovered"]))


# ---------------------------------------------------------------- state
def rid(rm):
    return hashlib.sha1(json.dumps(rm["stages"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]


def load_roadmap():
    if not ROADMAP_F.exists():
        sys.exit("no roadmap yet: python3 anti.py plan")
    return json.loads(ROADMAP_F.read_text())


def save_roadmap(rm):
    STATE.mkdir(exist_ok=True)
    ROADMAP_F.write_text(json.dumps(rm, ensure_ascii=False, indent=1))


def read_log(r):
    if not LOG_F.exists():
        return []
    rows = [json.loads(line) for line in LOG_F.read_text().splitlines() if line.strip()]
    return [x for x in rows if x.get("roadmap") == r]


def log_line(**d):
    STATE.mkdir(exist_ok=True)
    with open(LOG_F, "a") as f:
        f.write(json.dumps({"ts": now_iso(), **d}, ensure_ascii=False) + "\n")


def days(rm):
    return max(int(rm.get("days_per_stage") or 1), 1)


def slots(rm):
    """One slot = one day of one stage, in the order they are lived."""
    return [(s["k"], d) for s in rm["stages"] for d in range(1, days(rm) + 1)]


def load_variants():
    return json.loads(VARIANTS_F.read_text()) if VARIANTS_F.exists() else {}


def slot_source(rm, k, d, variants):
    """The stage itself on day 1, its saved rewording later, None if not generated yet."""
    stage = next(s for s in rm["stages"] if s["k"] == k)
    return stage if d == 1 else variants.get(f"{rid(rm)}:{k}.{d}")


def slot_actions(src, k, d):
    acts = [(k, d, ch, t) for f, ch in CHANNELS.items() for t in src.get(f, [])]
    random.Random(k * 1000 + d).shuffle(acts)  # mixed channels, same order on every retry
    return acts


def key(x):
    return (x["stage"], x.get("day", 1), x["channel"], x["text"])


def pending(acts, log, only=None):
    done = {key(x) for x in log if x.get("ok")}
    tries = collections.Counter(key(x) for x in log if not x.get("ok"))
    return [a for a in acts if a not in done and tries[a] < MAX_TRIES and (not only or a[2] in only)]


def open_slots(rm, log, variants, only=None, stage=None, every=False):
    """Slots with work left, in order; a slot whose wording is not generated yet counts as open."""
    out = []
    for k, d in slots(rm):
        if stage and k != stage:
            continue
        src = slot_source(rm, k, d, variants)
        if src is None or pending(slot_actions(src, k, d), log, only):
            out.append((k, d))
            if not every:
                break
    return out


def too_soon(log, k, d=1):
    """Hours left before day d of stage k may start; 0 means go."""
    if any((x["stage"], x.get("day", 1)) == (k, d) for x in log):
        return 0  # already started: resume
    prev = [x["ts"] for x in log if (x["stage"], x.get("day", 1)) < (k, d)]
    if not prev:
        return 0
    passed = (dt.datetime.now() - dt.datetime.fromisoformat(max(prev))).total_seconds() / 3600
    return max(GAP_HOURS - passed, 0)


def merge_variant(stage, out):
    """Keep the model's rewording where usable, fall back to the original wording per item."""
    v = {"k": stage["k"], "theme": stage["theme"]}
    for f in CHANNELS:
        orig = stage.get(f, [])
        new = [str(x).strip() for x in ((out or {}).get(f) or []) if str(x).strip()] if isinstance(out, dict) else []
        v[f] = (new + orig[len(new):])[: len(orig)]
    return v


def make_variant(rm, k, d, a):
    stage = next(s for s in rm["stages"] if s["k"] == k)
    fields = {f: stage.get(f, []) for f in CHANNELS if stage.get(f)}
    prompt = VARY.format(theme=stage["theme"], day=d, days=days(rm), actions=json.dumps(fields, ensure_ascii=False, indent=1))
    print(f"* rewording stage {k} for day {d} with {a.model} ...", flush=True)
    try:
        out = persona.llm_json(prompt, a.model, a.host, temperature=0.9)
    except (urllib.error.URLError, ValueError, KeyError) as e:
        print(f"  model unavailable ({e}); day {d} reuses the original wording")
        out = None
    variants = load_variants()
    variants[f"{rid(rm)}:{k}.{d}"] = v = merge_variant(stage, out)
    STATE.mkdir(exist_ok=True)
    VARIANTS_F.write_text(json.dumps(variants, ensure_ascii=False, indent=1))
    return v


# ---------------------------------------------------------------- commands
def cmd_plan(a):
    if a.check:
        rm = load_roadmap()
        if a.days_per_stage:  # pacing only: approval and progress survive
            rm["days_per_stage"] = a.days_per_stage
        rm["scores"] = score(rm)
        save_roadmap(rm)
        print_plan(rm)
        return 0
    try:
        pj = json.loads((OUT / "persona.json").read_text())
        summ = json.loads((OUT / "summary.json").read_text())
    except FileNotFoundError:
        sys.exit("run the research first: python3 persona.py --yes")
    per = {k: pj["persona"].get(k) for k in PICK}
    anti = {k: pj["anti_persona"].get(k) for k in PICK}
    searches = [x["key"] for x in (summ.get("chrome") or {}).get("searches", [])][:30]
    cyr = (summ.get("claude") or {}).get("cyrillic_ratio", 0)
    if a.goal:  # the user's own destination replaces the anti-persona's (inverted) work interests
        anti["interests"] = [g.strip() for g in a.goal.split(",") if g.strip()]
    targets = ", ".join(f"stage {k}: {(k - 1) / (a.stages - 1):.0%}" for k in range(1, a.stages + 1))
    dumps = lambda o: json.dumps(o, ensure_ascii=False)  # noqa: E731
    best, fb = None, ""
    for i in range(1, a.attempts + 1):
        print(f"* attempt {i}/{a.attempts}: {a.model} writes {a.stages} stages (a few minutes) ...", flush=True)
        prompt = ROADMAP.format(n=a.stages, cyr=cyr, targets=targets, persona=dumps(per), anti=dumps(anti), searches=dumps(searches), feedback=fb)
        try:
            cand = {"persona": per, "anti_persona": anti, "stages": normalize(persona.llm_json(prompt, a.model, a.host))}
        except (urllib.error.URLError, ValueError, KeyError) as e:
            print(f"  failed: {e}")
            continue
        if not cand["stages"]:
            fb = "Your previous answer had no usable stages. Follow the JSON format exactly."
            continue
        cand["scores"] = sc = score(cand, n=a.stages)
        print(
            f"  stages {sc['stages']}, min similarity {sc['min_cos']:.2f}, max step {sc['max_step']:.2f}, "
            f"backslides {sc['backslides']}, coverage {sc['coverage']:.0%}, valid {sc['valid']}"
        )
        if best is None or quality(sc) < quality(best["scores"]):
            best = cand
        if sc["valid"]:
            break
        fb = feedback(sc)
    if best is None:
        sys.exit("the model gave no usable roadmap; try again or use --model qwen3:14b")
    if ROADMAP_F.exists():  # keep the previous plan, its log lines stay keyed by its id
        ROADMAP_F.rename(STATE / f"roadmap-{rid(load_roadmap())}.json")
    rm = {"created": now_iso(), "model": a.model, **best, "searches": searches,
          "days_per_stage": a.days_per_stage or DAYS_PER_STAGE, "approved": None}
    save_roadmap(rm)
    print_plan(rm)
    print(f"\nsaved {ROADMAP_F.relative_to(ROOT)}; edit it by hand if you like, then: python3 anti.py approve")
    return 0


def cmd_approve(a):
    rm = load_roadmap()
    if "scores" not in rm:
        rm["scores"] = score(rm)
    print_plan(rm)
    if not rm["scores"]["valid"]:
        print("\nWARNING: this roadmap misses some quality targets (see MISS above).")
    print(TOS)
    if input("Approve this roadmap for autonomous runs? [y/N] ").strip().lower() != "y":
        return 1
    rm["approved"] = rid(rm)
    save_roadmap(rm)
    print(f"approved roadmap {rm['approved']}; any edit to its stages revokes this")
    return 0


def cmd_login(a):
    import browse

    with browse.session(close_popups=False) as page:
        page.goto("https://accounts.google.com/")
        for url in ("https://chatgpt.com/", "https://claude.ai/login"):
            page.context.new_page().goto(url)
        input("Log in to Google, ChatGPT and Claude in the opened window, then press Enter here ... ")
    return 0


def estimate(acts, fast):
    return round(sum(15 if fast else EST_SECONDS[ch] + 12 for _, _, ch, _ in acts) / 60)


def cmd_run(a):
    rm = load_roadmap()
    r = rid(rm)
    log = read_log(r)
    only = set(a.only.split(",")) if a.only else None
    if only and only - set(CHANNELS.values()):
        sys.exit(f"unknown channel(s) {sorted(only - set(CHANNELS.values()))}; use {sorted(CHANNELS.values())}")
    todo = open_slots(rm, log, load_variants(), only, a.stage, a.all)
    if not todo:
        print(f"nothing pending for stage {a.stage}" if a.stage else "nothing pending: the roadmap is complete")
        return 0
    if not (a.force or a.stage or a.all):
        left = too_soon(log, *todo[0])
        if left:
            print(f"stage {todo[0][0]} day {todo[0][1]} is due in {left:.1f} h (one day at a time keeps it gradual); --force overrides")
            return 0
    if a.yes and rm.get("approved") != r:
        sys.exit("the roadmap is not approved (or was edited after approval): python3 anti.py approve")
    plan = []
    for k, d in todo:
        src = slot_source(rm, k, d, load_variants())
        if src is None and not a.dry_run:
            src = make_variant(rm, k, d, a)
        theme = next(s["theme"] for s in rm["stages"] if s["k"] == k)
        print(f"\nstage {k}/{len(rm['stages'])} day {d}/{days(rm)}: {theme}")
        if src is None:
            print("  (today's wording is generated by the model when the day actually runs)")
            continue
        acts = pending(slot_actions(src, k, d), log, only)
        print(f"  {len(acts)} actions, ~{estimate(acts, a.fast)} min")
        for _, _, ch, t in acts:
            print(f"  {ch:<8} {t}")
        plan.append(acts)
    if a.dry_run:
        return 0
    if not a.yes and input("\nOpen the browser and run this? [y/N] ").strip().lower() != "y":
        return 1

    import browse

    run_id = now_iso()
    with browse.session() as page:
        for i, acts in enumerate(plan):
            if i:
                browse.pause(60, 180, a.fast)
            for j, (k, d, ch, t) in enumerate(acts):
                if j:
                    browse.pause(5, 20, a.fast)
                print(f"* stage {k} day {d} {ch}: {t}", flush=True)
                base = {"roadmap": r, "run": run_id, "stage": k, "day": d, "channel": ch, "text": t}
                try:
                    log_line(**base, ok=True, **browse.CHANNELS[ch](page, t, a.fast))
                except browse.Challenge as e:
                    log_line(**base, ok=False, error=f"challenge: {e}")
                    print(f"\nSTOP: {ch} shows a login or verification page:\n  {e}\nHandle it yourself: python3 anti.py login")
                    return 2
                except Exception as e:  # noqa: BLE001 - one broken channel must not stop the others
                    log_line(**base, ok=False, error=f"{type(e).__name__}: {str(e)[:300]}")
                    print(f"  failed: {type(e).__name__}: {str(e)[:200]}")
    print("\nday done. python3 anti.py metrics")
    return 0


def judge(pos, a):
    """An independent observer: what does the automation profile's own history say about its user?"""
    if not PROFILE_HISTORY.exists():
        print("no automation profile history yet; skipping judge")
        return None
    h = persona.collect_chrome(PROFILE_HISTORY)
    prompt = JUDGE.format(
        searches=json.dumps([x["key"] for x in h["searches"]][:40], ensure_ascii=False),
        titles=json.dumps([x["key"] for x in h["titles"]][:40], ensure_ascii=False),
    )
    its = [str(x) for x in persona.llm_json(prompt, a.model, a.host, temperature=0.2).get("interests", [])][:6]
    return {"interests": its, "progress": round(pos(embed(its)), 3)} if its else None


def observed(x):
    return (x.get("reply") or "")[:400] or x.get("title") or ""


def cmd_metrics(a):
    rm = load_roadmap()
    r = rid(rm)
    log = read_log(r)
    per_day = [a for s in rm["stages"] for a in slot_actions(s, s["k"], 1)]  # every day has the same shape
    done = {key(x) for x in log if x.get("ok")}
    tried = {key(x) for x in log}
    exe = {
        ch: {"planned": days(rm) * sum(x[2] == ch for x in per_day), "ok": sum(x[2] == ch for x in done),
             "failed": sum(x[2] == ch for x in tried - done)}
        for ch in CHANNELS.values()
    }
    runs = {x.get("run") for x in log}
    m = {
        "date": now_iso(),
        "roadmap": r,
        "approved": rm.get("approved") == r,
        "plan": {k: rm.get("scores", {}).get(k) for k in ("min_cos", "max_step", "step_limit", "backslides", "coverage", "valid")},
        "completion": round(len(done) / max(days(rm) * len(per_day), 1), 3),
        "exec_rate": round(len(done) / max(len(tried), 1), 3) if tried else None,
        "challenge_rate": round(sum(str(x.get("error", "")).startswith("challenge") for x in log) / max(len(runs), 1), 3),
        "channels": exe,
        "days_per_stage": days(rm),
        "stages_ok": {s["k"]: sum(x[0] == s["k"] for x in done) for s in rm["stages"]},
    }
    try:
        pos, _ = make_axis(rm, embed)
        m["pos_obs"] = {}
        for s in rm["stages"]:
            texts = [t for t in (observed(x) for x in log if x.get("ok") and x["stage"] == s["k"]) if t]
            m["pos_obs"][s["k"]] = round(pos(embed(texts)), 3) if texts else None
        if a.judge:
            m["judge"] = judge(pos, a)
    except urllib.error.URLError as e:
        print(f"ollama is not reachable, drift metrics skipped: {e}")
    STATE.mkdir(exist_ok=True)
    with open(METRICS_F, "a") as f:
        f.write(json.dumps(m, ensure_ascii=False) + "\n")
    history = [json.loads(line) for line in METRICS_F.read_text().splitlines() if line.strip()]
    OUT.mkdir(exist_ok=True)
    data = {"stages": rm["stages"], "scores": rm.get("scores", {}), "m": m, "history": [h for h in history if h["roadmap"] == r],
            "targets": {"smooth": SMOOTH, "coverage": MIN_COVERAGE}}
    (OUT / "transition.html").write_text(HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))
    print(json.dumps({k: v for k, v in m.items() if k != "stages_ok"}, ensure_ascii=False, indent=1))
    print(f"\nreport: {(OUT / 'transition.html').resolve()}")
    return 0


def plist_for(hh, mm):
    cmd = f'cd "{ROOT}" && "{sys.executable}" anti.py run --yes; "{sys.executable}" anti.py metrics --judge'
    return {
        "Label": "com.anti.run",
        "ProgramArguments": ["/bin/sh", "-c", cmd],
        "StartCalendarInterval": {"Hour": hh, "Minute": mm},
        "StandardOutPath": str(STATE / "launchd.log"),
        "StandardErrorPath": str(STATE / "launchd.log"),
    }


def cmd_schedule(a):
    if sys.platform != "darwin":  # ponytail: launchd only; other systems get a line for their own scheduler
        hh, mm = map(int, a.at.split(":"))
        print("Automatic scheduling is macOS-only for now. Add this line with `crontab -e` (Linux):")
        print(f'{mm} {hh} * * * cd "{ROOT}" && "{sys.executable}" anti.py run --yes; "{sys.executable}" anti.py metrics --judge')
        return 0
    target = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", target, str(PLIST)], capture_output=True)
    if a.remove:
        PLIST.unlink(missing_ok=True)
        print("schedule removed")
        return 0
    rm = load_roadmap()
    if rm.get("approved") != rid(rm):
        sys.exit("approve the roadmap first: python3 anti.py approve")
    hh, mm = map(int, a.at.split(":"))
    STATE.mkdir(exist_ok=True)
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist_for(hh, mm)))
    subprocess.run(["launchctl", "bootstrap", target, str(PLIST)], check=True)
    print(f"runs daily at {a.at} (on wake if the Mac was asleep); log: state/launchd.log; stop: python3 anti.py schedule --remove")
    return 0


HTML = r"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Persona Transition</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root{--bg:#0f1115;--card:#181b22;--fg:#e6e6e6;--mut:#8b90a0;--a:#5eead4;--b:#f97316;--ok:#4ade80;--bad:#f87171}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,Inter,sans-serif;padding:24px 16px}
h1{margin:0 0 4px;font-size:26px}h2{font-size:17px;margin:0 0 12px}.mut{color:var(--mut)}
.grid{display:grid;gap:16px;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));margin-top:16px}
.card{background:var(--card);border-radius:12px;padding:18px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px}td,th{padding:6px 8px;border-top:1px solid #262a35;text-align:left;vertical-align:top}
.ok{color:var(--ok)}.bad{color:var(--bad)}canvas{max-height:280px}
</style></head><body>
<h1>Persona &rarr; Anti-Persona</h1><div class="mut" id="meta"></div>
<div class="grid">
 <div class="card"><h2>Scorecard</h2><table id="score"></table></div>
 <div class="card"><h2>Position on the axis (0 persona, 1 anti-persona)</h2><canvas id="pos"></canvas></div>
 <div class="card"><h2>Judge progress by day</h2><canvas id="prog"></canvas><div class="mut" id="judge"></div></div>
</div>
<div class="card" style="margin-top:16px"><h2>Stages</h2><table id="stages"></table></div>
<script>
const D=__DATA__,S=D.scores,M=D.m,T=D.targets;
document.getElementById('meta').textContent=`roadmap ${M.roadmap} · ${M.approved?'approved':'not approved'} · ${M.date}`;
const f=x=>x==null?'–':(typeof x==='number'?(+x.toFixed(3)):x);
const row=(n,v,t,ok)=>`<tr><td>${n}</td><td>${f(v)}</td><td class="mut">${t}</td><td class="${ok==null?'mut':ok?'ok':'bad'}">${ok==null?'–':ok?'pass':'miss'}</td></tr>`;
const jp=M.judge&&M.judge.progress;
document.getElementById('score').innerHTML='<tr><th>metric</th><th>value</th><th>target</th><th></th></tr>'+[
 row('min neighbour similarity',S.min_cos,'≥ '+T.smooth,S.min_cos>=T.smooth),
 row('max step',S.max_step,'≤ '+S.step_limit,S.max_step<=S.step_limit),
 row('backslides',S.backslides,'= 0',S.backslides===0),
 row('coverage of anti interests',S.coverage,'≥ '+T.coverage,S.coverage>=T.coverage),
 row('completion',M.completion,'1.0 at the end',null),
 row('execution rate',M.exec_rate,'≥ 0.9',M.exec_rate==null?null:M.exec_rate>=0.9),
 row('challenge rate',M.challenge_rate,'0',M.challenge_rate===0),
 row('judge progress',jp,'≥ 0.6 at the end',null)].join('');
const K=D.stages.map(s=>s.k),obs=M.pos_obs||{};
new Chart(document.getElementById('pos'),{type:'line',data:{labels:K,datasets:[
 {label:'planned',data:S.pos||[],borderColor:'#5eead4',backgroundColor:'#5eead4',tension:.3},
 {label:'observed',data:K.map(k=>obs[k]??null),borderColor:'#f97316',backgroundColor:'#f97316',showLine:false,pointRadius:6}]},
 options:{scales:{x:{title:{display:true,text:'stage',color:'#8b90a0'},ticks:{color:'#8b90a0'}},y:{ticks:{color:'#8b90a0'}}},plugins:{legend:{labels:{color:'#e6e6e6'}}}}});
const H=D.history.filter(h=>h.judge);
new Chart(document.getElementById('prog'),{type:'line',data:{labels:H.map(h=>h.date.slice(0,10)),datasets:[
 {label:'progress',data:H.map(h=>h.judge.progress),borderColor:'#a78bfa',backgroundColor:'#a78bfa',tension:.3}]},
 options:{scales:{x:{ticks:{color:'#8b90a0'}},y:{ticks:{color:'#8b90a0'}}},plugins:{legend:{display:false}}}});
document.getElementById('judge').textContent=M.judge?'judge sees: '+M.judge.interests.join(', '):'run metrics --judge to measure the profile itself';
const ok=M.stages_ok||{},tot=s=>['search_queries','youtube_queries','chat_prompts','claude_prompts'].reduce((n,k)=>n+(s[k]||[]).length,0);
document.getElementById('stages').innerHTML='<tr><th>k</th><th>theme</th><th>bridge</th><th>done</th><th>planned pos</th><th>observed</th></tr>'+
 D.stages.map((s,i)=>`<tr><td>${s.k}</td><td>${s.theme}</td><td class="mut">${s.bridge}</td><td>${ok[s.k]||0}/${tot(s)*(M.days_per_stage||1)}</td><td>${f((S.pos||[])[i])}</td><td>${f(obs[s.k])}</td></tr>`).join('');
</script></body></html>"""


# ---------------------------------------------------------------- main
def selftest():
    p, a = [1.0, 0.0], [0.0, 1.0]
    assert abs(proj(p, p, a)) < 1e-9 and abs(proj(a, p, a) - 1) < 1e-9 and abs(proj([0.5, 0.5], p, a) - 0.5) < 1e-9
    angle = {"x1": 0.0, "x2": 0.1, "y1": 1.47, "y2": 1.57, "s1": 0.05, "s2": 0.5, "s3": 1.0, "s4": 1.5}
    fake = lambda ts: [[math.cos(angle[t]), math.sin(angle[t])] for t in ts]  # noqa: E731
    rm = {
        "persona": {"interests": ["x1", "x2"]},
        "anti_persona": {"interests": ["y1"], "interests_outside_work": ["y2"]},
        "stages": [{"k": i, "theme": "", "bridge": "", "search_queries": [q]} for i, q in enumerate(["s1", "s2", "s3", "s4"], 1)],
    }
    sc = score(rm, emb=fake)
    assert sc["backslides"] == 0 and sc["coverage"] == 1 and sc["pos"][0] < 0.1 < 0.9 < sc["pos"][-1], sc
    bad = json.loads(json.dumps(rm))
    bad["stages"][2]["search_queries"] = ["s1"]
    sb = score(bad, emb=fake)
    assert sb["backslides"] == 1 and not sb["valid"] and "drifts back" in feedback(sb), sb
    assert "stage 3 sits at" in feedback(sb) and "further toward the anti-persona" in feedback(sb)
    assert rid(rm) != rid(bad)
    log = [{"stage": 1, "channel": "google", "text": "s1", "ok": True, "ts": now_iso()}]  # old lines have no "day"
    log += [{"stage": 2, "day": 1, "channel": "google", "text": "s2", "ok": False, "ts": now_iso()}] * MAX_TRIES
    acts = [x for k, d in slots(rm) for x in slot_actions(rm["stages"][k - 1], k, d)]
    assert [x[3] for x in pending(acts, log)] == ["s3", "s4"]
    assert pending(acts, log, only={"chatgpt"}) == []
    assert open_slots(rm, log, {}) == [(3, 1)] and open_slots(rm, log, {}, every=True) == [(3, 1), (4, 1)]
    assert too_soon(log[:1], 2) > GAP_HOURS - 1 and too_soon(log[:1], 1) == 0 and too_soon([], 1) == 0
    rm2 = {**rm, "days_per_stage": 2}
    assert len(slots(rm2)) == 8 and open_slots(rm2, log[:1], {}) == [(1, 2)]  # day 2 not worded yet -> open
    assert too_soon(log[:1], 1, 2) > GAP_HOURS - 1  # day 2 waits for the next day
    var = {f"{rid(rm2)}:1.2": {"search_queries": ["s1 again"]}}
    assert slot_source(rm2, 1, 2, var)["search_queries"] == ["s1 again"]
    assert open_slots(rm2, log[:1] + [{"stage": 1, "day": 2, "channel": "google", "text": "s1 again", "ok": True}], var) == [(2, 1)]
    st = {"k": 1, "theme": "t", "search_queries": ["a", "b"], "chat_prompts": ["c"]}
    assert merge_variant(st, {"search_queries": ["a2"], "chat_prompts": "bad"})["search_queries"] == ["a2", "b"]
    assert merge_variant(st, None)["chat_prompts"] == ["c"]
    assert plistlib.loads(plistlib.dumps(plist_for(20, 5)))["StartCalendarInterval"] == {"Hour": 20, "Minute": 5}
    got = normalize({"stages": [{"theme": "t", "search_queries": ["q", " "], "chat_prompts": None}, "junk", {"theme": "only"}]})
    assert [s["k"] for s in got] == [1, 2] and got[0]["search_queries"] == ["q"] and got[1]["search_queries"] == ["only"]
    print("selftest ok")
    return 0


def main():
    global HOST
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--model", default=MODEL)
    common.add_argument("--host", default=HOST)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", parents=[common], help="generate and validate the roadmap")
    p.add_argument("--stages", type=int, default=10)
    p.add_argument("--attempts", type=int, default=3)
    p.add_argument("--check", action="store_true", help="re-score an edited roadmap without the LLM")
    p.add_argument("--days-per-stage", type=int, help=f"days spent on each stage (default {DAYS_PER_STAGE}); with --check changes the pace of an existing roadmap")
    p.add_argument("--goal", help="comma list that replaces the anti-persona's work interests, e.g. 'gardening, living in nature'")
    sub.add_parser("approve", parents=[common], help="review and approve the roadmap")
    sub.add_parser("login", parents=[common], help="log in once in the automation profile")
    p = sub.add_parser("run", parents=[common], help="run the next stage")
    p.add_argument("--stage", type=int, help="run the next open day of this stage")
    p.add_argument("--all", action="store_true", help="demo: every open day of every stage now, 1-3 min apart")
    p.add_argument("--only", help="comma list of channels: google,youtube,chatgpt,claude")
    p.add_argument("--dry-run", action="store_true", help="print what would run, open nothing")
    p.add_argument("--yes", action="store_true", help="no prompt; requires an approved roadmap")
    p.add_argument("--fast", action="store_true", help="2-4 s pauses, for smoke tests only")
    p.add_argument("--force", action="store_true", help="ignore the one-stage-a-day guard")
    p = sub.add_parser("metrics", parents=[common], help="measure and write out/transition.html")
    p.add_argument("--judge", action="store_true", help="also ask the model what the profile history says (1-2 min)")
    p = sub.add_parser("schedule", parents=[common], help="run daily via launchd")
    p.add_argument("--at", default="20:00", help="HH:MM")
    p.add_argument("--remove", action="store_true")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "selftest":
        return selftest()
    HOST = a.host
    if a.cmd == "plan" and a.stages < 3:
        sys.exit("--stages must be at least 3")
    if a.cmd == "plan" and a.days_per_stage is not None and a.days_per_stage < 1:
        sys.exit("--days-per-stage must be at least 1")
    cmds = {"plan": cmd_plan, "approve": cmd_approve, "login": cmd_login, "run": cmd_run, "metrics": cmd_metrics, "schedule": cmd_schedule}
    return cmds[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
