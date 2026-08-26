#!/usr/bin/env python3
"""
Bujairami stock monitor for FragranceNet.com
============================================

Watches the FragranceNet Bujairami brand page and alerts you when:
  1. A product comes BACK IN STOCK (was "Sold Out!", now buyable), or
  2. A NEW Bujairami listing is ADDED to the site.

It does NOT run on Anthropic's servers. You run it on any always-on machine
(your PC, a Raspberry Pi, or a cheap VPS). It stores a snapshot in a JSON file
and compares each run against the previous one.

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
CONFIG = {
    "brand_url": "https://www.fragrancenet.com/fragrances/bujairami",
    "state_file": "bujairami_state.json",      # snapshot of last run
    "event_log": "bujairami_events.log",       # append-only log of alerts
    "health_file": "bujairami_health.json",    # tracks up/down so we alert once
    "health_reminder_hours": 6,                # re-ping if still down this long
    "request_delay_sec": 2.0,                  # polite pause between pages
    "check_interval_min": 1,                  # watch-mode loop interval
    "max_pages": 15,                           # safety cap on pagination

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
    text = resp.text
    # Cheap block detection: real pages contain product links / the brand name.
    low = text.lower()
    if "bujairami" not in low or "access denied" in low or "captcha" in low:
        raise RuntimeError("page did not look like a normal listing (possible block)")
    return text


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


def scrape_all(session):
    """Walk every listing page. Returns (products_dict, healthy_bool).
    healthy=False means the scrape was incomplete and state must NOT be saved."""
    all_products = {}
    healthy = True
    for page in range(1, CONFIG["max_pages"] + 1):
        try:
            html = fetch_page(session, page)
        except Exception as e:
            log(f"[warn] failed to fetch page {page}: {e}")
            healthy = False
            break

        page_products = parse_products(html)
        if not page_products:
            if page == 1:
                healthy = False  # page 1 empty = something is wrong
            break  # no products => end of listing

        before = len(all_products)
        _merge_products(all_products, page_products)
        # Some sites clamp an out-of-range page to the last page and repeat it.
        if len(all_products) == before and page > 1:
            break
        time.sleep(CONFIG["request_delay_sec"])

    return all_products, healthy


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
        with open(CONFIG["state_file"], "w", encoding="utf-8") as f:
            json.dump(products, f, indent=2)
    except Exception as e:
        log(f"[error] could not save state: {e}")


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


def send_discord(subject, body):
    cfg = CONFIG["discord"]
    if not cfg.get("webhook_url"):
        return
    try:
        content = f"**{subject}**\n{body}"[:1900]  # Discord 2000-char limit
        requests.post(cfg["webhook_url"], json={"content": content}, timeout=20)
        log("Discord alert sent.")
    except Exception as e:
        log(f"[error] discord failed: {e}")


def notify(subject, body):
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
        with open(CONFIG["health_file"], "w", encoding="utf-8") as f:
            json.dump(h, f, indent=2)
    except Exception as e:
        log(f"[warn] could not save health file: {e}")


def report_unhealthy(reason):
    """Record a failed/blocked/empty cycle. Ping Discord on the FIRST failure
    after a good run, then again only every health_reminder_hours if still down."""
    h = load_health()
    now = time.time()
    h["fails"] = h.get("fails", 0) + 1
    is_new_outage = h.get("status") != "down"
    reminder_due = (now - h.get("last_alert", 0)) >= CONFIG["health_reminder_hours"] * 3600
    if is_new_outage or reminder_due:
        
        log_event(f"HEALTH: monitor quiet -- {reason} (fail #{h['fails']})")
        h["last_alert"] = now
    h["status"] = "down"
    save_health(h)


def report_healthy(product_count):
    """A good cycle ran. If we were previously down, ping once that it's back."""
    h = load_health()
    if h.get("status") == "down":
        
        log_event(f"HEALTH: monitor recovered ({product_count} products)")
    save_health({"status": "ok", "fails": 0, "last_alert": 0})


# ==========================================================================
# Main run logic
# ==========================================================================
def run_once(session):
    old = load_state()
    new, healthy = scrape_all(session)

    if not healthy or not new:
        log("[warn] scrape incomplete or empty; leaving previous state untouched.")
        report_unhealthy("Scrape failed or returned no products "
                         "(possible IP block or a change to the site).")
        return

    # Guard against a partial scrape wiping/altering state and causing false alerts.
    if old and len(new) < 0.5 * len(old):
        log(f"[warn] found only {len(new)} products vs {len(old)} last time; "
            f"skipping this cycle to avoid false alerts.")
        report_unhealthy(f"Only found {len(new)} products vs {len(old)} last "
                         f"time -- looks like a partial or blocked fetch.")
        return

    # Scrape looks healthy -- clear any prior outage (and ping if we're recovering).
    report_healthy(len(new))

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

    in_stock = sum(1 for p in new.values() if p.get("in_stock"))

    if not old:
        save_state(new)
        sold = sorted(_display(p) for p in new.values() if not p.get("in_stock"))
        log(f"Baseline saved: {len(new)} products "
            f"({in_stock} in stock, {len(sold)} sold out). Monitoring for changes...")
        if sold:
            log("Currently sold out: " + ", ".join(sold))
        return

    new_added, back = diff_states(old, new)
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
        log(f"No changes. {len(new)} products, {in_stock} in stock.")

    save_state(new)


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
        notify("Bujairami monitor test",
               "If you can read this, your notifications are working.")
        return

    if args.once:
        run_once(session)
        return

    log(f"Watch mode: checking every {args.interval} min. Press Ctrl+C to stop.")
    while True:
        try:
            run_once(session)
        except KeyboardInterrupt:
            log("Stopped by user.")
            break
        except Exception as e:
            log(f"[error] unexpected: {e}")
        time.sleep(max(1, args.interval) * 60)


if __name__ == "__main__":
    main()
