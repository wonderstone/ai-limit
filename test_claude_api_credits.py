"""Tests for the Claude Platform API credit (platform.claude.com/settings/billing)."""

from __future__ import annotations

import unittest
from unittest import mock

from ai_limit import providers
from test_widget_layout import menubar


# Trimmed from a real /prepaid/credits response for a Max 20x account, whose
# subscription grants a monthly scoped API credit.
CREDITS = {
    "amount": 20000,
    "currency": "USD",
    "balance": {"money": None, "credits": {"amount_minor": 20000, "exponent": 2}},
    "balance_credits": 200,
    "amount_without_scoped_credits": 0,
    "auto_reload_settings": None,
    "tranches": [],
    "promo_tranches": [
        {
            "remaining_amount_minor_units": 19999,
            "currency": "USD",
            "expires_at": "2026-11-11T00:00:00Z",
            "granted_amount_minor_units": 20000,
            "granted_at": "2026-10-15T07:37:04.026000Z",
            "remaining": {"money": None, "credits": {"amount_minor": 19999, "exponent": 2}},
            "granted": {"money": None, "credits": {"amount_minor": 20000, "exponent": 2}},
            "program_id": "subscription_api_credit",
            "name": "API credit (Max 20x)",
        }
    ],
    "next_expires_at": "2026-11-11T00:00:00Z",
}

CHAT_ORG = {"uuid": "chat-org", "name": "Chat", "billing_type": "stripe_subscription", "capabilities": ["claude_max", "chat"]}
API_ORG = {"uuid": "api-org", "name": "API", "billing_type": "prepaid", "capabilities": ["api", "api_individual"]}


def _fake_get(orgs, credits=CREDITS):
    def get(path, _headers, _timeout):
        if path == "/api/organizations":
            return orgs
        if path == "/api/organizations/api-org/prepaid/credits":
            return credits
        raise AssertionError(f"unexpected path {path}")

    return get


class NormalizeTest(unittest.TestCase):
    def test_reads_balance_grant_and_expiry(self):
        credit = providers._normalize_claude_api_credits(API_ORG, CREDITS)
        self.assertEqual(credit["balance"], 200.0)
        self.assertEqual(credit["granted"], 200.0)
        self.assertEqual(credit["remaining_percent"], 100)
        self.assertEqual(credit["expires_at"], "2026-11-11T00:00:00Z")
        self.assertEqual(credit["tranches"][0]["name"], "API credit (Max 20x)")
        self.assertEqual(credit["tranches"][0]["remaining"], 199.99)

    def test_partially_spent_grant_reports_remaining_percent(self):
        spent = dict(CREDITS, balance={"money": None, "credits": {"amount_minor": 5000, "exponent": 2}})
        self.assertEqual(providers._normalize_claude_api_credits(API_ORG, spent)["remaining_percent"], 25)

    def test_paid_balance_without_grants_has_no_percentage(self):
        paid = {"balance": {"money": {"amount_minor": 1234, "exponent": 2}}, "tranches": [], "promo_tranches": []}
        credit = providers._normalize_claude_api_credits(API_ORG, paid)
        self.assertEqual(credit["balance"], 12.34)
        self.assertIsNone(credit["granted"])
        self.assertIsNone(credit["remaining_percent"])

    def test_legacy_amount_in_cents(self):
        self.assertEqual(providers._normalize_claude_api_credits(API_ORG, {"amount": 750})["balance"], 7.5)


class LiveTest(unittest.TestCase):
    def test_no_console_session_means_nothing_to_show(self):
        with mock.patch.object(providers, "_claude_platform_headers", return_value=None):
            self.assertIsNone(providers.live_claude_api_credits())

    def test_account_without_api_org_means_nothing_to_show(self):
        with mock.patch.object(providers, "_claude_platform_headers", return_value={}), \
                mock.patch.object(providers, "_claude_platform_get", side_effect=_fake_get([CHAT_ORG])):
            self.assertIsNone(providers.live_claude_api_credits())

    def test_picks_the_prepaid_org_not_the_chat_subscription(self):
        with mock.patch.object(providers, "_claude_platform_headers", return_value={}), \
                mock.patch.object(providers, "_claude_platform_get", side_effect=_fake_get([CHAT_ORG, API_ORG])):
            credit = providers.live_claude_api_credits()
        self.assertEqual(credit["org_name"], "API")
        self.assertEqual(credit["balance"], 200.0)


class MenubarTest(unittest.TestCase):
    def _app(self, claude):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "zh", "global": "7d", "services": ["claude"], "widget": True}
        app._claude = claude
        app._codex = app._deepseek = app._google = app._gemini = app._copilot = app._grok = None
        return app

    def test_widget_shows_api_credit_row_under_claude(self):
        credit = providers._normalize_claude_api_credits(API_ORG, CREDITS)
        app = self._app({"5h_left": 34, "7d_left": 67, "5h_reset": None, "7d_reset": None, "api_credit": credit})
        rows = app._widget_detail_rows()
        api = [row for row in rows if row.get("name") == "API 额度"]
        self.assertEqual(len(api), 1)
        self.assertEqual(api[0]["value"], "$200.00 / $200")
        self.assertEqual(api[0]["kind"], "credit")
        self.assertIsNotNone(api[0]["reset"])

    def test_api_credit_is_never_a_low_quota_alert(self):
        empty = dict(CREDITS, balance={"money": None, "credits": {"amount_minor": 0, "exponent": 2}})
        credit = providers._normalize_claude_api_credits(API_ORG, empty)
        app = self._app({"5h_left": 90, "7d_left": 90, "api_credit": credit})
        self.assertEqual(app._widget_alert_rows(), [])

    def test_no_api_credit_adds_no_row(self):
        app = self._app({"5h_left": 34, "7d_left": 67, "api_credit": None})
        self.assertFalse([row for row in app._widget_detail_rows() if row.get("name") == "API 额度"])

    def test_console_failure_does_not_hide_chat_quota(self):
        usage = {"five_hour": {"utilization": 66}, "seven_day": {"utilization": 33}}
        with mock.patch.object(menubar, "live_claude_usage", return_value=usage), \
                mock.patch.object(menubar, "live_claude_plan", return_value="Max"), \
                mock.patch.object(menubar, "live_claude_api_credits", side_effect=providers.ClaudeWebError("boom")):
            data = menubar._fetch_claude("zh")
        self.assertEqual(data["5h_left"], 34)
        self.assertIsNone(data["api_credit"])


if __name__ == "__main__":
    unittest.main()
