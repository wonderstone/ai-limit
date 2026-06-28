#!/usr/bin/env python3
"""ai-limit 后台守护进程（无菜单栏、无 GUI）

可作为 LaunchAgent 独立运行，定期检查 Claude Code / CodeX / DeepSeek 额度，
写入日志文件，并在额度不足时发送 macOS 系统通知。

用法：
    # 前台运行（调试用）
    python -m ai_limit.daemon

    # 作为 LaunchAgent 安装（开机自启 + 保活）
    python -m ai_limit.daemon --install

    # 卸载 LaunchAgent
    python -m ai_limit.daemon --uninstall

LaunchAgent 安装后，日志写入 ~/.ai-limit-daemon.log。
"""
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
import socket
import urllib.error

from ai_limit.i18n import LANG, t
from ai_limit.providers import (
    ClaudeWebError,
    CodexAuthError,
    CodexWebError,
    DeepSeekAuthError,
    DeepSeekError,
    GoogleQuotaAuthError,
    GoogleQuotaError,
    current_codex_rate_limits,
    has_deepseek_api_key,
    has_google_oauth_creds,
    live_claude_plan,
    live_claude_usage,
    live_deepseek_balance,
    live_google_quota,
)

# ── 配置 ──────────────────────────────────────────────────────────────────────
CHECK_INTERVAL_SEC = 120          # 每 2 分钟检查一次
WARN_THRESHOLD     = 20           # 低于此百分比发通知
CRIT_THRESHOLD     = 10           # 低于此百分比发紧急通知
LOG_PATH           = pathlib.Path.home() / ".ai-limit-daemon.log"
STATE_PATH         = pathlib.Path.home() / ".ai-limit-daemon-state.json"
LAUNCH_AGENT_LABEL = "com.zhuchenxi.ai-limit-daemon"
LAUNCH_AGENT_PLIST = pathlib.Path.home() / "Library/LaunchAgents" / f"{LAUNCH_AGENT_LABEL}.plist"
PID_PATH           = pathlib.Path.home() / ".ai-limit-daemon.pid"

# ── 工具函数 ──────────────────────────────────────────────────────────────────

def _log(msg: str):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _notify(title: str, body: str, sound: bool = True):
    """发送 macOS 系统通知。"""
    try:
        script = f'display notification "{body}" with title "{title}"'
        if sound:
            script += ' sound name "default"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
    except Exception:
        pass


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_single_instance() -> bool:
    try:
        if PID_PATH.exists():
            existing = int(PID_PATH.read_text().strip())
            if existing != os.getpid() and _pid_is_running(existing):
                return False
    except Exception:
        pass
    try:
        PID_PATH.write_text(f"{os.getpid()}\n", encoding="utf-8")
    except Exception:
        return True
    return True


def _release_single_instance():
    try:
        if PID_PATH.exists() and PID_PATH.read_text().strip() == str(os.getpid()):
            PID_PATH.unlink()
    except Exception:
        pass


def _load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict):
    try:
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass


# ── 数据抓取 ──────────────────────────────────────────────────────────────────

