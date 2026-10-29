import base64
import datetime
import json
import os
import pathlib
import re
import select
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import time
import urllib.parse
import urllib.request

from ai_limit.i18n import t
from ai_limit.version import __version__
from ai_limit.keychain_status import default_keychain_locked

CLAUDE_USAGE_URL = "https://claude.ai/settings/usage"
CODEX_USAGE_URL = "https://chatgpt.com/codex/cloud/settings/analytics"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
GOOGLE_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
GEMINI_APP_USAGE_URL = "https://gemini.google.com/usage"
GEMINI_APP_BATCHEXECUTE_URL = "https://gemini.google.com/_/BardChatUi/data/batchexecute"
GEMINI_APP_USAGE_CACHE = pathlib.Path.home() / ".cache" / "ai-limit" / "gemini-app-usage.json"
# gemini.google.com/usage refreshes its own numbers every few minutes, and the
# 5-hour bucket can move tens of percent inside one window. A long fresh-cache
# TTL therefore shows numbers that visibly disagree with the vendor page, so the
# fresh window stays short and the older copy is only kept as an offline fallback.
GEMINI_APP_USAGE_CACHE_TTL_SEC = int(os.environ.get("AI_LIMIT_GEMINI_APP_CACHE_TTL_SEC", 120))
GEMINI_APP_USAGE_CACHE_STALE_SEC = int(os.environ.get("AI_LIMIT_GEMINI_APP_CACHE_STALE_SEC", 30 * 60))
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
# Same endpoint the Copilot CLI / IDE clients call for their quota footer. It is
# an internal GitHub API (not in the public REST reference), so treat it like
# the Claude / ChatGPT usage endpoints: reuse existing local sign-in, one narrow
# read-only request, and normalize defensively.
COPILOT_USER_URL = "https://api.github.com/copilot_internal/user"
COPILOT_USAGE_URL = "https://github.com/settings/billing"
COPILOT_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
COPILOT_KEYCHAIN_SERVICE = "copilot-cli"
COPILOT_TOKEN_PREFIXES = ("gho_", "ghu_", "github_pat_")
GROK_CREDITS_URL = "https://grok.com/grok_api_v2.GrokBuildBilling/GetGrokCreditsConfig"
GROK_USAGE_URL = "https://grok.com/?_s=usage"
GROK_SESSION_COOKIES = ("sso", "sso-rw")
GROK_CHROME_PROFILE_PATH = pathlib.Path.home() / ".config" / "ai-limit" / "grok-chrome-profile"
REMOTE_TIMEOUT_SEC = 15
CLAUDE_WEB_TIMEOUT_SEC = 15
CODEX_WINDOW_CACHE = pathlib.Path.home() / ".codex_window_cache"
GEMINI_OAUTH_PATH = pathlib.Path.home() / ".gemini" / "oauth_creds.json"
GEMINI_PROJECT_PATH = pathlib.Path.home() / ".gemini" / "config" / "projects" / "default-cli-project.json"
ANTIGRAVITY_LOG_DIR = pathlib.Path.home() / ".gemini" / "antigravity-cli" / "log"
ANTIGRAVITY_QUOTA_LOG_MAX_AGE_SEC = 36 * 60 * 60
ANTIGRAVITY_CLI_USAGE_CACHE = pathlib.Path.home() / ".cache" / "ai-limit" / "antigravity-cli-usage.json"
ANTIGRAVITY_CLI_USAGE_CACHE_TTL_SEC = 5 * 60
# Driving the TUI takes ~4-5s from a terminal but ~12s inside the menubar app,
# and at the old 18s the app timed out on every single attempt. Keep a wide
# margin over the slow case; a stuck drive still has to fit inside the app's
# 60s refresh tick alongside the other providers.
ANTIGRAVITY_CLI_USAGE_TIMEOUT_SEC = 30
# Driving the agy TUI is the only way to read the real quota windows, and when it
# fails it fails invisibly: the read loop returns whatever it captured, the parse
# raises, and live_google_quota degrades to a numberless view. Keep the last
# failed transcript on disk so the failure can be diagnosed after the fact.
ANTIGRAVITY_CLI_DEBUG_PATH = pathlib.Path.home() / ".cache" / "ai-limit" / "agy-usage-debug.txt"
ANTIGRAVITY_CLI_DEBUG_KEEP = 3
# The agy TUI is ready for a slash command once it renders a bare "> " prompt
# line. Whether this matches decides if /usage is ever typed, so the debug dump
# reports it too — hence a shared constant rather than an inline pattern.
ANTIGRAVITY_TUI_PROMPT_RE = re.compile(r"(?:^|\n)>\s*(?:\n|$)")
DEEPSEEK_KEY_PATHS = (
    pathlib.Path.home() / ".deepseek_api_key",
    pathlib.Path.home() / ".config" / "ai-limit" / "deepseek_api_key",
)
GOOGLE_MODEL_PRIORITY = (
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-flash-lite",
)
ANTIGRAVITY_MODEL_LABEL_RE = re.compile(r'Propagating selected model override to backend: label="([^"]+)"')
# Only the pre-1.1.9 wording is matched on purpose. 1.1.9 replaced it with a
# bare "RESOURCE_EXHAUSTED (code 429): Resource has been exhausted" that the
# userInfo and user-status cache refreshes also emit while model quota is still
# healthy, so matching the new line would raise a false "quota exhausted"
# banner. Current builds get their limited state from the quota numbers
# instead — see _antigravity_limit_from_quota.
ANTIGRAVITY_QUOTA_RE = re.compile(
    r"RESOURCE_EXHAUSTED \(code 429\): Individual quota reached\..*?Resets in "
    r"(?P<duration>(?:(?:\d+)h)?(?:(?:\d+)m)?(?:(?:\d+)s)?)"
)
# Antigravity CLI 1.1.9 (2026-07-31) began prefixing every log line with
# "ERROR: logging before google.Init: ", so the glog timestamp is no longer at
# the start of the line. Anchor on the severity+MMDD+HH:MM:SS shape instead of
# the line start; the trailing time makes a false positive very unlikely.
ANTIGRAVITY_LOG_TIME_RE = re.compile(
    r"(?:^|\s)[A-Z](?P<month>\d{2})(?P<day>\d{2}) (?P<time>\d{2}:\d{2}:\d{2})\.(?P<micro>\d{1,6})"
)
ANTIGRAVITY_LOG_FILE_RE = re.compile(r"cli-(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})_")
ANTIGRAVITY_CSRF_RE = re.compile(r"--csrf_token\s+(\S+)")
ANSI_RE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\[[0-?]*[ -/]*[@-~])"
)


# browser_cookie3 uses this marker for key-access/decryption failure. It does
# not identify the cause: lock state, item permissions and format failures
# must not be conflated. Keep the legacy helper names for existing callers.
CHROME_KEYCHAIN_LOCKED_MARKER = "Unable to get key for cookie decryption"


def _chrome_keychain_locked(errors) -> bool:
    return any(CHROME_KEYCHAIN_LOCKED_MARKER in str(err) for err in errors)


def _keychain_locked_error(error_cls):
    locked = default_keychain_locked()
    if locked is True:
        message = t(
            "已确认默认钥匙串锁定，暂时无法读取浏览器登录态；用密码解锁钥匙串后刷新，无需先退出网站登录",
            "The default keychain is confirmed locked; unlock it with your password and refresh. Website sign-out is not required.",
        )
    else:
        message = t(
            "无法读取浏览器登录密钥，未确认钥匙串锁定；浏览器登录可能仍有效，请检查钥匙串访问权限后刷新",
            "Browser sign-in key is unavailable; a locked keychain is not confirmed. Your browser session may still work; check keychain access and refresh.",
        )
    err = error_cls(message)
    err.keychain_locked = locked is True
    err.browser_key_unavailable = True
    err.error_code = "keychain_locked" if locked is True else "browser_key_unavailable"
    return err


class ClaudeWebError(Exception):
    pass


class CodexRemoteError(Exception):
    pass


class CodexWebError(Exception):
    pass


class CodexAuthError(CodexWebError):
    """401 / 403: unauthenticated or missing Codex access."""


class CodexSessionExpiredError(CodexAuthError):
    """The chatgpt.com browser session can no longer mint a valid access token.

    The session cookie can outlive the token it refreshes: next-auth then keeps
    returning the expired accessToken with ``error: RefreshAccessTokenError``,
    and the usage endpoint answers 401 "token is expired". That is a sign-in
    problem in the browser, not missing Codex access, so it gets its own type.
    """


class DeepSeekError(Exception):
    pass


class DeepSeekAuthError(DeepSeekError):
    pass


class GoogleQuotaError(Exception):
    pass


class GoogleQuotaAuthError(GoogleQuotaError):
    pass


class GeminiAppUsageError(Exception):
    pass


class CopilotQuotaError(Exception):
    pass


class CopilotAuthError(CopilotQuotaError):
    """No usable GitHub token, or the token has no Copilot access."""


class GrokUsageError(Exception):
    pass


class GrokAuthError(GrokUsageError):
    """No grok.com browser session, or the session was rejected."""


def clear_provider_caches() -> None:
    for path in (GEMINI_APP_USAGE_CACHE, ANTIGRAVITY_CLI_USAGE_CACHE, CODEX_WINDOW_CACHE):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass


def find_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _claude_web_context(referer: str) -> tuple[str, dict]:
    try:
        import browser_cookie3
    except ImportError as exc:
        raise ClaudeWebError(
            t(
                "未安装 browser_cookie3，请先运行: pip install browser-cookie3",
                "browser_cookie3 not installed, run: pip install browser-cookie3",
            )
        ) from exc

    cookies = []
    errs = []
    for name, loader in [("Chrome", browser_cookie3.chrome), ("Firefox", browser_cookie3.firefox)]:
        try:
            jar = loader(domain_name=".claude.ai")
            cookies = [(c.name, c.value) for c in jar]
            if cookies:
                break
        except Exception as exc:
            errs.append(f"{name}: {exc}")

    if not cookies:
        if _chrome_keychain_locked(errs):
            raise _keychain_locked_error(ClaudeWebError)
        detail = f" ({'; '.join(errs)})" if errs else ""
        raise ClaudeWebError(
            t(
                f"无法读取浏览器 cookie{detail}，请先在浏览器登录 claude.ai",
                f"cannot read browser cookies{detail}, please log in to claude.ai first",
            )
        )

    cookie_dict = dict(cookies)
    org_id = cookie_dict.get("lastActiveOrg", "")
    if not org_id:
        raise ClaudeWebError(
            t(
                "未能从 cookie 读取 org ID，请先在浏览器打开 claude.ai",
                "could not read org ID from cookie, please open claude.ai in your browser",
            )
        )

    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies)
    headers = {
        "Cookie": cookie_header,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://claude.ai",
        "Referer": referer,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }
    return org_id, headers


def _claude_web_get(path: str, headers: dict, timeout: int) -> dict:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"https://claude.ai{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise ClaudeWebError(
            f"HTTP {exc.code}: {exc.read()[:300].decode(errors='replace')}"
        ) from exc
    except Exception as exc:
        raise ClaudeWebError(str(exc)) from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ClaudeWebError(
            f"非 JSON 响应: {body[:300].decode(errors='replace')}"
        ) from exc


def live_claude_usage(timeout: int = CLAUDE_WEB_TIMEOUT_SEC) -> dict:
    org_id, headers = _claude_web_context(CLAUDE_USAGE_URL)
    return _claude_web_get(f"/api/organizations/{org_id}/usage", headers, timeout)


def live_claude_plan(timeout: int = CLAUDE_WEB_TIMEOUT_SEC) -> str | None:
    org_id, headers = _claude_web_context("https://claude.ai/settings/billing")
    data = _claude_web_get(f"/api/organizations/{org_id}", headers, timeout)
    capabilities = set(data.get("capabilities") or [])
    raven_type = data.get("raven_type")
    if raven_type == "enterprise":
        return "Enterprise"
    if raven_type == "team":
        return "Team"
    if "claude_max" in capabilities:
        return "Max"
    if "claude_pro" in capabilities:
        return "Pro"
    if "raven" in capabilities:
        return "Enterprise"
    if "chat" in capabilities:
        return "Free"
    return None


CLAUDE_PLATFORM_ORIGIN = "https://platform.claude.com"
CLAUDE_PLATFORM_BILLING_URL = f"{CLAUDE_PLATFORM_ORIGIN}/settings/billing"
CLAUDE_PLATFORM_RESPONSE_MAX_BYTES = 1024 * 1024


class ClaudeOrgBindingError(ClaudeWebError):
    """The authenticated console does not contain the explicitly bound org."""

    error_code = "account_mismatch"


