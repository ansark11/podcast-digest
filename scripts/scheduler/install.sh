#!/bin/bash
# Installs the weekly launchd job on this Mac (see weekly_run.py).
#
# Everything lives outside ~/Documents, which background jobs can't read
# without extra macOS permissions:
#   ~/Library/Application Support/podcast-digest/repo   the job's own clone of main
#   ~/Library/Application Support/podcast-digest/venv   its Python environment
#   ~/Library/Logs/podcast-digest/run.log               output of every run
#
# Safe to re-run: it refreshes .env, dependencies and the launchd job.
# Run it from your working copy: scripts/scheduler/install.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/../.." && pwd)"
APP="$HOME/Library/Application Support/podcast-digest"
REPO="$APP/repo"
VENV="$APP/venv"
LOGS="$HOME/Library/Logs/podcast-digest"
LABEL="com.podcast-digest.weekly"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PYTHON="${PYTHON:-/opt/homebrew/bin/python3}"

[ -f "$SRC/.env" ] || { echo "Missing $SRC/.env (it needs OPENAI_API_KEY)"; exit 1; }
command -v ffmpeg >/dev/null || [ -x /opt/homebrew/bin/ffmpeg ] || { echo "ffmpeg not found: brew install ffmpeg"; exit 1; }
mkdir -p "$APP" "$LOGS" "$HOME/Library/LaunchAgents"

if [ ! -d "$REPO/.git" ]; then
  git clone -q --branch main "$(git -C "$SRC" remote get-url origin)" "$REPO"
fi
git -C "$REPO" pull -q --rebase origin main
[ -f "$REPO/scripts/scheduler/weekly_run.py" ] || { echo "main doesn't have scripts/scheduler yet; merge it first"; exit 1; }
install -m 600 "$SRC/.env" "$REPO/.env"

[ -x "$VENV/bin/python" ] || "$PYTHON" -m venv "$VENV"
"$VENV/bin/pip" install -q -r "$REPO/requirements.txt"

# The first run is the next Friday 9:00, not the moment this is installed.
[ -f "$APP/last_run" ] || date +%Y-%m-%dT%H:%M:%S > "$APP/last_run"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$VENV/bin/python</string>
    <string>$REPO/scripts/scheduler/weekly_run.py</string>
  </array>
  <!-- Fridays 9:00 local time. If the Mac is asleep then, launchd runs it on wake. -->
  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <!-- Catch-up triggers: at login (the Mac was off) and hourly (it was offline).
       weekly_run.py exits immediately unless a run is actually due. -->
  <key>RunAtLoad</key><true/>
  <key>StartInterval</key><integer>3600</integer>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
  <key>StandardOutPath</key><string>$LOGS/run.log</string>
  <key>StandardErrorPath</key><string>$LOGS/run.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo "Installed $LABEL. Next run: Friday 9:00."
echo "  Run now:   \"$VENV/bin/python\" \"$REPO/scripts/scheduler/weekly_run.py\" --force"
echo "  Log:       $LOGS/run.log"
echo "  Remove:    launchctl bootout gui/\$(id -u)/$LABEL && rm \"$PLIST\""
