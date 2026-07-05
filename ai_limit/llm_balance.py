"""Minimal LLM balance adapters adopted from QuantOS for operator monitoring."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import importlib
from typing import Any
from urllib.parse import quote

import requests


_UNSET = object()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class BalanceAdapterContext:
    provider: str
    secret: str | None
    env_values: dict[str, str] | None = None


class BalanceAdapter:
    provider: str = ""
    source_type: str = "unsupported"
    supported: bool = False
    currency: str | None = None
    threshold: float | None = None

    def fetch(self, context: BalanceAdapterContext) -> dict[str, Any]:
        return self._build_result(status="unsupported", message="Balance check not supported")

    def _build_result(
        self,
        *,
        status: str,
        amount: float | None = None,
        currency: str | None = None,
        breakdown: dict[str, Any] | None = None,
        threshold: Any = _UNSET,
        message: str | None = None,
    ) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "source_type": self.source_type,
            "status": status,
            "currency": currency or self.currency,
            "amount": amount,
            "threshold": self.threshold if threshold is _UNSET else threshold,
            "breakdown": breakdown or {},
            "fetched_at": _utc_now_iso(),
            "message": message,
        }


class NoopBalanceAdapter(BalanceAdapter):
    def __init__(self, provider: str) -> None:
        self.provider = provider


class DeepSeekOfficialBalanceAdapter(BalanceAdapter):
    provider = "deepseek"
    source_type = "official_api"
    supported = True
    endpoint = "https://api.deepseek.com/v1/user/balance"
    threshold_by_currency = {"USD": 5.0, "CNY": 20.0}

    def fetch(self, context: BalanceAdapterContext) -> dict[str, Any]:
        if not context.secret:
            return self._build_result(status="missing_credentials", message="Missing API key for DeepSeek balance API")
        response = requests.get(
            self.endpoint,
            headers={"Authorization": f"Bearer {context.secret}"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        raw_infos = payload.get("balance_infos") if isinstance(payload, dict) else None
        if not isinstance(raw_infos, list):
            raise RuntimeError("Missing balance_infos in DeepSeek response")
        balances: list[dict[str, Any]] = []
        for item in raw_infos:
            if not isinstance(item, dict):
                continue
            currency = str(item.get("currency", "")).upper()
            balances.append({
                "currency": currency,
                "total_balance": self._safe_float(item.get("total_balance")) or 0.0,
                "granted_balance": self._safe_float(item.get("granted_balance")) or 0.0,
                "topped_up_balance": self._safe_float(item.get("topped_up_balance")) or 0.0,
            })
        if not balances:
            raise RuntimeError("No valid balances in DeepSeek response")
        primary = max(
            [b for b in balances if (b.get("total_balance") or 0) > 0] or balances,
            key=lambda b: float(b.get("total_balance") or 0.0),
        )
        currency = str(primary.get("currency") or "USD")
        return self._build_result(
            status="ok",
            amount=primary.get("total_balance"),
            currency=currency,
            threshold=self.threshold_by_currency.get(currency),
            breakdown={
                "currencies": balances,
                "granted_balance": primary.get("granted_balance"),
                "topped_up_balance": primary.get("topped_up_balance"),
            },
        )

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        try:
            return None if value is None else float(value)
        except (TypeError, ValueError):
            return None


class MoonshotOfficialBalanceAdapter(BalanceAdapter):
    provider = "moonshot"
    source_type = "official_api"
    supported = True
    currency = "USD"
    threshold = 5.0
    endpoint = "https://api.moonshot.ai/v1/users/me/balance"

    def fetch(self, context: BalanceAdapterContext) -> dict[str, Any]:
        if not context.secret:
            return self._build_result(status="missing_credentials", message="Missing API key for official balance API")
        response = requests.get(
            self.endpoint,
            headers={"Authorization": f"Bearer {context.secret}"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected balance response shape")
        return self._build_result(
            status="ok",
            amount=_safe_float(data.get("available_balance")),
            breakdown={
                "voucher_balance": _safe_float(data.get("voucher_balance")),
                "cash_balance": _safe_float(data.get("cash_balance")),
            },
        )


class XAIOfficialBalanceAdapter(BalanceAdapter):
    provider = "xai"
    source_type = "official_api"
    supported = True
    currency = "USD"
    threshold = 5.0
    management_key_envs = ("XAI_MANAGEMENT_API_KEY", "GROK_MANAGEMENT_API_KEY")
    team_id_envs = ("XAI_TEAM_ID", "GROK_TEAM_ID")

    def fetch(self, context: BalanceAdapterContext) -> dict[str, Any]:
        env_values = context.env_values or {}
        management_key = _lookup_env(env_values, self.management_key_envs)
        team_id = _lookup_env(env_values, self.team_id_envs)
        if not management_key:
            return self._build_result(
                status="missing_credentials",
                message="Official balance API requires xAI management key (XAI_MANAGEMENT_API_KEY)",
            )
        if not team_id:
            return self._build_result(
                status="missing_credentials",
                message="Official balance API requires xAI team id (XAI_TEAM_ID)",
            )
        endpoint = f"https://management-api.x.ai/v1/billing/teams/{team_id}/prepaid/balance"
        response = requests.get(endpoint, headers={"Authorization": f"Bearer {management_key}"}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        total = payload.get("total") if isinstance(payload, dict) else None
        changes = payload.get("changes") if isinstance(payload, dict) else None
        raw_cents = _extract_xai_cents(total)
        amount = abs(raw_cents) / 100.0 if raw_cents is not None else None
        return self._build_result(
            status="ok",
            amount=amount,
            breakdown={
                "team_id": team_id,
                "raw_total_cents": raw_cents,
                "changes_count": len(changes) if isinstance(changes, list) else 0,
            },
        )


class OpenRouterOfficialBalanceAdapter(BalanceAdapter):
    provider = "openrouter"
    source_type = "official_api"
    supported = True
    currency = "USD"
    threshold = 5.0
    endpoint = "https://openrouter.ai/api/v1/key"
    credits_endpoint = "https://openrouter.ai/api/v1/credits"
    management_key_envs = ("OPENROUTER_MANAGEMENT_API_KEY",)

    def fetch(self, context: BalanceAdapterContext) -> dict[str, Any]:
        env_values = context.env_values or {}
        management_secret = _lookup_env(env_values, self.management_key_envs)
        secret = management_secret or context.secret
        if not secret:
            return self._build_result(
                status="missing_credentials",
                message="Official key status API requires OPENROUTER_API_KEY or OPENROUTER_MANAGEMENT_API_KEY",
            )
        response = requests.get(self.endpoint, headers={"Authorization": f"Bearer {secret}"}, timeout=15)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected key status response shape")

        limit = _safe_float(data.get("limit"))
        limit_remaining = _safe_float(data.get("limit_remaining"))
        usage = _safe_float(data.get("usage"))
        amount = limit_remaining
        amount_source = "key.limit_remaining" if amount is not None else None
        total_credits = None
        total_usage = None
        credits_remaining = None
        credits_fetch_error = None
        if amount is None and limit is not None and usage is not None:
            amount = max(limit - usage, 0.0)
            amount_source = "key.limit_minus_usage"
        if amount is None and management_secret:
            try:
                total_credits, total_usage = self._fetch_total_credits(management_secret)
            except Exception as exc:
                credits_fetch_error = str(exc)
            else:
                if total_credits is not None and total_usage is not None:
                    credits_remaining = max(total_credits - total_usage, 0.0)
                    amount = credits_remaining
                    amount_source = "credits.total_credits_minus_total_usage"

        message = None
        if amount is None:
            message = "OpenRouter key status available, but remaining credit was not reported"
            if not management_secret:
                message += "; add OPENROUTER_MANAGEMENT_API_KEY to query account credits"
            elif credits_fetch_error:
                message += "; account credits lookup failed"
        elif amount_source == "credits.total_credits_minus_total_usage":
            message = "OpenRouter key status did not report remaining credit; using account-level credits from management API"

        return self._build_result(
            status="ok",
            amount=amount,
            message=message,
            breakdown={
                "usage": usage,
                "limit": limit,
                "limit_remaining": limit_remaining,
                "total_credits": total_credits,
                "total_usage": total_usage,
                "credits_remaining": credits_remaining,
                "amount_source": amount_source,
                "credits_fetch_error": credits_fetch_error,
            },
        )

    def _fetch_total_credits(self, management_secret: str) -> tuple[float | None, float | None]:
        response = requests.get(
            self.credits_endpoint,
            headers={"Authorization": f"Bearer {management_secret}"},
            timeout=15,
        )
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected credits response shape")
        return _safe_float(data.get("total_credits")), _safe_float(data.get("total_usage"))


class OpenAIOfficialBalanceAdapter(BalanceAdapter):
    provider = "openai"
    source_type = "official_api"
    supported = True
    currency = "USD"
    threshold = 5.0
    credits_endpoint = "https://api.openai.com/dashboard/billing/credit_grants"
    costs_endpoint = "https://api.openai.com/v1/organization/costs"
    billing_key_envs = ("OPENAI_ADMIN_API_KEY", "OPENAI_USAGE_API_KEY", "OPENAI_MANAGEMENT_API_KEY")
    cycle_start_envs = ("OPENAI_PREPAID_CYCLE_START", "OPENAI_BUDGET_CYCLE_START")
    max_cost_pages = 24

    def fetch(self, context: BalanceAdapterContext) -> dict[str, Any]:
        env_values = context.env_values or {}
        billing_key_name, billing_secret = _lookup_env_with_name(env_values, self.billing_key_envs)
        if not billing_secret and context.secret:
            billing_key_name = "OPENAI_API_KEY"
            billing_secret = context.secret
        if not billing_secret:
            return self._build_result(
                status="missing_credentials",
                message="Missing OPENAI_API_KEY or OPENAI_ADMIN_API_KEY for OpenAI billing introspection",
            )

        cycle_start = self._resolve_cycle_start(env_values)
        response = requests.get(
            self.credits_endpoint,
            headers={"Authorization": f"Bearer {billing_secret}"},
            timeout=15,
        )
        if response.status_code in {403, 404}:
            cost_metric = self._build_cost_metric_from_costs(
                secret=billing_secret,
                billing_key_name=billing_key_name,
                cycle_start=cycle_start,
            )
            if cost_metric is not None:
                return cost_metric
            return self._build_result(
                status="ok",
                amount=None,
                message="OpenAI billing credit endpoint unavailable for this account; check the Usage/Billing dashboard",
                breakdown={
                    "status_code": response.status_code,
                    **({"billing_key_env": billing_key_name} if billing_key_name is not None else {}),
                },
            )

        response.raise_for_status()
        payload = response.json()
        amount, breakdown = self._extract_credit_payload(payload)
        if amount is None:
            cost_metric = self._build_cost_metric_from_costs(
                secret=billing_secret,
                billing_key_name=billing_key_name,
                cycle_start=cycle_start,
            )
            if cost_metric is not None:
                return cost_metric
        return self._build_result(
            status="ok",
            amount=amount,
            breakdown={
                **breakdown,
                "financial_metric_type": "balance",
                **({"billing_key_env": billing_key_name} if billing_key_name is not None else {}),
            },
            message=None if amount is not None else "OpenAI billing endpoint responded, but remaining credit was not reported",
        )

    def _build_cost_metric_from_costs(
        self,
        *,
        secret: str,
        billing_key_name: str | None,
        cycle_start: datetime,
    ) -> dict[str, Any] | None:
        try:
            costs_snapshot = self._fetch_costs_total(secret=secret, cycle_start=cycle_start)
        except requests.RequestException:
            return None
        return self._build_result(
            status="ok",
            amount=None,
            threshold=None,
            message="OpenAI cost since cycle start",
            breakdown={
                "financial_metric_type": "cost",
                "display_amount": costs_snapshot["spent"],
                "display_currency": self.currency,
                "cost_since_cycle_start": costs_snapshot["spent"],
                "cycle_start": cycle_start.date().isoformat(),
                "cost_source": "organization.costs",
                "cost_pages_fetched": costs_snapshot["pages_fetched"],
                **({"billing_key_env": billing_key_name} if billing_key_name is not None else {}),
            },
        )

    def _fetch_costs_total(self, *, secret: str, cycle_start: datetime) -> dict[str, float | int]:
        total = 0.0
        pages_fetched = 0
        next_page: str | None = None
        while pages_fetched < self.max_cost_pages:
            params: dict[str, Any] = {
                "start_time": int(cycle_start.timestamp()),
                "bucket_width": "1d",
                "limit": 90,
            }
            if next_page:
                params["page"] = next_page
            response = requests.get(
                self.costs_endpoint,
                headers={"Authorization": f"Bearer {secret}"},
                params=params,
                timeout=15,
            )
            response.raise_for_status()
            payload = response.json()
            total += float(self._extract_costs_total(payload))
            pages_fetched += 1
            has_more = bool(payload.get("has_more")) if isinstance(payload, dict) else False
            raw_next_page = payload.get("next_page") if isinstance(payload, dict) else None
            next_page = str(raw_next_page).strip() if raw_next_page else None
            if not has_more or not next_page:
                break
        return {"spent": total, "pages_fetched": pages_fetched}

    def _extract_costs_total(self, payload: Any) -> float:
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise RuntimeError("Unexpected OpenAI costs response shape")
        total = 0.0
        for bucket in payload.get("data", []):
            if not isinstance(bucket, dict) or not isinstance(bucket.get("results"), list):
                continue
            for result in bucket.get("results", []):
                if not isinstance(result, dict):
                    continue
                amount = result.get("amount")
                value = _safe_float(amount.get("value")) if isinstance(amount, dict) else _safe_float(result.get("amount"))
                if value is not None:
                    total += value
        return total

    @classmethod
    def _resolve_cycle_start(cls, env_values: dict[str, str]) -> datetime:
        for name in cls.cycle_start_envs:
            value = env_values.get(name)
            if not value:
                continue
            try:
                parsed = datetime.fromisoformat(str(value).strip())
            except ValueError:
                continue
            return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        now = datetime.now(timezone.utc)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def _extract_credit_payload(self, payload: Any) -> tuple[float | None, dict[str, Any]]:
        if not isinstance(payload, dict):
            raise RuntimeError("Unexpected OpenAI billing response shape")
        total_available = _safe_float(payload.get("total_available"))
        total_granted = _safe_float(payload.get("total_granted"))
        total_used = _safe_float(payload.get("total_used"))
        amount = total_available
        amount_source = "credit_grants.total_available" if amount is not None else None
        if amount is None and total_granted is not None and total_used is not None:
            amount = max(total_granted - total_used, 0.0)
            amount_source = "credit_grants.total_granted_minus_total_used"
        return amount, {
            "total_available": total_available,
            "total_granted": total_granted,
            "total_used": total_used,
            "amount_source": amount_source,
        }


class ArkOfficialBalanceAdapter(BalanceAdapter):
    provider = "ark"
    source_type = "official_api"
    supported = True
    currency = "CNY"
    threshold = 50.0
    endpoint = "https://open.volcengineapi.com/"
    action = "QueryBalanceAcct"
    version = "2022-01-01"
    service = "billing"
    access_key_envs = ("VOLCENGINE_ACCESS_KEY_ID", "VOLC_ACCESS_KEY_ID")
    secret_key_envs = ("VOLCENGINE_SECRET_ACCESS_KEY", "VOLC_SECRET_ACCESS_KEY")
    region_envs = ("VOLCENGINE_REGION", "ARK_REGION")

    def fetch(self, context: BalanceAdapterContext) -> dict[str, Any]:
        env_values = context.env_values or {}
        access_key = _lookup_env(env_values, self.access_key_envs)
        secret_key = _lookup_env(env_values, self.secret_key_envs)
        region = _lookup_env(env_values, self.region_envs) or "cn-beijing"
        if not access_key or not secret_key:
            return self._build_result(
                status="missing_credentials",
                message="Official balance API requires Volcengine account AK/SK (VOLCENGINE_ACCESS_KEY_ID / VOLCENGINE_SECRET_ACCESS_KEY)",
            )

        query = {"Action": self.action, "Version": self.version}
        headers = self._sign_headers(access_key=access_key, secret_key=secret_key, region=region, query=query)
        response = requests.get(self.endpoint, params=query, headers=headers, timeout=15)
        payload = response.json() if response.content else {}
        if response.status_code >= 400:
            return self._build_result(
                status="error",
                message=self._extract_error_message(payload, response.status_code),
                breakdown={"region": region},
            )
        result = payload.get("Result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            raise RuntimeError("Unexpected billing response shape")
        return self._build_result(
            status="ok",
            amount=_safe_float(result.get("AvailableBalance")),
            breakdown={
                "cash_balance": _safe_float(result.get("CashBalance")),
                "credit_limit": _safe_float(result.get("CreditLimit")),
                "freeze_amount": _safe_float(result.get("FreezeAmount")),
                "arrears_balance": _safe_float(result.get("ArrearsBalance")),
                "region": region,
            },
        )

    def _sign_headers(self, *, access_key: str, secret_key: str, region: str, query: dict[str, str]) -> dict[str, str]:
        now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        body_hash = hashlib.sha256(b"").hexdigest()
        headers = {"Host": "open.volcengineapi.com", "X-Date": now, "X-Content-Sha256": body_hash}
        signed_headers = {key.lower(): value for key, value in headers.items() if key == "Host" or key.startswith("X-")}
        signed_str = "".join(f"{key}:{signed_headers[key]}\n" for key in sorted(signed_headers.keys()))
        signed_headers_string = ";".join(sorted(signed_headers.keys()))
        canonical_request = "\n".join(["GET", "/", _canonical_query(query), signed_str, signed_headers_string, body_hash])
        credential_scope = "/".join([now[:8], region, self.service, "request"])
        signing_str = "\n".join(["HMAC-SHA256", now, credential_scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])
        signing_key = _get_signing_secret_key(secret_key=secret_key, date=now[:8], region=region, service=self.service)
        signature = hmac.new(signing_key, signing_str.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            "HMAC-SHA256 "
            f"Credential={access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers_string}, "
            f"Signature={signature}"
        )
        return headers

    @staticmethod
    def _extract_error_message(payload: Any, status_code: int) -> str:
        if isinstance(payload, dict):
            metadata = payload.get("ResponseMetadata")
            if isinstance(metadata, dict):
                error = metadata.get("Error")
                if isinstance(error, dict):
                    code = error.get("Code")
                    message = error.get("Message")
                    if code == "AccessDenied":
                        return "Balance API access denied: missing billing:QueryBalanceAcct permission"
                    if code and message:
                        return f"Balance API request failed ({status_code} {code}: {message})"
        return f"Balance API request failed ({status_code})"


class DashScopeOfficialBalanceAdapter(BalanceAdapter):
    provider = "dashscope"
    source_type = "official_api"
    supported = True
    threshold_by_currency = {"USD": 5.0, "CNY": 50.0}
    access_key_envs = ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALICLOUD_ACCESS_KEY_ID")
    secret_key_envs = ("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALICLOUD_ACCESS_KEY_SECRET")
    region_envs = ("ALIBABA_CLOUD_REGION_ID", "ALICLOUD_REGION_ID", "DASHSCOPE_REGION")

    def fetch(self, context: BalanceAdapterContext) -> dict[str, Any]:
        env_values = context.env_values or {}
        access_key = _lookup_env(env_values, self.access_key_envs)
        secret_key = _lookup_env(env_values, self.secret_key_envs)
        region = _lookup_env(env_values, self.region_envs) or "cn-hangzhou"
        if not access_key or not secret_key:
            return self._build_result(
                status="missing_credentials",
                message="Official balance API requires Alibaba Cloud account AK/SK; DashScope API key alone cannot read account balance.",
            )
        payload = self._query_balance(access_key=access_key, secret_key=secret_key, region=region)
        body = payload.get("body") if isinstance(payload, dict) else None
        if not isinstance(body, dict):
            raise RuntimeError("Unexpected billing response shape")
        if body.get("Success") is False:
            code = body.get("Code")
            message = body.get("Message") or "Unknown billing error"
            return self._build_result(
                status="error",
                message=f"Balance API request failed ({code}: {message})" if code else f"Balance API request failed ({message})",
                breakdown={"region": region, "request_id": body.get("RequestId")},
            )
        data = body.get("Data")
        if not isinstance(data, dict):
            raise RuntimeError("Unexpected billing data shape")
        currency = str(data.get("Currency") or "CNY").upper()
        return self._build_result(
            status="ok",
            amount=_safe_float(data.get("AvailableAmount")),
            currency=currency,
            threshold=self.threshold_by_currency.get(currency),
            breakdown={
                "available_cash_amount": _safe_float(data.get("AvailableCashAmount")),
                "credit_amount": _safe_float(data.get("CreditAmount")),
                "quota_limit": _safe_float(data.get("QuotaLimit")),
                "region": region,
                "request_id": body.get("RequestId"),
            },
        )

    def _query_balance(self, *, access_key: str, secret_key: str, region: str) -> dict[str, Any]:
        try:
            open_api_models = importlib.import_module("alibabacloud_tea_openapi.models")
            bss_client_module = importlib.import_module("alibabacloud_bssopenapi20171214.client")
        except ModuleNotFoundError as exc:
            raise RuntimeError("Alibaba Cloud billing SDK unavailable; install alibabacloud-bssopenapi20171214") from exc

        config = open_api_models.Config(
            access_key_id=access_key,
            access_key_secret=secret_key,
            region_id=region,
            connect_timeout=5000,
            read_timeout=10000,
        )
        client = bss_client_module.Client(config)
        response = client.query_account_balance()
        if hasattr(response, "to_map"):
            payload = response.to_map()
            if isinstance(payload, dict):
                return payload
        if isinstance(response, dict):
            return response
        raise RuntimeError("Unexpected Alibaba Cloud billing SDK response")


class BalanceAdapterRegistry:
    def __init__(self) -> None:
        self._adapters = {
            "openai": OpenAIOfficialBalanceAdapter(),
            "openrouter": OpenRouterOfficialBalanceAdapter(),
            "xai": XAIOfficialBalanceAdapter(),
            "moonshot": MoonshotOfficialBalanceAdapter(),
            "ark": ArkOfficialBalanceAdapter(),
            "dashscope": DashScopeOfficialBalanceAdapter(),
            "deepseek": DeepSeekOfficialBalanceAdapter(),
        }

    def resolve(self, provider: str) -> BalanceAdapter:
        return self._adapters.get(provider.lower(), NoopBalanceAdapter(provider))

    def fetch_balance(
        self,
        provider: str,
        secret: str | None,
        env_values: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        adapter = self.resolve(provider)
        try:
            return adapter.fetch(BalanceAdapterContext(provider=provider, secret=secret, env_values=env_values))
        except requests.HTTPError as exc:
            status_code = exc.response.status_code if exc.response is not None else None
            message = f"Balance API request failed ({status_code})" if status_code else str(exc)
            return adapter._build_result(status="error", message=message)
        except requests.RequestException as exc:
            return adapter._build_result(status="error", message=f"Balance API unavailable: {exc}")
        except Exception as exc:
            return adapter._build_result(status="error", message=f"Balance API parse failed: {exc}")

    def describe_balance_credentials(
        self,
        provider: str,
        *,
        env_values: dict[str, str] | None = None,
        inference_env_name: str | None = None,
    ) -> dict[str, Any]:
        env_values = env_values or {}
        provider_id = provider.lower()
        if provider_id == "moonshot":
            return _describe_any_key_credentials(
                requirement_mode="inference_api_key",
                env_groups=[("MOONSHOT_API_KEY",)],
                env_values=env_values,
                inference_env_name=inference_env_name,
                note="Official balance API reuses the Moonshot inference key.",
            )
        if provider_id == "openrouter":
            return _describe_openrouter_credentials(env_values=env_values, inference_env_name=inference_env_name)
        if provider_id == "openai":
            return _describe_openai_credentials(env_values=env_values, inference_env_name=inference_env_name)
        if provider_id == "xai":
            return _describe_grouped_credentials(
                requirement_mode="management_api_key_plus_team_id",
                env_groups=[("XAI_MANAGEMENT_API_KEY", "GROK_MANAGEMENT_API_KEY"), ("XAI_TEAM_ID", "GROK_TEAM_ID")],
                env_values=env_values,
                note="xAI prepaid balance needs a management key plus team id.",
            )
        if provider_id == "ark":
            return _describe_grouped_credentials(
                requirement_mode="volcengine_account_aksk",
                env_groups=[("VOLCENGINE_ACCESS_KEY_ID", "VOLC_ACCESS_KEY_ID"), ("VOLCENGINE_SECRET_ACCESS_KEY", "VOLC_SECRET_ACCESS_KEY")],
                env_values=env_values,
                note="ARK balance uses Volcengine account AK/SK, not ARK_API_KEY alone.",
            )
        if provider_id == "dashscope":
            return _describe_grouped_credentials(
                requirement_mode="alibaba_cloud_account_aksk",
                env_groups=[("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALICLOUD_ACCESS_KEY_ID"), ("ALIBABA_CLOUD_ACCESS_KEY_SECRET", "ALICLOUD_ACCESS_KEY_SECRET")],
                env_values=env_values,
                note="DashScope balance uses Alibaba Cloud account AK/SK, not DASHSCOPE_API_KEY alone.",
            )
        if provider_id == "deepseek":
            return _describe_any_key_credentials(
                requirement_mode="inference_api_key",
                env_groups=[("DEEPSEEK_API_KEY", "DeepSeek_KEY")],
                env_values=env_values,
                inference_env_name=inference_env_name,
                note="Official balance API reuses the DeepSeek inference key.",
            )
        adapter = self.resolve(provider_id)
        return {
            "supported": adapter.supported,
            "configured": False,
            "access_level": "unsupported",
            "requirement_mode": "unsupported",
            "configured_via": [],
            "missing_env_groups": [],
            "note": adapter._build_result(status="unsupported", message="Balance check not supported").get("message"),
        }


def _safe_float(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _lookup_env(env_values: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env_values.get(name)
        if value and str(value).strip():
            return str(value).strip()
    return None


def _lookup_env_with_name(env_values: dict[str, str], names: tuple[str, ...]) -> tuple[str | None, str | None]:
    for name in names:
        value = env_values.get(name)
        if value and str(value).strip():
            return name, str(value).strip()
    return None, None


def _extract_xai_cents(total: Any) -> int | None:
    if not isinstance(total, dict):
        return None
    raw_value = total.get("val")
    try:
        return None if raw_value is None else int(str(raw_value))
    except (TypeError, ValueError):
        return None


def _first_present_env(env_values: dict[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = env_values.get(name)
        if value and str(value).strip():
            return name
    return None


def _describe_any_key_credentials(
    *,
    requirement_mode: str,
    env_groups: list[tuple[str, ...]],
    env_values: dict[str, str],
    inference_env_name: str | None,
    note: str,
) -> dict[str, Any]:
    configured_via = [inference_env_name] if inference_env_name else []
    if not configured_via:
        for group in env_groups:
            present = _first_present_env(env_values, group)
            if present:
                configured_via.append(present)
                break
    return {
        "supported": True,
        "configured": bool(configured_via),
        "access_level": "full" if configured_via else "missing",
        "requirement_mode": requirement_mode,
        "configured_via": configured_via,
        "missing_env_groups": [] if configured_via else [list(group) for group in env_groups],
        "note": note,
    }


def _describe_grouped_credentials(
    *,
    requirement_mode: str,
    env_groups: list[tuple[str, ...]],
    env_values: dict[str, str],
    note: str,
) -> dict[str, Any]:
    configured_via: list[str] = []
    missing_env_groups: list[list[str]] = []
    for group in env_groups:
        present = _first_present_env(env_values, group)
        if present:
            configured_via.append(present)
        else:
            missing_env_groups.append(list(group))
    return {
        "supported": True,
        "configured": not missing_env_groups,
        "access_level": "full" if not missing_env_groups else "missing",
        "requirement_mode": requirement_mode,
        "configured_via": configured_via,
        "missing_env_groups": missing_env_groups,
        "note": note,
    }


def _describe_openrouter_credentials(*, env_values: dict[str, str], inference_env_name: str | None) -> dict[str, Any]:
    management_env = _first_present_env(env_values, ("OPENROUTER_MANAGEMENT_API_KEY",))
    inference_env = inference_env_name or _first_present_env(env_values, ("OPENROUTER_API_KEY",))
    if management_env:
        return {
            "supported": True,
            "configured": True,
            "access_level": "full",
            "requirement_mode": "api_key_or_management_key",
            "configured_via": [management_env],
            "missing_env_groups": [],
            "note": "Management key enables account-level credits fallback when key status omits remaining credit.",
        }
    if inference_env:
        return {
            "supported": True,
            "configured": True,
            "access_level": "limited",
            "requirement_mode": "api_key_or_management_key",
            "configured_via": [inference_env],
            "missing_env_groups": [["OPENROUTER_MANAGEMENT_API_KEY"]],
            "note": "Inference key can query key status, but management key is still needed for account-level credits fallback.",
        }
    return {
        "supported": True,
        "configured": False,
        "access_level": "missing",
        "requirement_mode": "api_key_or_management_key",
        "configured_via": [],
        "missing_env_groups": [["OPENROUTER_API_KEY", "OPENROUTER_MANAGEMENT_API_KEY"]],
        "note": "OpenRouter balance needs a normal API key or a management key; management key gives fuller account-credit visibility.",
    }


def _describe_openai_credentials(*, env_values: dict[str, str], inference_env_name: str | None) -> dict[str, Any]:
    billing_env = _first_present_env(
        env_values,
        ("OPENAI_ADMIN_API_KEY", "OPENAI_USAGE_API_KEY", "OPENAI_MANAGEMENT_API_KEY"),
    )
    inference_env = inference_env_name or _first_present_env(env_values, ("OPENAI_API_KEY",))
    if billing_env:
        return {
            "supported": True,
            "configured": True,
            "access_level": "full",
            "requirement_mode": "billing_admin_or_usage_key",
            "configured_via": [billing_env],
            "missing_env_groups": [],
            "note": "Admin or usage key gives direct OpenAI billing introspection.",
        }
    if inference_env:
        return {
            "supported": True,
            "configured": True,
            "access_level": "limited",
            "requirement_mode": "billing_admin_or_usage_key",
            "configured_via": [inference_env],
            "missing_env_groups": [["OPENAI_ADMIN_API_KEY", "OPENAI_USAGE_API_KEY", "OPENAI_MANAGEMENT_API_KEY"]],
            "note": "Inference key may allow limited billing visibility, but admin or usage key is more reliable.",
        }
    return {
        "supported": True,
        "configured": False,
        "access_level": "missing",
        "requirement_mode": "billing_admin_or_usage_key",
        "configured_via": [],
        "missing_env_groups": [["OPENAI_ADMIN_API_KEY", "OPENAI_USAGE_API_KEY", "OPENAI_MANAGEMENT_API_KEY"], ["OPENAI_API_KEY"]],
        "note": "OpenAI billing usually needs admin or usage credentials; plain inference key support is account-dependent.",
    }


def _canonical_query(query: dict[str, str]) -> str:
    return "&".join(
        f"{quote(str(key), safe='-_.~')}={quote(str(value), safe='-_.~')}" for key, value in sorted(query.items())
    )


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signing_secret_key(*, secret_key: str, date: str, region: str, service: str) -> bytes:
    kdate = _hmac_sha256(secret_key.encode("utf-8"), date)
    kregion = _hmac_sha256(kdate, region)
    kservice = _hmac_sha256(kregion, service)
    return _hmac_sha256(kservice, "request")


balance_adapter_registry = BalanceAdapterRegistry()
