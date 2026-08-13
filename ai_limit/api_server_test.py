from __future__ import annotations

import time
import unittest
from unittest import mock

from ai_limit import api_server


class AggregateIsolationTests(unittest.TestCase):
    def test_slow_provider_does_not_hide_fast_provider(self):
        def slow():
            time.sleep(0.2)
            return {"provider": "google", "available": True}

        fast = {"provider": "claude", "available": True, "five_hour": {"remaining_percent": 91}}
        with (
            mock.patch.object(api_server, "AGGREGATE_DEADLINE_SECONDS", 0.03),
            mock.patch.object(api_server, "_claude_payload", return_value=fast),
            mock.patch.object(api_server, "_codex_payload", return_value=None),
            mock.patch.object(api_server, "_deepseek_payload", return_value=None),
            mock.patch.object(api_server, "_google_payload", side_effect=slow),
            mock.patch.object(api_server, "_gemini_payload", return_value=None),
            mock.patch.object(api_server, "_llm_api_payload", return_value=None),
        ):
            started = time.monotonic()
            payload = api_server.quota_payload()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(payload["providers"]["claude"], fast)
        self.assertFalse(payload["providers"]["google"]["available"])
        self.assertIn("deadline", payload["providers"]["google"]["error"])

    def test_authorized_app_server_refresh_updates_safe_disk_cache(self):
        rate_limits = {"primary": {"used_percent": 1, "window_minutes": 10080, "resets_at": 123}}
        with (
            mock.patch.object(api_server, "current_codex_rate_limits", return_value=(None, rate_limits, "web", None)),
            mock.patch.object(api_server, "_write_codex_disk_cache") as write_cache,
            mock.patch.object(api_server, "_CODEX_CACHE", {}),
        ):
            payload = api_server._codex_payload(allow_app_server=True)
        self.assertTrue(payload["available"])
        write_cache.assert_called_once()

    def test_safe_request_refreshes_web_after_disk_cache_ttl(self):
        stale_disk = {
            "provider": "codex",
            "available": True,
            "source": "snapshot",
            "cache": {"status": "stale-disk-hit", "age_seconds": 301},
        }
        rate_limits = {"primary": {"used_percent": 2, "window_minutes": 300, "resets_at": 123}}
        with (
            mock.patch.object(api_server, "_read_codex_disk_cache", return_value=stale_disk),
            mock.patch.object(
                api_server,
                "current_codex_rate_limits",
                return_value=(None, rate_limits, "web", None),
            ) as current,
            mock.patch.object(api_server, "_write_codex_disk_cache"),
            mock.patch.object(api_server, "_CODEX_CACHE", {}),
        ):
            payload = api_server._codex_payload()

        self.assertEqual(payload["source"], "web")
        self.assertEqual(payload["five_hour"]["remaining_percent"], 98)
        current.assert_called_once_with(api_server.latest_codex_rate_limits, allow_app_server_fallback=False)

    def test_safe_request_reuses_fresh_disk_cache(self):
        fresh_disk = {
            "provider": "codex",
            "available": True,
            "source": "web",
            "cache": {"status": "disk-hit", "age_seconds": 30},
        }
        with (
            mock.patch.object(api_server, "_read_codex_disk_cache", return_value=fresh_disk),
            mock.patch.object(api_server, "current_codex_rate_limits") as current,
            mock.patch.object(api_server, "_CODEX_CACHE", {}),
        ):
            payload = api_server._codex_payload()

        self.assertEqual(payload, fresh_disk)
        current.assert_not_called()


if __name__ == "__main__":
    unittest.main()