def _fetch_claude():
    try:
        data = live_claude_usage()
        five_h = data.get("five_hour") or {}
        seven_d = data.get("seven_day") or {}
        try:
            plan = live_claude_plan()
        except Exception:
            plan = None
        return {
            "5h_left": int(round(100 - float(five_h.get("utilization", 0)))),
            "7d_left": int(round(100 - float(seven_d.get("utilization", 0)))),
            "5h_reset": five_h.get("resets_at"),
            "7d_reset": seven_d.get("resets_at"),
            "plan": plan,
        }
    except ClaudeWebError as e:
        return {"error": str(e)[:120]}
    except (socket.timeout, TimeoutError):
        return {"error": "network timeout"}
    except urllib.error.URLError:
        return {"error": "network unavailable"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fetch_codex():
    try:
        # 尝试从 usage 模块获取本地快照兜底函数
        try:
            from usage import latest_codex_rate_limits as _codex_snapshot
        except Exception:
            _codex_snapshot = lambda: (None, None)
        _ts, rl, source, fallback_reason = current_codex_rate_limits(_codex_snapshot)
        if not rl:
            return {"error": fallback_reason or "no Codex data"}
        primary = rl.get("primary") or {}
        secondary = rl.get("secondary") or {}
        return {
            "5h_left": int(round(100 - primary.get("used_percent", 0))),
            "7d_left": int(round(100 - secondary.get("used_percent", 0))),
            "5h_reset": primary.get("resets_at"),
            "7d_reset": secondary.get("resets_at"),
            "plan": rl.get("plan_type") or "?",
        }
    except CodexAuthError:
        return {"error": "no Codex access"}
    except CodexWebError as e:
        return {"error": str(e)[:120]}
    except (socket.timeout, TimeoutError):
        return {"error": "network timeout"}
    except urllib.error.URLError:
        return {"error": "network unavailable"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fetch_deepseek():
    if not has_deepseek_api_key():
        return None
    try:
        _ts, data = live_deepseek_balance()
        balances = data.get("balance_infos") or []
        ranked = sorted(
            balances,
            key=lambda b: (
                float(b.get("total_balance", "0")) <= 0,
                {"CNY": 0, "USD": 1}.get(b.get("currency"), 9),
            ),
        )
        primary = ranked[0] if ranked else None
        return {
            "available": bool(data.get("is_available")),
            "primary": primary,
        }
    except (DeepSeekAuthError, DeepSeekError) as e:
        return {"error": str(e)[:120]}
    except (socket.timeout, TimeoutError):
        return {"error": "network timeout"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _fetch_google():
    if not has_google_oauth_creds():
        return None
    try:
        _ts, data = live_google_quota()
        summary = data.get("summary") or {}
        primary = data.get("primary") or {}
        return {
            "daily_left": summary.get("remaining_percent"),
            "daily_reset": summary.get("reset_time"),
            "bucket_count": summary.get("bucket_count", 0),
            "primary_model": primary.get("model_id"),
        }
    except (GoogleQuotaAuthError, GoogleQuotaError) as e:
        return {"error": str(e)[:120]}
    except (socket.timeout, TimeoutError):
        return {"error": "network timeout"}
    except urllib.error.URLError:
        return {"error": "network unavailable"}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── 主循环 ────────────────────────────────────────────────────────────────────

def _check_and_notify():
    """单次检查 + 阈值告警。"""
    prev = _load_state()
    lang = LANG or "zh"

    claude = _fetch_claude()
    codex = _fetch_codex()
    deepseek = _fetch_deepseek()
    google = _fetch_google()

    state = {"ts": datetime.datetime.now().isoformat()}

    # Claude
    if claude and "error" not in claude:
        pct = claude["5h_left"]
        state["claude_5h"] = pct
        prev_pct = prev.get("claude_5h", 100)
        if pct <= CRIT_THRESHOLD and prev_pct > CRIT_THRESHOLD:
            _notify(
                "⚠️ Claude 额度严重不足",
                f"5 小时内仅剩 {pct}%，请留意用量",
            )
            _log(f"CRITICAL: Claude 5h remaining = {pct}%")
        elif pct <= WARN_THRESHOLD and prev_pct > WARN_THRESHOLD:
            _notify(
                "Claude 额度偏低",
                f"5 小时内剩余 {pct}%",
            )
            _log(f"WARN: Claude 5h remaining = {pct}%")

    # Codex
    if codex and "error" not in codex:
        pct = codex["5h_left"]
        state["codex_5h"] = pct
        prev_pct = prev.get("codex_5h", 100)
        if pct <= CRIT_THRESHOLD and prev_pct > CRIT_THRESHOLD:
            _notify(
                "⚠️ CodeX 额度严重不足",
                f"5 小时内仅剩 {pct}%，请留意用量",
            )
            _log(f"CRITICAL: CodeX 5h remaining = {pct}%")
        elif pct <= WARN_THRESHOLD and prev_pct > WARN_THRESHOLD:
            _notify(
                "CodeX 额度偏低",
                f"5 小时内剩余 {pct}%",
            )
            _log(f"WARN: CodeX 5h remaining = {pct}%")

    # DeepSeek
    if deepseek and "error" not in deepseek:
        primary = deepseek.get("primary") or {}
        balance = primary.get("total_balance", "0")
        state["deepseek_balance"] = balance
        prev_balance = prev.get("deepseek_balance", "999")
        try:
            if float(balance) <= 0 and float(prev_balance) > 0:
                _notify("DeepSeek 余额不足", "余额已用尽，请充值")
                _log("CRITICAL: DeepSeek balance = 0")
        except Exception:
            pass

    # Google / Antigravity
    if google and "error" not in google and google.get("daily_left") is not None:
        pct = google["daily_left"]
        state["google_daily"] = pct
        prev_pct = prev.get("google_daily", 100)
        if pct <= CRIT_THRESHOLD and prev_pct > CRIT_THRESHOLD:
            _notify(
                "⚠️ Google 额度严重不足",
                f"日额度仅剩 {pct}%，请留意用量",
            )
            _log(f"CRITICAL: Google daily remaining = {pct}%")
        elif pct <= WARN_THRESHOLD and prev_pct > WARN_THRESHOLD:
            _notify(
                "Google 额度偏低",
                f"日额度剩余 {pct}%",
            )
            _log(f"WARN: Google daily remaining = {pct}%")

    _save_state(state)

    # 摘要日志
    parts = []
    if claude:
        parts.append(f"Claude={claude.get('5h_left', '?')}%")
    if codex:
        parts.append(f"CodeX={codex.get('5h_left', '?')}%")
    if deepseek and deepseek.get("primary"):
        parts.append(f"DS={deepseek['primary'].get('total_balance', '?')}")
    if google:
        parts.append(f"Google={google.get('daily_left', '?')}%")
    _log(" | ".join(parts) if parts else "no data")


def run_forever():
    """主循环：每 CHECK_INTERVAL_SEC 检查一次。"""
    if not _acquire_single_instance():
        _log("another daemon instance is already running, exiting")
        sys.exit(0)

    import atexit
    atexit.register(_release_single_instance)

    _log("ai-limit daemon started")
    _check_and_notify()

    while True:
        time.sleep(CHECK_INTERVAL_SEC)
        try:
            _check_and_notify()
        except Exception as e:
            _log(f"check error: {e}")


# ── LaunchAgent 安装 / 卸载 ──────────────────────────────────────────────────

def _get_python_path() -> str:
    """返回当前 Python 解释器路径。"""
    return sys.executable


def install_launch_agent():
    """安装 LaunchAgent：创建 plist → bootstrap 到 launchd。"""
    python = _get_python_path()
    module_dir = str(pathlib.Path(__file__).resolve().parent.parent)

    LAUNCH_AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT_PLIST.write_text(
        f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>-m</string>
        <string>ai_limit.daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{module_dir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{LOG_PATH}</string>
    <key>StandardErrorPath</key>
    <string>{LOG_PATH}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
""",
        encoding="utf-8",
    )

    try:
        subprocess.run(
            ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(LAUNCH_AGENT_PLIST)],
            capture_output=True,
            timeout=10,
        )
        print(f"✅ LaunchAgent 已安装并启动: {LAUNCH_AGENT_LABEL}")
        print(f"   日志: {LOG_PATH}")
        print(f"   停止: launchctl bootout gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}")
    except Exception as e:
        print(f"❌ launchctl bootstrap 失败: {e}")
        print(f"   请手动执行: launchctl bootstrap gui/{os.getuid()} {LAUNCH_AGENT_PLIST}")


def uninstall_launch_agent():
    """卸载 LaunchAgent：bootout → 删除 plist。"""
    try:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{os.getuid()}/{LAUNCH_AGENT_LABEL}"],
            capture_output=True,
            timeout=10,
        )
        print(f"✅ 已从 launchd 注销: {LAUNCH_AGENT_LABEL}")
    except Exception as e:
        print(f"⚠️  bootout 失败（可能未安装）: {e}")

    try:
        LAUNCH_AGENT_PLIST.unlink()
        print(f"✅ 已删除: {LAUNCH_AGENT_PLIST}")
    except FileNotFoundError:
        pass


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if "--install" in sys.argv:
        install_launch_agent()
    elif "--uninstall" in sys.argv:
        uninstall_launch_agent()
    else:
        run_forever()
