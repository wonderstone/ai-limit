"""Quota counters must not be presented as proof of a paid subscription."""
import unittest
from unittest import mock

from ai_limit import providers
from test_widget_layout import menubar


class GeminiEntitlementTests(unittest.TestCase):
    def setUp(self):
        # Observed after cancellation: the endpoint still returns zero usage.
        self.snapshot = providers._parse_gemini_app_usage_rpc([
            [1, [[12096, 0, 2, [[1792631610, 0]]],
                 [600, 0, 1, [[1792448010, 0]]]], False, [[True]]]
        ])

    def test_zero_usage_is_not_subscription_evidence(self):
        guarded = providers._guard_gemini_entitlement(self.snapshot)
        self.assertEqual(guarded["subscription_status"], "unknown")
        self.assertFalse(guarded["available"])
        self.assertIsNone(guarded["summary"]["remaining_percent"])
        self.assertTrue(all(b["remaining_percent"] is None for b in guarded["buckets"]))
        self.assertEqual(guarded["summary"]["used_percent"], 0)
        self.assertEqual(self.snapshot["summary"]["remaining_percent"], 100)

    def test_fresh_and_both_cache_paths_are_guarded(self):
        for path in ("fresh", "cache", "stale"):
            with self.subTest(path=path):
                cached = [(self.snapshot, 30)] if path == "cache" else [(None, None)]
                if path == "stale":
                    cached.append((self.snapshot, 300))
                with mock.patch.object(providers, "_load_gemini_app_usage_cache", side_effect=cached), \
                     mock.patch.object(providers, "_live_gemini_app_usage",
                                       return_value=self.snapshot,
                                       side_effect=RuntimeError("offline") if path == "stale" else None):
                    _, result = providers.live_gemini_app_usage()
                self.assertFalse(result["available"])
                self.assertIsNone(result["summary"]["remaining_percent"])

    def test_old_menubar_cache_and_inactive_accounts_are_hidden(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "en", "services": ["gemini"]}
        for status in (None, "unknown", "inactive"):
            with self.subTest(status=status):
                app._gemini = {"left": 100, "available": True,
                               "buckets": self.snapshot["buckets"]}
                if status is not None:
                    app._gemini["subscription_status"] = status
                self.assertEqual(app._widget_gemini_entries(), [])
                self.assertEqual(app._widget_gemini_lines(), [])

    def test_verified_active_entitlement_keeps_quota(self):
        self.snapshot["subscription_status"] = "active"
        result = providers._guard_gemini_entitlement(self.snapshot)
        self.assertTrue(result["available"])
        self.assertEqual(result["summary"]["remaining_percent"], 100)

    def test_menubar_preserves_entitlement_status(self):
        guarded = providers._guard_gemini_entitlement(self.snapshot)
        with mock.patch.object(menubar, "live_gemini_app_usage", return_value=(None, guarded)):
            result = menubar._fetch_gemini_app("en")
        self.assertEqual(result["subscription_status"], "unknown")
        self.assertIsNone(result["left"])
