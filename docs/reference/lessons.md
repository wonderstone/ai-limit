# 踩坑全录

## 001 · LaunchAgent 开机自启与 macOS 系统设置不同步

**现象**：用 `~/Library/LaunchAgents/` plist 实现开机自启，App 菜单里显示"已启用"（plist 存在），但 macOS 系统设置→登录项里显示为禁用/灰色，用户从系统设置无法开启。

**原因**：macOS 系统设置的"登录项"管理的是 Apple SMLoginItemSetEnabled API 注册的项目，与 LaunchAgent plist 是两套独立机制。LaunchAgent 会被 `launchd` 加载，但未公证的 App 会被 macOS 标记为不可信，系统设置里开关失效。

**解法**：
- 短期：移除该功能，避免用户困惑
- 长期：对 App 做公证（需 Developer ID Application 证书），或改用 SMLoginItemSetEnabled（需 Swift）

**相关**：Developer ID Application 证书已在 Apple Developer Portal（到期 2027/02/02），未下载安装。

---

## 002 · py2app bundle 里 AttributeError：菜单项 add 顺序

**现象**：`_about_menu.add(self._star_item)` 写在 `_star_item` 创建之前，App 启动时 crash，弹出 "Launch error" 对话框。

**原因**：`_build_menu` 里 `_about_menu` 的 `add` 调用发生在 `_star_item = rumps.MenuItem(...)` 之前，属性未赋值。

**解法**：把 `self._about_menu.add(self._star_item)` 移到 `self._star_item = ...` 赋值语句之后。

---

## 003 · Gitee Release 需要 token，无浏览器 OAuth

**现象**：GitHub 有 `gh` CLI 可以浏览器登录创建 Release，Gitee 没有等效官方 CLI。

**解法**：用 Gitee REST API + 个人令牌（只需 `projects` 权限）：
1. `POST /api/v5/repos/{owner}/{repo}/releases` 创建 Release，需带 `target_commitish`
2. `POST /api/v5/repos/{owner}/{repo}/releases/{id}/attach_files` 上传附件
用完后在 Gitee 令牌页撤销 token。

---

## 004 · Gemini 额度与 gemini.google.com/usage 对不上

**现象**：菜单栏/悬浮窗里的 Gemini 当前用量显示 2%，同一时刻 https://gemini.google.com/usage 显示 25%，之后实测已到 47%。

**原因**：`live_gemini_app_usage()` 把 `~/.cache/ai-limit/gemini-app-usage.json` 当成第一数据源，TTL 是 30 分钟。5 小时窗口在半小时里可以涨几十个百分点，所以缓存命中期间界面一直停留在半小时前的数字。Google 自己那一页大约每几分钟刷新一次。

**解法**：
- 新鲜期 TTL 降到 120 秒（`AI_LIMIT_GEMINI_APP_CACHE_TTL_SEC`）
- 旧快照只做离线兜底，最长 30 分钟（`AI_LIMIT_GEMINI_APP_CACHE_STALE_SEC`），并在 `source` 里标出 `(cached Ns)` / `(stale cache Ns)`
- 兜底 RPC 路径也要写缓存，之前只有主 RPC 路径写

---

## 005 · py2app 打包后 LLM API 全部「余额失败」

**现象**：App 连续运行两周后，LLM API 面板里 7 个 provider 全变成「余额失败」，重新用同一份代码在终端跑却全部正常。真实报错是 `Could not find a suitable TLS CA certificate bundle, invalid path: /var/folders/.../T/tmpXXXXcacert.pem`。

**原因**：`certifi` 被 py2app 塞进了 `python311.zip`，`certifi.where()` 只能把 `cacert.pem` 解压到 `/var/folders/.../T/` 的临时文件，并且只在进程存活期间保留。macOS 会清理几天没被访问的临时文件，长驻的菜单栏 App 于是丢掉了 CA 包，所有 `requests` 调用全部失败。`providers.py` 走 urllib + 系统 SSL，所以 Claude/Codex/Gemini 额度不受影响——只有 `llm_balance.py` 挂掉，这一点很容易误判成 API Key 失效。

**解法**：
- `menubar/setup.py` 的 `packages` 加上 `certifi`（和 `charset_normalizer`），让它落在磁盘上而不是 zip 里
- `llm_balance._ensure_ca_bundle()` 再兜一层：临时路径失效时把 `cacert.pem` 复制到 `~/.cache/ai-limit/cacert.pem`，同时改写 `certifi.where`、`requests.utils/adapters.DEFAULT_CA_BUNDLE_PATH` 和 `REQUESTS_CA_BUNDLE` / `SSL_CERT_FILE`
- 只设环境变量不够：阿里云 SDK（`Tea.core`）直接调 `certifi.where()`，requests 也在 import 时把 `DEFAULT_CA_BUNDLE_PATH` 冻住了

---

## 006 · 重建 .app 时踩的两个坑：构建环境丢失 + 依赖被 modulegraph 拖进来

