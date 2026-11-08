import datetime
import io
import json
import pathlib
import tempfile
import unittest
import urllib.error
from unittest import mock

from ai_limit import api_server, providers
from ai_limit.providers import (
    RunningHubAuthError,
    RunningHubError,
    _normalize_runninghub_status,
    live_runninghub_status,
    load_runninghub_config,
)
from test_widget_layout import menubar

NOW = datetime.datetime(2026, 11, 9, 3, 0, tzinfo=datetime.timezone.utc)
STATUS = {
    "code": 0,
    "msg": "success",
    "errorMessages": None,
    "data": {
        "remainCoins": "28203",
        "currentTaskCounts": "0",
        "remainMoney": "4.3470",
        "remainPoint": 19162.3,
        "currency": "USD",
        "apiType": "SHARED",
    },
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class NormalizeTests(unittest.TestCase):
    def test_string_amounts_become_numbers(self):
        data = _normalize_runninghub_status(STATUS)
        self.assertEqual(data["remain_money"], 4.347)
        self.assertEqual(data["legacy_remain_coins"], 28203)
        self.assertEqual(data["remain_points"], 19162.3)
        self.assertEqual(data["current_task_counts"], 0)
        self.assertTrue(data["available"])

    def test_error_codes_raise(self):
        with self.assertRaises(RunningHubAuthError):
            _normalize_runninghub_status({"code": 412, "msg": "apikey invalid"})
        with self.assertRaises(RunningHubError):
            _normalize_runninghub_status({"code": 500, "msg": "busy"})
        with self.assertRaises(RunningHubError):
            _normalize_runninghub_status({"code": 0, "data": {"currency": "USD"}})


class ConfigTests(unittest.TestCase):
    def test_env_file_supplies_key_and_base_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = pathlib.Path(tmp) / "runninghub.env"
            env.write_text("RUNNINGHUB_API_KEY=file-key\nRUNNINGHUB_BASE_URL=https://www.runninghub.cn/\n")
            with mock.patch.dict("os.environ", {}, clear=True), \
                    mock.patch.object(providers, "RUNNINGHUB_KEY_PATHS", ()), \
                    mock.patch.object(providers, "RUNNINGHUB_ENV_FILES", (env,)):
                self.assertEqual(load_runninghub_config(), ("file-key", "https://www.runninghub.cn"))

    def test_env_var_wins_and_missing_key_raises(self):
        with mock.patch.dict("os.environ", {"RUNNINGHUB_API_KEY": "env-key"}, clear=True), \
                mock.patch.object(providers, "RUNNINGHUB_ENV_FILES", ()):
            self.assertEqual(load_runninghub_config(), ("env-key", providers.RUNNINGHUB_DEFAULT_BASE_URL))
        with mock.patch.dict("os.environ", {}, clear=True), \
                mock.patch.object(providers, "RUNNINGHUB_KEY_PATHS", ()), \
                mock.patch.object(providers, "RUNNINGHUB_ENV_FILES", ()):
            with self.assertRaises(RunningHubAuthError):
                load_runninghub_config()


class LiveTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict("os.environ", {"RUNNINGHUB_API_KEY": "test-key", "RUNNINGHUB_BASE_URL": ""})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_posts_key_in_body(self):
        with mock.patch.object(providers, "runninghub_points_expiry", return_value=None), \
                mock.patch("urllib.request.urlopen", return_value=_Response(json.dumps(STATUS).encode())) as urlopen:
            _ts, data = live_runninghub_status()
        request = urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/uc/openapi/accountStatus"))
        self.assertEqual(json.loads(request.data), {"apikey": "test-key"})
        self.assertEqual(data["remain_money"], 4.347)

    def test_http_401_is_auth_error(self):
        err = urllib.error.HTTPError("https://www.runninghub.ai", 401, "err", {}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(RunningHubAuthError):
                live_runninghub_status()


POINT_RECORDS = {
    "code": 0,
    "msg": "success",
    "data": {
        "records": [
            {"sourceType": "SUBSCRIPTION_BONUS", "sourceTypeDesc": "会员加赠", "amountUnit": "400000",
             "remainedUnit": "23620", "gainTime": "2026-11-08 22:27:00", "expiredTime": "2099-12-09 22:27:00", "expired": False},
            {"sourceType": "SUBSCRIPTION_GRANT", "sourceTypeDesc": "会员权益发放", "amountUnit": "18900000",
             "remainedUnit": "18900000", "gainTime": "2026-11-08 22:27:00", "expiredTime": "2099-12-10 22:27:00", "expired": False},
            {"sourceType": "OLD", "amountUnit": "5000", "remainedUnit": "5000", "expiredTime": "2026-01-01 00:00:00", "expired": True},
            {"sourceType": "USED", "amountUnit": "5000", "remainedUnit": "0", "expiredTime": "2099-01-01 00:00:00", "expired": False},
        ],
        "pages": "1",
    },
}


class PointExpiryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(providers, "RUNNINGHUB_POINTS_CACHE_PATH", pathlib.Path(tmp.name) / "points.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_batches_use_thousandth_units_and_beijing_time(self):
        with mock.patch.object(providers, "_runninghub_access_tokens", return_value=[("Chrome/Default", "tok")]), \
                mock.patch("urllib.request.urlopen", return_value=_Response(json.dumps(POINT_RECORDS).encode())) as urlopen:
            expiry = providers.runninghub_points_expiry("https://www.runninghub.ai")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer tok")
        self.assertEqual([b["remaining"] for b in expiry["point_batches"]], [23.62, 18900.0])
        self.assertEqual(expiry["points_next_expiry"], "2099-12-09T14:27:00Z")
        self.assertEqual(expiry["points_expiring_next"], 23.62)
        self.assertEqual(expiry["points_expiry_source"], "browser live")

    def test_lapsed_session_serves_the_last_good_read_as_cached(self):
        with mock.patch.object(providers, "_runninghub_access_tokens", return_value=[("Chrome/Default", "tok")]), \
                mock.patch("urllib.request.urlopen", return_value=_Response(json.dumps(POINT_RECORDS).encode())):
            providers.runninghub_points_expiry()
        err = urllib.error.HTTPError("https://www.runninghub.ai", 403, "err", {}, io.BytesIO(b"{}"))
        with mock.patch.object(providers, "_runninghub_access_tokens", return_value=[("Chrome/Default", "old")]), \
                mock.patch("urllib.request.urlopen", side_effect=err):
            expiry = providers.runninghub_points_expiry()
        self.assertEqual(expiry["points_expiry_source"], "cached")
        self.assertEqual(expiry["points_next_expiry"], "2099-12-09T14:27:00Z")

    def test_no_session_and_no_cache_is_none(self):
        with mock.patch.object(providers, "_runninghub_access_tokens", return_value=[]):
            self.assertIsNone(providers.runninghub_points_expiry())

    def test_menubar_shows_next_expiry_and_partial_amount(self):
        data = {"remain_points": 18923.62, "points_next_expiry": "2099-12-09T14:27:00Z",
                "points_expiring_next": 23.62, "points_expiry_source": "cached"}
        label = menubar._runninghub_expiry_label(data, "zh")
        self.assertIn("· 24", label)
        self.assertTrue(label.endswith("（缓存）"))
        whole = menubar._runninghub_expiry_label({**data, "points_expiring_next": 18923.62, "points_expiry_source": "browser live"}, "zh")
        self.assertNotIn("·", whole)


class ApiServerTests(unittest.TestCase):
    def test_payload_and_missing_key(self):
        data = _normalize_runninghub_status(STATUS)
        with mock.patch.object(api_server, "has_runninghub_api_key", return_value=True), \
                mock.patch.object(api_server, "live_runninghub_status", return_value=(NOW, data)):
            payload = api_server._runninghub_payload()
        self.assertEqual(payload["remain_money"], 4.347)
        self.assertEqual(payload["observed_at"], "2026-11-09T03:00:00Z")
        with mock.patch.object(api_server, "has_runninghub_api_key", return_value=False):
            self.assertIsNone(api_server._runninghub_payload())
        with mock.patch.object(api_server, "live_runninghub_status", side_effect=RunningHubAuthError("no key")):
            payload = api_server._runninghub_payload(require_key=True)
        self.assertFalse(payload["available"])


class MenubarTests(unittest.TestCase):
    def test_widget_shows_balance_and_coins(self):
        data = _normalize_runninghub_status(STATUS)
        with mock.patch.object(menubar, "live_runninghub_status", return_value=(NOW, data)):
            runninghub = menubar._fetch_runninghub("zh")
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "zh", "global": "7d", "services": ["runninghub"], "widget": True}
        app._claude = app._codex = app._deepseek = app._google = app._gemini = app._copilot = app._grok = None
        app._api_credits_v1 = None
        app._runninghub = runninghub
        rows = app._widget_detail_rows()
        by_name = {row.get("name"): row for row in rows}
        self.assertEqual(by_name["积分"]["value"], "19,162")
        self.assertEqual(by_name["钱包"]["value"], "$4.35")
        names = [row.get("name") for row in rows]
        self.assertLess(names.index("积分"), names.index("钱包"))
        body = app._widget_text_body()
        self.assertIn("积分: 19,162.3  (≈¥191.62)", body)
        self.assertIn("钱包: $4.35", body)

    def test_legacy_rh_coins_are_not_shown(self):
        data = _normalize_runninghub_status(STATUS)
        with mock.patch.object(menubar, "live_runninghub_status", return_value=(NOW, data)):
            runninghub = menubar._fetch_runninghub("zh")
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "zh", "global": "7d", "services": ["runninghub"], "widget": True}
        app._claude = app._codex = app._deepseek = app._google = app._gemini = app._copilot = app._grok = None
        app._api_credits_v1 = None
        app._runninghub = runninghub
        names = [row.get("name") for row in app._widget_detail_rows()]
        self.assertFalse(any("RH" in str(name) for name in names))
        self.assertNotIn("28,203", app._widget_text_body())
        self.assertIn("runninghub", menubar._SERVICES)


if __name__ == "__main__":
    unittest.main()
