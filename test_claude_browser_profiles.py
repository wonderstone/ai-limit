import pathlib
import types
import unittest
import urllib.error
from unittest.mock import patch

from ai_limit import providers


class ClaudeBrowserProfilesTest(unittest.TestCase):
    def test_rejected_chrome_session_falls_back_to_canary(self):
        sources = [
            ("Chrome/Profile 1", pathlib.Path("/chrome/Cookies")),
            ("Chrome Canary/Default", pathlib.Path("/canary/Cookies")),
        ]

        def cookies(cookie_file, domain_name):
            org = pathlib.Path(cookie_file).parent.name
            return [
                types.SimpleNamespace(name=name, value=value, domain=domain_name)
                for name, value in (("lastActiveOrg", org), ("sessionKey", "test-session"))
            ]

        def fetch(path, headers, timeout):
            if path == "/api/organizations/chrome/usage":
                raise providers.ClaudeWebError("rejected") from urllib.error.HTTPError(
                    "", 403, "Forbidden", {}, None
                )
            self.assertEqual(path, "/api/organizations/canary/usage")
            return {"seven_day": {"utilization": 64}}

        with patch("browser_cookie3.chrome", side_effect=cookies), patch.object(
            providers, "_chromium_profile_cookie_files", return_value=sources
        ), patch.object(providers, "_claude_web_get", side_effect=fetch) as request:
            self.assertEqual(providers.live_claude_usage()["seven_day"]["utilization"], 64)
        self.assertEqual(request.call_count, 2)
