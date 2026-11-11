"""Tests for deep_check.py and the data-dir / import-time environment handling.
No network, no Discord, nothing outside temp dirs."""
import helpers  # noqa: F401  (must be first: sets up the safe environment)
from helpers import (BASE, REPO, Base, FakeResp, FakeSession, bm, deep_check,
                     product_page)

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

REAL_LOAD_WATCHLIST = deep_check.load_watchlist
REAL_REPLACE = os.replace


def purl(slug):
    return "%s/perfume/bujairami/%s/eau-de-parfum" % (BASE, slug)


# ======================================================================
# data_dir / data_path
# ======================================================================
class DataDirTests(Base):
    def setUp(self):
        super().setUp()
        os.environ.pop("BUJAIRAMI_DATA_DIR", None)
        os.environ.pop("GITHUB_ACTIONS", None)

    def test_env_override_wins(self):
        os.environ["BUJAIRAMI_DATA_DIR"] = "somewhere"
        self.assertEqual(deep_check.data_dir(), "somewhere")
        self.assertEqual(deep_check.data_path("a.json"), os.path.join("somewhere", "a.json"))

    def test_env_override_wins_on_github_actions_too(self):
        os.environ["BUJAIRAMI_DATA_DIR"] = "somewhere"
        os.environ["GITHUB_ACTIONS"] = "true"
        self.assertEqual(deep_check.data_dir(), "somewhere")

    def test_github_actions_uses_the_repo_root(self):
        os.environ["GITHUB_ACTIONS"] = "true"
        self.assertEqual(deep_check.data_dir(), "")
        self.assertEqual(deep_check.data_path("a.json"), "a.json")

    def test_empty_override_on_github_actions_is_still_the_repo_root(self):
        os.environ["BUJAIRAMI_DATA_DIR"] = ""
        os.environ["GITHUB_ACTIONS"] = "true"
        self.assertEqual(deep_check.data_dir(), "")

    def test_pc_default_is_local_data(self):
        self.assertEqual(deep_check.data_dir(), "local_data")
        self.assertEqual(deep_check.data_path("a.json"), os.path.join("local_data", "a.json"))

    def test_empty_override_on_a_pc_is_local_data(self):
        os.environ["BUJAIRAMI_DATA_DIR"] = ""
        self.assertEqual(deep_check.data_dir(), "local_data")

    def test_only_the_exact_string_true_counts_as_github_actions(self):
        for v in ("false", "1", "True", ""):
            os.environ["GITHUB_ACTIONS"] = v
            self.assertEqual(deep_check.data_dir(), "local_data", v)


