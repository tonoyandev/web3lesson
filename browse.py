#!/usr/bin/env python3
"""browse.py - human-paced browser channels for anti.py (Playwright driving your installed Chrome).

    python3 browse.py google  "query"  [--fast]
    python3 browse.py youtube "query"  [--fast]
    python3 browse.py chatgpt "prompt" [--fast]
    python3 browse.py claude  "prompt" [--fast]

Runs in its own profile (~/.anti/profile), always visible. Login walls and bot
checks stop the run: they are never solved, bypassed or retried through.
"""
import json
import os
import random
import re
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote_plus

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

PROFILE = Path.home() / ".anti/profile"
LOCK = PROFILE.parent / "run.lock"  # pid of the process that holds the profile open


class Busy(Exception):
    """Another run is using the profile right now (a manual run while the scheduler fires, or vice versa)."""


def acquire_lock():
    """Chrome corrupts a profile opened twice; clearing SingletonLock below is only safe if no run is alive."""
    try:
        pid = int(LOCK.read_text())
        os.kill(pid, 0)
    except (OSError, ValueError):
        pass  # no lock, unreadable, or the process is gone
    else:
        raise Busy(f"another run (pid {pid}) holds {PROFILE}; wait for it or stop it")
    LOCK.write_text(str(os.getpid()))

# Sites redesign; when a channel breaks, fix its selector here and test with `python3 browse.py <channel> test --fast`.
SEL = {
    "google_box": "textarea[name='q'], input[name='q']",
    "google_result": "#search a:has(h3)",
    "youtube_video": "a#video-title",
    "chatgpt": {
        "url": "https://chatgpt.com/",
        "box": "#prompt-textarea",
        "stop": "[data-testid='stop-button']",
        "reply": "[data-message-author-role='assistant']",
    },
    "claude": {
        "url": "https://claude.ai/new",
        "box": "div[contenteditable='true']",
        "stop": "[data-is-streaming='true']",
        "reply": "[data-is-streaming='false']",
    },
}
STOP = re.compile(
    r"/sorry/|consent\.(google|youtube)|accounts\.google\.com|auth\.openai|/auth/|/login|"
    r"unusual traffic|verify you are human|just a moment",
    re.I,
)


class Challenge(Exception):
    """A login wall or bot check. The caller stops; a human deals with it."""


def pause(lo, hi, fast=False):
    time.sleep(random.uniform(2, 4) if fast else random.uniform(lo, hi))


@contextmanager
def session(close_popups=True):
    PROFILE.mkdir(parents=True, exist_ok=True)
    for d in (PROFILE.parent, PROFILE):
        d.chmod(0o700)  # holds Google/OpenAI/Anthropic cookies
    acquire_lock()
    for f in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (PROFILE / f).unlink(missing_ok=True)  # stale after a crash; safe because the lock above proved no run is alive
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(PROFILE),
            channel="chrome",
            headless=False,
            no_viewport=True,
            accept_downloads=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if close_popups:
            ctx.on("page", lambda p: p.close())
        try:
            yield page
        finally:
            ctx.close()
            LOCK.unlink(missing_ok=True)


def check(page):
    """Call only on the site's own pages (a result page may legitimately contain /login)."""
    if STOP.search(page.url) or STOP.search(page.title()):
        raise Challenge(page.url)


def human_type(page, text):
    for ch in text:
        page.keyboard.type(ch)
        time.sleep(random.uniform(0.06, 0.18) + (0.4 if ch == " " and random.random() < 0.15 else 0))


def read_like_human(page, seconds):
    end = time.time() + seconds
    while time.time() < end:
        page.mouse.wheel(0, random.choice([-250, 300, 450, 600]))
        time.sleep(min(random.uniform(1.5, 4), max(end - time.time(), 0)))


def wait_or_check(page, locator, timeout=15000):
    """Wait for the element; if it never shows, tell a challenge apart from a broken selector."""
    try:
        locator.first.wait_for(state="visible", timeout=timeout)
    except PWTimeout:
        check(page)
        raise


def google(page, text, fast=False):
    page.goto("https://www.google.com/", wait_until="domcontentloaded")
    check(page)
    page.locator(SEL["google_box"]).first.click()
    human_type(page, text)
    pause(0.3, 1.2)
    page.keyboard.press("Enter")
    results = page.locator(SEL["google_result"])
    wait_or_check(page, results)
    check(page)
    pick = results.nth(random.randrange(min(3, results.count())))
    pick.scroll_into_view_if_needed()
    pause(1, 3, fast)
    try:
        with page.expect_navigation(wait_until="domcontentloaded", timeout=20000):
            pick.click()
    except PWTimeout:
        pass  # slow site: read whatever has loaded
    read_like_human(page, random.uniform(2, 4) if fast else random.uniform(20, 60))
    return {"title": page.title(), "url": page.url}


def youtube(page, text, fast=False):
    page.goto("https://www.youtube.com/results?search_query=" + quote_plus(text), wait_until="domcontentloaded")
    check(page)
    videos = page.locator(SEL["youtube_video"])
    wait_or_check(page, videos)
    pause(1, 3, fast)
    videos.nth(random.randrange(min(3, videos.count()))).click()
    page.wait_for_url(re.compile(r"/(watch|shorts)"), timeout=15000)
    pause(30, 90, fast)
    return {"title": re.sub(r"\s*-\s*YouTube$", "", page.title()), "url": page.url}


def page_text(page):
    main = page.locator("main")
    return (main.first if main.count() else page.locator("body")).inner_text()


def chat(page, name, text, fast=False):
    s = SEL[name]
    page.goto(s["url"], wait_until="domcontentloaded")
    pause(2, 4)
    check(page)
    box = page.locator(s["box"]).or_(page.get_by_role("textbox")).first
    wait_or_check(page, box, timeout=20000)
    box.click()
    human_type(page, text)
    pause(0.5, 1.5)
    page.keyboard.press("Enter")
    # done = the page text changed, then held still for 3 s with no stop/streaming marker (selector-free fallback)
    before, last, still, t0 = page_text(page), None, 0, time.time()
    while time.time() - t0 < 180:
        page.wait_for_timeout(1000)
        cur = page_text(page)
        still, last = (still + 1 if cur == last else 0), cur
        if cur != before and still >= 3 and page.locator(s["stop"]).count() == 0:
            break
    replies = page.locator(s["reply"])
    reply = replies.last.inner_text() if replies.count() else (last or "")[-1500:]
    pause(10, 30, fast)
    return {"title": page.title(), "url": page.url, "reply": reply[:2000]}


CHANNELS = {
    "google": google,
    "youtube": youtube,
    "chatgpt": lambda page, text, fast=False: chat(page, "chatgpt", text, fast),
    "claude": lambda page, text, fast=False: chat(page, "claude", text, fast),
}

if __name__ == "__main__":
    args = [x for x in sys.argv[1:] if x != "--fast"]
    if len(args) != 2 or args[0] not in CHANNELS:
        sys.exit(__doc__)
    try:
        with session() as page:
            print(json.dumps(CHANNELS[args[0]](page, args[1], "--fast" in sys.argv), ensure_ascii=False, indent=1))
    except (Challenge, Busy) as e:
        sys.exit(f"{type(e).__name__.lower()}: {e}")
