#!/bin/bash
# Install or remove the LaunchAgent that keeps the local quota API
# (ai_limit/api_server.py, http://127.0.0.1:17655) running at login.
#
#   ./install-api-agent.sh            install (or reinstall) and start
#   ./install-api-agent.sh uninstall  stop and remove
#
# AI_LIMIT_PYTHON overrides the interpreter (needs Python >= 3.10 with
# requirements.txt installed).
set -euo pipefail

LABEL="com.zhuchenxi.ai-limit-api"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs/ai-limit"
REPO="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${AI_LIMIT_PYTHON:-$HOME/.pyenv/versions/3.13.9/bin/python3.13}"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

if [[ "${1:-}" == "uninstall" ]]; then
  rm -f "$PLIST"
  echo "removed $LABEL"
  exit 0
fi

[[ -x "$PYTHON" ]] || { echo "python not found: $PYTHON" >&2; exit 1; }
mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"

# Binds 127.0.0.1 only. Codex app-server fallback stays opt-in per request
# (?allow_app_server=1) because it can start a Codex usage window.
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>-m</string>
    <string>ai_limit.api_server</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>17655</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <!-- launchd's bare PATH hides gh/gemini/copilot, which the Copilot and Google probes shell out to. -->
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key>
  <dict><key>SuccessfulExit</key><false/></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/api-server.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/api-server.err.log</string>
</dict>
</plist>
EOF

plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"
echo "loaded $LABEL -> http://127.0.0.1:17655 (logs: $LOG_DIR)"