def _claude_platform_headers(profile: str | None = None) -> dict | None:
    """Browser headers for platform.claude.com, or ``None`` when never signed in.

    The Claude Platform console keeps its own ``sessionKey`` on
    ``.platform.claude.com``, separate from the claude.ai one, so an account
    that only uses the chat app simply has no console session.
    """
    try:
        import browser_cookie3
    except ImportError as exc:
        raise ClaudeWebError("browser_cookie3 not installed") from exc
    errs = []
    cookies = []
    # Unpinned callers may search profiles until one carries the console
    # sessionKey. API-credit bindings filter this list to one exact label.
    cookie_files = _chromium_profile_cookie_files()
    if profile:
        # API-credit bindings name the full browser/profile label (for example
        # Chrome/Default). Never inspect another profile or Firefox for an
        # explicitly bound account, even when its session is unavailable.
        cookie_files = [(label, path) for label, path in cookie_files if label == profile]
    loaders = [
        (label, lambda path=path: browser_cookie3.chrome(cookie_file=str(path), domain_name="platform.claude.com"))
        for label, path in cookie_files
    ]
    if not profile:
        loaders.append(("Firefox", lambda: browser_cookie3.firefox(domain_name="platform.claude.com")))
    for name, loader in loaders:
        try:
            jar = [(c.name, c.value) for c in loader()]
        except Exception as exc:
            errs.append(f"{name}: {exc}")
            continue
        if any(cookie_name == "sessionKey" for cookie_name, _ in jar):
            cookies = jar
            break
    if not cookies:
        if _chrome_keychain_locked(errs):
            raise _keychain_locked_error(ClaudeWebError)
        return None
    return {
        "Cookie": "; ".join(f"{name}={value}" for name, value in cookies),
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": CLAUDE_PLATFORM_ORIGIN,
        "Referer": CLAUDE_PLATFORM_BILLING_URL,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def _claude_platform_get(path: str, headers: dict, timeout: int):
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{CLAUDE_PLATFORM_ORIGIN}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read(CLAUDE_PLATFORM_RESPONSE_MAX_BYTES + 1)
            if len(body) > CLAUDE_PLATFORM_RESPONSE_MAX_BYTES:
                raise ClaudeWebError("platform.claude.com: response exceeded the 1 MiB limit")
    except urllib.error.HTTPError as exc:
        error_body = exc.read(201)[:200].decode(errors="replace")
        raise ClaudeWebError(
            f"platform.claude.com HTTP {exc.code}: {error_body}"
        ) from exc
    except Exception as exc:
        raise ClaudeWebError(f"platform.claude.com: {exc}") from exc
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise ClaudeWebError("platform.claude.com: non-JSON response") from exc


def _claude_money(value) -> float | None:
    """Read the console's ``{"amount_minor": 20000, "exponent": 2}`` amounts."""
    if not isinstance(value, dict):
        return None
    minor = value.get("amount_minor")
    exponent = value.get("exponent", 2)
    try:
        return float(minor) / (10 ** int(exponent))
    except (TypeError, ValueError):
        return None


def _claude_balance_amount(balance) -> float | None:
    if not isinstance(balance, dict):
        return None
    for key in ("money", "credits"):
        amount = _claude_money(balance.get(key))
        if amount is not None:
            return amount
    return None


def _claude_tranche(raw: dict) -> dict:
    remaining = _claude_balance_amount(raw.get("remaining"))
    if remaining is None and raw.get("remaining_amount_minor_units") is not None:
        remaining = float(raw["remaining_amount_minor_units"]) / 100
    granted = _claude_balance_amount(raw.get("granted"))
    if granted is None and raw.get("granted_amount_minor_units") is not None:
        granted = float(raw["granted_amount_minor_units"]) / 100
    return {
        "name": raw.get("name"),
        "program_id": raw.get("program_id"),
        "remaining": remaining,
        "granted": granted,
        "expires_at": raw.get("expires_at"),
        "granted_at": raw.get("granted_at"),
    }


def _claude_console_purchased_balance(credits: dict) -> float | None:
    """Mirror Console's purchased-only figure, including tranche rounding.

    Verified against platform.claude.com chunk 0ceool7nuv-pz.js (2026-10-30).
    The API has no purchased_balance field. Console separates active paid and
    promotional tranches; it tolerates one minor unit of rounding per tranche.
    Missing/malformed inputs stay unknown, rather than becoming zero.
    """
    amount = credits.get("amount")
    paid, promo = credits.get("tranches"), credits.get("promo_tranches")
    if (type(amount) is not int or amount < 0
            or str(credits.get("currency", "")).upper() != "USD"
            or not isinstance(paid, list) or not isinstance(promo, list)):
        return None
    now = datetime.datetime.now(datetime.timezone.utc)
    groups = []
    for entries in (paid, promo):
        active = []
        for item in entries:
            if not isinstance(item, dict):
                return None
            if not isinstance(item.get("currency"), str) or not item["currency"]:
                return None
            if str(item.get("currency", "")).upper() != "USD":
                continue
            remaining = item.get("remaining_amount_minor_units")
            if type(remaining) is not int or remaining < 0:
                return None
            expiry = item.get("expires_at")
            if expiry:
                try:
                    end = datetime.datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
                    if end.tzinfo is None:
                        return None
                except ValueError:
                    return None
                if end <= now:
                    continue
            active.append(remaining)
        groups.append(active)
    paid_cents, promotional_cents = sum(groups[0]), sum(groups[1])
    if promotional_cents > 0:
        tolerance = len(groups[0]) + len(groups[1])
        figure = min(paid_cents, amount) if amount - paid_cents - promotional_cents <= tolerance else max(0, amount - promotional_cents)
    else:
        figure = amount
    return figure / 100


def _normalize_claude_api_credits(org: dict, credits: dict) -> dict:
    balance = _claude_balance_amount(credits.get("balance"))
    if balance is None and credits.get("balance_credits") is not None:
        balance = float(credits["balance_credits"])
    if balance is None and credits.get("amount") is not None:
        # Legacy shape: a bare integer amount in cents.
        balance = float(credits["amount"]) / 100
    raw_tranches = [raw for raw in (credits.get("tranches") or []) if isinstance(raw, dict)]
    raw_promo_value = credits.get("promo_tranches")
    promo_list_complete = isinstance(raw_promo_value, list) and all(
        isinstance(raw, dict) for raw in raw_promo_value
    )
    raw_promo_tranches = raw_promo_value if promo_list_complete else []
    tranches = [
        _claude_tranche(raw)
        for raw in raw_tranches + raw_promo_tranches
    ]
    promo_tranches = [_claude_tranche(raw) for raw in raw_promo_tranches]
    granted_values = [item["granted"] for item in tranches if item.get("granted") is not None]
    granted = sum(granted_values) if granted_values else None
    remaining_percent = None
    if balance is not None and granted:
        remaining_percent = max(0, min(100, int(round(balance / granted * 100))))
    return {
        "org_id": org.get("uuid"),
        "org_name": org.get("name"),
        "currency": credits.get("currency") or "USD",
        "balance": balance,
        "granted": granted,
        "remaining_percent": remaining_percent,
        "expires_at": credits.get("next_expires_at")
        or min((item["expires_at"] for item in tranches if item.get("expires_at")), default=None),
        "tranches": tranches,
        # Keep source categories separate for the additive API-credit contract.
        # An absent source field remains absent/unknown; callers must not infer
        # a purchased amount by subtracting one category from another.
        "promo_tranches": promo_tranches if promo_list_complete else None,
        "purchased_balance": _claude_balance_amount(credits.get("purchased_balance"))
        if "purchased_balance" in credits
        else _claude_console_purchased_balance(credits),
        "source_currency": credits.get("currency"),
        "source_updated_at": credits.get("source_updated_at"),
        "next_grant_at": credits.get("next_grant_at"),
        "usage_restrictions": credits.get("usage_restrictions"),
        "auto_reload": bool(credits.get("auto_reload_settings")),
        "source": "platform.claude.com",
    }


def live_claude_api_credits(
    timeout: int = CLAUDE_WEB_TIMEOUT_SEC,
    *,
    organization_id: str | None = None,
    profile: str | None = None,
) -> dict | None:
    """Return the Claude Platform API credit balance shown on settings/billing.

    An explicit organization binding is required. Returns ``None`` when the
    binding is absent or there is no console session. A configured organization
    that is not present in the authenticated account raises
    ``ClaudeOrgBindingError``; no other prepaid organization is substituted.
    """
    organization_id = str(organization_id or "").strip()
    if not organization_id:
        return None
    headers = _claude_platform_headers(profile=profile)
    if headers is None:
        return None
    orgs = _claude_platform_get("/api/organizations", headers, timeout)
    if not isinstance(orgs, list):
        raise ClaudeWebError("platform.claude.com: unexpected organizations response")
    # The same sign-in also lists the claude.ai subscription org; only the
    # API org carries a prepaid credit balance.
    api_orgs = [
        org
        for org in orgs
        if isinstance(org, dict)
        and (org.get("billing_type") == "prepaid" or "api" in (org.get("capabilities") or []))
    ]
    org = next((item for item in api_orgs if str(item.get("uuid") or "") == organization_id), None)
    if org is None:
        raise ClaudeOrgBindingError("Configured Claude API organization is not available in the authenticated account")
    from urllib.parse import quote

    credits = _claude_platform_get(
        f"/api/organizations/{quote(organization_id, safe='')}/prepaid/credits",
        headers,
        timeout,
    )
    if not isinstance(credits, dict):
        raise ClaudeWebError("platform.claude.com: unexpected credits response")
    return _normalize_claude_api_credits(org, credits)


def live_codex_rate_limits(timeout: int = REMOTE_TIMEOUT_SEC):
    if not shutil.which("codex"):
        raise CodexRemoteError("codex command not found")

    try:
        port = find_free_local_port()
        proc = subprocess.Popen(
            ["codex", "app-server", "--listen", f"ws://127.0.0.1:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except OSError as exc:
        raise CodexRemoteError(str(exc)) from exc

    try:
        _wait_codex_app_server(proc, port, timeout)
        result = _read_codex_rate_limits_ws(port, timeout)
        rate_limits = result.get("rateLimits") or {}
        if not rate_limits:
            raise CodexRemoteError("empty rate limits response")
        normalized = _normalize_remote_rate_limits(
            rate_limits,
            reset_credits=result.get("rateLimitResetCredits"),
        )
        return datetime.datetime.now(datetime.timezone.utc), normalized
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()


def _wait_codex_app_server(proc: subprocess.Popen, port: int, timeout: int):
    deadline = time.monotonic() + timeout
    lines: list[str] = []
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise CodexRemoteError("app-server exited: " + "".join(lines[-3:]).strip())
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                return
        except OSError:
            pass
        if proc.stdout:
            ready, _, _ = select.select([proc.stdout], [], [], 0)
            if ready:
                lines.append(proc.stdout.readline())
        time.sleep(0.1)
    raise CodexRemoteError("app-server start timed out")


def _optional_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
    return bool(value)


def _normalize_codex_credits(credits: dict | None) -> dict | None:
    """Normalize app-server camelCase and web snake_case credit snapshots."""
    if not isinstance(credits, dict):
        return None
    known_fields = {"hasCredits", "has_credits", "unlimited", "balance"}
    if not known_fields.intersection(credits):
        return None
    return {
        "has_credits": _optional_bool(credits.get("hasCredits", credits.get("has_credits"))),
        "unlimited": _optional_bool(credits.get("unlimited")),
        "balance": credits.get("balance"),
    }


def _normalize_rate_limit_reset_credits(summary: dict | None) -> dict | None:
    """Keep reset-credit inventory separate from the consumable balance."""
    if not isinstance(summary, dict):
        return None
    available_count = summary.get("availableCount", summary.get("available_count"))
    if available_count is None:
        return None
    try:
        available_count = int(available_count)
    except (TypeError, ValueError):
        return None
    return {"available_count": max(0, available_count)}


def _normalize_remote_rate_limits(rate_limits: dict, *, reset_credits: dict | None = None) -> dict:
    if reset_credits is None:
        reset_credits = rate_limits.get("rateLimitResetCredits") or rate_limits.get("rate_limit_reset_credits")

    def window(window_data):
        if not window_data:
            return None
        return {
            "used_percent": window_data.get("usedPercent", 0),
            "window_minutes": window_data.get("windowDurationMins"),
            "resets_at": window_data.get("resetsAt"),
        }

    return {
        "limit_id": rate_limits.get("limitId"),
        "limit_name": rate_limits.get("limitName"),
        "primary": window(rate_limits.get("primary")),
        "secondary": window(rate_limits.get("secondary")),
        "credits": _normalize_codex_credits(rate_limits.get("credits")),
        "rate_limit_reset_credits": _normalize_rate_limit_reset_credits(reset_credits),
        "plan_type": rate_limits.get("planType"),
        "rate_limit_reached_type": rate_limits.get("rateLimitReachedType"),
    }


def _read_codex_rate_limits_ws(port: int, timeout: int) -> dict:
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        _ws_handshake(sock, port)
        _ws_send_json(
            sock,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "ai-limit", "title": "ai-limit", "version": "0"},
                    "capabilities": {"experimentalApi": True, "requestAttestation": False},
                },
            },
        )
        _ws_send_json(
            sock,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "account/rateLimits/read",
                "params": None,
            },
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.1, deadline - time.monotonic()))
            msg = _ws_recv_json(sock)
            if msg.get("id") == 2:
                if "error" in msg:
                    raise CodexRemoteError(str(msg["error"]))
                return msg.get("result") or {}
    raise CodexRemoteError("rate limit response timed out")


