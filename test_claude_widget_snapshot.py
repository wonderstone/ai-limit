"""The Claude quota API must reuse the widget snapshot when it is provably fresh.

All fixtures are synthetic: temp cache files, mocked probes, no network.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import tempfile
import unittest
from unittest import mock

from ai_limit import api_server
from test_widget_layout import menubar

NOW = dt.datetime.now(dt.timezone.utc)


def _iso(delta_seconds):
    return (NOW + dt.timedelta(seconds=delta_seconds)).isoformat().replace("+00:00", "Z")


def _claude(**over):
    entry = {
        "5h_left": 42, "7d_left": 17,
        "5h_reset": _iso(3600), "7d_reset": _iso(86400),
        "weekly_models": [{"model": "Fable", "window": "weekly", "used_percent": 30,
                           "remaining_percent": 70, "reset_time": _iso(86400),
                           "severity": "normal", "is_active": True}],
        "cloud_session_credits": None,
        "plan": "Max", "api_credit": {"secret": "do-not-leak"}, "source": "browser",
        "observed_at": _iso(-120),
    }
    entry.update(over)
    return entry


LIVE_USAGE = {"five_hour": {"utilization": 10.0, "resets_at": _iso(3600)},
              "seven_day": {"utilization": 20.0, "resets_at": _iso(86400)}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name) / "cache.json"
        p = mock.patch.object(api_server, "_CLAUDE_WIDGET_CACHE_PATH", self.path)
        p.start()
        self.addCleanup(p.stop)

    def write(self, claude, **top):
        self.path.write_text(json.dumps({"cached_at": NOW.timestamp(), "claude": claude, **top}))

    def payload(self, usage=LIVE_USAGE, side_effect=None):
        live = mock.Mock(return_value=usage, side_effect=side_effect)
        with mock.patch.object(api_server, "live_claude_usage", live), \
                mock.patch.object(api_server, "live_claude_plan", return_value="Max"):
            result = api_server._claude_payload()
        return result, live


class WidgetHitTests(_Base):
    def test_fresh_snapshot_hits_without_network(self):
        claude = _claude()
        self.write(claude)
        result, live = self.payload()
        live.assert_not_called()
        self.assertTrue(result["available"])
        self.assertEqual((result["5h_left"], result["7d_left"]), (42, 17))
        self.assertEqual(result["observed_at"], claude["observed_at"])
        self.assertEqual(result["5h_reset"], claude["5h_reset"])
        self.assertEqual(result["7d_left_by_model"], {"Fable": 70})
        self.assertEqual(result["cache"]["status"], "widget-hit")
        self.assertEqual(result["cache"]["observed_at"], claude["observed_at"])

    def test_projection_is_allowlisted(self):
        self.write(_claude(token="sekret", extra="x"))
        result, _ = self.payload()
        self.assertIsNone(result["api_credit"])
        self.assertNotIn("token", result)
        self.assertNotIn("extra", result)
        self.assertNotIn("sekret", json.dumps(result))
        self.assertNotIn("do-not-leak", json.dumps(result))

    def test_fresh_zero_stays_zero_without_fetching(self):
        self.write(_claude(**{"5h_left": 0, "7d_left": 0}))
        result, live = self.payload()
        live.assert_not_called()
        self.assertEqual((result["5h_left"], result["7d_left"]), (0, 0))

    def test_float_percent_and_missing_reset_ok(self):
        self.write(_claude(**{"5h_left": 12.5, "5h_reset": None}))
        result, live = self.payload()
        live.assert_not_called()
        self.assertEqual(result["5h_left"], 12.5)


class FallbackTests(_Base):
    def assert_fallback(self, claude=None, raw=None, **top):
        if raw is not None:
            self.path.write_bytes(raw)
        elif claude is not None:
            self.write(claude, **top)
        result, live = self.payload()
        self.assertEqual(live.call_count, 1)
        self.assertEqual((result["5h_left"], result["7d_left"]), (90, 80))
        self.assertNotIn("cache", result)

    def test_missing_file(self):
        self.assert_fallback()

    def test_cached_at_cannot_prove_freshness(self):
        claude = _claude()
        del claude["observed_at"]
        self.assert_fallback(claude)

    def test_old_snapshot(self):
        self.assert_fallback(_claude(observed_at=_iso(-901)))

    def test_boundary_900_is_accepted(self):
        self.write(_claude(observed_at=_iso(-890)))
        _, live = self.payload()
        live.assert_not_called()

    def test_future_or_naive_or_garbage_timestamp(self):
        for bad in (_iso(300), "2026-11-05T10:00:00", "nope", 12345, None, True):
            with self.subTest(bad=bad):
                self.assert_fallback(_claude(observed_at=bad))

    def test_elapsed_or_invalid_resets(self):
        for key in ("5h_reset", "7d_reset"):
            for bad in (_iso(-5), "garbage", "2026-11-05T10:00:00", 5):
                with self.subTest(key=key, bad=bad):
                    self.assert_fallback(_claude(**{key: bad}))

    def test_elapsed_model_reset(self):
        models = _claude()["weekly_models"]
        models[0]["reset_time"] = _iso(-5)
        self.assert_fallback(_claude(weekly_models=models))

    def test_bad_percent(self):
        for bad in (None, True, "50", -1, 101, float("nan"), float("inf")):
            with self.subTest(bad=bad):
                self.assert_fallback(_claude(**{"7d_left": bad}))

    def test_huge_int_percent(self):
        for key in ("5h_left", "7d_left"):
            for bad in (10 ** 400, -10 ** 400):
                with self.subTest(key=key, bad=bad):
                    self.assert_fallback(_claude(**{key: bad}))
        models = _claude()["weekly_models"]
        models[0]["remaining_percent"] = 10 ** 400
        self.assert_fallback(_claude(weekly_models=models))

    def test_error_and_stale_flags(self):
        for extra in ({"error": "boom"}, {"stale_reason": "keychain_locked"},
                      {"stale_reason": "browser_key_unavailable"},
                      {"keychain_locked": True}, {"browser_key_unavailable": True}):
            with self.subTest(extra=extra):
                self.assert_fallback(_claude(**extra))

    def test_wrong_source(self):
        self.assert_fallback(_claude(source="snapshot"))

    def test_malformed_and_oversized_and_wrong_shape(self):
        self.assert_fallback(raw=b"{not json")
        self.assert_fallback(raw=b"[]")
        self.assert_fallback(raw=json.dumps({"claude": "x"}).encode())
        big = json.dumps({"claude": _claude(), "pad": "x" * (1024 * 1024)}).encode()
        self.assert_fallback(raw=big)

    def test_non_regular_file(self):
        self.path.mkdir()
        self.assert_fallback()

    def test_genuine_fetch_failure_is_unavailable(self):
        self.write(_claude(observed_at=_iso(-5000)))
        result, live = self.payload(side_effect=api_server.ClaudeWebError("down"))
        self.assertEqual(live.call_count, 1)
        self.assertFalse(result["available"])
        self.assertIn("down", result["error"])


class MenubarStampTests(unittest.TestCase):
    def test_fetch_stamps_utc_observed_at(self):
        before = dt.datetime.now(dt.timezone.utc)
        with mock.patch.object(menubar, "live_claude_usage", return_value=LIVE_USAGE), \
                mock.patch.object(menubar, "live_claude_plan", return_value="Max"):
            result = menubar._fetch_claude("en")
        after = dt.datetime.now(dt.timezone.utc)
        stamp = dt.datetime.fromisoformat(result["observed_at"].replace("Z", "+00:00"))
        self.assertIsNotNone(stamp.tzinfo)
        self.assertTrue(before <= stamp <= after)
        self.assertEqual(result["5h_left"], 90)

    def test_failed_fetch_has_no_stamp(self):
        with mock.patch.object(menubar, "live_claude_usage", side_effect=OSError("x")):
            self.assertNotIn("observed_at", menubar._fetch_claude("en"))

    def test_keychain_stale_retention_keeps_original_stamp(self):
        previous = _claude()
        candidate = {"error": "locked", "keychain_locked": True}
        kept = menubar._keep_through_keychain_lock(previous, candidate)
        self.assertEqual(kept["observed_at"], previous["observed_at"])
        self.assertEqual(kept["stale_reason"], "keychain_locked")

    def test_save_cache_keeps_stamp_and_cached_at_is_separate(self):
        claude = _claude(observed_at="2026-01-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "c.json"
            with mock.patch.object(menubar, "_CACHE_PATH", path):
                menubar._save_cache(claude, {"codex": None})
                menubar._save_cache(claude, {"codex": {"5h_left": 1}})
            saved = json.loads(path.read_text())
        self.assertEqual(saved["claude"]["observed_at"], "2026-01-01T00:00:00Z")

    def test_old_cache_without_stamp_still_loads(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "c.json"
            old = {"cached_at": dt.datetime.now().timestamp(),
                   "claude": {"5h_left": 5, "7d_left": 6, "source": "browser"}}
            path.write_text(json.dumps(old))
            with mock.patch.object(menubar, "_CACHE_PATH", path):
                claude, _ = menubar._load_cache()
        self.assertEqual(claude["5h_left"], 5)


if __name__ == "__main__":
    unittest.main()
