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
REMOTE_TIMEOUT_SEC = 15
CLAUDE_WEB_TIMEOUT_SEC = 15
CODEX_WINDOW_CACHE = pathlib.Path.home() / ".codex_window_cache"
GEMINI_OAUTH_PATH = pathlib.Path.home() / ".gemini" / "oauth_creds.json"
GEMINI_PROJECT_PATH = pathlib.Path.home() / ".gemini" / "config" / "projects" / "default-cli-project.json"
ANTIGRAVITY_LOG_DIR = pathlib.Path.home() / ".gemini" / "antigravity-cli" / "log"
ANTIGRAVITY_QUOTA_LOG_MAX_AGE_SEC = 36 * 60 * 60
ANTIGRAVITY_CLI_USAGE_CACHE = pathlib.Path.home() / ".cache" / "ai-limit" / "antigravity-cli-usage.json"
ANTIGRAVITY_CLI_USAGE_CACHE_TTL_SEC = 5 * 60
ANTIGRAVITY_CLI_USAGE_TIMEOUT_SEC = 18
ANTIGRAVITY_DEFAULT_MODELS = (
    "Gemini 3.5 Flash (Medium)",
    "Gemini 3.5 Flash (High)",
    "Gemini 3.5 Flash (Low)",
    "Gemini 3.1 Pro (Low)",
    "Gemini 3.1 Pro (High)",
    "Claude Sonnet 4.6 (Thinking)",
    "Claude Opus 4.6 (Thinking)",
    "GPT-OSS 120B (Medium)",
)
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
ANTIGRAVITY_QUOTA_RE = re.compile(
    r"RESOURCE_EXHAUSTED \(code 429\): Individual quota reached\..*?Resets in "
    r"(?P<duration>(?:(?:\d+)h)?(?:(?:\d+)m)?(?:(?:\d+)s)?)"
)
ANTIGRAVITY_LOG_TIME_RE = re.compile(r"^[A-Z](?P<month>\d{2})(?P<day>\d{2}) (?P<time>\d{2}:\d{2}:\d{2})\.(?P<micro>\d{1,6})")
ANTIGRAVITY_LOG_FILE_RE = re.compile(r"cli-(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})_")
ANTIGRAVITY_CSRF_RE = re.compile(r"--csrf_token\s+(\S+)")
ANSI_RE = re.compile(
    r"(?:\x1b\][^\x07]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]|\x1b\[[0-?]*[ -/]*[@-~])"
)


class ClaudeWebError(Exception):
    pass


class CodexRemoteError(Exception):
    pass


class CodexWebError(Exception):
    pass


class CodexAuthError(CodexWebError):
    """401 / 403: unauthenticated or missing Codex access."""


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
        normalized = _normalize_remote_rate_limits(rate_limits)
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


