"""Codex must find the ChatGPT session the user actually signed in with."""

import datetime
import unittest
from unittest import mock

from ai_limit import providers


class _Cookie:
    def __init__(self, name, value="v"):
        self.name = name
        self.value = value


SESSION = ["__Secure-next-auth.session-token.0", "__Secure-next-auth.session-token.1", "_cfuvid"]


def _cookie_module(jars):
    def chrome(cookie_file=None, domain_name=None):
        return [_Cookie(name, value=cookie_file) for name in jars[cookie_file]]

    return mock.MagicMock(chrome=chrome, firefox=mock.MagicMock(side_effect=Exception("no Firefox profile")))


class ChatGPTCandidateTests(unittest.TestCase):
    def _candidates(self, jars):
        sources = [(label, label) for label in jars]
        with mock.patch.dict("sys.modules", {"browser_cookie3": _cookie_module(jars)}), \
                mock.patch.object(providers, "_chromium_profile_cookie_files", return_value=sources):
            return providers._chatgpt_cookie_candidates()

    def test_canary_session_is_found_after_chrome(self):
        found = self._candidates({
            "Chrome/Default": SESSION,
            "Chrome/Profile 1": ["_cfuvid"],
            "Chrome Canary/Default": SESSION,
        })

        self.assertEqual([label for label, _ in found], ["Chrome/Default", "Chrome Canary/Default"])

    def test_no_session_anywhere_asks_the_user_to_sign_in(self):
        with self.assertRaises(providers.CodexWebError):
            self._candidates({"Chrome/Default": ["_cfuvid"]})


class LiveCodexProfileFallbackTests(unittest.TestCase):
    candidates = [("Chrome/Default", [("a", "stale")]), ("Chrome Canary/Default", [("a", "fresh")])]

    def test_expired_profile_falls_through_to_the_signed_in_one(self):
        def usage(cookies, _timeout):
            if cookies[0][1] == "stale":
                raise providers.CodexSessionExpiredError("expired")
            return datetime.datetime.now(datetime.timezone.utc), {"primary": {}}

        with mock.patch.object(providers, "_chatgpt_cookie_candidates", return_value=self.candidates), \
                mock.patch.object(providers, "_codex_web_usage_with", side_effect=usage):
            _ts, data = providers.live_codex_web_usage()

        self.assertEqual(data["cookie_source"], "Chrome Canary/Default")

    def test_every_profile_expired_still_reports_expiry(self):
        with mock.patch.object(providers, "_chatgpt_cookie_candidates", return_value=self.candidates), \
                mock.patch.object(providers, "_codex_web_usage_with", side_effect=providers.CodexSessionExpiredError("expired")):
            with self.assertRaises(providers.CodexSessionExpiredError):
                providers.live_codex_web_usage()

    def test_network_errors_are_not_swallowed_by_the_fallback(self):
        with mock.patch.object(providers, "_chatgpt_cookie_candidates", return_value=self.candidates), \
                mock.patch.object(providers, "_codex_web_usage_with", side_effect=providers.CodexWebError("HTTP 502")) as usage:
            with self.assertRaises(providers.CodexWebError):
                providers.live_codex_web_usage()

        self.assertEqual(usage.call_count, 1)


if __name__ == "__main__":
    unittest.main()
