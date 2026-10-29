import datetime
import os
import unittest
from unittest import mock

from ai_limit import providers
from ai_limit.providers import (
    CopilotAuthError,
    CopilotQuotaError,
    _normalize_copilot_quota,
    load_copilot_github_token,
)
import usage


def _snapshot(**overrides):
    base = {
        "overage_count": 0,
        "overage_permitted": True,
        "percent_remaining": 94.5,
        "quota_id": "premium_interactions",
        "quota_remaining": 18906.2,
        "unlimited": False,
        "timestamp_utc": "2026-09-15T19:40:08.509-07:00",
        "has_quota": True,
        "credits_used": 1093,
        "remaining": 18906,
        "entitlement": 20000,
    }
    base.update(overrides)
    return base


class NormalizeCopilotQuotaTests(unittest.TestCase):
    def test_premium_bucket_drives_summary_and_unlimited_buckets_stay_full(self):
        data = _normalize_copilot_quota(
            {
                "login": "octocat",
                "copilot_plan": "individual_max",
                "token_based_billing": True,
                "quota_reset_date_utc": "2026-10-01T00:00:00.000Z",
                "quota_snapshots": {
                    "chat": _snapshot(quota_id="chat", unlimited=True, entitlement=0, remaining=0, credits_used=0),
                    "premium_interactions": _snapshot(),
                },
            }
        )

        self.assertEqual(data["plan_label"], "Max")
        self.assertEqual(data["unit"], "AI credits")
        self.assertEqual(data["primary"]["bucket_id"], "premium_interactions")
        self.assertEqual(data["primary"]["display_name"], "AI Credits")
        self.assertEqual(data["summary"]["remaining_percent"], 94)
        self.assertEqual(data["summary"]["remaining"], 18906)
        self.assertEqual(data["summary"]["entitlement"], 20000)
        self.assertEqual(data["summary"]["used"], 1093)
        self.assertEqual(data["summary"]["reset_time"], "2026-10-01T00:00:00.000Z")
        chat = next(bucket for bucket in data["buckets"] if bucket["bucket_id"] == "chat")
        self.assertTrue(chat["unlimited"])
        self.assertEqual(chat["remaining_percent"], 100)

    def test_legacy_request_billing_uses_premium_request_wording(self):
        data = _normalize_copilot_quota(
            {
                "copilot_plan": "individual_pro",
                "token_based_billing": False,
                "quota_reset_date": "2026-10-01",
                "quota_snapshots": {
                    "premium_interactions": _snapshot(percent_remaining=40, remaining=120, entitlement=300, credits_used=None),
                },
            }
        )

        self.assertEqual(data["unit"], "premium requests")
        self.assertEqual(data["primary"]["display_name"], "Premium requests")
        self.assertEqual(data["summary"]["remaining_percent"], 40)
        # used is derived when the payload does not carry credits_used
        self.assertEqual(data["summary"]["used"], 180)

    def test_missing_snapshots_is_an_error_not_a_full_quota(self):
        with self.assertRaises(CopilotQuotaError):
            _normalize_copilot_quota({"copilot_plan": "individual_pro", "quota_snapshots": {}})


class TokenResolutionTests(unittest.TestCase):
    def test_env_var_precedence_matches_copilot_cli(self):
        env = {"GH_TOKEN": "gho_from_gh", "COPILOT_GITHUB_TOKEN": "gho_from_copilot"}
        with mock.patch.dict(os.environ, env, clear=False):
            token, source = load_copilot_github_token()
        self.assertEqual((token, source), ("gho_from_copilot", "COPILOT_GITHUB_TOKEN"))

    def test_non_token_env_values_are_ignored(self):
        with (
            mock.patch.dict(os.environ, {"GH_TOKEN": "not-a-token"}, clear=False),
            mock.patch.object(providers, "_copilot_token_from_keychain", return_value=None),
            mock.patch.object(providers, "_copilot_token_from_gh", return_value="gho_fallback"),
        ):
            for name in providers.COPILOT_TOKEN_ENV_VARS:
                if name != "GH_TOKEN":
                    os.environ.pop(name, None)
            token, source = load_copilot_github_token()
        self.assertEqual((token, source), ("gho_fallback", "gh auth token"))

    def test_no_credentials_raises_auth_error(self):
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(providers, "_copilot_token_from_keychain", return_value=None),
            mock.patch.object(providers, "_copilot_token_from_gh", return_value=None),
        ):
            for name in providers.COPILOT_TOKEN_ENV_VARS:
                os.environ.pop(name, None)
            with self.assertRaises(CopilotAuthError):
                load_copilot_github_token()