**现象一**：想重新 py2app 打包，发现机器上已经没有任何装了 `py2app` + `rumps` 的 Python 3.11 了（pyenv 3.11.9 有运行期依赖但没有这两个，homebrew 只剩 3.14）。原构建环境应该是个已被删掉的 venv。

**现象二**：直接用 pyenv 3.11.9 装上 py2app 再打包，产物 **1.8 GB**（原版 71 MB），`lib/python3.11/` 里混进了 matplotlib、numpy、pydantic、sqlalchemy、shiboken6、psycopg2。那个 pyenv 环境装了 1389 个包，`modulegraph` 顺着 import 链全拖进来了，中途还会 `RecursionError: maximum recursion depth exceeded`。

**解法**：用干净的 venv 打包，只装 setup.py `packages` 里真正需要的东西：

```bash
cd /Users/mac/Desktop/ai-limit
python3.11 -m venv .venv     # .gitignore 里已经忽略 .venv/
.venv/bin/python -m pip install py2app rumps browser-cookie3 pycryptodomex \
    requests certifi charset-normalizer \
    alibabacloud-bssopenapi20171214 alibabacloud-tea-openapi
cd menubar && ../.venv/bin/python setup.py py2app
```

产物回到 74 MB，RecursionError 也一起消失了。

**另一个坑**：不要用 `runpy.run_path('setup.py')` 之类的包装去调 setup.py。`setup.py` 靠 `pathlib.Path(__file__).parent.parent` 把项目根塞进 `sys.path`，包装之后 `__file__` 变成相对路径，算出来是 `menubar/` 而不是项目根，`ai_limit` 和 `usage` 就**静默地没被打进去**——构建照样 exit 0，只有 build log 里 "Modules not found" 一行 `* ai_limit` 能看出来，App 启动才会崩。打完包务必确认 `dist/ai-limit.app/Contents/Resources/lib/python3.11/ai_limit/` 存在。

---

## 007 · Antigravity CLI 1.1.9 改了日志格式，429 也不再区分"模型额度耗尽"

**现象**：菜单栏和 CLI 的 Antigravity 数据来源一直是 `agy /usage fallback`，`antigravity` 字段恒为 `null`，「已触发额度限制」的告警横幅再也不出现。额度数字本身是对的，所以很久没被发现。

**定位**：版本时间线卡得很死——最后一个能解析的日志 `cli-20260731_172713.log`（17:27），第一个新格式日志 `cli-20260731_172756.log`（17:28），中间 43 秒就是 language server 1.1.8 → 1.1.9 的升级点。

**两处独立的破坏**：

1. **时间戳不在行首了**。新版每行都加 `ERROR: logging before google.Init: ` 前缀，`ANTIGRAVITY_LOG_TIME_RE` 用 `^[A-Z](\d{2})(\d{2}) ` 配合 `.match()` 直接失配。改成不锚定行首 + `.search()` 即可，新旧两种格式都能解析。

2. **限流消息丢了模型名和倒计时**。旧版是 `RESOURCE_EXHAUSTED (code 429): Individual quota reached. … Resets in 3h20m`，新版只剩 `RESOURCE_EXHAUSTED (code 429): Resource has been exhausted (e.g. check quota).`。在最新 80 个日志里扫 `Resets in` / `Individual quota` / `quota reached` / `retryDelay` / `quotaId` **全部 0 命中**，`quota_manager.go` 也只打 `doRefreshQuota: starting reload`，不带任何额度数值。

**关键陷阱**：不要图省事去匹配裸的 `RESOURCE_EXHAUSTED (code 429)`。实际日志里这条消息的来源是 `Cache(userInfo): Singleflight refresh failed` / `failed to fetch user status` / `Failed to refresh cache in background`——**是 userInfo 缓存刷新被限流，不是模型额度耗尽**，这时候额度可能还很充足。照着匹配会得到假的「额度耗尽」横幅，比没有横幅更糟。

另外统计 `429` 子串会严重高估：`429.83525ms`、线程号 `429`、UUID `d08653c4-3dc2-429a` 都会命中，200 个日志里 14 个"含 429"实际只有 1 个真的含 `RESOURCE_EXHAUSTED`。

**解法**：横幅改由额度数字驱动（`_antigravity_limit_from_quota`）——哪个 bucket 的 `remaining_percent == 0` 就是它，重置时间直接取该 bucket 的 `reset_time`，比日志里那个 429 当时打印的倒计时更权威。挂在 `_normalize_antigravity_quota_summary` 的出口上，app sidecar 和 `agy /usage` 两条路径一次覆盖。旧格式日志仍然优先（它还能给出具体模型名和触发时刻），解析不到才回退到额度数字。

**副产品**：`agy /usage` 这条路径以前根本没有 `antigravity` 字段（只有 REST 兜底路径的 `_with_antigravity_view` 会加），所以就算日志能解析，走 `/usage` 时横幅也不会出现。

---

## 008 · 驱动 agy TUI 抓额度：超时静默、半屏数据当成功、降级冒充成功

