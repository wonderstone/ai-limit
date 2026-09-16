"""Layout regression tests for the menu bar quota dashboard.

The dashboard draws into a fixed-height document view whose height has to be
computed up front. When that estimate is smaller than what the draw loop
consumes, the last rows get a negative y and disappear, which looks like
missing quota data rather than a layout bug. These tests pin that down.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest
from unittest import mock


def _install_gui_stubs():
    """Stub the GUI modules so the menu bar module can be imported headlessly."""
    if "rumps" not in sys.modules:
        rumps = types.ModuleType("rumps")

        class _App:
            def __init__(self, *args, **kwargs):
                pass

        class _MenuItem:
            def __init__(self, title, callback=None):
                self.title = title

            def add(self, *_args):
                pass

        def _timer(_interval):
            def decorate(func):
                return func

            return decorate

        rumps.App = _App
        rumps.MenuItem = _MenuItem
        rumps.timer = _timer
        rumps.quit_application = lambda *_args: None
        sys.modules["rumps"] = rumps

    if "AppKit" not in sys.modules:
        sys.modules["AppKit"] = mock.MagicMock()


def _load_menubar_module():
    _install_gui_stubs()
    path = pathlib.Path(__file__).resolve().parent / "menubar" / "ai-limit-app.py"
    spec = importlib.util.spec_from_file_location("ai_limit_menubar", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


menubar = _load_menubar_module()


class _Size:
    def __init__(self, width, height):
        self.width = width
        self.height = height


class _Bounds:
    def __init__(self, width, height):
        self.size = _Size(width, height)


class _Panel:
    def __init__(self, width=760, height=680):
        self._bounds = _Bounds(width, height)

    def contentView(self):
        return self

    def bounds(self):
        return self._bounds


def _detail_rows():
    """A realistic six-service dashboard: sections plus rows with reset lines."""
    rows = []
    for section, row_names in (
        ("Claude", ["5h", "weekly"]),
        ("CodeX", ["Balance / Weekly", "Spark / 5h", "Spark / Weekly"]),
        ("DeepSeek", ["CNY 16.36"]),
        ("Antigravity", ["Gemini / Weekly", "Gemini / 5h", "Claude+GPT / Weekly", "Claude+GPT / 5h"]),
        ("Gemini", ["当前用量", "每周限额"]),
        ("Copilot", ["Copilot / AI Credits"]),
    ):
        rows.append({"type": "section", "name": section, "color": "#ffffff"})
        for name in row_names:
            rows.append(
                {
                    "name": name,
                    "pct": 90,
                    "value": "90%",
                    # DeepSeek is the one balance row that carries no reset time.
                    "reset": None if name.startswith("CNY") else "10月01日 12:00",
                    "color": "#4ade80",
                    "disabled": False,
                }
            )
    return rows


def _make_app(details, alerts, cards):
    app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
    app._state = {"lang": "zh", "global": "7d", "services": list(menubar._SERVICES), "widget": True}
    app._widget_panel = _Panel()
    app._widget_content = mock.MagicMock()
    app._widget_last_layout_size = None
    with mock.patch.object(menubar.AiLimitApp, "_widget_summary_cards", return_value=cards), \
            mock.patch.object(menubar.AiLimitApp, "_widget_alert_rows", return_value=alerts), \
            mock.patch.object(menubar.AiLimitApp, "_widget_detail_rows", return_value=details):
        yield_positions = _capture_draw_positions(app)
    return yield_positions


def _capture_draw_positions(app):
    positions = []

    def record(_self, *args, **_kwargs):
        # Every drawing helper takes (..., x, y, ...) with y as the 3rd arg.
        positions.append(args[2] if len(args) > 2 else 0)
        return mock.MagicMock()

    def record_box(_self, _x, y, *_args, **_kwargs):
        positions.append(y)
        return mock.MagicMock()

    def record_symbol(_self, _name, _x, y, *_args, **_kwargs):
        positions.append(y)
        return mock.MagicMock()

    def record_progress(_self, _x, y, *_args, **_kwargs):
        positions.append(y)
        return mock.MagicMock()

    with mock.patch.object(menubar.AiLimitApp, "_widget_add_label", record), \
            mock.patch.object(menubar.AiLimitApp, "_widget_add_box", record_box), \
            mock.patch.object(menubar.AiLimitApp, "_widget_add_symbol", record_symbol), \
            mock.patch.object(menubar.AiLimitApp, "_widget_add_progress", record_progress), \
            mock.patch.object(menubar.AiLimitApp, "_clear_widget_content", lambda _self: None):
        app._render_widget_dashboard()
    return positions


class DashboardLayoutTests(unittest.TestCase):
    def _render(self, details, alerts=(), cards=()):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "zh", "global": "7d", "services": list(menubar._SERVICES), "widget": True}
        app._widget_panel = _Panel()
        app._widget_content = mock.MagicMock()
        app._widget_last_layout_size = None
        with mock.patch.object(menubar.AiLimitApp, "_widget_summary_cards", return_value=list(cards)), \
                mock.patch.object(menubar.AiLimitApp, "_widget_alert_rows", return_value=list(alerts)), \
                mock.patch.object(menubar.AiLimitApp, "_widget_detail_rows", return_value=list(details)):
            return _capture_draw_positions(app)

    def test_nothing_is_drawn_below_the_document_view(self):
        alerts = [{
            "text": "Claude and GPT models / Weekly",
            "value": "0%",
            "bg": "#2a171a",
            "border": "#7f1d1d",
            "fg": "#fecdd3",
            "symbol": "exclamationmark.triangle.fill",
            "pct": 0,
        }]
        cards = [
            {
                "title": name,
                "symbol": "gauge",
                "accent": "#0f766e",
                "pct": 90,
                "value": "90%",
                "value_color": "#4ade80",
                "subtitle": "",
                "metrics": [{"label": "5h", "value": "90%", "pct": 90, "color": "#4ade80", "reset": None}],
                "reset_text": "本月 ↻ 10月01日 12:00",
                "bg": "#202124",
                "border": "#34343a",
            }
            for name in ("Claude", "CodeX", "Antigravity", "Gemini", "Copilot", "DeepSeek")
        ]

        positions = self._render(_detail_rows(), alerts, cards)

        self.assertTrue(positions, "dashboard drew nothing")
        self.assertGreaterEqual(
            min(positions),
            0,
            "dashboard drew content below the document view, so the last rows are invisible",
        )

    def test_layout_height_covers_every_row_advance(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        details = _detail_rows()
        consumed = (
            menubar.AiLimitApp._WIDGET_TOP_MARGIN
            + menubar.AiLimitApp._WIDGET_HEADER_ADVANCE
            + 3 * (100 + 12)
            + menubar.AiLimitApp._WIDGET_CARD_BLOCK_EXTRA
            + menubar.AiLimitApp._WIDGET_DETAIL_HEADING_ADVANCE
            + sum(app._widget_detail_row_advance(row) for row in details)
        )

        self.assertGreaterEqual(app._widget_layout_height(3, 100, [], details), consumed)

    def test_rows_with_a_reset_line_reserve_more_height(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        with_reset = {"name": "x", "reset": "10月01日 12:00"}
        without_reset = {"name": "x", "reset": None}

        self.assertGreater(
            app._widget_detail_row_advance(with_reset),
            app._widget_detail_row_advance(without_reset),
        )


if __name__ == "__main__":
    unittest.main()
