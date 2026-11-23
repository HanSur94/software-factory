#!/usr/bin/env bash
# Runs the factory board as a launchd service on this Mac: it starts at login, restarts when it
# dies and keeps running when the terminal that started it closes. Run it again after moving the
# repository; `--remove` takes the service away.
set -euo pipefail

LABEL="com.software-factory.board"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/software-factory-board.log"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="$(command -v python3)"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "--remove" ]; then
  rm -f "$PLIST"
  echo "Board-Dienst entfernt."
  exit 0
fi

# gh (GitHub CLI) and git must be found from inside launchd, which starts with a bare PATH.
SERVICE_PATH="$(dirname "$(command -v gh)"):$(dirname "$(command -v git)"):/usr/bin:/bin:/usr/sbin:/sbin"

mkdir -p "$(dirname "$PLIST")" "$(dirname "$LOG")"
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>$PYTHON</string><string>-m</string><string>factory</string><string>board</string></array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$SERVICE_PATH</string>
    <key>HOME</key><string>$HOME</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Board läuft als Dienst $LABEL (Log: $LOG)."
