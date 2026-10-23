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
