#!/usr/bin/env python3
"""Local read-only JSON API for ai-limit quota state.

The server binds to localhost by default and exposes normalized quota payloads
for local tools such as operator dashboards. It intentionally keeps Codex
app-server fallback opt-in because that path can start a Codex usage window.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import pathlib
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ai_limit.providers import (  # noqa: E402
    ClaudeWebError,
    CodexAuthError,
    CodexWebError,
    DeepSeekAuthError,
    DeepSeekError,
    GeminiAppUsageError,
    GoogleQuotaAuthError,
    GoogleQuotaError,
    codex_5h_remaining_percent,
    codex_window_remaining_percent,
    codex_window_reset_time,
    current_codex_rate_limits,
    has_deepseek_api_key,
    has_gemini_app_cookies,
    has_google_oauth_creds,
    live_claude_plan,
    live_claude_usage,
    live_deepseek_balance,
    live_gemini_app_usage,
    live_google_quota,
)
from ai_limit.llm_api import has_llm_api_provider_config, live_llm_api_balances  # noqa: E402

try:  # usage.py is the CLI module at repo root.
    from usage import latest_codex_rate_limits
except Exception:  # noqa: BLE001
    latest_codex_rate_limits = lambda: (None, None)  # type: ignore[assignment]


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 17655
CODEX_CACHE_TTL_SECONDS = int(os.environ.get("AI_LIMIT_API_CODEX_CACHE_TTL_SECONDS", "300") or "300")
CODEX_DISK_STALE_SECONDS = int(os.environ.get("AI_LIMIT_API_CODEX_DISK_STALE_SECONDS", "3600") or "3600")
AGGREGATE_DEADLINE_SECONDS = float(os.environ.get("AI_LIMIT_API_AGGREGATE_DEADLINE_SECONDS", "8") or "8")
_CODEX_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CODEX_DISK_CACHE_PATH = pathlib.Path.home() / ".cache" / "ai-limit" / "codex-quota-api-cache.json"


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _as_bool(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _jsonable_error(exc: Exception) -> dict[str, Any]:
    return {"error": f"{type(exc).__name__}: {str(exc)[:240]}"}


def _window_payload(rate_limits: dict[str, Any], window: str) -> dict[str, Any] | None:
    remaining = codex_window_remaining_percent(rate_limits, window)
    reset_time = codex_window_reset_time(rate_limits, window)
    window_minutes = None
    if remaining is None and reset_time is None:
        for legacy_key in ("primary", "secondary"):
            legacy = rate_limits.get(legacy_key)
            if not isinstance(legacy, dict):
                continue
            minutes = legacy.get("window_minutes")
            legacy_matches = (
                (window == "5h" and isinstance(minutes, (int, float)) and minutes <= 5 * 60)
                or (window == "weekly" and isinstance(minutes, (int, float)) and minutes >= 7 * 24 * 60)
                or (window == "5h" and legacy_key == "primary" and minutes is None)
                or (window == "weekly" and legacy_key == "secondary" and minutes is None)
            )
            if not legacy_matches:
                continue
            if "remaining_percent" in legacy:
                remaining = legacy.get("remaining_percent")
            elif "used_percent" in legacy:
                remaining = 100 - legacy.get("used_percent", 0)
            reset_time = legacy.get("resets_at") or legacy.get("reset_time")
            window_minutes = minutes
            break
    if remaining is None and reset_time is None:
        return None
    return {
        "window": window,
        "remaining_percent": remaining,
        "used_percent": None if remaining is None else max(0, min(100, 100 - remaining)),
        "window_minutes": window_minutes,
        "reset_time": reset_time,
    }


def _codex_payload(*, allow_app_server: bool = False) -> dict[str, Any]:
    cache_key = "allow_app_server" if allow_app_server else "safe"
    cached = _CODEX_CACHE.get(cache_key)
    now_ts = dt.datetime.now(dt.timezone.utc).timestamp()
    if cached and now_ts - cached[0] <= CODEX_CACHE_TTL_SECONDS:
        payload = dict(cached[1])
        payload["cache"] = {
            "status": "hit",
            "ttl_seconds": CODEX_CACHE_TTL_SECONDS,
            "age_seconds": round(now_ts - cached[0], 3),
        }
        return payload
    if not allow_app_server:
        disk_cached = _read_codex_disk_cache(now_ts)
        if disk_cached is not None:
            _CODEX_CACHE[cache_key] = (now_ts, disk_cached)
            return disk_cached

    try:
        if not allow_app_server:
            observed_at, rate_limits = latest_codex_rate_limits()
            if rate_limits:
                payload = _codex_rate_limits_payload(
                    observed_at=observed_at,
                    rate_limits=rate_limits,
                    source="snapshot",
                    allow_app_server=allow_app_server,
                    fallback_reason="safe_api_snapshot_first",
                    cache_status="miss",
                )
                _CODEX_CACHE[cache_key] = (now_ts, payload)
                _write_codex_disk_cache(now_ts, payload)
                return payload

        observed_at, rate_limits, source, fallback_reason = current_codex_rate_limits(
            latest_codex_rate_limits,
            allow_app_server_fallback=allow_app_server,
        )
        if not rate_limits:
            payload = {
                "provider": "codex",
                "available": False,
                "source": source,
                "allow_app_server": allow_app_server,
                "error": fallback_reason or "no Codex data",
                "observed_at": _utc_now(),
            }
            _CODEX_CACHE[cache_key] = (now_ts, payload)
            return payload

        payload = _codex_rate_limits_payload(
            observed_at=observed_at,
            rate_limits=rate_limits,
            source=source,
            allow_app_server=allow_app_server,
            fallback_reason=fallback_reason,
            cache_status="miss",
        )
        _CODEX_CACHE[cache_key] = (now_ts, payload)
        # A user-authorized App Server refresh is also valid safe-read cache
        # evidence. Persist it so later ordinary `/v1/quota` observations do
        # not fall back to an older snapshot and misclassify Codex as stale.
        _write_codex_disk_cache(now_ts, payload)
        return payload
    except CodexAuthError as exc:
        return {"provider": "codex", "available": False, "source": "web", "error": str(exc), "observed_at": _utc_now()}
    except CodexWebError as exc:
        return {"provider": "codex", "available": False, "source": "web", "error": str(exc), "observed_at": _utc_now()}
    except Exception as exc:  # noqa: BLE001
        return {"provider": "codex", "available": False, "observed_at": _utc_now(), **_jsonable_error(exc)}


def _read_codex_disk_cache(now_ts: float) -> dict[str, Any] | None:
    try:
        raw = json.loads(_CODEX_DISK_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(raw, dict):
        return None
    cached_at = raw.get("cached_at")
    payload = raw.get("payload")
    if not isinstance(cached_at, (int, float)) or not isinstance(payload, dict):
        return None
    age_seconds = now_ts - float(cached_at)
    if age_seconds < 0 or age_seconds > CODEX_DISK_STALE_SECONDS:
        return None
    result = dict(payload)
    result["cache"] = {
        "status": "disk-hit" if age_seconds <= CODEX_CACHE_TTL_SECONDS else "stale-disk-hit",
        "ttl_seconds": CODEX_CACHE_TTL_SECONDS,
        "stale_after_seconds": CODEX_DISK_STALE_SECONDS,
        "age_seconds": round(age_seconds, 3),
    }
    return result


def _write_codex_disk_cache(now_ts: float, payload: dict[str, Any]) -> None:
    try:
        _CODEX_DISK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CODEX_DISK_CACHE_PATH.write_text(
            json.dumps({"cached_at": now_ts, "payload": payload}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _codex_rate_limits_payload(
    *,
    observed_at: dt.datetime | None,
    rate_limits: dict[str, Any],
    source: str,
    allow_app_server: bool,
    fallback_reason: str | None,
    cache_status: str,
) -> dict[str, Any]:
    five_hour = _window_payload(rate_limits, "5h")
    weekly = _window_payload(rate_limits, "weekly")
    buckets = rate_limits.get("buckets") if isinstance(rate_limits.get("buckets"), list) else []
    groups = rate_limits.get("groups") if isinstance(rate_limits.get("groups"), list) else []
    return {
        "provider": "codex",
        "available": True,
        "source": source,
        "allow_app_server": allow_app_server,
        "observed_at": observed_at.isoformat().replace("+00:00", "Z") if observed_at else _utc_now(),
        "plan": rate_limits.get("plan_type") or "?",
        "limit_id": rate_limits.get("limit_id"),
        "rate_limit_reached_type": rate_limits.get("rate_limit_reached_type"),
        "five_hour": five_hour,
        "weekly": weekly,
        "5h_left": None if five_hour is None else five_hour.get("remaining_percent"),
        "7d_left": None if weekly is None else weekly.get("remaining_percent"),
        "5h_reset": None if five_hour is None else five_hour.get("reset_time"),
        "7d_reset": None if weekly is None else weekly.get("reset_time"),
        "groups": groups,
        "buckets": buckets,
        "primary": rate_limits.get("primary") or None,
        "secondary": rate_limits.get("secondary") or None,
        "group_count": len(groups),
        "bucket_count": len(buckets),
        "fallback_reason": fallback_reason,
        "cache": {"status": cache_status, "ttl_seconds": CODEX_CACHE_TTL_SECONDS, "age_seconds": 0},
    }


def _claude_payload() -> dict[str, Any]:
    try:
        data = live_claude_usage(timeout=5)
        five_h = data.get("five_hour") or {}
        seven_d = data.get("seven_day") or {}
        try:
            plan = live_claude_plan(timeout=2)
        except Exception:  # noqa: BLE001
            plan = None
        return {
            "provider": "claude",
            "available": True,
            "observed_at": _utc_now(),
            "source": "browser",
            "plan": plan,
            "5h_left": int(round(100 - float(five_h.get("utilization", 0)))),
            "7d_left": int(round(100 - float(seven_d.get("utilization", 0)))),
            "5h_reset": five_h.get("resets_at"),
            "7d_reset": seven_d.get("resets_at"),
        }
    except ClaudeWebError as exc:
        return {"provider": "claude", "available": False, "observed_at": _utc_now(), "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"provider": "claude", "available": False, "observed_at": _utc_now(), **_jsonable_error(exc)}


def _deepseek_payload() -> dict[str, Any] | None:
    if not has_deepseek_api_key():
        return None
    try:
        observed_at, data = live_deepseek_balance()
        balances = data.get("balance_infos") or []
        return {
            "provider": "deepseek",
            "available": bool(data.get("is_available")),
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "source": "api.deepseek.com",
            "balances": balances,
        }
    except (DeepSeekAuthError, DeepSeekError) as exc:
        return {"provider": "deepseek", "available": False, "observed_at": _utc_now(), "error": str(exc)}


def _google_payload() -> dict[str, Any] | None:
    if not has_google_oauth_creds():
        return None
    try:
        observed_at, data = live_google_quota()
        return {
            "provider": "google",
            "available": True,
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "source": data.get("source") or "google_quota",
            "summary": data.get("summary") or {},
            "primary": data.get("primary") or {},
            "groups": data.get("quota_groups") or [],
            "buckets": data.get("buckets") or [],
            "antigravity": data.get("antigravity") or {},
        }
    except (GoogleQuotaAuthError, GoogleQuotaError) as exc:
        return {"provider": "google", "available": False, "observed_at": _utc_now(), "error": str(exc)}


def _gemini_payload() -> dict[str, Any] | None:
    if not has_gemini_app_cookies():
        # Dropping the provider entirely reads as "Gemini has no quota" to a
        # dashboard; say why it is missing instead.
        return {
            "provider": "gemini",
            "available": False,
            "observed_at": _utc_now(),
            "error": "No Google cookies for gemini.google.com in Chrome",
        }
    try:
        observed_at, data = live_gemini_app_usage()
        return {
            "provider": "gemini",
            "available": bool(data.get("available", True)),
            "observed_at": observed_at.isoformat().replace("+00:00", "Z"),
            "source": data.get("source") or "gemini.google.com/usage",
            "cached": bool(data.get("cached")),
            "cache_age_seconds": data.get("cache_age_seconds"),
            "cache_stale": bool(data.get("cache_stale")),
            "summary": data.get("summary") or {},
            "buckets": data.get("buckets") or [],
            "models": data.get("models") or [],
            "unavailable_reason": data.get("unavailable_reason"),
        }
    except GeminiAppUsageError as exc:
        return {"provider": "gemini", "available": False, "observed_at": _utc_now(), "error": str(exc)}


def _llm_api_payload() -> dict[str, Any] | None:
    if not has_llm_api_provider_config():
        return None
    try:
        # live_llm_api_balances returns a single snapshot dict, not a
        # (timestamp, payload) pair like the other providers.
        snapshot = live_llm_api_balances()
        return {
            "provider": "llm_api",
            "available": True,
            "observed_at": snapshot.get("generated_at") or _utc_now(),
            "source": "configured_api_keys",
            "balances": snapshot,
        }
    except Exception as exc:  # noqa: BLE001
        return {"provider": "llm_api", "available": False, "observed_at": _utc_now(), **_jsonable_error(exc)}


def quota_payload(*, allow_app_server: bool = False) -> dict[str, Any]:
    # Provider probes are independent remote operations. Running them serially
    # made one slow source hold the entire aggregate endpoint for up to a
    # minute, which in turn made healthy Claude/Codex observations look
    # unavailable. Isolate them behind one bounded aggregate deadline.
    probes = {
        "claude": _claude_payload,
        "codex": lambda: _codex_payload(allow_app_server=allow_app_server),
        "deepseek": _deepseek_payload,
        "google": _google_payload,
        "gemini": _gemini_payload,
        "llm_api": _llm_api_payload,
    }
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=len(probes), thread_name_prefix="ai-limit-provider")
    futures = {executor.submit(probe): name for name, probe in probes.items()}
    done, pending = concurrent.futures.wait(futures, timeout=AGGREGATE_DEADLINE_SECONDS)
    providers: dict[str, Any] = {}
    for future in done:
        name = futures[future]
        try:
            payload = future.result()
        except Exception as exc:  # noqa: BLE001
            payload = {"provider": name, "available": False, "observed_at": _utc_now(), **_jsonable_error(exc)}
        if payload is not None:
            providers[name] = payload
    for future in pending:
        name = futures[future]
        future.cancel()
        providers[name] = {
            "provider": name,
            "available": False,
            "observed_at": _utc_now(),
            "error": f"aggregate probe exceeded {AGGREGATE_DEADLINE_SECONDS:g}s deadline",
        }
    # Do not wait for a provider's own bounded network timeout after the
    # aggregate response is already complete.
    executor.shutdown(wait=False, cancel_futures=True)
    return {
        "module": "ai-limit-local-api",
        "observed_at": _utc_now(),
        "providers": providers,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ai-limit-local-api/0"

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        allow_app_server = _as_bool((params.get("allow_app_server") or [None])[0])

        if parsed.path == "/health":
            self._write_json({"module": "ai-limit-local-api", "status": "ready", "observed_at": _utc_now()})
            return
        if parsed.path in {"/quota", "/v1/quota"}:
            self._write_json(quota_payload(allow_app_server=allow_app_server))
            return
        if parsed.path in {"/quota/codex", "/v1/quota/codex"}:
            self._write_json(_codex_payload(allow_app_server=allow_app_server))
            return
        if parsed.path in {"/quota/claude", "/v1/quota/claude"}:
            self._write_json(_claude_payload())
            return
        self._write_json({"error": "not_found", "path": parsed.path}, status=404)

    def log_message(self, fmt: str, *args: Any) -> None:
        if _as_bool(os.environ.get("AI_LIMIT_API_LOG_REQUESTS")):
            super().log_message(fmt, *args)

    def _write_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"ai-limit local API listening on http://{host}:{port}", flush=True)
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run ai-limit local read-only JSON API")
    parser.add_argument("--host", default=os.environ.get("AI_LIMIT_API_HOST", DEFAULT_HOST))
    parser.add_argument("--port", type=int, default=int(os.environ.get("AI_LIMIT_API_PORT", DEFAULT_PORT)))
    args = parser.parse_args(argv)
    run(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
