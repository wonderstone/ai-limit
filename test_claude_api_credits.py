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


class PurchasedConsoleMappingTest(unittest.TestCase):
    def test_promotional_rounding_cent_does_not_become_paid_credit(self):
        raw = dict(CREDITS, amount=20000)
        self.assertEqual(providers._claude_console_purchased_balance(raw), 0.0)

    def test_mixed_paid_and_promotion_matches_console(self):
        raw = dict(CREDITS, amount=21234, tranches=[{
            "currency": "USD", "remaining_amount_minor_units": 1234,
            "expires_at": "2099-01-01T00:00:00Z",
        }])
        self.assertEqual(providers._claude_console_purchased_balance(raw), 12.34)

    def test_incomplete_data_is_unknown_not_zero(self):
        self.assertIsNone(providers._claude_console_purchased_balance({"amount": 0, "currency": "USD"}))
        self.assertIsNone(providers._claude_console_purchased_balance(dict(CREDITS, tranches=[{}])))


class LiveTest(unittest.TestCase):
    def test_no_console_session_means_nothing_to_show(self):
        with mock.patch.object(providers, "_claude_platform_headers", return_value=None):
            self.assertIsNone(providers.live_claude_api_credits())

    def test_account_without_api_org_means_nothing_to_show(self):
        with mock.patch.object(providers, "_claude_platform_headers", return_value={}), \
                mock.patch.object(providers, "_claude_platform_get", side_effect=_fake_get([CHAT_ORG])):
            with self.assertRaises(providers.ClaudeOrgBindingError):
                providers.live_claude_api_credits(organization_id="api-org")

    def test_uses_only_the_explicitly_bound_prepaid_org(self):
        with mock.patch.object(providers, "_claude_platform_headers", return_value={}), \
                mock.patch.object(providers, "_claude_platform_get", side_effect=_fake_get([CHAT_ORG, API_ORG])):
            credit = providers.live_claude_api_credits(organization_id="api-org")
        self.assertEqual(credit["org_name"], "API")
        self.assertEqual(credit["balance"], 200.0)

    def test_missing_binding_does_not_enumerate_or_select_an_org(self):
        with mock.patch.object(providers, "_claude_platform_headers") as headers, \
                mock.patch.object(providers, "_claude_platform_get") as get:
            self.assertIsNone(providers.live_claude_api_credits())
        headers.assert_not_called()
        get.assert_not_called()


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

    def test_explicit_profile_never_loads_other_chromium_profiles_or_firefox(self):
        calls = []

        def chrome(cookie_file=None, domain_name=None):
            calls.append((cookie_file, domain_name))
            value = "selected" if cookie_file == "/cookies/default" else "other-profile"
            return [_Cookie("sessionKey", value)]

        firefox = mock.Mock(return_value=[_Cookie("sessionKey")])
        cookie_module = mock.MagicMock(chrome=chrome, firefox=firefox)
        sources = [
            ("Chrome/Profile 1", "/cookies/other"),
            ("Chrome Canary/Default", "/cookies/canary"),
            ("Chrome/Default", "/cookies/default"),
        ]
        with mock.patch.dict("sys.modules", {"browser_cookie3": cookie_module}), mock.patch.object(
            providers, "_chromium_profile_cookie_files", return_value=sources
        ):
            headers = providers._claude_platform_headers(profile="Chrome/Default")

        self.assertIn("sessionKey=selected", headers["Cookie"])
        self.assertEqual(calls, [("/cookies/default", "platform.claude.com")])
        firefox.assert_not_called()

    def test_missing_explicit_profile_does_not_fall_back_to_another_browser(self):
        chrome = mock.Mock(return_value=[_Cookie("sessionKey")])
        firefox = mock.Mock(return_value=[_Cookie("sessionKey")])
        cookie_module = mock.MagicMock(chrome=chrome, firefox=firefox)
        with mock.patch.dict("sys.modules", {"browser_cookie3": cookie_module}), mock.patch.object(
            providers,
            "_chromium_profile_cookie_files",
            return_value=[("Chrome/Profile 1", "/cookies/other")],
        ):
            headers = providers._claude_platform_headers(profile="Chrome/Default")

        self.assertIsNone(headers)
        chrome.assert_not_called()
        firefox.assert_not_called()

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
        app._api_credits_v1 = None
        return app

    def test_manual_refresh_forces_api_credit_refresh(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        with mock.patch.object(menubar, "_clear_all_caches"), mock.patch.object(
            menubar.AiLimitApp,
            "_start_background_refresh",
        ) as start:
            app._force_refresh(None)
        start.assert_called_once_with(force_api_refresh=True)

    def test_widget_shows_api_credit_in_separate_section(self):
        app = self._app({"5h_left": 34, "7d_left": 67, "5h_reset": None, "7d_reset": None})
        app._api_credits_v1 = {
            "openai": {"status": "unknown", "error_code": "source_unverified", "account_identity": {}, "organization_identity": {}, "grants": None},
            "claude": {
                "status": "ok", "error_code": None, "account_identity": {"label": "Sample Claude"},
                "organization_identity": {"name": "Sample API"}, "currency": "USD",
                "promotional_balance": 199.99, "purchased_balance": None, "total": 200.0,
                "grants": [{"name": "Sample grant", "remaining": 199.99, "expires_at": "2099-01-01T00:00:00Z"}],
                "source_updated_at": None, "observed_at": "2026-10-30T00:00:00Z", "checked_at": "2026-10-30T00:00:00Z",
            },
        }
        rows = app._widget_detail_rows()
        sections = [row["name"] for row in rows if row.get("type") == "section"]
        self.assertNotIn("API 额度", sections)
        self.assertIn("Claude API", sections)
        promo = [row for row in rows if row.get("name") == "赠送余额"][-1]
        purchased = [row for row in rows if row.get("name") == "购买余额"][-1]
        self.assertEqual(promo["value"], "$199.99")
        self.assertEqual(purchased["value"], "未知")
        self.assertFalse(any(row.get("name") == "来源更新时间" for row in rows))
        self.assertFalse(any(row.get("name") == "API 额度" and row.get("type") != "section" for row in rows))

    def test_compact_api_credit_rows_hide_diagnostics_and_keep_expiry(self):
        app = self._app({"5h_left": 34, "7d_left": 67})
        app._state["lang"] = "en"
        app._api_credit_preview_label = "SYNTHETIC FIXTURE — NOT LIVE OR HISTORICAL ACCOUNT DATA"
        app._api_credits_v1 = {
            "openai": {"status": "unknown", "error_code": "source_unverified", "account_identity": {}, "organization_identity": {}, "grants": None},
            "claude": {
                "status": "ok", "account_identity": {"profile": "Chrome/Default", "label": "Sample account"},
                "organization_identity": {"name": "Sample API", "id": "org-1234567890"}, "currency": "USD",
                "promotional_balance": 199.99, "purchased_balance": None, "total": 200.0,
                "grants": [{"name": "Sample grant", "remaining": 199.99, "expires_at": "2026-11-11T00:00:00Z"}],
                "source_updated_at": "2026-10-29T23:58:00Z", "observed_at": "2026-10-30T00:00:00Z", "checked_at": "2026-10-30T00:01:00Z",
            },
        }

        rows = app._widget_detail_rows()
        sections = [row["name"] for row in rows if row.get("type") == "section"]
        self.assertTrue(any("SYNTHETIC" in name for name in sections))
        names = [row.get("name") for row in rows]
        for hidden in ("Account / organization", "Status", "Binding", "Source updated", "Last successful read", "Last checked", "Total"):
            self.assertNotIn(hidden, names)
        self.assertEqual([row["value"] for row in rows if row.get("name") == "Expires (local)"], ["Unknown", "2026-11-11"])
        self.assertEqual(sum(row.get("name") in {"Promotional", "Purchased", "Expires (local)"} for row in rows), 6)

    def test_compact_credit_dates_fit_native_widths(self):
        app = self._app({"5h_left": 34, "7d_left": 67})
        app._api_credits_v1 = {
            "openai": {"status": "unknown", "error_code": "source_unverified", "account_identity": {}, "organization_identity": {}, "grants": None},
            "claude": {
                "status": "ok", "account_identity": {"profile": "Chrome/Default", "label": "Sample account"},
                "organization_identity": {"name": "Sample API", "id": "org-1234567890"}, "currency": "USD",
                "promotional_balance": 199.99, "purchased_balance": None, "total": 200.0,
                "grants": [{"name": "Sample grant", "remaining": 199.99, "expires_at": "2026-11-11T00:00:00Z"}],
                "source_updated_at": "2026-10-29T23:58:00Z", "observed_at": "2026-10-30T00:00:00Z", "checked_at": "2026-10-30T00:01:00Z",
            },
        }

        for width in (420, 640):
            with self.subTest(width=width):
                app._widget_panel = _Panel(width=width, height=900)
                app._widget_content = mock.MagicMock()
                labels = []
                with mock.patch.object(menubar.AiLimitApp, "_widget_alert_rows", return_value=[]), \
                        mock.patch.object(menubar.AiLimitApp, "_widget_add_label", side_effect=lambda text, x, y, w, h, **kw: labels.append((str(text), y, w, h, kw))), \
                        mock.patch.object(menubar.AiLimitApp, "_widget_add_box"), \
                        mock.patch.object(menubar.AiLimitApp, "_widget_add_ring"), \
                        mock.patch.object(menubar.AiLimitApp, "_clear_widget_content", lambda _self: None):
                    app._render_widget_dashboard()

                expiry = next(label for label in labels if label[0] == "2026-11-11")
                self.assertEqual(expiry[2], menubar.AiLimitApp._WIDGET_WIDE_VALUE_WIDTH)
                self.assertFalse(any("Chrome/Default" in label[0] for label in labels))
                self.assertTrue(any(label[0] == "到期（本地）" for label in labels))

    def test_api_credit_is_never_a_low_quota_alert(self):
        app = self._app({"5h_left": 90, "7d_left": 90})
        app._api_credits_v1 = {"openai": {"status": "ok", "total": 0}, "claude": {"status": "unknown"}}
        self.assertEqual(app._widget_alert_rows(), [])

    def test_unknown_balance_keeps_its_cause_and_shows_refresh_failure_separately(self):
        label = menubar._api_credit_status_text(
            {
                "status": "unknown",
                "error_code": "balance_unreported",
                "refresh_error_code": "fetch_failed",
            },
            "en",
        )

        self.assertIn("source reported no balance", label)
        self.assertIn("latest refresh failed", label)

    def test_no_api_credit_adds_no_row(self):
        app = self._app({"5h_left": 34, "7d_left": 67, "api_credit": None})
        self.assertFalse([row for row in app._widget_detail_rows() if row.get("name") == "API 额度"])

    def test_credit_value_gets_the_wide_column_and_percent_rows_do_not(self):
        app = self._app({"5h_left": 34, "7d_left": 67})
        app._api_credits_v1 = {
            "openai": {"status": "unknown", "error_code": "source_unverified", "account_identity": {}, "organization_identity": {}, "grants": None},
            "claude": {"status": "ok", "account_identity": {}, "organization_identity": {}, "currency": "USD", "promotional_balance": 199.99, "purchased_balance": None, "total": 200.0, "grants": [], "source_updated_at": None, "observed_at": None, "checked_at": None},
        }
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
        self.assertEqual(widths["$199.99"], menubar.AiLimitApp._WIDGET_WIDE_VALUE_WIDTH)
        self.assertEqual(widths["34%"], 58)

    def test_console_failure_does_not_hide_chat_quota(self):
        usage = {"five_hour": {"utilization": 66}, "seven_day": {"utilization": 33}}
        with mock.patch.object(menubar, "live_claude_usage", return_value=usage), \
                mock.patch.object(menubar, "live_claude_plan", return_value="Max"):
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
                mock.patch.object(menubar, "live_claude_plan", return_value="Max"):
            data = menubar._fetch_claude("zh")
        self.assertEqual(data["weekly_models"][0]["remaining_percent"], 31)
        app = self._app(data)
        fable = [row for row in app._widget_detail_rows() if row.get("name") == "Fable weekly"]
        self.assertEqual(len(fable), 1)
        self.assertEqual(fable[0]["pct"], 31)
        self.assertIn("Fable weekly", "\n".join(app._widget_claude_lines()))


if __name__ == "__main__":
    unittest.main()
