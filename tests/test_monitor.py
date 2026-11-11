"""Tests for bujairami_monitor.py. No network, no Discord, nothing outside temp dirs."""
import helpers  # noqa: F401  (must be first: sets up the safe environment)
from helpers import (BASE, Base, Clock, FakeResp, FakeSession, bm, deep_check,
                     listing, listing_session, listing_url, make_catalog, pagination,
                     product_page, requests, serve, tile)

import json
import os
import time
import unittest
from unittest import mock

T0 = 1_700_000_000.0
HOUR = 3600.0
MIN = 60.0


# ======================================================================
# parse_products / page_info
# ======================================================================
class ParseProductsTests(Base):
    def test_in_stock_tile(self):
        prods = bm.parse_products(listing([tile("bujairami-foo", pid=123)]))
        self.assertEqual(list(prods), ["bujairami-foo|eau de parfum"])
        p = prods["bujairami-foo|eau de parfum"]
        self.assertTrue(p["in_stock"])
        self.assertEqual(p["id"], "123")
        self.assertEqual(p["name"], "Bujairami Foo")
        self.assertEqual(p["concentration"], "eau de parfum")
        self.assertEqual(p["url"], BASE + "/perfume/bujairami/bujairami-foo/eau-de-parfum")

    def test_sold_out_via_request_it_link(self):
        prods = bm.parse_products(listing([tile("bujairami-foo", sold_out=True)]))
        p = prods["bujairami-foo|eau de parfum"]
        self.assertFalse(p["in_stock"])
        self.assertEqual(p["name"], "Bujairami Foo")  # name link still wins

    def test_request_it_is_case_insensitive(self):
        html = listing(['<a href="/perfume/bujairami/bujairami-foo/eau-de-parfum#5">Bujairami Foo</a>'
                        '<a href="/perfume/bujairami/bujairami-foo/eau-de-parfum#5">REQUEST IT</a>'])
        self.assertFalse(bm.parse_products(html)["bujairami-foo|eau de parfum"]["in_stock"])

    def test_sold_out_only_link_builds_an_entry_from_the_slug(self):
        html = listing(['<a href="/perfume/bujairami/bujairami-date-night/eau-de-parfum#9">Request it</a>'])
        prods = bm.parse_products(html)
        p = prods["bujairami-date-night|eau de parfum"]
        self.assertFalse(p["in_stock"])
        self.assertEqual(p["name"], "Bujairami Date Night")
        self.assertEqual(p["id"], "")
        self.assertEqual(p["url"], BASE + "/perfume/bujairami/bujairami-date-night/eau-de-parfum")

    def test_slug_with_period_is_normalised_in_the_key_only(self):
        prods = bm.parse_products(listing([tile("bujairami-drip-no.7", name="Bujairami Drip No.7")]))
        self.assertEqual(list(prods), ["bujairami-drip-no-7|eau de parfum"])
        self.assertEqual(prods["bujairami-drip-no-7|eau de parfum"]["slug"], "bujairami-drip-no.7")

    def test_period_and_hyphen_slug_are_the_same_product(self):
        html = listing([tile("bujairami-drip-no.7", pid=1, name="Bujairami Drip No.7"),
                        tile("bujairami-drip-no-7", pid=2, name="Bujairami Drip No 7")])
        self.assertEqual(len(bm.parse_products(html)), 1)

    def test_fn_prefix_is_stripped_and_does_not_split_the_product(self):
        html = listing([tile("bujairami-foo", pid=1, fn=True), tile("bujairami-foo", pid=2)])
        prods = bm.parse_products(html)
        self.assertEqual(len(prods), 1)
        self.assertEqual(prods["bujairami-foo|eau de parfum"]["url"],
                         BASE + "/perfume/bujairami/bujairami-foo/eau-de-parfum")

    def test_sidebar_and_non_product_links_are_ignored(self):
        html = listing([], extra='<a href="/fragrances/bujairami">All</a>'
                                 '<a href="/perfume/other-brand/other-foo/eau-de-parfum#4">Other Foo</a>')
        self.assertEqual(bm.parse_products(html), {})

    def test_image_only_link_does_not_create_an_entry(self):
        html = listing(['<a href="/perfume/bujairami/bujairami-foo/eau-de-parfum#1"><img src="x.jpg"></a>'])
        self.assertEqual(bm.parse_products(html), {})

    def test_concentrations_are_separate_products(self):
        html = listing([tile("bujairami-foo", "eau-de-parfum", 1),
                        tile("bujairami-foo", "extrait-de-parfum", 2, sold_out=True)])
        prods = bm.parse_products(html)
        self.assertEqual(set(prods), {"bujairami-foo|eau de parfum", "bujairami-foo|extrait de parfum"})
        self.assertTrue(prods["bujairami-foo|eau de parfum"]["in_stock"])
        self.assertFalse(prods["bujairami-foo|extrait de parfum"]["in_stock"])

    def test_count_badge_is_stripped_from_the_name(self):
        prods = bm.parse_products(listing([tile("bujairami-foo", name="(3) Bujairami Foo")]))
        self.assertEqual(prods["bujairami-foo|eau de parfum"]["name"], "Bujairami Foo")

    def test_cologne_url_prefix_is_recognised(self):
        prods = bm.parse_products(listing([tile("bujairami-foo", prefix="/cologne/bujairami")]))
        self.assertEqual(len(prods), 1)


class PageInfoTests(Base):
    def test_reads_total_and_per_page(self):
        html = "<script>var x={%s};</script>" % pagination(115, 24, 1)
        self.assertEqual(bm.page_info(html), (115, 24))

    def test_no_pagination_blob_returns_none(self):
        self.assertIsNone(bm.page_info("<html>nothing here</html>"))

    def test_zero_per_page_returns_none(self):
        html = '"pagination":{"numOfResultsPerPage":0,"numOfResults":10}'
        self.assertIsNone(bm.page_info(html))


