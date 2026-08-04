#!/usr/bin/env python3
"""ai-limit 菜单栏 App（rumps 版）

独立 macOS App，不依赖 SwiftBar，有自己的图标和进程。
py2app 打包：cd menubar && python3 setup.py py2app
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import threading
import webbrowser
import atexit

os.environ.setdefault("LANG", "en_US.UTF-8")
os.environ.setdefault("LC_ALL", "en_US.UTF-8")
os.environ.setdefault("PYTHONUTF8", "1")
_CLI_PATHS = (
    str(pathlib.Path.home() / ".local" / "bin"),
    "/opt/homebrew/bin",
    "/opt/homebrew/sbin",
    "/usr/local/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)
_existing_path = os.environ.get("PATH") or ""
os.environ["PATH"] = os.pathsep.join(
    [path for path in _CLI_PATHS if path]
    + [path for path in _existing_path.split(os.pathsep) if path and path not in _CLI_PATHS]
)

import rumps
import AppKit

_REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))

from usage import (
    __version__,
    fmt_money,
    live_claude_plan,
    live_claude_usage,
    latest_codex_rate_limits,
    TZ_LOCAL,
    epoch_to_local,
)
from ai_limit.providers import (
    live_codex_web_usage,
    ClaudeWebError,
    CodexWebError,
    CodexAuthError,
    current_codex_rate_limits as resolve_codex_rate_limits,
    codex_5h_remaining_percent,
    codex_window_remaining_percent,
    codex_window_reset_time,
    clear_provider_caches,
    DeepSeekAuthError,
    DeepSeekError,
    GeminiAppUsageError,
    GoogleQuotaAuthError,
    GoogleQuotaError,
    has_deepseek_api_key,
    has_gemini_app_cookies,
    has_google_oauth_creds,
    live_deepseek_balance,
    live_gemini_app_usage,
    live_google_quota,
)
from ai_limit.llm_api import (
    clear_llm_api_balance_cache,
    has_llm_api_provider_config,
    live_llm_api_balances,
)

# ── 常量 ─────────────────────────────────────────────────────────────────────

_STATE_PATH   = pathlib.Path.home() / ".ai-limit-menubar.json"
_CACHE_PATH   = pathlib.Path.home() / ".ai-limit-menubar-cache.json"
_CACHE_TTL    = 55
_ERROR_CACHE_TTL = 5
_REFRESH_SEC  = 60
_DISPLAY_MODES = ("5h", "7d")
_LANGS         = ("zh", "en")
_SERVICES      = ("claude", "codex", "deepseek", "google", "gemini", "llm_api")
_MENU_MIN_WIDTH = 290
_ZH_WEEKDAYS   = "一二三四五六日"
_EN_WEEKDAYS   = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_EN_RESET_PAD  = 8
_PROJECT_URL   = "https://github.com/wonderstone/ai-limit/tree/main"
_AUTHOR_URL_ZH = "https://gitee.com/zhuchenxi113"
_AUTHOR_URL_EN = "https://github.com/zhuchenxi113"
_DEEPSEEK_USAGE_URL = "https://platform.deepseek.com/usage"
_GOOGLE_QUOTA_DOCS_URL = "https://antigravity.google/docs/cli-credits"
_GEMINI_APP_USAGE_URL = "https://gemini.google.com/usage"
_LAUNCH_AGENT_LABEL = "com.zhuchenxi.ai-limit"
_LAUNCH_AGENT_PLIST = pathlib.Path.home() / "Library/LaunchAgents" / f"{_LAUNCH_AGENT_LABEL}.plist"
_APP_EXECUTABLE     = pathlib.Path("/Applications/ai-limit.app/Contents/MacOS/ai-limit")
_PID_PATH           = pathlib.Path.home() / ".ai-limit-menubar.pid"

# ── 工具函数 ─────────────────────────────────────────────────────────────────

def _login_item_enabled():
    return _LAUNCH_AGENT_PLIST.exists()


def _default_services():
    services = ["claude", "codex"]
    if has_deepseek_api_key():
        services.append("deepseek")
    if has_google_oauth_creds():
        services.append("google")
    if has_gemini_app_cookies():
        services.append("gemini")
    if has_llm_api_provider_config():
        services.append("llm_api")
    return services


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _release_single_instance():
    try:
        if _PID_PATH.exists() and _PID_PATH.read_text().strip() == str(os.getpid()):
            _PID_PATH.unlink()
    except Exception:
        pass


def _acquire_single_instance() -> bool:
    try:
        if _PID_PATH.exists():
            existing = int(_PID_PATH.read_text().strip())
            if existing != os.getpid() and _pid_is_running(existing):
                return False
    except Exception:
        pass

    try:
        _PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except Exception:
        return True

    atexit.register(_release_single_instance)
    return True

def _set_login_item(enabled: bool):
    """通过 launchctl bootstrap / bootout 管理 LaunchAgent。

    只有装到 /Applications 的正式 App 才走 launchctl；源码运行不写自启。
    """
    app_path = pathlib.Path(sys.executable if getattr(sys, 'frozen', False) else __file__)
    if not str(app_path).startswith("/Applications/"):
        return  # 非正式安装路径，不操作 launchctl

    _LAUNCH_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)

    if enabled:
        _LAUNCH_AGENT_PLIST.write_text(
            f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{_APP_EXECUTABLE}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>{pathlib.Path.home()}/.ai-limit-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>{pathlib.Path.home()}/.ai-limit-launchd.log</string>
</dict>
</plist>
""",
            encoding="utf-8",
        )
        # bootstrap: 注册并立即启动；已注册则无操作（幂等）
        _run_launchctl("bootstrap", f"gui/{os.getuid()}", str(_LAUNCH_AGENT_PLIST))
    else:
        # bootout: 停止并从 launchd 注销
        _run_launchctl("bootout", f"gui/{os.getuid()}/{_LAUNCH_AGENT_LABEL}")
        try:
            _LAUNCH_AGENT_PLIST.unlink()
        except FileNotFoundError:
            pass


