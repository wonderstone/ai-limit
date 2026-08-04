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