# ======================================================================
# scrape_all
# ======================================================================
class ScrapeAllTests(Base):
    def test_clean_run_fetches_exactly_the_needed_pages(self):
        cat = make_catalog(50, sold_out=(3, 7))
        s = listing_session({p: serve(cat, p) for p in (1, 2, 3)})
        meta = {}
        prods, healthy, reason = bm.scrape_all(s, meta)
        self.assertTrue(healthy, reason)
        self.assertEqual(s.calls, [listing_url(1), listing_url(2), listing_url(3)])
        self.assertEqual(len(prods), 50)
        self.assertEqual(sum(1 for p in prods.values() if not p["in_stock"]), 2)
        self.assertEqual(meta, {"site_total": 50, "stale_pages": []})
        self.assertEqual(s.misses, [])

    def test_stale_page_is_healed_and_server_pin_dropped_before_each_retry(self):
        new = make_catalog(12)
        old = new[:9]
        s = listing_session({
            1: serve(new, 1, 5), 2: serve(new, 2, 5),
            3: [serve(old, 3, 5), serve(old, 3, 5), serve(new, 3, 5)],
        })
        s.cookies.set("__cflb", "abc", domain="www.fragrancenet.com", path="/")
        s.cookies.set("other", "keep", domain="www.fragrancenet.com", path="/")
        meta = {}
        with mock.patch.object(bm, "_drop_server_pin", wraps=bm._drop_server_pin) as drop:
            prods, healthy, reason = bm.scrape_all(s, meta)
        self.assertTrue(healthy, reason)
        self.assertEqual(len(prods), 12)
        self.assertEqual(s.counts[listing_url(3)], 3)
        self.assertEqual(drop.call_count, 2)
        self.assertEqual(meta["stale_pages"], [])
        self.assertEqual(meta["site_total"], 12)
        self.assertEqual({c.name for c in s.cookies}, {"other"})

    def test_page_that_stays_stale_is_reported_and_scrape_stays_healthy(self):
        bm.CONFIG["stale_page_retries"] = 2
        new = make_catalog(12)
        old = new[:9]
        s = listing_session({1: serve(new, 1, 5), 2: serve(new, 2, 5), 3: serve(old, 3, 5)})
        meta = {}
        prods, healthy, reason = bm.scrape_all(s, meta)
        self.assertTrue(healthy, reason)
        self.assertEqual(reason, "")
        self.assertEqual(meta["stale_pages"], [3])
        self.assertEqual(meta["site_total"], 12)
        self.assertEqual(s.counts[listing_url(3)], 3)   # first fetch + 2 retries
        self.assertEqual(len(prods), 10)
        self.assertIn("still stale", self.output())

    def test_newer_total_on_a_later_page_pulls_in_the_extra_page(self):
        new = make_catalog(12)
        old = new[:9]
        s = listing_session({
            1: [serve(old, 1, 5), serve(new, 1, 5)],
            2: serve(new, 2, 5),
            3: serve(new, 3, 5),
        })
        meta = {}
        prods, healthy, reason = bm.scrape_all(s, meta)
        self.assertTrue(healthy, reason)
        self.assertEqual(len(prods), 12)
        self.assertEqual(s.counts[listing_url(3)], 1)
        self.assertEqual(s.counts[listing_url(1)], 2)   # re-fetched because stale
        self.assertEqual(meta["site_total"], 12)
        self.assertEqual(meta["stale_pages"], [])

    def test_max_pages_caps_the_walk(self):
        bm.CONFIG["max_pages"] = 2
        cat = make_catalog(12)
        s = listing_session({1: serve(cat, 1, 5), 2: serve(cat, 2, 5)})
        prods, healthy, _ = bm.scrape_all(s, {})
        self.assertTrue(healthy)
        self.assertEqual(len(prods), 10)
        self.assertEqual(s.misses, [])

    def test_no_pagination_falls_back_to_walking_until_an_empty_page(self):
        cat = make_catalog(5)
        t = [tile(s_, c, pid) for (s_, c, pid, _) in cat]
        s = listing_session({1: listing(t[:3]), 2: listing(t[3:]), 3: listing([])})
        meta = {}
        prods, healthy, reason = bm.scrape_all(s, meta)
        self.assertTrue(healthy, reason)
        self.assertEqual(len(prods), 5)
        self.assertEqual(s.calls, [listing_url(1), listing_url(2), listing_url(3)])
        self.assertIn("old page walk", self.output())
        self.assertNotIn("site_total", meta)

    def test_fallback_walk_stops_when_a_page_repeats(self):
        cat = make_catalog(3)
        page = listing([tile(s_, c, pid) for (s_, c, pid, _) in cat])
        s = listing_session({1: page, 2: page})
        prods, healthy, _ = bm.scrape_all(s, {})
        self.assertTrue(healthy)
        self.assertEqual(len(prods), 3)
        self.assertEqual(s.calls, [listing_url(1), listing_url(2)])

    def test_page_1_http_error_is_unhealthy_with_a_reason(self):
        s = listing_session({1: FakeResp("", 503)})
        prods, healthy, reason = bm.scrape_all(s, {})
        self.assertFalse(healthy)
        self.assertEqual(prods, {})
        self.assertTrue(reason.startswith("page 1 fetch failed"), reason)
        self.assertIn("503", reason)

    def test_page_1_network_exception_is_unhealthy(self):
        s = listing_session({1: requests.ConnectionError("boom")})
        _, healthy, reason = bm.scrape_all(s, {})
        self.assertFalse(healthy)
        self.assertIn("boom", reason)

    def test_later_page_failure_is_unhealthy_and_returns_nothing(self):
        cat = make_catalog(50)
        s = listing_session({1: serve(cat, 1), 2: FakeResp("", 500), 3: serve(cat, 3)})
        prods, healthy, reason = bm.scrape_all(s, {})
        self.assertFalse(healthy)
        self.assertEqual(prods, {})
        self.assertTrue(reason.startswith("listing page fetch failed"), reason)

    def test_block_page_is_reported_as_blocked(self):
        html = "<html><body>Access Denied <script>%s</script></body></html>" % ("{" + pagination(24) + "}")
        s = listing_session({1: html})
        _, healthy, reason = bm.scrape_all(s, {})
        self.assertFalse(healthy)
        self.assertEqual(reason, "page 1 looked like a block/captcha page")

    def test_block_page_without_pagination_goes_through_the_walk(self):
        s = listing_session({1: "<html>Access Denied</html>"})
        _, healthy, reason = bm.scrape_all(s, {})
        self.assertFalse(healthy)
        self.assertIn("block/captcha", reason)

    def test_page_1_with_pagination_but_no_products_is_a_site_change(self):
        s = listing_session({1: listing([], total=24)})
        _, healthy, reason = bm.scrape_all(s, {})
        self.assertFalse(healthy)
        self.assertIn("no products", reason)

    def test_captcha_word_on_a_page_with_products_is_not_a_block(self):
        cat = make_catalog(3)
        s = listing_session({1: serve(cat, 1)})
        s.routes[listing_url(1)] = serve(cat, 1).replace("</body>", "recaptcha</body>")
        prods, healthy, reason = bm.scrape_all(s, {})
        self.assertTrue(healthy, reason)
        self.assertEqual(len(prods), 3)

    def test_variant_seen_sold_out_on_any_page_stays_sold_out(self):
        into = {"k": {"in_stock": True}}
        bm._merge_products(into, {"k": {"in_stock": False}})
        self.assertFalse(into["k"]["in_stock"])
        bm._merge_products(into, {"k": {"in_stock": True}})
        self.assertFalse(into["k"]["in_stock"])


