#!/usr/bin/env python3
"""
deep_check.py - product page stock verification for bujairami_monitor.py

WHY THIS EXISTS
The brand listing page can be stale. A product can show a "Request it" (sold
out) link on the listing while its own product page is purchasable. The monitor
never fires because, as far as the listing is concerned, nothing changed. This
module opens the product page itself and reads the real stock state there.

HOW IT PLUGS IN
run_deep_checks() takes the products dict straight out of scrape_all() and
flips in_stock to True on anything the product page says is buyable. The
existing diff_states() then fires a normal BACK IN STOCK alert. No new alert
path needed.

Deep state lives in its own file (bujairami_deep.json) rather than inside
bujairami_state.json, because that state file IS the products dict and any
extra key in it would be picked up by diff_states() as a new product.

TARGETING
    watchlist : entries in bujairami_watchlist.json, checked every cycle
    rotate    : DEEP_BATCH other sold-out products per cycle, round robin
Default is both. Rotation resumes after the last key checked, so it survives
products being added, removed or reordered.

STICKINESS
Once a product page confirms in stock, that verdict is re-applied every cycle
until a later deep check sees it sold out. Without this you get a duplicate
alert when the listing finally catches up.

CLI
    python deep_check.py --probe "https://www.fragrancenet.com/..."
    python deep_check.py --probe-file saved.html
Probe mode prints every stock signal found plus the verdict, so you can confirm
detection against a listing you know is wrong before trusting alerts.
"""

import argparse
import json
import os
import random
import re
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
WATCHLIST_FILE = os.environ.get("DEEP_CHECK_WATCHLIST", "bujairami_watchlist.json")
DEEP_STATE_FILE = os.environ.get("DEEP_CHECK_STATE", "bujairami_deep.json")

IN_TOKENS = ("instock", "limitedavailability", "onlineonly", "instoreonly")
OUT_TOKENS = ("outofstock", "soldout", "discontinued", "preorder", "backorder")

CART_PAT = re.compile(r"add\s*to\s*(cart|bag)|addtocart|add-to-cart", re.I)
OUT_TEXT_PAT = re.compile(
    r"sold\s*out|out\s*of\s*stock|currently\s*unavailable|"
    r"temporarily\s*unavailable|notify\s*me\s*when|request\s*it",
    re.I,
)


# ---------------------------------------------------------------- session

def make_session():
    """Standalone session for probe mode. The monitor passes in its own."""
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
                       "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
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
    for tag in soup.find_all("script", attrs={"type": re.compile(r"ld\+json", re.I)}):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            _collect_availability(json.loads(raw), found)
        except Exception:
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
        style = (tag.get("style", "") or "").lower().replace(" ", "")
        disabled = (
            tag.has_attr("disabled")
            or "disabled" in classes
            or "hidden" in classes
            or "display:none" in style
        )
        label = (tag.get_text(" ", strip=True) or tag.get("value", "")
                 or tag.get("id", ""))[:60]
        (dead if disabled else live).append(label)
    return live, dead


def text_signals(soup):
    return OUT_TEXT_PAT.findall(soup.get_text(" ", strip=True))


def detect_stock(html):
    """
    Returns (state, reason).
      True  = confidently purchasable
      False = confidently not purchasable
      None  = cannot tell, caller must not act
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

    if text_signals(soup) or dead:
        return False, "no active cart control, out-of-stock wording present"

    return None, "no usable stock signal found"


def check_product(session, url):
    """Returns (state, reason, http_status)."""
    status, html, final = fetch(session, url)
    if status in (403, 429):
        return None, "blocked http %s" % status, status
    if status == 404:
        return None, "404, product page gone", status
    if status != 200:
        return None, "http %s (%s)" % (status, str(final)[:60]), status
    state, reason = detect_stock(html)
    return state, reason, status


# ---------------------------------------------------------------- state

def load_deep(path=DEEP_STATE_FILE):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("cursor", "")
        data.setdefault("items", {})
        return data
    except Exception:
        return {"cursor": "", "items": {}}


def save_deep(data, path=DEEP_STATE_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def load_watchlist(path=WATCHLIST_FILE):
    """
    bujairami_watchlist.json is either a list of strings or {"watch": [...]}.
    Entries match case-insensitively as substrings against the product key,
    slug, name and concentration.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, dict):
        data = data.get("watch") or data.get("watchlist") or []
    return [str(x).strip().lower() for x in data if str(x).strip()]


