import struct
import unittest

from ai_limit.providers import (
    GrokAuthError,
    GrokUsageError,
    _normalize_grok_credits_config,
    _parse_grpc_web_proto,
)


# ── Minimal protobuf encoder (mirrors grok_build_billing.proto field numbers) ──

def _varint(value):
    value &= (1 << 64) - 1
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field_varint(number, value):
    return _varint(number << 3) + _varint(value)


def _field_bytes(number, payload):
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


def _field_float(number, value):
    return _varint((number << 3) | 5) + struct.pack("<f", value)


def _timestamp(seconds):
    return _field_varint(1, seconds)


def _cent(value):
    return _field_varint(1, value)


def _config(
    *,
    used=None,
    unified=True,
    period_type=2,
    start=1791777868,
    end=1792382668,
    prepaid=0,
    on_demand_cap=0,
    products=(),
):
    body = b""
    if used is not None:
        body += _field_float(1, used)
    if on_demand_cap:
        body += _field_bytes(2, _cent(on_demand_cap))
    body += _field_bytes(4, _timestamp(start))
    body += _field_bytes(5, _timestamp(end))
    for product, pct in products:
        body += _field_bytes(7, _field_varint(1, product) + _field_float(2, pct))
    body += _field_bytes(8, _field_varint(1, period_type) + _field_bytes(2, _timestamp(start)) + _field_bytes(3, _timestamp(end)))
    if unified:
        body += _field_varint(11, 1)
    if prepaid:
        body += _field_bytes(12, _cent(prepaid))
    return _field_bytes(1, body)


def _grpc_web(message, status=0, detail=""):
    trailer = f"grpc-status:{status}\r\n".encode()
    if detail:
        trailer += f"grpc-message:{detail}\r\n".encode()
    return (
        b"\x00" + struct.pack(">I", len(message)) + message
        + b"\x80" + struct.pack(">I", len(trailer)) + trailer
    )


class NormalizeGrokCreditsConfigTests(unittest.TestCase):
    def test_fresh_week_reads_full_pool_and_reset(self):
        data = _normalize_grok_credits_config(_config())

        self.assertEqual(data["period_type"], "weekly")
        self.assertEqual(data["summary"]["used_percent"], 0)
        self.assertEqual(data["summary"]["remaining_percent"], 100)
        self.assertEqual(data["reset_time"], "2026-10-19T04:04:28Z")
        self.assertEqual(data["period_start"], "2026-10-12T04:04:28Z")
        self.assertEqual(data["prepaid_balance_cents"], 0)
        self.assertEqual(data["products"], [])

    def test_used_percent_rounds_up_like_the_usage_page(self):
        data = _normalize_grok_credits_config(_config(used=42.3))

        # grok.com prints Math.ceil(used): "43% used".
        self.assertEqual(data["summary"]["used_percent"], 43)
        self.assertEqual(data["summary"]["remaining_percent"], 57)
        self.assertAlmostEqual(data["summary"]["used_percent_exact"], 42.3, places=2)

    def test_products_credits_and_on_demand_are_decoded(self):
        data = _normalize_grok_credits_config(
            _config(used=30, prepaid=1250, on_demand_cap=500, products=[(4, 12.5), (5, 17.5), (9, 0)])
        )

        self.assertEqual(
            [(item["product"], item["used_percent"]) for item in data["products"]],
            [("imagine", 17.5), ("chat", 12.5), ("grok_bot", 0)],
        )
        self.assertEqual(data["prepaid_balance_cents"], 1250)
        self.assertEqual(data["summary"]["prepaid_balance_usd"], 12.5)
        self.assertTrue(data["summary"]["on_demand_enabled"])

    def test_over_quota_clamps_to_zero_remaining(self):
        data = _normalize_grok_credits_config(_config(used=104.0))

        self.assertEqual(data["summary"]["remaining_percent"], 0)

    def test_account_off_the_unified_pool_is_an_error_not_a_number(self):
        with self.assertRaises(GrokUsageError):
            _normalize_grok_credits_config(_config(unified=False))


class GrpcWebFramingTests(unittest.TestCase):
    def test_data_frame_is_returned_and_trailer_ignored_on_ok(self):
        message = _config(used=5)
        self.assertEqual(_parse_grpc_web_proto(_grpc_web(message)), message)

    def test_unauthenticated_trailer_is_an_auth_error(self):
        with self.assertRaises(GrokAuthError):
            _parse_grpc_web_proto(_grpc_web(b"", status=16, detail="no%20session"))

    def test_other_grpc_status_is_a_usage_error(self):
        with self.assertRaises(GrokUsageError) as ctx:
            _parse_grpc_web_proto(_grpc_web(b"", status=13, detail="boom"))
        self.assertNotIsInstance(ctx.exception, GrokAuthError)
        self.assertIn("13", str(ctx.exception))


class GrokWidgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from test_widget_layout import menubar

        cls.menubar = menubar

    def _app(self, grok, lang="en"):
        app = self.menubar.AiLimitApp.__new__(self.menubar.AiLimitApp)
        app._state = {"lang": lang, "global": "7d", "services": ["grok"], "widget": True}
        app._claude = app._codex = app._deepseek = app._google = app._gemini = app._copilot = None
        app._grok = grok
        return app

    def test_card_shows_weekly_remaining_and_reset(self):
        app = self._app({"left": 64, "used": 36, "reset": "2026-10-19T04:04:28Z", "window": "weekly"})

        card = app._widget_card_for_service("grok", "Grok", "bolt.fill", "#52525b", app._grok)

        self.assertEqual([(m["label"], m["pct"]) for m in card["metrics"]], [("1w", 64)])
        self.assertTrue(card["reset_text"].startswith("Resets "))

    def test_detail_rows_add_credit_row_only_when_funded(self):
        app = self._app({"left": 80, "reset": "2026-10-19T04:04:28Z", "window": "weekly", "prepaid_balance_cents": 0})
        names = [row.get("name") for row in app._widget_detail_rows()]
        self.assertEqual(names, ["Grok", "Grok / Weekly"])

        app._grok["prepaid_balance_cents"] = 1250
        rows = app._widget_detail_rows()
        credit = rows[-1]
        self.assertEqual(credit["value"], "$12.50")
        self.assertEqual(credit["kind"], "credit")

    def test_low_pool_alerts_under_grok_code(self):
        app = self._app({"left": 8, "reset": "2026-10-19T04:04:28Z", "window": "weekly"})

        alerts = app._widget_alert_rows()

        self.assertEqual([row["provider"] for row in alerts], ["GK"])
        self.assertEqual(app._compact_bar_items(), [("GK", 8, "percent", False)])

    def test_error_hides_rows_and_flags_bar(self):
        app = self._app({"error": "grok.com session expired"})

        self.assertEqual(app._widget_detail_rows(), [])
        self.assertEqual(app._compact_bar_items(), [("AI", 0, "percent", True)])


if __name__ == "__main__":
    unittest.main()