class ImportTimeEnvTests(Base):
    """Values read once at import (RUNNER, state paths, DEEP_STATE_FILE,
    HEALTH_REMINDER_HOURS) are checked in a fresh interpreter."""

    CODE = ("import json, deep_check as d, bujairami_monitor as b; "
            "print(json.dumps({'runner': b.RUNNER, 'state': b.CONFIG['state_file'], "
            "'log': b.CONFIG['event_log'], 'health': b.CONFIG['health_file'], "
            "'lock': b.CONFIG['lock_file'], 'deep': d.DEEP_STATE_FILE, "
            "'reminder': b.CONFIG['health_reminder_hours'], 'dir': d.data_dir()}))")

    def run_py(self, extra=None):
        env = {k: v for k, v in os.environ.items()
               if k not in helpers.CLEAR_ENV and k != "BUJAIRAMI_DATA_DIR"}
        env.update({"BUJAIRAMI_DISCORD_WEBHOOK": "", "BUJAIRAMI_SMTP_USER": "",
                    "BUJAIRAMI_SMTP_PASS": "", "PYTHONDONTWRITEBYTECODE": "1"})
        env.update(extra or {})
        code = "import sys; sys.dont_write_bytecode = True; sys.path.insert(0, %r); %s" % (REPO, self.CODE)
        r = subprocess.run([sys.executable, "-c", code], cwd=self.tmp, env=env,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(os.listdir(self.tmp), ["data"], "importing must not create files")
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_pc_defaults(self):
        r = self.run_py()
        self.assertEqual(r["dir"], "local_data")
        self.assertEqual(r["state"], os.path.join("local_data", "bujairami_state.json"))
        self.assertEqual(r["log"], os.path.join("local_data", "bujairami_events.log"))
        self.assertEqual(r["health"], os.path.join("local_data", "bujairami_health.json"))
        self.assertEqual(r["lock"], os.path.join("local_data", "bujairami.lock"))
        self.assertEqual(r["deep"], os.path.join("local_data", "bujairami_deep.json"))
        self.assertEqual(r["runner"], "PC")
        self.assertEqual(r["reminder"], 6.0)

    def test_github_actions_uses_root_files_and_the_github_label(self):
        r = self.run_py({"GITHUB_ACTIONS": "true"})
        self.assertEqual(r["state"], "bujairami_state.json")
        self.assertEqual(r["log"], "bujairami_events.log")
        self.assertEqual(r["health"], "bujairami_health.json")
        self.assertEqual(r["lock"], "bujairami.lock")
        self.assertEqual(r["deep"], "bujairami_deep.json")
        self.assertEqual(r["runner"], "GitHub")

    def test_data_dir_env_moves_everything(self):
        custom = os.path.join(self.tmp, "elsewhere")
        r = self.run_py({"BUJAIRAMI_DATA_DIR": custom, "GITHUB_ACTIONS": "true"})
        for k, name in (("state", "bujairami_state.json"), ("log", "bujairami_events.log"),
                        ("health", "bujairami_health.json"), ("lock", "bujairami.lock"),
                        ("deep", "bujairami_deep.json")):
            self.assertEqual(r[k], os.path.join(custom, name), k)

    def test_deep_check_state_env_overrides_only_the_deep_file(self):
        custom = os.path.join(self.tmp, "deep_elsewhere.json")
        r = self.run_py({"DEEP_CHECK_STATE": custom, "BUJAIRAMI_DATA_DIR": os.path.join(self.tmp, "d")})
        self.assertEqual(r["deep"], custom)
        self.assertEqual(r["state"], os.path.join(self.tmp, "d", "bujairami_state.json"))

    def test_runner_env_overrides_both_defaults(self):
        self.assertEqual(self.run_py({"BUJAIRAMI_RUNNER": "Laptop"})["runner"], "Laptop")
        self.assertEqual(self.run_py({"BUJAIRAMI_RUNNER": "Laptop", "GITHUB_ACTIONS": "true"})["runner"],
                         "Laptop")

    def test_empty_runner_env_falls_back_to_the_default(self):
        self.assertEqual(self.run_py({"BUJAIRAMI_RUNNER": ""})["runner"], "PC")

    def test_health_reminder_hours_env(self):
        self.assertEqual(self.run_py({"HEALTH_REMINDER_HOURS": "24"})["reminder"], 24.0)


# ======================================================================
# write_json_atomic
# ======================================================================
class WriteJsonAtomicTests(Base):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(self.data, "x.json")

    def leftovers(self):
        return sorted(f for f in os.listdir(self.data) if f != "x.json")

    def test_writes_valid_json_and_leaves_no_temp_file(self):
        deep_check.write_json_atomic(self.path, {"a": [1, 2], "b": "c"})
        self.assertEqual(self.read_json(self.path), {"a": [1, 2], "b": "c"})
        self.assertEqual(self.leftovers(), [])

    def test_replaces_an_existing_file(self):
        self.write(self.path, '{"old": true}')
        deep_check.write_json_atomic(self.path, {"new": True})
        self.assertEqual(self.read_json(self.path), {"new": True})

    def test_works_with_a_bare_filename_in_the_cwd(self):
        deep_check.write_json_atomic("bare.json", {"a": 1})
        self.assertEqual(self.read_json(os.path.join(self.tmp, "bare.json")), {"a": 1})

    def test_permission_error_is_retried_then_succeeds(self):
        calls = []

        def flaky(src, dst):
            calls.append(1)
            if len(calls) < 3:
                raise PermissionError("in use")
            return REAL_REPLACE(src, dst)

        with mock.patch("os.replace", side_effect=flaky):
            deep_check.write_json_atomic(self.path, {"ok": 1}, tries=5, delay=0.5)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.sleeps, [0.5, 0.5])
        self.assertEqual(self.read_json(self.path), {"ok": 1})
        self.assertEqual(self.leftovers(), [])

    def test_final_failure_raises_keeps_the_old_file_and_removes_the_temp(self):
        self.write(self.path, '{"old": true}')
        with mock.patch("os.replace", side_effect=PermissionError("in use")) as rep:
            with self.assertRaises(PermissionError):
                deep_check.write_json_atomic(self.path, {"new": True}, tries=3, delay=0.1)
        self.assertEqual(rep.call_count, 3)
        self.assertEqual(self.sleeps, [0.1, 0.1])
        self.assertEqual(self.read_json(self.path), {"old": True})
        self.assertEqual(self.leftovers(), [])

    def test_other_os_errors_are_not_retried(self):
        with mock.patch("os.replace", side_effect=OSError("disk")) as rep:
            with self.assertRaises(OSError):
                deep_check.write_json_atomic(self.path, {})
        self.assertEqual(rep.call_count, 1)
        self.assertEqual(self.leftovers(), [])

    def test_unserialisable_data_raises_and_keeps_the_old_file(self):
        self.write(self.path, '{"old": true}')
        with self.assertRaises(TypeError):
            deep_check.write_json_atomic(self.path, {"bad": object()})
        self.assertEqual(self.read_json(self.path), {"old": True})
        self.assertEqual(self.leftovers(), [])

    def test_missing_folder_raises(self):
        with self.assertRaises(OSError):
            deep_check.write_json_atomic(os.path.join(self.tmp, "nope", "x.json"), {})

    def test_save_deep_writes_through_the_atomic_writer(self):
        deep_check.save_deep({"cursor": "k", "items": {}})
        self.assertEqual(self.read_json(self.deep_path)["cursor"], "k")
        self.assertEqual(deep_check.load_deep()["cursor"], "k")

    def test_load_deep_defaults_when_missing_or_corrupt(self):
        self.assertEqual(deep_check.load_deep(), {"cursor": "", "items": {}})
        self.write(self.deep_path, "{oops")
        self.assertEqual(deep_check.load_deep(), {"cursor": "", "items": {}})
        self.write(self.deep_path, "{}")
        self.assertEqual(deep_check.load_deep(), {"cursor": "", "items": {}})


