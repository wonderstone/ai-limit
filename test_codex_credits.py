"""Codex credit contract tests for app-server, web, and menu-bar handoff."""

from __future__ import annotations

import pathlib
import tempfile
import unittest
from unittest import mock

from ai_limit.providers import (
    _normalize_remote_rate_limits,
    _normalize_web_rate_limits,
)

from test_widget_layout import _Panel, menubar


def _remote_rate_limits(credits):
    return {
        "limitId": "codex",
        "primary": {"usedPercent": 10, "windowDurationMins": 300, "resetsAt": 123},
        "secondary": {"usedPercent": 20, "windowDurationMins": 10080, "resetsAt": 456},
        "credits": credits,
        "planType": "pro",
    }


def _web_usage(credits, reset_credits=None):
    return {
        "plan_type": "pro",
        "rate_limit": {
            "primary_window": {"used_percent": 10, "limit_window_seconds": 18_000, "reset_at": 123},
            "secondary_window": {"used_percent": 20, "limit_window_seconds": 604_800, "reset_at": 456},
        },
        "credits": credits,
        "rate_limit_reset_credits": reset_credits,
    }


class TestCodexCreditNormalization(unittest.TestCase):
    def test_app_server_uses_credits_snapshot_and_separates_reset_inventory(self):
        raw = _remote_rate_limits({"hasCredits": True, "unlimited": False, "balance": "2625"})
        raw["rateLimitResetCredits"] = {"availableCount": 0}
        normalized = _normalize_remote_rate_limits(
            raw,
        )

        self.assertEqual(normalized["credits"], {
            "has_credits": True,
            "unlimited": False,
            "balance": "2625",
        })
        self.assertEqual(normalized["rate_limit_reset_credits"], {"available_count": 0})

    def test_web_uses_snake_case_and_accepts_numeric_balance(self):
        normalized = _normalize_web_rate_limits(
            _web_usage(
                {"has_credits": True, "unlimited": False, "balance": 2625},
                {"available_count": 3},
            )
        )

        self.assertEqual(normalized["credits"], {
            "has_credits": True,
            "unlimited": False,
            "balance": 2625,
        })
        self.assertEqual(normalized["rate_limit_reset_credits"], {"available_count": 3})

    def test_web_accepts_nested_app_server_credit_shape(self):
        raw = _web_usage(None)
        raw["rateLimits"] = {
            "credits": {"hasCredits": True, "unlimited": False, "balance": "12.5"},
            "rateLimitResetCredits": {"availableCount": "2"},
        }

        normalized = _normalize_web_rate_limits(raw)

        self.assertEqual(normalized["credits"], {
            "has_credits": True,
            "unlimited": False,
            "balance": "12.5",
        })
        self.assertEqual(normalized["rate_limit_reset_credits"], {"available_count": 2})

    def test_missing_or_non_consumable_credit_snapshots_are_safe(self):
        self.assertIsNone(_normalize_remote_rate_limits(_remote_rate_limits(None))["credits"])
        self.assertIsNone(_normalize_web_rate_limits(_web_usage({}))["credits"])
        self.assertIs(
            _normalize_remote_rate_limits(
                _remote_rate_limits({"hasCredits": False, "unlimited": False, "balance": "0"})
            )["credits"]["has_credits"],
            False,
        )
        self.assertIs(
            _normalize_web_rate_limits(
                _web_usage({"has_credits": True, "unlimited": True, "balance": "999"})
            )["credits"]["unlimited"],
            True,
        )


class TestCodexCreditMenuBar(unittest.TestCase):
    def test_fetch_and_cache_keep_normalized_credits_and_reset_inventory_separate(self):
        rate_limits = _normalize_web_rate_limits(
            _web_usage(
                {"has_credits": True, "unlimited": False, "balance": "2625"},
                {"available_count": 0},
            )
        )
        with mock.patch.object(
            menubar,
            "resolve_codex_rate_limits",
            return_value=(None, rate_limits, "web", None),
        ):
            data = menubar._fetch_codex("en")

        self.assertEqual(data["credits"], rate_limits["credits"])
        self.assertEqual(data["rate_limit_reset_credits"], {"available_count": 0})

        with tempfile.TemporaryDirectory() as tmp_dir:
            cache_path = pathlib.Path(tmp_dir) / "menubar-cache.json"
            with mock.patch.object(menubar, "_CACHE_PATH", cache_path):
                menubar._save_cache(None, {"codex": data})
                _claude, cached = menubar._load_cache()

            self.assertEqual(cached["codex"]["credits"], data["credits"])
            self.assertEqual(cached["codex"]["rate_limit_reset_credits"], {"available_count": 0})

    def test_header_credit_summary_is_a_balance_not_a_quota_percent(self):
        self.assertEqual(menubar._codex_credit_summary(
            {"credits": {"has_credits": True, "unlimited": False, "balance": "2625"}},
            "en",
        ), "Codex credits 2,625")
        self.assertEqual(menubar._codex_credit_summary(
            {"credits": {"has_credits": True, "unlimited": False, "balance": 12.5}},
            "en",
        ), "Codex credits 12.5")
        self.assertEqual(menubar._codex_credit_summary(
            {"credits": {"has_credits": True, "unlimited": True, "balance": "999"}},
            "en",
        ), "Codex credits unlimited")
        self.assertIsNone(menubar._codex_credit_summary(
            {"credits": {"has_credits": False, "unlimited": False, "balance": "0"}},
            "en",
        ))

    def test_codex_section_receives_credit_summary_as_non_percent_row(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "en", "global": "7d", "services": list(menubar._SERVICES), "widget": True}
        app._codex = {"credits": {"has_credits": True, "unlimited": False, "balance": "2625"}}
        app._widget_panel = _Panel(width=760, height=900)
        app._widget_content = mock.MagicMock()
        app._claude = app._deepseek = app._google = app._gemini = app._copilot = None

        rows = app._widget_detail_rows()

        credit = next(row for row in rows if row.get("kind") == "credit")
        self.assertEqual(credit["name"], "Credits")
        self.assertEqual(credit["value"], "2,625")
        self.assertNotIn("%", credit["value"])