**现象**：菜单栏 Antigravity 卡片显示「未知」。CLI 和本地 API 却是正常数字——因为它们跟菜单栏共用 `~/.cache/ai-limit/antigravity-cli-usage.json`（TTL 5 分钟），只要在终端跑过一次，菜单栏就会"恢复"5 分钟，然后再变回未知。这个共享缓存把问题伪装成时好时坏。

**失败链**（四层，每层都在吞信息）：

1. `_run_antigravity_cli_usage_text` 超时**不抛异常**，只 `return` 手上那点残缺抄本
2. `_parse_antigravity_cli_usage_text` 找不到 `MODELS & QUOTA` → `GoogleQuotaError`
3. `live_google_quota` 的 `except GoogleQuotaError: pass` 静默吞掉，落到第三级
4. 第三级 `_with_antigravity_view` 返回 `_antigravity_model_buckets` 拿模型名单硬凑的空壳，`remaining_percent` 全是 `None`——菜单栏 `_has_windowed_quota()` 因此为 False，渲染成「未知」

**定位数据**（同一份代码，三种环境）：

| 环境 | agy 驱动耗时 |
|---|---|
| 终端 | 3.9–5.3s |
| GUI app 进程 | 12s |
| 跑了 34 小时的旧 app 进程 | 稳定 18s 撞满超时，每次都失败 |

GUI 进程里本来就慢 2.5–3 倍，而 `ANTIGRAVITY_CLI_USAGE_TIMEOUT_SEC` 只有 18s。**排除掉的假设**（都实测过）：PATH（app 启动时已前置 `~/.local/bin`，agy 确实被 spawn 了）、无控制终端（`os.setsid()` 后跑通）、环境变量（完全复刻 app 的 env 跑通）、并发争用（趁 app 那次跑着插进去也跑通）、App Sandbox（`codesign -d --entitlements` 为空）。

**更严重的第二个坑**：超时**不等于**解析失败。加了 dump 之后第一次就抓到——翻页抢在渲染前面：`/usage` 标题一出现就连按两次 PageDown，Gemini 组还没渲染就被跳过，该组从此不在抄本里，退出判据永远等不到，跑满 30s；但抄本里有 `MODELS & QUOTA`，解析照样"成功"，于是**半屏数据被写进缓存**：

```
group_count = 1 | bucket_count = 2       （正常应为 2 组 4 个）
  claude-and-gpt-models-weekly  99%
  claude-and-gpt-models-5h     100%
```

Gemini 组整个丢了，`primary` 从只剩的 bucket 里挑，菜单栏有 5 分钟显示 **99%**，真值 80%。**显示错数字比显示「未知」更糟**，而且在加仪表盘之前完全隐形。

**解法**（四处）：

1. **超时 18s → 30s**，GUI 里观测值 12s 的 2.5 倍，仍在 app 60s 刷新周期内留足其他 provider 的时间
2. **超时即拒绝**：完整屏幕必然满足退出条件并走 `/exit`，所以超时 ⟹ 抄本不完整，直接抛错不写缓存。宁可没数不要错数
3. **翻页加门槛**：从「看到 `Models & Quota` 标题就翻」改成「还要看到 `Limit Remaining` 正文才翻」。这个改动是单调的——只推迟翻页、不跳过任何东西，不会破坏本来正常的路径
4. **降级不再冒充成功**：空壳视图的 `quota_state` 从 `unknown` 改成 `unavailable` + `unavailable_reason`，`live_google_quota` 把前两级的失败原因往下传；菜单栏显示从「未知」改成「取数失败」+ 具体原因。「未知」暗示的是额度状态未知，实际是取数管道断了，这个区别正是排查绕远路的原因

**真正的教训是可观测性**：这条路径原本超时不抛错、原始 pty 文本直接丢弃、降级静默，外面只看到「未知」，零线索。现在 `_run_antigravity_cli_usage_text` 接收 `trace`（走到哪一步 / 耗时 / 是否超时 / 抓了多少字节 / 子进程退出码），失败时 `_dump_antigravity_cli_debug` 把完整抄本 + trace + marker 计数 + `>` 提示符是否命中写到 `~/.cache/ai-limit/agy-usage-debug.txt`（滚动保留 3 次）。上面那个「半屏当成功」的坑就是这个 dump 抓出来的，否则根本不会被发现。

**排查手法备忘**：菜单栏 app 的状态可以从 `~/.ai-limit-menubar-cache.json` 直接读，不用猜 UI 显示什么；确认 app 是否真的 spawn 了子进程用 `ps -eo pid,ppid,etime,comm` 采样，`etime` 恰好等于超时值就是撞满了；跨缓存过期的那一刻才是检验点（`agy_cache_age` 超过 TTL 后 app 能否自己刷新成功），缓存新鲜时一切都看起来正常。

**遗留**：`claude-and-gpt-models-5h` 的百分比经常是 `None`——退出判据 `text.count("Refreshes in") >= 3` 在屏幕翻完前就触发，最后一个 bucket 被分页器截在 `(1–20 of 30 lines)`。改它要重新设计滚动逻辑，风险比收益大，且现在失败会留 dump。另外 GUI 进程里为什么比终端慢 2.5–3 倍，没查出来。
