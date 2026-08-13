import unittest

from ai_limit.providers import _antigravity_cli_usage_complete


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


if __name__ == "__main__":
    unittest.main()
