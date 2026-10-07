"""Local reset times must follow the date's DST rules, not startup's offset."""
import datetime
import os
import time
import unittest
from unittest import mock

import usage
from test_widget_layout import menubar


@unittest.skipUnless(hasattr(time, "tzset"), "requires POSIX timezone support")
class LocalTimeTests(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {"TZ": "Pacific/Auckland"})
        self.env.start()
        time.tzset()

    def tearDown(self):
        self.env.stop()
        time.tzset()

    def test_both_dst_boundaries_preserve_absolute_reset(self):
        cases = [
            ("2026-09-26T13:59:00Z", "2026-09-27 01:59", "NZST", 12),
            ("2026-09-26T14:00:00Z", "2026-09-27 03:00", "NZDT", 13),
            ("2027-04-03T13:59:00Z", "2027-04-04 02:59", "NZDT", 13),
            ("2027-04-03T14:00:00Z", "2027-04-04 02:00", "NZST", 12),
        ]
        for iso, clock, abbreviation, offset in cases:
            with self.subTest(iso=iso):
                original = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
                local = usage.ts_to_local(iso)
                self.assertEqual(local.strftime("%Y-%m-%d %H:%M"), clock)
                self.assertEqual(local.utcoffset(), datetime.timedelta(hours=offset))
                self.assertEqual(local.timestamp(), original.timestamp())
                self.assertEqual(usage.epoch_to_local(int(original.timestamp())), local)
                self.assertTrue(usage.fmt_dt(local).endswith(abbreviation))
                self.assertTrue(usage.fmt_reset_dt(local).endswith(abbreviation))
                for language in ("zh", "en"):
                    self.assertIn(clock[-5:], menubar._fmt_reset_iso(iso, language))
                    self.assertIn(clock[-5:], menubar._fmt_reset_epoch(original.timestamp(), language))

    def test_timezone_change_without_module_reload(self):
        iso = "2026-10-01T00:00:00Z"
        self.assertEqual(usage.ts_to_local(iso).hour, 13)
        os.environ["TZ"] = "UTC"
        time.tzset()
        self.assertEqual(usage.ts_to_local(iso).hour, 0)
        self.assertIn("00:00", menubar._fmt_reset_iso(iso))

    def test_elapsed_time_across_spring_jump_is_not_wall_clock_delta(self):
        before = usage.ts_to_local("2026-09-26T13:30:00Z")
        after = usage.ts_to_local("2026-09-26T14:30:00Z")
        self.assertEqual(after - before, datetime.timedelta(hours=1))


if __name__ == "__main__":
    unittest.main()