def _ws_handshake(sock: socket.socket, port: int):
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET / HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n\r\n"
    )
    sock.sendall(req.encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            break
        resp += chunk
    if b" 101 " not in resp.split(b"\r\n", 1)[0]:
        raise CodexRemoteError("websocket handshake failed")


def _ws_send_json(sock: socket.socket, obj: dict):
    payload = json.dumps(obj, separators=(",", ":")).encode()
    key = os.urandom(4)
    size = len(payload)
    if size < 126:
        header = bytes([0x81, 0x80 | size])
    elif size < 65536:
        header = bytes([0x81, 0x80 | 126]) + struct.pack("!H", size)
    else:
        header = bytes([0x81, 0x80 | 127]) + struct.pack("!Q", size)
    masked = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    sock.sendall(header + key + masked)


def _ws_recv_json(sock: socket.socket) -> dict:
    opcode, payload = _ws_recv_frame(sock)
    if opcode == 8:
        raise CodexRemoteError("websocket closed")
    if opcode != 1:
        return {}
    return json.loads(payload.decode("utf-8"))


def _ws_recv_frame(sock: socket.socket):
    header = _recv_exact(sock, 2)
    b1, b2 = header
    size = b2 & 0x7F
    if size == 126:
        size = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif size == 127:
        size = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    key = _recv_exact(sock, 4) if (b2 & 0x80) else b""
    payload = _recv_exact(sock, size) if size else b""
    if key:
        payload = bytes(byte ^ key[i % 4] for i, byte in enumerate(payload))
    return b1 & 0x0F, payload


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise CodexRemoteError("unexpected EOF")
        data += chunk
    return data


def _load_chatgpt_cookies():
    try:
        import browser_cookie3
    except ImportError as exc:
        raise CodexWebError(
            t(
                "未安装 browser_cookie3，请先运行: pip install browser-cookie3",
                "browser_cookie3 not installed, run: pip install browser-cookie3",
            )
        ) from exc
    # Raises when no profile holds a session, so the list is never empty.
    return _chatgpt_cookie_candidates(browser_cookie3)[0][1]


def _chatgpt_session_cookie(name: str) -> bool:
    # next-auth splits a long session token into .0/.1 chunks.
    return name.startswith("__Secure-next-auth.session-token")


def _chatgpt_cookie_candidates(browser_cookie3=None) -> list[tuple[str, list]]:
    """Every browser profile that holds a chatgpt.com session, in search order.

    More than one profile can carry a session, and a stale one keeps its
    cookie long after OpenAI stops refreshing it, so callers try each in turn
    instead of trusting the first.
    """
    if browser_cookie3 is None:
        try:
            import browser_cookie3
        except ImportError as exc:
            raise CodexWebError("browser_cookie3 not installed, run: pip install browser-cookie3") from exc
    loaders = [
        (label, lambda path=path: browser_cookie3.chrome(cookie_file=str(path), domain_name=".chatgpt.com"))
        for label, path in _chromium_profile_cookie_files()
    ]
    loaders.append(("Firefox", lambda: browser_cookie3.firefox(domain_name=".chatgpt.com")))
    candidates = []
    errs = []
    for name, loader in loaders:
        try:
            cookies = [(c.name, c.value) for c in loader()]
        except Exception as exc:
            errs.append(f"{name}: {exc}")
            continue
        if any(_chatgpt_session_cookie(cookie_name) for cookie_name, _ in cookies):
            candidates.append((name, cookies))
    if candidates:
        return candidates
    if _chrome_keychain_locked(errs):
        raise _keychain_locked_error(CodexWebError)
    detail = f" ({'; '.join(errs)})" if errs else ""
    raise CodexWebError(
        t(
            f"无法读取 chatgpt.com cookie{detail}，请先在浏览器登录 chatgpt.com",
            f"cannot read chatgpt.com cookies{detail}, please log in to chatgpt.com in your browser",
        )
    )


CHATGPT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _chatgpt_headers(
    cookie_header: str,
    *,
    referer: str = "https://chatgpt.com/codex/cloud/settings/analytics",
    bearer: str | None = None,
) -> dict:
    headers = {
        "Cookie": cookie_header,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "User-Agent": CHATGPT_UA,
        "Referer": referer,
        "Origin": "https://chatgpt.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    return headers


def _chatgpt_session_expired_message() -> str:
    return t(
        "ChatGPT 登录已过期：请在 Chrome 打开 chatgpt.com 重新登录",
        "ChatGPT sign-in expired: open chatgpt.com in Chrome and sign in again",
    )


def _get_chatgpt_access_token(cookie_header: str, timeout: int) -> str:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(
        "https://chatgpt.com/api/auth/session",
        headers=_chatgpt_headers(cookie_header),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise CodexWebError(f"session HTTP {exc.code}") from exc
    except Exception as exc:
        raise CodexWebError(f"session: {exc}") from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CodexWebError("session: non-JSON response") from exc

    if data.get("error") == "RefreshAccessTokenError":
        raise CodexSessionExpiredError(_chatgpt_session_expired_message())
    token = data.get("accessToken")
    if not token:
        raise CodexWebError(
            t(
                "请先在浏览器登录 chatgpt.com",
                "please log in to chatgpt.com in your browser",
            )
        )
    return token


def _normalize_web_rate_limits(data: dict) -> dict:
    nested_rate_limits = data.get("rateLimits")
    if not isinstance(nested_rate_limits, dict):
        nested_rate_limits = {}
    def window_kind(window_minutes):
        if window_minutes is None:
            return None
        if window_minutes <= 5 * 60:
            return "5h"
        if window_minutes >= 7 * 24 * 60:
            return "weekly"
        return f"{window_minutes}m"

    def window(window_data):
        if not window_data:
            return None
        window_seconds = window_data.get("limit_window_seconds")
        window_minutes = window_seconds // 60 if window_seconds else None
        used_percent = window_data.get("used_percent", 0) or 0
        return {
            "used_percent": used_percent,
            "remaining_percent": max(0, min(100, int(round(100 - used_percent)))),
            "window_minutes": window_minutes,
            "window": window_kind(window_minutes),
            "resets_at": window_data.get("reset_at"),
            "reset_time": window_data.get("reset_at"),
        }

    def group_from_rate_limit(name: str, raw_rate_limit: dict, *, default_group: bool = False) -> dict:
        primary = window((raw_rate_limit or {}).get("primary_window"))
        secondary = window((raw_rate_limit or {}).get("secondary_window"))

        def label_for(bucket: dict | None, fallback: str) -> str:
            if not bucket:
                return fallback
            if bucket.get("window") == "5h":
                return "5 hour usage limit"
            if bucket.get("window") == "weekly":
                return "Weekly usage limit"
            return fallback

        buckets = []
        for fallback_label, bucket in (
            ("Primary usage limit", primary),
            ("Secondary usage limit", secondary),
        ):
            if not bucket:
                continue
            buckets.append(
                {
                    **bucket,
                    "display_name": label_for(bucket, fallback_label),
                    "group_display_name": name,
                    "default_group": default_group,
                }
            )
        return {
            "display_name": name,
            "default_group": default_group,
            "allowed": bool((raw_rate_limit or {}).get("allowed", True)),
            "limit_reached": bool((raw_rate_limit or {}).get("limit_reached", False)),
            "buckets": buckets,
        }

    rate_limit = data.get("rate_limit") or {}
    groups = [group_from_rate_limit("Balance", rate_limit, default_group=True)]
    for item in data.get("additional_rate_limits") or []:
        # GPT-5.3-Codex-Spark is no longer callable, but its legacy quota
        # bucket may still be returned by the usage endpoint. Do not expose
        # that stale entitlement as an available quota group.
        item_name = item.get("limit_name") or item.get("metered_feature") or ""
        if "spark" in str(item_name).strip().lower():
            continue
        groups.append(
            group_from_rate_limit(
                item_name or "Additional Codex limit",
                item.get("rate_limit") or {},
            )
        )
    buckets = [bucket for group in groups for bucket in group.get("buckets") or []]
    default_buckets = [bucket for bucket in buckets if bucket.get("default_group")]
    summary_buckets = default_buckets or buckets
    five_hour = [bucket for bucket in summary_buckets if bucket.get("window") == "5h"]
    weekly = [bucket for bucket in summary_buckets if bucket.get("window") == "weekly"]

    normalized = {
        "limit_id": None,
        "limit_name": None,
        "primary": window(rate_limit.get("primary_window")),
        "secondary": window(rate_limit.get("secondary_window")),
        "groups": groups,
        "buckets": buckets,
        "summary": {
            "5h_remaining_percent": min((bucket["remaining_percent"] for bucket in five_hour), default=None),
            "weekly_remaining_percent": min((bucket["remaining_percent"] for bucket in weekly), default=None),
            "bucket_count": len(buckets),
            "group_count": len(groups),
        },
        "credits": _normalize_codex_credits(
            data.get("credits") or nested_rate_limits.get("credits")
        ),
        "rate_limit_reset_credits": _normalize_rate_limit_reset_credits(
            data.get("rate_limit_reset_credits")
            or data.get("rateLimitResetCredits")
            or nested_rate_limits.get("rate_limit_reset_credits")
            or nested_rate_limits.get("rateLimitResetCredits")
        ),
        "plan_type": data.get("plan_type"),
        "rate_limit_reached_type": rate_limit.get("rate_limit_reached_type"),
    }
    return normalized


def live_codex_web_usage(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    import urllib.error
    import urllib.request

    candidates = _chatgpt_cookie_candidates()
    auth_error = None
    for source, cookies in candidates:
        try:
            ts, data = _codex_web_usage_with(cookies, timeout)
        except CodexAuthError as exc:
            # This profile's session is expired or lacks Codex; another
            # profile may be the one the user signed in again with.
            auth_error = auth_error or exc
            continue
        data["cookie_source"] = source
        return ts, data
    raise auth_error


def _codex_web_usage_with(cookies, timeout: int):
    import urllib.error
    import urllib.request

    cookie_header = "; ".join(f"{name}={value}" for name, value in cookies)
    token = _get_chatgpt_access_token(cookie_header, timeout)
    req = urllib.request.Request(
        "https://chatgpt.com/backend-api/codex/usage",
        headers=_chatgpt_headers(cookie_header, bearer=token),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = ""
            if "token is expired" in detail:
                raise CodexSessionExpiredError(_chatgpt_session_expired_message()) from exc
        if exc.code in (401, 403):
            raise CodexAuthError(
                t(
                    f"HTTP {exc.code}：未登录 ChatGPT 或无 Codex 权限（可能未订阅，或需重新登录）",
                    f"HTTP {exc.code}: not signed in to ChatGPT or no Codex access (subscription may be required)",
                )
            ) from exc
        raise CodexWebError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise CodexWebError(str(exc)) from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CodexWebError("non-JSON response") from exc

    return datetime.datetime.now(datetime.timezone.utc), _normalize_web_rate_limits(data)


def load_window_cache():
    try:
        return float(CODEX_WINDOW_CACHE.read_text().strip())
    except (FileNotFoundError, ValueError, OSError):
        return None


def save_window_cache(resets_at_unix):
    try:
        CODEX_WINDOW_CACHE.write_text(str(resets_at_unix))
    except OSError:
        pass


def prompt_app_server_confirm() -> bool:
    msg = t(
        "Web 查询失败，且当前窗口未激活。\n"
        "继续调用 app-server 会触发新的 Codex 5 小时冷却窗口。\n"
        "确认继续？[y/N]: ",
        "Web fetch failed and no active window cached.\n"
        "Calling app-server will trigger a new Codex 5-hour cooldown.\n"
        "Continue? [y/N]: ",
    )
    try:
        ans = input(msg).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ("y", "yes")


def _codex_default_group_bucket(rate_limits: dict, window: str):
    for group in rate_limits.get("groups") or []:
        group_name = str(group.get("display_name") or "").strip().lower()
        if not (group.get("default_group") or group_name == "balance"):
            continue
        for bucket in group.get("buckets") or []:
            if bucket.get("window") == window:
                return bucket
    for bucket in rate_limits.get("buckets") or []:
        group_name = str(bucket.get("group_display_name") or "").strip().lower()
        if group_name == "balance" and bucket.get("window") == window:
            return bucket
    return None


def _codex_remaining_from_bucket(bucket: dict | None):
    if not bucket:
        return None
    remaining = bucket.get("remaining_percent")
    if remaining is not None:
        return remaining
    if "used_percent" in bucket:
        return 100 - bucket["used_percent"]
    return None


def codex_window_remaining_percent(rate_limits: dict, window: str):
    """Return remaining percent for the default/Balance Codex quota window."""
    if not rate_limits:
        return None
    remaining = _codex_remaining_from_bucket(_codex_default_group_bucket(rate_limits, window))
    if remaining is not None:
        return remaining
    summary_key = "5h_remaining_percent" if window == "5h" else "weekly_remaining_percent"
    summary = rate_limits.get("summary") or {}
    remaining = summary.get(summary_key)
    if remaining is not None:
        return remaining
    if window == "5h":
        return None
    legacy = rate_limits.get("primary" if window == "5h" else "secondary") or {}
    if "used_percent" in legacy:
        return 100 - legacy["used_percent"]
    return None


def codex_window_reset_time(rate_limits: dict, window: str):
    if not rate_limits:
        return None
    bucket = _codex_default_group_bucket(rate_limits, window)
    if bucket:
        return bucket.get("resets_at") or bucket.get("reset_time")
    if window == "5h":
        return None
    legacy = rate_limits.get("primary" if window == "5h" else "secondary") or {}
    return legacy.get("resets_at") or legacy.get("reset_time")


def codex_5h_remaining_percent(rate_limits: dict):
    """返回 Codex 默认 Balance 5 小时窗口的剩余百分比，数据缺失时返回 None。

    优先用 web 归一化后的默认 ``Balance`` 额度组。Spark 等附加额度组可以
    在详情里单独展示，但不能冒充 CodeX 总额度。
    """
    return codex_window_remaining_percent(rate_limits, "5h")


def current_codex_rate_limits(latest_codex_rate_limits_func, *, allow_app_server_fallback=True):
    reasons = []

    try:
        ts, rate_limits = live_codex_web_usage()
        resets_at = (rate_limits.get("primary") or {}).get("resets_at")
        if resets_at:
            save_window_cache(float(resets_at))
        return ts, rate_limits, "web", None
    except CodexSessionExpiredError as exc:
        return None, None, "session_expired", str(exc)
    except CodexAuthError as exc:
        return None, None, "no_access", str(exc)
    except CodexWebError as exc:
        reasons.append(f"web: {exc}")
    except Exception as exc:
        reasons.append(f"web: {exc.__class__.__name__}: {exc}")

    cached_expiry = load_window_cache()
    now_unix = datetime.datetime.now(datetime.timezone.utc).timestamp()
    window_active = cached_expiry is not None and cached_expiry > now_unix

    if not allow_app_server_fallback:
        # 监控场景（daemon / 菜单栏）禁用 app-server 回退：它会 spawn codex
        # app-server、触发 5h 冷却副作用，且只反映本机状态会少报用量。
        allow_app_server = False
        reasons.append("app-server: monitor_disabled")
    elif window_active:
        allow_app_server = True
    elif sys.stdin.isatty() and sys.stdout.isatty():
        allow_app_server = prompt_app_server_confirm()
        if not allow_app_server:
            reasons.append("app-server: user_declined")
    else:
        allow_app_server = False
        reasons.append("app-server: non_tty_skip")

    if allow_app_server:
        try:
            ts, rate_limits = live_codex_rate_limits()
            resets_at = (rate_limits.get("primary") or {}).get("resets_at")
            if resets_at:
                save_window_cache(float(resets_at))
            return ts, rate_limits, "live", None
        except (CodexRemoteError, OSError, subprocess.SubprocessError) as exc:
            reasons.append(f"app-server: {exc or exc.__class__.__name__}")
        except Exception as exc:
            reasons.append(f"app-server: {exc.__class__.__name__}: {exc}")

    ts, rate_limits = latest_codex_rate_limits_func()
    return ts, rate_limits, "snapshot", " → ".join(reasons) if reasons else None


def load_google_oauth_creds() -> dict:
    try:
        raw = json.loads(GEMINI_OAUTH_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GoogleQuotaAuthError(
            t(
                "未找到 Gemini / Antigravity 登录态，请先登录 Google CLI",
                "Gemini / Antigravity auth not found. Please sign in to the Google CLI first",
            )
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise GoogleQuotaError(str(exc)) from exc

    access_token = str(raw.get("access_token") or "").strip()
    if not access_token:
        raise GoogleQuotaAuthError(
            t(
                "Gemini / Antigravity 登录态缺少 access token，请重新登录",
                "Gemini / Antigravity auth is missing an access token. Please sign in again",
            )
        )
    return raw


def has_google_oauth_creds() -> bool:
    try:
        load_google_oauth_creds()
    except GoogleQuotaAuthError:
        return False
    except GoogleQuotaError:
        return True
    return True


def google_oauth_token_expired(creds: dict) -> bool:
    expiry_date = creds.get("expiry_date")
    if expiry_date is None:
        return False
    try:
        expiry_ms = int(expiry_date)
    except (TypeError, ValueError):
        return False
    return int(time.time() * 1000) >= expiry_ms - 60_000


def save_google_oauth_creds(creds: dict):
    try:
        GEMINI_OAUTH_PATH.write_text(
            json.dumps(creds, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        raise GoogleQuotaError(str(exc)) from exc


def load_google_oauth_client_config() -> dict:
    gemini_path = shutil.which("gemini")
    if not gemini_path:
        raise GoogleQuotaError("gemini command not found")

    bundle_dir = pathlib.Path(gemini_path).resolve().parent
    for bundle_file in bundle_dir.glob("chunk-*.js"):
        try:
            text = bundle_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        marker = 'var OAUTH_CLIENT_ID = "'
        start = text.find(marker)
        if start < 0:
            continue
        client_id_start = start + len(marker)
        client_id_end = text.find('"', client_id_start)
        secret_marker = 'var OAUTH_CLIENT_SECRET = "'
        secret_start = text.find(secret_marker, client_id_end)
        if client_id_end < 0 or secret_start < 0:
            continue
        secret_value_start = secret_start + len(secret_marker)
        secret_value_end = text.find('"', secret_value_start)
        if secret_value_end < 0:
            continue
        return {
            "client_id": text[client_id_start:client_id_end],
            "client_secret": text[secret_value_start:secret_value_end],
        }

    raise GoogleQuotaError("could not locate Gemini OAuth client config")


def refresh_google_oauth_creds(creds: dict | None = None, timeout: int = CLAUDE_WEB_TIMEOUT_SEC) -> dict:
    import urllib.error
    import urllib.parse
    import urllib.request

    current = dict(creds or load_google_oauth_creds())
    refresh_token = str(current.get("refresh_token") or "").strip()
    if not refresh_token:
        raise GoogleQuotaAuthError(
            t(
                "Gemini / Antigravity 登录态缺少 refresh token，请重新登录",
                "Gemini / Antigravity auth is missing a refresh token. Please sign in again",
            )
        )
    client_config = load_google_oauth_client_config()

    payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_config["client_id"],
            "client_secret": client_config["client_secret"],
        }
    ).encode()
    req = urllib.request.Request(GOOGLE_OAUTH_TOKEN_URL, data=payload, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            raise GoogleQuotaAuthError(
                t(
                    f"HTTP {exc.code}：Google CLI 登录已失效，请重新登录 Antigravity / Gemini",
                    f"HTTP {exc.code}: Google CLI auth expired. Please sign in to Antigravity / Gemini again",
                )
            ) from exc
        raise GoogleQuotaError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise GoogleQuotaError(str(exc)) from exc

    try:
        refreshed = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GoogleQuotaError("non-JSON token refresh response") from exc

    access_token = str(refreshed.get("access_token") or "").strip()
    expires_in = refreshed.get("expires_in")
    if not access_token or expires_in is None:
        raise GoogleQuotaAuthError(
            t(
                "Google CLI token 刷新失败，请重新登录 Antigravity / Gemini",
                "Google CLI token refresh failed. Please sign in to Antigravity / Gemini again",
            )
        )

    updated = dict(current)
    updated["access_token"] = access_token
    updated["token_type"] = refreshed.get("token_type") or current.get("token_type") or "Bearer"
    updated["expiry_date"] = int(time.time() * 1000) + int(expires_in) * 1000
    if refreshed.get("id_token"):
        updated["id_token"] = refreshed["id_token"]
    save_google_oauth_creds(updated)
    return updated


def load_google_project_id() -> str:
    default_project = "default-cli-project"
    try:
        raw = json.loads(GEMINI_PROJECT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_project
    project_id = str(raw.get("id") or "").strip()
    return project_id or default_project


def _google_bucket_priority(bucket: dict) -> tuple[int, float, str]:
    model_id = bucket.get("model_id") or ""
    try:
        remaining = float(bucket.get("remaining_fraction"))
    except (TypeError, ValueError):
        remaining = -1.0
    try:
        rank = GOOGLE_MODEL_PRIORITY.index(model_id)
    except ValueError:
        rank = len(GOOGLE_MODEL_PRIORITY)
    return rank, -remaining, model_id


def _iso_local(dt: datetime.datetime) -> str:
    return dt.astimezone().isoformat()


def _parse_antigravity_duration(value: str) -> datetime.timedelta | None:
    match = re.fullmatch(r"(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?", value or "")
    if not match or not any(match.groupdict().values()):
        return None
    return datetime.timedelta(
        hours=int(match.group("h") or 0),
        minutes=int(match.group("m") or 0),
        seconds=int(match.group("s") or 0),
    )


def _antigravity_log_year(path: pathlib.Path) -> int:
    match = ANTIGRAVITY_LOG_FILE_RE.search(path.name)
    if match:
        try:
            return int(match.group("year"))
        except ValueError:
            pass
    return datetime.datetime.now().year


def _parse_antigravity_log_time(line: str, path: pathlib.Path, tzinfo) -> datetime.datetime | None:
    match = ANTIGRAVITY_LOG_TIME_RE.search(line)
    if not match:
        return None
    try:
        micro = match.group("micro")[:6].ljust(6, "0")
        return datetime.datetime(
            _antigravity_log_year(path),
            int(match.group("month")),
            int(match.group("day")),
            *[int(part) for part in match.group("time").split(":")],
            int(micro),
            tzinfo=tzinfo,
        )
    except ValueError:
        return None


def latest_antigravity_quota_limit() -> dict | None:
    """Return the newest still-active Antigravity CLI quota block found in agy logs."""
    try:
        log_paths = sorted(
            ANTIGRAVITY_LOG_DIR.glob("cli-*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return None

    now = datetime.datetime.now().astimezone()
    latest = None
    for path in log_paths[:60]:
        try:
            if now.timestamp() - path.stat().st_mtime > ANTIGRAVITY_QUOTA_LOG_MAX_AGE_SEC:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue

        current_model = None
        for line in lines:
            model_match = ANTIGRAVITY_MODEL_LABEL_RE.search(line)
            if model_match:
                current_model = model_match.group(1)

            quota_match = ANTIGRAVITY_QUOTA_RE.search(line)
            if not quota_match:
                continue
            event_time = _parse_antigravity_log_time(line, path, now.tzinfo)
            reset_delta = _parse_antigravity_duration(quota_match.group("duration"))
            if event_time is None or reset_delta is None:
                continue
            reset_time = event_time + reset_delta
            if reset_time <= now:
                continue
            if latest and event_time <= latest["_event_dt"]:
                continue
            latest = {
                "_event_dt": event_time,
                "limited": True,
                "source": "antigravity-cli-log",
                "model_label": current_model,
                "reset_in": quota_match.group("duration"),
                "reset_time": _iso_local(reset_time),
                "event_time": _iso_local(event_time),
                "log_path": str(path),
            }

    if not latest:
        return None
    latest.pop("_event_dt", None)
    return latest


def _antigravity_devtools_port() -> int | None:
    try:
        raw = (pathlib.Path.home() / "Library" / "Application Support" / "Antigravity" / "DevToolsActivePort").read_text(
            encoding="utf-8"
        )
    except OSError:
        return None
    first = raw.splitlines()[0].strip() if raw.splitlines() else ""
    try:
        return int(first)
    except ValueError:
        return None


def _antigravity_sidecar_origin(timeout: int) -> str | None:
    import urllib.error
    import urllib.request

    port = _antigravity_devtools_port()
    if not port:
        return None
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as response:
            targets = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    for target in targets:
        url = str(target.get("url") or "")
        title = str(target.get("title") or "")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "https" and parsed.hostname in ("127.0.0.1", "localhost") and parsed.port:
            if "/sidecars" in url or title.lower() == "antigravity":
                return f"https://127.0.0.1:{parsed.port}"
    for target in targets:
        url = str(target.get("url") or "")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme == "https" and parsed.hostname in ("127.0.0.1", "localhost") and parsed.port:
            return f"https://127.0.0.1:{parsed.port}"
    return None


def _antigravity_csrf_token() -> str | None:
    try:
        result = subprocess.run(
            ["ps", "-ax", "-o", "args="],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    for line in result.stdout.splitlines():
        if "language_server" not in line or "--override_ide_name antigravity" not in line:
            continue
        match = ANTIGRAVITY_CSRF_RE.search(line)
        if match:
            return match.group(1)
    return None


def _grpc_web_json_body(payload: dict) -> bytes:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return b"\x00" + len(raw).to_bytes(4, "big") + raw


def _parse_grpc_web_json(raw: bytes) -> dict:
    offset = 0
    while offset + 5 <= len(raw):
        frame_type = raw[offset]
        frame_len = int.from_bytes(raw[offset + 1 : offset + 5], "big")
        offset += 5
        frame = raw[offset : offset + frame_len]
        offset += frame_len
        if frame_type == 0:
            return json.loads(frame)
    raise GoogleQuotaError("empty grpc-web quota response")


def _antigravity_quota_bucket(raw_bucket: dict) -> dict:
    remaining_fraction = raw_bucket.get("remainingFraction")
    try:
        remaining_fraction = float(remaining_fraction) if remaining_fraction is not None else None
    except (TypeError, ValueError):
        remaining_fraction = None
    remaining_percent = None
    if remaining_fraction is not None:
        remaining_percent = max(0, min(100, int(round(remaining_fraction * 100))))
    return {
        "bucket_id": raw_bucket.get("bucketId"),
        "display_name": raw_bucket.get("displayName"),
        "description": raw_bucket.get("description"),
        "window": raw_bucket.get("window"),
        "remaining_fraction": remaining_fraction,
        "remaining_percent": remaining_percent,
        "reset_time": raw_bucket.get("resetTime"),
        "disabled": bool(raw_bucket.get("disabled")),
    }


def _antigravity_limit_from_quota(normalized: dict) -> dict | None:
    """Derive the "quota exhausted" state from quota numbers instead of logs.

    The log line that used to name the exhausted model and its reset countdown
    disappeared in Antigravity CLI 1.1.9 (see ANTIGRAVITY_QUOTA_RE), so the
    banner is driven by the bucket that actually reports 0% remaining. That is
    also the more authoritative reset moment: it comes from the quota service
    rather than from a countdown printed when the 429 happened.

    Returns ``None`` when nothing is exhausted, matching the log-based helper.
    """
    exhausted = [
        bucket
        for bucket in normalized.get("buckets") or []
        if not bucket.get("disabled") and bucket.get("remaining_percent") == 0
    ]
    if not exhausted:
        return None
    # Prefer the window that frees up first, and keep buckets with a known
    # reset time ahead of ones without.
    bucket = min(
        exhausted,
        key=lambda item: (item.get("reset_time") is None, item.get("reset_time") or ""),
    )
    label = " / ".join(
        part
        for part in (bucket.get("group_display_name"), bucket.get("display_name"))
        if part
    )
    return {
        "limited": True,
        "source": normalized.get("source") or "antigravity quota",
        "model_label": label or None,
        "reset_in": None,
        "reset_time": bucket.get("reset_time"),
        "bucket_id": bucket.get("bucket_id"),
    }


def _normalize_antigravity_quota_summary(data: dict, source: str = "Antigravity app RetrieveUserQuotaSummary") -> dict:
    response = data.get("response") or data
    groups = []
    flat_buckets = []
    for raw_group in response.get("groups") or []:
        buckets = [_antigravity_quota_bucket(bucket) for bucket in raw_group.get("buckets") or []]
        group = {
            "display_name": raw_group.get("displayName"),
            "description": raw_group.get("description"),
            "buckets": buckets,
        }
        groups.append(group)
        for bucket in buckets:
            flat_buckets.append({**bucket, "group_display_name": group["display_name"]})

    active_buckets = [bucket for bucket in flat_buckets if not bucket.get("disabled")]
    percent_values = [bucket["remaining_percent"] for bucket in active_buckets if bucket.get("remaining_percent") is not None]
    primary = None
    if active_buckets:
        primary = min(
            active_buckets,
            key=lambda bucket: (
                101 if bucket.get("remaining_percent") is None else bucket.get("remaining_percent"),
                bucket.get("group_display_name") or "",
                bucket.get("display_name") or "",
            ),
        )
    normalized = {
        "source": source,
        "quota_groups": groups,
        "primary": primary,
        "buckets": flat_buckets,
        "summary": {
            "remaining_percent": min(percent_values) if percent_values else None,
            "reset_time": (primary or {}).get("reset_time"),
            "bucket_count": len(flat_buckets),
            "group_count": len(groups),
            "quota_state": "live",
            "description": response.get("description"),
        },
    }
    # Both the app sidecar and the `agy /usage` fallback funnel through here, so
    # attaching the banner once covers every current Antigravity source.
    normalized["antigravity"] = _antigravity_limit_from_quota(normalized)
    return normalized


def live_antigravity_quota_summary(timeout: int = 8) -> dict:
    import urllib.error
    import urllib.request

    origin = _antigravity_sidecar_origin(timeout)
    csrf_token = _antigravity_csrf_token()
    if not origin or not csrf_token:
        raise GoogleQuotaError("Antigravity app quota endpoint unavailable")
    req = urllib.request.Request(
        f"{origin}/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary",
        data=_grpc_web_json_body({"forceRefresh": True}),
        headers={
            "Content-Type": "application/grpc-web+json",
            "Accept": "application/grpc-web+json",
            "x-grpc-web": "1",
            "x-codeium-csrf-token": csrf_token,
            "x-user-agent": "CONNECT_ES_USER_AGENT",
            "Referer": f"{origin}/sidecars?settingsOpen=true&settingsScreen=Models",
            "User-Agent": f"ai-limit/{__version__} Antigravity",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(
            req,
            timeout=timeout,
            context=ssl._create_unverified_context(),
        ) as response:
            body = response.read()
    except Exception as exc:
        raise GoogleQuotaError(str(exc)) from exc
    return _normalize_antigravity_quota_summary(_parse_grpc_web_json(body))


def _load_antigravity_cli_usage_cache() -> dict | None:
    try:
        raw = json.loads(ANTIGRAVITY_CLI_USAGE_CACHE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    cached_at = raw.get("cached_at")
    try:
        age = time.time() - float(cached_at)
    except (TypeError, ValueError):
        return None
    if age < 0 or age > ANTIGRAVITY_CLI_USAGE_CACHE_TTL_SEC:
        return None
    data = raw.get("data")
    return data if isinstance(data, dict) else None


def _save_antigravity_cli_usage_cache(data: dict):
    try:
        ANTIGRAVITY_CLI_USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        ANTIGRAVITY_CLI_USAGE_CACHE.write_text(
            json.dumps({"cached_at": time.time(), "data": data}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def _strip_terminal_control(text: str) -> str:
    text = ANSI_RE.sub("", text)
    text = text.replace("\r", "\n")
    return "".join(ch for ch in text if ch == "\n" or ch == "\t" or ord(ch) >= 32)


def _duration_text_to_reset_time(value: str, now: datetime.datetime) -> str | None:
    hours = minutes = seconds = 0
    for amount, unit in re.findall(r"(\d+)\s*([hms])", value or ""):
        if unit == "h":
            hours += int(amount)
        elif unit == "m":
            minutes += int(amount)
        elif unit == "s":
            seconds += int(amount)
    if hours == minutes == seconds == 0:
        return None
    return _iso_local(now + datetime.timedelta(hours=hours, minutes=minutes, seconds=seconds))


def _parse_antigravity_cli_usage_text(text: str) -> dict:
    clean = _strip_terminal_control(text)
    upper = clean.upper()
    if "MODELS & QUOTA" not in upper:
        raise GoogleQuotaError("agy /usage output did not contain quota data")

    group_defs = [
        ("Gemini Models", "GEMINI MODELS"),
        ("Claude and GPT models", "CLAUDE AND GPT MODELS"),
    ]
    groups = []
    now = datetime.datetime.now().astimezone()
    for index, (display_name, marker) in enumerate(group_defs):
        start = upper.find(marker)
        if start < 0:
            continue
        next_starts = [upper.find(next_marker, start + len(marker)) for _, next_marker in group_defs[index + 1 :]]
        next_starts = [pos for pos in next_starts if pos >= 0]
        end = min(next_starts) if next_starts else len(clean)
        section = clean[start:end]
        lines = [line.strip() for line in section.splitlines() if line.strip()]
        description = next((line for line in lines if line.startswith("Models within this group:")), None)
        buckets = []
        for bucket_name, window in (("Weekly Limit", "weekly"), ("Five Hour Limit", "5h")):
            bucket_start = section.rfind(bucket_name)
            if bucket_start < 0:
                continue
            next_bucket_positions = [
                section.find(other, bucket_start + len(bucket_name))
                for other in ("Weekly Limit", "Five Hour Limit")
                if other != bucket_name
            ]
            next_bucket_positions = [pos for pos in next_bucket_positions if pos >= 0]
            bucket_end = min(next_bucket_positions) if next_bucket_positions else len(section)
            bucket_text = section[bucket_start:bucket_end]
            percent_match = re.search(r"(\d+(?:\.\d+)?)%", bucket_text)
            remaining_percent = int(round(float(percent_match.group(1)))) if percent_match else None
            duration_match = re.search(r"Refreshes in\s+((?:\d+\s*h)?\s*(?:\d+\s*m)?\s*(?:\d+\s*s)?)", bucket_text)
            disabled = "Disabled:" in bucket_text
            buckets.append(
                {
                    "bucket_id": f"{display_name.lower().replace(' ', '-').replace('&', 'and')}-{window}",
                    "display_name": bucket_name,
                    "description": " ".join(bucket_text.split()),
                    "window": window,
                    "remaining_fraction": remaining_percent / 100 if remaining_percent is not None else None,
                    "remaining_percent": remaining_percent,
                    "reset_time": _duration_text_to_reset_time(duration_match.group(1), now) if duration_match else None,
                    "disabled": disabled,
                }
            )
        groups.append({"display_name": display_name, "description": description, "buckets": buckets})

    if not groups:
        raise GoogleQuotaError("agy /usage output did not include known quota groups")
    return _normalize_antigravity_quota_summary(
        {
            "response": {
                "groups": [
                    {
                        "displayName": group["display_name"],
                        "description": group.get("description"),
                        "buckets": [
                            {
                                "bucketId": bucket.get("bucket_id"),
                                "displayName": bucket.get("display_name"),
                                "description": bucket.get("description"),
                                "window": bucket.get("window"),
                                "remainingFraction": bucket.get("remaining_fraction"),
                                "resetTime": bucket.get("reset_time"),
                                "disabled": bucket.get("disabled"),
                            }
                            for bucket in group.get("buckets") or []
                        ],
                    }
                    for group in groups
                ],
                "description": "Parsed from Antigravity CLI /usage.",
            }
        },
        source="agy /usage fallback",
    )


def _antigravity_cli_usage_complete(text: str) -> bool:
    """Return whether the TUI transcript contains every expected quota window.

    A fully available (100%) window is rendered as ``Quota available`` without
    a ``Refreshes in`` line.  Counting refresh labels therefore rejects valid,
    complete screens and makes the driver wait until it times out.  Reuse the
    parser as the completion check so the driver and the accepted payload stay
    in agreement as the TUI wording changes.
    """
    try:
        data = _parse_antigravity_cli_usage_text(text)
    except GoogleQuotaError:
        return False
    groups = data.get("quota_groups") or []
    if len(groups) != 2:
        return False
    return all(
        {bucket.get("window") for bucket in group.get("buckets") or []} == {"weekly", "5h"}
        and all(bucket.get("remaining_percent") is not None for bucket in group.get("buckets") or [])
        for group in groups
    )


def _run_antigravity_cli_usage_text(timeout: int, trace: dict | None = None) -> str:
    """Drive the agy TUI to its /usage screen and return the raw pty transcript.

    ``trace`` is filled in with how far the drive got. A timeout is not an error
    here — the loop just returns what it captured — so without the trace a stuck
    TUI is indistinguishable from a healthy one that produced odd output.
    """
    import fcntl
    import pty
    import termios

    if trace is None:
        trace = {}
    trace.update({"stage": "start", "elapsed_sec": 0.0, "timed_out": False})
    started = time.monotonic()

    agy_path = shutil.which("agy")
    if not agy_path:
        trace["stage"] = "agy-not-found"
        raise GoogleQuotaError("agy command not found")
    master_fd, slave_fd = pty.openpty()
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))
    proc = subprocess.Popen(
        [agy_path],
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=str(pathlib.Path.home()),
        env={**os.environ, "TERM": os.environ.get("TERM") or "xterm-256color"},
        close_fds=True,
    )
    os.close(slave_fd)
    flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
    fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    chunks: list[bytes] = []
    sent_trust = False
    sent_usage = False
    page_down_count = 0
    last_page_down = 0.0
    sent_exit = False
    trace["stage"] = "spawned"
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master_fd], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master_fd, 8192)
                except BlockingIOError:
                    chunk = b""
                except OSError:
                    trace["stage"] = "pty-read-error"
                    break
                if not chunk:
                    trace["stage"] = "pty-eof"
                    break
                chunks.append(chunk)
            text = _strip_terminal_control(b"".join(chunks).decode("utf-8", errors="replace"))
            if "Do you trust the contents of this project?" in text and not sent_trust:
                os.write(master_fd, b"\r")
                sent_trust = True
                trace["stage"] = "sent-trust"
                continue
            if (
                "Antigravity CLI" in text
                and "Do you trust the contents of this project?" not in text
                and not sent_usage
                and ANTIGRAVITY_TUI_PROMPT_RE.search(text)
            ):
                os.write(master_fd, b"/usage\r")
                sent_usage = True
                trace["stage"] = "sent-usage"
                continue
            # Wait for real body content, not just the header: paging the moment
            # "Models & Quota" paints can scroll past the first group before it
            # ever renders, and a group that never renders never enters the
            # transcript, so the exit condition can no longer be met.
            if (
                "Models & Quota" in text
                and "Limit Remaining" in text
                and page_down_count < 2
                and time.monotonic() - last_page_down > 0.6
            ):
                os.write(master_fd, b"\x1b[6~")
                page_down_count += 1
                last_page_down = time.monotonic()
                trace["stage"] = f"paged-{page_down_count}"
                continue
            if not sent_exit and _antigravity_cli_usage_complete(text):
                os.write(master_fd, b"\x1b/exit\r")
                sent_exit = True
                trace["stage"] = "sent-exit"
                time.sleep(0.3)
                break
        else:
            trace["timed_out"] = True
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
        trace.update(
            {
                "elapsed_sec": round(time.monotonic() - started, 2),
                "sent_trust": sent_trust,
                "sent_usage": sent_usage,
                "page_down_count": page_down_count,
                "sent_exit": sent_exit,
                "captured_bytes": sum(len(chunk) for chunk in chunks),
                "agy_path": agy_path,
                "exit_code": proc.poll(),
            }
        )
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


def _dump_antigravity_cli_debug(text: str, trace: dict, reason: str) -> None:
    """Persist the transcript of a failed agy drive. Never raises."""
    try:
        clean = _strip_terminal_control(text)
        markers = {
            marker: clean.count(marker)
            for marker in (
                "Do you trust the contents of this project?",
                "Antigravity CLI",
                "Models & Quota",
                "GEMINI MODELS",
                "CLAUDE AND GPT MODELS",
                "Five Hour Limit",
                "Refreshes in",
            )
        }
        header = [
            f"=== {datetime.datetime.now().astimezone().isoformat()} pid={os.getpid()}",
            f"reason: {reason}",
            f"trace: {json.dumps(trace, default=str, ensure_ascii=False)}",
            f"markers: {json.dumps(markers, ensure_ascii=False)}",
            f"prompt_regex_hit: {bool(ANTIGRAVITY_TUI_PROMPT_RE.search(clean))}",
            "--- transcript (control sequences stripped) ---",
        ]
        entry = "\n".join(header) + "\n" + clean + "\n"

        ANTIGRAVITY_CLI_DEBUG_PATH.parent.mkdir(parents=True, exist_ok=True)
        previous = ""
        if ANTIGRAVITY_CLI_DEBUG_PATH.exists():
            previous = ANTIGRAVITY_CLI_DEBUG_PATH.read_text(encoding="utf-8", errors="replace")
        entries = [block for block in previous.split("\n=== ") if block.strip()]
        # split() eats the delimiter on every entry but the first; put it back.
        entries = [entries[0]] + ["=== " + block for block in entries[1:]] if entries else []
        entries.append(entry)
        ANTIGRAVITY_CLI_DEBUG_PATH.write_text(
            "\n".join(entries[-ANTIGRAVITY_CLI_DEBUG_KEEP:]), encoding="utf-8"
        )
    except Exception:  # noqa: BLE001 - diagnostics must never break a quota read
        pass


def live_antigravity_cli_usage(timeout: int = ANTIGRAVITY_CLI_USAGE_TIMEOUT_SEC) -> dict:
    cached = _load_antigravity_cli_usage_cache()
    if cached:
        return cached
    trace: dict = {}
    text = _run_antigravity_cli_usage_text(timeout, trace=trace)
    try:
        data = _parse_antigravity_cli_usage_text(text)
    except GoogleQuotaError as exc:
        _dump_antigravity_cli_debug(text, trace, str(exc))
        raise GoogleQuotaError(f"{exc} (stage={trace.get('stage')}, timed_out={trace.get('timed_out')})") from exc
    if trace.get("timed_out"):
        # A complete screen would have satisfied the exit condition and left via
        # /exit, so a timeout means the transcript is partial — and the parser
        # only insists on the "Models & Quota" header, so it happily returns
        # whichever groups did render. Caching that reports one group's numbers
        # as if they were the whole account, which is worse than no data at all.
        groups = [group.get("display_name") for group in data.get("quota_groups") or []]
        _dump_antigravity_cli_debug(text, trace, f"timed out; partial parse gave groups={groups}")
        raise GoogleQuotaError(
            f"agy /usage timed out with a partial screen (stage={trace.get('stage')}, groups={groups})"
        )
    _save_antigravity_cli_usage_cache(data)
    return data


def _normalize_google_quota(data: dict) -> dict:
    buckets = []
    for raw_bucket in data.get("buckets") or []:
        model_id = raw_bucket.get("modelId")
        remaining_amount = raw_bucket.get("remainingAmount")
        remaining_fraction = raw_bucket.get("remainingFraction")
        try:
            remaining_amount = int(remaining_amount) if remaining_amount is not None else None
        except (TypeError, ValueError):
            remaining_amount = None
        try:
            remaining_fraction = float(remaining_fraction) if remaining_fraction is not None else None
        except (TypeError, ValueError):
            remaining_fraction = None
        remaining_percent = None
        if remaining_fraction is not None:
            remaining_percent = max(0, min(100, int(round(remaining_fraction * 100))))
        buckets.append(
            {
                "model_id": model_id,
                "remaining_amount": remaining_amount,
                "remaining_fraction": remaining_fraction,
                "remaining_percent": remaining_percent,
                "reset_time": raw_bucket.get("resetTime"),
            }
        )

    buckets.sort(key=_google_bucket_priority)
    primary = buckets[0] if buckets else None
    percent_values = [bucket["remaining_percent"] for bucket in buckets if bucket.get("remaining_percent") is not None]
    summary_percent = min(percent_values) if percent_values else (primary or {}).get("remaining_percent")
    reset_times = sorted({bucket.get("reset_time") for bucket in buckets if bucket.get("reset_time")})

    return {
        "source": "cloudcode-pa.googleapis.com v1internal:retrieveUserQuota",
        "primary": primary,
        "buckets": buckets,
        "summary": {
            "remaining_percent": summary_percent,
            "reset_time": reset_times[0] if reset_times else (primary or {}).get("reset_time"),
            "bucket_count": len(buckets),
        },
    }


def _unavailable_antigravity_quota_groups() -> tuple[list[dict], list[dict]]:
    groups = []
    flat_buckets = []
    for display_name, description in (
        ("Gemini Models", "Models within this group: Gemini Flash, Gemini Pro"),
        ("Claude and GPT models", "Models within this group: Claude Opus, Claude Sonnet, GPT-OSS"),
    ):
        buckets = []
        for bucket_name, window in (("Weekly Limit", "weekly"), ("Five Hour Limit", "5h")):
            bucket = {
                "bucket_id": f"{display_name.lower().replace(' ', '-').replace('&', 'and')}-{window}",
                "display_name": bucket_name,
                "description": "Quota data unavailable",
                "window": window,
                "remaining_fraction": None,
                "remaining_percent": None,
                "reset_time": None,
                "disabled": False,
                "quota_state": "unavailable",
            }
            buckets.append(bucket)
            flat_buckets.append({**bucket, "group_display_name": display_name})
        groups.append({"display_name": display_name, "description": description, "buckets": buckets})
    return groups, flat_buckets


def _with_antigravity_view(
    normalized: dict,
    limit: dict | None,
    degraded_reasons: list[str] | None = None,
) -> dict:
    quota_groups, buckets = _unavailable_antigravity_quota_groups()
    primary = buckets[0]

    summary = dict(normalized.get("summary") or {})
    if limit:
        summary.update(
            {
                "remaining_percent": 0,
                "reset_time": limit.get("reset_time") or summary.get("reset_time"),
                "bucket_count": len(buckets),
                "group_count": len(quota_groups),
                "quota_state": "limited",
            }
        )
    else:
        # Nothing here carries a number: the buckets are just the model list.
        # Say the data is unavailable rather than implying the quota is unknown.
        summary.update(
            {
                "remaining_percent": None,
                "reset_time": None,
                "bucket_count": len(buckets),
                "group_count": len(quota_groups),
                "quota_state": "unavailable",
                "unavailable_reason": "; ".join(degraded_reasons or []) or "no live quota source responded",
            }
        )
    return {
        **normalized,
        "source": "antigravity quota groups unavailable",
        "degraded_reasons": list(degraded_reasons or []),
        "primary": primary,
        "buckets": buckets,
        "quota_groups": quota_groups,
        "legacy_google_buckets": normalized.get("buckets") or [],
        "legacy_google_source": normalized.get("source"),
        "summary": summary,
        # Pre-1.1.9 logs still give the richer signal (which model, countdown at
        # 429 time), so keep them first and fall back to the quota numbers.
        "antigravity": limit or _antigravity_limit_from_quota(normalized),
    }


def live_google_quota(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    import urllib.error
    import urllib.request

    # Both real quota sources are best-effort. Remember why each one dropped out
    # so the numberless last-resort view can say so instead of looking like a
    # successful read with unknown numbers.
    degraded_reasons: list[str] = []
    try:
        return datetime.datetime.now(datetime.timezone.utc), live_antigravity_quota_summary(timeout=min(timeout, 8))
    except GoogleQuotaError as exc:
        degraded_reasons.append(f"antigravity app: {exc}")
    try:
        return datetime.datetime.now(datetime.timezone.utc), live_antigravity_cli_usage()
    except GoogleQuotaError as exc:
        degraded_reasons.append(f"agy /usage: {exc}")

    def fetch(creds: dict) -> bytes:
        req = urllib.request.Request(
            GOOGLE_QUOTA_URL,
            data=json.dumps({"project": load_google_project_id()}).encode(),
            headers={
                "Authorization": f"Bearer {creds['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": f"ai-limit/{__version__}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.read()

    creds = load_google_oauth_creds()
    if google_oauth_token_expired(creds):
        creds = refresh_google_oauth_creds(creds, timeout=timeout)

    try:
        body = fetch(creds)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            try:
                creds = refresh_google_oauth_creds(creds, timeout=timeout)
                body = fetch(creds)
            except urllib.error.HTTPError as retry_exc:
                if retry_exc.code in (401, 403):
                    raise GoogleQuotaAuthError(
                        t(
                            f"HTTP {retry_exc.code}：Google CLI 登录已失效，请重新登录 Antigravity / Gemini",
                            f"HTTP {retry_exc.code}: Google CLI auth expired. Please sign in to Antigravity / Gemini again",
                        )
                    ) from retry_exc
                raise GoogleQuotaError(f"HTTP {retry_exc.code}") from retry_exc
        else:
            raise GoogleQuotaError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise GoogleQuotaError(str(exc)) from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GoogleQuotaError("non-JSON response") from exc

    normalized = _normalize_google_quota(data)
    normalized = _with_antigravity_view(
        normalized,
        latest_antigravity_quota_limit(),
        degraded_reasons,
    )
    return datetime.datetime.now(datetime.timezone.utc), normalized


def _gemini_app_cookie_context(timeout: int):
    try:
        import browser_cookie3
    except ImportError as exc:
        raise GeminiAppUsageError(
            t(
                "未安装 browser_cookie3，无法读取 Chrome 中的 Gemini 登录态",
                "browser_cookie3 not installed; cannot read Gemini Chrome cookies",
            )
        ) from exc

    import urllib.request

    try:
        jar = browser_cookie3.chrome(domain_name=".google.com")
    except Exception as exc:
        if _chrome_keychain_locked([exc]):
            raise _keychain_locked_error(GeminiAppUsageError) from exc
        raise GeminiAppUsageError(f"cannot read Chrome cookies: {exc}") from exc

    if not any(cookie.name in {"SID", "__Secure-1PSID", "__Secure-3PSID"} for cookie in jar):
        raise GeminiAppUsageError(
            t(
                "未找到 Google 登录 cookie，请先在 Chrome 打开 gemini.google.com/usage 并登录",
                "Google login cookies not found; open gemini.google.com/usage in Chrome first",
            )
        )

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    req = urllib.request.Request(GEMINI_APP_USAGE_URL, headers=headers)
    with opener.open(req, timeout=timeout) as response:
        html = response.read().decode("utf-8", "replace")
        final_url = response.geturl()
    return opener, html, final_url


def has_gemini_app_cookies() -> bool:
    try:
        import browser_cookie3

        jar = browser_cookie3.chrome(domain_name=".google.com")
        return any(cookie.name in {"SID", "__Secure-1PSID", "__Secure-3PSID"} for cookie in jar)
    except Exception:
        return False


def _gemini_app_page_param(html: str, key: str) -> str | None:
    match = re.search(rf'"{re.escape(key)}":"([^"]+)"', html)
    if match:
        return match.group(1)
    return None


def _parse_batchexecute_payload(raw: str) -> list:
    payloads = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.isdigit() or line.startswith(")]}'"):
            continue
        try:
            chunk = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(chunk, list):
            continue
        for row in chunk:
            if isinstance(row, list) and len(row) >= 3 and row[0] == "wrb.fr":
                value = row[2]
                if isinstance(value, str):
                    try:
                        payloads.append(json.loads(value))
                    except json.JSONDecodeError:
                        payloads.append(value)
                elif value is not None:
                    payloads.append(value)
    return payloads


def _gemini_app_batchexecute(opener, html: str, rpc: str, arg, timeout: int):
    import urllib.request

    bl = _gemini_app_page_param(html, "cfb2h")
    fsid = _gemini_app_page_param(html, "FdrFJe")
    at = _gemini_app_page_param(html, "SNlM0e")
    if not bl or not fsid or not at:
        raise GeminiAppUsageError("Gemini page did not expose batchexecute tokens")

    query = urllib.parse.urlencode(
        {
            "rpcids": rpc,
            "source-path": "/usage",
            "bl": bl,
            "f.sid": fsid,
            "hl": "en-US",
            "_reqid": int(time.time() * 1000) % 1_000_000,
            "rt": "c",
        }
    )
    body = urllib.parse.urlencode(
        {
            "f.req": json.dumps(
                [[[rpc, json.dumps(arg, separators=(",", ":")), None, "generic"]]],
                separators=(",", ":"),
            ),
            "at": at,
        }
    ) + "&"
    req = urllib.request.Request(
        f"{GEMINI_APP_BATCHEXECUTE_URL}?{query}",
        data=body.encode(),
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
            ),
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin": "https://gemini.google.com",
            "Referer": GEMINI_APP_USAGE_URL,
            "X-Same-Domain": "1",
        },
        method="POST",
    )
    with opener.open(req, timeout=timeout) as response:
        return _parse_batchexecute_payload(response.read().decode("utf-8", "replace"))


def _walk_json(value):
    yield value
    if isinstance(value, list):
        for item in value:
            yield from _walk_json(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_json(item)


def _extract_gemini_app_models(payloads: list) -> list[dict]:
    models = []
    seen = set()
    for value in _walk_json(payloads):
        if not isinstance(value, list) or len(value) < 16:
            continue
        model_id, label, description = value[0], value[1], value[2]
        if not (isinstance(model_id, str) and isinstance(label, str)):
            continue
        display = None
        for candidate in (value[15], value[10] if len(value) > 10 else None, label):
            if isinstance(candidate, str) and candidate.strip():
                display = candidate.strip()
                break
        if not display or model_id in seen:
            continue
        if not re.search(r"(gemini|flash|pro|thinking|lite|veo|imagen)", " ".join([display, label, str(description)]), re.I):
            continue
        seen.add(model_id)
        models.append(
            {
                "model_id": model_id,
                "display_name": display,
                "label": label,
                "description": description if isinstance(description, str) else None,
            }
        )
    return models


def _extract_gemini_app_quota_buckets(payloads: list) -> list[dict]:
    buckets = []
    seen = set()
    for value in _walk_json(payloads):
        if not isinstance(value, dict):
            continue
        quota = value.get("quotaInfo") or value.get("quota") or value.get("usage")
        if not isinstance(quota, dict):
            continue
        reset = quota.get("Cfa") or quota.get("resetTime") or quota.get("reset_time")
        remaining = quota.get("remainingPercent") or quota.get("remaining_percent") or quota.get("L7c")
        limit = quota.get("limit") or quota.get("Hna")
        key = json.dumps([reset, remaining, limit], sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        try:
            remaining = int(round(float(remaining)))
        except Exception:
            remaining = None
        buckets.append(
            {
                "display_name": value.get("displayName") or value.get("name") or "Gemini App quota",
                "remaining_percent": remaining,
                "reset_time": reset,
                "raw_limit": limit,
            }
        )
    return buckets


def _gemini_epoch_seconds(value):
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, list) and value:
        return _gemini_epoch_seconds(value[0])
    return None


def _gemini_epoch_iso(value):
    seconds = _gemini_epoch_seconds(value)
    if seconds is None:
        return None
    return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def gemini_has_verified_quota(data: dict) -> bool:
    """Usage counters alone do not establish a current paid entitlement."""
    return data.get("subscription_status") == "active" and data.get("available") is True


def _guard_gemini_entitlement(data: dict) -> dict:
    """Suppress unverified remaining quota, including pre-upgrade caches.

    Use the RPC plan tier, never its overage boolean, zero usage or model list.
    Unknown/new tiers stay unknown until their meaning can be verified.
    """
    if gemini_has_verified_quota(data):
        return data
    guarded = dict(data)
    guarded["subscription_status"] = data.get("subscription_status") or "unknown"
    guarded["available"] = False
    guarded["unavailable_reason"] = (
        "Gemini account is on the free tier" if guarded["subscription_status"] == "inactive"
        else "Gemini subscription entitlement is not verified"
    )
    guarded["summary"] = {**(data.get("summary") or {}), "remaining_percent": None}
    guarded["buckets"] = [
        {**bucket, "remaining_percent": None} for bucket in data.get("buckets") or []
    ]
    guarded["primary"] = {**(data.get("primary") or {}), "remaining_percent": None}
    return guarded


def _parse_gemini_app_usage_rpc(payloads: list) -> dict:
    response = payloads[0] if payloads else None
    if not isinstance(response, list) or len(response) < 2 or not isinstance(response[1], list):
        raise GeminiAppUsageError("Gemini usage RPC returned an unexpected payload")

    # GetUsageInfo (jSf9Qc), field 0: plan tier. Field 2 is overage preference,
    # not subscription status. Mapping: Gemini-API client._fetch_usage_info.
    tier_id = response[0] if type(response[0]) is int else None
    plan = {1: "Free", 2: "Pro", 3: "Ultra", 4: "Plus", 6: "Ultra"}.get(tier_id)
    status = "inactive" if tier_id == 1 else "active" if plan else "unknown"

    buckets = []
    for raw_bucket in response[1]:
        if not isinstance(raw_bucket, list) or len(raw_bucket) < 4:
            continue
        # Type 3 is an AI-credit balance, not a time-window percentage.
        if raw_bucket[2] not in (1, 2):
            continue
        usage_fraction = raw_bucket[1]
        bucket_kind = raw_bucket[2]
        reset_time = _gemini_epoch_iso(raw_bucket[3])
        try:
            used_percent = max(0, min(100, int(round(float(usage_fraction) * 100))))
        except (TypeError, ValueError):
            used_percent = None
        remaining_percent = None if used_percent is None else max(0, min(100, 100 - used_percent))
        if bucket_kind == 1:
            display_name = "当前用量"
            window = "current"
        elif bucket_kind == 2:
            display_name = "每周限额"
            window = "weekly"
        else:
            display_name = "Gemini App quota"
            window = str(bucket_kind) if bucket_kind is not None else None
        buckets.append(
            {
                "display_name": display_name,
                "used_percent": used_percent,
                "remaining_percent": remaining_percent,
                "reset_time": reset_time,
                "window": window,
                "source": "jSf9Qc",
            }
        )

    if not buckets and status == "active":
        raise GeminiAppUsageError("Gemini usage RPC did not contain quota buckets")
    primary = min(
        buckets,
        key=lambda bucket: 101 if bucket.get("remaining_percent") is None else bucket["remaining_percent"],
        default={},
    )
    return {
        "source": "gemini.google.com/usage RPC jSf9Qc",
        "final_url": GEMINI_APP_USAGE_URL,
        "available": True,
        "subscription_status": status,
        "plan": plan,
        "plan_tier_id": tier_id,
        "subscription_source": "jSf9Qc.plan_tier",
        "unavailable_reason": None,
        "summary": {
            "remaining_percent": primary.get("remaining_percent"),
            "used_percent": primary.get("used_percent"),
            "reset_time": primary.get("reset_time"),
            "reset_text": None,
            "bucket_count": len(buckets),
            "model_count": 0,
        },
        "primary": primary,
        "buckets": buckets,
        "models": [],
    }


def _live_gemini_app_usage_from_rpc(timeout: int) -> dict:
    opener, html, _final_url = _gemini_app_cookie_context(timeout)
    payloads = _gemini_app_batchexecute(opener, html, "jSf9Qc", [], timeout=min(timeout, 8))
    data = _parse_gemini_app_usage_rpc(payloads)
    _save_gemini_app_usage_cache(data)
    return data


def _load_gemini_app_usage_cache(max_age_sec: int = GEMINI_APP_USAGE_CACHE_TTL_SEC):
    """Return ``(data, age_seconds)`` for the cached usage snapshot, if young enough."""
    try:
        data = json.loads(GEMINI_APP_USAGE_CACHE.read_text(encoding="utf-8"))
        cached_at = float(data.get("cached_at", 0))
        age = time.time() - cached_at
        payload = data.get("data")
        if data.get("schema_version") == 2 and payload is not None and 0 <= age <= max_age_sec:
            return payload, int(age)
    except Exception:
        pass
    return None, None


def _mark_gemini_app_usage_cached(data: dict, age_seconds: int | None, *, stale: bool) -> dict:
    data = dict(data)
    age_seconds = int(age_seconds or 0)
    base_source = str(data.get("source") or "gemini.google.com/usage").split(" (cached")[0]
    suffix = "stale cache" if stale else "cached"
    data["source"] = f"{base_source} ({suffix} {age_seconds}s)"
    data["cached"] = True
    data["cache_age_seconds"] = age_seconds
    data["cache_stale"] = stale
    return data


def _save_gemini_app_usage_cache(data: dict):
    try:
        GEMINI_APP_USAGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        GEMINI_APP_USAGE_CACHE.write_text(
            json.dumps({"schema_version": 2, "cached_at": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass



def live_gemini_app_usage(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    cached, age = _load_gemini_app_usage_cache()
    if cached is not None:
        return (
            datetime.datetime.now(datetime.timezone.utc),
            _guard_gemini_entitlement(_mark_gemini_app_usage_cached(cached, age, stale=False)),
        )

    try:
        return datetime.datetime.now(datetime.timezone.utc), _guard_gemini_entitlement(_live_gemini_app_usage(timeout))
    except Exception:
        # Serving an older snapshot beats showing nothing, but it must be
        # labelled so the caller can tell it apart from a fresh reading.
        stale, stale_age = _load_gemini_app_usage_cache(GEMINI_APP_USAGE_CACHE_STALE_SEC)
        if stale is None:
            raise
        return (
            datetime.datetime.now(datetime.timezone.utc),
            _guard_gemini_entitlement(_mark_gemini_app_usage_cached(stale, stale_age, stale=True)),
        )


def _live_gemini_app_usage(timeout: int) -> dict:
    rpc_error = None
    try:
        return _live_gemini_app_usage_from_rpc(timeout=timeout)
    except GeminiAppUsageError as exc:
        rpc_error = exc

    opener, html, final_url = _gemini_app_cookie_context(timeout)
    if not (
        _gemini_app_page_param(html, "cfb2h")
        and _gemini_app_page_param(html, "FdrFJe")
        and _gemini_app_page_param(html, "SNlM0e")
    ):
        raise GeminiAppUsageError(
            t(
                "Gemini App 未登录，请先在 Chrome 打开 gemini.google.com/usage",
                "Gemini App is not signed in; open gemini.google.com/usage in Chrome first",
            )
        ) from rpc_error

    payloads = []
    rpc_errors = []
    for rpc, arg in (
        ("otAQ7b", []),
        ("GPRiHf", []),
        ("maGuAc", [1]),
        ("sJBwce", []),
        ("Te6DCf", [["en-US"], [1]]),
    ):
        try:
            payloads.extend(_gemini_app_batchexecute(opener, html, rpc, arg, timeout=min(timeout, 8)))
        except Exception as exc:
            rpc_errors.append(f"{rpc}: {exc}")

    buckets = _extract_gemini_app_quota_buckets(payloads)
    models = _extract_gemini_app_models(payloads)
    summary_remaining = None
    reset_time = None
    usable_buckets = [bucket for bucket in buckets if bucket.get("remaining_percent") is not None]
    if usable_buckets:
        primary = min(usable_buckets, key=lambda item: item.get("remaining_percent") or 0)
        summary_remaining = primary.get("remaining_percent")
        reset_time = primary.get("reset_time")
    else:
        primary = {}

    unavailable_reason = None
    if "/usage" not in final_url:
        unavailable_reason = f"usage page redirected to {urllib.parse.urlparse(final_url).path or final_url}"
    if not buckets and not models:
        unavailable_reason = unavailable_reason or "Gemini usage RPC returned no quota payload"

    data = {
        "source": "gemini.google.com/usage",
        "final_url": final_url,
        "available": unavailable_reason is None or bool(buckets),
        "unavailable_reason": unavailable_reason,
        "summary": {
            "remaining_percent": summary_remaining,
            "reset_time": reset_time,
            "bucket_count": len(buckets),
            "model_count": len(models),
        },
        "primary": primary,
        "buckets": buckets,
        "models": models,
    }
    if rpc_errors and not payloads:
        data["rpc_errors"] = rpc_errors
    if buckets:
        _save_gemini_app_usage_cache(data)
    return data


def load_deepseek_api_key() -> str:
    env_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env_key:
        return env_key

    for path in DEEPSEEK_KEY_PATHS:
        try:
            key = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if key:
            return key

    raise DeepSeekAuthError(
        t(
            "未找到 DeepSeek API Key，请设置 DEEPSEEK_API_KEY 或 ~/.deepseek_api_key",
            "DeepSeek API key not found. Set DEEPSEEK_API_KEY or ~/.deepseek_api_key",
        )
    )


def has_deepseek_api_key() -> bool:
    try:
        load_deepseek_api_key()
    except DeepSeekAuthError:
        return False
    return True


def live_deepseek_balance(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    import urllib.error
    import urllib.request

    api_key = load_deepseek_api_key()
    req = urllib.request.Request(
        DEEPSEEK_BALANCE_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": f"ai-limit/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise DeepSeekAuthError(
                t(
                    f"HTTP {exc.code}：DeepSeek API Key 无效或无权限",
                    f"HTTP {exc.code}: DeepSeek API key is invalid or unauthorized",
                )
            ) from exc
        raise DeepSeekError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise DeepSeekError(str(exc)) from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise DeepSeekError("non-JSON response") from exc

    return datetime.datetime.now(datetime.timezone.utc), data


# ── GitHub Copilot ────────────────────────────────────────────────────────────

def _copilot_token_like(value) -> str | None:
    text = str(value or "").strip()
    return text if text.startswith(COPILOT_TOKEN_PREFIXES) else None


def _copilot_token_from_keychain() -> str | None:
    """OAuth token stored by `copilot login` in the macOS Keychain.

    The CLI documents the service name (`copilot-cli`) but not the payload
    shape, so accept both a bare token and a JSON blob that carries one.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", COPILOT_KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    direct = _copilot_token_like(raw)
    if direct:
        return direct
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    for value in _walk_json(parsed):
        token = _copilot_token_like(value)
        if token:
            return token
    return None


def _copilot_token_from_gh() -> str | None:
    gh_path = shutil.which("gh")
    if not gh_path:
        return None
    try:
        result = subprocess.run(
            [gh_path, "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return _copilot_token_like(result.stdout)


def load_copilot_github_token() -> tuple[str, str]:
    """Return ``(token, source)`` following the Copilot CLI credential order.

    Env vars first, then the Keychain entry written by `copilot login`, then
    the GitHub CLI token as the documented lowest-priority fallback.
    """
    for name in COPILOT_TOKEN_ENV_VARS:
        token = _copilot_token_like(os.environ.get(name))
        if token:
            return token, name
    token = _copilot_token_from_keychain()
    if token:
        return token, "copilot-cli keychain"
    token = _copilot_token_from_gh()
    if token:
        return token, "gh auth token"
    raise CopilotAuthError(
        t(
            "未找到 GitHub Copilot 登录态，请先运行 copilot login 或 gh auth login",
            "GitHub Copilot sign-in not found. Run copilot login or gh auth login first",
        )
    )


def has_copilot_credentials() -> bool:
    try:
        load_copilot_github_token()
    except CopilotAuthError:
        return False
    return True


_COPILOT_PLAN_LABELS = {
    "individual": "Pro",
    "individual_pro": "Pro",
    "individual_pro_plus": "Pro+",
    "individual_max": "Max",
    "free": "Free",
    "free_limited_copilot": "Free",
    "student": "Student",
    "business": "Business",
    "enterprise": "Enterprise",
}


def _copilot_plan_label(plan) -> str | None:
    if not plan:
        return None
    key = str(plan).strip().lower()
    if key in _COPILOT_PLAN_LABELS:
        return _COPILOT_PLAN_LABELS[key]
    return key.replace("individual_", "").replace("_", " ").title()


def _copilot_bucket_display_name(quota_id: str, token_based_billing: bool) -> str:
    if quota_id == "premium_interactions":
        return "AI Credits" if token_based_billing else "Premium requests"
    return {"chat": "Chat", "completions": "Completions"}.get(quota_id, quota_id.replace("_", " ").title())


def _copilot_int(value) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _normalize_copilot_quota(data: dict) -> dict:
    reported_plan = data.get("copilot_plan")
    raw_sku = data.get("access_type_sku")
    sku = raw_sku.strip().lower() if isinstance(raw_sku, str) else None
    sku_supplied = raw_sku is not None and (not isinstance(raw_sku, str) or bool(sku))
    # The broad account category can remain "individual" after cancellation.
    # A supplied entitlement SKU takes precedence; an unfamiliar SKU must not
    # silently inherit Pro from that legacy category.
    plan = sku if sku_supplied else reported_plan
    plan = plan.strip().lower() if isinstance(plan, str) else None
    status = (
        "free" if plan in {"free", "free_limited_copilot"}
        else "inactive" if plan == "none"
        else "active" if plan in _COPILOT_PLAN_LABELS else "unknown"
    )
    token_based_billing = bool(data.get("token_based_billing"))
    reset_time = data.get("quota_reset_date_utc") or data.get("quota_reset_date")
    snapshots = data.get("quota_snapshots") or {}
    if not isinstance(snapshots, dict):
        snapshots = {}

    buckets = []
    for quota_id, raw in snapshots.items():
        if not isinstance(raw, dict):
            continue
        unlimited = bool(raw.get("unlimited"))
        percent = None if unlimited else raw.get("percent_remaining")
        try:
            percent = None if percent is None else max(0, min(100, int(round(float(percent)))))
        except (TypeError, ValueError):
            percent = None
        entitlement = _copilot_int(raw.get("entitlement"))
        remaining = _copilot_int(raw.get("remaining"))
        if remaining is None:
            remaining = _copilot_int(raw.get("quota_remaining"))
        used = _copilot_int(raw.get("credits_used"))
        if used is None and entitlement is not None and remaining is not None:
            used = max(0, entitlement - remaining)
        buckets.append(
            {
                "bucket_id": quota_id,
                "display_name": _copilot_bucket_display_name(str(quota_id), token_based_billing),
                "window": "monthly",
                "unlimited": unlimited,
                "has_quota": bool(raw.get("has_quota", True)),
                "remaining_percent": 100 if unlimited else percent,
                "entitlement": entitlement,
                "remaining": remaining,
                "used": used,
                "overage_permitted": bool(raw.get("overage_permitted")),
                "overage_count": _copilot_int(raw.get("overage_count")) or 0,
                "reset_time": reset_time,
                "observed_at": raw.get("timestamp_utc"),
            }
        )

    # Free can retain chat/completion allowances with no premium entitlement.
    eligible = [bucket for bucket in buckets if bucket["has_quota"]] if status == "free" else buckets
    primary = next((bucket for bucket in eligible if bucket["bucket_id"] == "premium_interactions"), None)
    if primary is None:
        metered = [bucket for bucket in eligible if not bucket["unlimited"] and bucket["remaining_percent"] is not None]
        primary = min(metered, key=lambda bucket: bucket["remaining_percent"]) if metered else (eligible[0] if eligible else None)
    if primary is None and status == "active":
        raise CopilotQuotaError("Copilot response did not include quota snapshots")
    primary = primary or {}
    if status not in {"active", "free"}:
        buckets = [{**bucket, "remaining_percent": None} for bucket in buckets]
        primary = {**primary, "remaining_percent": None}

    return {
        "source": "api.github.com copilot_internal/user",
        "available": status in {"active", "free"},
        "subscription_status": status,
        "subscription_source": "access_type_sku" if sku_supplied else "copilot_plan",
        "access_type_sku": sku,
        "reported_plan": reported_plan,
        "login": data.get("login"),
        "plan": plan,
        "plan_label": _copilot_plan_label(plan) if status != "unknown" else None,
        "token_based_billing": token_based_billing,
        "unit": "AI credits" if token_based_billing else "premium requests",
        "reset_time": reset_time,
        "primary": primary,
        "buckets": buckets,
        "summary": {
            "remaining_percent": primary.get("remaining_percent"),
            "remaining": primary.get("remaining"),
            "used": primary.get("used"),
            "entitlement": primary.get("entitlement"),
            "overage_permitted": primary.get("overage_permitted"),
            "overage_count": primary.get("overage_count"),
            "reset_time": reset_time,
            "bucket_count": len(buckets),
        },
    }


def live_copilot_quota(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    import urllib.error
    import urllib.request

    token, token_source = load_copilot_github_token()
    req = urllib.request.Request(
        COPILOT_USER_URL,
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/json",
            "User-Agent": f"ai-limit/{__version__}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise CopilotAuthError(
                t(
                    f"HTTP {exc.code}：GitHub 登录已失效或该 token 无 Copilot 权限（来源 {token_source}）",
                    f"HTTP {exc.code}: GitHub sign-in expired or token lacks Copilot access (source {token_source})",
                )
            ) from exc
        if exc.code == 404:
            # A successful sign-in with no entitlement is a normal state.
            # Keep polling so resubscription restores the service automatically.
            normalized = _normalize_copilot_quota({"copilot_plan": "none"})
            normalized["subscription_source"] = "copilot_internal/user HTTP 404"
            normalized["token_source"] = token_source
            return datetime.datetime.now(datetime.timezone.utc), normalized
        raise CopilotQuotaError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise CopilotQuotaError(str(exc)) from exc

    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise CopilotQuotaError("non-JSON response") from exc
    if not isinstance(data, dict):
        raise CopilotQuotaError("unexpected Copilot response shape")

    normalized = _normalize_copilot_quota(data)
    normalized["token_source"] = token_source
    return datetime.datetime.now(datetime.timezone.utc), normalized


# ── Grok (SuperGrok weekly pool) ──────────────────────────────────────────────
#
# grok.com Settings → Usage reads `GrokBuildBilling/GetGrokCreditsConfig`, a
# Connect service spoken as gRPC-web with binary protobuf. The schema below is
# the subset of `prod/grok/backend/proto/grok_build_billing.proto` the usage
# page reads; field numbers come from the descriptor shipped in grok.com's own
# JS bundle.
#
#   GetGrokCreditsConfigResponse { GrokCreditsConfig config = 1; }
#   GrokCreditsConfig {
#     float credit_usage_percent = 1;      Cent on_demand_cap = 2;
#     Cent on_demand_used = 3;             Timestamp billing_period_start = 4;
#     Timestamp billing_period_end = 5;    repeated ProductUsage product_usage = 7;
#     UsagePeriod current_period = 8;      bool is_unified_billing_user = 11;
#     Cent prepaid_balance = 12;
#   }
#   UsagePeriod { UsagePeriodType type = 1; Timestamp start = 2; Timestamp end = 3; }
#   ProductUsage { billing_product.Product product = 1; float usage_percent = 2; }
#   prod_charger.Cent { int64 val = 1; }

_GROK_PERIOD_TYPES = {1: "monthly", 2: "weekly"}
_GROK_PRODUCTS = {
    1: ("api", "API"),
    2: ("build", "Build"),
    3: ("plugins", "Plugins"),
    4: ("chat", "Chat"),
    5: ("imagine", "Imagine"),
    6: ("voice", "Voice"),
    7: ("app_builder", "App Builder"),
    8: ("tasks", "Tasks"),
    9: ("grok_bot", "Grok Bot"),
}


def _pb_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(buf):
            raise GrokUsageError("truncated protobuf varint")
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if byte < 0x80:
            return result, pos
        shift += 7


def _pb_fields(buf: bytes) -> dict[int, list]:
    """Decode one protobuf message into ``{field_number: [raw values]}``."""
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(buf):
        key, pos = _pb_varint(buf, pos)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, pos = _pb_varint(buf, pos)
        elif wire == 1:
            value, pos = buf[pos:pos + 8], pos + 8
        elif wire == 2:
            size, pos = _pb_varint(buf, pos)
            value, pos = buf[pos:pos + size], pos + size
        elif wire == 5:
            value, pos = buf[pos:pos + 4], pos + 4
        else:
            raise GrokUsageError(f"unsupported protobuf wire type {wire}")
        fields.setdefault(number, []).append(value)
    return fields


def _pb_first(fields: dict, number: int, default=None):
    values = fields.get(number)
    return values[-1] if values else default


def _pb_float(fields: dict, number: int) -> float:
    raw = _pb_first(fields, number)
    if not isinstance(raw, (bytes, bytearray)) or len(raw) != 4:
        return 0.0
    value = struct.unpack("<f", raw)[0]
    return value if value == value and value not in (float("inf"), float("-inf")) else 0.0


def _pb_int64(value) -> int:
    value = int(value or 0)
    return value - (1 << 64) if value >= (1 << 63) else value


def _pb_cent(fields: dict, number: int) -> int:
    raw = _pb_first(fields, number)
    if not raw:
        return 0
    return _pb_int64(_pb_first(_pb_fields(raw), 1, 0))


def _pb_timestamp_iso(raw) -> str | None:
    if not raw:
        return None
    ts = _pb_fields(raw)
    seconds = _pb_int64(_pb_first(ts, 1, 0))
    if seconds <= 0:
        return None
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_grpc_web_proto(raw: bytes) -> bytes:
    """Return the single data frame of a unary gRPC-web response.

    Trailers ride in a frame flagged 0x80; a non-zero ``grpc-status`` there
    (or in the HTTP headers for trailers-only replies) is an RPC error.
    """
    message = None
    trailers = ""
    pos = 0
    while pos + 5 <= len(raw):
        flag = raw[pos]
        size = struct.unpack(">I", raw[pos + 1:pos + 5])[0]
        body = raw[pos + 5:pos + 5 + size]
        pos += 5 + size
        if flag & 0x80:
            trailers += body.decode("utf-8", "replace")
        elif message is None:
            message = body
    status = re.search(r"grpc-status:\s*(\d+)", trailers)
    if status and status.group(1) != "0":
        detail = re.search(r"grpc-message:\s*([^\r\n]*)", trailers)
        _raise_grok_grpc_status(int(status.group(1)), urllib.parse.unquote(detail.group(1)) if detail else "")
    if message is None:
        raise GrokUsageError("Grok usage response carried no message frame")
    return message


def _raise_grok_grpc_status(code: int, detail: str = ""):
    # 16 UNAUTHENTICATED, 7 PERMISSION_DENIED
    if code in (7, 16):
        raise GrokAuthError(
            t(
                "grok.com 登录已失效，请先在 Chrome 打开 grok.com 并登录",
                "grok.com session expired; open grok.com in Chrome and sign in",
            )
        )
    # 9 FAILED_PRECONDITION: seen for a signed-in account with no paid
    # SuperGrok / Premium+ pool behind it.
    if code == 9:
        raise GrokUsageError(
            t(
                "grok.com 未返回额度池：当前登录的账号可能没有 SuperGrok / Premium+ 订阅",
                "grok.com returned no usage pool: the signed-in account may lack SuperGrok / Premium+",
            )
        )
    raise GrokUsageError(f"grpc-status {code}{': ' + detail if detail else ''}")


def _normalize_grok_credits_config(message: bytes) -> dict:
    response = _pb_fields(message)
    config_raw = _pb_first(response, 1)
    config = _pb_fields(config_raw) if config_raw else {}
    unified = bool(_pb_first(config, 11, 0))
    if not unified:
        # The usage page treats this as "gated off": the account is not on the
        # shared weekly pool, so there is no percentage to show.
        raise GrokUsageError(
            t(
                "该 Grok 账号未启用 SuperGrok 每周额度池",
                "this Grok account is not on the SuperGrok weekly usage pool",
            )
        )

    used = max(0.0, min(100.0, _pb_float(config, 1)))
    period_raw = _pb_first(config, 8)
    period = _pb_fields(period_raw) if period_raw else {}
    period_type = _GROK_PERIOD_TYPES.get(_pb_first(period, 1, 0), "unspecified")
    period_start = _pb_timestamp_iso(_pb_first(period, 2))
    period_end = _pb_timestamp_iso(_pb_first(period, 3))
    billing_end = _pb_timestamp_iso(_pb_first(config, 5))
    reset_time = period_end or billing_end

    products = []
    for raw in config.get(7, []):
        item = _pb_fields(raw)
        product_id, display_name = _GROK_PRODUCTS.get(
            _pb_first(item, 1, 0), (f"product_{_pb_first(item, 1, 0)}", "Other")
        )
        products.append(
            {
                "product": product_id,
                "display_name": display_name,
                "used_percent": round(max(0.0, _pb_float(item, 2)), 2),
            }
        )
    products.sort(key=lambda item: item["used_percent"], reverse=True)

    # Settings → Usage prints Math.ceil(used); mirror it so both read the same.
    used_display = int(-(-used // 1))
    window = "weekly" if period_type == "weekly" else period_type
    primary = {
        "bucket_id": "supergrok_pool",
        "display_name": "Weekly" if window == "weekly" else "Usage",
        "window": window,
        "used_percent": used_display,
        "remaining_percent": max(0, 100 - used_display),
        "reset_time": reset_time,
    }
    prepaid_cents = max(0, _pb_cent(config, 12))
    on_demand_cap_cents = max(0, _pb_cent(config, 2))
    on_demand_used_cents = max(0, _pb_cent(config, 3))
    return {
        "source": "grok.com GrokBuildBilling/GetGrokCreditsConfig",
        "period_type": period_type,
        "period_start": period_start,
        "reset_time": reset_time,
        "billing_period_end": billing_end,
        "primary": primary,
        "buckets": [primary],
        "products": products,
        "prepaid_balance_cents": prepaid_cents,
        "on_demand_cap_cents": on_demand_cap_cents,
        "on_demand_used_cents": on_demand_used_cents,
        "summary": {
            "used_percent": used_display,
            "used_percent_exact": round(used, 2),
            "remaining_percent": primary["remaining_percent"],
            "reset_time": reset_time,
            "window": window,
            "prepaid_balance_usd": prepaid_cents / 100.0,
            "on_demand_enabled": on_demand_cap_cents > 0,
            "product_count": len(products),
        },
    }


def _chrome_profile_cookie_files() -> list[pathlib.Path]:
    root = pathlib.Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    profiles = [root / "Default"] + sorted(root.glob("Profile *"))
    files = []
    for profile in profiles:
        for candidate in (profile / "Network" / "Cookies", profile / "Cookies"):
            if candidate.is_file():
                files.append(candidate)
                break
    return files


def _chromium_profile_cookie_files() -> list[tuple[str, pathlib.Path]]:
    """Cookie DBs of every Chrome profile, then every Chrome Canary profile.

    Canary has no keychain item of its own on macOS: it encrypts with the
    same "Chrome Safe Storage" key, so browser_cookie3.chrome() can open its
    DB through ``cookie_file``. Labels look like ``Chrome Canary/Default``.
    """
    files = []
    for browser in ("Chrome", "Chrome Canary"):
        root = pathlib.Path.home() / "Library" / "Application Support" / "Google" / browser
        for profile in [root / "Default"] + sorted(root.glob("Profile *")):
            for candidate in (profile / "Network" / "Cookies", profile / "Cookies"):
                if candidate.is_file():
                    files.append((f"{browser}/{profile.name}", candidate))
                    break
    return files


def _load_grok_cookies():
    try:
        import browser_cookie3
    except ImportError as exc:
        raise GrokUsageError(
            t(
                "未安装 browser_cookie3，请先运行: pip install browser-cookie3",
                "browser_cookie3 not installed, run: pip install browser-cookie3",
            )
        ) from exc
    errs = []
    # browser_cookie3 only opens Chrome's Default profile on its own; grok.com
    # is often signed in under a second profile, so try each one explicitly.
    try:
        selected_profile = GROK_CHROME_PROFILE_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        selected_profile = ""
    except OSError as exc:
        raise GrokUsageError("Cannot read Grok Chrome profile setting") from exc
    cookie_files = _chrome_profile_cookie_files()
    if selected_profile:
        # An explicit account selection must never silently fall back to another.
        cookie_files = [
            path for path in cookie_files
            if (path.parent.parent if path.parent.name == "Network" else path.parent).name == selected_profile
        ]
    loaders = [
        (f"Chrome/{(path.parent.parent if path.parent.name == 'Network' else path.parent).name}", lambda path=path: browser_cookie3.chrome(cookie_file=str(path), domain_name="grok.com"))
        for path in cookie_files
    ]
    if not selected_profile:
        # Unpinned: Chrome Canary profiles come after Chrome's, then Firefox.
        loaders.extend(
            (label, lambda path=path: browser_cookie3.chrome(cookie_file=str(path), domain_name="grok.com"))
            for label, path in _chromium_profile_cookie_files()
            if label.startswith("Chrome Canary/")
        )
        loaders.append(("Firefox", lambda: browser_cookie3.firefox(domain_name="grok.com")))
    for name, loader in loaders:
        try:
            cookies = [(c.name, c.value) for c in loader()]
            if any(cookie_name in GROK_SESSION_COOKIES for cookie_name, _ in cookies):
                return cookies
        except Exception as exc:
            errs.append(f"{name}: {exc}")
    if _chrome_keychain_locked(errs):
        raise _keychain_locked_error(GrokAuthError)
    detail = f" ({'; '.join(errs)})" if errs else ""
    raise GrokAuthError(
        t(
            f"未找到 grok.com 登录 cookie{detail}，请先在浏览器登录 grok.com",
            f"grok.com sign-in cookies not found{detail}; sign in to grok.com in your browser",
        )
    )


def has_grok_cookies() -> bool:
    try:
        _load_grok_cookies()
    except GrokUsageError:
        return False
    return True


def live_grok_usage(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    import urllib.error
    import urllib.request

    cookies = _load_grok_cookies()
    req = urllib.request.Request(
        GROK_CREDITS_URL,
        # An empty GetGrokCreditsConfigRequest: one uncompressed, zero-length frame.
        data=b"\x00\x00\x00\x00\x00",
        method="POST",
        headers={
            "Cookie": "; ".join(f"{name}={value}" for name, value in cookies),
            "Content-Type": "application/grpc-web+proto",
            "Accept": "application/grpc-web+proto",
            "X-Grpc-Web": "1",
            "X-User-Agent": "connect-es/2.1.1",
            "User-Agent": CHATGPT_UA,
            "Origin": "https://grok.com",
            "Referer": GROK_USAGE_URL,
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            header_status = response.headers.get("grpc-status")
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise GrokAuthError(
                t(
                    f"HTTP {exc.code}：grok.com 登录已失效或被拦截，请在 Chrome 重新打开 grok.com",
                    f"HTTP {exc.code}: grok.com session expired or blocked; reopen grok.com in Chrome",
                )
            ) from exc
        raise GrokUsageError(f"HTTP {exc.code}") from exc
    except Exception as exc:
        raise GrokUsageError(str(exc)) from exc

    if header_status and header_status != "0":
        _raise_grok_grpc_status(int(header_status))
    normalized = _normalize_grok_credits_config(_parse_grpc_web_proto(body))
    return datetime.datetime.now(datetime.timezone.utc), normalized
