import base64
import datetime
import json
import os
import pathlib
import select
import shutil
import socket
import struct
import subprocess
import sys
import time

from ai_limit.i18n import t

CLAUDE_USAGE_URL = "https://claude.ai/settings/usage"
CODEX_USAGE_URL = "https://chatgpt.com/codex/cloud/settings/analytics"
DEEPSEEK_BALANCE_URL = "https://api.deepseek.com/user/balance"
GOOGLE_QUOTA_URL = "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota"
REMOTE_TIMEOUT_SEC = 15
CLAUDE_WEB_TIMEOUT_SEC = 15
CODEX_WINDOW_CACHE = pathlib.Path.home() / ".codex_window_cache"
GEMINI_OAUTH_PATH = pathlib.Path.home() / ".gemini" / "oauth_creds.json"
GEMINI_PROJECT_PATH = pathlib.Path.home() / ".gemini" / "config" / "projects" / "default-cli-project.json"
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
    rate_limit = data.get("rate_limit") or {}

    def window(window_data):
        if not window_data:
            return None
        window_seconds = window_data.get("limit_window_seconds")
        return {
            "used_percent": window_data.get("used_percent", 0),
            "window_minutes": window_seconds // 60 if window_seconds else None,
            "resets_at": window_data.get("reset_at"),
        }

    return {
        "limit_id": None,
        "limit_name": None,
        "primary": window(rate_limit.get("primary_window")),
        "secondary": window(rate_limit.get("secondary_window")),
        "credits": data.get("credits"),
        "plan_type": data.get("plan_type"),
        "rate_limit_reached_type": rate_limit.get("rate_limit_reached_type"),
    }


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


def current_codex_rate_limits(latest_codex_rate_limits_func):
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

    if window_active:
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
        "primary": primary,
        "buckets": buckets,
        "summary": {
            "remaining_percent": summary_percent,
            "reset_time": reset_times[0] if reset_times else (primary or {}).get("reset_time"),
            "bucket_count": len(buckets),
        },
    }


def live_google_quota(timeout: int = CLAUDE_WEB_TIMEOUT_SEC):
    import urllib.error
    import urllib.request

    creds = load_google_oauth_creds()
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
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
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
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise GoogleQuotaError("non-JSON response") from exc

    return datetime.datetime.now(datetime.timezone.utc), _normalize_google_quota(data)


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