def _run_launchctl(*args):
    """执行 launchctl，静默失败（用户可能没有 launchctl 权限）。"""
    try:
        subprocess.run(
            ["launchctl"] + list(args),
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _ensure_login_item_on_first_run():
    """首次启动时自动注册开机自启（静默，不弹窗）。"""
    if _STATE_PATH.exists():
        return  # 已有状态文件 = 非首次运行

    # 仅对 /Applications 下的正式安装启用
    app_path = pathlib.Path(sys.executable if getattr(sys, 'frozen', False) else __file__)
    if str(app_path).startswith("/Applications/"):
        try:
            _set_login_item(True)
        except Exception:
            pass

def _tr(lang, zh, en):
    return en if lang == "en" else zh

def _native_bar(pct, width=4):
    filled = round(max(0, min(100, pct)) / 100 * width)
    return "▰" * filled + "▱" * (width - filled)

def _fmt_plan(plan, lang="zh"):
    if not plan or plan == "?":
        return ""
    plan = str(plan).replace("_", " ").title()
    return f" Plan: {plan}" if lang == "en" else f" 方案：{plan}"


def _fmt_balance_short(balance):
    if not balance:
        return "?"
    currency = balance.get("currency", "USD")
    total = balance.get("total_balance", "0")
    return fmt_money(total, currency)


def _fmt_balance_compact(balance):
    if not balance:
        return "?"
    currency = balance.get("currency", "USD")
    total = balance.get("total_balance", "0")
    try:
        amount = float(total)
    except Exception:
        amount = None
    if amount is None:
        return str(total)
    if currency == "USD":
        return f"${amount:.0f}" if amount >= 100 else f"${amount:.2f}"
    if currency == "CNY":
        return f"¥{amount:.0f}" if amount >= 100 else f"¥{amount:.2f}"
    return f"{currency}{amount:.0f}" if amount >= 100 else f"{currency}{amount:.2f}"


def _status_service_label(service):
    return {
        "claude": "C",
        "codex": "X",
        "deepseek": "D",
        "google": "G",
        "gemini": "M",
        "llm_api": "LLM",
    }.get(service, service[:1].upper())


def _balance_amount(balance) -> float:
    try:
        return float((balance or {}).get("total_balance", "0"))
    except Exception:
        return 0.0


def _pick_primary_balance(balances):
    if not balances:
        return None
    ranked = sorted(
        balances,
        key=lambda item: (
            _balance_amount(item) <= 0,
            {"CNY": 0, "USD": 1}.get(item.get("currency"), 9),
        ),
    )
    return ranked[0]

def _fmt_reset_dt(dt, lang):
    today = datetime.datetime.now(TZ_LOCAL).date()
    target = dt.date()
    days = (target - today).days
    next_week = target.isocalendar()[:2] > today.isocalendar()[:2]
    if lang == "en":
        if days == 0:    wd = "today"
        elif days == 1:  wd = "tomorrow"
        elif days == 2:  wd = "2 days"
        elif next_week:  wd = f"next {_EN_WEEKDAYS[dt.weekday()]}"
        else:            wd = _EN_WEEKDAYS[dt.weekday()]
        return f"{dt:%H:%M}  {wd}"
    if days == 0:    wd = "今天"
    elif days == 1:  wd = "明天"
    elif days == 2:  wd = "后天"
    elif next_week:  wd = f"下周{_ZH_WEEKDAYS[dt.weekday()]}"
    else:            wd = f"周{_ZH_WEEKDAYS[dt.weekday()]}"
    if len(wd) < 3:
        wd += "　" * (3 - len(wd))
    return f"{wd} {dt:%H:%M}"

def _fmt_reset_epoch(epoch, lang="zh"):
    try:
        return _fmt_reset_dt(epoch_to_local(int(epoch)), lang)
    except Exception:
        return "?"

def _fmt_reset_iso(iso, lang="zh"):
    try:
        return _fmt_reset_dt(datetime.datetime.fromisoformat(iso).astimezone(TZ_LOCAL), lang)
    except Exception:
        return "?"

# ── 状态 / 缓存 ──────────────────────────────────────────────────────────────

def _load_state():
    state = {"global": "5h", "lang": "zh", "services": _default_services(), "widget": True}
    try:
        raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            if raw.get("global") in _DISPLAY_MODES:
                state["global"] = raw["global"]
            if raw.get("lang") in _LANGS:
                state["lang"] = raw["lang"]
            if isinstance(raw.get("widget"), bool):
                state["widget"] = raw["widget"]
            if isinstance(raw.get("services"), list):
                svc = ["llm_api" if s == "infoweave" else s for s in raw["services"]]
                svc = [s for s in svc if s in _SERVICES]
                if "gemini" not in svc and has_gemini_app_cookies():
                    svc.append("gemini")
                if "llm_api" not in svc and has_llm_api_provider_config():
                    svc.append("llm_api")
                if svc:
                    state["services"] = svc
    except Exception:
        pass
    return state

def _save_state(state):
    try:
        _STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except Exception:
        pass

def _load_cache():
    try:
        raw = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        age = datetime.datetime.now().timestamp() - float(raw.get("cached_at", 0))
        claude = raw.get("claude")
        codex = raw.get("codex")
        if isinstance(codex, dict) and codex.get("source") != "web":
            codex = None
        ttl = _ERROR_CACHE_TTL if isinstance(claude, dict) and claude.get("error") else _CACHE_TTL
        if age <= ttl:
            cached = {
                "codex": codex,
                "deepseek": raw.get("deepseek"),
                "google": raw.get("google"),
                "gemini": raw.get("gemini"),
                "llm_api": raw.get("llm_api"),
            }
            # Older builds stored only claude/codex. Keep accepting that shape
            # so users do not need to delete their cache after upgrading.
            if all(value is None for value in cached.values()):
                return claude, codex
            return claude, cached
    except Exception:
        pass
    return None, None

def _save_cache(claude, cached):
    try:
        if isinstance(cached, dict) and (
            "codex" in cached or "deepseek" in cached or "google" in cached or "gemini" in cached or "llm_api" in cached
        ):
            codex = cached.get("codex")
            deepseek = cached.get("deepseek")
            google = cached.get("google")
            gemini = cached.get("gemini")
            llm_api = cached.get("llm_api")
        else:
            codex = cached
            deepseek = google = gemini = llm_api = None
        _CACHE_PATH.write_text(
            json.dumps({
                "cached_at": datetime.datetime.now().timestamp(),
                "claude": claude,
                "codex": codex,
                "deepseek": deepseek,
                "google": google,
                "gemini": gemini,
                "llm_api": llm_api,
            }, ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception:
        pass

def _codex_transient_error(codex):
    if not isinstance(codex, dict) or not codex.get("error"):
        return False
    text = str(codex.get("error") or "")
    return any(
        needle in text
        for needle in (
            "实时数据暂不可用",
            "live data temporarily unavailable",
            "额度数据暂不完整",
            "usage data temporarily incomplete",
            "网络超时",
            "Network timeout",
            "网络不可用",
            "Network unavailable",
        )
    )

def _codex_window_jump(previous, candidate, value_key, reset_key):
    if not isinstance(previous, dict) or not isinstance(candidate, dict):
        return False
    prev_value = previous.get(value_key)
    next_value = candidate.get(value_key)
    if prev_value is None or next_value is None:
        return False
    prev_reset = previous.get(reset_key)
    next_reset = candidate.get(reset_key)
    if not (prev_reset and next_reset):
        return False
    if prev_reset != next_reset:
        return False
    try:
        return float(next_value) - float(prev_value) > 25
    except (TypeError, ValueError):
        return False

def _codex_unstable_sample(previous, candidate):
    if not (
        isinstance(previous, dict)
        and isinstance(candidate, dict)
        and previous.get("source") == "web"
        and candidate.get("source") == "web"
        and not previous.get("error")
        and not candidate.get("error")
    ):
        return False
    return (
        _codex_window_jump(previous, candidate, "5h_left", "5h_reset")
        or _codex_window_jump(previous, candidate, "7d_left", "7d_reset")
    )


def _clear_all_caches():
    for path in (_CACHE_PATH,):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except Exception:
            pass
    try:
        clear_provider_caches()
    except Exception:
        pass
    try:
        clear_llm_api_balance_cache()
    except Exception:
        pass


# ── 数据获取 ─────────────────────────────────────────────────────────────────

def _fetch_claude(lang):
    import socket, urllib.error
    try:
        data = live_claude_usage()
        five_h = data.get("five_hour") or {}
        seven_d = data.get("seven_day") or {}
        try:
            plan = live_claude_plan()
        except Exception:
            plan = None
        return {
            "5h_left":  int(round(100 - float(five_h.get("utilization", 0)))),
            "7d_left":  int(round(100 - float(seven_d.get("utilization", 0)))),
            "5h_reset": five_h.get("resets_at"),
            "7d_reset": seven_d.get("resets_at"),
            "plan":     plan,
            "source":   "browser",
        }
    except ClaudeWebError as e:
        msg = str(e)
        if "JSON" in msg or "DOCTYPE" in msg or "html" in msg.lower():
            msg = _tr(lang, "网络不可用或需重新登录 claude.ai", "Network error or re-login at claude.ai required")
        return {"error": msg}
    except (socket.timeout, TimeoutError):
        return {"error": _tr(lang, "网络超时，请稍后重试", "Network timeout, please retry later")}
    except urllib.error.URLError:
        return {"error": _tr(lang, "网络不可用", "Network unavailable")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

def _fetch_codex(lang):
    import socket, urllib.error
    try:
        _ts, rl, source, fallback_reason = resolve_codex_rate_limits(
            latest_codex_rate_limits, allow_app_server_fallback=False
        )
        if not rl:
            if source == "no_access":
                return {"error": _tr(
                    lang,
                    "无 Codex 权限（可能未订阅或需重新登录）",
                    "No Codex access (subscription required or re-login needed)",
                )}
            if fallback_reason:
                return {"error": fallback_reason}
            return {"error": _tr(lang, "未找到 Codex 数据", "No Codex data found")}
        # 只信任 web 权威源。live(app-server) 和 snapshot 都只反映本机状态、会
        # 少报云端/其他设备用量（真实 26% 时可能显示 99%/77%），而且 live 每次
        # 还会 spawn codex app-server 并触发 5h 冷却副作用。web 瞬时失败时拒绝
        # 这些回退源，保留上一份缓存好值，等待下一轮 web 自动纠正。
        if source != "web":
            return None
        primary   = rl.get("primary") or {}
        secondary = rl.get("secondary") or {}
        summary = rl.get("summary") or {}
        buckets = rl.get("buckets") or []
        five_hour_left = codex_5h_remaining_percent(rl)
        weekly_left = codex_window_remaining_percent(rl, "weekly")
        if five_hour_left is None and weekly_left is None:
            return None
        return {
            "5h_left":  None if five_hour_left is None else int(round(five_hour_left)),
            "7d_left":  int(round(weekly_left if weekly_left is not None else 100 - secondary.get("used_percent", 0))),
            "5h_reset": codex_window_reset_time(rl, "5h"),
            "7d_reset": codex_window_reset_time(rl, "weekly") or secondary.get("resets_at"),
            "plan":     rl.get("plan_type") or "?",
            "source":   source,
            "groups":   rl.get("groups") or [],
            "buckets":  buckets,
            "group_count": summary.get("group_count") or len(rl.get("groups") or []),
            "bucket_count": summary.get("bucket_count") or len(buckets),
        }
    except CodexAuthError:
        return {"error": _tr(lang,
            "无 Codex 权限（可能未订阅或需重新登录）",
            "No Codex access (subscription required or re-login needed)")}
    except CodexWebError as e:
        msg = str(e)
        if "timed out" in msg or "urlopen" in msg:
            return None
        return {"error": msg}
    except (socket.timeout, TimeoutError):
        return None
    except urllib.error.URLError:
        return None
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fetch_deepseek(lang):
    import socket, urllib.error
    try:
        _ts, data = live_deepseek_balance()
        balances = data.get("balance_infos") or []
        primary = _pick_primary_balance(balances)
        return {
            "available": bool(data.get("is_available")),
            "balances": balances,
            "primary": primary,
            "source": "api key live",
        }
    except DeepSeekAuthError as e:
        return {"error": str(e)}
    except DeepSeekError as e:
        return {"error": str(e)}
    except (socket.timeout, TimeoutError):
        return {"error": _tr(lang, "网络超时，请稍后重试", "Network timeout, please retry later")}
    except urllib.error.URLError:
        return {"error": _tr(lang, "网络不可用", "Network unavailable")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fetch_google(lang):
    import socket, urllib.error
    try:
        _ts, data = live_google_quota()
        summary = data.get("summary") or {}
        primary = data.get("primary") or {}
        primary_model = primary.get("model_id") or " / ".join(
            item for item in (primary.get("group_display_name"), primary.get("display_name")) if item
        )
        return {
            "daily_left": summary.get("remaining_percent"),
            "daily_reset": summary.get("reset_time"),
            "bucket_count": summary.get("bucket_count", 0),
            "group_count": summary.get("group_count"),
            "primary_model": primary_model,
            "buckets": data.get("buckets") or [],
            "quota_groups": data.get("quota_groups") or [],
            "antigravity": data.get("antigravity"),
            "source": data.get("source") or "oauth live",
        }
    except GoogleQuotaAuthError as e:
        return {"error": str(e)}
    except GoogleQuotaError as e:
        return {"error": str(e)}
    except (socket.timeout, TimeoutError):
        return {"error": _tr(lang, "网络超时，请稍后重试", "Network timeout, please retry later")}
    except urllib.error.URLError:
        return {"error": _tr(lang, "网络不可用", "Network unavailable")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fetch_gemini_app(lang):
    import socket, urllib.error
    try:
        _ts, data = live_gemini_app_usage()
        summary = data.get("summary") or {}
        return {
            "left": summary.get("remaining_percent"),
            "used": summary.get("used_percent"),
            "reset": summary.get("reset_time"),
            "reset_text": summary.get("reset_text"),
            "bucket_count": summary.get("bucket_count", 0),
            "model_count": summary.get("model_count", 0),
            "available": data.get("available"),
            "unavailable_reason": data.get("unavailable_reason"),
            "buckets": data.get("buckets") or [],
            "models": data.get("models") or [],
            "source": data.get("source") or "gemini.google.com/usage",
            "final_url": data.get("final_url"),
        }
    except GeminiAppUsageError as e:
        return {"error": str(e)}
    except (socket.timeout, TimeoutError):
        return {"error": _tr(lang, "网络超时，请稍后重试", "Network timeout, please retry later")}
    except urllib.error.URLError:
        return {"error": _tr(lang, "网络不可用", "Network unavailable")}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fetch_llm_api(lang):
    try:
        return live_llm_api_balances(cache_ttl_seconds=300)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

# ── AppKit 辅助 ───────────────────────────────────────────────────────────────

def _status_button(app):
    """返回 NSStatusItem.button()；rumps 在不同版本里把它存在不同属性下。"""
    # 已知 rumps 0.4 在 _nsapp.nsstatusitem，但版本间不一致；做一次探测
    candidates = ("_status_item", "_status_bar_item", "_nsstatusitem")
    for attr in candidates:
        item = getattr(app, attr, None)
        if item and hasattr(item, "button"):
            return item.button()
    # rumps 0.4.x 路径：app._nsapp.nsstatusitem
    nsapp = getattr(app, "_nsapp", None)
    if nsapp is not None:
        item = getattr(nsapp, "nsstatusitem", None)
        if item and hasattr(item, "button"):
            return item.button()
    # 兜底：扫一遍 app 所有属性，找一个 .button() 看起来对的
    for name in dir(app):
        if name.startswith("__"):
            continue
        try:
            item = getattr(app, name)
        except Exception:
            continue
        if item is not None and hasattr(item, "button") and callable(getattr(item, "button", None)):
            try:
                btn = item.button()
                if hasattr(btn, "setTitle_") and hasattr(btn, "setImage_"):
                    return btn
            except Exception:
                continue
    return None


def _set_bar_title(app, text):
    """纯文字标题（用作 SF Symbol 不可用时的兜底）。"""
    btn = _status_button(app)
    if btn is not None:
        btn.setImage_(None)
        btn.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_(""))
        btn.setTitle_(text)
        btn.setImagePosition_(0)  # NSNoImage
        return
    app.title = text


def _sf_battery_image(pct, point_size=14):
    """返回对应百分比的 SF Symbol 电池 NSImage（5 档量化）。

    粒度：0(<13) / 25 / 50 / 75 / 100(≥88)。
    不在这里上色——会作为 template 一起整合进 composite，由 AppKit 在状态
    栏上下文里和系统 Wi-Fi、电池等一起决定实际颜色（vibrancy/明暗自适应）。
    """
    if pct >= 88:
        name = "battery.100"
    elif pct >= 63:
        name = "battery.75"
    elif pct >= 38:
        name = "battery.50"
    elif pct >= 13:
        name = "battery.25"
    else:
        name = "battery.0"
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
    if img is None:
        return None
    cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        point_size, AppKit.NSFontWeightMedium
    )
    return img.imageWithSymbolConfiguration_(cfg)


def _battery_attachment(pct, font):
    """SF Symbol 电池包成 NSTextAttachment，可塞进 NSAttributedString 里跟文字一行排。

    image 设 template，菜单栏会把它当系统图标处理（vibrancy + 亮暗自适应），
    跟 Wi-Fi / 系统电池图标在同一渲染通道。
    """
    bat = _sf_battery_image(pct)
    if bat is None:
        return None
    bat.setTemplate_(True)
    attach = AppKit.NSTextAttachment.alloc().init()
    attach.setImage_(bat)
    sz = bat.size()
    # 垂直微调：让电池中线大致对齐文字中线
    y_offset = (font.capHeight() - sz.height) / 2
    attach.setBounds_(AppKit.NSMakeRect(0, y_offset, sz.width, sz.height))
    return AppKit.NSAttributedString.attributedStringWithAttachment_(attach)


def _render_attributed_title(items):
    """构建状态栏 attributed title：文字交给 NSStatusBarButton 原生渲染（拿到
    系统 vibrancy 和亮暗自适应），电池作为内联 template image 附件。

    旧方案是把整条画成位图（NSImage.lockFocus + labelColor），但 bitmap 里
    的文字是一次性栅格化的灰度，拿不到状态栏文字的 vibrancy，视觉上比系统
    时钟、菜单文字偏暗。
    """
    font = AppKit.NSFont.menuBarFontOfSize_(0)
    text_attrs = {AppKit.NSFontAttributeName: font}
    mas = AppKit.NSMutableAttributedString.alloc().init()

    def append_text(s):
        mas.appendAttributedString_(
            AppKit.NSAttributedString.alloc().initWithString_attributes_(s, text_attrs)
        )

    for i, (label, value, kind, err) in enumerate(items):
        prefix = " " if i > 0 else ""
        if err:
            append_text(f"{prefix}{label} ⚠️")
            continue
        if kind == "percent":
            append_text(f"{prefix}{label} {value}% ")
            bat_attach = _battery_attachment(value, font)
            if bat_attach is not None:
                mas.appendAttributedString_(bat_attach)
        else:
            append_text(f"{prefix}{label} {value}")

    if mas.length() == 0:
        append_text("ai-limit ⚠️")
    return mas


def _set_bar_with_batteries(app, items):
    """把 attributed title（文字 + 电池附件）安到状态栏按钮上。"""
    btn = _status_button(app)
    if btn is None:
        raise RuntimeError("no status button")
    btn.setImage_(None)
    btn.setTitle_("")
    btn.setAttributedTitle_(_render_attributed_title(items))


def _set_bar_icon(app):
    """系统栏只放一个 template icon；额度细节交给浮窗。"""
    btn = _status_button(app)
    if btn is None:
        app.title = "AI"
        return
    img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("gauge.with.dots.needle.67percent", "AI Limit")
    if img is None:
        img = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_("gauge", "AI Limit")
    if img is None:
        btn.setImage_(None)
        btn.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_(""))
        btn.setTitle_("AI")
        return
    cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(
        14, AppKit.NSFontWeightSemibold
    )
    img = img.imageWithSymbolConfiguration_(cfg) or img
    img.setTemplate_(True)
    btn.setTitle_("")
    btn.setAttributedTitle_(AppKit.NSAttributedString.alloc().initWithString_(""))
    btn.setImage_(img)
    btn.setImagePosition_(getattr(AppKit, "NSImageOnly", 1))

def _noop(_):
    """无副作用 callback，仅用于让 macOS 把无动作菜单项也按常规文字色渲染。
    AppKit 会把 NSMenuItem.target=nil 的项自动灰化，setEnabled_(True) 也救不了；
    挂一个真实 callback（哪怕什么都不做）才会让 macOS 视为正常项。"""
    pass


def _disable(menu_item):
    """让菜单项显式灰色（仅用于'上次刷新'这种刻意的次要信息）。"""
    menu_item._menuitem.setEnabled_(False)
    return menu_item


def _inert(menu_item):
    """挂 no-op callback，让 macOS 按常规文字色渲染（不灰），点击无效果。"""
    menu_item.set_callback(_noop)
    return menu_item

def _detail_text(mode, pct, reset, lang):
    if lang == "en":
        return f"  {mode}\t{pct:>3}% left   \t↻ {reset}"
    return f"  {mode}\t{pct:>3}% 剩余\t↻ {reset}"


def _appkit_color(hex_color, alpha=1.0):
    hex_color = hex_color.lstrip("#")
    red = int(hex_color[0:2], 16) / 255
    green = int(hex_color[2:4], 16) / 255
    blue = int(hex_color[4:6], 16) / 255
    return AppKit.NSColor.colorWithCalibratedRed_green_blue_alpha_(red, green, blue, alpha)


def _window_style_mask(*names):
    style = 0
    for name in names:
        style |= getattr(AppKit, f"NSWindowStyleMask{name}", getattr(AppKit, f"NS{name}WindowMask", 0))
    return style


def _fmt_widget_pct(value, lang="zh", remaining=True):
    if value is None:
        return "?"
    label = _tr(lang, "剩余", "left") if remaining else _tr(lang, "已用", "used")
    try:
        value = int(round(float(value)))
    except Exception:
        return f"{label} {value}%"
    return f"{label} {value}%"


def _fmt_widget_reset_epoch_or_iso(value, lang="zh"):
    if not value:
        return "?"
    if isinstance(value, (int, float)) or str(value).isdigit():
        return _fmt_reset_epoch(value, lang)
    formatted = _fmt_reset_iso(value, lang)
    return str(value) if formatted == "?" else formatted


def _short_widget_error(value):
    text = str(value or "")
    return text if len(text) <= 86 else text[:83] + "..."


def _widget_pct_value(value, default=0):
    try:
        return max(0, min(100, int(round(float(value)))))
    except Exception:
        return default


def _has_windowed_quota(data):
    for bucket in data.get("buckets") or []:
        if bucket.get("disabled"):
            continue
        window = str(bucket.get("window") or "").lower()
        name = " ".join(str(bucket.get(key) or "") for key in ("display_name", "bucket_name", "group_display_name", "model_id")).lower()
        if window or "5h" in name or "5 hour" in name or "weekly" in name or "week" in name:
            return True
    return False


def _widget_risk_color(pct):
    pct = _widget_pct_value(pct)
    if pct <= 5:
        return "#fb7185"
    if pct <= 20:
        return "#fbbf24"
    return "#4ade80"


def _widget_risk_tone(pct):
    pct = _widget_pct_value(pct)
    if pct <= 5:
        return "#3f1d25", "#7f1d1d", "#fecdd3", "exclamationmark.triangle.fill"
    if pct <= 20:
        return "#3a2c16", "#854d0e", "#fde68a", "exclamationmark.circle.fill"
    return "#143320", "#166534", "#bbf7d0", "checkmark.circle.fill"


def _short_widget_name(value, limit=38):
    text = str(value or "?")
    return text if len(text) <= limit else text[: limit - 1] + "…"

# ── 主 App ────────────────────────────────────────────────────────────────────

class AiLimitApp(rumps.App):
    def __init__(self):
        super().__init__("…", quit_button=None)
        self._state = _load_state()
        self._claude = None
        self._codex  = None
        self._deepseek = None
        self._google = None
        self._gemini = None
        self._llm_api = None
        self._widget_panel = None
        self._widget_content = None
        self._widget_last_layout_size = None
        # 后台线程把抓取结果放这里，由主线程的 _apply_pending 定时器接力
        self._pending = None
        self._pending_lock = threading.Lock()
        self._build_menu()

    # ── 菜单构建 ──────────────────────────────────────────────────────────────

    def _build_menu(self):
        lang = self._state["lang"]

        # Claude 区块（段头 + 详情都挂 no-op callback 避免 macOS 自动灰化）
        self._claude_header = _inert(rumps.MenuItem("Claude Code"))
        self._claude_source = _inert(rumps.MenuItem("  source  …"))
        self._claude_5h     = _inert(rumps.MenuItem("  5h  …"))
        self._claude_7d     = _inert(rumps.MenuItem("  7d  …"))

        # CodeX 区块
        self._codex_header = _inert(rumps.MenuItem("CodeX"))
        self._codex_source = _inert(rumps.MenuItem("  source  …"))
        self._codex_5h     = _inert(rumps.MenuItem("  5h  …"))
        self._codex_7d     = _inert(rumps.MenuItem("  7d  …"))
        self._codex_bucket_items = [
            _inert(rumps.MenuItem("  bucket  …")) for _ in range(8)
        ]

        self._deepseek_header  = _inert(rumps.MenuItem("DeepSeek"))
        self._deepseek_source  = _inert(rumps.MenuItem("  source  …"))
        self._deepseek_main    = _inert(rumps.MenuItem("  balance  …"))
        self._deepseek_detail  = _inert(rumps.MenuItem("  detail  …"))

        self._google_header    = _inert(rumps.MenuItem("Google"))
        self._google_source    = _inert(rumps.MenuItem("  source  …"))
        self._google_main      = _inert(rumps.MenuItem("  quota  …"))
        self._google_detail    = _inert(rumps.MenuItem("  detail  …"))
        self._google_bucket_items = [
            _inert(rumps.MenuItem("  bucket  …")) for _ in range(8)
        ]

        self._gemini_header    = _inert(rumps.MenuItem("Gemini App"))
        self._gemini_source    = _inert(rumps.MenuItem("  source  …"))
        self._gemini_main      = _inert(rumps.MenuItem("  quota  …"))
        self._gemini_detail    = _inert(rumps.MenuItem("  detail  …"))
        self._gemini_items = [
            _inert(rumps.MenuItem("  item  …")) for _ in range(8)
        ]

        # 上次刷新（次要信息，刻意灰色）
        self._last_refresh = _disable(rumps.MenuItem("…"))

        # 默认窗口子菜单
        self._mode_5h = rumps.MenuItem("5 小时" if lang == "zh" else "5 hours",
                                       callback=self._set_mode_5h)
        self._mode_7d = rumps.MenuItem("7 天" if lang == "zh" else "7 days",
                                       callback=self._set_mode_7d)
        mode_label = "默认窗口" if lang == "zh" else "Default window"
        self._mode_menu = rumps.MenuItem(mode_label)
        self._mode_menu.add(self._mode_5h)
        self._mode_menu.add(self._mode_7d)

        # 语言子菜单
        self._lang_zh = rumps.MenuItem("中文", callback=self._set_lang_zh)
        self._lang_en = rumps.MenuItem("English", callback=self._set_lang_en)
        lang_label = "语言" if lang == "zh" else "Language"
        self._lang_menu = rumps.MenuItem(lang_label)
        self._lang_menu.add(self._lang_zh)
        self._lang_menu.add(self._lang_en)

        # 监控服务子菜单
        self._svc_claude = rumps.MenuItem("Claude Code", callback=self._toggle_claude)
        self._svc_codex  = rumps.MenuItem("CodeX",       callback=self._toggle_codex)
        self._svc_deepseek = rumps.MenuItem("DeepSeek",  callback=self._toggle_deepseek)
        self._svc_google = rumps.MenuItem("Google", callback=self._toggle_google)
        self._svc_gemini = rumps.MenuItem("Gemini App", callback=self._toggle_gemini)
        self._svc_llm_api = rumps.MenuItem("LLM API", callback=self._toggle_llm_api)
        svc_label = "监控服务" if lang == "zh" else "Services"
        self._svc_menu = rumps.MenuItem(svc_label)
        self._svc_menu.add(self._svc_claude)
        self._svc_menu.add(self._svc_codex)
        self._svc_menu.add(self._svc_deepseek)
        self._svc_menu.add(self._svc_google)
        self._svc_menu.add(self._svc_gemini)
        self._svc_menu.add(self._svc_llm_api)

        # 开机自启
        self._login_item = rumps.MenuItem(
            "开机自启" if lang == "zh" else "Launch at Login",
            callback=self._toggle_login_item,
        )
        self._update_login_item_check()

        # 操作项
        self._refresh_item = rumps.MenuItem(
            "立即刷新" if lang == "zh" else "Refresh now",
            callback=self._force_refresh,
        )
        self._widget_item = rumps.MenuItem(
            "打开独立额度浮窗" if lang == "zh" else "Open quota widget",
            callback=self._toggle_widget,
        )
        self._codex_dash = rumps.MenuItem(
            "打开 CodeX 分析页" if lang == "zh" else "Open CodeX analytics",
            callback=lambda _: webbrowser.open("https://chatgpt.com/codex/cloud/settings/analytics"),
        )
        self._claude_dash = rumps.MenuItem(
            "打开 Claude 用量页" if lang == "zh" else "Open Claude usage",
            callback=lambda _: webbrowser.open("https://claude.ai/settings/usage"),
        )
        self._deepseek_dash = rumps.MenuItem(
            "打开 DeepSeek 用量页" if lang == "zh" else "Open DeepSeek usage",
            callback=lambda _: webbrowser.open(_DEEPSEEK_USAGE_URL),
        )
        self._google_dash = rumps.MenuItem(
            "打开 Google 配额说明页" if lang == "zh" else "Open Google quota docs",
            callback=lambda _: webbrowser.open(_GOOGLE_QUOTA_DOCS_URL),
        )
        self._gemini_dash = rumps.MenuItem(
            "打开 Gemini App 用量页" if lang == "zh" else "Open Gemini App usage",
            callback=lambda _: webbrowser.open(_GEMINI_APP_USAGE_URL),
        )

        # 项目信息子菜单
        about_label = f"项目信息（ai-limit {__version__}）" if lang == "zh" else f"Project (ai-limit {__version__})"
        self._about_menu   = rumps.MenuItem(about_label)
        self._about_repo   = rumps.MenuItem(
            "打开项目仓库" if lang == "zh" else "Open project repository",
            callback=lambda _: webbrowser.open(_PROJECT_URL),
        )
        self._about_ver    = _disable(rumps.MenuItem(
            f"版本：ai-limit {__version__}" if lang == "zh" else f"Version: ai-limit {__version__}"
        ))
        self._about_scope  = _disable(rumps.MenuItem(
            "监控：Claude / CodeX / DeepSeek / Google / Gemini App / LLM API" if lang == "zh" else "Monitors: Claude / CodeX / DeepSeek / Google / Gemini App / LLM API"
        ))
        self._about_surfaces = _disable(rumps.MenuItem(
            "界面：菜单栏 / CLI / daemon" if lang == "zh" else "Surfaces: menu bar / CLI / daemon"
        ))
        self._about_status = _disable(rumps.MenuItem(
            "状态：当前版本已接入 Google、Gemini App 与 LLM API 配额" if lang == "zh" else "Status: current build includes Google, Gemini App, and LLM API quota"
        ))
        self._about_menu.add(self._about_repo)
        self._about_menu.add(self._about_ver)
        self._about_menu.add(self._about_scope)
        self._about_menu.add(self._about_surfaces)
        self._about_menu.add(self._about_status)

        # 退出
        self._quit_item = rumps.MenuItem(
            "退出" if lang == "zh" else "Quit",
            callback=rumps.quit_application,
        )

        self.menu = [
            self._widget_item,
            self._refresh_item,
            None,
            self._mode_menu,
            self._lang_menu,
            self._svc_menu,
            self._login_item,
            None,
            self._codex_dash,
            self._claude_dash,
            self._deepseek_dash,
            self._google_dash,
            self._gemini_dash,
            None,
            self._about_menu,
            None,
            self._quit_item,
        ]
        # NSMenu otherwise shrinks to the longest localized label, so the
        # Chinese and English panels visibly jump between different widths.
        self.menu._menu.setMinimumWidth_(_MENU_MIN_WIDTH)
        self._update_mode_checks()
        self._update_lang_checks()
        self._update_service_checks()

    # ── 数据更新 ──────────────────────────────────────────────────────────────
    #
    # 原则：网络抓取一律在后台线程跑，绝对不阻塞主 UI 线程，否则切换菜单时
    # macOS 会显示转圈光标。
    # 流程：
    #   主线程触发    → 立即用 _load_cache() 重画一次（瞬时响应）
    #                → 启动后台线程 _async_refresh()
    #   后台线程     → 调 _fetch_claude / _fetch_codex（耗时几秒）
    #                → 把结果塞进 self._pending（加锁）
    #   主线程定时器 → _apply_pending 每 0.4s 检查 _pending，有就 apply + 重画

    @rumps.timer(0.3)
    def _init_render(self, sender):
        """启动后立即用缓存重画 + 后台拉一次最新数据。"""
        self._refresh_from_cache()
        self._show_widget_if_enabled()
        self._kick_background_fetch()
        sender.stop()

    @rumps.timer(_REFRESH_SEC)
    def _auto_refresh(self, _):
        """每 60s 后台拉一次。"""
        self._kick_background_fetch()

    @rumps.timer(0.35)
    def _widget_resize_tick(self, _):
        """浮窗尺寸变化时重排 dashboard；不触发任何网络刷新。"""
        if not self._widget_is_visible() or self._widget_panel is None:
            return
        size = self._widget_panel.contentView().bounds().size
        current = (int(size.width), int(size.height))
        if self._widget_last_layout_size != current:
            self._render_widget()

    @rumps.timer(0.4)
    def _apply_pending(self, _):
        """主线程接力点：把后台线程取到的数据 apply 到 UI。

        重点：服务被禁用时不要清空内存里的旧数据。后台线程对禁用服务返回
        None 表示"没拉新的"，不是"清空"——保留上次的值，重新启用时菜单栏
        瞬间显示该服务的最近一次缓存，避免 1-2s 网络抓取的等待感。
        """
        with self._pending_lock:
            pending = self._pending
            self._pending = None
        if pending is None:
            return
        claude, codex, deepseek, google, gemini, llm_api = pending
        if claude is not None:
            self._claude = claude
        if codex is not None:
            if _codex_transient_error(codex) and isinstance(self._codex, dict) and not self._codex.get("error"):
                codex = None
            elif _codex_unstable_sample(self._codex, codex):
                codex = None
            else:
                self._codex = codex
        if deepseek is not None:
            self._deepseek = deepseek
        if google is not None:
            self._google = google
        if gemini is not None:
            self._gemini = gemini
        if llm_api is not None:
            self._llm_api = llm_api
        _save_cache(
            self._claude,
            {
                "codex": self._codex,
                "deepseek": self._deepseek,
                "google": self._google,
                "gemini": self._gemini,
                "llm_api": self._llm_api,
            },
        )
        self._render()

    def _refresh_from_cache(self):
        """主线程瞬时操作：读短缓存重画，不碰网络。"""
        claude, cached = _load_cache()
        codex = None
        deepseek = None
        google = None
        gemini = None
        llm_api = None
        if isinstance(cached, dict) and (
            "codex" in cached or "deepseek" in cached or "google" in cached or "gemini" in cached or "llm_api" in cached
        ):
            codex = cached.get("codex")
            deepseek = cached.get("deepseek")
            google = cached.get("google")
            gemini = cached.get("gemini")
            llm_api = cached.get("llm_api")
        else:
            codex = cached
        # 不按 services 过滤——内存里保留两份数据，UI 显示由 _render 控
        if claude is not None:
            self._claude = claude
        if codex is not None:
            self._codex = codex
        if deepseek is not None:
            self._deepseek = deepseek
        if google is not None:
            self._google = google
        if gemini is not None:
            self._gemini = gemini
        if llm_api is not None:
            self._llm_api = llm_api
        self._render()

    def _kick_background_fetch(self):
        """启动后台线程抓数据；线程内不要碰任何 UI 对象。"""
        t = threading.Thread(target=self._async_refresh, daemon=True)
        t.start()

    def _async_refresh(self):
        """后台线程：抓数据 → 写共享变量。不能调任何 rumps/AppKit UI。"""
        lang = self._state["lang"]
        services = self._state.get("services") or list(_SERVICES)
        claude = _fetch_claude(lang) if "claude" in services else None
        codex  = _fetch_codex(lang)  if "codex"  in services else None
        deepseek = _fetch_deepseek(lang) if "deepseek" in services else None
        google = _fetch_google(lang) if "google" in services else None
        gemini = _fetch_gemini_app(lang) if "gemini" in services else None
        llm_api = _fetch_llm_api(lang) if "llm_api" in services else None
        with self._pending_lock:
            self._pending = (claude, codex, deepseek, google, gemini, llm_api)

    def _render(self):
        lang     = self._state["lang"]
        mode     = self._state["global"]
        services = self._state.get("services") or list(_SERVICES)
        show_claude = "claude" in services
        show_codex  = "codex"  in services
        show_deepseek = "deepseek" in services
        show_google = "google" in services
        show_gemini = "gemini" in services
        show_llm_api = "llm_api" in services
        claude = self._claude or {}
        codex  = self._codex  or {}
        deepseek = self._deepseek or {}
        google = self._google or {}
        gemini = self._gemini or {}
        llm_api = self._llm_api or {}

        # 系统栏只显示入口图标；额度细节交给独立浮窗。
        _set_bar_icon(self)

        # Claude 区块 —— 服务被关时整段隐藏
        self._claude_header._menuitem.setHidden_(not show_claude)
        self._claude_source._menuitem.setHidden_(not show_claude)
        self._claude_5h._menuitem.setHidden_(not show_claude)
        self._claude_7d._menuitem.setHidden_(not show_claude)
        if show_claude:
            if "error" in claude:
                self._claude_header.title = "Claude Code ⚠️"
                self._claude_source.title = _tr(lang, "  来源：browser error", "  Source: browser error")
                self._claude_5h.title = f"  {claude['error'][:60]}"
                self._claude_7d._menuitem.setHidden_(True)
            elif claude:
                plan = _fmt_plan(claude.get("plan"), lang)
                self._claude_header.title = f"Claude Code{plan}"
                self._claude_source.title = _tr(lang, "  来源：browser live", "  Source: browser live")
                c5_reset = _fmt_reset_iso(claude["5h_reset"], lang)
                c7_reset = _fmt_reset_iso(claude["7d_reset"], lang)
                self._claude_5h.title = _detail_text("5h", claude["5h_left"], c5_reset, lang)
                self._claude_7d.title = _detail_text("7d", claude["7d_left"], c7_reset, lang)

        # CodeX 区块
        self._codex_header._menuitem.setHidden_(not show_codex)
        self._codex_source._menuitem.setHidden_(not show_codex)
        self._codex_5h._menuitem.setHidden_(not show_codex)
        self._codex_7d._menuitem.setHidden_(not show_codex)
        for item in self._codex_bucket_items:
            item._menuitem.setHidden_(not show_codex)
        if show_codex:
            if "error" in codex:
                self._codex_header.title = "CodeX ⚠️"
                self._codex_source.title = _tr(lang, "  来源：browser error", "  Source: browser error")
                self._codex_5h.title = f"  {codex['error'][:60]}"
                self._codex_7d._menuitem.setHidden_(True)
                for item in self._codex_bucket_items:
                    item._menuitem.setHidden_(True)
            elif codex:
                plan = _fmt_plan(codex.get("plan"), lang)
                self._codex_header.title = f"CodeX{plan}"
                source = codex.get("source") or "unknown"
                source_label = {
                    "web": _tr(lang, "browser live", "browser live"),
                    "snapshot": _tr(lang, "local snapshot", "local snapshot"),
                    "live": _tr(lang, "codex app-server", "codex app-server"),
                }.get(source, source)
                self._codex_source.title = _tr(lang, f"  来源：{source_label}", f"  Source: {source_label}")
                x7_reset = _fmt_reset_epoch(codex["7d_reset"], lang)
                if codex.get("5h_left") is None:
                    self._codex_5h._menuitem.setHidden_(True)
                else:
                    x5_reset = _fmt_reset_epoch(codex.get("5h_reset"), lang)
                    self._codex_5h.title = _detail_text("5h", codex["5h_left"], x5_reset, lang)
                    self._codex_5h._menuitem.setHidden_(False)
                self._codex_7d.title = _detail_text("7d", codex["7d_left"], x7_reset, lang)
                buckets = codex.get("buckets") or []
                for index, item in enumerate(self._codex_bucket_items):
                    if index >= len(buckets):
                        item._menuitem.setHidden_(True)
                        continue
                    bucket = buckets[index]
                    group_name = bucket.get("group_display_name") or ""
                    bucket_name = bucket.get("display_name") or bucket.get("window") or "limit"
                    pct = bucket.get("remaining_percent")
                    if pct is None and bucket.get("used_percent") is not None:
                        pct = max(0, min(100, int(round(100 - bucket.get("used_percent")))))
                    pct_text = "?" if pct is None else f"{pct}%"
                    reset = bucket.get("resets_at") or bucket.get("reset_time")
                    reset_text = _fmt_reset_epoch(reset, lang) if reset else "?"
                    label = f"{group_name} / {bucket_name}" if group_name else bucket_name
                    item.title = _tr(
                        lang,
                        f"  {index + 1}. {label}\t{pct_text} 剩余\t↻ {reset_text}",
                        f"  {index + 1}. {label}\t{pct_text} left\t↻ {reset_text}",
                    )
                    item._menuitem.setHidden_(False)

        self._deepseek_header._menuitem.setHidden_(not show_deepseek)
        self._deepseek_source._menuitem.setHidden_(not show_deepseek)
        self._deepseek_main._menuitem.setHidden_(not show_deepseek)
        self._deepseek_detail._menuitem.setHidden_(not show_deepseek)
        if show_deepseek:
            if "error" in deepseek:
                self._deepseek_header.title = "DeepSeek ⚠️"
                self._deepseek_source.title = _tr(lang, "  来源：api key error", "  Source: api key error")
                self._deepseek_main.title = f"  {deepseek['error'][:60]}"
                self._deepseek_detail._menuitem.setHidden_(True)
            elif deepseek:
                primary = deepseek.get("primary") or {}
                currency = primary.get("currency", "USD")
                total = _fmt_balance_short(primary)
                granted = fmt_money(primary.get("granted_balance", "0"), currency)
                topped = fmt_money(primary.get("topped_up_balance", "0"), currency)
                available = _tr(lang, "可用", "available") if deepseek.get("available") else _tr(lang, "余额不足", "insufficient")
                self._deepseek_header.title = "DeepSeek"
                self._deepseek_source.title = _tr(lang, "  来源：api key live", "  Source: api key live")
                self._deepseek_main.title = _tr(lang, f"  余额\t{total}\t{available}", f"  Balance\t{total}\t{available}")
                self._deepseek_detail.title = _tr(lang, f"  赠送 {granted}  |  充值 {topped}", f"  Granted {granted}  |  Topped-up {topped}")

        self._google_header._menuitem.setHidden_(not show_google)
        self._google_source._menuitem.setHidden_(not show_google)
        self._google_main._menuitem.setHidden_(not show_google)
        self._google_detail._menuitem.setHidden_(not show_google)
        for item in self._google_bucket_items:
            item._menuitem.setHidden_(not show_google)
        if show_google:
            if "error" in google:
                self._google_header.title = "Google ⚠️"
                self._google_source.title = _tr(lang, "  来源：oauth error", "  Source: oauth error")
                self._google_main.title = f"  {google['error'][:60]}"
                self._google_detail._menuitem.setHidden_(True)
                for item in self._google_bucket_items:
                    item._menuitem.setHidden_(True)
            elif google:
                daily = google.get("daily_left")
                daily_text = "?" if daily is None else f"{daily}%"
                primary_model = google.get("primary_model") or "?"
                bucket_count = google.get("bucket_count", 0)
                quota_groups = google.get("quota_groups") or []
                any_empty = any(
                    bucket.get("remaining_percent") == 0 and not bucket.get("disabled")
                    for bucket in (google.get("buckets") or [])
                )
                self._google_header.title = "Google ⚠️" if any_empty else "Google"
                source = google.get("source") or ""
                source_label = "agy usage fallback" if "agy /usage" in source else ("antigravity app live" if quota_groups else "antigravity fallback")
                self._google_source.title = _tr(lang, f"  来源：{source_label}", f"  Source: {source_label}")
                if quota_groups:
                    group_count = google.get("group_count") or len(quota_groups)
                    self._google_main.title = _tr(
                        lang,
                        f"  Model Quota\t{daily_text}\t{primary_model}",
                        f"  Model Quota\t{daily_text}\t{primary_model}",
                    )
                    self._google_detail.title = _tr(
                        lang,
                        f"  {group_count} 个额度组  |  {bucket_count} 个窗口",
                        f"  {group_count} groups  |  {bucket_count} windows",
                    )
                else:
                    self._google_main.title = _tr(lang, f"  日额度\t{daily_text}\t{primary_model}", f"  Daily\t{daily_text}\t{primary_model}")
                    self._google_detail.title = _tr(lang, f"  fallback  |  {bucket_count} 项", f"  fallback  |  {bucket_count} items")
                buckets = google.get("buckets") or []
                for index, item in enumerate(self._google_bucket_items):
                    if index >= len(buckets):
                        item._menuitem.setHidden_(True)
                        continue
                    bucket = buckets[index]
                    model_id = " / ".join(
                        value for value in (
                            bucket.get("group_display_name"),
                            bucket.get("display_name") or bucket.get("model_id"),
                        )
                        if value
                    ) or "?"
                    pct = bucket.get("remaining_percent")
                    pct_text = "?" if pct is None else f"{pct}%"
                    if bucket.get("disabled"):
                        pct_text += _tr(lang, " 不适用", " disabled")
                    reset = bucket.get("reset_time")
                    reset_bucket_text = _fmt_reset_iso(reset, lang) if reset else "?"
                    item.title = _tr(
                        lang,
                        f"  {index + 1}. {model_id}\t{pct_text}\t↻ {reset_bucket_text}",
                        f"  {index + 1}. {model_id}\t{pct_text}\t↻ {reset_bucket_text}",
                    )
                    item._menuitem.setHidden_(False)

        self._gemini_header._menuitem.setHidden_(not show_gemini)
        self._gemini_source._menuitem.setHidden_(not show_gemini)
        self._gemini_main._menuitem.setHidden_(not show_gemini)
        self._gemini_detail._menuitem.setHidden_(not show_gemini)
        for item in self._gemini_items:
            item._menuitem.setHidden_(not show_gemini)
        if show_gemini:
            if "error" in gemini:
                self._gemini_header.title = "Gemini App ⚠️"
                self._gemini_source.title = _tr(lang, "  来源：browser cookie error", "  Source: browser cookie error")
                self._gemini_main.title = f"  {gemini['error'][:60]}"
                self._gemini_detail._menuitem.setHidden_(True)
                for item in self._gemini_items:
                    item._menuitem.setHidden_(True)
            elif gemini:
                left = gemini.get("left")
                used = gemini.get("used")
                left_text = "?" if left is None else f"{left}%"
                used_text = "?" if used is None else f"{used}%"
                unavailable = gemini.get("unavailable_reason")
                self._gemini_header.title = "Gemini App ⚠️" if unavailable else "Gemini App"
                self._gemini_source.title = _tr(lang, "  来源：gemini app live", "  Source: gemini app live")
                self._gemini_main.title = _tr(
                    lang,
                    f"  Usage\t已用 {used_text}\t剩余 {left_text}",
                    f"  Usage\tused {used_text}\tleft {left_text}",
                )
                self._gemini_detail.title = _tr(
                    lang,
                    f"  {gemini.get('bucket_count', 0)} 个额度项  |  {gemini.get('model_count', 0)} 个模型入口",
                    f"  {gemini.get('bucket_count', 0)} quota items  |  {gemini.get('model_count', 0)} model entries",
                )
                rows = []
                if unavailable:
                    rows.append({"text": _tr(lang, f"页面未返回额度：{unavailable}", f"Usage page did not return quota: {unavailable}")})
                for bucket in gemini.get("buckets") or []:
                    pct = bucket.get("remaining_percent")
                    used_pct = bucket.get("used_percent")
                    pct_text = "?" if pct is None else f"剩余 {pct}%"
                    if used_pct is not None:
                        pct_text = _tr(lang, f"已用 {used_pct}% / {pct_text}", f"used {used_pct}% / left {pct}%")
                    reset = bucket.get("reset_time")
                    reset_text = _fmt_reset_iso(reset, lang) if reset else (bucket.get("reset_text") or "?")
                    rows.append({"text": f"{bucket.get('display_name') or 'Gemini App quota'}\t{pct_text}\t↻ {reset_text}"})
                if not rows:
                    for model in (gemini.get("models") or [])[:8]:
                        name = model.get("display_name") or model.get("label") or model.get("model_id") or "?"
                        desc = model.get("description") or ""
                        rows.append({"text": f"{name}\t{desc}"})
                for index, item in enumerate(self._gemini_items):
                    if index >= len(rows):
                        item._menuitem.setHidden_(True)
                        continue
                    item.title = f"  {index + 1}. {rows[index]['text']}"
                    item._menuitem.setHidden_(False)

        # 刷新时间
        self._apply_compact_menu(
            show_claude,
            show_codex,
            show_deepseek,
            show_google,
            show_gemini,
            claude,
            codex,
            deepseek,
            google,
            gemini,
        )
        now = datetime.datetime.now(TZ_LOCAL).strftime("%H:%M:%S")
        self._last_refresh.title = _tr(lang, f"上次刷新: {now}", f"Last refresh: {now}")
        self._render_widget()

    def _apply_compact_menu(self, show_claude, show_codex, show_deepseek, show_google, show_gemini, claude, codex, deepseek, google, gemini):
        # The floating widget is now the quota surface. Keep the menu as an
        # action panel only, so it does not repeat incomplete group summaries.
        self._last_refresh._menuitem.setHidden_(True)
        self._claude_header._menuitem.setHidden_(True)
        self._claude_source._menuitem.setHidden_(True)
        self._claude_5h._menuitem.setHidden_(True)
        self._claude_7d._menuitem.setHidden_(True)

        self._codex_header._menuitem.setHidden_(True)
        self._codex_source._menuitem.setHidden_(True)
        self._codex_5h._menuitem.setHidden_(True)
        self._codex_7d._menuitem.setHidden_(True)
        for item in self._codex_bucket_items:
            item._menuitem.setHidden_(True)

        self._deepseek_header._menuitem.setHidden_(True)
        self._deepseek_source._menuitem.setHidden_(True)
        self._deepseek_main._menuitem.setHidden_(True)
        self._deepseek_detail._menuitem.setHidden_(True)

        self._google_header._menuitem.setHidden_(True)
        self._google_source._menuitem.setHidden_(True)
        self._google_main._menuitem.setHidden_(True)
        self._google_detail._menuitem.setHidden_(True)
        for item in self._google_bucket_items:
            item._menuitem.setHidden_(True)

        self._gemini_header._menuitem.setHidden_(True)
        self._gemini_source._menuitem.setHidden_(True)
        self._gemini_main._menuitem.setHidden_(True)
        self._gemini_detail._menuitem.setHidden_(True)
        for item in self._gemini_items:
            item._menuitem.setHidden_(True)

    def _worst_menu_entry(self, entries):
        entries = entries or []
        return min(entries, key=lambda item: item.get("pct", 100)) if entries else None

    def _compact_menu_summary(self, entry, count=None):
        lang = self._state["lang"]
        if not entry:
            return _tr(lang, "  等待数据", "  Waiting")
        count_text = ""
        if count:
            count_text = _tr(lang, f"  ·  {count} 项", f"  ·  {count} items")
        name = _short_widget_name(entry.get("name"), 34)
        return _tr(lang, f"  最低 {entry.get('pct', '?')}%  ·  {name}{count_text}", f"  lowest {entry.get('pct', '?')}%  ·  {name}{count_text}")

    # ── 桌面浮窗 ────────────────────────────────────────────────────────────

    def _toggle_widget(self, _):
        self._ensure_widget()
        if self._widget_panel.isVisible():
            self._widget_panel.orderOut_(None)
            self._state["widget"] = False
        else:
            self._render_widget()
            AppKit.NSApp.activateIgnoringOtherApps_(True)
            self._widget_panel.makeKeyAndOrderFront_(None)
            self._state["widget"] = True
        _save_state(self._state)
        self._update_widget_item()

    def _show_widget_if_enabled(self):
        if not self._state.get("widget", True):
            self._update_widget_item()
            return
        self._ensure_widget()
        self._render_widget()
        self._widget_panel.makeKeyAndOrderFront_(None)
        self._update_widget_item()

    def _ensure_widget(self):
        if self._widget_panel is not None:
            return

        width, height = 760, 680
        screen = AppKit.NSScreen.mainScreen()
        visible = screen.visibleFrame() if screen is not None else AppKit.NSMakeRect(80, 80, width, height)
        origin_x = visible.origin.x + max(24, visible.size.width - width - 28)
        origin_y = visible.origin.y + max(24, visible.size.height - height - 36)
        frame = AppKit.NSMakeRect(origin_x, origin_y, width, height)
        style = _window_style_mask("Titled", "Closable", "Resizable", "Miniaturizable")
        panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            frame,
            style,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        panel.setTitle_("AI Limit")
        panel.setReleasedWhenClosed_(False)
        panel.setHidesOnDeactivate_(False)
        panel.setLevel_(getattr(AppKit, "NSFloatingWindowLevel", 3))
        panel.setMinSize_(AppKit.NSMakeSize(520, 520))
        panel.setBackgroundColor_(_appkit_color("#171717", 0.96))
        panel.setOpaque_(False)

        scroll = AppKit.NSScrollView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, height))
        scroll.setAutoresizingMask_(getattr(AppKit, "NSViewWidthSizable", 2) | getattr(AppKit, "NSViewHeightSizable", 16))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(getattr(AppKit, "NSNoBorder", 0))
        scroll.setDrawsBackground_(False)

        content = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(0, 0, width, height))
        content.setWantsLayer_(True)
        content.layer().setBackgroundColor_(_appkit_color("#171717", 0.96).CGColor())

        scroll.setDocumentView_(content)
        panel.setContentView_(scroll)
        self._widget_panel = panel
        self._widget_content = content
        self._render_widget()
        self._update_widget_item()

    def _widget_is_visible(self):
        return bool(self._widget_panel is not None and self._widget_panel.isVisible())

    def _update_widget_item(self):
        if not hasattr(self, "_widget_item"):
            return
        lang = self._state["lang"]
        self._widget_item.title = _tr(
            lang,
            "隐藏独立额度浮窗" if self._widget_is_visible() else "打开独立额度浮窗",
            "Hide quota widget" if self._widget_is_visible() else "Open quota widget",
        )

    def _render_widget(self):
        self._sync_codex_from_web_cache()
        if self._widget_content is not None:
            self._render_widget_dashboard()
        self._update_widget_item()

    def _sync_codex_from_web_cache(self):
        try:
            _claude, cached = _load_cache()
            codex = cached.get("codex") if isinstance(cached, dict) else cached
            if isinstance(codex, dict) and codex.get("source") == "web":
                self._codex = codex
        except Exception:
            pass

    def _clear_widget_content(self):
        for view in list(self._widget_content.subviews()):
            view.removeFromSuperview()

    def _widget_add_box(self, x, y, w, h, color, radius=10, border=None):
        view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, w, h))
        view.setWantsLayer_(True)
        layer = view.layer()
        layer.setBackgroundColor_(_appkit_color(color).CGColor())
        layer.setCornerRadius_(radius)
        if border:
            layer.setBorderColor_(_appkit_color(border).CGColor())
            layer.setBorderWidth_(1)
        self._widget_content.addSubview_(view)
        return view

    def _widget_add_label(self, text, x, y, w, h, size=12, weight="regular", color="#f4f4f5", align="left"):
        label = AppKit.NSTextField.labelWithString_(str(text))
        label.setFrame_(AppKit.NSMakeRect(x, y, w, h))
        label.setTextColor_(_appkit_color(color))
        font_weight = AppKit.NSFontWeightRegular
        if weight == "bold":
            font_weight = AppKit.NSFontWeightBold
        elif weight == "medium":
            font_weight = AppKit.NSFontWeightMedium
        label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(size, font_weight))
        label.setLineBreakMode_(getattr(AppKit, "NSLineBreakByTruncatingTail", 4))
        if align == "right":
            label.setAlignment_(getattr(AppKit, "NSTextAlignmentRight", 2))
        elif align == "center":
            label.setAlignment_(getattr(AppKit, "NSTextAlignmentCenter", 1))
        self._widget_content.addSubview_(label)
        return label

    def _widget_add_symbol(self, name, x, y, size=18, color="#ffffff"):
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, None)
        if image is None:
            return None
        image.setTemplate_(True)
        cfg = AppKit.NSImageSymbolConfiguration.configurationWithPointSize_weight_(size, AppKit.NSFontWeightSemibold)
        image = image.imageWithSymbolConfiguration_(cfg) or image
        image_view = AppKit.NSImageView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, size + 4, size + 4))
        image_view.setImage_(image)
        if hasattr(image_view, "setContentTintColor_"):
            image_view.setContentTintColor_(_appkit_color(color))
        self._widget_content.addSubview_(image_view)
        return image_view

    def _widget_add_progress(self, x, y, w, h, pct):
        pct = _widget_pct_value(pct)
        color = _widget_risk_color(pct)
        self._widget_add_box(x, y, w, h, "#2b2b2f", radius=h / 2)
        self._widget_add_box(x, y, max(h, w * pct / 100), h, color, radius=h / 2)

    def _widget_add_ring(self, x, y, size, pct):
        pct = _widget_pct_value(pct)
        view = AppKit.NSView.alloc().initWithFrame_(AppKit.NSMakeRect(x, y, size, size))
        view.setWantsLayer_(True)
        layer = view.layer()

        center = AppKit.NSMakePoint(size / 2, size / 2)
        radius = max(2, size / 2 - 3)
        track_path = AppKit.NSBezierPath.bezierPathWithOvalInRect_(AppKit.NSMakeRect(3, 3, size - 6, size - 6))
        arc_path = AppKit.NSBezierPath.bezierPath()
        arc_path.appendBezierPathWithArcWithCenter_radius_startAngle_endAngle_clockwise_(
            center, radius, 90, 90 - 360 * pct / 100, True
        )

        track = AppKit.CAShapeLayer.layer()
        track.setPath_(track_path.CGPath())
        track.setFillColor_(None)
        track.setStrokeColor_(_appkit_color("#3a3a40").CGColor())
        track.setLineWidth_(3)

        arc = AppKit.CAShapeLayer.layer()
        arc.setPath_(arc_path.CGPath())
        arc.setFillColor_(None)
        arc.setStrokeColor_(_appkit_color(_widget_risk_color(pct)).CGColor())
        arc.setLineWidth_(3)
        if hasattr(arc, "setLineCap_"):
            arc.setLineCap_("round")

        layer.addSublayer_(track)
        layer.addSublayer_(arc)
        self._widget_content.addSubview_(view)
        return view

    def _render_widget_dashboard(self):
        lang = self._state["lang"]
        cards = self._widget_summary_cards()
        alerts = self._widget_alert_rows()
        details = self._widget_detail_rows()
        bounds = self._widget_panel.contentView().bounds() if self._widget_panel is not None else AppKit.NSMakeRect(0, 0, 480, 560)
        content_w = max(360, int(bounds.size.width))
        self._widget_last_layout_size = (int(bounds.size.width), int(bounds.size.height))
        margin = 18
        gap = 14
        cols = 3 if content_w >= 960 else (2 if content_w >= 560 else 1)
        card_w = int((content_w - margin * 2 - gap * (cols - 1)) / cols)
        card_h = 100
        card_rows = max(1, (len(cards) + cols - 1) // cols)
        alerts_h = 0 if not alerts else 36 + min(len(alerts), 5) * 34
        details_h = 42 + sum(26 if row.get("type") == "section" else (42 if row.get("reset") else 34) for row in details)
        total_h = max(560, 70 + card_rows * (card_h + 12) + alerts_h + details_h + 34)
        self._widget_content.setFrame_(AppKit.NSMakeRect(0, 0, content_w, total_h))
        self._clear_widget_content()

        def y(top, h):
            return total_h - top - h

        top = 18
        self._widget_add_label("AI Limit", margin, y(top, 26), 170, 26, size=22, weight="bold")
        self._widget_add_label(
            _tr(lang, f"更新 {datetime.datetime.now(TZ_LOCAL):%H:%M:%S}", f"Updated {datetime.datetime.now(TZ_LOCAL):%H:%M:%S}"),
            max(margin + 170, content_w - 188),
            y(top + 3, 20),
            170,
            20,
            size=12,
            color="#a1a1aa",
            align="right",
        )
        top += 46

        for index, card in enumerate(cards):
            col = index % cols
            row = index // cols
            x = margin + col * (card_w + gap)
            cy = y(top + row * (card_h + 12), card_h)
            self._draw_widget_card(card, x, cy, card_w, card_h)
        top += card_rows * (card_h + 12) + 6

        if alerts:
            self._widget_add_label(_tr(lang, "需要注意", "Needs attention"), margin, y(top, 22), 180, 22, size=15, weight="bold")
            top += 30
            for alert in alerts[:5]:
                cy = y(top, 28)
                row_w = content_w - margin * 2
                self._widget_add_box(margin, cy, row_w, 28, alert["bg"], radius=8, border=alert["border"])
                self._widget_add_symbol(alert["symbol"], margin + 10, cy + 5, size=14, color=alert["fg"])
                self._widget_add_label(alert["text"], margin + 34, cy + 5, max(120, row_w - 120), 18, size=12, weight="medium", color=alert["fg"])
                self._widget_add_label(alert["value"], margin + row_w - 86, cy + 5, 72, 18, size=12, weight="bold", color=alert["fg"], align="right")
                top += 34
            top += 10

        self._widget_add_label(_tr(lang, "分组额度", "Quota groups"), margin, y(top, 22), 180, 22, size=15, weight="bold")
        top += 32
        value_w = 72
        progress_w = max(76, min(180, int((content_w - margin * 2) * 0.28)))
        name_x = margin + 4
        value_x = content_w - margin - value_w
        progress_x = value_x - progress_w - 10
        name_w = max(110, progress_x - name_x - 12)
        for row in details:
            if row.get("type") == "section":
                cy = y(top, 20)
                self._widget_add_label(row["name"], name_x, cy + 2, 200, 16, size=12, weight="bold", color=row["color"])
                self._widget_add_box(progress_x, cy + 8, max(40, content_w - progress_x - margin), 1, "#323238", radius=0)
                top += 26
                continue
            row_h = 42 if row.get("reset") else 28
            cy = y(top, row_h)
            self._widget_add_label(row["name"], name_x, cy + 8, name_w, 16, size=11, color="#d4d4d8")
            if row.get("reset"):
                self._widget_add_label(f"↻ {row['reset']}", name_x, cy - 5, name_w, 14, size=9, color="#8b8b93")
            self._widget_add_progress(progress_x, cy + 10, progress_w, 8, row["pct"])
            self._widget_add_label(row["value"], value_x, cy + 5, value_w, 18, size=12, weight="bold", color=row["color"], align="right")
            top += row_h + 6

    def _draw_widget_card(self, card, x, y, w, h):
        self._widget_add_box(x, y, w, h, card["bg"], radius=12, border=card["border"])
        self._widget_add_box(x + 12, y + h - 43, 32, 32, card["accent"], radius=16)
        self._widget_add_symbol(card["symbol"], x + 18, y + h - 37, size=18, color="#ffffff")
        self._widget_add_label(card["title"], x + 52, y + h - 31, w - 68, 18, size=12, weight="bold")

        metrics = card.get("metrics") or []
        if metrics:
            reset_text = self._card_reset_text(metrics)
            if reset_text:
                self._widget_add_label(reset_text, x + 58, y + h - 47, max(80, w - 72), 14, size=9, color="#8b8b93")
            row_x = x + 16
            row_w = max(120, w - 30)
            value_w = 44
            label_w = 28
            bar_x = row_x + label_w + 6
            bar_w = max(46, row_w - label_w - value_w - 14)
            first_y = y + 37
            for index, metric in enumerate(metrics[:2]):
                row_y = first_y - index * 21
                self._widget_add_label(metric["label"], row_x, row_y, label_w, 13, size=10, weight="medium", color="#a1a1aa")
                self._widget_add_progress(bar_x, row_y + 3, bar_w, 7, metric["pct"])
                self._widget_add_label(metric["value"], x + w - 14 - value_w, row_y - 3, value_w, 18, size=14, weight="bold", color=metric["color"], align="right")
        else:
            self._widget_add_label(card["value"], x + 14, y + 45, w - 28, 22, size=17, weight="bold", color=card["value_color"], align="right")
            self._widget_add_label(card["subtitle"], x + 14, y + 27, w - 28, 16, size=11, color="#a1a1aa")

    def _widget_summary_cards(self):
        services = self._state.get("services") or list(_SERVICES)
        cards = []
        specs = [
            ("claude", "Claude", "sparkles", "#7c3aed", self._claude or {}),
            ("codex", "CodeX", "terminal.fill", "#2563eb", self._codex or {}),
            ("google", "Antigravity", "globe", "#dc2626", self._google or {}),
            ("gemini", "Gemini", "sparkles", "#9333ea", self._gemini or {}),
            ("llm_api", "LLM API", "rectangle.stack.badge.person.crop", "#0f766e", self._llm_api or {}),
            ("deepseek", "DeepSeek", "dollarsign.circle.fill", "#0891b2", self._deepseek or {}),
        ]
        for service, title, symbol, accent, data in specs:
            if service not in services:
                continue
            card = self._widget_card_for_service(service, title, symbol, accent, data)
            cards.append(card)
        return cards

    def _widget_card_for_service(self, service, title, symbol, accent, data):
        lang = self._state["lang"]
        if data.get("error"):
            return {
                "title": title,
                "symbol": "xmark.octagon.fill",
                "accent": "#ef4444",
                "pct": 0,
                "value": "!",
                "value_color": "#fecdd3",
                "subtitle": _short_widget_error(data.get("error")),
                "bg": "#2a171a",
                "border": "#7f1d1d",
            }
        pct, subtitle, value, metrics = None, _tr(lang, "等待数据", "Waiting"), "…", []
        if service == "claude" and data:
            metrics = self._quota_card_metrics([
                ("5h", data.get("5h_left"), data.get("5h_reset")),
                (_tr(lang, "1周", "1w"), data.get("7d_left"), data.get("7d_reset")),
            ])
            pct = self._metric_floor(metrics)
        elif service == "codex" and data:
            five_h, five_h_reset, weekly, weekly_reset = self._codex_balance_summary(data)
            if five_h is None and weekly is None:
                five_h, five_h_reset, weekly, weekly_reset = self._window_summary_from_buckets(
                    data.get("buckets") or [],
                    data.get("5h_left"),
                    data.get("5h_reset"),
                    data.get("7d_left"),
                    data.get("7d_reset"),
                )
            metrics = self._quota_card_metrics([
                ("5h", five_h, five_h_reset),
                (_tr(lang, "1周", "1w"), weekly, weekly_reset),
            ])
            pct = self._metric_floor(metrics)
        elif service == "deepseek" and data:
            primary = data.get("primary") or {}
            pct = 100 if data.get("available") and _balance_amount(primary) > 0 else 0
            value = _fmt_balance_compact(primary)
            subtitle = _tr(lang, "API balance", "API balance")
        elif service == "google" and data:
            if _has_windowed_quota(data):
                five_h, five_h_reset, weekly, weekly_reset = self._google_gemini_models_summary(data)
                if five_h is None and weekly is None:
                    five_h, five_h_reset, weekly, weekly_reset = self._window_summary_from_buckets(
                        data.get("buckets") or [],
                        None,
                        None,
                        None,
                        None,
                    )
                metrics = self._quota_card_metrics([
                    ("5h", five_h, five_h_reset),
                    (_tr(lang, "1周", "1w"), weekly, weekly_reset),
                ])
                pct = self._metric_floor(metrics)
            else:
                antigravity = data.get("antigravity") or {}
                limited = bool(antigravity.get("limited"))
                pct = 0 if limited else 70
                value = _tr(lang, "受限", "Limited") if limited else _tr(lang, "未知", "Unknown")
                model = antigravity.get("model_label") or data.get("primary_model") or "Antigravity"
                reset = self._format_detail_reset(antigravity.get("reset_time") or data.get("daily_reset"))
                subtitle = _short_widget_name(f"{model} ↻ {reset}" if reset else model, 38)
                metrics = []
        elif service == "gemini" and data:
            current, current_reset, weekly, weekly_reset = self._gemini_card_windows(data)
            metrics = self._quota_card_metrics([
                (_tr(lang, "当前", "Now"), current, current_reset),
                (_tr(lang, "1周", "1w"), weekly, weekly_reset),
            ])
            pct = self._metric_floor(metrics)
        elif service == "llm_api" and data:
            providers = data.get("providers") or []
            ready = int(data.get("ready_provider_count") or sum(1 for provider in providers if provider.get("configured")))
            total = int(data.get("provider_count") or len(providers))
            warnings = sum(1 for provider in providers if str(provider.get("warning_level")) in {"critical", "warning"})
            pct = 100 if warnings == 0 and ready == total and total > 0 else (35 if warnings else 70)
            value = f"{ready}/{total}" if total else "?"
            subtitle = _tr(lang, f"{warnings} 个余额告警", f"{warnings} balance warnings") if warnings else _tr(lang, "provider 余额", "provider balances")

        pct = _widget_pct_value(pct)
        value_color = _widget_risk_color(pct)
        bg, border, _, _ = _widget_risk_tone(pct)
        if pct > 20:
            bg, border = "#202124", "#34343a"
        return {
            "title": title,
            "symbol": symbol,
            "accent": accent,
            "pct": pct,
            "value": value,
            "value_color": value_color,
            "subtitle": subtitle,
            "metrics": metrics,
            "bg": bg,
            "border": border,
        }

    def _quota_card_metrics(self, pairs):
        metrics = []
        for item in pairs:
            label = item[0]
            pct = item[1] if len(item) > 1 else None
            reset = item[2] if len(item) > 2 else None
            if pct is None:
                continue
            pct = _widget_pct_value(pct)
            metrics.append({
                "label": label,
                "value": f"{pct}%",
                "pct": pct,
                "color": _widget_risk_color(pct),
                "reset": reset,
            })
        return metrics

    def _metric_floor(self, metrics):
        values = [item["pct"] for item in (metrics or [])]
        return min(values) if values else None

    def _bucket_remaining(self, bucket):
        remaining = bucket.get("remaining_percent")
        if remaining is None and bucket.get("used_percent") is not None:
            remaining = 100 - float(bucket.get("used_percent"))
        return _widget_pct_value(remaining)

    def _bucket_reset(self, bucket):
        return bucket.get("resets_at") or bucket.get("reset_time") or bucket.get("reset_text")

    def _codex_balance_summary(self, data):
        balance_buckets = [
            bucket for bucket in data.get("buckets") or []
            if str(bucket.get("group_display_name") or "").strip().lower() == "balance"
        ]
        return self._window_summary_from_buckets(
            balance_buckets,
            data.get("5h_left"),
            data.get("5h_reset"),
            data.get("7d_left"),
            data.get("7d_reset"),
        )

    def _google_gemini_models_summary(self, data):
        gemini_buckets = [
            bucket for bucket in data.get("buckets") or []
            if str(bucket.get("group_display_name") or "").strip().lower() == "gemini models"
        ]
        return self._window_summary_from_buckets(
            gemini_buckets,
            data.get("daily_left"),
            data.get("daily_reset"),
            data.get("daily_left"),
            data.get("daily_reset"),
        )

    def _window_summary_from_buckets(self, buckets, fallback_5h=None, fallback_5h_reset=None, fallback_weekly=None, fallback_weekly_reset=None):
        windows = {"5h": [], "weekly": []}
        for bucket in buckets or []:
            if bucket.get("disabled"):
                continue
            window = (bucket.get("window") or "").lower()
            name = " ".join(str(bucket.get(key) or "") for key in ("display_name", "bucket_name", "group_display_name")).lower()
            entry = (self._bucket_remaining(bucket), self._bucket_reset(bucket))
            if window == "5h" or "five hour" in name or "5 hour" in name or "5h" in name:
                windows["5h"].append(entry)
            elif window == "weekly" or "weekly" in name or "week" in name:
                windows["weekly"].append(entry)
        five_h, five_h_reset = min(windows["5h"], key=lambda item: item[0]) if windows["5h"] else (fallback_5h, fallback_5h_reset)
        weekly, weekly_reset = min(windows["weekly"], key=lambda item: item[0]) if windows["weekly"] else (fallback_weekly, fallback_weekly_reset)
        return five_h, five_h_reset, weekly, weekly_reset

    def _window_floor_from_buckets(self, buckets, fallback_5h=None, fallback_weekly=None):
        windows = {"5h": [], "weekly": []}
        for bucket in buckets or []:
            if bucket.get("disabled"):
                continue
            window = (bucket.get("window") or "").lower()
            name = " ".join(str(bucket.get(key) or "") for key in ("display_name", "bucket_name", "group_display_name")).lower()
            if window == "5h" or "five hour" in name or "5 hour" in name or "5h" in name:
                windows["5h"].append(self._bucket_remaining(bucket))
            elif window == "weekly" or "weekly" in name or "week" in name:
                windows["weekly"].append(self._bucket_remaining(bucket))
        five_h = min(windows["5h"]) if windows["5h"] else fallback_5h
        weekly = min(windows["weekly"]) if windows["weekly"] else fallback_weekly
        return five_h, weekly

    def _gemini_card_windows(self, data):
        current = data.get("left")
        current_reset = data.get("reset") or data.get("reset_text")
        weekly = None
        weekly_reset = None
        for bucket in data.get("buckets") or []:
            window = (bucket.get("window") or "").lower()
            name = (bucket.get("display_name") or "").lower()
            pct = bucket.get("remaining_percent")
            reset = bucket.get("reset_time") or bucket.get("reset_text")
            if window == "current" or "当前" in name or "current" in name:
                current = pct
                current_reset = reset
            elif window == "weekly" or "每周" in name or "weekly" in name:
                weekly = pct
                weekly_reset = reset
        return current, current_reset, weekly, weekly_reset

    def _card_reset_text(self, metrics):
        parts = []
        for metric in (metrics or [])[:2]:
            reset = metric.get("reset")
            if reset is None:
                continue
            formatted = _fmt_widget_reset_epoch_or_iso(reset, self._state["lang"])
            if not formatted or formatted == "?":
                formatted = str(reset)
            parts.append(f"{metric.get('label')} ↻ {formatted}")
        return " · ".join(parts)

    def _format_detail_reset(self, reset):
        if reset is None:
            return None
        formatted = _fmt_widget_reset_epoch_or_iso(reset, self._state["lang"])
        if not formatted or formatted == "?":
            formatted = str(reset)
        return formatted

    def _llm_api_provider_name(self, provider):
        aliases = {
            "openrouter": "OpenRoute",
            "openai": "OpenAI",
            "xai": "xAI",
            "moonshot": "Kimi",
            "dashscope": "Qwen",
            "ark": "Doubao",
            "deepseek": "DeepSeek",
        }
        profile_id = str(provider.get("provider_profile_id") or "")
        return aliases.get(profile_id) or provider.get("display_name") or profile_id or "LLM"

    def _llm_api_provider_metric(self, provider):
        balance = provider.get("balance") if isinstance(provider.get("balance"), dict) else {}
        amount = balance.get("amount")
        currency = balance.get("currency")
        if str(balance.get("status")) == "ok" and isinstance(amount, (int, float)):
            return f"{amount:.2f} {currency}" if currency else f"{amount:.2f}"
        if not provider.get("configured"):
            return _tr(self._state["lang"], "未配置", "missing")
        status = str(balance.get("status") or "")
        if status == "missing_credentials":
            return _tr(self._state["lang"], "缺账务密钥", "no billing key")
        if status == "error":
            return _tr(self._state["lang"], "余额失败", "balance error")
        if status == "unsupported":
            return _tr(self._state["lang"], "无接口", "unsupported")
        return _tr(self._state["lang"], "待同步", "syncing")

    def _llm_api_provider_health_pct(self, provider):
        if not provider.get("configured"):
            return 0
        level = str(provider.get("warning_level") or "normal")
        if level == "critical":
            return 10
        if level == "warning":
            return 35
        if level == "info":
            return 70
        return 100

    def _widget_alert_rows(self):
        rows = []
        for item in self._widget_detail_rows(include_disabled=False):
            if item.get("type") == "section":
                continue
            pct = item["pct"]
            if pct > 20:
                continue
            bg, border, fg, symbol = _widget_risk_tone(pct)
            rows.append({
                "text": _short_widget_name(item["name"], 42),
                "value": item["value"],
                "bg": bg,
                "border": border,
                "fg": fg,
                "symbol": symbol,
                "pct": pct,
            })
        rows.sort(key=lambda item: item["pct"])
        return rows

    def _compact_bar_items(self):
        error_services = []
        for service, data in (
            ("claude", self._claude or {}),
            ("codex", self._codex or {}),
            ("deepseek", self._deepseek or {}),
            ("google", self._google or {}),
            ("gemini", self._gemini or {}),
            ("llm_api", self._llm_api or {}),
        ):
            if service in (self._state.get("services") or list(_SERVICES)) and data.get("error"):
                error_services.append(service)
        if error_services:
            return [("AI", 0, "percent", True)]

        rows = [
            row for row in self._widget_detail_rows(include_disabled=False)
            if row.get("type") != "section" and not row.get("disabled")
        ]
        if not rows:
            return [("AI", "OK", "text", False)]
        worst = min(rows, key=lambda row: row["pct"])
        if worst["pct"] <= 20:
            label = self._compact_bar_label(worst["name"])
            return [(label, worst["pct"], "percent", False)]
        return [("AI", "OK", "text", False)]

    def _compact_bar_label(self, name):
        text = str(name or "").lower()
        if text.startswith("5h") or text.startswith("weekly"):
            return "C"
        if "balance" in text or "spark" in text or "codex" in text:
            return "X"
        if "gemini models" in text or "claude and gpt" in text:
            return "AG"
        if "current" in text or "usage" in text or "每周" in text or "当前" in text:
            return "M"
        if "kimi" in text or "moonshot" in text:
            return "KM"
        if "doubao" in text or "ark" in text:
            return "DB"
        if "qwen" in text or "dashscope" in text:
            return "QW"
        if "openroute" in text or "openrouter" in text:
            return "OR"
        if "xai" in text:
            return "xAI"
        if "deepseek" in text:
            return "D"
        return "AI"

    def _widget_detail_rows(self, include_disabled=True):
        rows = []
        services = self._state.get("services") or list(_SERVICES)

        def section(name, color):
            rows.append({"type": "section", "name": name, "color": color})

        def append(name, pct, disabled=False, value=None, reset=None, unknown=False):
            if disabled and not include_disabled:
                return
            if unknown and not include_disabled:
                return
            pct = _widget_pct_value(pct)
            suffix = " off" if disabled and self._state["lang"] == "en" else (" 不适用" if disabled else "")
            if value is not None:
                display_value = f"{value}{suffix}"
            elif unknown:
                display_value = _tr(self._state["lang"], "未知", "unknown")
            else:
                display_value = f"{pct}%{suffix}"
            rows.append({
                "name": _short_widget_name(name, 44),
                "pct": pct,
                "value": display_value,
                "reset": self._format_detail_reset(reset),
                "color": "#71717a" if (disabled or unknown) else _widget_risk_color(pct),
                "disabled": disabled or unknown,
            })

        if "claude" in services and self._claude and not self._claude.get("error"):
            section("Claude", "#c4b5fd")
            append("5h", self._claude.get("5h_left"), reset=self._claude.get("5h_reset"))
            append("weekly", self._claude.get("7d_left"), reset=self._claude.get("7d_reset"))
        if "codex" in services:
            entries = self._widget_codex_entries()
            if entries:
                section("CodeX", "#93c5fd")
                for item in entries:
                    append(item["name"], item["pct"], item.get("disabled", False), reset=item.get("reset"), unknown=item.get("unknown", False))
        if "deepseek" in services and self._deepseek and not self._deepseek.get("error"):
            primary = self._deepseek.get("primary") or {}
            section("DeepSeek", "#67e8f9")
            append(_fmt_balance_compact(primary), 100 if _balance_amount(primary) > 0 else 0)
        if "google" in services:
            entries = self._widget_google_entries()
            if entries:
                section("Antigravity", "#fca5a5")
                for item in entries:
                    append(item["name"], item["pct"], item.get("disabled", False), reset=item.get("reset"), unknown=item.get("unknown", False))
        if "gemini" in services:
            entries = self._widget_gemini_entries()
            if entries:
                section("Gemini", "#d8b4fe")
                for item in entries:
                    append(item["name"], item["pct"], item.get("disabled", False), reset=item.get("reset"))
        if "llm_api" in services:
            entries = self._widget_llm_api_entries()
            if entries:
                section("LLM API", "#5eead4")
                for item in entries:
                    append(item["name"], item["pct"], item.get("disabled", False), item.get("value"))
        return rows[:34]

    def _widget_codex_entries(self):
        data = self._codex or {}
        if data.get("error"):
            return []
        entries = []
        buckets = data.get("buckets") or []
        if buckets:
            for bucket in buckets:
                remaining = bucket.get("remaining_percent")
                if remaining is None and bucket.get("used_percent") is not None:
                    remaining = 100 - float(bucket.get("used_percent"))
                name = " / ".join(
                    value for value in (
                        bucket.get("group_display_name"),
                        bucket.get("display_name") or bucket.get("window"),
                    )
                    if value
                )
                entries.append({
                    "name": name or "limit",
                    "pct": _widget_pct_value(remaining),
                    "reset": self._bucket_reset(bucket),
                })
        elif data:
            if data.get("5h_left") is not None:
                entries.append({"name": "Balance / 5h", "pct": _widget_pct_value(data.get("5h_left")), "reset": data.get("5h_reset")})
            entries.append({"name": "Balance / weekly", "pct": _widget_pct_value(data.get("7d_left")), "reset": data.get("7d_reset")})
        return entries

    def _widget_google_entries(self):
        data = self._google or {}
        if data.get("error"):
            return []
        entries = []
        buckets = data.get("buckets") or []
        for bucket in buckets:
            name = " / ".join(
                value for value in (
                    bucket.get("group_display_name"),
                    bucket.get("display_name") or bucket.get("model_id"),
                )
                if value
            )
            remaining = bucket.get("remaining_percent")
            unknown = remaining is None
            entries.append({
                "name": name or "limit",
                "pct": _widget_pct_value(remaining),
                "reset": self._bucket_reset(bucket),
                "disabled": bool(bucket.get("disabled")),
                "unknown": unknown,
            })
        if not entries and data:
            daily = data.get("daily_left")
            entries.append({"name": data.get("primary_model") or "Antigravity", "pct": _widget_pct_value(daily), "reset": data.get("daily_reset"), "unknown": daily is None})
        return entries

    def _widget_gemini_entries(self):
        data = self._gemini or {}
        if data.get("error"):
            return []
        entries = []
        buckets = data.get("buckets") or []
        for bucket in buckets:
            entries.append({
                "name": bucket.get("display_name") or "Gemini App quota",
                "pct": _widget_pct_value(bucket.get("remaining_percent")),
                "reset": self._bucket_reset(bucket),
                "disabled": bool(bucket.get("disabled")),
            })
        if not entries and data:
            entries.append({"name": "Usage", "pct": _widget_pct_value(data.get("left")), "reset": data.get("reset") or data.get("reset_text")})
        return entries

    def _widget_llm_api_entries(self):
        data = self._llm_api or {}
        if data.get("error"):
            return []
        entries = []
        for provider in data.get("providers") or []:
            entries.append({
                "name": self._llm_api_provider_name(provider),
                "pct": self._llm_api_provider_health_pct(provider),
                "value": self._llm_api_provider_metric(provider),
                "disabled": not bool(provider.get("configured")),
            })
        return entries

    def _widget_text_body(self):
        lang = self._state["lang"]
        services = self._state.get("services") or list(_SERVICES)
        lines = [
            "AI Limit",
            _tr(
                lang,
                f"更新 {datetime.datetime.now(TZ_LOCAL):%H:%M:%S}    菜单栏模式 {self._state['global']}",
                f"Updated {datetime.datetime.now(TZ_LOCAL):%H:%M:%S}    Menu mode {self._state['global']}",
            ),
            "",
        ]
        if "claude" in services:
            lines.extend(self._widget_claude_lines())
        if "codex" in services:
            lines.extend(self._widget_codex_lines())
        if "deepseek" in services:
            lines.extend(self._widget_deepseek_lines())
        if "google" in services:
            lines.extend(self._widget_google_lines())
        if "gemini" in services:
            lines.extend(self._widget_gemini_lines())
        if "llm_api" in services:
            lines.extend(self._widget_llm_api_lines())
        return "\n".join(lines).rstrip() + "\n"

    def _widget_section(self, title, data, source=None):
        lines = [title]
        if data.get("error"):
            lines.append(f"  ! {_short_widget_error(data.get('error'))}")
        elif source:
            lines.append(_tr(self._state["lang"], f"  来源: {source}", f"  Source: {source}"))
        return lines

    def _widget_quota_row(self, name, remaining=None, used=None, reset=None, disabled=False):
        lang = self._state["lang"]
        parts = [f"  {name}"]
        if remaining is not None:
            parts.append(_fmt_widget_pct(remaining, lang, True))
        elif used is not None:
            parts.append(_fmt_widget_pct(used, lang, False))
        else:
            parts.append("?")
        if used is not None and remaining is not None:
            parts.append(_fmt_widget_pct(used, lang, False))
        if disabled:
            parts.append(_tr(lang, "不适用", "disabled"))
        if reset:
            parts.append(f"↻ {_fmt_widget_reset_epoch_or_iso(reset, lang)}")
        return "    ".join(parts)

    def _widget_claude_lines(self):
        lang = self._state["lang"]
        data = self._claude or {}
        lines = self._widget_section("Claude Code", data, data.get("source") or "browser live")
        if data and not data.get("error"):
            plan = data.get("plan")
            if plan:
                lines.append(f"  Plan: {plan}")
            lines.append(self._widget_quota_row("5h", data.get("5h_left"), reset=data.get("5h_reset")))
            lines.append(self._widget_quota_row(_tr(lang, "weekly", "weekly"), data.get("7d_left"), reset=data.get("7d_reset")))
        lines.append("")
        return lines

    def _widget_codex_lines(self):
        lang = self._state["lang"]
        data = self._codex or {}
        lines = self._widget_section("CodeX", data, data.get("source") or "")
        if data and not data.get("error"):
            buckets = data.get("buckets") or []
            if buckets:
                current_group = None
                for bucket in buckets:
                    group = bucket.get("group_display_name") or _tr(lang, "未分组", "Ungrouped")
                    if group != current_group:
                        lines.append(f"  {group}")
                        current_group = group
                    name = bucket.get("display_name") or bucket.get("window") or "limit"
                    remaining = bucket.get("remaining_percent")
                    if remaining is None and bucket.get("used_percent") is not None:
                        remaining = max(0, min(100, int(round(100 - bucket.get("used_percent")))))
                    lines.append(self._widget_quota_row(name, remaining, bucket.get("used_percent"), bucket.get("resets_at") or bucket.get("reset_time")))
            else:
                if data.get("5h_left") is not None:
                    lines.append(self._widget_quota_row("5h", data.get("5h_left"), reset=data.get("5h_reset")))
                lines.append(self._widget_quota_row(_tr(lang, "weekly", "weekly"), data.get("7d_left"), reset=data.get("7d_reset")))
        lines.append("")
        return lines

    def _widget_deepseek_lines(self):
        lang = self._state["lang"]
        data = self._deepseek or {}
        lines = self._widget_section("DeepSeek", data, data.get("source") or "api key live")
        if data and not data.get("error"):
            balances = data.get("balances") or []
            if not balances and data.get("primary"):
                balances = [data.get("primary")]
            for balance in balances:
                currency = balance.get("currency", "USD")
                total = _fmt_balance_short(balance)
                granted = fmt_money(balance.get("granted_balance", "0"), currency)
                topped = fmt_money(balance.get("topped_up_balance", "0"), currency)
                lines.append(_tr(lang, f"  {currency}: {total}    赠送 {granted} / 充值 {topped}", f"  {currency}: {total}    granted {granted} / topped {topped}"))
        lines.append("")
        return lines

    def _widget_google_lines(self):
        lang = self._state["lang"]
        data = self._google or {}
        lines = self._widget_section("Google / Antigravity", data, data.get("source") or "")
        if data and not data.get("error"):
            buckets = data.get("buckets") or []
            current_group = None
            if buckets:
                for bucket in buckets:
                    group = bucket.get("group_display_name") or _tr(lang, "未分组", "Ungrouped")
                    if group != current_group:
                        lines.append(f"  {group}")
                        current_group = group
                    name = bucket.get("display_name") or bucket.get("model_id") or "limit"
                    lines.append(self._widget_quota_row(name, bucket.get("remaining_percent"), bucket.get("used_percent"), bucket.get("reset_time"), bucket.get("disabled")))
            else:
                lines.append(self._widget_quota_row(data.get("primary_model") or "daily", data.get("daily_left"), reset=data.get("daily_reset")))
        lines.append("")
        return lines

    def _widget_gemini_lines(self):
        lang = self._state["lang"]
        data = self._gemini or {}
        lines = self._widget_section("Gemini App", data, data.get("source") or "gemini.google.com/usage")
        if data and not data.get("error"):
            if data.get("unavailable_reason"):
                lines.append(f"  ! {_short_widget_error(data.get('unavailable_reason'))}")
            buckets = data.get("buckets") or []
            if buckets:
                for bucket in buckets:
                    name = bucket.get("display_name") or "Gemini App quota"
                    reset = bucket.get("reset_time") or bucket.get("reset_text")
                    lines.append(self._widget_quota_row(name, bucket.get("remaining_percent"), bucket.get("used_percent"), reset))
            else:
                lines.append(self._widget_quota_row("Usage", data.get("left"), data.get("used"), data.get("reset") or data.get("reset_text")))
        lines.append("")
        return lines

    def _widget_llm_api_lines(self):
        data = self._llm_api or {}
        lines = self._widget_section("LLM API", data, data.get("source") or "ai-limit native llm balance adapters")
        if data and not data.get("error"):
            ready = data.get("ready_provider_count")
            total = data.get("provider_count")
            lines.append(_tr(self._state["lang"], f"  Provider: {ready}/{total}", f"  Providers: {ready}/{total}"))
            for provider in data.get("providers") or []:
                lines.append(f"  {self._llm_api_provider_name(provider)}    {self._llm_api_provider_metric(provider)}")
        lines.append("")
        return lines

    # ── 模式 / 语言切换 ──────────────────────────────────────────────────────

    def _set_mode_5h(self, _):
        self._state["global"] = "5h"
        _save_state(self._state)
        self._update_mode_checks()
        self._render()  # 只换显示窗口，数据没变，直接重画

    def _set_mode_7d(self, _):
        self._state["global"] = "7d"
        _save_state(self._state)
        self._update_mode_checks()
        self._render()

    def _update_mode_checks(self):
        lang = self._state["lang"]
        mode = self._state["global"]
        self._mode_5h.title = ("✓ " if mode == "5h" else "  ") + _tr(lang, "5 小时", "5 hours")
        self._mode_7d.title = ("✓ " if mode == "7d" else "  ") + _tr(lang, "7 天", "7 days")
        self._mode_menu.title = _tr(lang,
            f"默认窗口（{_tr(lang, '5 小时', '5 hours') if mode == '5h' else _tr(lang, '7 天', '7 days')}）",
            f"Default window ({_tr(lang, '5 hours', '5 hours') if mode == '5h' else '7 days'})",
        )

    def _set_lang_zh(self, _):
        self._state["lang"] = "zh"
        _save_state(self._state)
        self._update_lang_checks()
        # 重画所有 i18n 文本（详情行 / 段头 / "上次刷新" 等）
        self._update_mode_checks()
        self._update_service_checks()
        self._refresh_static_labels()
        self._render()

    def _set_lang_en(self, _):
        self._state["lang"] = "en"
        _save_state(self._state)
        self._update_lang_checks()
        self._update_mode_checks()
        self._update_service_checks()
        self._refresh_static_labels()
        self._render()

    def _refresh_static_labels(self):
        """语言切换后，更新所有不依赖数据的菜单文字。"""
        lang = self._state["lang"]
        self._refresh_item.title = _tr(lang, "立即刷新", "Refresh now")
        self._codex_dash.title  = _tr(lang, "打开 CodeX 分析页", "Open CodeX analytics")
        self._claude_dash.title = _tr(lang, "打开 Claude 用量页", "Open Claude usage")
        self._deepseek_dash.title = _tr(lang, "打开 DeepSeek 用量页", "Open DeepSeek usage")
        self._google_dash.title = _tr(lang, "打开 Google 配额说明页", "Open Google quota docs")
        self._gemini_dash.title = _tr(lang, "打开 Gemini App 用量页", "Open Gemini App usage")
        self._about_repo.title = _tr(lang, "打开项目仓库", "Open project repository")
        self._about_menu.title  = _tr(lang,
            f"项目信息（ai-limit {__version__}）",
            f"Project (ai-limit {__version__})",
        )
        self._about_ver.title = _tr(lang,
            f"版本：ai-limit {__version__}",
            f"Version: ai-limit {__version__}",
        )
        self._about_scope.title = _tr(lang,
            "监控：Claude / CodeX / DeepSeek / Google / Gemini App / LLM API",
            "Monitors: Claude / CodeX / DeepSeek / Google / Gemini App / LLM API",
        )
        self._about_surfaces.title = _tr(lang,
            "界面：菜单栏 / CLI / daemon",
            "Surfaces: menu bar / CLI / daemon",
        )
        self._about_status.title = _tr(lang,
            "状态：当前版本已接入 Google、Gemini App 与 LLM API 配额",
            "Status: current build includes Google, Gemini App, and LLM API quota",
        )
        self._update_login_item_check()
        self._update_widget_item()
        self._quit_item.title    = _tr(lang, "退出", "Quit")

    def _update_lang_checks(self):
        lang = self._state["lang"]
        self._lang_zh.title = ("✓ " if lang == "zh" else "  ") + "中文"
        self._lang_en.title = ("✓ " if lang == "en" else "  ") + "English"
        self._lang_menu.title = _tr(lang,
            f"语言（{'中文' if lang == 'zh' else 'English'}）",
            f"Language ({'中文' if lang == 'zh' else 'English'})",
        )

    # ── 监控服务切换 ────────────────────────────────────────────────────────

    def _toggle_claude(self, _):
        self._toggle_service("claude")

    def _toggle_codex(self, _):
        self._toggle_service("codex")

    def _toggle_deepseek(self, _):
        self._toggle_service("deepseek")

    def _toggle_google(self, _):
        self._toggle_service("google")

    def _toggle_gemini(self, _):
        self._toggle_service("gemini")

    def _toggle_llm_api(self, _):
        self._toggle_service("llm_api")

    def _toggle_service(self, service):
        svc = list(self._state.get("services") or list(_SERVICES))
        if service in svc:
            svc.remove(service)
        else:
            svc.append(service)
        if not svc:
            # 不允许两个都关掉，回退保留刚才被关的
            svc = [service]
        self._state["services"] = svc
        _save_state(self._state)
        self._update_service_checks()
        # 立即用现有数据重画（隐藏/显示对应区块），不卡 UI；
        # 新启用的服务若有 ≤55s 的缓存就用，否则等下面后台拉
        self._render()
        # 后台异步刷新（如果新启用的服务无缓存，几秒后自动出现）
        self._kick_background_fetch()

    def _toggle_login_item(self, _):
        _set_login_item(not _login_item_enabled())
        self._update_login_item_check()

    def _update_login_item_check(self):
        lang = self._state["lang"]
        enabled = _login_item_enabled()
        suffix = " ✓" if enabled else ""
        self._login_item.title = _tr(lang, "开机自启", "Launch at Login") + suffix

    def _update_service_checks(self):
        lang = self._state["lang"]
        svc = self._state.get("services") or list(_SERVICES)
        self._svc_claude.title = ("✓ " if "claude" in svc else "  ") + "Claude Code"
        self._svc_codex.title  = ("✓ " if "codex"  in svc else "  ") + "CodeX"
        self._svc_deepseek.title = ("✓ " if "deepseek" in svc else "  ") + "DeepSeek"
        self._svc_google.title = ("✓ " if "google" in svc else "  ") + "Google"
        self._svc_gemini.title = ("✓ " if "gemini" in svc else "  ") + "Gemini App"
        self._svc_llm_api.title = ("✓ " if "llm_api" in svc else "  ") + "LLM API"
        enabled = []
        if "claude" in svc:
            enabled.append("Claude Code")
        if "codex" in svc:
            enabled.append("CodeX")
        if "deepseek" in svc:
            enabled.append("DeepSeek")
        if "google" in svc:
            enabled.append("Google")
        if "gemini" in svc:
            enabled.append("Gemini App")
        if "llm_api" in svc:
            enabled.append("LLM API")
        summary = _tr(lang, "全部", "All") if len(svc) == len(_SERVICES) else ", ".join(enabled)
        self._svc_menu.title = _tr(lang, f"监控服务（{summary}）", f"Services ({summary})")

    # ── 立即刷新 ──────────────────────────────────────────────────────────────

    def _force_refresh(self, _):
        _clear_all_caches()
        # 后台拉，不卡 UI；新数据 ≤几秒内通过 _apply_pending 落到菜单上
        self._kick_background_fetch()


if __name__ == "__main__":
    if not _acquire_single_instance():
        sys.exit(0)
    _ensure_login_item_on_first_run()
    AiLimitApp().run()
