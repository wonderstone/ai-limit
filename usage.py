#!/usr/bin/env python3
"""
usage.py — 查看 Claude Code + CodeX 本周 token 消耗与额度状态

用法：
    python tools/usage.py
    python tools/usage.py --days 3     # 只看最近 3 天
    python tools/usage.py --all        # 看全部历史（跨周汇总）
"""
import argparse
import datetime
import json
import pathlib
import sys
from ai_limit.i18n import LANG, t
from ai_limit.providers import (
    CLAUDE_USAGE_URL,
    CODEX_USAGE_URL,
    ClaudeWebError,
    CodexAuthError,
    DeepSeekAuthError,
    DeepSeekError,
    GoogleQuotaAuthError,
    GoogleQuotaError,
    current_codex_rate_limits as resolve_codex_rate_limits,
    has_deepseek_api_key,
    has_google_oauth_creds,
    live_claude_plan,
    live_claude_usage,
    live_deepseek_balance,
    live_google_quota,
)

CLAUDE_BASE = pathlib.Path.home() / ".claude" / "projects"
CODEX_BASE = pathlib.Path.home() / ".codex" / "sessions"
TZ_LOCAL = datetime.datetime.now().astimezone().tzinfo
TZ_ABBR  = datetime.datetime.now().astimezone().strftime('%Z')
__version__ = "0.3.5"

# ── 外观配置（可直接修改） ────────────────────────────────────────────────────
WARN_THRESHOLD = 20    # 剩余低于此值（%）显示黄色
CRIT_THRESHOLD = 10    # 剩余低于此值（%）显示红色
COLOR_OK   = "\033[32m"   # 绿：正常（ANSI 色码，32=绿 33=黄 36=青 34=蓝）
COLOR_WARN = "\033[33m"   # 黄：偏低
COLOR_CRIT = "\033[31m"   # 红：告警
# ─────────────────────────────────────────────────────────────────────────────

_C   = sys.stdout.isatty()
_DIM = "\033[2m" if _C else ""
_BOLD= "\033[1m" if _C else ""
_RST = "\033[0m" if _C else ""
_OK  = COLOR_OK   if _C else ""
_WRN = COLOR_WARN if _C else ""
_CRT = COLOR_CRIT if _C else ""

def _bc(r: float) -> str:
    return _OK if r >= WARN_THRESHOLD else (_WRN if r >= CRIT_THRESHOLD else _CRT)

def _colored_bar(remaining: float, width: int = 20) -> str:
    filled = round(remaining / 100 * width)
    return f"{_bc(remaining)}{'█'*filled}{_DIM}{'░'*(width-filled)}{_RST}"

def _bold_bar(pct: float, width: int = 20) -> str:
    filled = round(pct / 100 * width)
    return f"{_BOLD}{'█'*filled}{_RST}{_DIM}{'░'*(width-filled)}{_RST}"


# ── 工具函数 ─────────────────────────────────────────────────────────────────



def ts_to_local(iso: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ_LOCAL)


def epoch_to_local(epoch: int) -> datetime.datetime:
    return datetime.datetime.fromtimestamp(epoch, tz=TZ_LOCAL)


def bar(pct: float, width: int = 20) -> str:
    filled = round(pct / 100 * width)
    return "█" * filled + "░" * (width - filled)


def remaining_percent(used_pct: float) -> float:
    return max(0, min(100, 100 - used_pct))


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def fmt_plan(plan: str) -> str:
    if not plan or plan == "?":
        return "?"
    return str(plan).replace("_", " ").title()


def fmt_money(value: str, currency: str) -> str:
    symbol = {"USD": "$", "CNY": "CNY "}.get(currency, f"{currency} ")
    try:
        amount = float(value)
        return f"{symbol}{amount:,.2f}"
    except Exception:
        return f"{symbol}{value}"


def fmt_dt(dt: datetime.datetime) -> str:
    return f"{dt.strftime('%m-%d %H:%M')} {TZ_ABBR}"


