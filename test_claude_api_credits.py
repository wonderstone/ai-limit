"""Tests for the Claude Platform API credit (platform.claude.com/settings/billing)."""

from __future__ import annotations

import unittest
from unittest import mock

from ai_limit import providers
from test_widget_layout import _Panel, menubar


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


class _Cookie:
    def __init__(self, name, value="v"):
        self.name = name
        self.value = value


class ProfileScanTest(unittest.TestCase):
    """The console session may live in any Chrome profile."""

    def _run(self, jars, firefox=()):
        sources = [(f"Chrome/{name}", name) for name in jars]

        def chrome(cookie_file=None, domain_name=None):
            result = jars[cookie_file]
            if isinstance(result, Exception):
                raise result
            return [_Cookie(name) for name in result]

        cookie_module = mock.MagicMock(chrome=chrome, firefox=mock.MagicMock(return_value=[_Cookie(n) for n in firefox]))
        with mock.patch.dict("sys.modules", {"browser_cookie3": cookie_module}), \
                mock.patch.object(providers, "_chromium_profile_cookie_files", return_value=sources):
            return providers._claude_platform_headers()

    def test_session_in_a_second_profile_is_found(self):
        # Default only carries analytics cookies from a past visit.
        headers = self._run({"Default": ["_dd_s_v2", "anthropic-device-id"], "Profile 1": ["sessionKey", "__cf_bm"]})

        self.assertIn("sessionKey=", headers["Cookie"])

    def test_no_session_anywhere_means_nothing_to_show(self):
        self.assertIsNone(self._run({"Default": ["_dd_s_v2"], "Profile 1": []}))

    def test_locked_keychain_is_reported_not_hidden(self):
        locked = Exception(providers.CHROME_KEYCHAIN_LOCKED_MARKER)
        with mock.patch.object(providers, "default_keychain_locked", return_value=True), self.assertRaises(providers.ClaudeWebError) as ctx:
            self._run({"Default": locked, "Profile 1": locked})

        self.assertTrue(getattr(ctx.exception, "keychain_locked", False))


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

    def test_credit_value_gets_the_wide_column_and_percent_rows_do_not(self):
        credit = providers._normalize_claude_api_credits(API_ORG, CREDITS)
        app = self._app({"5h_left": 34, "7d_left": 67, "api_credit": credit})
        app._widget_panel = _Panel(width=640, height=500)
        app._widget_content = mock.MagicMock()
        labels = []
        with mock.patch.object(menubar.AiLimitApp, "_widget_alert_rows", return_value=[]), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_label", side_effect=lambda text, x, y, w, h, **kw: labels.append((text, w))), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_box"), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_ring"), \
                mock.patch.object(menubar.AiLimitApp, "_clear_widget_content", lambda _self: None):
            app._render_widget_dashboard()
        widths = dict(labels)
        self.assertEqual(widths["$200.00 / $200"], menubar.AiLimitApp._WIDGET_WIDE_VALUE_WIDTH)
        self.assertEqual(widths["34%"], 58)

    def test_console_failure_does_not_hide_chat_quota(self):
        usage = {"five_hour": {"utilization": 66}, "seven_day": {"utilization": 33}}
        with mock.patch.object(menubar, "live_claude_usage", return_value=usage), \
                mock.patch.object(menubar, "live_claude_plan", return_value="Max"), \
                mock.patch.object(menubar, "live_claude_api_credits", side_effect=providers.ClaudeWebError("boom")):
            data = menubar._fetch_claude("zh")
        self.assertEqual(data["5h_left"], 34)
        self.assertIsNone(data["api_credit"])

    def test_fable_weekly_limit_is_fetched_and_listed(self):
        usage = {
            "five_hour": {"utilization": 32},
            "seven_day": {"utilization": 50},
            "limits": [{
                "kind": "weekly_scoped", "percent": 69, "resets_at": "2026-10-28T08:59:59Z",
                "scope": {"model": {"id": None, "display_name": "Fable"}}, "is_active": True,
            }],
        }
        with mock.patch.object(menubar, "live_claude_usage", return_value=usage), \
                mock.patch.object(menubar, "live_claude_plan", return_value="Max"), \
                mock.patch.object(menubar, "live_claude_api_credits", return_value=None):
            data = menubar._fetch_claude("zh")
        self.assertEqual(data["weekly_models"][0]["remaining_percent"], 31)
        app = self._app(data)
        fable = [row for row in app._widget_detail_rows() if row.get("name") == "Fable weekly"]
        self.assertEqual(len(fable), 1)
        self.assertEqual(fable[0]["pct"], 31)
        self.assertIn("Fable weekly", "\n".join(app._widget_claude_lines()))


if __name__ == "__main__":
    unittest.main()
