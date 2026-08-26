#!/usr/bin/env python3
"""
deep_check.py - product-page stock verification for the Bujairami monitor.

WHY THIS EXISTS
The brand listing page can be stale. A product can show "sold out" on the
listing while the actual product page is purchasable. The monitor never fires
because, as far as the listing is concerned, nothing changed. This module goes
one level deeper: it opens the product page itself and reads the real stock
state there.

WHAT IT DOES
- Verifies sold-out products by fetching their product detail page (PDP).
- Two target modes that stack:
    watchlist : always check the products you name in bujairami_watchlist.json
    rotate    : check DEEP_BATCH other sold-out products per cycle, round robin
- Returns a confident True / False / None. None means "could not tell", and the
  caller must leave state alone, same philosophy as the existing safety guards.
- Stops the sweep immediately on 403 / 429 so a block does not turn into 60
  more blocked requests.

IMPORTANT: pass in products from the CURRENT scrape, not from the state file.
The numeric #id and the /fn/ prefix rotate between requests, so a URL saved on
a previous run can be dead. Fresh scrape, fresh URLs.

CLI
    python deep_check.py --probe "https://www.fragrancenet.com/..."   # live page
    python deep_check.py --probe-file saved.html                      # saved HTML
Probe mode prints every stock signal it finds and the verdict, so you can
confirm detection against a listing you know is wrong before trusting alerts.
"""

import argparse
import json
import os
import random
import re
import sys
import time

import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:
    cloudscraper = None


# ---------------------------------------------------------------- settings

DEEP_BATCH = int(os.environ.get("DEEP_CHECK_BATCH", "8"))
DEEP_MODE = os.environ.get("DEEP_CHECK_MODE", "both").lower()  # watchlist|rotate|both|all
MIN_DELAY = float(os.environ.get("DEEP_CHECK_MIN_DELAY", "1.5"))
MAX_DELAY = float(os.environ.get("DEEP_CHECK_MAX_DELAY", "3.5"))
TIMEOUT = int(os.environ.get("DEEP_CHECK_TIMEOUT", "25"))
WATCHLIST_PATH = os.environ.get("DEEP_CHECK_WATCHLIST", "bujairami_watchlist.json")

IN_TOKENS = ("instock", "limitedavailability", "onlineonly", "instoreonly")
OUT_TOKENS = ("outofstock", "soldout", "discontinued", "preorder", "backorder")

CART_PAT = re.compile(r"add\s*to\s*(cart|bag)|addtocart|add-to-cart", re.I)
OUT_TEXT_PAT = re.compile(
    r"sold\s*out|out\s*of\s*stock|currently\s*unavailable|"
    r"temporarily\s*unavailable|notify\s*me\s*when",
    re.I,
)


# ---------------------------------------------------------------- session

def make_session():
    """Standalone session. If the main script already has one, pass that in instead."""
    if cloudscraper is not None:
        try:
            return cloudscraper.create_scraper(
                browser={"browser": "chrome", "platform": "windows", "mobile": False}
            )
        except Exception:
            pass
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
        "Accept-Language": "en-US,en;q=0.9",
    })
    return s


def fetch(session, url):
    """Returns (status_code, html, final_url). status 0 means the request blew up."""
    try:
        r = session.get(url, timeout=TIMEOUT)
        return r.status_code, r.text, r.url
    except Exception as e:
        return 0, "", str(e)


# ---------------------------------------------------------------- detection

def _norm(value):
    return re.sub(r"[^a-z]", "", str(value).lower())


def _collect_availability(node, found):
    if isinstance(node, dict):
        for k, v in node.items():
            if str(k).lower() == "availability" and isinstance(v, str):
                found.append(v)
            else:
                _collect_availability(v, found)
    elif isinstance(node, list):
        for item in node:
            _collect_availability(item, found)


def jsonld_signals(soup):
    """Every schema.org availability value on the page, as raw strings."""
    found = []
    for tag in soup.find_all("script", attrs={"type": re.compile("ld\\+json", re.I)}):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            _collect_availability(json.loads(raw), found)
        except Exception:
            # some sites emit slightly broken JSON-LD, fall back to a regex sweep
            found.extend(re.findall(r'"availability"\s*:\s*"([^"]+)"', raw))
    return found


