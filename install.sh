#!/usr/bin/env bash
# ai-limit menubar app — 一键安装脚本
#
# 用法：
#   # 安装菜单栏 App（默认，含开机自启）
#   curl -fsSL https://raw.githubusercontent.com/zhuchenxi113/ai-limit/main/install.sh | bash
#
#   # 安装后台守护进程（无菜单栏，仅通知 + 日志）
#   curl -fsSL https://raw.githubusercontent.com/zhuchenxi113/ai-limit/main/install.sh | bash -s -- --daemon
#
#   # 卸载
#   curl -fsSL https://raw.githubusercontent.com/zhuchenxi113/ai-limit/main/install.sh | bash -s -- --uninstall
#
# 流程：从 GitHub Releases 获取最新 DMG → 挂载 → 复制到 /Applications → 注册开机自启 → 卸载挂载 → 清理

set -euo pipefail

REPO="zhuchenxi113/ai-limit"
APP_NAME="ai-limit.app"
INSTALL_DIR="/Applications"
LAUNCH_AGENT_LABEL="com.zhuchenxi.ai-limit"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/${LAUNCH_AGENT_LABEL}.plist"
DAEMON_LABEL="com.zhuchenxi.ai-limit-daemon"
DAEMON_PLIST="$HOME/Library/LaunchAgents/${DAEMON_LABEL}.plist"
MODE="${1:-app}"   # app | daemon | uninstall