class ResetLabelTests(unittest.TestCase):
    def test_reset_beyond_next_week_uses_calendar_date(self):
        reset = datetime.datetime(2026, 10, 1, 12, 0).astimezone()

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 9, 16, 14, 0)
                return value.replace(tzinfo=tz) if tz else value

        with (
            mock.patch.object(usage.datetime, "datetime", FixedDateTime),
            mock.patch.object(usage, "LANG", "zh"),
        ):
            label = usage.fmt_reset_dt(reset)

        self.assertIn("10月01日 12:00", label)
        self.assertNotIn("下周", label)

class EffectiveEntitlementTests(unittest.TestCase):
    def test_free_sku_overrides_individual_and_uses_real_free_bucket(self):
        result = _normalize_copilot_quota({
            'copilot_plan': 'individual', 'access_type_sku': 'free_limited_copilot',
            'token_based_billing': True, 'quota_snapshots': {
                'chat': _snapshot(quota_id='chat', remaining=200, entitlement=200, percent_remaining=100),
                'completions': _snapshot(quota_id='completions', remaining=2000, entitlement=2000, percent_remaining=100),
                'premium_interactions': _snapshot(remaining=0, entitlement=0, percent_remaining=0, has_quota=False),
            }})
        self.assertEqual(result['plan_label'], 'Free')
        self.assertEqual(result['subscription_status'], 'free')
        self.assertEqual(result['subscription_source'], 'access_type_sku')
        self.assertTrue(result['available'])
        self.assertEqual(result['primary']['bucket_id'], 'chat')
        self.assertEqual(result['summary']['remaining'], 200)

    def test_unknown_sku_does_not_claim_legacy_pro(self):
        result = _normalize_copilot_quota({'copilot_plan': 'individual', 'access_type_sku': 'future_unknown_sku'})
        self.assertEqual(result['subscription_status'], 'unknown')
        self.assertNotEqual(result['plan_label'], 'Pro')

    def test_malformed_entitlement_does_not_inherit_pro(self):
        for sku in (False, 1, [], {}):
            with self.subTest(sku=sku):
                result = _normalize_copilot_quota({'copilot_plan':'individual', 'access_type_sku':sku})
                self.assertEqual(result['subscription_status'], 'unknown')
                self.assertIsNone(result['plan_label'])

    def test_free_sku_wins_over_stale_paid_plan(self):
        for plan in ('individual_pro', 'individual_max', None):
            result = _normalize_copilot_quota({'copilot_plan': plan, 'access_type_sku': 'free_limited_copilot'})
            self.assertEqual(result['subscription_status'], 'free')
            self.assertEqual(result['plan_label'], 'Free')


class FreePresentationTests(unittest.TestCase):
    def test_free_header_and_only_entitled_buckets_are_visible(self):
        from test_widget_layout import menubar
        raw = {'copilot_plan': 'individual', 'access_type_sku': 'free_limited_copilot',
               'quota_snapshots': {'chat': _snapshot(remaining=200, entitlement=200, percent_remaining=100),
                                   'premium_interactions': _snapshot(remaining=0, entitlement=0, percent_remaining=0, has_quota=False)}}
        app = menubar.AiLimitApp.__new__(menubar.AiLimitApp)
        app._state = {'lang': 'en', 'services': ['copilot']}
        with mock.patch.object(menubar, 'live_copilot_quota', return_value=(None, _normalize_copilot_quota(raw))):
            app._copilot = menubar._fetch_copilot('en')
        rows = app._widget_detail_rows()
        self.assertIn('Copilot Free', [row.get('name') for row in rows])
        self.assertFalse(any('Pro' in row.get('name','') for row in rows))
        self.assertEqual(len(app._widget_copilot_entries()), 1)
        self.assertIn('200 / 200', app._widget_copilot_entries()[0]['name'])
        self.assertEqual(app._copilot['subscription_source'], 'access_type_sku')

    def test_old_pro_cache_is_not_reused_at_startup(self):
        import json, pathlib, tempfile, time
        from test_widget_layout import menubar
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp)/'cache.json'
            path.write_text(json.dumps({'cached_at':time.time(), 'claude':{'5h_left':20},
                                        'copilot':{'plan':'Pro','subscription_status':'active'}}))
            with mock.patch.object(menubar, '_CACHE_PATH', path):
                claude, cached = menubar._load_cache()
        self.assertEqual(claude, {'5h_left':20})
        self.assertIsNone(cached)


if __name__ == '__main__':unittest.main()
