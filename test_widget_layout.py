"""Layout regression tests for the menu bar quota dashboard."""

from __future__ import annotations

import datetime
import importlib.util
import locale
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
import usage  # noqa: E402  (imported after the GUI stubs are installed)


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


class _Point:
    def __init__(self, x, y):
        self.x = x
        self.y = y


class _Frame:
    def __init__(self, x, y, width, height):
        self.origin = _Point(x, y)
        self.size = _Size(width, height)


class _ResizablePanel:
    def __init__(self, width=640, height=360):
        self._frame = _Frame(100, 100, width, height)

    def frame(self):
        return self._frame

    def setFrame_display_animate_(self, frame, _display, _animate):
        self._frame = frame


def _detail_rows():
    """A realistic six-service dashboard: sections plus rows with reset lines."""
    rows = []
    for section, row_names in (
        ("Claude", ["5h", "weekly"]),
        ("CodeX", ["Balance / Weekly"]),
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

    def record_ring(_self, _x, y, *_args, **_kwargs):
        positions.append(y)
        return mock.MagicMock()

    with mock.patch.object(menubar.AiLimitApp, "_widget_add_label", record), \
            mock.patch.object(menubar.AiLimitApp, "_widget_add_box", record_box), \
            mock.patch.object(menubar.AiLimitApp, "_widget_add_symbol", record_symbol), \
            mock.patch.object(menubar.AiLimitApp, "_widget_add_progress", record_progress), \
            mock.patch.object(menubar.AiLimitApp, "_widget_add_ring", record_ring), \
            mock.patch.object(menubar.AiLimitApp, "_clear_widget_content", lambda _self: None):
        app._render_widget_dashboard()
    return positions


class DashboardLayoutTests(unittest.TestCase):
    def _render(self, details, alerts=(), cards=(), width=760, height=680):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "zh", "global": "7d", "services": list(menubar._SERVICES), "widget": True}
        app._widget_panel = _Panel(width=width, height=height)
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
            + 3 * (menubar.AiLimitApp._WIDGET_CARD_HEIGHT + menubar.AiLimitApp._WIDGET_CARD_GAP)
            + menubar.AiLimitApp._WIDGET_CARD_BLOCK_EXTRA
            + menubar.AiLimitApp._WIDGET_DETAIL_HEADING_ADVANCE
            + sum(app._widget_detail_row_advance(row) for row in details)
        )

        self.assertGreaterEqual(
            app._widget_layout_height(3, menubar.AiLimitApp._WIDGET_CARD_HEIGHT, [], details),
            consumed,
        )

    def test_narrow_dashboard_uses_one_column_without_negative_positions(self):
        positions = self._render(_detail_rows(), width=390, height=680)

        self.assertTrue(positions, "narrow dashboard drew nothing")
        self.assertGreaterEqual(min(positions), 0)

    def test_service_columns_are_balanced_by_actual_block_height(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)

        _groups, heights = app._widget_detail_columns(_detail_rows(), 2)

        self.assertEqual(len(heights), 2)
        self.assertLessEqual(max(heights) - min(heights), 50)

    def test_balance_and_credit_rows_do_not_draw_percentage_rings(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "en", "global": "7d", "services": list(menubar._SERVICES), "widget": True}
        app._codex = {"7d_left": 50, "credits": {"has_credits": True, "unlimited": False, "balance": 25}}
        app._claude = app._google = app._gemini = app._copilot = None
        app._deepseek = {"primary": {"currency": "CNY", "total_balance": "16.36"}}
        app._widget_panel = _Panel(width=640, height=500)
        app._widget_content = mock.MagicMock()
        rings = []
        with mock.patch.object(menubar.AiLimitApp, "_widget_alert_rows", return_value=[]), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_label"), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_box"), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_ring", side_effect=lambda *args: rings.append(args)), \
                mock.patch.object(menubar.AiLimitApp, "_clear_widget_content", lambda _self: None):
            app._render_widget_dashboard()

        # Only the Codex weekly percentage draws a ring; its credit balance and
        # the DeepSeek currency balance are rendered as plain value rows.
        self.assertEqual(len(rings), 1)
        self.assertEqual(rings[0][-1], 50)

    def test_balance_and_credit_rows_are_not_low_percentage_alerts(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "en", "global": "7d", "services": ["codex", "deepseek"], "widget": True}
        app._codex = {"7d_left": 50, "credits": {"has_credits": True, "unlimited": False, "balance": 25}}
        app._deepseek = {"primary": {"currency": "CNY", "total_balance": "16.36"}}
        app._claude = app._google = app._gemini = app._copilot = None

        self.assertEqual(app._widget_alert_rows(), [])

    def test_alert_rows_include_provider_abbreviations(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        details = [
            {"type": "section", "name": "Antigravity", "color": "#fff"},
            {"name": "Claude + GPT · Weekly", "kind": "percent", "pct": 0, "value": "0%"},
            {"type": "section", "name": "Copilot", "color": "#fff"},
            {"name": "AI Credits", "kind": "percent", "pct": 10, "value": "10%"},
        ]
        with mock.patch.object(menubar.AiLimitApp, "_widget_detail_rows", return_value=details):
            alerts = app._widget_alert_rows()

        self.assertEqual([row["text"] for row in alerts], [
            "AG · Claude + GPT · Weekly",
            "CP · AI Credits",
        ])

    def test_document_height_is_natural_even_when_viewport_is_taller(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {"lang": "en", "global": "7d", "services": list(menubar._SERVICES), "widget": True}
        app._codex = {"credits": {"has_credits": True, "unlimited": False, "balance": "2625"}}
        app._widget_panel = _Panel(width=760, height=900)
        app._widget_content = mock.MagicMock()
        app._widget_last_layout_size = None
        rects = []

        def record_rect(x, y, width, height):
            rect = _Frame(x, y, width, height)
            rects.append(rect)
            return rect

        with mock.patch.object(menubar.AppKit, "NSMakeRect", side_effect=record_rect), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_label"), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_box"), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_symbol"), \
                mock.patch.object(menubar.AiLimitApp, "_widget_add_ring"), \
                mock.patch.object(menubar.AiLimitApp, "_widget_detail_rows", return_value=_detail_rows()), \
                mock.patch.object(menubar.AiLimitApp, "_widget_alert_rows", return_value=[]), \
                mock.patch.object(menubar.AiLimitApp, "_clear_widget_content", lambda _self: None):
            app._render_widget_dashboard()

        document_height = app._widget_content.setFrame_.call_args.args[0].size.height
        self.assertLess(document_height, 900)
        self.assertEqual(document_height, rects[0].size.height)

    def test_panel_resize_preserves_top_and_right_anchors(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        panel = _ResizablePanel()
        app._widget_panel = panel
        with mock.patch.object(menubar.AppKit.NSScreen, "mainScreen", return_value=None), \
                mock.patch.object(menubar.AppKit, "NSMakeRect", side_effect=lambda x, y, w, h: _Frame(x, y, w, h)):
            app._widget_resize_panel(500)

        self.assertEqual(panel.frame().origin.x, 100)
        self.assertEqual(panel.frame().origin.y, -40)
        self.assertEqual(panel.frame().size.width, 640)
        self.assertEqual(panel.frame().size.height, 500)

    def test_rows_with_a_reset_line_reserve_more_height(self):
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        with_reset = {"name": "x", "reset": "10月01日 12:00"}
        without_reset = {"name": "x", "reset": None}

        self.assertEqual(
            app._widget_detail_row_advance(with_reset),
            app._widget_detail_row_advance(without_reset),
        )


class CJKUnderPosixLocaleTests(unittest.TestCase):
    """The packaged app runs with LC_CTYPE=C.

    Under that locale strftime returns an empty string for any format holding
    non-ASCII characters, so ``f"{dt:%m月%d日}"`` renders as nothing inside the
    app while looking correct in a UTF-8 terminal. These tests pin the date
    formatters to that locale so the failure cannot come back unnoticed.
    """

    def setUp(self):
        self._saved = locale.setlocale(locale.LC_CTYPE)
        locale.setlocale(locale.LC_CTYPE, "C")

    def tearDown(self):
        locale.setlocale(locale.LC_CTYPE, self._saved)

    def test_strftime_with_cjk_is_indeed_broken_here(self):
        # Guards the premise of the tests below; if a future macOS/Python makes
        # this work, these tests would otherwise pass for the wrong reason.
        moment = datetime.datetime(2026, 10, 1, 12, 0)
        self.assertEqual(f"{moment:%H:%M}", "12:00")
        if f"{moment:%m月%d日}":
            self.skipTest("this runtime preserves CJK strftime text under the C locale")
        self.assertEqual(f"{moment:%m月%d日}", "")

    def test_copilot_reset_keeps_the_date(self):
        text = menubar._fmt_copilot_reset("2026-10-01T00:00:00.000Z", "zh")

        self.assertIn("10月01日", text)
        self.assertIn("12:00", text)

    def test_copilot_reset_english_keeps_the_date(self):
        text = menubar._fmt_copilot_reset("2026-10-01T00:00:00.000Z", "en")

        self.assertIn("Oct 1", text)
        self.assertIn("12:00", text)

    def test_copilot_reset_explains_why_the_date_is_next_month(self):
        # "本月" labels the allowance window while the date starts the next one,
        # so the countdown has to be there to make the pairing readable.
        moment = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=menubar.TZ_LOCAL)

        class _Now(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 9, 16, 14, 0)
                return value.replace(tzinfo=tz) if tz else value

        with mock.patch.object(menubar.datetime, "datetime", _Now):
            zh = menubar._fmt_copilot_reset(moment.isoformat(), "zh")
            en = menubar._fmt_copilot_reset(moment.isoformat(), "en")

        self.assertEqual(zh, "10月01日 12:00 · 15 天后")
        self.assertEqual(en, "Oct 1 12:00 · in 15 days")

    def test_distant_reset_label_keeps_the_date(self):
        moment = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=menubar.TZ_LOCAL)

        class _Now(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 9, 16, 14, 0)
                return value.replace(tzinfo=tz) if tz else value

        with mock.patch.object(menubar.datetime, "datetime", _Now):
            text = menubar._fmt_reset_dt(moment, "zh")

        self.assertIn("10月01日", text)

    def test_cli_distant_reset_label_keeps_the_date(self):
        moment = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=usage.TZ_LOCAL)

        class _Now(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 9, 16, 14, 0)
                return value.replace(tzinfo=tz) if tz else value

        with mock.patch.object(usage.datetime, "datetime", _Now), \
                mock.patch.object(usage, "LANG", "zh"):
            text = usage.fmt_reset_dt(moment)

        self.assertIn("10月01日", text)


if __name__ == "__main__":
    unittest.main()