# ======================================================================
# detect_stock
# ======================================================================
class DetectStockTests(unittest.TestCase):
    def test_json_ld_in_stock(self):
        state, reason = deep_check.detect_stock(product_page("in"))
        self.assertIs(state, True)
        self.assertIn("json-ld", reason)

    def test_json_ld_out_of_stock(self):
        self.assertIs(deep_check.detect_stock(product_page("out"))[0], False)

    def test_json_ld_nested_in_a_list_with_an_offers_array(self):
        html = ('<html><head><script type="application/ld+json">'
                '[{"@type":"Product","offers":[{"availability":"http://schema.org/LimitedAvailability"}]}]'
                '</script></head><body>' + "x" * 600 + '</body></html>')
        self.assertIs(deep_check.detect_stock(html)[0], True)

    def test_json_ld_wins_over_an_active_cart_button(self):
        html = product_page("out").replace("<body>", '<body><button class="add-to-cart">Add to Cart</button>')
        self.assertIs(deep_check.detect_stock(html)[0], False)

    def test_malformed_json_ld_falls_back_to_a_regex(self):
        html = ('<html><head><script type="application/ld+json">{"availability": '
                '"https://schema.org/OutOfStock", oops</script></head><body>' + "x" * 600 + "</body></html>")
        self.assertIs(deep_check.detect_stock(html)[0], False)

    def test_json_ld_with_an_unknown_value_falls_through_to_the_next_signal(self):
        html = product_page("cart").replace(
            "</head>", '<script type="application/ld+json">{"availability":"Mystery"}</script></head>')
        self.assertIs(deep_check.detect_stock(html)[0], True)

    def test_next_data_some_size_in_stock(self):
        state, reason = deep_check.detect_stock(product_page("nd_in"))
        self.assertIs(state, True)
        self.assertIn("1 of 2", reason)

    def test_next_data_all_sizes_out(self):
        state, reason = deep_check.detect_stock(product_page("nd_out"))
        self.assertIs(state, False)
        self.assertIn("all 2", reason)

    def test_active_cart_button_means_in_stock(self):
        state, reason = deep_check.detect_stock(product_page("cart"))
        self.assertIs(state, True)
        self.assertIn("add-to-cart", reason)

    def test_disabled_cart_button_with_sold_out_wording_means_out(self):
        self.assertIs(deep_check.detect_stock(product_page("soldwording"))[0], False)

    def test_out_of_stock_wording_alone_means_out(self):
        html = "<html><body><p>Notify me when available</p>" + "x" * 600 + "</body></html>"
        self.assertIs(deep_check.detect_stock(html)[0], False)

    def test_no_signal_means_cannot_tell(self):
        state, reason = deep_check.detect_stock(product_page("unknown"))
        self.assertIsNone(state)
        self.assertIn("no usable stock signal", reason)

    def test_short_or_empty_page_means_cannot_tell(self):
        self.assertIsNone(deep_check.detect_stock("")[0])
        self.assertIsNone(deep_check.detect_stock("<html>tiny</html>")[0])
        self.assertIsNone(deep_check.detect_stock(None)[0])


