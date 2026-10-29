"""Read-only, account-bound API credit snapshots.

This module deliberately does not share balances with subscription quota
providers. OpenAI remains unknown until a read-only billing source with verified
organization and grant semantics is available.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Any, Callable

from ai_limit import providers


API_CREDIT_CONTRACT = "api-credit/v1"
API_CREDIT_CACHE_TTL_SECONDS = 300
API_CREDIT_RETRY_MIN_SECONDS = 60
API_CREDIT_RETRY_MAX_SECONDS = 900
API_CREDIT_CACHE_MAX_ENTRIES = 2
API_CREDIT_MAX_GRANTS = 256
API_CREDIT_CONFIG_MAX_BYTES = 16 * 1024
API_CREDIT_FETCH_TIMEOUT_SECONDS = 15
API_CREDIT_SINGLEFLIGHT_WAITERS_PER_BINDING = 2

OPENAI_BILLING_URL = "https://platform.openai.com/settings/organization/billing/overview"
CLAUDE_BILLING_URL = "https://platform.claude.com/settings/billing"
PROFILE_CONFIG_PATH = Path(
    os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
) / "ai-limit" / "api-credit-profiles.json"

_ORG_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_CACHE: OrderedDict[tuple[str, str, str, str, str, bool], dict[str, Any]] = OrderedDict()
_IN_FLIGHT: set[tuple[str, str, str, str, str, bool]] = set()
_IN_FLIGHT_WAITERS: dict[tuple[str, str, str, str, str, bool], int] = {}
_CACHE_CONDITION = threading.Condition()


class APICreditConfigurationError(Exception):
    pass


class _CollectorError(Exception):
    def __init__(self, error_code: str, *, mismatch: bool = False):
        super().__init__(error_code)
        self.error_code = error_code
        self.mismatch = mismatch


@dataclass(frozen=True)
class AccountBinding:
    provider: str
    profile: str | None = None
    account_label: str | None = None
    organization_id: str | None = None
    organization_name: str | None = None
    platform_session_allowed: bool = False

    @property
    def configured(self) -> bool:
        return bool(self.profile and self.organization_id)

    @property
    def cache_key(self) -> tuple[str, str, str, str, str, bool]:
        return (
            self.provider,
            self.profile or "",
            self.account_label or "",
            self.organization_id or "",
            self.organization_name or "",
            self.platform_session_allowed,
        )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, *, limit: int = 160) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = "".join(ch for ch in value.strip() if ch >= " " and ch != "\x7f")
    return cleaned[:limit] or None


def _profile_config() -> dict[str, Any]:
    try:
        with PROFILE_CONFIG_PATH.open("rb") as stream:
            raw = stream.read(API_CREDIT_CONFIG_MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise APICreditConfigurationError("profile_config_unreadable") from exc
    if len(raw) > API_CREDIT_CONFIG_MAX_BYTES:
        raise APICreditConfigurationError("profile_config_too_large")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise APICreditConfigurationError("profile_config_invalid") from exc
    if not isinstance(value, dict):
        raise APICreditConfigurationError("profile_config_invalid")
    return value


def configured_api_credit_binding(provider: str) -> AccountBinding:
    """Load one explicit local profile/org binding without reading credentials."""
    if provider not in {"openai", "claude"}:
        raise ValueError("unsupported API credit provider")
    raw = _profile_config().get(provider)
    if raw is None:
        return AccountBinding(provider=provider)
    if not isinstance(raw, dict):
        raise APICreditConfigurationError("profile_config_invalid")

    profile = _clean_text(raw.get("profile"))
    account_label = _clean_text(raw.get("account")) or profile
    organization_name = _clean_text(raw.get("organization_name"))
    organization_id = _clean_text(raw.get("organization_id"), limit=128)
    if organization_id and not _ORG_ID_RE.fullmatch(organization_id):
        raise APICreditConfigurationError("organization_id_invalid")
    return AccountBinding(
        provider=provider,
        profile=profile,
        account_label=account_label,
        organization_id=organization_id,
        organization_name=organization_name,
        platform_session_allowed=raw.get("reuse_platform_session") is True,
    )


def _base_record(
    provider: str,
    binding: AccountBinding,
    *,
    checked_at: str,
    status: str = "unknown",
    error_code: str | None = None,
    binding_status: str | None = None,
) -> dict[str, Any]:
    if binding_status is None:
        binding_status = "configured" if binding.configured else "unbound"
    source = "platform.claude.com" if provider == "claude" else "unverified"
    billing_url = CLAUDE_BILLING_URL if provider == "claude" else OPENAI_BILLING_URL
    return {
        "contract": API_CREDIT_CONTRACT,
        "provider": provider,
        "status": status,
        "account_identity": {
            "profile": binding.profile,
            "label": binding.account_label,
        },
        "organization_identity": {
            "id": binding.organization_id,
            "name": binding.organization_name,
        },
        "binding_status": binding_status,
        "currency": None,
        "promotional_balance": None,
        "purchased_balance": None,
        "total": None,
        "grants": None,
        "observed_at": None,
        "checked_at": checked_at,
        "source_updated_at": None,
        "source": source,
        "error_code": error_code,
        "refresh_error_code": None,
        "next_grant_at": None,
        "usage_restrictions": None,
        "billing_url": billing_url,
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _source_time(value: Any) -> str | None:
    return _clean_text(value, limit=80)


def _source_restrictions(value: Any) -> list[str] | str | None:
    if isinstance(value, str):
        return _clean_text(value, limit=240)
    if isinstance(value, list):
        values = [_clean_text(item, limit=120) for item in value[:32]]
        return [item for item in values if item is not None]
    return None


def _claude_grants(raw_grants: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], float | None]:
    if len(raw_grants) > API_CREDIT_MAX_GRANTS:
        raise _CollectorError("too_many_grants")
    grants: list[dict[str, Any]] = []
    total = 0.0
    all_remaining_known = True
    for item in raw_grants:
        remaining = _number(item.get("remaining"))
        granted = _number(item.get("granted"))
        if remaining is None:
            all_remaining_known = False
        else:
            total += remaining
        grants.append({
            "name": _clean_text(item.get("name"), limit=120),
            "program_id": _clean_text(item.get("program_id"), limit=120),
            "granted": granted,
            "remaining": remaining,
            "expires_at": _source_time(item.get("expires_at")),
            "granted_at": _source_time(item.get("granted_at")),
            "usage_restrictions": _source_restrictions(item.get("usage_restrictions")),
        })
    return grants, total if all_remaining_known else None


def _collect_claude(binding: AccountBinding, timeout: int, checked_at: str) -> dict[str, Any]:
    if not binding.configured:
        record = _base_record(
            "claude",
            binding,
            checked_at=checked_at,
            error_code="binding_required",
            binding_status="unbound",
        )
        return record

    try:
        credit = providers.live_claude_api_credits(
            timeout=timeout,
            organization_id=binding.organization_id,
            profile=binding.profile,
        )
    except providers.ClaudeOrgBindingError as exc:
        raise _CollectorError("account_mismatch", mismatch=True) from exc
    except Exception as exc:
        code = getattr(exc, "error_code", None) or "fetch_failed"
        raise _CollectorError(str(code)) from exc
    if credit is None:
        raise _CollectorError("auth_unavailable")
    if str(credit.get("org_id") or "") != binding.organization_id:
        raise _CollectorError("account_mismatch", mismatch=True)

    source_name = _clean_text(credit.get("org_name"))
    expected_name = binding.organization_name
    if expected_name and source_name and expected_name.casefold() != source_name.casefold():
        raise _CollectorError("account_mismatch", mismatch=True)

    raw_grants = credit.get("promo_tranches")
    if not isinstance(raw_grants, list):
        grants = None
        promotional_balance = None
    else:
        grants, promotional_balance = _claude_grants(
            [item for item in raw_grants if isinstance(item, dict)]
        )

    record = _base_record(
        "claude",
        AccountBinding(
            provider="claude",
            profile=binding.profile,
            account_label=binding.account_label,
            organization_id=binding.organization_id,
            organization_name=source_name or binding.organization_name,
        ),
        checked_at=checked_at,
        status="ok",
        error_code=None,
        binding_status="bound",
    )
    record.update({
        "currency": _clean_text(credit.get("source_currency"), limit=8),
        "promotional_balance": promotional_balance,
        "purchased_balance": _number(credit.get("purchased_balance")),
        "total": _number(credit.get("balance")),
        "grants": grants,
        "observed_at": checked_at,
        "source_updated_at": _source_time(credit.get("source_updated_at")),
        "next_grant_at": _source_time(credit.get("next_grant_at")),
        "usage_restrictions": _source_restrictions(credit.get("usage_restrictions")),
    })
    if not any(record.get(key) is not None for key in ("promotional_balance", "purchased_balance", "total")):
        record["status"] = "unknown"
        record["error_code"] = "balance_unreported"
    return record


def _collect_openai(binding: AccountBinding, timeout: int, checked_at: str) -> dict[str, Any]:
    if not binding.configured:
        return _base_record("openai", binding, checked_at=checked_at,
                            error_code="binding_required", binding_status="unbound")
    if not binding.platform_session_allowed:
        return _base_record("openai", binding, checked_at=checked_at,
                            error_code="platform_session_permission_required", binding_status="unverified")
    from ai_limit.openai_platform import fetch_credit_grants, normalize_credit_grants, PlatformBalanceError
    try:
        org, payload = fetch_credit_grants(binding.profile, binding.organization_id, min(timeout, 5))
        name = org.get("title") or org.get("name")
        if binding.organization_name and name and binding.organization_name != name:
            raise _CollectorError("account_mismatch", mismatch=True)
        record = _base_record("openai", binding, checked_at=checked_at, status="ok", binding_status="bound")
        record.update(normalize_credit_grants(payload))
        record.update(source="api.openai.com/v1/dashboard/billing/credit_grants", observed_at=checked_at)
        if record.get("promotional_balance") is None and record.get("purchased_balance") is None:
            record.update(status="unknown", error_code="balance_unreported")
        return record
    except PlatformBalanceError as exc:
        raise _CollectorError(exc.code, mismatch=exc.mismatch) from None


# Collectors never call inference endpoints; authentication stays memory-only.
_COLLECTORS: dict[str, Callable[[AccountBinding, int, str], dict[str, Any]]] = {
    "openai": _collect_openai,
    "claude": _collect_claude,
}


def _parse_expiry(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _spendable_view(payload: dict[str, Any], *, now_epoch: float, status: str, checked_at: str, error_code: str | None) -> dict[str, Any]:
    view = deepcopy(payload)
    view["status"] = status
    view["checked_at"] = checked_at
    view["error_code"] = error_code
    grants = view.get("grants")
    expired_amount = 0.0
    expired_any = False
    if isinstance(grants, list):
        for grant in grants:
            expiry = _parse_expiry(grant.get("expires_at"))
            remaining = _number(grant.get("remaining"))
            expired = expiry is not None and expiry <= now_epoch
            grant["expired"] = expired
            grant["spendable"] = not expired and remaining is not None and remaining > 0
            if expired:
                expired_any = True
                if remaining is not None:
                    expired_amount += remaining
                grant["remaining"] = 0.0
        promotional_balance = _number(view.get("promotional_balance"))
        if promotional_balance is not None and expired_any:
            view["promotional_balance"] = max(0.0, promotional_balance - expired_amount)
    if expired_any:
        promotional_balance = _number(view.get("promotional_balance"))
        purchased_balance = _number(view.get("purchased_balance"))
        if promotional_balance is not None and purchased_balance is not None:
            view["total"] = promotional_balance + purchased_balance
        else:
            # The cached source total may include a grant that has since expired.
            view["total"] = None
    return view


def _refresh_failure_view(
    payload: dict[str, Any],
    *,
    now_epoch: float,
    checked_at: str,
    error_code: str,
) -> dict[str, Any]:
    """Keep an unreported balance unknown while recording a failed refresh."""
    if payload.get("status") == "unknown":
        view = _spendable_view(
            payload,
            now_epoch=now_epoch,
            status="unknown",
            checked_at=checked_at,
            error_code=payload.get("error_code"),
        )
        view["refresh_error_code"] = error_code
        return view
    return _spendable_view(
        payload,
        now_epoch=now_epoch,
        status="stale",
        checked_at=checked_at,
        error_code=error_code,
    )


def _copy_state_payload(
    state: dict[str, Any] | None,
    *,
    provider: str,
    binding: AccountBinding,
    checked_at: str,
    now_epoch: float,
) -> dict[str, Any]:
    if state and isinstance(state.get("success"), dict):
        success = state["success"]
        last_error_code = state.get("last_error_code")
        if last_error_code:
            return _refresh_failure_view(
                success,
                now_epoch=now_epoch,
                checked_at=state.get("last_attempt_at") or checked_at,
                error_code=last_error_code,
            )
        return _spendable_view(
            success,
            now_epoch=now_epoch,
            status=success.get("status", "ok"),
            checked_at=state.get("last_attempt_at") or checked_at,
            error_code=success.get("error_code"),
        )
    code = state.get("last_error_code") if state else None
    binding_status = state.get("binding_status") if state else None
    binding_status = binding_status or ("configured" if binding.configured else "unbound")
    return _base_record(
        provider,
        binding,
        checked_at=state.get("last_attempt_at") if state else checked_at,
        status="error" if code else "unknown",
        error_code=code or ("binding_required" if not binding.configured else None),
        binding_status=binding_status,
    )


def _remember(key: tuple[str, str, str, str, str, bool], state: dict[str, Any]) -> None:
    _CACHE[key] = state
    _CACHE.move_to_end(key)
    while len(_CACHE) > API_CREDIT_CACHE_MAX_ENTRIES:
        _CACHE.popitem(last=False)


def _fetch_bound_provider(
    provider: str,
    binding: AccountBinding,
    *,
    force: bool,
    timeout: int,
) -> dict[str, Any]:
    now_epoch = time.time()
    checked_at = _utc_now_iso()
    if not binding.configured:
        return _COLLECTORS[provider](binding, timeout, checked_at)

    key = binding.cache_key
    wait_seconds = min(32.0, timeout * 2.0 + 2.0)
    with _CACHE_CONDITION:
        state = _CACHE.get(key)
        if state is not None:
            _CACHE.move_to_end(key)
        if not force and state is not None:
            if state.get("last_error_code"):
                if time.monotonic() < float(state.get("retry_after", 0.0)):
                    return _copy_state_payload(
                        state,
                        provider=provider,
                        binding=binding,
                        checked_at=checked_at,
                        now_epoch=now_epoch,
                    )
            elif isinstance(state.get("success"), dict) and (
                time.monotonic() - float(state.get("success_monotonic", 0.0))
            ) < API_CREDIT_CACHE_TTL_SECONDS:
                return _copy_state_payload(
                    state,
                    provider=provider,
                    binding=binding,
                    checked_at=checked_at,
                    now_epoch=now_epoch,
                )
        if key in _IN_FLIGHT:
            waiter_count = _IN_FLIGHT_WAITERS.get(key, 0)
            if waiter_count >= API_CREDIT_SINGLEFLIGHT_WAITERS_PER_BINDING:
                if state and isinstance(state.get("success"), dict):
                    return _refresh_failure_view(
                        state["success"],
                        now_epoch=now_epoch,
                        checked_at=checked_at,
                        error_code="singleflight_capacity",
                    )
                return _base_record(
                    provider,
                    binding,
                    checked_at=checked_at,
                    status="error",
                    error_code="singleflight_capacity",
                    binding_status="configured",
                )
            _IN_FLIGHT_WAITERS[key] = waiter_count + 1
            try:
                awakened = _CACHE_CONDITION.wait_for(lambda: key not in _IN_FLIGHT, timeout=wait_seconds)
            finally:
                remaining_waiters = _IN_FLIGHT_WAITERS.get(key, 1) - 1
                if remaining_waiters > 0:
                    _IN_FLIGHT_WAITERS[key] = remaining_waiters
                else:
                    _IN_FLIGHT_WAITERS.pop(key, None)
            state = _CACHE.get(key)
            if not awakened:
                if state and isinstance(state.get("success"), dict):
                    return _refresh_failure_view(
                        state["success"],
                        now_epoch=now_epoch,
                        checked_at=state.get("last_attempt_at") or checked_at,
                        error_code="singleflight_timeout",
                    )
                fallback = _base_record(
                    provider,
                    binding,
                    checked_at=checked_at,
                    status="error",
                    error_code="singleflight_timeout",
                    binding_status="configured",
                )
                return fallback
            if state and (
                state.get("last_error_code")
                or time.monotonic() - float(state.get("success_monotonic", 0.0)) < API_CREDIT_CACHE_TTL_SECONDS
            ):
                return _copy_state_payload(
                    state,
                    provider=provider,
                    binding=binding,
                    checked_at=checked_at,
                    now_epoch=now_epoch,
                )
        if len(_IN_FLIGHT) >= API_CREDIT_CACHE_MAX_ENTRIES:
            return _base_record(
                provider,
                binding,
                checked_at=checked_at,
                status="error",
                error_code="singleflight_capacity",
                binding_status="configured",
            )
        _IN_FLIGHT.add(key)

    try:
        payload = _COLLECTORS[provider](binding, timeout, checked_at)
        payload["checked_at"] = checked_at
        if payload.get("status") in {"ok", "unknown"} and payload.get("binding_status") == "bound":
            payload["observed_at"] = checked_at
            state = {
                "success": payload,
                "success_monotonic": time.monotonic(),
                "failure_count": 0,
                "retry_after": 0.0,
                "last_attempt_at": checked_at,
                "last_error_code": None,
            }
            with _CACHE_CONDITION:
                _remember(key, state)
            return _spendable_view(
                payload,
                now_epoch=time.time(),
                status=payload["status"],
                checked_at=checked_at,
                error_code=payload.get("error_code"),
            )
        return payload
    except _CollectorError as exc:
        prior = _CACHE.get(key) or {}
        failure_count = int(prior.get("failure_count", 0)) + 1
        delay = min(
            API_CREDIT_RETRY_MIN_SECONDS * (2 ** (failure_count - 1)),
            API_CREDIT_RETRY_MAX_SECONDS,
        )
        success = None if exc.mismatch else prior.get("success")
        state = {
            "success": success,
            "success_monotonic": prior.get("success_monotonic", 0.0),
            "failure_count": failure_count,
            "retry_after": time.monotonic() + delay,
            "last_attempt_at": checked_at,
            "last_error_code": exc.error_code,
            "binding_status": "mismatch" if exc.mismatch else "configured",
        }
        with _CACHE_CONDITION:
            _remember(key, state)
        if success:
            return _refresh_failure_view(
                success,
                now_epoch=time.time(),
                checked_at=checked_at,
                error_code=exc.error_code,
            )
        return _base_record(
            provider,
            binding,
            checked_at=checked_at,
            status="error",
            error_code=exc.error_code,
            binding_status="mismatch" if exc.mismatch else "configured",
        )
    except Exception:
        prior = _CACHE.get(key) or {}
        failure_count = int(prior.get("failure_count", 0)) + 1
        delay = min(
            API_CREDIT_RETRY_MIN_SECONDS * (2 ** (failure_count - 1)),
            API_CREDIT_RETRY_MAX_SECONDS,
        )
        state = {
            "success": prior.get("success"),
            "success_monotonic": prior.get("success_monotonic", 0.0),
            "failure_count": failure_count,
            "retry_after": time.monotonic() + delay,
            "last_attempt_at": checked_at,
            "last_error_code": "fetch_failed",
        }
        with _CACHE_CONDITION:
            _remember(key, state)
        if state["success"]:
            return _refresh_failure_view(
                state["success"],
                now_epoch=time.time(),
                checked_at=checked_at,
                error_code="fetch_failed",
            )
        return _base_record(
            provider,
            binding,
            checked_at=checked_at,
            status="error",
            error_code="fetch_failed",
            binding_status="configured",
        )
    finally:
        with _CACHE_CONDITION:
            _IN_FLIGHT.discard(key)
            _CACHE_CONDITION.notify_all()


def live_api_credit_balances(*, force: bool = False, timeout: int = API_CREDIT_FETCH_TIMEOUT_SECONDS) -> dict[str, dict[str, Any]]:
    """Return separate OpenAI and Claude API-credit/v1 snapshots.

    The caller's 60-second widget tick can call this each cycle; bound live
    reads are cached for five minutes and share one in-flight fetch per account.
    """
    timeout = max(1, min(int(timeout), API_CREDIT_FETCH_TIMEOUT_SECONDS))
    checked_at = _utc_now_iso()
    try:
        config = _profile_config()
    except APICreditConfigurationError as exc:
        code = str(exc)
        return {
            provider: _base_record(
                provider,
                AccountBinding(provider=provider),
                checked_at=checked_at,
                status="error",
                error_code=code,
                binding_status="invalid",
            )
            for provider in ("openai", "claude")
        }

    result: dict[str, dict[str, Any]] = {}
    for provider in ("openai", "claude"):
        try:
            raw = config.get(provider)
            if raw is None:
                binding = AccountBinding(provider=provider)
            elif not isinstance(raw, dict):
                raise APICreditConfigurationError("profile_config_invalid")
            else:
                profile = _clean_text(raw.get("profile"))
                organization_id = _clean_text(raw.get("organization_id"), limit=128)
                if organization_id and not _ORG_ID_RE.fullmatch(organization_id):
                    raise APICreditConfigurationError("organization_id_invalid")
                binding = AccountBinding(
                    provider=provider,
                    profile=profile,
                    account_label=_clean_text(raw.get("account")) or profile,
                    organization_id=organization_id,
                    organization_name=_clean_text(raw.get("organization_name")),
                    platform_session_allowed=raw.get("reuse_platform_session") is True,
                )
            result[provider] = _fetch_bound_provider(
                provider,
                binding,
                force=force,
                timeout=timeout,
            )
        except APICreditConfigurationError as exc:
            result[provider] = _base_record(
                provider,
                AccountBinding(provider=provider),
                checked_at=checked_at,
                status="error",
                error_code=str(exc),
                binding_status="invalid",
            )
    return result


def clear_api_credit_cache() -> None:
    """Clear the bounded in-memory snapshot cache, primarily for tests."""
    with _CACHE_CONDITION:
        _CACHE.clear()


def claude_api_credit_legacy(record: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project Claude API-credit/v1 into the former consumer shape."""
    if not isinstance(record, dict) or record.get("status") not in {"ok", "stale"}:
        return None
    grants = record.get("grants") or []
    granted_values = [_number(item.get("granted")) for item in grants if isinstance(item, dict)]
    granted = sum(value for value in granted_values if value is not None) if granted_values and all(
        value is not None for value in granted_values
    ) else None
    expiries = [item.get("expires_at") for item in grants if isinstance(item, dict) and item.get("expires_at")]
    balance = _number(record.get("total"))
    remaining_percent = None
    if balance is not None and granted is not None and granted > 0:
        remaining_percent = max(0, min(100, int(round(balance / granted * 100))))
    return {
        "org_id": (record.get("organization_identity") or {}).get("id"),
        "org_name": (record.get("organization_identity") or {}).get("name"),
        "currency": record.get("currency"),
        "balance": balance,
        "granted": granted,
        "remaining_percent": remaining_percent,
        "expires_at": min(expiries, default=None),
        "tranches": grants,
        "auto_reload": None,
        "source": record.get("source"),
    }
