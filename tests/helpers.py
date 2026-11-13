"""Shared test setup. Import this module FIRST in every test file.

Safety is established here, before the bot modules are imported:
  * the Discord webhook and email credentials are blanked,
  * BUJAIRAMI_DATA_DIR points at a throwaway temp dir,
  * bytecode writing is off (no __pycache__ in the repo),
  * every real socket connection is blocked, so a stray network call fails loudly.
"""
import atexit
import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Environment that changes import-time defaults in the bot. Cleared so the suite
# behaves the same on a PC, on Actions and in a shell with leftovers.
CLEAR_ENV = (
    "BUJAIRAMI_RUNNER", "GITHUB_ACTIONS", "DEEP_CHECK_STATE", "HEALTH_REMINDER_HOURS",
    "DEEP_CHECK_BATCH", "DEEP_CHECK_MODE", "DEEP_CHECK_MIN_DELAY", "DEEP_CHECK_MAX_DELAY",
    "DEEP_CHECK_RETRIES", "DEEP_CHECK_RETRY_MIN", "DEEP_CHECK_RETRY_MAX",
    "DEEP_CHECK_BUDGET_SEC", "VERIFY_CACHE_MIN", "VERIFY_BUDGET_SEC",
    "MISSING_GRACE_HOURS", "SOLDOUT_CONFIRM_RUNS", "HEALTH_ALERT_AFTER_MIN",
    "STALE_PAGE_RETRIES", "BACK_ALERT_COOLDOWN_MIN", "BUJAIRAMI_ALERT_TO",
    "DEEP_CHECK_ROTATE_MIN", "DISCORD_RETRY_HOURS",
)
for _name in CLEAR_ENV:
    os.environ.pop(_name, None)

BOOT_DIR = tempfile.mkdtemp(prefix="bujairami_tests_boot_")
atexit.register(shutil.rmtree, BOOT_DIR, ignore_errors=True)

os.environ["BUJAIRAMI_DISCORD_WEBHOOK"] = ""
os.environ["BUJAIRAMI_SMTP_USER"] = ""
os.environ["BUJAIRAMI_SMTP_PASS"] = ""
os.environ["BUJAIRAMI_DATA_DIR"] = BOOT_DIR
os.environ["DEEP_CHECK_WATCHLIST"] = os.path.join(BOOT_DIR, "no_watchlist.json")


def _no_network(*args, **kwargs):
    raise AssertionError("network access attempted during a unit test")


socket.socket.connect = _no_network
socket.socket.connect_ex = _no_network
socket.create_connection = _no_network

if REPO not in sys.path:
    sys.path.insert(0, REPO)

import requests  # noqa: E402

import deep_check  # noqa: E402
import bujairami_monitor as bm  # noqa: E402

# Belt and braces: even if the environment had a webhook, it is gone now.
bm.CONFIG["discord"]["webhook_url"] = ""
bm.CONFIG["email"]["username"] = ""
bm.CONFIG["email"]["password"] = ""
bm.CONFIG["email"]["to_addr"] = ""
bm.CONFIG["request_delay_sec"] = 0
deep_check.MIN_DELAY = 0.0
deep_check.MAX_DELAY = 0.0
deep_check.RETRY_MIN = 0.0
deep_check.RETRY_MAX = 0.0

requests.post = _no_network
requests.get = _no_network
requests.Session.request = _no_network


# ------------------------------------------------------------------ html
BASE = "https://www.fragrancenet.com"


def tile(slug, conc="eau-de-parfum", pid=100, name=None, sold_out=False,
         prefix="/perfume/bujairami", fn=False):
    """One listing tile. A sold-out tile also carries a 'Request it' link."""
    if name is None:
        name = "Bujairami " + slug.replace("bujairami-", "").title()
    href = "%s%s/%s/%s#%s" % ("/fn" if fn else "", prefix, slug, conc, pid)
    html = '<div class="tile"><a href="%s">%s</a>' % (href, name)
    if sold_out:
        html += '<a href="%s">Request it</a>' % href
    return html + "</div>"


