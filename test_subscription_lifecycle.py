"""Entitlement expiry/recovery without changing the user's enabled services."""
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from ai_limit import providers
from test_widget_layout import menubar


def gemini(tier, used=0):
    return providers._guard_gemini_entitlement(providers._parse_gemini_app_usage_rpc([
        [tier, [[600, used, 1, [[1792448010, 0]]]], False, [[True]]]
    ]))


def copilot(plan, remaining=100):
    return providers._normalize_copilot_quota({
        "copilot_plan": plan,
        "quota_snapshots": {"premium_interactions": {
            "percent_remaining": remaining, "remaining": remaining,
            "entitlement": 100, "has_quota": True,
        }},
    })


class SubscriptionLifecycleTests(unittest.TestCase):
    def test_gemini_known_and_unknown_tiers(self):
        for tier, expected in [(1, "inactive"), (2, "active"), (3, "active"),
                               (4, "active"), (6, "active"), (None, "unknown"),
                               (True, "unknown"), (99, "unknown"), ("2", "unknown")]:
            with self.subTest(tier=tier):
                result = gemini(tier)
                self.assertEqual(result["subscription_status"], expected)
                self.assertEqual(result["available"], expected == "active")
                self.assertEqual(result["summary"]["remaining_percent"],
                                 100 if expected == "active" else None)

    def test_exhausted_paid_quota_is_still_active(self):
        self.assertTrue(gemini(2, 1)["available"])
        self.assertEqual(gemini(2, 1)["summary"]["remaining_percent"], 0)
        self.assertEqual(copilot("individual_pro", 0)["subscription_status"], "active")

    def test_copilot_plans(self):
        for plan, expected in [("individual_max", "active"), ("business", "active"),
                               ("enterprise", "active"), ("student", "active"),
                               ("free", "inactive"), ("free_limited_copilot", "inactive"),
                               (None, "unknown"), ("future_plan", "unknown")]:
            with self.subTest(plan=plan):
                result = copilot(plan)
                self.assertEqual(result["subscription_status"], expected)
                self.assertEqual(result["available"], expected == "active")

    def test_expiry_and_resubscription_reach_ui_for_both_providers(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "en", "services": ["gemini", "copilot"]}
        for paid in (True, False, True):
            with self.subTest(paid=paid):
                with mock.patch.object(menubar, "live_gemini_app_usage",
                                       return_value=(None, gemini(2 if paid else 1))), \
                     mock.patch.object(menubar, "live_copilot_quota",
                                       return_value=(None, copilot("individual_pro" if paid else "free"))):
                    app._gemini = menubar._fetch_gemini_app("en")
                    app._copilot = menubar._fetch_copilot("en")
                self.assertEqual(bool(app._widget_gemini_entries()), paid)
                self.assertEqual(bool(app._widget_copilot_entries()), paid)
                self.assertEqual(app._state["services"], ["gemini", "copilot"])

    def test_copilot_404_inactive_but_auth_failure_is_error(self):
        for code in (404, 401, 403, 500):
            with self.subTest(code=code), \
                 mock.patch.object(providers, "load_copilot_github_token", return_value=("test", "test")), \
                 mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(
                     "https://api.github.com/copilot_internal/user", code, "test", {}, None)):
                if code == 404:
                    _, result = providers.live_copilot_quota()
                    self.assertEqual(result["subscription_status"], "inactive")
                    self.assertFalse(result["available"])
                else:
                    with self.assertRaises((providers.CopilotAuthError, providers.CopilotQuotaError)):
                        providers.live_copilot_quota()

    def test_free_with_no_quota_is_not_an_error(self):
        self.assertFalse(providers._normalize_copilot_quota({"copilot_plan": "free"})["available"])
        result = providers._guard_gemini_entitlement(providers._parse_gemini_app_usage_rpc([[1, []]]))
        self.assertEqual(result["subscription_status"], "inactive")

    def test_gemini_credit_balance_is_not_a_percentage(self):
        result = providers._parse_gemini_app_usage_rpc([
            [2, [[600, 0.25, 1, [[1792448010, 0]]], [5000, 0, 3, None]]]
        ])
        self.assertEqual(len(result["buckets"]), 1)
        self.assertEqual(result["summary"]["remaining_percent"], 75)

    def test_gemini_cache_version_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "cache.json"
            with mock.patch.object(providers, "GEMINI_APP_USAGE_CACHE", path):
                providers._save_gemini_app_usage_cache(gemini(2))
                result, _ = providers._load_gemini_app_usage_cache()
                self.assertEqual(result["subscription_status"], "active")
                old = json.loads(path.read_text())
                old.pop("schema_version")
                path.write_text(json.dumps(old))
                self.assertEqual(providers._load_gemini_app_usage_cache(), (None, None))

    def test_cache_expiry_fetches_new_entitlement(self):
        with mock.patch.object(providers, "_load_gemini_app_usage_cache", return_value=(None, None)), \
             mock.patch.object(providers, "_live_gemini_app_usage", side_effect=[gemini(1), gemini(2)]):
            self.assertEqual(providers.live_gemini_app_usage()[1]["subscription_status"], "inactive")
            self.assertEqual(providers.live_gemini_app_usage()[1]["subscription_status"], "active")