class DropServerPinTests(Base):
    def test_removes_only_the_cflb_cookie(self):
        jar = requests.cookies.RequestsCookieJar()
        jar.set("__cflb", "1", domain="www.fragrancenet.com", path="/")
        jar.set("__cflb", "2", domain=".fragrancenet.com", path="/")
        jar.set("session", "keep", domain="www.fragrancenet.com", path="/")
        jar.set("__cf_bm", "keep2", domain=".fragrancenet.com", path="/")
        s = FakeSession()
        s.cookies = jar
        bm._drop_server_pin(s)
        self.assertEqual({c.name for c in jar}, {"session", "__cf_bm"})

    def test_session_without_cookies_does_not_raise(self):
        bm._drop_server_pin(object())


# ======================================================================
# reconcile / diff_states / cooldown
# ======================================================================
class ReconcileTests(Base):
    def test_missing_product_is_carried_then_dropped_after_the_grace_period(self):
        k = self.key("bujairami-gone")
        old = {k: self.entry("bujairami-gone")}
        r1 = bm.reconcile(old, {}, now=T0)
        self.assertIn(k, r1)
        self.assertEqual(r1[k]["missing_since"], T0)
        r2 = bm.reconcile(r1, {}, now=T0 + 47 * HOUR)
        self.assertEqual(r2[k]["missing_since"], T0)          # not reset
        r3 = bm.reconcile(r2, {}, now=T0 + 49 * HOUR)
        self.assertNotIn(k, r3)
        self.assertIn("Dropped from state", self.output())

    def test_returning_missing_product_is_not_a_new_listing(self):
        k = self.key("bujairami-blink")
        old = {k: self.entry("bujairami-blink", missing_since=T0)}
        new = {k: self.entry("bujairami-blink")}
        final = bm.reconcile(old, new, now=T0 + 3 * HOUR)
        self.assertNotIn("missing_since", final[k])
        added, back = bm.diff_states(old, final)
        self.assertEqual((added, back), ([], []))

    def test_sold_out_needs_two_runs_in_a_row(self):
        k = self.key()
        old = {k: self.entry(in_stock=True)}
        new = {k: self.entry(in_stock=False)}
        r1 = bm.reconcile(old, new, now=T0)
        self.assertTrue(r1[k]["in_stock"])
        self.assertEqual(r1[k]["out_streak"], 1)
        r2 = bm.reconcile(r1, {k: self.entry(in_stock=False)}, now=T0 + 5 * MIN)
        self.assertFalse(r2[k]["in_stock"])

    def test_one_run_blip_resets_the_streak(self):
        k = self.key()
        r1 = bm.reconcile({k: self.entry(in_stock=True)}, {k: self.entry(in_stock=False)}, now=T0)
        r2 = bm.reconcile(r1, {k: self.entry(in_stock=True)}, now=T0 + 5 * MIN)
        self.assertTrue(r2[k]["in_stock"])
        self.assertNotIn("out_streak", r2[k])
        r3 = bm.reconcile(r2, {k: self.entry(in_stock=False)}, now=T0 + 10 * MIN)
        self.assertTrue(r3[k]["in_stock"])   # streak starts again at 1

    def test_confirm_runs_1_means_immediate(self):
        bm.CONFIG["soldout_confirm_runs"] = 1
        k = self.key()
        r = bm.reconcile({k: self.entry(in_stock=True)}, {k: self.entry(in_stock=False)}, now=T0)
        self.assertFalse(r[k]["in_stock"])

    def test_page_verified_sold_out_applies_immediately(self):
        k = self.key()
        old = {k: self.entry(in_stock=True)}
        new = {k: self.entry(in_stock=False, page_verified=True)}
        self.assertFalse(bm.reconcile(old, new, now=T0)[k]["in_stock"])

    def test_already_sold_out_product_stays_sold_out(self):
        k = self.key()
        r = bm.reconcile({k: self.entry(in_stock=False)}, {k: self.entry(in_stock=False)}, now=T0)
        self.assertFalse(r[k]["in_stock"])

    def test_last_back_alert_is_carried_forward(self):
        k = self.key()
        old = {k: self.entry(in_stock=True, last_back_alert=T0 - 100)}
        r = bm.reconcile(old, {k: self.entry(in_stock=True)}, now=T0)
        self.assertEqual(r[k]["last_back_alert"], T0 - 100)

    def test_reconcile_does_not_mutate_its_inputs(self):
        k = self.key()
        new = {k: self.entry(in_stock=False)}
        old = {k: self.entry(in_stock=True)}
        bm.reconcile(old, new, now=T0)
        self.assertFalse(new[k]["in_stock"])
        self.assertTrue(old[k]["in_stock"])