def pagination(total, per_page=24, page=1):
    pages = max(1, -(-total // per_page))
    return ('"pagination":{"numOfResultsPerPage":%d,"numOfResults":%d,'
            '"numOfPages":%d,"currentPageNum":%d}' % (per_page, total, pages, page))


def listing(tiles, total=None, per_page=24, page=1, extra=""):
    """A listing page. total=None means 'no pagination blob at all'."""
    blob = "" if total is None else "<script>var d={%s};</script>" % pagination(total, per_page, page)
    sidebar = '<ul><li><a href="/perfume/bujairami/bujairami-sidebar">Bujairami Sidebar</a></li></ul>'
    return ("<html><head><title>Bujairami Fragrances</title></head><body>%s%s%s%s</body></html>"
            % (sidebar, "".join(tiles), blob, extra))


def make_catalog(n, sold_out=()):
    """n products as (slug, conc, pid, sold_out) tuples."""
    return [("bujairami-p%03d" % i, "eau-de-parfum", 1000 + i, i in sold_out) for i in range(n)]


def serve(catalog, page, per_page=24, total=None):
    """The listing page a server holding `catalog` returns for `page`."""
    total = len(catalog) if total is None else total
    chunk = catalog[(page - 1) * per_page: page * per_page]
    tiles = [tile(s, c, pid, sold_out=so) for (s, c, pid, so) in chunk]
    return listing(tiles, total=total, per_page=per_page, page=page)


def product_page(kind, pad=True):
    """A product page. kind: in, out, unknown, nd_in, nd_out, cart, soldwording."""
    head = "<html><head><title>Bujairami Foo - FragranceNet</title>"
    body = "<body><h1>Bujairami Foo</h1>"
    if kind in ("in", "out"):
        av = "https://schema.org/InStock" if kind == "in" else "https://schema.org/OutOfStock"
        head += ('<script type="application/ld+json">{"@type":"Product","offers":'
                 '{"@type":"Offer","availability":"%s"}}</script>' % av)
    elif kind in ("nd_in", "nd_out"):
        flags = [True, False] if kind == "nd_in" else [True, True]
        sizes = ",".join('{"isOutOfStock":%s}' % str(f).lower() for f in flags)
        head += ('<script id="__NEXT_DATA__" type="application/json">{"props":{"pageProps":'
                 '{"pageData":{"skuList":[{"SIZE":[%s]}]}}}}</script>' % sizes)
    elif kind == "cart":
        body += '<button class="btn add-to-cart">Add to Cart</button>'
    elif kind == "soldwording":
        body += '<button class="btn add-to-cart disabled">Add to Cart</button><p>Sold out</p>'
    html = head + "</head>" + body + "</body></html>"
    if pad:
        html += "<!-- " + "x" * 600 + " -->"
    return html


# ------------------------------------------------------------------ fakes
class FakeResp:
    def __init__(self, text="", status=200, url=""):
        self.text = text
        self.status_code = status
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError("HTTP %d" % self.status_code)

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """routes: url -> FakeResp | str | list of those (consumed in order, the last
    one repeats) | callable(url, n) | Exception instance (raised).
    Unrouted URLs get a 500 and are recorded in .misses."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []
        self.misses = []
        self.counts = {}
        self.cookies = requests.cookies.RequestsCookieJar()
        self.headers = {}

    def get(self, url, timeout=None, headers=None, **kw):
        self.calls.append(url)
        n = self.counts.get(url, 0)
        self.counts[url] = n + 1
        if url not in self.routes:
            self.misses.append(url)
            return FakeResp("", 500, url)
        r = self.routes[url]
        if callable(r):
            r = r(url, n)
        elif isinstance(r, list):
            r = r[min(n, len(r) - 1)]
        if isinstance(r, Exception):
            raise r
        if isinstance(r, str):
            r = FakeResp(r, 200, url)
        if not r.url:
            r.url = url
        return r


def listing_url(page):
    base = bm.CONFIG["brand_url"]
    return base if page == 1 else "%s?page=%d" % (base, page)


def listing_session(pages):
    """pages: {page_number: html | list of html}."""
    return FakeSession({listing_url(p): v for p, v in pages.items()})


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ------------------------------------------------------------------ base test
class Base(unittest.TestCase):
    """Per-test temp dir, cwd inside it, CONFIG paths redirected, stdout captured,
    time.sleep neutralised, Discord/email proven to be off."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="bujairami_test_")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.data = os.path.join(self.tmp, "data")
        os.makedirs(self.data)
        old_cwd = os.getcwd()
        os.chdir(self.tmp)
        self.addCleanup(os.chdir, old_cwd)

        self.out = io.StringIO()
        p = mock.patch("sys.stdout", self.out)
        p.start()
        self.addCleanup(p.stop)

        # Never really sleep.
        self.sleeps = []
        p = mock.patch("time.sleep", side_effect=lambda s: self.sleeps.append(s))
        p.start()
        self.addCleanup(p.stop)

        env = mock.patch.dict(os.environ, {"BUJAIRAMI_DATA_DIR": self.data,
                                           "BUJAIRAMI_DISCORD_WEBHOOK": ""})
        env.start()
        self.addCleanup(env.stop)

        cfg = mock.patch.dict(bm.CONFIG, {
            "state_file": os.path.join(self.data, "bujairami_state.json"),
            "event_log": os.path.join(self.data, "bujairami_events.log"),
            "health_file": os.path.join(self.data, "bujairami_health.json"),
            "lock_file": os.path.join(self.data, "bujairami.lock"),
            "pending_file": os.path.join(self.data, "bujairami_pending.json"),
            "discord_retry_hours": 24.0,
            "pending_max": 50,
            "request_delay_sec": 0,
            "stale_page_retries": 8,
            "max_pages": 15,
            "missing_grace_hours": 48.0,
            "soldout_confirm_runs": 2,
            "health_alert_after_min": 60.0,
            "health_reminder_hours": 6.0,
            "back_alert_cooldown_min": 120.0,
            "lock_stale_min": 15,
        })
        cfg.start()
        self.addCleanup(cfg.stop)
        for sub in ("discord", "email"):
            d = mock.patch.dict(bm.CONFIG[sub])
            d.start()
            self.addCleanup(d.stop)
        bm.CONFIG["discord"]["webhook_url"] = ""
        bm.CONFIG["email"].update(username="", password="", to_addr="")

        # load_deep()/save_deep() bind the deep state path as a default argument
        # at import, so rebind it to this test's temp dir.
        self.deep_path = os.path.join(self.data, "bujairami_deep.json")
        for fn in (deep_check.load_deep, deep_check.save_deep):
            old = fn.__defaults__
            fn.__defaults__ = (self.deep_path,)
            self.addCleanup(setattr, fn, "__defaults__", old)

        p = mock.patch.object(deep_check, "load_watchlist", return_value=[])
        p.start()
        self.addCleanup(p.stop)

        self.assertEqual(bm.CONFIG["discord"]["webhook_url"], "")

    def output(self):
        return self.out.getvalue()

    def read_json(self, path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    def read(self, path):
        with open(path, "r", encoding="utf-8", newline="") as f:
            return f.read()

    def entry(self, slug="bujairami-foo", conc="eau de parfum", in_stock=True, **extra):
        e = {"id": "1", "slug": slug, "name": "Bujairami " + slug.split("-", 1)[-1].title(),
             "concentration": conc,
             "url": "%s/perfume/bujairami/%s/%s" % (BASE, slug, conc.replace(" ", "-")),
             "in_stock": in_stock}
        e.update(extra)
        return e

    def key(self, slug="bujairami-foo", conc="eau de parfum"):
        return bm._key(slug, conc)