# ======================================================================
# fetch_retry / check_product
# ======================================================================
class FetchRetryTests(Base):
    URL = purl("bujairami-foo")
    NOSLEEP = staticmethod(lambda s: None)

    def sess(self, *responses):
        return FakeSession({self.URL: list(responses)})

    def test_404_is_retried_until_it_loads(self):
        s = self.sess(FakeResp("", 404), FakeResp("", 404), FakeResp(product_page("in"), 200))
        status, html, final, tries = deep_check.fetch_retry(s, self.URL, retries=7, sleep=self.NOSLEEP)
        self.assertEqual((status, tries), (200, 3))
        self.assertEqual(len(s.calls), 3)

    def test_403_returns_immediately(self):
        s = self.sess(FakeResp("", 403), FakeResp(product_page("in"), 200))
        status, _, _, tries = deep_check.fetch_retry(s, self.URL, retries=7, sleep=self.NOSLEEP)
        self.assertEqual((status, tries), (403, 1))
        self.assertEqual(len(s.calls), 1)

    def test_429_returns_immediately(self):
        s = self.sess(FakeResp("", 429))
        status, _, _, tries = deep_check.fetch_retry(s, self.URL, retries=7, sleep=self.NOSLEEP)
        self.assertEqual((status, tries), (429, 1))

    def test_persistent_404_gives_up_after_the_retry_count(self):
        s = self.sess(FakeResp("", 404))
        status, _, _, tries = deep_check.fetch_retry(s, self.URL, retries=3, sleep=self.NOSLEEP)
        self.assertEqual((status, tries), (404, 3))
        self.assertEqual(len(s.calls), 3)

    def test_soft_404_served_with_status_200_is_retried(self):
        soft = "<html><head><title>Page Not Found</title></head></html>"
        s = self.sess(FakeResp(soft, 200), FakeResp(product_page("in"), 200))
        status, html, _, tries = deep_check.fetch_retry(s, self.URL, retries=5, sleep=self.NOSLEEP)
        self.assertEqual((status, tries), (200, 2))
        self.assertIn("InStock", html)

    def test_5xx_and_network_errors_are_retried(self):
        s = self.sess(FakeResp("", 502), requests_error(), FakeResp(product_page("in"), 200))
        status, _, _, tries = deep_check.fetch_retry(s, self.URL, retries=5, sleep=self.NOSLEEP)
        self.assertEqual((status, tries), (200, 3))

    def test_network_errors_report_status_zero(self):
        s = self.sess(requests_error())
        status, _, final, tries = deep_check.fetch_retry(s, self.URL, retries=2, sleep=self.NOSLEEP)
        self.assertEqual((status, tries), (0, 2))
        self.assertIn("down", final)

    def test_check_product_blocked(self):
        s = self.sess(FakeResp("", 403))
        self.assertEqual(deep_check.check_product(s, self.URL),
                         (None, "blocked http 403", 403))

    def test_check_product_reads_the_page(self):
        s = self.sess(FakeResp("", 404), FakeResp(product_page("out"), 200))
        state, reason, status = deep_check.check_product(s, self.URL)
        self.assertEqual((state, status), (False, 200))
        self.assertIn("took 2 tries", reason)

    def test_check_product_404_is_never_a_verdict(self):
        s = self.sess(FakeResp("", 404))
        state, reason, status = deep_check.check_product(s, self.URL, retries=2)
        self.assertEqual((state, status), (None, 404))
        self.assertIn("404 on all 2 tries", reason)


def requests_error():
    return helpers.requests.ConnectionError("down")


# ======================================================================
# watchlist
# ======================================================================
class WatchlistTests(Base):
    def test_list_and_dict_formats_lowercase_and_strip(self):
        p = os.path.join(self.tmp, "w1.json")
        self.write(p, '[" Foo ", "", "BAR"]')
        self.assertEqual(REAL_LOAD_WATCHLIST(p), ["foo", "bar"])
        self.write(p, '{"watch": ["Baz"]}')
        self.assertEqual(REAL_LOAD_WATCHLIST(p), ["baz"])

    def test_bom_missing_and_corrupt_files(self):
        p = os.path.join(self.tmp, "w2.json")
        with open(p, "wb") as f:
            f.write(b'\xef\xbb\xbf["x"]')
        self.assertEqual(REAL_LOAD_WATCHLIST(p), ["x"])
        self.assertEqual(REAL_LOAD_WATCHLIST(os.path.join(self.tmp, "nope.json")), [])
        self.write(p, "{bad")
        self.assertEqual(REAL_LOAD_WATCHLIST(p), [])

    def test_on_watchlist_matches_key_slug_name_or_concentration(self):
        prod = {"slug": "bujairami-tea-splash", "name": "Bujairami Tea Splash",
                "concentration": "extrait de parfum"}
        key = "bujairami-tea-splash|extrait de parfum"
        self.assertTrue(deep_check.on_watchlist(key, prod, ["tea-splash"]))
        self.assertTrue(deep_check.on_watchlist(key, prod, ["extrait"]))
        self.assertFalse(deep_check.on_watchlist(key, prod, ["roses"]))
        self.assertFalse(deep_check.on_watchlist(key, prod, []))

    def watch(self, slug, patterns, name=None):
        key = bm._key(slug, "eau de parfum")
        prod = {"slug": slug, "name": name or "", "concentration": "eau de parfum"}
        return deep_check.on_watchlist(key, prod, patterns)

    def test_period_pattern_matches_a_hyphen_slug(self):
        self.assertTrue(self.watch("bujairami-drip-no-7", ["bujairami-drip-no.7"]))

    def test_hyphen_pattern_matches_a_period_slug(self):
        self.assertTrue(self.watch("bujairami-drip-no.7", ["bujairami-drip-no-7"]))

    def test_period_pattern_matches_a_period_slug(self):
        self.assertTrue(self.watch("bujairami-drip-no.7", ["bujairami-drip-no.7"]))

    def test_unrelated_pattern_still_does_not_match(self):
        self.assertFalse(self.watch("bujairami-drip-no.7", ["bujairami-drip-no.8"]))
        self.assertFalse(self.watch("bujairami-drip-no-7", ["roses", "no.9"]))

    def test_matching_is_still_case_insensitive_on_the_blob_side(self):
        self.assertTrue(self.watch("bujairami-foo", ["tea splash"], name="Bujairami TEA SPLASH"))
        self.assertTrue(self.watch("bujairami-drip-no.7", ["drip-no-7"], name="BUJAIRAMI DRIP NO.7"))

    def test_empty_pattern_list_is_false(self):
        self.assertFalse(self.watch("bujairami-drip-no.7", []))


