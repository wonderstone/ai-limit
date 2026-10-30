"""Per-model weekly limits from the claude.ai usage response.

Besides the account-wide `seven_day` window, the usage API lists model-scoped
weekly caps in `limits[]` as `kind: "weekly_scoped"` with
`scope.model.display_name` (e.g. "Fable"). `percent` there is the used share.
"""
from __future__ import annotations

from typing import Any


def claude_weekly_model_limits(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    limits = (data or {}).get("limits")
    if not isinstance(limits, list):
        return []
    result = []
    for item in limits:
        if not isinstance(item, dict) or item.get("kind") != "weekly_scoped":
            continue
        scope = item.get("scope") or {}
        model = (scope.get("model") or {}) if isinstance(scope, dict) else {}
        name = model.get("display_name") or model.get("id")
        if not name:
            continue
        try:
            used = max(0, min(100, int(round(float(item.get("percent"))))))
        except (TypeError, ValueError):
            continue
        result.append(
            {
                "model": name,
                "window": "weekly",
                "used_percent": used,
                "remaining_percent": 100 - used,
                "reset_time": item.get("resets_at"),
                "severity": item.get("severity"),
                "is_active": bool(item.get("is_active")),
            }
        )
    return result


def claude_cloud_session_credits(data: dict[str, Any] | None) -> dict[str, Any] | None:
    """Cloud-session promotion from the existing usage response.

    Observed 2026-10-30: this bucket matches the cloud-credit panel's amount,
    $250 grant and expiry. It is not extra_usage/spend or Console API credit.
    Missing values stay unknown; no promotion is invented when the field is null.
    """
    import math
    from datetime import datetime, timezone

    raw = (data or {}).get("iguana_necktie")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raw = {}

    def amount(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            value = float(value)
        except (ValueError, OverflowError):
            return None
        return value if math.isfinite(value) and value >= 0 else None

    limit = amount(raw.get("limit_dollars"))
    remaining = amount(raw.get("remaining_dollars"))
    if remaining is not None and limit is not None and remaining > limit:
        remaining = None
    expires_at = raw.get("resets_at")
    expired = False
    try:
        parsed = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            expires_at = None
        else:
            expired = parsed <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        expires_at = None
    locked_reason = raw.get("locked_reason") if isinstance(raw.get("locked_reason"), str) else None
    spendable = remaining is not None and limit is not None and remaining > 0 and not expired and not locked_reason
    return {
        "remaining": None if locked_reason else (0.0 if expired and remaining is not None else remaining),
        "reported_remaining": remaining,
        "limit": limit,
        "used": amount(raw.get("used_dollars")),
        "expires_at": expires_at,
        "expired": expired,
        "locked_reason": locked_reason,
        "spendable": bool(spendable),
        "amount_unit": "dollars",
        "source_field": "iguana_necktie",
    }
