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
        self.assertEqual(data["remain_coins"], 28203)
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
        with mock.patch("urllib.request.urlopen", return_value=_Response(json.dumps(STATUS).encode())) as urlopen:
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
        self.assertEqual(by_name["API 余额"]["value"], "$4.35")
        self.assertEqual(by_name["RH 币"]["value"], "28,203")
        body = app._widget_text_body()
        self.assertIn("余额: $4.35", body)
        self.assertIn("runninghub", menubar._SERVICES)


if __name__ == "__main__":
    unittest.main()
