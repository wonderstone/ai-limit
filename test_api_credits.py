"""Deterministic contract, binding, cache, and expiry tests for API credits."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from ai_limit import api_credits, providers


def _claude_credit(
    *,
    org_id="claude-fixture-org",
    org_name="Claude Fixture Org",
    balance=17.25,
    promo_tranches=None,
    purchased_balance=7.0,
    currency="USD",
    **extra,
):
    raw = {
        "currency": currency,
        "balance": {"money": {"amount_minor": round(balance * 100), "exponent": 2}},
        "balance_credits": balance,
        "promo_tranches": promo_tranches if promo_tranches is not None else [
            {
                "name": "Fixture promotion",
                "program_id": "fixture_promo",
                "remaining_amount_minor_units": 1025,
                "granted_amount_minor_units": 1200,
                "remaining": {"money": None, "credits": {"amount_minor": 1025, "exponent": 2}},
                "granted": {"money": None, "credits": {"amount_minor": 1200, "exponent": 2}},
                "expires_at": "2099-12-31T00:00:00Z",
                "granted_at": "2099-01-01T00:00:00Z",
            }
        ],
        "purchased_balance": {"money": {"amount_minor": round(purchased_balance * 100), "exponent": 2}},
        "tranches": [],
        **extra,
    }
    org = {"uuid": org_id, "name": org_name, "billing_type": "prepaid", "capabilities": ["api"]}
    return providers._normalize_claude_api_credits(org, raw)


class APICreditTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "api-credit-profiles.json"
        self.config_patch = mock.patch.object(api_credits, "PROFILE_CONFIG_PATH", self.config_path)
        self.config_patch.start()
        api_credits.clear_api_credit_cache()
        self.addCleanup(api_credits.clear_api_credit_cache)
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def configure(self, *, openai=None, claude=None):
        value = {}
        if openai is not None:
            value["openai"] = openai
        if claude is not None:
            value["claude"] = claude
        self.config_path.write_text(json.dumps(value), encoding="utf-8")

    @staticmethod
    def claude_binding(org_id="claude-fixture-org", profile="claude-fixture"):
        return {
            "profile": profile,
            "account": "Fixture Claude account",
            "organization_id": org_id,
            "organization_name": "Claude Fixture Org",
        }

    def test_unknown_is_null_and_never_coerced_to_zero(self):
        result = api_credits.live_api_credit_balances()
        for provider in ("openai", "claude"):
            self.assertEqual(result[provider]["status"], "unknown")
            self.assertEqual(result[provider]["error_code"], "binding_required")
            self.assertIsNone(result[provider]["promotional_balance"])
            self.assertIsNone(result[provider]["purchased_balance"])
            self.assertIsNone(result[provider]["total"])
            self.assertIsNone(result[provider]["grants"])

    def test_openai_with_binding_requires_explicit_session_permission(self):
        self.configure(openai={
            "profile": "openai-fixture",
            "organization_id": "org-fixture-1",
            "organization_name": "Fixture OpenAI organization",
        })
        result = api_credits.live_api_credit_balances()["openai"]
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["binding_status"], "unverified")
        self.assertEqual(result["error_code"], "platform_session_permission_required")
        self.assertIsNone(result["total"])
        self.assertEqual(result["billing_url"], api_credits.OPENAI_BILLING_URL)

    def test_openai_verified_fixture_can_use_the_collector_seam(self):
        self.configure(openai={
            "profile": "openai-fixture",
            "organization_id": "org-fixture-1",
            "organization_name": "Fixture OpenAI organization",
        })

        def verified_fixture(binding, _timeout, checked_at):
            record = api_credits._base_record(
                "openai", binding, checked_at=checked_at, status="ok", binding_status="bound"
            )
            record.update({
                "currency": "USD", "promotional_balance": 5.0,
                "purchased_balance": 10.0, "total": 15.0,
                "grants": [{"name": "fixture", "remaining": 5.0, "expires_at": "2099-01-01T00:00:00Z"}],
            })
            return record

        with mock.patch.dict(api_credits._COLLECTORS, {"openai": verified_fixture}):
            result = api_credits.live_api_credit_balances(force=True)["openai"]
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["binding_status"], "bound")
        self.assertEqual(result["promotional_balance"], 5.0)
        self.assertEqual(result["purchased_balance"], 10.0)
        self.assertEqual(result["total"], 15.0)

    def test_claude_separates_promotions_purchases_and_each_expiry(self):
        self.configure(claude=self.claude_binding())
        fixture = _claude_credit(
            balance=17.25,
            purchased_balance=7.0,
            promo_tranches=[
                {
                    "name": "First fixture grant",
                    "program_id": "promo_a",
                    "remaining": {"money": None, "credits": {"amount_minor": 625, "exponent": 2}},
                    "granted": {"money": None, "credits": {"amount_minor": 700, "exponent": 2}},
                    "expires_at": "2099-11-01T00:00:00Z",
                },
                {
                    "name": "Second fixture grant",
                    "program_id": "promo_b",
                    "remaining": {"money": None, "credits": {"amount_minor": 400, "exponent": 2}},
                    "granted": {"money": None, "credits": {"amount_minor": 450, "exponent": 2}},
                    "expires_at": "2099-12-01T00:00:00Z",
                },
            ],
            usage_restrictions=["fixture restriction"],
        )
        with mock.patch.object(providers, "live_claude_api_credits", return_value=fixture) as collect:
            result = api_credits.live_api_credit_balances(force=True)["claude"]
        collect.assert_called_once_with(
            timeout=15,
            organization_id="claude-fixture-org",
            profile="claude-fixture",
        )
        self.assertEqual(result["contract"], "api-credit/v1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["binding_status"], "bound")
        self.assertEqual(result["promotional_balance"], 10.25)
        self.assertEqual(result["purchased_balance"], 7.0)
        self.assertEqual(result["total"], 17.25)
        self.assertEqual([item["name"] for item in result["grants"]], ["First fixture grant", "Second fixture grant"])
        self.assertEqual([item["expires_at"] for item in result["grants"]], ["2099-11-01T00:00:00Z", "2099-12-01T00:00:00Z"])
        self.assertIsNone(result["source_updated_at"])
        self.assertIsNone(result["next_grant_at"])
        self.assertEqual(result["usage_restrictions"], ["fixture restriction"])

    def test_source_reported_zero_stays_zero_while_missing_fields_stay_null(self):
        self.configure(claude=self.claude_binding())
        explicit_zero = _claude_credit(
            balance=0,
            purchased_balance=0,
            promo_tranches=[],
        )
        with mock.patch.object(providers, "live_claude_api_credits", return_value=explicit_zero):
            zero = api_credits.live_api_credit_balances(force=True)["claude"]
        self.assertEqual(zero["status"], "ok")
        self.assertEqual(zero["promotional_balance"], 0.0)
        self.assertEqual(zero["purchased_balance"], 0.0)
        self.assertEqual(zero["total"], 0.0)
        self.assertEqual(zero["grants"], [])

        missing = providers._normalize_claude_api_credits(
            {"uuid": "claude-fixture-org", "name": "Claude Fixture Org"},
            {},
        )
        api_credits.clear_api_credit_cache()
        with mock.patch.object(providers, "live_claude_api_credits", return_value=missing):
            unknown = api_credits.live_api_credit_balances(force=True)["claude"]
        self.assertEqual(unknown["status"], "unknown")
        self.assertIsNone(unknown["promotional_balance"])
        self.assertIsNone(unknown["purchased_balance"])
        self.assertIsNone(unknown["total"])
        self.assertIsNone(unknown["grants"])

    def test_claude_collector_uses_exact_org_when_multiple_prepaid_orgs_exist(self):
        org_a = {"uuid": "api-org-a", "name": "API A", "billing_type": "prepaid", "capabilities": ["api"]}
        org_b = {"uuid": "api-org-b", "name": "API B", "billing_type": "prepaid", "capabilities": ["api"]}
        requested = []

        def get(path, _headers, _timeout):
            requested.append(path)
            if path == "/api/organizations":
                return [org_a, org_b]
            if path == "/api/organizations/api-org-b/prepaid/credits":
                return {"balance": {"money": {"amount_minor": 500, "exponent": 2}}, "promo_tranches": []}
            raise AssertionError("unexpected organization request")

        with mock.patch.object(providers, "_claude_platform_headers", return_value={}), mock.patch.object(
            providers, "_claude_platform_get", side_effect=get
        ):
            result = providers.live_claude_api_credits(organization_id="api-org-b")
        self.assertEqual(result["org_id"], "api-org-b")
        self.assertEqual(requested[-1], "/api/organizations/api-org-b/prepaid/credits")

    def test_expired_grant_is_zero_spendable_and_removed_from_safe_total(self):
        self.configure(claude=self.claude_binding())
        expired = _claude_credit(
            balance=10,
            purchased_balance=2,
            promo_tranches=[{
                "name": "Expired fixture grant",
                "program_id": "fixture_expired",
                "remaining": {"money": None, "credits": {"amount_minor": 800, "exponent": 2}},
                "granted": {"money": None, "credits": {"amount_minor": 800, "exponent": 2}},
                "expires_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
            }],
        )
        with mock.patch.object(providers, "live_claude_api_credits", return_value=expired):
            result = api_credits.live_api_credit_balances(force=True)["claude"]
        self.assertEqual(result["promotional_balance"], 0.0)
        self.assertEqual(result["purchased_balance"], 2.0)
        self.assertEqual(result["total"], 2.0)
        self.assertEqual(result["grants"][0]["remaining"], 0.0)
        self.assertTrue(result["grants"][0]["expired"])
        self.assertFalse(result["grants"][0]["spendable"])

    def test_mismatch_discards_cached_money_for_the_explicit_binding(self):
        self.configure(claude=self.claude_binding())
        good = _claude_credit()
        with mock.patch.object(api_credits.time, "monotonic", return_value=100.0), mock.patch.object(
            providers,
            "live_claude_api_credits",
            side_effect=[good, providers.ClaudeOrgBindingError("mismatch")],
        ) as collect:
            first = api_credits.live_api_credit_balances(force=True)["claude"]
            mismatch = api_credits.live_api_credit_balances(force=True)["claude"]
            suppressed = api_credits.live_api_credit_balances()["claude"]
        self.assertEqual(first["total"], 17.25)
        self.assertEqual(mismatch["error_code"], "account_mismatch")
        self.assertEqual(mismatch["binding_status"], "mismatch")
        self.assertIsNone(mismatch["total"])
        self.assertIsNone(suppressed["total"])
        self.assertEqual(suppressed["binding_status"], "mismatch")
        self.assertEqual(collect.call_count, 2)

    def test_profile_switch_does_not_reuse_a_different_org_cache(self):
        self.configure(claude=self.claude_binding(org_id="claude-org-a", profile="profile-a"))
        good = _claude_credit(org_id="claude-org-a")
        with mock.patch.object(providers, "live_claude_api_credits", side_effect=[good, providers.ClaudeOrgBindingError("mismatch")]):
            first = api_credits.live_api_credit_balances(force=True)["claude"]
            self.configure(claude=self.claude_binding(org_id="claude-org-b", profile="profile-b"))
            second = api_credits.live_api_credit_balances(force=True)["claude"]
        self.assertEqual(first["total"], 17.25)
        self.assertEqual(second["organization_identity"]["id"], "claude-org-b")
        self.assertEqual(second["binding_status"], "mismatch")
        self.assertIsNone(second["total"])

    def test_claude_collector_receives_the_explicit_configured_profile(self):
        binding = self.claude_binding(profile="Chrome/Default")
        self.configure(claude=binding)
        with mock.patch.object(providers, "live_claude_api_credits", return_value=_claude_credit()) as collect:
            result = api_credits.live_api_credit_balances(force=True)["claude"]

        self.assertEqual(result["status"], "ok")
        collect.assert_called_once_with(
            timeout=api_credits.API_CREDIT_FETCH_TIMEOUT_SECONDS,
            organization_id=binding["organization_id"],
            profile="Chrome/Default",
        )

    def test_account_label_change_does_not_reuse_cached_identity(self):
        first_binding = self.claude_binding()
        self.configure(claude=first_binding)
        good = _claude_credit()
        with mock.patch.object(api_credits.time, "monotonic", return_value=100.0), mock.patch.object(
            providers, "live_claude_api_credits", return_value=good
        ) as collect:
            first = api_credits.live_api_credit_balances(force=True)["claude"]
            second_binding = dict(first_binding, account="Corrected account label")
            self.configure(claude=second_binding)
            second = api_credits.live_api_credit_balances()["claude"]

        self.assertEqual(first["account_identity"]["label"], "Fixture Claude account")
        self.assertEqual(second["account_identity"]["label"], "Corrected account label")
        self.assertEqual(collect.call_count, 2)

    def test_expected_organization_name_change_is_checked_before_cache_reuse(self):
        binding = self.claude_binding()
        self.configure(claude=binding)
        good = _claude_credit()
        with mock.patch.object(api_credits.time, "monotonic", return_value=100.0), mock.patch.object(
            providers, "live_claude_api_credits", return_value=good
        ) as collect:
            first = api_credits.live_api_credit_balances(force=True)["claude"]
            self.configure(claude=dict(binding, organization_name="Different Org"))
            second = api_credits.live_api_credit_balances()["claude"]

        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "error")
        self.assertEqual(second["error_code"], "account_mismatch")
        self.assertEqual(second["binding_status"], "mismatch")
        self.assertIsNone(second["total"])
        self.assertEqual(collect.call_count, 2)

    def test_failed_refresh_preserves_cached_unknown_balance_and_cause(self):
        binding = api_credits.AccountBinding(
            provider="claude",
            profile="Chrome/Default",
            account_label="Fixture Claude account",
            organization_id="org-unknown",
            organization_name="Unknown Balance Org",
        )
        checked_at = "2026-10-30T00:00:00Z"
        unknown = api_credits._base_record(
            "claude",
            binding,
            checked_at=checked_at,
            status="unknown",
            error_code="balance_unreported",
            binding_status="bound",
        )
        responses = [unknown, RuntimeError("fixture refresh failure")]

        def collect(_binding, _timeout, _checked_at):
            response = responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return response

        with mock.patch.object(api_credits, "_COLLECTORS", {"claude": collect}), mock.patch.object(
            api_credits.time, "monotonic", return_value=100.0
        ):
            first = api_credits._fetch_bound_provider("claude", binding, force=True, timeout=1)
            failed = api_credits._fetch_bound_provider("claude", binding, force=True, timeout=1)
            cached = api_credits._fetch_bound_provider("claude", binding, force=False, timeout=1)

        for result in (first, failed, cached):
            self.assertEqual(result["status"], "unknown")
            self.assertEqual(result["error_code"], "balance_unreported")
            self.assertIsNone(result["promotional_balance"])
            self.assertIsNone(result["purchased_balance"])
            self.assertIsNone(result["total"])
        self.assertIsNone(first.get("refresh_error_code"))
        self.assertEqual(failed["refresh_error_code"], "fetch_failed")
        self.assertEqual(cached["refresh_error_code"], "fetch_failed")

    def test_failed_refresh_is_stale_then_recovers_on_manual_refresh(self):
        self.configure(claude=self.claude_binding())
        good = _claude_credit()
        responses = [good, RuntimeError("must not be exposed"), good]

        def collect(**_kwargs):
            result = responses.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        clock = [100.0]
        with mock.patch.object(api_credits.time, "monotonic", side_effect=lambda: clock[0]), mock.patch.object(
            api_credits.time, "time", return_value=datetime(2026, 10, 30, tzinfo=timezone.utc).timestamp()
        ), mock.patch.object(providers, "live_claude_api_credits", side_effect=collect) as call:
            first = api_credits.live_api_credit_balances(force=True)["claude"]
            clock[0] = 401.0
            stale = api_credits.live_api_credit_balances()["claude"]
            blocked = api_credits.live_api_credit_balances()["claude"]
            recovered = api_credits.live_api_credit_balances(force=True)["claude"]
        self.assertEqual(first["status"], "ok")
        self.assertEqual(stale["status"], "stale")
        self.assertEqual(stale["observed_at"], first["observed_at"])
        self.assertEqual(stale["error_code"], "fetch_failed")
        self.assertEqual(blocked["status"], "stale")
        self.assertEqual(recovered["status"], "ok")
        self.assertIsNone(recovered["error_code"])
        self.assertEqual(call.call_count, 3)

    def test_backoff_is_bounded_and_forced_reads_bypass_it(self):
        binding = api_credits.AccountBinding(
            provider="claude", profile="profile", organization_id="org-fixture"
        )
        clock = [100.0]
        expected = [60, 120, 240, 480, 900, 900]

        def fail(_binding, _timeout, _checked_at):
            raise api_credits._CollectorError("fixture_failure")

        with mock.patch.object(api_credits, "_COLLECTORS", {"claude": fail}), mock.patch.object(
            api_credits.time, "monotonic", side_effect=lambda: clock[0]
        ):
            for delay in expected:
                api_credits._fetch_bound_provider("claude", binding, force=True, timeout=1)
                state = api_credits._CACHE[binding.cache_key]
                self.assertEqual(state["retry_after"] - clock[0], delay)

    def test_single_flight_and_repeated_cycles_keep_cache_bounded(self):
        binding = api_credits.AccountBinding(provider="claude", profile="p0", organization_id="org-0")
        started = threading.Event()
        release = threading.Event()
        call_count = [0]

        def collect(bound, _timeout, checked_at):
            call_count[0] += 1
            started.set()
            release.wait(timeout=2)
            return {
                "contract": "api-credit/v1", "provider": "claude", "status": "ok",
                "account_identity": {"profile": bound.profile, "label": bound.profile},
                "organization_identity": {"id": bound.organization_id, "name": bound.organization_id},
                "binding_status": "bound", "currency": "USD", "promotional_balance": 3.0,
                "purchased_balance": 1.0, "total": 4.0, "grants": [], "observed_at": checked_at,
                "checked_at": checked_at, "source_updated_at": None, "source": "fixture",
                "error_code": None, "next_grant_at": None, "usage_restrictions": None,
            }

        result_rows = []
        with mock.patch.object(api_credits, "_COLLECTORS", {"claude": collect}):
            first = threading.Thread(target=lambda: result_rows.append(
                api_credits._fetch_bound_provider("claude", binding, force=True, timeout=1)
            ))
            first.start()
            self.assertTrue(started.wait(timeout=1))
            second = threading.Thread(target=lambda: result_rows.append(
                api_credits._fetch_bound_provider("claude", binding, force=True, timeout=1)
            ))
            second.start()
            release.set()
            first.join(timeout=2)
            second.join(timeout=2)
            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(call_count[0], 1)

            for index in range(20):
                other = api_credits.AccountBinding(
                    provider="claude", profile=f"p{index}", organization_id=f"org-{index}"
                )
                api_credits._fetch_bound_provider("claude", other, force=True, timeout=1)
            self.assertLessEqual(len(api_credits._CACHE), api_credits.API_CREDIT_CACHE_MAX_ENTRIES)

    def test_claude_response_body_is_capped(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size):
                return b"x" * size

        with mock.patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaises(providers.ClaudeWebError):
                providers._claude_platform_get("/api/organizations", {}, timeout=1)


if __name__ == "__main__":
    unittest.main()
