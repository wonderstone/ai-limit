"""A locked login keychain must read as 'locked', and must not blank quotas."""

import datetime
import types
import unittest
from unittest import mock

from ai_limit import providers
from ai_limit.providers import ClaudeWebError, GeminiAppUsageError, GrokAuthError
from test_widget_layout import menubar


LOCKED = "Unable to get key for cookie decryption"


def _cookie_module(chrome_error=None, chrome_cookies=()):
    def chrome(**_kwargs):
        if chrome_error:
            raise Exception(chrome_error)
        return list(chrome_cookies)

    def firefox(**_kwargs):
        raise Exception("Could not find Firefox profile directory")

    return types.SimpleNamespace(chrome=chrome, firefox=firefox)


class ProviderKeychainLockTests(unittest.TestCase):
    def test_claude_reports_locked_keychain_not_missing_login(self):
        with mock.patch.dict("sys.modules", {"browser_cookie3": _cookie_module(LOCKED)}):
            with self.assertRaises(ClaudeWebError) as ctx:
                providers._claude_web_context("https://claude.ai/settings/usage")

        self.assertTrue(getattr(ctx.exception, "keychain_locked", False))
        self.assertNotIn("log in to claude.ai", str(ctx.exception))

    def test_grok_reports_locked_keychain(self):
        with mock.patch.dict("sys.modules", {"browser_cookie3": _cookie_module(LOCKED)}), \
                mock.patch.object(providers, "_chrome_profile_cookie_files", return_value=[mock.MagicMock()]), \
                mock.patch.object(providers, "GROK_CHROME_PROFILE_PATH", mock.MagicMock(read_text=mock.MagicMock(side_effect=FileNotFoundError))):
            with self.assertRaises(GrokAuthError) as ctx:
                providers._load_grok_cookies()

        self.assertTrue(getattr(ctx.exception, "keychain_locked", False))

    def test_gemini_reports_locked_keychain(self):
        with mock.patch.dict("sys.modules", {"browser_cookie3": _cookie_module(LOCKED)}):
            with self.assertRaises(GeminiAppUsageError) as ctx:
                providers._gemini_app_cookie_context(5)

        self.assertTrue(getattr(ctx.exception, "keychain_locked", False))

    def test_genuinely_missing_cookies_are_not_called_locked(self):
        with mock.patch.dict("sys.modules", {"browser_cookie3": _cookie_module(None, ())}):
            with self.assertRaises(ClaudeWebError) as ctx:
                providers._claude_web_context("https://claude.ai/settings/usage")

        self.assertFalse(getattr(ctx.exception, "keychain_locked", False))


class KeepThroughKeychainLockTests(unittest.TestCase):
    good = {"5h_left": 95, "7d_left": 16, "5h_reset": None, "7d_reset": None}
    locked = {"error": "macOS keychain is locked", "keychain_locked": True}

    def test_locked_refresh_keeps_previous_numbers_marked_stale(self):
        kept = menubar._keep_through_keychain_lock(self.good, self.locked)

        self.assertEqual(kept["7d_left"], 16)
        self.assertEqual(kept["stale_reason"], "keychain_locked")
        self.assertNotIn("error", kept)
        self.assertNotIn("stale_reason", self.good, "previous reading must not be mutated")

    def test_stale_since_sticks_to_the_first_locked_refresh(self):
        first = menubar._keep_through_keychain_lock(self.good, self.locked)
        later = menubar._keep_through_keychain_lock(dict(first, stale_since=1.0), self.locked)

        self.assertEqual(later["stale_since"], 1.0)

    def test_fresh_reading_clears_the_stale_mark(self):
        stale = menubar._keep_through_keychain_lock(self.good, self.locked)

        self.assertIs(menubar._keep_through_keychain_lock(stale, self.good), self.good)

    def test_without_previous_data_the_locked_error_is_shown(self):
        self.assertIs(menubar._keep_through_keychain_lock(None, self.locked), self.locked)
        error = {"error": "old"}
        self.assertIs(menubar._keep_through_keychain_lock(error, self.locked), self.locked)

    def test_other_errors_still_replace_data(self):
        other = {"error": "HTTP 500"}

        self.assertIs(menubar._keep_through_keychain_lock(self.good, other), other)


class StaleDisplayTests(unittest.TestCase):
    def _app(self, lang="zh"):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": lang, "global": "7d", "services": ["claude", "grok"], "widget": True}
        app._codex = app._deepseek = app._google = app._gemini = app._copilot = None
        since = datetime.datetime(2026, 10, 13, 14, 17).timestamp()
        app._claude = {"5h_left": 95, "7d_left": 16, "5h_reset": None, "7d_reset": None,
                       "stale_reason": "keychain_locked", "stale_since": since}
        app._grok = {"left": 69, "reset": "2026-10-19T04:04:28Z", "window": "weekly",
                     "stale_reason": "keychain_locked", "stale_since": since}
        return app

    def test_cards_keep_numbers_and_carry_a_stale_note(self):
        app = self._app()

        card = app._widget_card_for_service("grok", "Grok", "bolt.fill", "#52525b", app._grok)

        self.assertEqual(card["metrics"][0]["pct"], 69)
        self.assertEqual(card["note"], "旧数据(14:17)")

    def test_sections_carry_the_note_but_keep_their_names(self):
        rows = self._app("en")._widget_detail_rows()
        sections = [row for row in rows if row.get("type") == "section"]

        self.assertEqual([row["name"] for row in sections], ["Claude", "Grok"])
        self.assertTrue(all(row["note"] == "stale (14:17)" for row in sections))

    def test_text_export_explains_the_lock(self):
        text = self._app()._widget_text_body()

        self.assertIn("钥匙串已锁定", text)


if __name__ == "__main__":
    unittest.main()