class DiffStatesTests(Base):
    def test_new_listing_and_back_in_stock(self):
        a, b, c = self.key("bujairami-a"), self.key("bujairami-b"), self.key("bujairami-c")
        old = {a: self.entry("bujairami-a", in_stock=False), b: self.entry("bujairami-b", in_stock=True)}
        new = {a: self.entry("bujairami-a", in_stock=True), b: self.entry("bujairami-b", in_stock=True),
               c: self.entry("bujairami-c", in_stock=False)}
        added, back = bm.diff_states(old, new)
        self.assertEqual([p["slug"] for p in added], ["bujairami-c"])
        self.assertEqual([p["slug"] for p in back], ["bujairami-a"])

    def test_going_out_of_stock_and_unchanged_make_no_alert(self):
        a, b = self.key("bujairami-a"), self.key("bujairami-b")
        old = {a: self.entry("bujairami-a", in_stock=True), b: self.entry("bujairami-b", in_stock=False)}
        new = {a: self.entry("bujairami-a", in_stock=False), b: self.entry("bujairami-b", in_stock=False)}
        self.assertEqual(bm.diff_states(old, new), ([], []))

    def test_old_entry_without_a_stock_flag_is_not_back_in_stock(self):
        k = self.key()
        old = {k: {"slug": "bujairami-foo"}}
        self.assertEqual(bm.diff_states(old, {k: self.entry(in_stock=True)}), ([], []))


class BackCooldownTests(Base):
    def test_repeat_inside_the_window_is_dropped(self):
        p = self.entry(last_back_alert=T0 - 60 * MIN)
        self.assertEqual(bm._apply_back_cooldown([p], now=T0), [])
        self.assertIn("Skipped repeat BACK IN STOCK", self.output())

    def test_outside_the_window_is_kept_and_stamped(self):
        p = self.entry(last_back_alert=T0 - 200 * MIN)
        kept = bm._apply_back_cooldown([p], now=T0)
        self.assertEqual(kept, [p])
        self.assertEqual(p["last_back_alert"], T0)

    def test_first_ever_alert_is_kept(self):
        p = self.entry()
        self.assertEqual(len(bm._apply_back_cooldown([p], now=T0)), 1)
        self.assertEqual(p["last_back_alert"], T0)

    def test_cooldown_zero_disables_it(self):
        bm.CONFIG["back_alert_cooldown_min"] = 0
        p = self.entry(last_back_alert=T0 - 1)
        self.assertEqual(len(bm._apply_back_cooldown([p], now=T0)), 1)


# ======================================================================
# lock
# ======================================================================
class LockTests(Base):
    def test_acquire_blocks_a_second_copy_and_release_frees_it(self):
        self.assertTrue(bm.acquire_lock())
        self.assertTrue(os.path.exists(bm.CONFIG["lock_file"]))
        self.assertFalse(bm.acquire_lock())
        bm.release_lock()
        self.assertFalse(os.path.exists(bm.CONFIG["lock_file"]))
        self.assertTrue(bm.acquire_lock())
        bm.release_lock()

    def test_lock_file_holds_the_pid(self):
        bm.acquire_lock()
        self.assertEqual(self.read(bm.CONFIG["lock_file"]), str(os.getpid()))
        bm.release_lock()

    def test_stale_lock_is_taken_over(self):
        self.assertTrue(bm.acquire_lock())
        old = time.time() - 16 * 60
        os.utime(bm.CONFIG["lock_file"], (old, old))
        self.assertTrue(bm.acquire_lock())
        bm.release_lock()

    def test_fresh_lock_is_not_taken_over(self):
        self.assertTrue(bm.acquire_lock())
        recent = time.time() - 5 * 60
        os.utime(bm.CONFIG["lock_file"], (recent, recent))
        self.assertFalse(bm.acquire_lock())
        bm.release_lock()

    def test_release_without_a_lock_does_not_raise(self):
        bm.release_lock()

    def test_unexpected_lock_error_runs_anyway(self):
        with mock.patch("os.open", side_effect=OSError("disk gone")):
            self.assertTrue(bm.acquire_lock())
        self.assertIn("lock problem", self.output())