# ======================================================================
# select_targets
# ======================================================================
class SelectTargetsTests(unittest.TestCase):
    def products(self, *slugs):
        out = {}
        for s in slugs:
            out["%s|eau de parfum" % s] = {"slug": s, "name": s, "concentration": "eau de parfum"}
        return out

    def test_rotation_advances_wraps_and_resumes_after_the_cursor(self):
        prods = self.products("bujairami-a", "bujairami-b", "bujairami-c", "bujairami-d", "bujairami-e")
        keys = list(prods)
        t, cur = deep_check.select_targets(keys, prods, [], "", 2, "both")
        self.assertEqual(t, [keys[0], keys[1]])
        self.assertEqual(cur, keys[1])
        t, cur = deep_check.select_targets(keys, prods, [], cur, 2, "both")
        self.assertEqual(t, [keys[2], keys[3]])
        t, cur = deep_check.select_targets(keys, prods, [], cur, 2, "both")
        self.assertEqual(t, [keys[4], keys[0]])      # wraps
        self.assertEqual(cur, keys[0])

    def test_cursor_past_the_end_restarts_at_the_beginning(self):
        prods = self.products("bujairami-a", "bujairami-b")
        t, _ = deep_check.select_targets(list(prods), prods, [], "zzz", 1, "both")
        self.assertEqual(t, ["bujairami-a|eau de parfum"])

    def test_cursor_survives_a_removed_product(self):
        prods = self.products("bujairami-a", "bujairami-c")      # "b" was the cursor and is gone
        t, _ = deep_check.select_targets(list(prods), prods, [], "bujairami-b|eau de parfum", 1, "both")
        self.assertEqual(t, ["bujairami-c|eau de parfum"])

    def test_watchlist_items_come_first_and_use_up_batch_room(self):
        prods = self.products("bujairami-a", "bujairami-b", "bujairami-c", "bujairami-star")
        t, _ = deep_check.select_targets(list(prods), prods, ["star"], "", 2, "both")
        self.assertEqual(t, ["bujairami-star|eau de parfum", "bujairami-a|eau de parfum"])

    def test_watchlist_larger_than_the_batch_still_all_checked(self):
        prods = self.products("bujairami-w1", "bujairami-w2", "bujairami-x")
        t, cur = deep_check.select_targets(list(prods), prods, ["w1", "w2"], "", 1, "both")
        self.assertEqual(t, ["bujairami-w1|eau de parfum", "bujairami-w2|eau de parfum"])

    def test_watchlist_mode_checks_only_watchlist_and_keeps_the_cursor(self):
        prods = self.products("bujairami-a", "bujairami-star")
        t, cur = deep_check.select_targets(list(prods), prods, ["star"], "keep", 5, "watchlist")
        self.assertEqual(t, ["bujairami-star|eau de parfum"])
        self.assertEqual(cur, "keep")

    def test_rotate_mode_ignores_watchlist_priority(self):
        prods = self.products("bujairami-a", "bujairami-star")
        t, _ = deep_check.select_targets(list(prods), prods, ["star"], "", 1, "rotate")
        self.assertEqual(t, ["bujairami-a|eau de parfum"])

    def test_all_mode_returns_everything_sorted(self):
        prods = self.products("bujairami-b", "bujairami-a")
        t, cur = deep_check.select_targets(list(prods), prods, [], "", 1, "all")
        self.assertEqual(t, ["bujairami-a|eau de parfum", "bujairami-b|eau de parfum"])
        self.assertEqual(cur, "bujairami-b|eau de parfum")

    def test_nothing_sold_out_gives_no_targets(self):
        self.assertEqual(deep_check.select_targets([], {}, [], "c", 3, "both"), ([], "c"))


