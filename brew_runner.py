#!/usr/bin/env python3
"""Brew runner — watches data/brew_jobs/ and renders each job by driving the
real /aircraft page (?brew=<id>) in headless Chromium. The page itself uploads
every canvas in strict poster→datasheet order and signals brew_done (which
pokes the WeCom bot). Serialized: one render at a time (3.7GB-RAM box + swap).

systemd: brew-runner.service
"""
from __future__ import annotations

import json
import logging
import os
import time

from playwright.sync_api import sync_playwright

BASE = "/root/infographic-maker"
BREW_DIR = os.path.join(BASE, "data", "brew_jobs")
BREWS = os.path.join(BASE, "data", "brews")
SITE = "http://127.0.0.1:8640"
POLL_S = 10
RENDER_TIMEOUT_S = 420  # 10 styles x big canvases on a small box takes a while

logging.basicConfig(filename=os.path.join(BASE, "brew_runner.log"),
                    level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("brew")


def pending_jobs():
    out = []
    for f in sorted(os.listdir(BREW_DIR)):
        if not f.endswith(".json"):
            continue
        bid = f[:-5]
        jobp = os.path.join(BREW_DIR, f)
        man = os.path.join(BREWS, bid, "manifest.json")
        done = False
        if os.path.exists(man):
            try:
                done = json.load(open(man)).get("done", False)
            except Exception:
                pass
            # brew id == entry id, so re-running an entry reuses the id. If the
            # job file was (re)written AFTER the last render, the owner wants a
            # fresh brew — treat it as pending and clear the stale render.
            if done and os.path.getmtime(jobp) > os.path.getmtime(man) + 1:
                done = False
                import shutil
                shutil.rmtree(os.path.join(BREWS, bid), ignore_errors=True)
                log.info("brew %s: job newer than render — re-rendering", bid)
        if not done:
            out.append(bid)
    return out


def render(bid: str) -> bool:
    log.info("rendering brew %s", bid)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=[
            "--no-sandbox", "--disable-dev-shm-usage",
            "--js-flags=--max-old-space-size=1024",
        ])
        try:
            page = browser.new_page(viewport={"width": 1400, "height": 1000})
            page.goto(f"{SITE}/aircraft?brew={bid}", timeout=60_000)
            deadline = time.time() + RENDER_TIMEOUT_S
            while time.time() < deadline:
                done = page.evaluate("window.__BREW_DONE__ || null")
                err = page.evaluate("window.__BREW_ERROR__ || null")
                if err:
                    log.error("brew %s page error: %s", bid, err)
                    return False
                if done:
                    log.info("brew %s complete: %s images", bid, done)
                    return True
                time.sleep(3)
            log.error("brew %s timed out", bid)
            return False
        finally:
            browser.close()


def main():
    log.info("brew runner up; watching %s", BREW_DIR)
    fails: dict[str, int] = {}
    seen: dict[str, float] = {}
    while True:
        for bid in pending_jobs():
            # a freshly (re)written job resets its failure count
            jm = os.path.getmtime(os.path.join(BREW_DIR, f"{bid}.json"))
            if jm > seen.get(bid, 0):
                seen[bid] = jm
                fails[bid] = 0
            if fails.get(bid, 0) >= 3:
                continue  # give up quietly; bot already told the owner once
            ok = False
            try:
                ok = render(bid)
            except Exception as e:
                log.error("brew %s crashed: %s", bid, e)
            if not ok:
                fails[bid] = fails.get(bid, 0) + 1
                if fails[bid] == 3:
                    try:  # tell the owner once via the bot's internal hook
                        import requests
                        tok = ""
                        for line in open("/root/wecom-bot/wecom.env"):
                            if line.startswith("WECOM_TOKEN="):
                                tok = line.split("=", 1)[1].strip()
                        requests.post("http://127.0.0.1:8642/internal/brew_failed",
                                      json={"entry_id": int(bid), "secret": tok},
                                      timeout=8)
                    except Exception:
                        pass
        time.sleep(POLL_S)


if __name__ == "__main__":
    main()