def fmt_reset_dt(dt: datetime.datetime) -> str:
    _bare_zh = ["一", "二", "三", "四", "五", "六", "日"]
    _bare_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    today = datetime.datetime.now(TZ_LOCAL).date()
    target = dt.date()
    days = (target - today).days
    next_week = target.isocalendar()[:2] > today.isocalendar()[:2]
    if LANG == "zh":
        if days == 0:
            wd = "今天  "
        elif days == 1:
            wd = "明天  "
        elif days == 2:
            wd = "后天  "
        elif next_week:
            wd = f"下周{_bare_zh[dt.weekday()]}"
        else:
            wd = f"周{_bare_zh[dt.weekday()]}  "
    else:
        if days == 0:
            wd = "today   "
        elif days == 1:
            wd = "tomorrow"
        elif days == 2:
            wd = "2 days  "
        elif next_week:
            wd = f"next {_bare_en[dt.weekday()]}"
        else:
            wd = f"{_bare_en[dt.weekday()]:<8}"
    return f"{wd} {dt.strftime('%m-%d %H:%M')} {TZ_ABBR}"


# ── Claude 解析 ───────────────────────────────────────────────────────────────

def collect_claude(since: datetime.datetime):
    """
    返回 {model: {input, cache_create, cache_read, output, calls, days: set}}
    since 必须是 aware datetime (UTC)
    """
    totals: dict[str, dict] = {}
    since_ts = since.timestamp()
    for jf in sorted(CLAUDE_BASE.rglob("*.jsonl")):
        try:
            if jf.stat().st_mtime < since_ts:
                continue
            _parse_claude_file(jf, since, totals)
        except Exception:
            pass
    return totals


