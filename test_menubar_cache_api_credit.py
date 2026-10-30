"""The menu bar cache must keep carrying ``claude.api_credit`` for other tools.

InfoWeave reads ``~/.ai-limit-menubar-cache.json`` and sums the
``subscription_api_credit`` tranches under ``claude.api_credit``. When API
credits moved to their own api-credit/v1 fetch, the widget kept showing the
balance but the cache field stayed ``null``.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from ai_limit import api_credits
from test_widget_layout import menubar


def _claude_v1(status="ok", observed_at="2026-10-30T22:06:59Z"):
    return {
        "contract": "api-credit/v1",
        "provider": "claude",
        "status": status,
        "account_identity": {"profile": "Default", "label": "Default"},
        "organization_identity": {"id": "org-fixture", "name": "Fixture Org"},
        "binding_status": "bound",
        "currency": "USD",
        "promotional_balance": 76.58,
        "purchased_balance": 0.01,
        "total": 76.59,
        "grants": [
            {
                "name": "API credit (Max 20x)",
                "program_id": "subscription_api_credit",
                "granted": 200.0,
                "remaining": 76.58,
                "expires_at": "2026-11-11T00:00:00Z",
                "granted_at": "2026-10-15T07:37:04Z",
                "usage_restrictions": None,
            }
        ],
        "observed_at": observed_at,
        "checked_at": observed_at,
        "source": "platform.claude.com",
        "error_code": None if status == "ok" else "fetch_failed",
    }


CLAUDE = {"5h_left": 80, "7d_left": 60, "plan": "Max", "source": "browser", "api_credit": None}


def _infoweave_gift_remaining(api_credit):
    """Mirror InfoWeave's anthropic_usage_ledger._reading_from_api_credit."""
    tranches = [row for row in (api_credit.get("tranches") or []) if isinstance(row, dict)]
    gift = [row for row in tranches if row.get("program_id") == "subscription_api_credit"]
    return sum(float(row.get("remaining") or 0.0) for row in gift)


class ClaudeCacheEntryTest(unittest.TestCase):
    def test_fresh_v1_record_fills_the_legacy_field(self):
        entry = menubar._claude_cache_entry(CLAUDE, {"claude": _claude_v1()})
        credit = entry["api_credit"]
        self.assertEqual(credit["balance"], 76.59)
        self.assertEqual(credit["currency"], "USD")
        self.assertEqual(credit["granted"], 200.0)
        self.assertEqual(credit["expires_at"], "2026-11-11T00:00:00Z")
        self.assertEqual(credit["observed_at"], "2026-10-30T22:06:59Z")
        self.assertEqual(credit["status"], "ok")
        self.assertAlmostEqual(_infoweave_gift_remaining(credit), 76.58)
        # The quota fields are untouched and the caller's dict is not mutated.
        self.assertEqual(entry["7d_left"], 60)
        self.assertIsNone(CLAUDE["api_credit"])

    def test_stale_record_is_not_written_as_a_new_observation(self):
        entry = menubar._claude_cache_entry(CLAUDE, {"claude": _claude_v1(status="stale")})
        self.assertIsNone(entry["api_credit"])

    def test_missing_or_unbound_v1_leaves_the_field_empty(self):
        self.assertIsNone(menubar._claude_cache_entry(CLAUDE, None)["api_credit"])
        unbound = {"claude": {"status": "unknown", "error_code": "binding_required"}}
        self.assertIsNone(menubar._claude_cache_entry(CLAUDE, unbound)["api_credit"])

    def test_disabled_claude_service_is_left_alone(self):
        self.assertIsNone(menubar._claude_cache_entry(None, {"claude": _claude_v1()}))

    def test_apply_pending_writes_the_credit_to_the_cache_file(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._claude = app._codex = app._deepseek = app._google = None
        app._gemini = app._copilot = app._grok = None
        app._api_credits_v1 = None
        app._pending_lock = menubar.threading.Lock()
        app._pending = (dict(CLAUDE), None, None, None, None, None, None, {"claude": _claude_v1()})
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "cache.json"
            with mock.patch.object(menubar, "_CACHE_PATH", path), \
                    mock.patch.object(menubar.AiLimitApp, "_render", lambda _self: None), \
                    mock.patch.object(menubar.AiLimitApp, "_render_widget", lambda _self: None, create=True):
                try:
                    app._apply_pending(None)
                except Exception:
                    # Later UI steps need real AppKit; the cache is written first.
                    pass
            cached = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(cached["claude"]["api_credit"]["balance"], 76.59)
        self.assertAlmostEqual(_infoweave_gift_remaining(cached["claude"]["api_credit"]), 76.58)


class LegacyProjectionTest(unittest.TestCase):
    def test_projection_reports_when_and_how_fresh(self):
        legacy = api_credits.claude_api_credit_legacy(_claude_v1(status="stale"))
        self.assertEqual(legacy["status"], "stale")
        self.assertEqual(legacy["observed_at"], "2026-10-30T22:06:59Z")


if __name__ == "__main__":
    unittest.main()