def cart_signals(soup):
    """Add-to-cart controls that are present and not visibly disabled."""
    live, dead = [], []
    for tag in soup.find_all(["button", "input", "a"]):
        blob = " ".join([
            tag.get_text(" ", strip=True) or "",
            tag.get("value", "") or "",
            tag.get("id", "") or "",
            tag.get("name", "") or "",
            " ".join(tag.get("class", []) or []),
            tag.get("data-action", "") or "",
        ])
        if not CART_PAT.search(blob):
            continue
        classes = " ".join(tag.get("class", []) or []).lower()
        style = (tag.get("style", "") or "").lower()
        disabled = (
            tag.has_attr("disabled")
            or "disabled" in classes
            or "hidden" in classes
            or "display:none" in style.replace(" ", "")
        )
        label = (tag.get_text(" ", strip=True) or tag.get("value", "") or tag.get("id", ""))[:60]
        (dead if disabled else live).append(label)
    return live, dead


def text_signals(soup):
    text = soup.get_text(" ", strip=True)
    return OUT_TEXT_PAT.findall(text)


def detect_stock(html):
    """
    Returns (state, reason).
      state True  = confidently purchasable
      state False = confidently not purchasable
      state None  = cannot tell, caller must not act
    """
    if not html or len(html) < 500:
        return None, "page too short to trust"

    soup = BeautifulSoup(html, "html.parser")

    avail = jsonld_signals(soup)
    if avail:
        norms = [_norm(a) for a in avail]
        if any(any(t in n for t in IN_TOKENS) for n in norms):
            return True, "json-ld availability: %s" % ", ".join(sorted(set(avail)))
        if any(any(t in n for t in OUT_TOKENS) for n in norms):
            return False, "json-ld availability: %s" % ", ".join(sorted(set(avail)))

    live, dead = cart_signals(soup)
    if live:
        return True, "active add-to-cart control (%s)" % live[0]

    outs = text_signals(soup)
    if outs or dead:
        return False, "no active cart control, out-of-stock wording present"

    return None, "no usable stock signal found"


def check_product(session, url):
    status, html, final = fetch(session, url)
    if status in (403, 429):
        return None, "blocked http %s" % status, status
    if status == 404:
        return None, "404, url likely rotated", status
    if status != 200:
        return None, "http %s" % status, status
    state, reason = detect_stock(html)
    return state, reason, status


# ---------------------------------------------------------------- targeting

def _get(prod, *names):
    for n in names:
        v = prod.get(n)
        if v:
            return v
    return ""


