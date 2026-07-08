"""Native LLM API balance snapshot for AI Limit.

The balance adapters are local to this app; env names intentionally mirror the
operator setup used elsewhere so existing API keys can be reused without
running another project.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import time
from typing import Any

from ai_limit.llm_balance import balance_adapter_registry


_ENV_LOADED = False
_BALANCE_CACHE: dict[str, dict[str, Any]] = {}

_PROVIDERS: dict[str, dict[str, str]] = {
    "openai": {
        "api_key_env": "OPENAI_API_KEY",
        "display_name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "openrouter": {
        "api_key_env": "OPENROUTER_API_KEY",
        "display_name": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "default_model": "openai/gpt-4o-mini",
    },
    "xai": {
        "api_key_env": "XAI_API_KEY",
        "display_name": "xAI",
        "base_url": "https://api.x.ai/v1",
        "default_model": "grok-3-mini",
    },
    "moonshot": {
        "api_key_env": "MOONSHOT_API_KEY",
        "display_name": "Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
    },
    "ark": {
        "api_key_env": "ARK_API_KEY",
        "display_name": "Doubao",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "default_model": "doubao-pro-4k",
    },
    "dashscope": {
        "api_key_env": "DASHSCOPE_API_KEY",
        "display_name": "Qwen",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-turbo",
    },
    "deepseek": {
        "api_key_env": "DEEPSEEK_API_KEY",
        "display_name": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
}


def _load_env_files() -> None:
    """Load local env files once, without overwriting already-exported values."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True

    repo_root = Path(__file__).resolve().parents[1]
    extra_paths = []
    for var_name in ("AI_LIMIT_LLM_ENV_FILES", "INFOWEAVE_LLM_ENV_FILES"):
        extra_paths.extend(
            Path(p).expanduser()
            for p in os.environ.get(var_name, "").split(os.pathsep)
            if p.strip()
        )

    candidates = [
        repo_root / ".env",
        Path.cwd() / ".env",
        Path("/Users/mac/development/projects/InfoWeave/.env"),
        *extra_paths,
    ]
    seen: set[Path] = set()
    for raw_path in candidates:
        resolved = raw_path.resolve(strict=False)
        if resolved in seen:
            continue
        seen.add(resolved)
        if not raw_path.exists():
            continue
        try:
            lines = raw_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        for raw_line in lines:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and value and not os.environ.get(key):
                os.environ[key] = value


def has_llm_api_provider_config() -> bool:
    _load_env_files()
    env_values = {key: value for key, value in os.environ.items() if value}
    for provider_id, config in _PROVIDERS.items():
        if os.environ.get(config["api_key_env"]):
            return True
        credentials = balance_adapter_registry.describe_balance_credentials(
            provider_id,
            env_values=env_values,
            inference_env_name=None,
        )
        if credentials.get("configured"):
            return True
    return False


def clear_llm_api_balance_cache() -> None:
    _BALANCE_CACHE.clear()


def live_llm_api_balances(cache_ttl_seconds: int = 300) -> dict[str, Any]:
    _load_env_files()
    env_values = {key: value for key, value in os.environ.items() if value}
    provider_items = list(_PROVIDERS.items())

    def fetch_one(item: tuple[str, dict[str, str]]) -> tuple[str, dict[str, Any]]:
        provider_id, config = item
        api_key = os.environ.get(config["api_key_env"]) or None
        return provider_id, _fetch_provider_balance(provider_id, api_key, env_values, cache_ttl_seconds)

    balances: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(provider_items)) as executor:
        for provider_id, balance in executor.map(fetch_one, provider_items):
            balances[provider_id] = balance

    providers = []
    for provider_id, config in provider_items:
        api_key_env = config["api_key_env"]
        configured = bool(os.environ.get(api_key_env))
        credentials = balance_adapter_registry.describe_balance_credentials(
            provider_id,
            env_values=env_values,
            inference_env_name=api_key_env if configured else None,
        )
        balance = balances.get(provider_id)
        warning_level, warning_message = _derive_provider_warning(
            configured=configured,
            balance=balance,
        )
        providers.append(
            {
                "provider_profile_id": provider_id,
                "display_name": config["display_name"],
                "status": "configured" if configured else "missing-credential",
                "configured": configured,
                "available": configured,
                "transport_kind": "openai-compatible",
                "base_url": config["base_url"],
                "default_model": config["default_model"],
                "api_key_env_var": api_key_env,
                "billing_supported": bool(credentials.get("supported")),
                "billing_configured": bool(credentials.get("configured")),
                "billing_access_level": str(credentials.get("access_level") or "unsupported"),
                "billing_requirement_mode": str(credentials.get("requirement_mode") or "unsupported"),
                "billing_configured_via": list(credentials.get("configured_via") or []),
                "billing_missing_env_groups": list(credentials.get("missing_env_groups") or []),
                "billing_note": credentials.get("note"),
                "balance": balance,
                "warning_level": warning_level,
                "warning_message": warning_message,
                "last_refresh_at": _utc_now_iso(),
            }
        )

    ready_provider_count = sum(1 for provider in providers if provider.get("configured"))
    balance_warning_count = sum(
        1 for provider in providers if str(provider.get("warning_level")) in {"warning", "critical"}
    )
    overall_status = "warning" if ready_provider_count == 0 or balance_warning_count else "ready"
    return {
        "source": "ai-limit native llm balance adapters",
        "generated_at": _utc_now_iso(),
        "overall_status": overall_status,
        "include_balance": True,
        "provider_count": len(providers),
        "ready_provider_count": ready_provider_count,
        "balance_warning_count": balance_warning_count,
        "providers": providers,
    }


def _fetch_provider_balance(
    provider_id: str,
    api_key: str | None,
    env_values: dict[str, str],
    cache_ttl_seconds: int,
) -> dict[str, Any]:
    if cache_ttl_seconds > 0:
        cached = _BALANCE_CACHE.get(provider_id)
        if cached is not None and (time.monotonic() - float(cached["cached_at"])) < cache_ttl_seconds:
            return dict(cached["payload"])
    payload = balance_adapter_registry.fetch_balance(provider_id, api_key, env_values)
    if cache_ttl_seconds > 0:
        _BALANCE_CACHE[provider_id] = {"cached_at": time.monotonic(), "payload": dict(payload)}
    return payload


def _derive_provider_warning(*, configured: bool, balance: dict[str, Any] | None) -> tuple[str, str | None]:
    if not configured:
        return "warning", "Provider API key missing"
    if balance is None:
        return "normal", None
    status = str(balance.get("status") or "unknown")
    amount = balance.get("amount")
    threshold = balance.get("threshold")
    if status == "error":
        return "warning", str(balance.get("message") or "Balance lookup failed")
    if status == "missing_credentials":
        return "warning", str(balance.get("message") or "Balance credentials missing")
    if status == "unsupported":
        return "info", str(balance.get("message") or "Balance unsupported")
    if status == "ok" and isinstance(amount, (int, float)) and isinstance(threshold, (int, float)):
        if amount <= 0:
            return "critical", "Balance exhausted"
        if amount <= threshold * 0.5:
            return "critical", "Balance below critical threshold"
        if amount <= threshold:
            return "warning", "Balance below warning threshold"
    if status == "ok" and amount is None:
        return "info", str(balance.get("message") or "Balance metric unavailable")
    return "normal", str(balance.get("message")) if balance.get("message") else None


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
