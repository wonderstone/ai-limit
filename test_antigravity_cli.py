import unittest

from ai_limit.providers import _antigravity_cli_usage_complete, _with_antigravity_view


class AntigravityCliUsageCompleteTests(unittest.TestCase):
    def test_accepts_complete_screen_with_quota_available_windows(self):
        transcript = """
        Models & Quota
        GEMINI MODELS
        Weekly Limit Remaining
        78.93%
        79% remaining · Refreshes in 138h 52m
        Five Hour Limit Remaining
        85.36%
        85% remaining · Refreshes in 58m
        CLAUDE AND GPT MODELS
        Weekly Limit Remaining
        100.00%
        Quota available
        Five Hour Limit Remaining
        100.00%
        Quota available
        """

        self.assertTrue(_antigravity_cli_usage_complete(transcript))

    def test_rejects_partial_screen_missing_a_window(self):
        transcript = """
        Models & Quota
        GEMINI MODELS
        Weekly Limit Remaining
        79% remaining · Refreshes in 138h 52m
        Five Hour Limit Remaining
        85% remaining · Refreshes in 58m
        CLAUDE AND GPT MODELS
        Weekly Limit Remaining
        100.00%
        Quota available
        """

        self.assertFalse(_antigravity_cli_usage_complete(transcript))

    def test_unavailable_view_keeps_normal_quota_group_shape(self):
        data = _with_antigravity_view(
            {"source": "oauth", "summary": {}, "buckets": []},
            None,
            ["app unavailable", "agy /usage timed out"],
        )

        self.assertEqual(
            [group["display_name"] for group in data["quota_groups"]],
            ["Gemini Models", "Claude and GPT models"],
        )
        self.assertEqual(
            [[bucket["window"] for bucket in group["buckets"]] for group in data["quota_groups"]],
            [["weekly", "5h"], ["weekly", "5h"]],
        )
        self.assertTrue(all(bucket["remaining_percent"] is None for bucket in data["buckets"]))
        self.assertEqual(data["summary"]["quota_state"], "unavailable")
        self.assertEqual(data["summary"]["group_count"], 2)
        self.assertEqual(data["summary"]["bucket_count"], 4)


if __name__ == "__main__":
    unittest.main()
