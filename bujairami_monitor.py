#!/usr/bin/env python3
"""
Bujairami stock monitor for FragranceNet.com
============================================

Watches the FragranceNet Bujairami brand page and alerts you when:
  1. A product comes BACK IN STOCK (was "Sold Out!", now buyable), or
  2. A NEW Bujairami listing is ADDED to the site.

The main copy runs on my PC (Task Scheduler + watch mode). GitHub Actions runs
a best-effort backup copy. It stores a snapshot in a JSON file (in local_data/
on a PC, the repo root on Actions) and compares each run against the previous
one.

--------------------------------------------------------------------------
QUICK START
--------------------------------------------------------------------------
1. Install dependencies:
       pip install requests beautifulsoup4
   (Optional, only if the site starts blocking plain requests:)
       pip install cloudscraper

2. (Optional) Turn on alerts. Easiest is a Discord webhook OR email.
   Edit the CONFIG block below, or set environment variables. If you set
   nothing, it just prints alerts to the console / event log.

3. Run it:
       # Continuous watch mode (leave it running):
       python bujairami_monitor.py

       # Single check then exit (use this with cron / Task Scheduler):
       python bujairami_monitor.py --once

       # Send a test alert to your configured channels:
       python bujairami_monitor.py --test-notify

The FIRST run just records a baseline of everything currently listed and does
not spam you. Real alerts start on the next run when something changes.
--------------------------------------------------------------------------
"""

import argparse
import json
import os
import re
import shutil
import smtplib
import sys
import time
from datetime import datetime
from email.message import EmailMessage

import requests
from bs4 import BeautifulSoup

import deep_check

# ==========================================================================
# CONFIG  -- edit these, or set the matching environment variables
# ==========================================================================
# State files live in deep_check.data_dir(): local_data/ on a PC, the repo
# root on GitHub Actions (see deep_check.data_dir for why).
CONFIG = {
    "brand_url": "https://www.fragrancenet.com/fragrances/bujairami",
    "state_file": deep_check.data_path("bujairami_state.json"),    # snapshot of last run
    "event_log": deep_check.data_path("bujairami_events.log"),     # append-only log of alerts
    "health_file": deep_check.data_path("bujairami_health.json"),  # tracks up/down so we alert once
    # Re-ping if still down this long. 0 = never re-ping (one "went quiet",
    # then nothing until "recovered"). The GitHub copy sets 0 because it can be
    # blocked (403) for days.
    "health_reminder_hours": float(os.environ.get("HEALTH_REMINDER_HOURS", "6")),
    "request_delay_sec": 2.0,                  # polite pause between pages
    "check_interval_min": 1,                  # watch-mode loop interval
    "max_pages": 15,                           # safety cap on pagination

    # A product missing from one scrape is kept for this long before it's
    # dropped. Stops the "NEW LISTING" spam when a tile blinks out of the
    # listing for a cycle and comes back (Madness / Fairy Dust / Breezy).
    "missing_grace_hours": float(os.environ.get("MISSING_GRACE_HOURS", "48")),
    # A product has to read sold out this many runs in a row before it counts
    # as sold out. One-run blips no longer arm a fake BACK IN STOCK alert.
    "soldout_confirm_runs": int(os.environ.get("SOLDOUT_CONFIRM_RUNS", "2")),
    # Only ping Discord about an outage once it has lasted this long.
    # Half the outages in the log cleared on the very next run.
    "health_alert_after_min": float(os.environ.get("HEALTH_ALERT_AFTER_MIN", "60")),
    # FragranceNet's backend servers fall out of sync after a restock: one
    # page comes from the new catalog, the next from the old one. Each page
    # says how many products its server thinks exist ("numOfResults"), so a
    # page that disagrees with the highest total is stale and gets re-fetched
    # up to this many times. (Nov 2026: page 5 kept coming back as the old
    # 103-product version, so 12 new releases were never seen.)
    "stale_page_retries": int(os.environ.get("STALE_PAGE_RETRIES", "8")),
    # Don't send BACK IN STOCK for the same product again within this window.
    # Stops the Dubai Crush style repeat pings every 20 minutes. 0 = off.
    "back_alert_cooldown_min": float(os.environ.get("BACK_ALERT_COOLDOWN_MIN", "120")),
    # Stops two copies of the monitor on one PC from running at the same time
    # (watch mode + Task Scheduler) and stepping on each other's state file.
    "lock_file": deep_check.data_path("bujairami.lock"),
    "lock_stale_min": 15,

    # --- Email alerts (Gmail example). Use a Gmail "App Password", not your
    #     normal password: https://myaccount.google.com/apppasswords
    #     Activates automatically once a username AND password are set (here or
    #     via the BUJAIRAMI_SMTP_USER / BUJAIRAMI_SMTP_PASS env vars). ---
    "email": {
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 587,
        "username": os.environ.get("BUJAIRAMI_SMTP_USER", ""),
        "password": os.environ.get("BUJAIRAMI_SMTP_PASS", ""),  # App Password
        "from_addr": os.environ.get("BUJAIRAMI_SMTP_USER", ""),
        "to_addr": os.environ.get("BUJAIRAMI_ALERT_TO", ""),
    },

    # --- Discord alerts (easiest phone notification). Create a webhook:
    #     Server Settings > Integrations > Webhooks > New Webhook.
    #     Activates automatically once a webhook URL is set -- either paste it
    #     below, OR set the BUJAIRAMI_DISCORD_WEBHOOK env var once and never
    #     edit this file again. ---
    "discord": {
        "enabled": True,
        # Read from the BUJAIRAMI_DISCORD_WEBHOOK env var (a GitHub Actions
        # secret). Never hardcode the webhook in a file you push to a public
        # repo -- anyone could read it and spam your channel.
        "webhook_url": os.environ.get("BUJAIRAMI_DISCORD_WEBHOOK", ""),
    },
}