def _normalize_remote_rate_limits(rate_limits: dict) -> dict:
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
        "credits": rate_limits.get("credits"),
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
    errs = []
    for name, loader in [("Chrome", browser_cookie3.chrome), ("Firefox", browser_cookie3.firefox)]:
        try:
            jar = loader(domain_name=".chatgpt.com")
            cookies = [(c.name, c.value) for c in jar]
            if cookies:
                return cookies
        except Exception as exc:
            errs.append(f"{name}: {exc}")
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
        groups.append(
            group_from_rate_limit(
                item.get("limit_name") or item.get("metered_feature") or "Additional Codex limit",
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
        "credits": data.get("credits"),
        "plan_type": data.get("plan_type"),
        "rate_limit_reached_type": rate_limit.get("rate_limit_reached_type"),
    }
    return normalized


def live_codex_web_usage(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    import urllib.error
    import urllib.request

    cookies = _load_chatgpt_cookies()
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
    match = ANTIGRAVITY_LOG_TIME_RE.match(line)
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


def list_antigravity_models(timeout: int = 8) -> list[str]:
    agy_path = shutil.which("agy")
    if not agy_path:
        return list(ANTIGRAVITY_DEFAULT_MODELS)
    try:
        result = subprocess.run(
            [agy_path, "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return list(ANTIGRAVITY_DEFAULT_MODELS)
    if result.returncode != 0:
        return list(ANTIGRAVITY_DEFAULT_MODELS)
    models = []
    for line in result.stdout.splitlines():
        model = line.strip()
        if model and model not in models:
            models.append(model)
    return models or list(ANTIGRAVITY_DEFAULT_MODELS)


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
    return {
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
            "User-Agent": "ai-limit/0.3.5 Antigravity",
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


def _run_antigravity_cli_usage_text(timeout: int) -> str:
    import fcntl
    import pty
    import termios

    agy_path = shutil.which("agy")
    if not agy_path:
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
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            text = _strip_terminal_control(b"".join(chunks).decode("utf-8", errors="replace"))
            if "Do you trust the contents of this project?" in text and not sent_trust:
                os.write(master_fd, b"\r")
                sent_trust = True
                continue
            if (
                "Antigravity CLI" in text
                and "Do you trust the contents of this project?" not in text
                and not sent_usage
                and re.search(r"(?:^|\n)>\s*(?:\n|$)", text)
            ):
                os.write(master_fd, b"/usage\r")
                sent_usage = True
                continue
            if "Models & Quota" in text and page_down_count < 2 and time.monotonic() - last_page_down > 0.6:
                os.write(master_fd, b"\x1b[6~")
                page_down_count += 1
                last_page_down = time.monotonic()
                continue
            if (
                "GEMINI MODELS" in text
                and "CLAUDE AND GPT MODELS" in text
                and text.count("Five Hour Limit") >= 2
                and text.count("Refreshes in") >= 3
                and not sent_exit
            ):
                os.write(master_fd, b"\x1b/exit\r")
                sent_exit = True
                time.sleep(0.3)
                break
        return b"".join(chunks).decode("utf-8", errors="replace")
    finally:
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


def live_antigravity_cli_usage(timeout: int = ANTIGRAVITY_CLI_USAGE_TIMEOUT_SEC) -> dict:
    cached = _load_antigravity_cli_usage_cache()
    if cached:
        return cached
    data = _parse_antigravity_cli_usage_text(_run_antigravity_cli_usage_text(timeout))
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


def _antigravity_model_buckets(models: list[str], limit: dict | None) -> list[dict]:
    limited_model = (limit or {}).get("model_label")
    buckets = []
    for model in models:
        limited = bool(limit and limited_model == model)
        buckets.append(
            {
                "model_id": model,
                "remaining_amount": 0 if limited else None,
                "remaining_fraction": 0.0 if limited else None,
                "remaining_percent": 0 if limited else None,
                "reset_time": (limit or {}).get("reset_time") if limited else None,
                "quota_state": "limited" if limited else "unknown",
                "quota_source": (limit or {}).get("source") if limited else "agy models",
            }
        )
    if limit and limited_model and all(bucket.get("model_id") != limited_model for bucket in buckets):
        buckets.insert(
            0,
            {
                "model_id": limited_model,
                "remaining_amount": 0,
                "remaining_fraction": 0.0,
                "remaining_percent": 0,
                "reset_time": limit.get("reset_time"),
                "quota_state": "limited",
                "quota_source": limit.get("source"),
            },
        )
    return buckets


def _with_antigravity_view(normalized: dict, limit: dict | None, models: list[str]) -> dict:
    agy_buckets = _antigravity_model_buckets(models, limit)
    primary = next((bucket for bucket in agy_buckets if bucket.get("quota_state") == "limited"), None)
    if primary is None:
        primary = agy_buckets[0] if agy_buckets else normalized.get("primary")

    summary = dict(normalized.get("summary") or {})
    if limit:
        summary.update(
            {
                "remaining_percent": 0,
                "reset_time": limit.get("reset_time") or summary.get("reset_time"),
                "bucket_count": len(agy_buckets),
                "quota_state": "limited",
            }
        )
    else:
        summary.update(
            {
                "remaining_percent": None,
                "reset_time": None,
                "bucket_count": len(agy_buckets),
                "quota_state": "unknown",
            }
        )
    return {
        **normalized,
        "source": "agy models + antigravity-cli-log",
        "primary": primary,
        "buckets": agy_buckets,
        "legacy_google_buckets": normalized.get("buckets") or [],
        "legacy_google_source": normalized.get("source"),
        "summary": summary,
        "antigravity": limit,
    }


def live_google_quota(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    import urllib.error
    import urllib.request

    try:
        return datetime.datetime.now(datetime.timezone.utc), live_antigravity_quota_summary(timeout=min(timeout, 8))
    except GoogleQuotaError:
        pass
    try:
        return datetime.datetime.now(datetime.timezone.utc), live_antigravity_cli_usage()
    except GoogleQuotaError:
        pass

    def fetch(creds: dict) -> bytes:
        req = urllib.request.Request(
            GOOGLE_QUOTA_URL,
            data=json.dumps({"project": load_google_project_id()}).encode(),
            headers={
                "Authorization": f"Bearer {creds['access_token']}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "ai-limit/0.3.5",
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
        list_antigravity_models(),
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


def _parse_gemini_app_usage_rpc(payloads: list) -> dict:
    response = payloads[0] if payloads else None
    if not isinstance(response, list) or len(response) < 2 or not isinstance(response[1], list):
        raise GeminiAppUsageError("Gemini usage RPC returned an unexpected payload")

    buckets = []
    for raw_bucket in response[1]:
        if not isinstance(raw_bucket, list) or len(raw_bucket) < 4:
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

    if not buckets:
        raise GeminiAppUsageError("Gemini usage RPC did not contain quota buckets")
    primary = min(
        buckets,
        key=lambda bucket: 101 if bucket.get("remaining_percent") is None else bucket["remaining_percent"],
    )
    return {
        "source": "gemini.google.com/usage RPC jSf9Qc",
        "final_url": GEMINI_APP_USAGE_URL,
        "available": True,
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
        if payload is not None and 0 <= age <= max_age_sec:
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
            json.dumps({"cached_at": time.time(), "data": data}, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass



def live_gemini_app_usage(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    cached, age = _load_gemini_app_usage_cache()
    if cached is not None:
        return (
            datetime.datetime.now(datetime.timezone.utc),
            _mark_gemini_app_usage_cached(cached, age, stale=False),
        )

    try:
        return datetime.datetime.now(datetime.timezone.utc), _live_gemini_app_usage(timeout)
    except Exception:
        # Serving an older snapshot beats showing nothing, but it must be
        # labelled so the caller can tell it apart from a fresh reading.
        stale, stale_age = _load_gemini_app_usage_cache(GEMINI_APP_USAGE_CACHE_STALE_SEC)
        if stale is None:
            raise
        return (
            datetime.datetime.now(datetime.timezone.utc),
            _mark_gemini_app_usage_cached(stale, stale_age, stale=True),
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
            "User-Agent": "ai-limit/0.3.5",
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