# ── 颜色输出 ────────────────────────────────────────────────────────────────
info()  { printf "  \033[34m•\033[0m %s\n" "$*"; }
ok()    { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn()  { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()   { printf "\n\033[31merror:\033[0m %s\n" "$*" >&2; exit 1; }

# ── 环境检查 ─────────────────────────────────────────────────────────────────
[[ "$(uname)" == "Darwin" ]] || die "仅支持 macOS"

# ── 卸载模式 ─────────────────────────────────────────────────────────────────
if [[ "$MODE" == "--uninstall" ]]; then
  info "正在卸载 ai-limit…"

  # 停止运行中的进程
  pkill -x "ai-limit" 2>/dev/null && ok "已停止 ai-limit 进程" || info "无运行中进程"

  # 注销 LaunchAgent
  if launchctl print "gui/$(id -u)/${LAUNCH_AGENT_LABEL}" &>/dev/null; then
    launchctl bootout "gui/$(id -u)/${LAUNCH_AGENT_LABEL}" 2>/dev/null || true
    ok "已从 launchd 注销 ${LAUNCH_AGENT_LABEL}"
  fi

  # 删除 plist
  rm -f "$LAUNCH_AGENT_PLIST" && ok "已删除 LaunchAgent plist"

  # 删除 daemon plist
  if launchctl print "gui/$(id -u)/${DAEMON_LABEL}" &>/dev/null; then
    launchctl bootout "gui/$(id -u)/${DAEMON_LABEL}" 2>/dev/null || true
    ok "已从 launchd 注销 ${DAEMON_LABEL}"
  fi
  rm -f "$DAEMON_PLIST"

  # 删除 App
  if [[ -d "${INSTALL_DIR}/${APP_NAME}" ]]; then
    rm -rf "${INSTALL_DIR}/${APP_NAME}"
    ok "已删除 ${INSTALL_DIR}/${APP_NAME}"
  fi

  # 清理数据文件
  rm -f "$HOME/.ai-limit-menubar.json" "$HOME/.ai-limit-menubar-cache.json" \
        "$HOME/.ai-limit-menubar.pid" "$HOME/.ai-limit-daemon.log" \
        "$HOME/.ai-limit-daemon-state.json" "$HOME/.ai-limit-daemon.pid" \
        "$HOME/.ai-limit-launchd.log"

  ok "卸载完成"
  exit 0
fi

# ── 守护进程模式 ─────────────────────────────────────────────────────────────
if [[ "$MODE" == "--daemon" ]]; then
  info "安装后台守护进程…"

  # 检查 Python 环境
  if ! command -v python3 &>/dev/null; then
    die "未找到 python3，请先安装 Python 3.10+"
  fi

  # 安装/升级依赖
  info "安装依赖…"
  pip3 install --quiet --upgrade browser-cookie3 2>/dev/null || \
    die "browser-cookie3 安装失败，请检查 pip3 和网络"

  # 下载完整包到 ~/.ai-limit/
  REPO_DIR="$HOME/.ai-limit"
  mkdir -p "$REPO_DIR/ai_limit"

  BASE_URL="https://raw.githubusercontent.com/${REPO}/main"
  info "下载 ai-limit 模块…"
  for f in __init__.py daemon.py providers.py i18n.py; do
    curl -fsSL "$BASE_URL/ai_limit/$f" -o "$REPO_DIR/ai_limit/$f" || die "下载 ai_limit/$f 失败"
  done
  curl -fsSL "$BASE_URL/usage.py" -o "$REPO_DIR/usage.py" || die "下载 usage.py 失败"

  PYTHON_PATH="$(command -v python3)"
  mkdir -p "$HOME/Library/LaunchAgents"

  cat > "$DAEMON_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.zhuchenxi.ai-limit-daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>$PYTHON_PATH</string>
        <string>-m</string>
        <string>ai_limit.daemon</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$HOME/.ai-limit-daemon.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.ai-limit-daemon.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLIST

  launchctl bootstrap "gui/$(id -u)" "$DAEMON_PLIST" 2>/dev/null || \
    die "launchctl bootstrap 失败，请手动执行: launchctl bootstrap gui/$(id -u) $DAEMON_PLIST"

  ok "守护进程已安装并启动"
  printf "\n"
  info "日志: $HOME/.ai-limit-daemon.log"
  info "停止: launchctl bootout gui/$(id -u)/${DAEMON_LABEL}"
  info "卸载: curl -fsSL https://raw.githubusercontent.com/zhuchenxi113/ai-limit/main/install.sh | bash -s -- --uninstall"
  exit 0
fi

# ── 菜单栏 App 模式（默认）───────────────────────────────────────────────────

# ── 获取最新 Release 下载链接 ────────────────────────────────────────────────
info "查询最新版本…"
API_URL="https://api.github.com/repos/${REPO}/releases/latest"

if command -v curl &>/dev/null; then
  RELEASE_JSON=$(curl -fsSL "$API_URL")
else
  die "未找到 curl，无法继续"
fi

VERSION=$(printf '%s' "$RELEASE_JSON" | grep '"tag_name"' | head -1 | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')
[[ -n "$VERSION" ]] || die "无法获取版本号，请检查网络或稍后重试"

DMG_URL=$(printf '%s' "$RELEASE_JSON" | grep '"browser_download_url"' | grep '\.dmg"' | head -1 | sed 's/.*"browser_download_url": *"\([^"]*\)".*/\1/')
[[ -n "$DMG_URL" ]] || die "Release ${VERSION} 中未找到 DMG 文件"

ok "最新版本：${VERSION}"

# ── 下载 DMG ─────────────────────────────────────────────────────────────────
TMP_DIR=$(mktemp -d -t ai-limit-install)
DMG_PATH="${TMP_DIR}/ai-limit.dmg"
trap 'rm -rf "$TMP_DIR"' EXIT

info "下载 DMG（${DMG_URL##*/}）…"
curl -fsSL --progress-bar -o "$DMG_PATH" "$DMG_URL"
ok "下载完成"

# ── 挂载 DMG ─────────────────────────────────────────────────────────────────
info "挂载磁盘映像…"
MOUNT_POINT=$(mktemp -d -t ai-limit-mount)
hdiutil attach "$DMG_PATH" -nobrowse -noverify -noautoopen -mountpoint "$MOUNT_POINT" \
  >/dev/null 2>&1 || die "挂载失败"

# ── 复制 App ─────────────────────────────────────────────────────────────────
SRC="${MOUNT_POINT}/${APP_NAME}"
[[ -d "$SRC" ]] || die "DMG 中未找到 ${APP_NAME}"

if [[ -d "${INSTALL_DIR}/${APP_NAME}" ]]; then
  info "检测到旧版本，先停止并移除…"
  pkill -x "ai-limit" 2>/dev/null || true
  rm -rf "${INSTALL_DIR}/${APP_NAME}"
fi

info "安装到 ${INSTALL_DIR}…"
cp -R "$SRC" "${INSTALL_DIR}/"

# ── 卸载挂载 ─────────────────────────────────────────────────────────────────
hdiutil detach "$MOUNT_POINT" -quiet 2>/dev/null || true

ok "ai-limit ${VERSION} 已安装到 ${INSTALL_DIR}/${APP_NAME}"

# ── 注册开机自启 ─────────────────────────────────────────────────────────────
info "注册开机自启…"
APP_EXEC="${INSTALL_DIR}/${APP_NAME}/Contents/MacOS/ai-limit"
mkdir -p "$(dirname "$LAUNCH_AGENT_PLIST")"

cat > "$LAUNCH_AGENT_PLIST" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LAUNCH_AGENT_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${APP_EXEC}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
    <key>StandardOutPath</key>
    <string>$HOME/.ai-limit-launchd.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.ai-limit-launchd.log</string>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$LAUNCH_AGENT_PLIST" 2>/dev/null || \
  warn "自动注册失败（首次启动 App 时会自动重试）"
ok "已注册开机自启（登录时自动启动）"

# ── 提示 ──────────────────────────────────────────────────────────────────────
printf "\n"
warn "首次启动提示（App 尚未公证）："
printf "    右键点击 ai-limit.app → 打开 → 仍要打开\n"
printf "\n"
info "当前即可启动："
printf "    open \"${INSTALL_DIR}/${APP_NAME}\"\n"
printf "\n"
info "下次登录时自动启动，无需手动操作。"
info "卸载: curl -fsSL https://raw.githubusercontent.com/zhuchenxi113/ai-limit/main/install.sh | bash -s -- --uninstall"
