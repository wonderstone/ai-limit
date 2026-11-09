"""Each provider updates its own card as soon as its read arrives."""
import threading
import unittest
from unittest import mock

from test_widget_layout import menubar


def _app(services):
    app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
    app._state = {"lang": "zh", "services": services}
    app._pending = None
    app._pending_lock = threading.Lock()
    return app


class IncrementalRefreshTests(unittest.TestCase):
    def test_fast_provider_is_queued_before_slow_one_finishes(self):
        release = threading.Event()
        seen_before_release = {}

        def slow(_lang):
            release.wait(5)
            return {"slow": True}

        app = _app(["venice", "grok"])

        def fast(_lang):
            return {"fast": True}

        def watch():
            # Wait until the fast result is queued, record it, then free the slow fetch.
            for _ in range(200):
                with app._pending_lock:
                    if isinstance(app._pending, dict) and "venice" in app._pending:
                        seen_before_release.update(app._pending)
                        break
                threading.Event().wait(0.01)
            release.set()

        with mock.patch.object(menubar, "_fetch_venice", fast), \
                mock.patch.object(menubar, "_fetch_grok", slow), \
                mock.patch.object(menubar, "_fetch_api_credit_balances", return_value=None):
            watcher = threading.Thread(target=watch)
            watcher.start()
            app._async_refresh()
            watcher.join()
        self.assertEqual(seen_before_release, {"venice": {"fast": True}})
        self.assertEqual(app._pending, {"venice": {"fast": True}, "grok": {"slow": True}})

    def test_failed_or_disabled_provider_queues_nothing(self):
        app = _app(["venice"])

        def boom(_lang):
            raise RuntimeError("down")

        with mock.patch.object(menubar, "_fetch_venice", boom), \
                mock.patch.object(menubar, "_fetch_grok") as grok, \
                mock.patch.object(menubar, "_fetch_api_credit_balances", return_value=None):
            app._async_refresh()
        grok.assert_not_called()
        self.assertIsNone(app._pending)

    def test_apply_pending_applies_a_partial_update(self):
        app = _app(["venice", "grok"])
        app._claude = app._codex = app._deepseek = app._google = app._gemini = None
        app._copilot = app._cloudflare = app._runninghub = app._api_credits_v1 = None
        app._venice = {"old": True}
        app._grok = {"keep": True}
        app._pending = {"venice": {"new": True}}
        with mock.patch.object(menubar, "_save_cache"), \
                mock.patch.object(menubar.AiLimitApp, "_render", lambda _self: None):
            app._apply_pending(None)
        self.assertEqual(app._venice, {"new": True})
        self.assertEqual(app._grok, {"keep": True})
        self.assertIsNone(app._pending)


if __name__ == "__main__":
    unittest.main()
