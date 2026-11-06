import datetime
import io
import json
import unittest
import urllib.error
from unittest import mock

from ai_limit import api_server, providers
from ai_limit.providers import (
    CloudflareAuthError,
    CloudflareCreditsError,
    live_cloudflare_credits,
)
from test_widget_layout import menubar

NOW = datetime.datetime(2026, 11, 6, 1, 0, tzinfo=datetime.timezone.utc)
ACCOUNT = "ffe885b244e1ba3ebdf7f28e1f858195"


def _ok(result):
    return {"success": True, "result": result, "errors": [], "messages": []}


BALANCE = _ok({"balance": 2500, "topup_config": {"threshold": 0, "amount": 0}})
SPENDING = _ok({"enabled": False, "config": {"amount": 10, "duration": "monthly", "strategy": "fixed"}})
ACCOUNTS = _ok([{"id": ACCOUNT, "name": "Personal"}])


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _respond(routes):
    def urlopen(request, timeout=None):
        for suffix, payload in routes.items():
            if request.full_url.endswith(suffix):
                if isinstance(payload, Exception):
                    raise payload
                return _Response(json.dumps(payload).encode())
        raise AssertionError(f"unexpected URL {request.full_url}")

    return urlopen


def _http_error(code):
    return urllib.error.HTTPError("https://dash.cloudflare.com", code, "err", {}, io.BytesIO(b"{}"))


class LiveCloudflareTests(unittest.TestCase):
    def test_discovers_account_and_reads_cents_as_dollars(self):
        routes = {"/accounts?per_page=50": ACCOUNTS, "/credit-balance": BALANCE, "/spending-limit": SPENDING}
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(providers, "_cloudflare_cookie_sets", return_value=[("Chrome/Default", "vses2=x")]), \
                mock.patch("urllib.request.urlopen", side_effect=_respond(routes)):
            _ts, data = live_cloudflare_credits()
        self.assertEqual(data["total_usd"], 25.0)
        self.assertTrue(data["available"])
        account = data["accounts"][0]
        self.assertEqual((account["account_id"], account["balance_usd"]), (ACCOUNT, 25.0))
        self.assertFalse(account["spending_limit_enabled"])
        self.assertIsNone(account["spending_limit_amount"])

    def test_expired_profile_falls_through_to_signed_in_one(self):
        def urlopen(request, timeout=None):
            if "expired" in request.get_header("Cookie"):
                raise _http_error(400)
            return _respond({"/accounts?per_page=50": ACCOUNTS, "/credit-balance": BALANCE, "/spending-limit": SPENDING})(request)

        sets = [("Chrome/Profile 1", "vses2=expired"), ("Chrome/Default", "vses2=live")]
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(providers, "_cloudflare_cookie_sets", return_value=sets), \
                mock.patch("urllib.request.urlopen", side_effect=urlopen):
            _ts, data = live_cloudflare_credits()
        self.assertEqual(data["browser_profile"], "Chrome/Default")

    def test_pinned_account_skips_discovery(self):
        routes = {"/credit-balance": BALANCE, "/spending-limit": SPENDING}
        with mock.patch.dict("os.environ", {"CLOUDFLARE_ACCOUNT_ID": ACCOUNT}), \
                mock.patch.object(providers, "_cloudflare_cookie_sets", return_value=[("Chrome/Default", "vses2=x")]), \
                mock.patch("urllib.request.urlopen", side_effect=_respond(routes)) as urlopen:
            _ts, data = live_cloudflare_credits()
        self.assertEqual(data["total_usd"], 25.0)
        self.assertFalse(any("/accounts?" in call.args[0].full_url for call in urlopen.call_args_list))

    def test_all_sessions_expired_is_an_auth_error(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(providers, "_cloudflare_cookie_sets", return_value=[("Chrome/Default", "vses2=x")]), \
                mock.patch("urllib.request.urlopen", side_effect=_http_error(403)):
            with self.assertRaises(CloudflareAuthError):
                live_cloudflare_credits()

    def test_spending_limit_failure_keeps_balance(self):
        routes = {"/accounts?per_page=50": ACCOUNTS, "/credit-balance": BALANCE, "/spending-limit": _http_error(500)}
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(providers, "_cloudflare_cookie_sets", return_value=[("Chrome/Default", "vses2=x")]), \
                mock.patch("urllib.request.urlopen", side_effect=_respond(routes)):
            _ts, data = live_cloudflare_credits()
        self.assertEqual(data["total_usd"], 25.0)


class CloudflareApiServerTests(unittest.TestCase):
    def test_payload_and_missing_session(self):
        data = {"source": "dash.cloudflare.com ai-gateway/billing/credit-balance", "total_usd": 25.0,
                "available": True, "accounts": [{"account_id": ACCOUNT, "balance_usd": 25.0}]}
        with mock.patch.object(api_server, "has_cloudflare_cookies", return_value=True), \
                mock.patch.object(api_server, "live_cloudflare_credits", return_value=(NOW, data)):
            payload = api_server._cloudflare_payload()
        self.assertEqual(payload["total_usd"], 25.0)
        with mock.patch.object(api_server, "has_cloudflare_cookies", return_value=False):
            self.assertIsNone(api_server._cloudflare_payload())
        with mock.patch.object(api_server, "live_cloudflare_credits", side_effect=CloudflareCreditsError("down")):
            payload = api_server._cloudflare_payload(require_session=True)
        self.assertFalse(payload["available"])


class CloudflareMenubarTests(unittest.TestCase):
    def test_widget_lists_cloudflare_credits(self):
        data = {"source": "x", "total_usd": 25.0, "available": True,
                "accounts": [{"account_id": ACCOUNT, "balance_usd": 25.0, "spending_limit_enabled": False}]}
        with mock.patch.object(menubar, "live_cloudflare_credits", return_value=(NOW, data)):
            cloudflare = menubar._fetch_cloudflare("zh")
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "zh", "global": "7d", "services": ["cloudflare"], "widget": True}
        app._claude = app._codex = app._deepseek = app._google = app._gemini = app._copilot = app._grok = None
        app._api_credits_v1 = None
        app._cloudflare = cloudflare
        rows = app._widget_detail_rows()
        credit = [row for row in rows if row.get("name") == "AI 额度"]
        self.assertEqual(credit[0]["value"], "$25.00")
        self.assertIn("额度: $25.00", app._widget_text_body())
        self.assertIn("cloudflare", menubar._SERVICES)


if __name__ == "__main__":
    unittest.main()