class RunLockedTests(Base):
    def test_lock_is_released_when_run_once_raises(self):
        with mock.patch.object(bm, "run_once", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                bm.run_locked(object())
        self.assertFalse(os.path.exists(bm.CONFIG["lock_file"]))

    def test_second_copy_skips_and_keeps_the_first_lock(self):
        self.assertTrue(bm.acquire_lock())
        with mock.patch.object(bm, "run_once") as run:
            bm.run_locked(object())
        run.assert_not_called()
        self.assertIn("Another copy", self.output())
        self.assertTrue(os.path.exists(bm.CONFIG["lock_file"]))
        bm.release_lock()

    def test_data_dir_is_created_before_the_lock_is_taken(self):
        fresh = os.path.join(self.tmp, "brand", "new")
        os.environ["BUJAIRAMI_DATA_DIR"] = fresh
        bm.CONFIG["lock_file"] = os.path.join(fresh, "bujairami.lock")
        with mock.patch.object(bm, "run_once") as run:
            bm.run_locked(object())
        run.assert_called_once()
        self.assertTrue(os.path.isdir(fresh))
        self.assertFalse(os.path.exists(bm.CONFIG["lock_file"]))

    def test_migration_runs_inside_the_lock_and_before_run_once(self):
        seen = []
        lock = bm.CONFIG["lock_file"]
        with mock.patch.object(bm, "migrate_root_state",
                               side_effect=lambda: seen.append(("migrate", os.path.exists(lock)))), \
                mock.patch.object(bm, "run_once",
                                  side_effect=lambda s: seen.append(("run", os.path.exists(lock)))):
            bm.run_locked(object())
        self.assertEqual(seen, [("migrate", True), ("run", True)])

    def test_migration_is_skipped_when_the_lock_is_held(self):
        bm.acquire_lock()
        with mock.patch.object(bm, "migrate_root_state") as mig, mock.patch.object(bm, "run_once"):
            bm.run_locked(object())
        mig.assert_not_called()
        bm.release_lock()

    def test_ensure_data_dir_creates_nested_dirs_and_tolerates_failure(self):
        fresh = os.path.join(self.tmp, "a", "b")
        os.environ["BUJAIRAMI_DATA_DIR"] = fresh
        bm.ensure_data_dir()
        self.assertTrue(os.path.isdir(fresh))
        with mock.patch("os.makedirs", side_effect=OSError("denied")):
            os.environ["BUJAIRAMI_DATA_DIR"] = os.path.join(self.tmp, "c")
            bm.ensure_data_dir()
        self.assertIn("could not create data dir", self.output())

    def test_ensure_data_dir_is_a_noop_when_the_data_dir_is_the_root(self):
        os.environ["BUJAIRAMI_DATA_DIR"] = ""
        os.environ["GITHUB_ACTIONS"] = "true"
        with mock.patch("os.makedirs") as mk:
            bm.ensure_data_dir()
        mk.assert_not_called()


# ======================================================================
# migrate_root_state
# ======================================================================
class MigrateTests(Base):
    FILES = {
        "bujairami_state.json": '{"k": 1}',
        "bujairami_deep.json": '{"cursor": "", "items": {}}',
        "bujairami_health.json": '{"status": "ok"}',
        "bujairami_events.log": "[x] old event\n",
    }

    def put_root(self, names=None):
        for n, text in self.FILES.items():
            if names is None or n in names:
                self.write(os.path.join(self.tmp, n), text)

    def dst(self, name):
        return os.path.join(self.data, name)

    def test_copies_all_four_files_once_and_leaves_the_root_untouched(self):
        self.put_root()
        bm.migrate_root_state()
        for n, text in self.FILES.items():
            self.assertEqual(self.read(self.dst(n)), text, n)
            self.assertEqual(self.read(os.path.join(self.tmp, n)), text, "root changed: " + n)
        self.assertEqual([f for f in os.listdir(self.data) if f.endswith(".tmp")], [])
        self.assertIn("Copied old root-level state", self.output())

    def test_second_call_is_a_noop_and_does_not_log_again(self):
        self.put_root()
        bm.migrate_root_state()
        self.out.truncate(0)
        self.out.seek(0)
        self.write(os.path.join(self.tmp, "bujairami_state.json"), '{"k": 2}')
        self.write(self.dst("bujairami_state.json"), '{"mine": true}')
        bm.migrate_root_state()
        self.assertEqual(self.read(self.dst("bujairami_state.json")), '{"mine": true}')
        self.assertNotIn("Copied old root-level state", self.output())

    def test_never_overwrites_an_existing_data_dir_file(self):
        self.put_root()
        self.write(self.dst("bujairami_events.log"), "new location history\n")
        bm.migrate_root_state()
        self.assertEqual(self.read(self.dst("bujairami_events.log")), "new location history\n")
        self.assertEqual(self.read(self.dst("bujairami_state.json")), self.FILES["bujairami_state.json"])

    def test_noop_when_the_data_dir_is_the_repo_root(self):
        self.put_root()
        os.environ["BUJAIRAMI_DATA_DIR"] = ""
        os.environ["GITHUB_ACTIONS"] = "true"
        with mock.patch("shutil.copy2") as cp:
            bm.migrate_root_state()
        cp.assert_not_called()
        self.assertEqual(os.listdir(self.data), [])

    def test_noop_without_a_root_state_file(self):
        self.put_root(names=("bujairami_deep.json", "bujairami_events.log"))
        bm.migrate_root_state()
        self.assertEqual(os.listdir(self.data), [])
        self.assertNotIn("Copied", self.output())

    def test_survives_a_copy_error_and_retries_on_the_next_run(self):
        self.put_root()
        real = __import__("shutil").copy2
        calls = []

        def flaky(src, dst, *a, **k):
            calls.append(src)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(src, dst, *a, **k)

        with mock.patch("shutil.copy2", side_effect=flaky):
            bm.migrate_root_state()          # must not raise
        self.assertIn("could not copy old state", self.output())
        self.assertFalse(os.path.exists(self.dst("bujairami_state.json")))   # state goes last
        self.assertEqual([f for f in os.listdir(self.data) if f.endswith(".tmp")], [])
        bm.migrate_root_state()              # second run finishes the job
        for n, text in self.FILES.items():
            self.assertEqual(self.read(self.dst(n)), text, n)

    def test_survives_a_missing_data_dir(self):
        self.put_root()
        os.environ["BUJAIRAMI_DATA_DIR"] = os.path.join(self.tmp, "nope")
        bm.CONFIG["state_file"] = os.path.join(self.tmp, "nope", "bujairami_state.json")
        bm.migrate_root_state()              # copy fails, must not raise
        self.assertIn("could not copy old state", self.output())


# ======================================================================
# save_* never raise
# ======================================================================
class SaveHelpersTests(Base):
    def test_save_state_writes_valid_json(self):
        bm.save_state({"a": {"in_stock": True}})
        self.assertEqual(self.read_json(bm.CONFIG["state_file"]), {"a": {"in_stock": True}})
        self.assertEqual(os.listdir(self.data), ["bujairami_state.json"])

    def test_savers_never_raise_when_the_write_fails(self):
        with mock.patch.object(deep_check, "write_json_atomic", side_effect=PermissionError("locked")):
            bm.save_state({"a": 1})
            bm.save_health({"status": "ok"})
            deep_check.save_deep({"cursor": "", "items": {}})
        out = self.output()
        self.assertIn("could not save state", out)
        self.assertIn("could not save health", out)
        self.assertIn("could not save deep state", out)

    def test_savers_never_raise_when_the_folder_is_missing(self):
        gone = os.path.join(self.tmp, "gone")
        bm.CONFIG["state_file"] = os.path.join(gone, "s.json")
        bm.CONFIG["health_file"] = os.path.join(gone, "h.json")
        bm.save_state({"a": 1})
        bm.save_health({"a": 1})
        deep_check.save_deep({"cursor": "", "items": {}}, os.path.join(gone, "d.json"))

    def test_load_state_survives_a_corrupt_file(self):
        self.write(bm.CONFIG["state_file"], '{"half": ')
        self.assertEqual(bm.load_state(), {})
        self.assertIn("could not read state file", self.output())


# ======================================================================
# notify / discord / chunks
# ======================================================================
class NotifyTests(Base):
    def run_notify(self, runner):
        with mock.patch.object(bm, "RUNNER", runner), \
                mock.patch.object(bm, "send_discord") as disc, \
                mock.patch.object(bm, "send_email") as mail:
            bm.notify("Subj", "line one\nline two")
        return disc, mail

    def test_label_pc(self):
        disc, mail = self.run_notify("PC")
        disc.assert_called_once_with("Subj [PC]", "line one\nline two")
        mail.assert_called_once_with("Subj [PC]", "line one\nline two")

    def test_label_github(self):
        disc, _ = self.run_notify("GitHub")
        self.assertEqual(disc.call_args[0][0], "Subj [GitHub]")

    def test_label_override(self):
        disc, _ = self.run_notify("Laptop")
        self.assertEqual(disc.call_args[0][0], "Subj [Laptop]")

    def test_labelled_subject_reaches_the_event_log_and_console(self):
        self.run_notify("PC")
        log = self.read(bm.CONFIG["event_log"]).replace("\r\n", "\n")   # CRLF on Windows
        self.assertIn("Subj [PC]\nline one\nline two\n", log)
        self.assertIn("ALERT: Subj [PC]", self.output())

    def test_labelled_subject_reaches_the_real_send_discord(self):
        bm.CONFIG["discord"]["webhook_url"] = "https://discord.invalid/api/webhooks/1/x"
        with mock.patch.object(bm, "RUNNER", "PC"), \
                mock.patch.object(requests, "post", return_value=FakeResp("", 204)) as post:
            bm.notify("Subj", "body")
        post.assert_called_once()
        self.assertEqual(post.call_args[1]["json"]["content"], "**Subj [PC]**\nbody")

    def test_no_webhook_means_no_post(self):
        with mock.patch.object(requests, "post") as post:
            bm.send_discord("s", "b")
        post.assert_not_called()

    def test_email_is_off_without_credentials(self):
        with mock.patch("smtplib.SMTP") as smtp:
            bm.send_email("s", "b")
        smtp.assert_not_called()

    def test_runner_defaults_in_this_process(self):
        # helpers cleared GITHUB_ACTIONS / BUJAIRAMI_RUNNER before import.
        self.assertEqual(bm.RUNNER, "PC")


class DiscordTests(Base):
    def setUp(self):
        super().setUp()
        bm.CONFIG["discord"]["webhook_url"] = "https://discord.invalid/api/webhooks/1/x"

    def test_long_body_is_split_into_several_posts(self):
        body = "\n".join("  - product %03d" % i + " x" * 30 for i in range(80))
        with mock.patch.object(requests, "post", return_value=FakeResp("", 204)) as post:
            bm.send_discord("Big", body)
        self.assertGreater(post.call_count, 1)
        for c in post.call_args_list:
            self.assertLessEqual(len(c[1]["json"]["content"]), 1900)

    def test_rate_limit_waits_and_retries(self):
        resp = [FakeResp('{"retry_after": 0.25}', 429), FakeResp("", 204)]
        with mock.patch.object(requests, "post", side_effect=resp) as post:
            bm.send_discord("S", "b")
        self.assertEqual(post.call_count, 2)
        self.assertIn(0.25, self.sleeps)

    def test_http_error_is_logged_not_raised(self):
        with mock.patch.object(requests, "post", return_value=FakeResp("nope", 400)):
            bm.send_discord("S", "b")
        self.assertIn("HTTP 400", self.output())

    def test_network_exception_is_retried_then_given_up(self):
        with mock.patch.object(requests, "post", side_effect=requests.ConnectionError("x")) as post:
            bm.send_discord("S", "b")
        self.assertEqual(post.call_count, 3)


class ChunksTests(unittest.TestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(bm._chunks("a\nb"), ["a\nb"])

    def test_empty_text_gives_no_chunks(self):
        self.assertEqual(bm._chunks(""), [])

    def test_splits_on_line_breaks_within_the_limit_and_loses_nothing(self):
        text = "\n".join(("line %03d " % i) + "y" * 80 for i in range(60))
        parts = bm._chunks(text, limit=500)
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= 500 for p in parts))
        self.assertEqual("\n".join(parts), text)

    def test_one_absurdly_long_line_is_cut_into_limit_sized_pieces(self):
        parts = bm._chunks("z" * 4500, limit=1900)
        self.assertEqual([len(p) for p in parts], [1900, 1900, 700])
        self.assertEqual("".join(parts), "z" * 4500)