# ======================================================================
# verify_changes
# ======================================================================
class VerifyChangesTests(Base):
    def setUp(self):
        super().setUp()
        self.logs = []

    def log(self, msg):
        self.logs.append(msg)

    def world(self, specs, pages, old_stock=None):
        """specs: {slug: listing_in_stock}. pages: {slug: product_page kind | FakeResp}.
        old_stock: {slug: bool}; slugs missing from it are not in the old state."""
        products, old, routes = {}, {}, {}
        for slug, listed in specs.items():
            products[self.key(slug)] = self.entry(slug, in_stock=listed)
            if old_stock and slug in old_stock:
                old[self.key(slug)] = self.entry(slug, in_stock=old_stock[slug])
        for slug, page in pages.items():
            routes[purl(slug)] = FakeResp(product_page(page), 200) if isinstance(page, str) else page
        return products, old, FakeSession(routes)

    def verify(self, old, products, session, patterns=(), **kw):
        return deep_check.verify_changes(session, old, products, list(patterns), log=self.log, **kw)

    def test_watchlist_item_is_checked_even_when_nothing_changed(self):
        prods, old, s = self.world({"bujairami-foo": False}, {"bujairami-foo": "out"},
                                   {"bujairami-foo": False})
        self.assertFalse(self.verify(old, prods, s, ["foo"]))
        self.assertEqual(s.calls, [purl("bujairami-foo")])
        p = prods[self.key("bujairami-foo")]
        self.assertTrue(p["page_verified"])
        self.assertFalse(p["in_stock"])

    def test_product_page_wins_over_the_listing_for_watchlist_items(self):
        prods, old, s = self.world({"bujairami-foo": False}, {"bujairami-foo": "in"},
                                   {"bujairami-foo": False})
        self.verify(old, prods, s, ["foo"])
        self.assertTrue(prods[self.key("bujairami-foo")]["in_stock"])

    def test_listing_in_stock_but_page_out_flips_to_out(self):
        prods, old, s = self.world({"bujairami-foo": True}, {"bujairami-foo": "out"},
                                   {"bujairami-foo": False})
        self.verify(old, prods, s)
        p = prods[self.key("bujairami-foo")]
        self.assertFalse(p["in_stock"])
        self.assertTrue(p["page_verified"])
        self.assertTrue(any("listing says in, product page says out" in m for m in self.logs))

    def test_fake_restock_from_a_stale_listing_is_blocked(self):
        prods, old, s = self.world({"bujairami-foo": True}, {"bujairami-foo": "out"},
                                   {"bujairami-foo": False})
        self.verify(old, prods, s)
        added, back = bm.diff_states(old, prods)
        self.assertEqual((added, back), ([], []))

    def test_real_restock_passes(self):
        prods, old, s = self.world({"bujairami-foo": True}, {"bujairami-foo": "in"},
                                   {"bujairami-foo": False})
        self.verify(old, prods, s)
        added, back = bm.diff_states(old, prods)
        self.assertEqual([p["slug"] for p in back], ["bujairami-foo"])
        self.assertTrue(prods[self.key("bujairami-foo")]["page_verified"])

    def test_new_listing_is_verified_before_it_can_alert(self):
        prods, old, s = self.world({"bujairami-new": True}, {"bujairami-new": "out"})
        self.verify(old, prods, s)
        self.assertEqual(s.calls, [purl("bujairami-new")])
        p = prods[self.key("bujairami-new")]
        self.assertFalse(p["in_stock"])
        self.assertTrue(p["page_verified"])

    def test_period_watchlist_entry_still_checks_a_hyphen_slug_product(self):
        slug = "bujairami-drip-no-7"
        prods, old, s = self.world({slug: False}, {slug: "in"}, {slug: False})
        self.verify(old, prods, s, ["bujairami-drip-no.7"])
        self.assertEqual(s.calls, [purl(slug)])          # unchanged, so only the watchlist explains the fetch
        self.assertTrue(prods[self.key(slug)]["in_stock"])
        self.assertTrue(prods[self.key(slug)]["page_verified"])

    def test_period_watchlist_entry_is_selected_for_rotation_priority(self):
        key = bm._key("bujairami-drip-no-7", "eau de parfum")
        other = bm._key("bujairami-aaa", "eau de parfum")
        prods = {key: {"slug": "bujairami-drip-no-7", "name": "", "concentration": "eau de parfum"},
                 other: {"slug": "bujairami-aaa", "name": "", "concentration": "eau de parfum"}}
        t, _ = deep_check.select_targets(list(prods), prods, ["bujairami-drip-no.7"], "", 1, "watchlist")
        self.assertEqual(t, [key])

    def test_unchanged_non_watchlist_item_is_never_fetched(self):
        prods, old, s = self.world({"bujairami-foo": True, "bujairami-bar": False},
                                   {}, {"bujairami-foo": True, "bujairami-bar": False})
        self.assertFalse(self.verify(old, prods, s))
        self.assertEqual(s.calls, [])
        for p in prods.values():
            self.assertNotIn("page_verified", p)

    def test_recent_non_watchlist_verdict_is_reused_from_the_cache(self):
        key = self.key("bujairami-foo")
        self.write(self.deep_path, json.dumps(
            {"cursor": "", "items": {key: {"stock": "in", "checked": int(time.time()) - 60}}}))
        prods, old, s = self.world({"bujairami-foo": False}, {}, {"bujairami-foo": False})
        # old says out, listing says out -> unchanged; make it changed instead
        old[key]["in_stock"] = True
        self.verify(old, prods, s)
        self.assertEqual(s.calls, [])
        self.assertTrue(prods[key]["in_stock"])          # the cached "in" verdict decides
        self.assertTrue(prods[key]["page_verified"])

    def test_expired_cache_is_fetched_again(self):
        key = self.key("bujairami-foo")
        self.write(self.deep_path, json.dumps(
            {"cursor": "", "items": {key: {"stock": "in", "checked": int(time.time()) - 10 * 60}}}))
        prods, old, s = self.world({"bujairami-foo": False}, {"bujairami-foo": "out"},
                                   {"bujairami-foo": True})
        self.verify(old, prods, s)
        self.assertEqual(s.calls, [purl("bujairami-foo")])
        self.assertFalse(prods[key]["in_stock"])

    def test_watchlist_items_ignore_the_cache_but_reuse_a_verdict_from_this_run(self):
        key = self.key("bujairami-foo")
        self.write(self.deep_path, json.dumps(
            {"cursor": "", "items": {key: {"stock": "in", "checked": int(time.time()) - 60}}}))
        prods, old, s = self.world({"bujairami-foo": False}, {"bujairami-foo": "out"},
                                   {"bujairami-foo": False})
        self.verify(old, prods, s, ["foo"])
        self.assertEqual(len(s.calls), 1)                 # cache ignored for the watchlist
        # a verdict stamped inside this run (run_deep_checks) is trusted, no second fetch
        s2 = FakeSession({})
        self.verify(old, prods, s2, ["foo"], started=time.time() - 5)
        self.assertEqual(s2.calls, [])

    def test_block_stops_the_sweep_and_returns_true(self):
        prods, old, s = self.world({"bujairami-a": True, "bujairami-b": True},
                                   {"bujairami-a": FakeResp("", 403), "bujairami-b": "out"},
                                   {"bujairami-a": False, "bujairami-b": False})
        blocked = self.verify(old, prods, s)
        self.assertTrue(blocked)
        self.assertEqual(s.calls, [purl("bujairami-a")])
        for p in prods.values():
            self.assertTrue(p["in_stock"])               # listing answer untouched
            self.assertNotIn("page_verified", p)

    def test_unknown_verdict_leaves_the_listing_answer(self):
        prods, old, s = self.world({"bujairami-foo": True}, {"bujairami-foo": "unknown"},
                                   {"bujairami-foo": False})
        self.assertFalse(self.verify(old, prods, s))
        p = prods[self.key("bujairami-foo")]
        self.assertTrue(p["in_stock"])
        self.assertNotIn("page_verified", p)
        self.assertEqual(deep_check.load_deep()["items"], {})
        self.assertTrue(any("unknown" in m for m in self.logs))

    def test_persistent_404_leaves_the_listing_answer(self):
        prods, old, s = self.world({"bujairami-foo": True}, {"bujairami-foo": FakeResp("", 404)},
                                   {"bujairami-foo": False})
        self.verify(old, prods, s)
        self.assertTrue(prods[self.key("bujairami-foo")]["in_stock"])

    def test_verdicts_are_saved_to_the_deep_file(self):
        prods, old, s = self.world({"bujairami-foo": True}, {"bujairami-foo": "out"},
                                   {"bujairami-foo": False})
        self.verify(old, prods, s)
        item = deep_check.load_deep()["items"][self.key("bujairami-foo")]
        self.assertEqual(item["stock"], "out")
        self.assertAlmostEqual(item["checked"], time.time(), delta=5)

    def test_watchlist_items_are_checked_before_other_changes(self):
        prods, old, s = self.world({"bujairami-a": True, "bujairami-w": True},
                                   {"bujairami-a": "in", "bujairami-w": "in"},
                                   {"bujairami-a": False, "bujairami-w": False})
        self.verify(old, prods, s, ["bujairami-w"])
        self.assertEqual(s.calls, [purl("bujairami-w"), purl("bujairami-a")])

    def test_time_budget_stops_fetching_and_keeps_the_listing(self):
        prods, old, s = self.world({"bujairami-foo": True}, {"bujairami-foo": "out"},
                                   {"bujairami-foo": False})
        with mock.patch.object(deep_check, "VERIFY_BUDGET_SEC", -1):
            self.verify(old, prods, s)
        self.assertEqual(s.calls, [])
        self.assertTrue(prods[self.key("bujairami-foo")]["in_stock"])
        self.assertTrue(any("time budget" in m for m in self.logs))