# The PC and GitHub copies post to the same Discord channel, so every alert
# says which one sent it.
RUNNER = os.environ.get("BUJAIRAMI_RUNNER", "") or \
    ("GitHub" if os.environ.get("GITHUB_ACTIONS") == "true" else "PC")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Each product tile links to a specific variant with a UNIQUE numeric id, e.g.:
#   /cologne/bujairami/bujairami-enforcer/eau-de-parfum#503948
#   group(1)=slug   group(2)=concentration   group(3)=id
# We track by that id so different concentrations of the same scent (which can
# have different stock) are NOT merged. The brand sidebar uses short slug-only
# links with no concentration/#id, so this regex naturally skips it. Sold-out
# variants carry a "Request it" link with the same #id.
PRODUCT_URL_RE = re.compile(
    r"/(?:cologne|perfume|fragrances)/bujairami/(bujairami-[a-z0-9.-]+)/([^#/?\s\"']+)#(\d+)",
    re.I,
)
COUNT_PREFIX_RE = re.compile(r"^\s*\(\d+\)\s*")  # strips a "(1) " badge if present


# ==========================================================================
# Logging
# ==========================================================================
def log(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def log_event(text):
    """Append alert text to the event log so cron users have a record."""
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(CONFIG["event_log"], "a", encoding="utf-8") as f:
            f.write(f"[{ts}] {text}\n")
    except Exception as e:
        log(f"[warn] could not write event log: {e}")


# ==========================================================================
# Fetching
# ==========================================================================
def make_session():
    """Prefer cloudscraper if installed (helps if the site adds bot checks),
    otherwise fall back to a plain requests session."""
    try:
        import cloudscraper
        session = cloudscraper.create_scraper()
        log("Using cloudscraper session.")
    except Exception:
        session = requests.Session()
    session.headers.update(HEADERS)
    return session


def fetch_page(session, page):
    url = CONFIG["brand_url"] if page == 1 else f'{CONFIG["brand_url"]}?page={page}'
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def looks_blocked(html):
    """Only consulted when a page has NO products. A normal page that happens to
    mention "captcha" somewhere (a reCAPTCHA script, a footer form) still has
    product tiles, so it should never be thrown out over that word."""
    low = html.lower()
    return "bujairami" not in low or "access denied" in low or "captcha" in low


# ==========================================================================
# Parsing
# ==========================================================================
def _clean_name(text):
    return COUNT_PREFIX_RE.sub("", text).strip()


def _name_from_slug(slug):
    # bujairami-date-night -> Bujairami Date Night (fallback only)
    return " ".join(w.capitalize() for w in slug.split("-"))


def _display(p):
    """Name plus concentration, so variants of one scent are distinguishable."""
    conc = p.get("concentration")
    return f"{p['name']} ({conc})" if conc else p["name"]


def _key(slug, concentration):
    # Stable identity: name + concentration. The numeric #id and the /fn/ URL
    # prefix both rotate between requests for the SAME product, so they can't
    # be used as the key or every check would look "new".
    # Normalize periods to hyphens so a slug like "bujairami-drip-no.7" and
    # "bujairami-drip-no-7" are treated as the same product (no double alerts).
    slug = slug.replace(".", "-")
    return f"{slug}|{concentration}"


def parse_products(html):
    """Return {slug|concentration: {...}} for a page. Keyed by name+concentration
    so the rotating #id / /fn/ URL variants of one product don't split apart."""
    soup = BeautifulSoup(html, "html.parser")
    products = {}
    soldout = {}  # key -> {slug, concentration, url} (from its "Request it" link)

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = PRODUCT_URL_RE.search(href)
        if not m:
            continue  # skips sidebar (no concentration/#id) and non-product links
        slug = m.group(1).lower()
        concentration = m.group(2).replace("-", " ").strip().lower()
        pid = m.group(3)
        key = _key(slug, concentration)
        # Normalize the URL: drop the rotating "/fn/" prefix and the #id fragment
        # so the stored link is stable and consistent.
        clean = re.sub(r"^/fn/", "/", href).split("#")[0]
        url = ("https://www.fragrancenet.com" + clean) if clean.startswith("/") else clean
        text = a.get_text(" ", strip=True)

        # Sold-out products carry a "Request it" link for the same product.
        if "request" in text.lower():
            soldout[key] = {"slug": slug, "concentration": concentration, "url": url}
            continue

        name = _clean_name(text)
        if not name.lower().startswith("bujairami"):
            continue  # image link / non-name link

        if key not in products:
            products[key] = {
                "id": pid, "slug": slug, "name": name,
                "concentration": concentration, "url": url, "in_stock": True,
            }

    # Apply sold-out status; build an entry if only its "Request it" link
    # was seen (name comes from the slug as a fallback).
    for key, info in soldout.items():
        if key in products:
            products[key]["in_stock"] = False
        else:
            products[key] = {
                "id": "", "slug": info["slug"],
                "name": _name_from_slug(info["slug"]),
                "concentration": info["concentration"],
                "url": info["url"], "in_stock": False,
            }

    return products


PAGINATION_RE = re.compile(
    r'"pagination"\s*:\s*\{[^}]*?"numOfResultsPerPage"\s*:\s*(\d+)'
    r'[^}]*?"numOfResults"\s*:\s*(\d+)'
)


def page_info(html):
    """(site_total, per_page) from the page's own pagination data, or None.
    The total is what THAT server thinks the brand has, which is how a stale
    page gives itself away."""
    m = PAGINATION_RE.search(html)
    if not m:
        return None
    per_page, total = int(m.group(1)), int(m.group(2))
    return (total, per_page) if per_page > 0 else None


def scrape_all(session, meta=None):
    """Walk every listing page. Returns (products_dict, healthy_bool, reason).
    healthy=False means the scrape was incomplete and state must NOT be saved.
    reason says what actually went wrong, so the log tells you something.
    If a dict is passed as meta, it's filled with site_total / stale_pages."""
    meta = {} if meta is None else meta
    try:
        html1 = fetch_page(session, 1)
    except Exception as e:
        log(f"[warn] failed to fetch page 1: {e}")
        return {}, False, f"page 1 fetch failed: {str(e)[:150]}"
    if page_info(html1) is None:
        log("[warn] no pagination data on page 1, using the old page walk.")
        return _scrape_walk(session, html1)
    return _scrape_consistent(session, html1, meta)


def _scrape_consistent(session, html1, meta):
    """Fetch pages 1..N, then re-fetch any page whose server reports a lower
    product total than the newest one seen, until every page agrees."""
    pages = {1: html1}
    totals = {1: page_info(html1)[0]}
    per_page = page_info(html1)[1]
    retries = max(0, CONFIG["stale_page_retries"])

    def n_pages(total):
        return max(1, min(CONFIG["max_pages"], -(-total // per_page)))

    def get(page):
        time.sleep(CONFIG["request_delay_sec"])
        html = fetch_page(session, page)
        info = page_info(html)
        return html, (info[0] if info else None)

    page = 2
    try:
        while page <= n_pages(max(totals.values())):
            pages[page], totals[page] = get(page)
            page += 1

        stale = []
        for attempt in range(retries + 1):
            target = max(t for t in totals.values() if t is not None)
            # A newer total can mean more pages than we fetched.
            for p in range(len(pages) + 1, n_pages(target) + 1):
                pages[p], totals[p] = get(p)
            stale = [p for p in range(1, n_pages(target) + 1) if totals[p] != target]
            if not stale or attempt == retries:
                break
            for p in stale:
                _drop_server_pin(session)
                pages[p], totals[p] = get(p)
    except Exception as e:
        log(f"[warn] failed to fetch page {page}: {e}")
        return {}, False, f"listing page fetch failed: {str(e)[:150]}"

    target = max(t for t in totals.values() if t is not None)
    all_products = {}
    for p in range(1, n_pages(target) + 1):
        page_products = parse_products(pages[p])
        if not page_products:
            if looks_blocked(pages[p]):
                return all_products, False, f"page {p} looked like a block/captcha page"
            if p == 1:
                return all_products, False, "page 1 loaded but had no products (site change?)"
        _merge_products(all_products, page_products)

    meta["site_total"] = target
    meta["stale_pages"] = stale
    if stale:
        log(f"[warn] page(s) {stale} still stale after {retries} retries "
            f"(site says {target}, got {len(all_products)}).")
    elif len(all_products) != target:
        log(f"[warn] site says {target} products but parsed {len(all_products)}.")
    return all_products, True, ""


def _drop_server_pin(session):
    """Cloudflare's load balancer sets __cflb to pin a session to one backend
    server. If that server is the stale one, every retry hits it again (8/8 in
    testing), which is why watch mode stayed at 103 even with retries. Dropping
    the cookie lets the next request land on a random server."""
    try:
        for c in list(session.cookies):
            if c.name == "__cflb":
                session.cookies.clear(c.domain, c.path, c.name)
    except Exception:
        pass


def _scrape_walk(session, html1):
    """Fallback if FragranceNet drops the pagination data: walk pages until
    one is empty or repeats."""
    all_products = {}
    for page in range(1, CONFIG["max_pages"] + 1):
        try:
            html = html1 if page == 1 else fetch_page(session, page)
        except Exception as e:
            log(f"[warn] failed to fetch page {page}: {e}")
            return all_products, False, f"page {page} fetch failed: {str(e)[:150]}"

        page_products = parse_products(html)
        if not page_products:
            if looks_blocked(html):
                return all_products, False, f"page {page} looked like a block/captcha page"
            if page == 1:
                return all_products, False, "page 1 loaded but had no products (site change?)"
            break  # no products => end of listing

        before = len(all_products)
        _merge_products(all_products, page_products)
        # Some sites clamp an out-of-range page to the last page and repeat it.
        if len(all_products) == before and page > 1:
            break
        time.sleep(CONFIG["request_delay_sec"])

    return all_products, True, ""


def _merge_products(all_products, page_products):
    """Merge one page's variants into the running set (keyed by variant id).
    If a variant is seen sold out on any page, keep it sold out."""
    for pid, prod in page_products.items():
        if pid in all_products:
            if not prod["in_stock"]:
                all_products[pid]["in_stock"] = False
        else:
            all_products[pid] = prod


# ==========================================================================
# State + diffing
# ==========================================================================
def load_state():
    path = CONFIG["state_file"]
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"[warn] could not read state file, starting fresh: {e}")
        return {}


def save_state(products):
    try:
        deep_check.write_json_atomic(CONFIG["state_file"], products)
    except Exception as e:
        log(f"[error] could not save state: {e}")


def reconcile(old, new, now=None):
    """Turn a raw scrape into the state we save and diff against.

    1. Missing products are carried forward for missing_grace_hours instead of
       being forgotten, so a tile that blinks out for a run doesn't come back
       as a "NEW LISTING".
    2. An in-stock product has to read sold out soldout_confirm_runs runs in a
       row before it's stored as sold out, so a one-run blip can't set up a
       fake "BACK IN STOCK" on the next run.
    """
    now = time.time() if now is None else now
    grace = CONFIG["missing_grace_hours"] * 3600
    confirm = max(1, CONFIG["soldout_confirm_runs"])
    final = {}

    for key, prod in new.items():
        prod = dict(prod)
        prev = old.get(key)
        if prev and prev.get("last_back_alert"):
            prod["last_back_alert"] = prev["last_back_alert"]
        # A product page "sold out" is trusted right away; only a listing-only
        # read has to repeat soldout_confirm_runs times.
        if prev and prev.get("in_stock") and not prod.get("in_stock") \
                and not prod.get("page_verified"):
            streak = prev.get("out_streak", 0) + 1
            if streak < confirm:
                prod["in_stock"] = True        # not confirmed sold out yet
                prod["out_streak"] = streak
        final[key] = prod

    for key, prev in old.items():
        if key in new:
            continue
        since = prev.get("missing_since") or now
        if now - since < grace:
            kept = dict(prev)
            kept["missing_since"] = since
            final[key] = kept
        else:
            log(f"Dropped from state (gone {CONFIG['missing_grace_hours']:.0f}h+): {_display(prev)}")

    return final


def diff_states(old, new):
    new_added, back_in_stock = [], []
    for pid, prod in new.items():
        if pid not in old:
            new_added.append(prod)
        elif old[pid].get("in_stock") is False and prod.get("in_stock") is True:
            back_in_stock.append(prod)
    return new_added, back_in_stock


# ==========================================================================
# Notifications
# ==========================================================================
def send_email(subject, body):
    cfg = CONFIG["email"]
    if not (cfg.get("username") and cfg.get("password") and cfg.get("to_addr")):
        return
    try:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = cfg["from_addr"]
        msg["To"] = cfg["to_addr"]
        msg.set_content(body)
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as s:
            s.starttls()
            s.login(cfg["username"], cfg["password"])
            s.send_message(msg)
        log("Email alert sent.")
    except Exception as e:
        log(f"[error] email failed: {e}")


def _chunks(text, limit=1900):
    """Split on line breaks so a big restock wave isn't cut off at 2000 chars."""
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:              # absurdly long single line
            out.append(line[:limit])
            line = line[limit:]
        if cur and len(cur) + 1 + len(line) > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out


def send_discord(subject, body):
    cfg = CONFIG["discord"]
    if not cfg.get("webhook_url"):
        return
    parts = _chunks(f"**{subject}**\n{body}")
    for i, part in enumerate(parts):
        for attempt in range(3):
            try:
                r = requests.post(cfg["webhook_url"], json={"content": part}, timeout=20)
            except Exception as e:
                log(f"[error] discord failed: {e}")
                time.sleep(2)
                continue
            if r.status_code == 429:          # rate limited, wait and retry
                try:
                    wait = float(r.json().get("retry_after", 2))
                except Exception:
                    wait = 2
                time.sleep(min(wait, 15))
                continue
            if r.status_code >= 400:
                log(f"[error] discord returned HTTP {r.status_code}: {r.text[:150]}")
            break
        if i + 1 < len(parts):
            time.sleep(1)
    log(f"Discord alert sent ({len(parts)} message(s)).")


def notify(subject, body):
    subject = f"{subject} [{RUNNER}]"
    log(f"ALERT: {subject}")
    print(body)
    log_event(subject + "\n" + body)
    send_email(subject, body)
    send_discord(subject, body)


# ==========================================================================
# Health alerts -- tells you when the monitor goes QUIET (blocked / empty /
# partial scrape) so silence never gets mistaken for "nothing in stock".
# Pings once when it goes down, re-pings every health_reminder_hours if it
# stays down, and pings once when it recovers. State lives in health_file.
# ==========================================================================
def load_health():
    path = CONFIG["health_file"]
    if not os.path.exists(path):
        return {"status": "ok", "fails": 0, "last_alert": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"status": "ok", "fails": 0, "last_alert": 0}


def save_health(h):
    try:
        deep_check.write_json_atomic(CONFIG["health_file"], h)
    except Exception as e:
        log(f"[warn] could not save health file: {e}")


def _fmt_minutes(m):
    return f"{m:.0f} min" if m < 90 else f"{m / 60:.1f} h"


def report_unhealthy(reason):
    """Record a failed/blocked/empty cycle. Discord only hears about it once the
    outage has lasted health_alert_after_min, then every health_reminder_hours
    (never again if that is 0)."""
    h = load_health()
    now = time.time()
    if h.get("status") != "down":
        h = {"status": "down", "fails": 0, "down_since": now,
             "alerted": False, "last_alert": 0}
    h["fails"] = h.get("fails", 0) + 1
    h["reason"] = reason
    down_min = (now - h.get("down_since", now)) / 60

    first_alert = not h.get("alerted") and down_min >= CONFIG["health_alert_after_min"]
    remind_after = CONFIG["health_reminder_hours"] * 3600
    reminder = remind_after > 0 and h.get("alerted") and \
        (now - h.get("last_alert", 0)) >= remind_after
    if first_alert or reminder:
        subject = "Bujairami monitor went quiet" if first_alert else \
                  "Bujairami monitor still down"
        body = (f"No good check for {_fmt_minutes(down_min)} "
                f"({h['fails']} failed runs).\nLatest: {reason}\n"
                "Silence right now does NOT mean nothing is in stock.")
        notify(subject, body)
        h["alerted"] = True
        h["last_alert"] = now
    save_health(h)


def report_healthy(product_count, site_total=None):
    """A good cycle ran. Ping 'recovered' only if we pinged 'quiet' earlier;
    short blips just get one line in the event log."""
    h = load_health()
    if h.get("status") == "down":
        down_min = (time.time() - h.get("down_since", time.time())) / 60
        if h.get("alerted"):
            notify("Bujairami monitor recovered",
                   f"Back after {_fmt_minutes(down_min)} ({product_count} products).")
        else:
            log_event(f"HEALTH: blip, {h.get('fails', 0)} failed run(s) over "
                      f"{_fmt_minutes(down_min)}, no alert sent. Last: {h.get('reason', '?')}")
    save_health({"status": "ok", "fails": 0, "last_alert": 0,
                 "products": product_count, "site_total": site_total,
                 "checked": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})


# ==========================================================================
# Main run logic
# ==========================================================================
def run_once(session):
    run_started = time.time()
    old = load_state()
    meta = {}
    new, healthy, reason = scrape_all(session, meta)

    if not healthy or not new:
        log(f"[warn] scrape incomplete or empty ({reason}); leaving state untouched.")
        report_unhealthy(reason or "scrape returned no products")
        return

    # Guard against a partial scrape wiping/altering state and causing false alerts.
    # Carried-forward (missing) entries don't count toward "last time".
    live_old = sum(1 for p in old.values() if not p.get("missing_since"))
    if live_old and len(new) < 0.5 * live_old:
        log(f"[warn] found only {len(new)} products vs {live_old} last time; "
            f"skipping this cycle to avoid false alerts.")
        report_unhealthy(f"Only found {len(new)} products vs {live_old} last "
                         f"time -- looks like a partial or blocked fetch.")
        return

    # Scrape looks healthy -- clear any prior outage (and ping if we're recovering).
    report_healthy(len(new), meta.get("site_total"))

    # The listing page can be stale: a product can read "sold out" there while
    # its own product page is purchasable. Verify sold-out items directly.
    try:
        restocked, blocked = deep_check.run_deep_checks(session, new, log=log)
        if blocked:
            log("[warn] deep check hit a block; skipped the rest of the sweep.")
        if restocked:
            log(f"Deep check: {len(restocked)} listing(s) were stale.")
    except Exception as e:
        log(f"[error] deep check failed, continuing without it: {e}")

    # The listing's servers also disagree about stock, so any product whose
    # stock looks changed (plus every watchlist item) is checked on its own
    # product page, which is consistent. Skipped on the very first baseline.
    if old:
        try:
            if deep_check.verify_changes(session, old, new, log=log, started=run_started):
                log("[warn] product page verify hit a block; rest use the listing.")
        except Exception as e:
            log(f"[error] product page verify failed, using the listing: {e}")

    in_stock =sum(1 for p in new.values() if p.get("in_stock"))

    if not old:
        save_state(new)
        sold = sorted(_display(p) for p in new.values() if not p.get("in_stock"))
        log(f"Baseline saved: {len(new)} products "
            f"({in_stock} in stock, {len(sold)} sold out). Monitoring for changes...")
        if sold:
            log("Currently sold out: " + ", ".join(sold))
        return

    final = reconcile(old, new)
    new_added, back = diff_states(old, final)
    back = _apply_back_cooldown(back)
    if meta.get("stale_pages"):
        log_event(f"SCRAPE: site says {meta['site_total']} products, got {len(new)}; "
                  f"page(s) {meta['stale_pages']} stayed stale after retries.")
    if new_added or back:
        lines = []
        if back:
            lines.append("BACK IN STOCK:")
            for p in back:
                tag = "  (listing still says sold out)" if p.get("deep_verified") else ""
                lines.append(f"  - {_display(p)}{tag}\n    {p['url']}")
        if new_added:
            lines.append("NEW LISTING:")
            for p in new_added:
                tag = "in stock" if p.get("in_stock") else "sold out"
                lines.append(f"  - {_display(p)} ({tag})\n    {p['url']}")
        subject = (f"Bujairami: {len(back)} back in stock, "
                   f"{len(new_added)} new listing(s)")
        notify(subject, "\n".join(lines))
    else:
        site = f" (site says {meta['site_total']})" if meta.get("site_total") else ""
        log(f"No changes. {len(new)} products{site}, {in_stock} in stock.")

    save_state(final)


def _apply_back_cooldown(back, now=None):
    """Drop BACK IN STOCK alerts for products we already pinged inside the
    cooldown window, and stamp the ones we do send."""
    now = time.time() if now is None else now
    cool = CONFIG["back_alert_cooldown_min"] * 60
    kept = []
    for p in back:
        last = p.get("last_back_alert", 0)
        if cool > 0 and now - last < cool:
            log(f"Skipped repeat BACK IN STOCK for {_display(p)} "
                f"(pinged {_fmt_minutes((now - last) / 60)} ago).")
            continue
        p["last_back_alert"] = now
        kept.append(p)
    return kept


# ==========================================================================
# Single-instance lock (local PC only; GitHub Actions has its own concurrency)
# ==========================================================================
def acquire_lock():
    path = CONFIG["lock_file"]
    try:
        if os.path.exists(path) and \
                time.time() - os.path.getmtime(path) > CONFIG["lock_stale_min"] * 60:
            os.remove(path)  # left behind by a crashed run
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception as e:
        log(f"[warn] lock problem, running anyway: {e}")
        return True


def release_lock():
    try:
        os.remove(CONFIG["lock_file"])
    except Exception:
        pass


def ensure_data_dir():
    """The lock file lives in the data dir, so it has to exist first."""
    base = deep_check.data_dir()
    if base:
        try:
            os.makedirs(base, exist_ok=True)
        except Exception as e:
            log(f"[warn] could not create data dir {base}: {e}")


# Older PC installs kept live state in the git-tracked root files. Copied
# (not moved, they're tracked) so the first run in local_data/ isn't a fresh
# baseline. The state file goes last: if the copy dies part way, there's still
# no state file in the data dir and the next run tries again.
MIGRATE_FILES = ("bujairami_deep.json", "bujairami_health.json",
                 "bujairami_events.log", "bujairami_state.json")


def migrate_root_state():
    """One-time copy of root-level state into the data dir. Never raises."""
    base = deep_check.data_dir()
    try:
        if not base or os.path.exists(CONFIG["state_file"]) \
                or not os.path.exists("bujairami_state.json"):
            return
        copied = []
        for name in MIGRATE_FILES:
            dst = os.path.join(base, name)
            # Never overwrite something the new location already has.
            if not os.path.exists(name) or os.path.exists(dst):
                continue
            # Copy to a temp name and swap in, so a killed copy can't leave a
            # half-written state file behind.
            shutil.copy2(name, dst + ".tmp")
            os.replace(dst + ".tmp", dst)
            copied.append(name)
        log(f"Copied old root-level state into {base}/: {', '.join(copied)}")
    except Exception as e:
        log(f"[warn] could not copy old state into {base}/, continuing: {e}")


def run_locked(session):
    ensure_data_dir()
    if not acquire_lock():
        log("Another copy of the monitor is mid-check; skipping this run.")
        return
    try:
        migrate_root_state()
        run_once(session)
    finally:
        release_lock()


def main():
    ap = argparse.ArgumentParser(description="Monitor Bujairami stock on FragranceNet.")
    ap.add_argument("--once", action="store_true",
                    help="Run a single check and exit (for cron / Task Scheduler).")
    ap.add_argument("--interval", type=int, default=CONFIG["check_interval_min"],
                    help="Minutes between checks in watch mode (default: %(default)s).")
    ap.add_argument("--test-notify", action="store_true",
                    help="Send a test alert to enabled channels and exit.")
    args = ap.parse_args()

    session = make_session()

    if args.test_notify:
        ensure_data_dir()  # the event log lives there
        notify("Bujairami monitor test",
               "If you can read this, your notifications are working.")
        return

    if args.once:
        run_locked(session)
        return

    log(f"Watch mode: checking every {args.interval} min. Press Ctrl+C to stop.")
    while True:
        try:
            run_locked(session)
        except KeyboardInterrupt:
            log("Stopped by user.")
            break
        except Exception as e:
            log(f"[error] unexpected: {e}")
        time.sleep(max(1, args.interval) * 60)


if __name__ == "__main__":
    main()