# ======================================================================
# health alerts
# ======================================================================
class HealthTests(Base):
    def setUp(self):
        super().setUp()
        self.clock = Clock()
        p = mock.patch("time.time", self.clock)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(bm, "notify")
        self.notify = p.start()
        self.addCleanup(p.stop)

    def at(self, minutes):
        self.clock.t = Clock().t + minutes * MIN

    def health(self):
        return self.read_json(bm.CONFIG["health_file"])

    def test_no_ping_before_the_alert_threshold_then_exactly_one(self):
        self.at(0)
        bm.report_unhealthy("blocked 403")
        self.at(30)
        bm.report_unhealthy("blocked 403")
        self.notify.assert_not_called()
        self.assertEqual(self.health()["status"], "down")
        self.assertEqual(self.health()["fails"], 2)
        self.at(61)
        bm.report_unhealthy("blocked 403")
        self.assertEqual(self.notify.call_count, 1)
        subject, body = self.notify.call_args[0]
        self.assertEqual(subject, "Bujairami monitor went quiet")
        self.assertIn("3 failed runs", body)
        self.assertIn("blocked 403", body)
        self.at(66)
        bm.report_unhealthy("blocked 403")
        self.assertEqual(self.notify.call_count, 1)

    def test_reminder_only_after_the_reminder_hours(self):
        self.at(0)
        bm.report_unhealthy("x")
        self.at(61)
        bm.report_unhealthy("x")
        self.assertEqual(self.notify.call_count, 1)
        self.at(61 + 6 * 60 - 1)
        bm.report_unhealthy("x")
        self.assertEqual(self.notify.call_count, 1)
        self.at(61 + 6 * 60)
        bm.report_unhealthy("x")
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(self.notify.call_args[0][0], "Bujairami monitor still down")
        self.at(61 + 6 * 60 + 5)
        bm.report_unhealthy("x")
        self.assertEqual(self.notify.call_count, 2)

    def test_reminder_hours_setting_is_honoured(self):
        bm.CONFIG["health_reminder_hours"] = 24.0
        self.at(0)
        bm.report_unhealthy("x")
        self.at(61)
        bm.report_unhealthy("x")
        self.at(61 + 7 * 60)
        bm.report_unhealthy("x")
        self.assertEqual(self.notify.call_count, 1)
        self.at(61 + 24 * 60)
        bm.report_unhealthy("x")
        self.assertEqual(self.notify.call_count, 2)

    def test_reminder_hours_zero_means_no_reminders(self):
        bm.CONFIG["health_reminder_hours"] = 0.0
        self.at(0)
        bm.report_unhealthy("x")
        self.at(61)
        bm.report_unhealthy("x")
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.notify.call_args[0][0], "Bujairami monitor went quiet")
        for minutes in (62, 61 + 6 * 60, 61 + 24 * 60, 61 + 30 * 24 * 60):
            self.at(minutes)
            bm.report_unhealthy("x")
        self.assertEqual(self.notify.call_count, 1)
        # Muted reminders must not mute the recovery ping.
        self.at(61 + 30 * 24 * 60 + 5)
        bm.report_healthy(115, 115)
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(self.notify.call_args[0][0], "Bujairami monitor recovered")

    def test_recovered_ping_only_if_a_quiet_ping_was_sent(self):
        self.at(0)
        bm.report_unhealthy("x")
        self.at(61)
        bm.report_unhealthy("x")
        self.notify.reset_mock()
        self.at(70)
        bm.report_healthy(100, 115)
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.notify.call_args[0][0], "Bujairami monitor recovered")
        h = self.health()
        self.assertEqual((h["status"], h["products"], h["site_total"]), ("ok", 100, 115))

    def test_short_blip_gets_a_log_line_and_no_ping(self):
        self.at(0)
        bm.report_unhealthy("flaky")
        self.at(10)
        bm.report_healthy(100, 100)
        self.notify.assert_not_called()
        self.assertIn("HEALTH: blip", self.read(bm.CONFIG["event_log"]))
        self.assertEqual(self.health()["status"], "ok")

    def test_healthy_when_already_ok_pings_nothing_and_records_counts(self):
        bm.report_healthy(42, None)
        self.notify.assert_not_called()
        h = self.health()
        self.assertEqual((h["status"], h["products"], h["site_total"]), ("ok", 42, None))
        self.assertIn("checked", h)

    def test_corrupt_health_file_is_treated_as_ok(self):
        self.write(bm.CONFIG["health_file"], "{not json")
        self.assertEqual(bm.load_health()["status"], "ok")


