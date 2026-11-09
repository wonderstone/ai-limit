"""Numbers kept after a failed refresh use their own color, not a quota warning."""
import unittest

from test_widget_layout import menubar


def _record(status):
    return {"status": status, "currency": "USD", "promotional_balance": 400.0,
            "purchased_balance": 19.7, "grants": [], "error_code": "fetch_failed" if status == "stale" else None}


class StaleValueColorTests(unittest.TestCase):
    def _rows(self, openai_status):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "zh", "global": "7d", "services": [], "widget": True}
        app._claude = app._codex = app._deepseek = app._google = app._gemini = app._copilot = app._grok = None
        app._api_credits_v1 = {"openai": _record(openai_status), "claude": _record("ok")}
        return app._widget_detail_rows()

    def test_stale_api_credit_values_use_the_stale_color(self):
        rows = self._rows("stale")
        openai = rows[[r.get("name") for r in rows].index("OpenAI API · 缓存") + 1]
        self.assertEqual(openai["value"], "$400.00")
        self.assertEqual(openai["color"], menubar._STALE_VALUE_COLOR)
        self.assertTrue(openai["stale"])

    def test_fresh_sections_keep_their_own_color(self):
        rows = self._rows("stale")
        claude = rows[[r.get("name") for r in rows].index("Claude API") + 1]
        self.assertEqual(claude["color"], "#c4b5fd")
        self.assertNotIn("stale", claude)

    def test_stale_color_differs_from_every_warning_color(self):
        warnings = {menubar._widget_risk_color(p) for p in (0, 10, 50)} | {"#fb923c", "#fbbf24"}
        self.assertNotIn(menubar._STALE_VALUE_COLOR, warnings)


if __name__ == "__main__":
    unittest.main()