def on_watchlist(key, prod, patterns):
    if not patterns:
        return False
    blob = " ".join([
        key, prod.get("slug", ""), prod.get("name", ""), prod.get("concentration", ""),
    ]).lower()
    return any(p in blob for p in patterns)


# ---------------------------------------------------------------- targeting

def select_targets(sold_out_keys, products, patterns, cursor, batch, mode=DEEP_MODE):
    """
    sold_out_keys: keys the LISTING says are sold out, before stickiness.
    Returns (ordered list of keys to check, next_cursor).
    """
    watch = [k for k in sorted(sold_out_keys) if on_watchlist(k, products[k], patterns)]
    if mode == "watchlist":
        return watch, cursor
    if mode == "all":
        every = sorted(sold_out_keys)
        return every, (every[-1] if every else cursor)

    watch_set = set(watch)
    rest = sorted(k for k in sold_out_keys if k not in watch_set)
    if not rest:
        return watch, cursor

    start = 0
    if cursor:
        for i, k in enumerate(rest):
            if k > cursor:
                start = i
                break

    room = max(0, batch - len(watch)) if mode == "both" else batch
    picked = [rest[(start + i) % len(rest)] for i in range(min(room, len(rest)))]
    next_cursor = picked[-1] if picked else cursor
    return (watch + picked if mode == "both" else picked), next_cursor


# ---------------------------------------------------------------- main entry

def run_deep_checks(session, products, patterns=None, batch=DEEP_BATCH,
                    mode=DEEP_MODE, log=print):
    """
    Call this right after scrape_all() passes its health guards, and BEFORE
    diff_states().

    products : the dict from scrape_all(), key -> product. Mutated in place.
    Returns  : (restocked_products, blocked_bool)
    """
    patterns = load_watchlist() if patterns is None else patterns
    deep = load_deep()
    items = deep["items"]

    # Snapshot what the LISTING says before any stickiness is applied, or a
    # sticky in-stock product would never be re-checked and never decay.
    sold_out_keys = [k for k, p in products.items() if not p.get("in_stock")]
    if not sold_out_keys:
        save_deep(deep)
        return [], False

    targets, next_cursor = select_targets(sold_out_keys, products, patterns,
                                          deep.get("cursor", ""), batch, mode)

    restocked, blocked = [], False

    for i, key in enumerate(targets):
        prod = products[key]
        url = prod.get("url")
        label = prod.get("name") or key
        if not url:
            log(f"  deep: no url for {key}, skipping")
            continue

        if i:
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))

        state, reason, status = check_product(session, url)
        log(f"  deep: {label[:42]:<42} -> {str(state):<5} ({reason})")

        if status in (403, 429):
            blocked = True
            log("  deep: blocked, aborting the rest of the sweep")
            break

        if state is None:
            continue  # unknown, never act on it

        prev = items.get(key, {}).get("stock")
        items[key] = {"stock": "in" if state else "out", "checked": int(time.time())}
        if state and prev != "in":
            restocked.append(prod)

    # Anything a product page confirmed in stock stays flagged in stock until a
    # later deep check says otherwise. Prevents a duplicate alert when the
    # listing eventually catches up.
    for key in sold_out_keys:
        if items.get(key, {}).get("stock") == "in":
            products[key]["in_stock"] = True
            products[key]["deep_verified"] = True

    # Listing itself says in stock, so it is authoritative again. Drop the entry.
    for key, prod in products.items():
        if prod.get("in_stock") and not prod.get("deep_verified"):
            items.pop(key, None)

    deep["cursor"] = next_cursor
    save_deep(deep)
    return restocked, blocked


# ---------------------------------------------------------------- probe CLI

def probe(html, url=""):
    soup = BeautifulSoup(html, "html.parser")
    print("=" * 68)
    if url:
        print("url:", url)
    print("page length:", len(html))
    title = soup.find("title")
    print("title:", title.get_text(strip=True)[:90] if title else "(none)")
    print("-" * 68)
    print("json-ld availability:", jsonld_signals(soup) or "(none found)")
    live, dead = cart_signals(soup)
    print("active cart controls:", live or "(none)")
    print("disabled cart controls:", dead or "(none)")
    print("out-of-stock wording:", text_signals(soup) or "(none)")
    print("-" * 68)
    state, reason = detect_stock(html)
    print("VERDICT:", state, "|", reason)
    print("=" * 68)


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
                print("This IP is being blocked.")
            return
        probe(html, final)
        return

    ap.print_help()


if __name__ == "__main__":
    main()