def _parse_claude_file(jf: pathlib.Path, since: datetime.datetime, totals: dict):
    with open(jf, errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "assistant":
                continue
            ts_raw = rec.get("timestamp", "")
            if not ts_raw:
                continue
            t = datetime.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if t < since:
                continue
            msg = rec.get("message") or {}
            usage = msg.get("usage") or {}
            model = msg.get("model", "unknown")
            totals.setdefault(model, {
                "input": 0, "cache_create": 0, "cache_read": 0,
                "output": 0, "calls": 0, "days": set(),
            })
            d = totals[model]
            d["input"] += usage.get("input_tokens", 0)
            d["cache_create"] += usage.get("cache_creation_input_tokens", 0)
            d["cache_read"] += usage.get("cache_read_input_tokens", 0)
            d["output"] += usage.get("output_tokens", 0)
            d["calls"] += 1
            d["days"].add(t.astimezone(TZ_LOCAL).date())


def latest_codex_rate_limits():
    """返回 (timestamp, rate_limits_dict) 或 (None, None)"""
    latest_ts = None
    latest_rl = None
    for jf in sorted(CODEX_BASE.rglob("*.jsonl")):
        try:
            ts, rl = _scan_codex_file(jf)
        except Exception:
            continue
        if rl and (latest_ts is None or ts > latest_ts):
            latest_ts = ts
            latest_rl = rl
    return latest_ts, latest_rl


def _scan_codex_file(jf: pathlib.Path):
    best_ts = None
    best_rl = None
    with open(jf, errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "event_msg":
                continue
            payload = rec.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            rl = payload.get("rate_limits")
            if not rl:
                continue
            ts = datetime.datetime.fromisoformat(
                rec["timestamp"].replace("Z", "+00:00")
            )
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best_rl = rl
    return best_ts, best_rl


def collect_codex_tokens(since: datetime.datetime):
    """返回 {date: {input, output, calls}} 按日汇总"""
    by_day: dict = {}
    for jf in sorted(CODEX_BASE.rglob("*.jsonl")):
        try:
            _parse_codex_file(jf, since, by_day)
        except Exception:
            pass
    return by_day


def _parse_codex_file(jf: pathlib.Path, since: datetime.datetime, by_day: dict):
    session_last: dict[str, dict] = {}  # turn_id → last token_count
    with open(jf, errors="replace") as f:
        for line in f:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("type") != "event_msg":
                continue
            payload = rec.get("payload") or {}
            if payload.get("type") != "token_count":
                continue
            ts = datetime.datetime.fromisoformat(
                rec["timestamp"].replace("Z", "+00:00")
            )
            if ts < since:
                continue
            info = payload.get("info") or {}
            last_usage = info.get("last_token_usage") or {}
            day = ts.astimezone(TZ_LOCAL).date()
            by_day.setdefault(day, {"input": 0, "output": 0, "calls": 0})
            by_day[day]["input"] += last_usage.get("input_tokens", 0)
            by_day[day]["output"] += last_usage.get("output_tokens", 0)
            by_day[day]["calls"] += 1


# ── 渲染 ─────────────────────────────────────────────────────────────────────

SEP = "─" * 52


def render_claude(totals: dict, since: datetime.datetime, days_count: int,
                  web_data: dict = None, web_error: str = None, detail: bool = False):
    title = "Claude Code"
    print(f"\n{_DIM}{SEP}{_RST}")
    print(f"{_BOLD}{title.center(52)}{_RST}")
    print()
    since_local = since.astimezone(TZ_LOCAL)
    print(f"  {_DIM}{t('统计自', 'Since')}: {fmt_dt(since_local)}  ({t(f'近 {days_count} 天', f'last {days_count} days')}){_RST}")

    if not totals:
        print(t("  （该时间段无记录）", "  (no records in this period)"))
        return

    active = {m: d for m, d in totals.items() if m != "<synthetic>"}
    grand_out = sum(d["output"] for d in active.values())
    grand_in_net = sum(d["input"] + d["cache_create"] for d in active.values())
    show_ratio = len(active) > 1 and grand_out > 0

    if detail:
        for model in sorted(active.keys()):
            d = active[model]
            total_in = d["input"] + d["cache_create"] + d["cache_read"]
            cache_pct = d["cache_read"] / total_in * 100 if total_in else 0
            if show_ratio:
                pct = d["output"] / grand_out * 100
                pct_s = '<1%' if pct < 1 else f'{pct:.0f}%'
                ratio_str = t(f"  (占总输出 {pct_s})", f"  ({pct_s} of total output)")
            else:
                ratio_str = ""
            print(f"  {model}")
            print(f"    {t('调用次数', 'Calls')}: {d['calls']:,}")
            print(f"    {t('输入合计', 'Input')}: {fmt_tokens(total_in):>8}  ({t(f'缓存命中 {cache_pct:.0f}%', f'cache hit {cache_pct:.0f}%')})")
            print(f"    {t('输出合计', 'Output')}: {fmt_tokens(d['output']):>8}{ratio_str}")
            actual_days = len(d["days"])
            if actual_days > 0:
                rate = d["output"] / actual_days
                print(f"    {t('日均输出', 'Daily avg')}: {fmt_tokens(int(rate)):>8}  ({t(f'共 {actual_days} 天有记录', f'{actual_days} days recorded')})")
            print()

    print(f"  {t('总输出', 'Total output')}: {_BOLD}{fmt_tokens(grand_out)}{_RST}  |  {t('净输入(非缓存)', 'Net input (non-cache)')}: {_BOLD}{fmt_tokens(grand_in_net)}{_RST}")
    if show_ratio:
        print(f"\n  {_BOLD}{t('输出占比', 'Output share')}{_RST}")
        name_w = max(len(m.replace("claude-", "")) for m in active)
        for m in sorted(active.keys(), key=lambda x: active[x]["output"], reverse=True):
            pct = active[m]["output"] / grand_out * 100
            pct_str = "<1%" if pct < 1 else f"{pct:.0f}%"
            short = m.replace("claude-", "")
            print(f"  {short:<{name_w}}  {_bold_bar(pct)}  {pct_str}")
    if web_data is not None:
        five_h = web_data.get("five_hour") or {}
        seven_d = web_data.get("seven_day") or {}
        if five_h or seven_d:
            print(f"\n  {_BOLD}{t('实时额度', 'Live quota')}{_RST}  {_DIM}{t('(与 --days 统计范围无关)', '(independent of --days range)')}{_RST}")
            print(f"  {_DIM}{t('数据来源', 'Source')}: claude.ai usage API  ({t('浏览器登录态', 'browser session')}){_RST}")
            print()
            for win_key, label, win in [
                ("5h", t("5小时滚动窗", "5-hour window"), five_h),
                ("7d", t("7天滚动窗  ", "7-day window "), seven_d),
            ]:
                if not win:
                    continue
                used = float(win.get("utilization", 0))
                remaining = remaining_percent(used)
                r_str = f"{_bc(remaining)}{_BOLD}{remaining:.0f}%{_RST}"
                print(f"  {label}  {_colored_bar(remaining)}  {t(f'剩余 {r_str}  {_DIM}(已用 {used:.0f}%){_RST}', f'left {r_str}  {_DIM}(used {used:.0f}%){_RST}')}")
                resets_at = win.get("resets_at")
                reset_dt = None
                if resets_at:
                    try:
                        reset_dt = datetime.datetime.fromisoformat(resets_at).astimezone(TZ_LOCAL)
                        print(f"  {_DIM}{t('重置时间', 'Resets at')}: {fmt_reset_dt(reset_dt)}{_RST}")
                    except Exception:
                        pass
                printed_estimate = False
                if win_key == "7d" and used and reset_dt:
                    window_min = 7 * 24 * 60
                    elapsed = (datetime.timedelta(minutes=window_min)
                               - (reset_dt - datetime.datetime.now(TZ_LOCAL)))
                    if elapsed.total_seconds() > 0:
                        rate = used / (elapsed.total_seconds() / 3600)
                        if rate > 0:
                            hours_left = remaining / rate
                            print(f"\n  📊 {_DIM}{t(f'按当前速率 ({rate:.1f}%/小时)，剩余 {remaining:.0f}% 约可用', f'At current rate ({rate:.1f}%/hr), {remaining:.0f}% left ≈')}{_RST} {_BOLD}{hours_left:.0f} {t('小时', 'hrs')}{_RST}")
                            printed_estimate = True
                if not printed_estimate:
                    print()
        else:
            print(f"\n  {t('claude.ai usage 原始响应', 'claude.ai usage raw response')}: {json.dumps(web_data, ensure_ascii=False)[:400]}")
            print(f"  →  {CLAUDE_USAGE_URL}  ({t('Cmd+双击打开', 'Cmd+double-click to open')})")
    elif web_error:
        print(f"\n  {t('实时额度  (与 --days 统计范围无关)', 'Live quota  (independent of --days range)')}")
        print(f"  ⚠️  {t('读取失败', 'Failed to fetch')}: {web_error}")
        print(f"  →  {CLAUDE_USAGE_URL}  ({t('Cmd+双击打开', 'Cmd+double-click to open')})")
    else:
        print(f"\n  ⚠️  {t('Claude 周额度百分比本地不可得', 'Claude quota unavailable locally')}  →  {CLAUDE_USAGE_URL}  ({t('Cmd+双击打开', 'Cmd+double-click to open')})")


def render_codex(since: datetime.datetime):
    title = "CodeX (OpenAI GPT-5)"
    print(f"\n{_DIM}{SEP}{_RST}")
    print(f"{_BOLD}{title.center(52)}{_RST}")
    print()

    ts, rl, source, fallback_reason = resolve_codex_rate_limits(latest_codex_rate_limits)
    if not rl:
        if source == "no_access":
            print(f"  {_WARN}{t('未检测到 Codex 权限', 'No Codex access detected')}{_RST}")
            print(f"  {_DIM}{fallback_reason}{_RST}")
        else:
            if fallback_reason:
                print(f"  {t('实时读取失败', 'Live fetch failed')}: {fallback_reason}")
            print(t("  （未找到 CodeX 数据）", "  (no CodeX data found)"))
        return

    now_local = datetime.datetime.now(TZ_LOCAL)
    ts_local = ts.astimezone(TZ_LOCAL)

    source_labels = {
        "live": t("实时", "live"),
        "web": t("实时(网页)", "live (web)"),
        "snapshot": t("本地快照", "snapshot"),
    }
    source_details = {
        "live": "codex app-server WebSocket",
        "web": t("chatgpt.com usage API  (浏览器登录态)", "chatgpt.com usage API  (browser session)"),
        "snapshot": t("本地快照", "local snapshot") + " (~/.codex/sessions/)",
    }
    print(f"  {_DIM}{t('数据时间', 'Data time')}: {fmt_dt(ts_local)}  ({source_labels[source]}){_RST}")
    print(f"  {_DIM}{t('数据来源', 'Source')}: {source_details[source]}{_RST}")
    if fallback_reason and source == "snapshot":
        print(f"  {t('实时读取失败', 'Live fetch failed')}: {fallback_reason}")
    plan = rl.get("plan_type") or "?"
    print(f"  {t('套餐', 'Plan')}: {_BOLD}{fmt_plan(plan)}{_RST}")
    print()

    secondary = rl.get("secondary") or {}
    primary = rl.get("primary") or {}

    data_age_min = (now_local - ts_local).total_seconds() / 60

    # 5-hour window
    p_pct = primary.get("used_percent", 0)
    p_remaining = remaining_percent(p_pct)
    p_reset = epoch_to_local(primary["resets_at"]) if primary.get("resets_at") else None
    p_min = primary.get("window_minutes", 300)
    p_stale = source == "snapshot" and data_age_min > p_min
    p_label = t("5小时滚动窗", "5-hour window")

    if p_stale:
        if p_reset and now_local >= p_reset:
            # 快照过期且窗口重置时间已过；web/live 都失败 → 保守推断已重置
            full_str = f"{_OK}{_BOLD}100%{_RST}"
            print(f"  {p_label}  {_colored_bar(100)}  {t(f'剩余 {full_str}  {_DIM}(推断：CLI 无新记录，可能漏检 Cloud){_RST}', f'left {full_str}  {_DIM}(inferred: no new CLI usage; Cloud may be missed){_RST}')}")
            print(f"  {_DIM}{t('重置时间', 'Reset at')}: {fmt_reset_dt(p_reset)}{_RST}")
        elif p_reset:
            print(f"  {p_label}  {_DIM}{t(f'快照已过期，预计 {fmt_reset_dt(p_reset)} 后恢复', f'snapshot expired, expected reset at {fmt_reset_dt(p_reset)}')}{_RST}")
        else:
            age_h = data_age_min / 60
            print(f"  {p_label}  {_DIM}{t(f'快照已过期 ({age_h:.0f}h 前)', f'snapshot expired ({age_h:.0f}h ago)')}{_RST}  →  {CODEX_USAGE_URL}")
    else:
        p_r_str = f"{_bc(p_remaining)}{_BOLD}{p_remaining:.0f}%{_RST}"
        print(f"  {p_label}  {_colored_bar(p_remaining)}  {t(f'剩余 {p_r_str}  {_DIM}(已用 {p_pct:.0f}%){_RST}', f'left {p_r_str}  {_DIM}(used {p_pct:.0f}%){_RST}')}")
        if p_reset:
            print(f"  {_DIM}{t('重置时间', 'Resets at')}: {fmt_reset_dt(p_reset)}{_RST}")
    print()

    # 7-day window
    w_pct = secondary.get("used_percent", 0)
    w_remaining = remaining_percent(w_pct)
    w_reset = epoch_to_local(secondary["resets_at"]) if secondary.get("resets_at") else None
    w_min = secondary.get("window_minutes", 10080)
    if w_min:
        days = w_min // 60 // 24
        w_label = t(f"{days}天滚动窗  ", f"{days}-day window ")
    else:
        w_label = t("周额度    ", "Weekly quota")
    w_stale = bool(source == "snapshot" and w_min and data_age_min > w_min)
    w_r_str = f"{_bc(w_remaining)}{_BOLD}{w_remaining:.0f}%{_RST}"
    if w_stale and w_reset and now_local >= w_reset:
        full_str = f"{_OK}{_BOLD}100%{_RST}"
        print(f"  {w_label}  {_colored_bar(100)}  {t(f'剩余 {full_str}  {_DIM}(推断：已重置){_RST}', f'left {full_str}  {_DIM}(inferred: reset){_RST}')}")
        print(f"  {_DIM}{t('重置时间', 'Reset at')}: {fmt_reset_dt(w_reset)}{_RST}")
    else:
        age_note = f"  {_DIM}{t(f'{data_age_min/60:.0f}h 前快照', f'snapshot {data_age_min/60:.0f}h ago')}{_RST}" if p_stale else ""
        print(f"  {w_label}  {_colored_bar(w_remaining)}  {t(f'剩余 {w_r_str}  {_DIM}(已用 {w_pct:.0f}%){_RST}', f'left {w_r_str}  {_DIM}(used {w_pct:.0f}%){_RST}')}{age_note}")
        if w_reset:
            print(f"  {_DIM}{t('重置时间', 'Resets at')}: {fmt_reset_dt(w_reset)}{_RST}")

    # remaining quota estimate
    if w_pct and w_reset:
        remaining_pct = 100 - w_pct
        elapsed_since_reset = (
            datetime.timedelta(minutes=w_min)
            - (w_reset - datetime.datetime.now(TZ_LOCAL))
        )
        if elapsed_since_reset.total_seconds() > 0:
            rate_per_hour = w_pct / (elapsed_since_reset.total_seconds() / 3600)
            if rate_per_hour > 0:
                hours_left = remaining_pct / rate_per_hour
                print(f"\n  📊 {_DIM}{t(f'按当前速率 ({rate_per_hour:.1f}%/小时)，剩余 {remaining_pct:.0f}% 约可用', f'At current rate ({rate_per_hour:.1f}%/hr), {remaining_pct:.0f}% left ≈')}{_RST} {_BOLD}{hours_left:.0f} {t('小时', 'hrs')}{_RST}")


def render_summary():
    print(f"\n{_DIM}{SEP}{_RST}\n")


def render_deepseek():
    title = "DeepSeek API"
    print(f"\n{_DIM}{SEP}{_RST}")
    print(f"{_BOLD}{title.center(52)}{_RST}")
    print()
    try:
        ts, data = live_deepseek_balance()
    except DeepSeekAuthError as e:
        print(f"  ⚠️  {t('读取失败', 'Failed to fetch')}: {e}")
        return
    except DeepSeekError as e:
        print(f"  ⚠️  {t('读取失败', 'Failed to fetch')}: {e}")
        return

    print(f"  {_DIM}{t('数据时间', 'Data time')}: {fmt_dt(ts.astimezone(TZ_LOCAL))}  (api key live){_RST}")
    print(f"  {_DIM}{t('数据来源', 'Source')}: api.deepseek.com /user/balance  (API key){_RST}")
    print()
    available = data.get("is_available")
    print(f"  {t('可用状态', 'Availability')}: {_BOLD}{t('可用', 'available') if available else t('余额不足', 'insufficient')}{_RST}")
    for item in data.get("balance_infos") or []:
        currency = item.get("currency", "?")
        total = fmt_money(item.get('total_balance', '0'), currency)
        granted = fmt_money(item.get('granted_balance', '0'), currency)
        topped = fmt_money(item.get('topped_up_balance', '0'), currency)
        print(f"  {currency:<4} {t('总余额', 'Total')}: {_BOLD}{total}{_RST}  |  {t('赠送', 'Granted')}: {granted}  |  {t('充值', 'Topped-up')}: {topped}")


def render_google():
    title = "Google (Antigravity)"
    print(f"\n{_DIM}{SEP}{_RST}")
    print(f"{_BOLD}{title.center(52)}{_RST}")
    print()
    try:
        ts, data = live_google_quota()
    except GoogleQuotaAuthError as e:
        print(f"  ⚠️  {t('读取失败', 'Failed to fetch')}: {e}")
        return
    except GoogleQuotaError as e:
        print(f"  ⚠️  {t('读取失败', 'Failed to fetch')}: {e}")
        return

    print(f"  {_DIM}{t('数据时间', 'Data time')}: {fmt_dt(ts.astimezone(TZ_LOCAL))}  (oauth live){_RST}")
    print(f"  {_DIM}{t('数据来源', 'Source')}: cloudcode-pa.googleapis.com v1internal:retrieveUserQuota  (Gemini / Antigravity OAuth){_RST}")
    print()

    summary = data.get("summary") or {}
    primary = data.get("primary") or {}
    remaining = summary.get("remaining_percent")
    remaining_label = "?" if remaining is None else f"{remaining:.0f}%"
    primary_model = primary.get("model_id") or "?"
    bucket_count = summary.get("bucket_count", 0)
    print(f"  {t('保守汇总', 'Conservative summary')}: {_BOLD}{remaining_label}{_RST}  |  {t('模型桶', 'Buckets')}: {bucket_count}")
    print(f"  {t('主参考模型', 'Primary reference model')}: {_BOLD}{primary_model}{_RST}")
    reset_time = summary.get("reset_time")
    if reset_time:
        print(f"  {_DIM}{t('重置时间', 'Resets at')}: {fmt_reset_dt(ts_to_local(reset_time))}{_RST}")

    buckets = data.get("buckets") or []
    if buckets:
        print(f"\n  {_BOLD}{t('主要模型', 'Key models')}{_RST}")
        for bucket in buckets[:4]:
            model_id = bucket.get("model_id") or "?"
            pct = bucket.get("remaining_percent")
            pct_text = "?" if pct is None else f"{pct:.0f}%"
            print(f"  {model_id:<28} {_bold_bar(pct or 0)}  {pct_text}")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=t("查看 Claude / CodeX / Google 本周消耗", "Show Claude / CodeX / Google token usage and quota"),
    )
    parser.add_argument("--days", type=int, default=7,
                        help=t("统计最近 N 天（默认 7）", "show last N days (default: 7)"))
    parser.add_argument("--all", action="store_true",
                        help=t("统计全部历史（忽略 --days）", "show all history (overrides --days)"))
    parser.add_argument("--detail", action="store_true",
                        help=t("展示每个模型的详细 token 统计", "show per-model token breakdown"))
    args = parser.parse_args()

    now_utc = datetime.datetime.now(datetime.timezone.utc)

    if args.all:
        since = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
        days_count = (now_utc - since).days
    else:
        since = now_utc - datetime.timedelta(days=args.days)
        days_count = args.days

    now_local = datetime.datetime.now(TZ_LOCAL)
    _wd_zh = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    _wd_en = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    wd_now = _wd_zh[now_local.weekday()] if LANG == "zh" else _wd_en[now_local.weekday()]
    print(f"\n{_DIM}{t('查询时间', 'Queried at')}: {wd_now} {now_local.strftime('%m-%d %H:%M')} {TZ_ABBR}{_RST}")

    claude_totals = collect_claude(since)

    web_data, web_error = None, None
    try:
        web_data = live_claude_usage()
    except ClaudeWebError as e:
        web_error = str(e)

    render_claude(claude_totals, since, days_count, web_data=web_data, web_error=web_error, detail=args.detail)
    render_codex(since)
    if has_deepseek_api_key():
        render_deepseek()
    if has_google_oauth_creds():
        render_google()
    render_summary()


if __name__ == "__main__":
    main()
