from __future__ import annotations

import datetime as dt
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
            mock.patch.object(api_server, "_copilot_payload", return_value=None),
            mock.patch.object(api_server, "_grok_payload", return_value=None),
        ):
            started = time.monotonic()
            payload = api_server.quota_payload()
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.15)
        self.assertEqual(payload["providers"]["claude"], fast)
        self.assertFalse(payload["providers"]["google"]["available"])
        self.assertIn("deadline", payload["providers"]["google"]["error"])

    def test_authorized_app_server_refresh_updates_safe_disk_cache(self):
        rate_limits = {
            "primary": {"used_percent": 1, "window_minutes": 10080, "resets_at": 123},
            "credits": {"has_credits": True, "unlimited": False, "balance": "2625"},
            "rate_limit_reset_credits": {"available_count": 0},
        }
        with (
            mock.patch.object(api_server, "current_codex_rate_limits", return_value=(None, rate_limits, "web", None)),
            mock.patch.object(api_server, "_write_codex_disk_cache") as write_cache,
            mock.patch.object(api_server, "_CODEX_CACHE", {}),
        ):
            payload = api_server._codex_payload(allow_app_server=True)
        self.assertTrue(payload["available"])
        self.assertEqual(payload["credits"]["balance"], "2625")
        self.assertEqual(payload["rate_limit_reset_credits"], {"available_count": 0})
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


class CopilotGrokPayloadTests(unittest.TestCase):
    NOW = dt.datetime(2026, 10, 20, 8, 0, tzinfo=dt.timezone.utc)

    def test_copilot_payload_normalizes_monthly_credits(self):
        data = {
            "source": "api.github.com copilot_internal/user",
            "available": True,
            "subscription_status": "active",
            "plan": "individual_pro",
            "plan_label": "Pro",
            "unit": "AI credits",
            "reset_time": "2026-11-01T00:00:00Z",
            "buckets": [{"bucket_id": "premium_interactions"}],
            "summary": {
                "remaining_percent": 72,
                "remaining": 216,
                "used": 84,
                "entitlement": 300,
                "overage_permitted": True,
                "overage_count": 0,
            },
        }
        with mock.patch.object(api_server, "live_copilot_quota", return_value=(self.NOW, data)):
            payload = api_server._copilot_payload()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["provider"], "copilot")
        self.assertEqual(payload["remaining_percent"], 72)
        self.assertEqual((payload["remaining"], payload["entitlement"]), (216, 300))
        self.assertTrue(payload["overage_permitted"])
        self.assertEqual(payload["reset_time"], "2026-11-01T00:00:00Z")
        self.assertEqual(payload["observed_at"], "2026-10-20T08:00:00Z")

    def test_grok_payload_normalizes_weekly_pool(self):
        data = {
            "source": "grok.com GrokBuildBilling/GetGrokCreditsConfig",
            "period_type": "weekly",
            "reset_time": "2026-10-25T00:00:00Z",
            "products": [],
            "summary": {"used_percent": 31, "remaining_percent": 69, "window": "weekly"},
        }
        with mock.patch.object(api_server, "live_grok_usage", return_value=(self.NOW, data)):
            payload = api_server._grok_payload()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["window"], "weekly")
        self.assertEqual(payload["remaining_percent"], 69)
        self.assertEqual(payload["reset_time"], "2026-10-25T00:00:00Z")

    def test_errors_degrade_to_unavailable_entries(self):
        cases = [
            ("live_copilot_quota", api_server.CopilotAuthError("expired"), api_server._copilot_payload),
            ("live_copilot_quota", RuntimeError("boom"), api_server._copilot_payload),
            ("live_grok_usage", api_server.GrokUsageError("not on pool"), api_server._grok_payload),
            ("live_grok_usage", OSError("no cookies"), api_server._grok_payload),
        ]
        for target, exc, probe in cases:
            with self.subTest(target=target, exc=type(exc).__name__):
                with mock.patch.object(api_server, target, side_effect=exc):
                    payload = probe()
                self.assertFalse(payload["available"])
                self.assertIn("error", payload)

    def test_aggregate_includes_copilot_and_grok_and_survives_failures(self):
        with (
            mock.patch.object(api_server, "_claude_payload", return_value=None),
            mock.patch.object(api_server, "_codex_payload", return_value=None),
            mock.patch.object(api_server, "_deepseek_payload", return_value=None),
            mock.patch.object(api_server, "_google_payload", return_value=None),
            mock.patch.object(api_server, "_gemini_payload", return_value=None),
            mock.patch.object(api_server, "_llm_api_payload", return_value=None),
            mock.patch.object(api_server, "live_copilot_quota", side_effect=RuntimeError("down")),
            mock.patch.object(
                api_server,
                "live_grok_usage",
                return_value=(self.NOW, {"summary": {"remaining_percent": 50, "window": "weekly"}}),
            ),
        ):
            payload = api_server.quota_payload()
        self.assertFalse(payload["providers"]["copilot"]["available"])
        self.assertEqual(payload["providers"]["grok"]["remaining_percent"], 50)

    def test_slow_grok_is_cut_by_aggregate_deadline(self):
        def slow():
            time.sleep(0.2)
            return {"provider": "grok", "available": True}

        with (
            mock.patch.object(api_server, "AGGREGATE_DEADLINE_SECONDS", 0.03),
            mock.patch.object(api_server, "_claude_payload", return_value=None),
            mock.patch.object(api_server, "_codex_payload", return_value=None),
            mock.patch.object(api_server, "_deepseek_payload", return_value=None),
            mock.patch.object(api_server, "_google_payload", return_value=None),
            mock.patch.object(api_server, "_gemini_payload", return_value=None),
            mock.patch.object(api_server, "_llm_api_payload", return_value=None),
            mock.patch.object(api_server, "_copilot_payload", return_value={"provider": "copilot", "available": True}),
            mock.patch.object(api_server, "_grok_payload", side_effect=slow),
        ):
            payload = api_server.quota_payload()
        self.assertTrue(payload["providers"]["copilot"]["available"])
        self.assertIn("deadline", payload["providers"]["grok"]["error"])