def load_watchlist(path=WATCHLIST_PATH):
    """
    bujairami_watchlist.json can be either:
        ["bujairami-drip-no.7", "oud", "bujairami-noir|eau de parfum"]
    or:
        {"watch": ["bujairami-drip-no.7", "oud"]}
    Entries are matched case-insensitively as substrings against key, slug and name.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("watch") or data.get("watchlist") or []
    return [str(x).strip().lower() for x in data if str(x).strip()]


def on_watchlist(prod, patterns):
    if not patterns:
        return False
    blob = " ".join([
        _get(prod, "key"), _get(prod, "slug"), _get(prod, "name", "title"),
        _get(prod, "concentration"),
    ]).lower()
    return any(p in blob for p in patterns)


def select_targets(products, patterns, cursor, batch, mode=DEEP_MODE):
    """
    products: list of dicts from the current scrape. Needs key, url and in_stock.
    Returns (targets, next_cursor).

    Rotation resumes after the last key checked rather than an index, so it
    survives products being added, removed or reordered between runs.
    """
    sold_out = [p for p in products if not p.get("in_stock")]

    watch = [p for p in sold_out if on_watchlist(p, patterns)]
    if mode == "watchlist":
        return watch, cursor
    if mode == "all":
        return sold_out, (sorted(_get(p, "key") for p in sold_out)[-1:] or [cursor])[0]

    watch_keys = {_get(p, "key") for p in watch}
    rest = sorted([p for p in sold_out if _get(p, "key") not in watch_keys],
                  key=lambda p: _get(p, "key"))
    if not rest:
        return watch, cursor

    start = 0
    if cursor:
        for i, p in enumerate(rest):
            if _get(p, "key") > cursor:
                start = i
                break
        else:
            start = 0

    room = max(0, batch - len(watch)) if mode == "both" else batch
    picked = [rest[(start + i) % len(rest)] for i in range(min(room, len(rest)))]
    next_cursor = _get(picked[-1], "key") if picked else cursor
    return (watch + picked if mode == "both" else picked), next_cursor


# ---------------------------------------------------------------- main entry

def run_deep_checks(session, products, state, patterns=None, batch=DEEP_BATCH,
                    mode=DEEP_MODE, log=print):
    """
    Call this right after a successful listing scrape.

    products : list of dicts from THIS scrape (key, url, name, in_stock)
    state    : the loaded bujairami_state.json dict, modified in place

    Returns (restocked, blocked) where restocked is the list of products whose
    product page says purchasable even though the listing said sold out.
    """
    patterns = load_watchlist() if patterns is None else patterns
    deep = state.setdefault("_deep", {"cursor": "", "items": {}})
    items = deep.setdefault("items", {})

    targets, next_cursor = select_targets(products, patterns, deep.get("cursor", ""),
                                          batch, mode)
    if not targets:
        return [], False

    restocked = []
    blocked = False

    for i, prod in enumerate(targets):
        key = _get(prod, "key")
        url = _get(prod, "url", "link", "product_url", "href")
        name = _get(prod, "name", "title") or key
        if not url:
            log("deep: no url for %s, skipping" % key)
            continue

        if i:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        result, reason, status = check_product(session, url)
        log("deep: %-45s -> %-9s (%s)" % (name[:45], str(result), reason))

        if status in (403, 429):
            blocked = True
            log("deep: blocked, aborting sweep early")
            break

        if result is None:
            continue  # unknown, never act on it

        prev = items.get(key, {}).get("stock")
        items[key] = {"stock": "in" if result else "out", "checked": int(time.time())}

        if result and prev != "in":
            restocked.append(prod)
            # Mark it in stock in the main state too, so when the listing finally
            # catches up the normal logic does not fire a duplicate alert.
            entry = state.get(key)
            if isinstance(entry, dict):
                entry["in_stock"] = True

    deep["cursor"] = next_cursor
    return restocked, blocked


# ---------------------------------------------------------------- probe CLI

def probe(html, url=""):
    soup = BeautifulSoup(html, "html.parser")
    print("=" * 66)
    if url:
        print("url:", url)
    print("page length:", len(html))
    print("-" * 66)

    avail = jsonld_signals(soup)
    print("json-ld availability values:", avail or "(none found)")

    live, dead = cart_signals(soup)
    print("active cart controls:", live or "(none)")
    print("disabled cart controls:", dead or "(none)")

    print("out-of-stock wording hits:", text_signals(soup) or "(none)")

    title = soup.find("title")
    print("title:", title.get_text(strip=True)[:90] if title else "(none)")

    print("-" * 66)
    state, reason = detect_stock(html)
    print("VERDICT:", state, "|", reason)
    print("=" * 66)


def main():
    ap = argparse.ArgumentParser(description="Probe a product page for stock signals.")
    ap.add_argument("--probe", metavar="URL", help="fetch a live product page")
    ap.add_argument("--probe-file", metavar="PATH", help="read a saved HTML file")
    args = ap.parse_args()

    if args.probe_file:
        with open(args.probe_file, "r", encoding="utf-8", errors="replace") as f:
            probe(f.read(), args.probe_file)
        return

    if args.probe:
        session = make_session()
        status, html, final = fetch(session, args.probe)
        print("http status:", status)
        if status != 200:
            print("final/error:", final)
            if status in (403, 429):
                print("This IP is being blocked. Run it from the GitHub Actions runner.")
            return
        probe(html, final)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
