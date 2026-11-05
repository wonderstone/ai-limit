import datetime
import io
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from ai_limit import api_server, providers
from ai_limit.providers import (
    VeniceAuthError,
    VeniceError,
    _normalize_venice_balance,
    live_venice_balance,
    load_venice_api_key,
)
from test_widget_layout import menubar

NOW = datetime.datetime(2026, 11, 5, 8, 0, tzinfo=datetime.timezone.utc)

BILLING = {
    "canConsume": True,
    "consumptionCurrency": "USD",
    "balances": {"diem": None, "usd": 25, "bundledCredits": 0, "earnedCredits": 0},
    "diemEpochAllocation": 0,
}
RATE_LIMITS = {
    "data": {
        "accessPermitted": True,
        "apiTier": {"id": "paid", "isCharged": True},
        "balances": {"USD": 25.0, "DIEM": 0, "BUNDLED_CREDITS": 0},
    }
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    return urllib.error.HTTPError("https://api.venice.ai", code, "err", {}, io.BytesIO(b"{}"))


class NormalizeVeniceTests(unittest.TestCase):
    def test_billing_balance_totals_usd_credits(self):
        data = _normalize_venice_balance(
            {**BILLING, "balances": {"usd": 25, "bundledCredits": 10, "earnedCredits": 5, "diem": 40}, "diemEpochAllocation": 100},
            "billing/balance",
        )
        self.assertEqual(data["total_usd"], 40)
        self.assertEqual(data["usd"], 25)
        self.assertEqual(data["diem_remaining_percent"], 40)
        self.assertTrue(data["available"])

    def test_rate_limits_shape_is_understood(self):
        data = _normalize_venice_balance(RATE_LIMITS, "api_keys/rate_limits")
        self.assertEqual(data["total_usd"], 25.0)
        self.assertEqual(data["api_tier"], "paid")
        self.assertEqual(data["source"], "api.venice.ai api_keys/rate_limits")

    def test_missing_balances_raise(self):
        with self.assertRaises(VeniceError):
            _normalize_venice_balance({"canConsume": True}, "billing/balance")

    def test_negative_usd_is_kept_and_not_available(self):
        data = _normalize_venice_balance({"canConsume": False, "balances": {"usd": -1.5}}, "billing/balance")
        self.assertEqual(data["total_usd"], -1.5)
        self.assertFalse(data["available"])


class LiveVeniceTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"VENICE_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_billing_balance_is_preferred(self):
        import json

        with mock.patch("urllib.request.urlopen", return_value=_Response(json.dumps(BILLING).encode())) as urlopen:
            _ts, data = live_venice_balance()
        self.assertEqual(data["total_usd"], 25)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, providers.VENICE_BALANCE_URL)
        self.assertEqual(request.get_header("Authorization"), "Bearer test-key")

    def test_falls_back_to_rate_limits_when_billing_endpoint_fails(self):
        import json

        responses = [_http_error(404), _Response(json.dumps(RATE_LIMITS).encode())]
        with mock.patch("urllib.request.urlopen", side_effect=responses) as urlopen:
            _ts, data = live_venice_balance()
        self.assertEqual(data["total_usd"], 25.0)
        self.assertEqual(urlopen.call_args.args[0].full_url, providers.VENICE_RATE_LIMITS_URL)

    def test_inference_key_401_on_billing_uses_rate_limits(self):
        import json

        responses = [_http_error(401), _Response(json.dumps(RATE_LIMITS).encode())]
        with mock.patch("urllib.request.urlopen", side_effect=responses):
            _ts, data = live_venice_balance()
        self.assertEqual(data["source"], "api.venice.ai api_keys/rate_limits")

    def test_key_rejected_by_both_endpoints_is_an_auth_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=[_http_error(401), _http_error(401)]) as urlopen:
            with self.assertRaises(VeniceAuthError):
                live_venice_balance()
        self.assertEqual(urlopen.call_count, 2)


class LoadVeniceKeyTests(unittest.TestCase):
    def test_key_file_is_used_when_env_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            key_path = pathlib.Path(tmp) / "venice_api_key"
            key_path.write_text("file-key\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True), \
                    mock.patch.object(providers, "VENICE_KEY_PATHS", (key_path,)):
                self.assertEqual(load_venice_api_key(), "file-key")

    def test_missing_key_raises_auth_error(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(providers, "VENICE_KEY_PATHS", ()):
            with self.assertRaises(VeniceAuthError):
                load_venice_api_key()


class VeniceApiServerTests(unittest.TestCase):
    def test_payload_normalizes_balance(self):
        data = _normalize_venice_balance(BILLING, "billing/balance")
        with mock.patch.object(api_server, "has_venice_api_key", return_value=True), \
                mock.patch.object(api_server, "live_venice_balance", return_value=(NOW, data)):
            payload = api_server._venice_payload()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["total_usd"], 25)
        self.assertEqual(payload["observed_at"], "2026-11-05T08:00:00Z")

    def test_aggregate_skips_venice_without_key_but_route_reports_it(self):
        with mock.patch.object(api_server, "has_venice_api_key", return_value=False):
            self.assertIsNone(api_server._venice_payload())
        with mock.patch.object(api_server, "has_venice_api_key", return_value=False), \
                mock.patch.object(api_server, "live_venice_balance", side_effect=VeniceAuthError("no key")):
            payload = api_server._venice_payload(require_key=True)
        self.assertFalse(payload["available"])
        self.assertIn("no key", payload["error"])


class VeniceMenubarTests(unittest.TestCase):
    def _app(self, venice):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "zh", "global": "7d", "services": ["venice"], "widget": True}
        app._claude = app._codex = app._deepseek = app._google = app._gemini = app._copilot = app._grok = None
        app._api_credits_v1 = None
        app._venice = venice
        return app

    def test_fetch_and_widget_show_venice_balance(self):
        data = _normalize_venice_balance(BILLING, "billing/balance")
        with mock.patch.object(menubar, "live_venice_balance", return_value=(NOW, data)):
            venice = menubar._fetch_venice("zh")
        self.assertEqual(venice["primary"]["total_balance"], 25)
        app = self._app(venice)
        rows = app._widget_detail_rows()
        self.assertEqual([row["name"] for row in rows if row.get("type") == "section"], ["Venice"])
        balance = [row for row in rows if row.get("name") == "API 余额"]
        self.assertEqual(balance[0]["value"], "$25.00")
        self.assertIn("Venice", app._widget_text_body())
        self.assertIn("venice", menubar._SERVICES)

    def test_fetch_error_becomes_error_entry(self):
        with mock.patch.object(menubar, "live_venice_balance", side_effect=VeniceAuthError("HTTP 401")):
            venice = menubar._fetch_venice("zh")
        self.assertEqual(venice, {"error": "HTTP 401"})


if __name__ == "__main__":
    unittest.main()