# ======================================================================
# run_once end to end
# ======================================================================
class RunOnceTests(Base):
    A = "bujairami-aaa"
    B = "bujairami-bbb"
    C = "bujairami-ccc"

    def setUp(self):
        super().setUp()
        p = mock.patch.object(bm, "notify")
        self.notify = p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(bm, "RUNNER", "PC")
        p.start()
        self.addCleanup(p.stop)

    def purl(self, slug):
        return "%s/perfume/bujairami/%s/eau-de-parfum" % (BASE, slug)

    def session(self, a="in", c="in"):
        page = listing([tile(self.A, pid=1), tile(self.B, pid=2), tile(self.C, pid=3)], total=3)
        return FakeSession({listing_url(1): page,
                            self.purl(self.A): product_page(a),
                            self.purl(self.C): product_page(c)})

    def seed_old(self):
        old = {self.key(self.A): self.entry(self.A, in_stock=False),
               self.key(self.B): self.entry(self.B, in_stock=True)}
        self.write(bm.CONFIG["state_file"], json.dumps(old))

    def test_new_listing_and_back_in_stock_send_exactly_one_alert(self):
        self.seed_old()
        s = self.session()
        bm.run_once(s)
        self.assertEqual(s.misses, [])
        self.assertEqual(self.notify.call_count, 1)
        subject, body = self.notify.call_args[0]
        self.assertEqual(subject, "Bujairami: 1 back in stock, 1 new listing(s)")
        self.assertIn("BACK IN STOCK:", body)
        self.assertIn("NEW LISTING:", body)
        self.assertIn(self.purl(self.A), body)
        self.assertIn(self.purl(self.C), body)
        self.assertNotIn(self.purl(self.B), body)
        self.assertLess(body.index("BACK IN STOCK:"), body.index("NEW LISTING:"))
        self.assertIn("(in stock)", body)

        state = self.read_json(bm.CONFIG["state_file"])
        self.assertEqual(set(state), {self.key(self.A), self.key(self.B), self.key(self.C)})
        self.assertTrue(state[self.key(self.A)]["in_stock"])
        self.assertIn("last_back_alert", state[self.key(self.A)])
        health = self.read_json(bm.CONFIG["health_file"])
        self.assertEqual((health["status"], health["products"], health["site_total"]),
                         ("ok", 3, 3))
        # everything went to the data dir, nothing to the working directory
        self.assertEqual(os.listdir(self.tmp), ["data"])
        self.assertFalse(any(f.endswith(".tmp") for f in os.listdir(self.data)))

    def test_a_second_identical_run_sends_no_duplicate_alert(self):
        self.seed_old()
        bm.run_once(self.session())
        self.assertEqual(self.notify.call_count, 1)
        s2 = self.session()
        bm.run_once(s2)
        self.assertEqual(self.notify.call_count, 1)
        self.assertIn("No changes", self.output())
        self.assertEqual(s2.calls, [listing_url(1)])    # nothing changed: no product pages

    def test_listing_says_in_stock_but_product_page_says_sold_out_sends_nothing(self):
        self.seed_old()
        s = self.session(a="out", c="in")
        bm.run_once(s)
        subject, body = self.notify.call_args[0]
        self.assertEqual(subject, "Bujairami: 0 back in stock, 1 new listing(s)")
        self.assertNotIn(self.purl(self.A), body)
        self.assertFalse(self.read_json(bm.CONFIG["state_file"])[self.key(self.A)]["in_stock"])

    def test_first_run_saves_a_baseline_without_alerting(self):
        s = self.session()
        bm.run_once(s)
        self.notify.assert_not_called()
        self.assertIn("Baseline saved: 3 products", self.output())
        self.assertEqual(len(self.read_json(bm.CONFIG["state_file"])), 3)
        self.assertEqual(s.calls, [listing_url(1)])     # no product page checks on a baseline

    def test_failed_scrape_leaves_state_untouched_and_does_not_alert_yet(self):
        self.seed_old()
        before = self.read(bm.CONFIG["state_file"])
        bm.run_once(listing_session({1: FakeResp("", 403)}))
        self.assertEqual(self.read(bm.CONFIG["state_file"]), before)
        self.notify.assert_not_called()
        self.assertEqual(self.read_json(bm.CONFIG["health_file"])["status"], "down")

    def test_partial_scrape_is_skipped(self):
        old = {self.key("bujairami-p%03d" % i): self.entry("bujairami-p%03d" % i) for i in range(10)}
        self.write(bm.CONFIG["state_file"], json.dumps(old))
        before = self.read(bm.CONFIG["state_file"])
        page = listing([tile("bujairami-p000", pid=1), tile("bujairami-p001", pid=2)], total=2)
        bm.run_once(listing_session({1: page}))
        self.assertEqual(self.read(bm.CONFIG["state_file"]), before)
        self.notify.assert_not_called()
        self.assertIn("skipping this cycle", self.output())

    def test_back_in_stock_repeat_inside_the_cooldown_is_suppressed(self):
        old = {self.key(self.A): self.entry(self.A, in_stock=False, last_back_alert=time.time() - 10 * MIN),
               self.key(self.B): self.entry(self.B, in_stock=True),
               self.key(self.C): self.entry(self.C, in_stock=True)}
        self.write(bm.CONFIG["state_file"], json.dumps(old))
        bm.run_once(self.session())
        self.notify.assert_not_called()
        self.assertIn("Skipped repeat BACK IN STOCK", self.output())


if __name__ == "__main__":
    unittest.main()