# ======================================================================
# run_deep_checks
# ======================================================================
class RunDeepChecksTests(Base):
    def setUp(self):
        super().setUp()
        self.logs = []

    def run_deep(self, products, session, patterns=(), batch=2, mode="both"):
        return deep_check.run_deep_checks(session, products, list(patterns), batch=batch,
                                          mode=mode, log=self.logs.append)

    def listing(self, *slugs, stock=None):
        stock = stock or {}
        return {self.key(s): self.entry(s, in_stock=stock.get(s, False)) for s in slugs}

    def routes(self, **kinds):
        return FakeSession({purl("bujairami-" + k): (v if isinstance(v, FakeResp) else
                                                     FakeResp(product_page(v), 200))
                            for k, v in kinds.items()})

    def test_restock_found_on_the_product_page_flags_the_listing_entry(self):
        prods = self.listing("bujairami-a", "bujairami-b", "bujairami-c")
        s = self.routes(a="in", b="out", c="out")
        restocked, blocked = self.run_deep(prods, s)
        self.assertFalse(blocked)
        self.assertEqual([p["slug"] for p in restocked], ["bujairami-a"])
        a = prods[self.key("bujairami-a")]
        self.assertTrue(a["in_stock"])
        self.assertTrue(a["deep_verified"])
        self.assertFalse(prods[self.key("bujairami-b")]["in_stock"])
        self.assertEqual(s.calls, [purl("bujairami-a"), purl("bujairami-b")])   # batch of 2
        deep = deep_check.load_deep()
        self.assertEqual(deep["cursor"], self.key("bujairami-b"))
        self.assertEqual(deep["items"][self.key("bujairami-a")]["stock"], "in")

    def test_in_verdict_is_sticky_until_a_later_check_says_sold_out(self):
        s1 = self.routes(a="in", b="out", c="out")
        self.run_deep(self.listing("bujairami-a", "bujairami-b", "bujairami-c"), s1)
        # run 2: rotation reaches c and wraps to a, a's page now 404s (unknown)
        prods = self.listing("bujairami-a", "bujairami-b", "bujairami-c")
        s2 = self.routes(a=FakeResp("", 404), c="out")
        restocked, _ = self.run_deep(prods, s2)
        self.assertEqual(restocked, [])                      # not a new restock: no duplicate alert
        a = prods[self.key("bujairami-a")]
        self.assertTrue(a["in_stock"] and a["deep_verified"])
        # run 3: a is checked again and the page says sold out -> decays
        prods = self.listing("bujairami-a", "bujairami-b", "bujairami-c")
        s3 = self.routes(a="out", b="out", c="out")
        self.run_deep(prods, s3, batch=3)
        self.assertFalse(prods[self.key("bujairami-a")]["in_stock"])

    def test_block_aborts_the_sweep_and_resumes_at_the_blocked_product(self):
        prods = self.listing("bujairami-a", "bujairami-b", "bujairami-c")
        s = self.routes(a=FakeResp("", 403), b="in")
        restocked, blocked = self.run_deep(prods, s)
        self.assertTrue(blocked)
        self.assertEqual(restocked, [])
        self.assertEqual(s.calls, [purl("bujairami-a")])
        self.assertEqual(deep_check.load_deep()["cursor"], self.key("bujairami-a"))

    def test_watchlist_mode_only_checks_watchlist_products(self):
        prods = self.listing("bujairami-a", "bujairami-star")
        s = self.routes(star="in", a="in")
        restocked, _ = self.run_deep(prods, s, patterns=["star"], mode="watchlist")
        self.assertEqual(s.calls, [purl("bujairami-star")])
        self.assertEqual([p["slug"] for p in restocked], ["bujairami-star"])

    def test_nothing_sold_out_means_no_fetches(self):
        prods = self.listing("bujairami-a", stock={"bujairami-a": True})
        s = self.routes()
        self.assertEqual(self.run_deep(prods, s), ([], False))
        self.assertEqual(s.calls, [])

    def test_stale_in_verdict_is_dropped_once_the_listing_says_in_stock(self):
        ka = self.key("bujairami-a")
        self.write(self.deep_path, json.dumps(
            {"cursor": "", "items": {ka: {"stock": "in", "checked": int(time.time())}}}))
        prods = self.listing("bujairami-a", "bujairami-b", stock={"bujairami-a": True})
        self.run_deep(prods, self.routes(b="out"))
        self.assertNotIn(ka, deep_check.load_deep()["items"])

    def test_week_old_verdicts_for_gone_products_are_forgotten(self):
        gone, recent = self.key("bujairami-gone"), self.key("bujairami-recent")
        now = int(time.time())
        self.write(self.deep_path, json.dumps({"cursor": "", "items": {
            gone: {"stock": "out", "checked": now - 8 * 86400},
            recent: {"stock": "out", "checked": now - 86400}}}))
        self.run_deep(self.listing("bujairami-a"), self.routes(a="out"))
        items = deep_check.load_deep()["items"]
        self.assertNotIn(gone, items)
        self.assertIn(recent, items)


if __name__ == "__main__":
    unittest.main()