class ClaudeWeeklyModelTests(unittest.TestCase):
    def test_fable_weekly_scoped_limit_is_exposed(self):
        usage = {
            "five_hour": {"utilization": 32.0, "resets_at": "2026-10-23T04:39:59Z"},
            "seven_day": {"utilization": 50.0, "resets_at": "2026-10-28T08:59:59Z"},
            "limits": [
                {"kind": "weekly_all", "percent": 50, "scope": None},
                {
                    "kind": "weekly_scoped",
                    "percent": 69,
                    "severity": "normal",
                    "resets_at": "2026-10-28T08:59:59Z",
                    "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None},
                    "is_active": True,
                },
            ],
        }
        with (
            mock.patch.object(api_server, "live_claude_usage", return_value=usage),
            mock.patch.object(api_server, "live_claude_plan", return_value="Max"),
            mock.patch.object(api_server, "live_claude_api_credits", return_value=None),
        ):
            payload = api_server._claude_payload()
        self.assertEqual(payload["7d_left"], 50)
        self.assertEqual(payload["7d_left_by_model"], {"Fable": 31})
        self.assertEqual(payload["weekly_models"][0]["reset_time"], "2026-10-28T08:59:59Z")

    def test_missing_or_malformed_limits_yield_empty(self):
        from ai_limit.claude_limits import claude_weekly_model_limits

        self.assertEqual(claude_weekly_model_limits(None), [])
        self.assertEqual(claude_weekly_model_limits({"limits": None}), [])
        self.assertEqual(
            claude_weekly_model_limits({"limits": [{"kind": "weekly_scoped", "percent": "x", "scope": {"model": {"display_name": "Fable"}}}]}),
            [],
        )


if __name__ == "__main__":
    unittest.main()
