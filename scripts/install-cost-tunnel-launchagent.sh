#!/usr/bin/env bash
# Install a macOS LaunchAgent that keeps an SSH tunnel to the cost dashboard open,
# so http://127.0.0.1:<local-port>/cost is always reachable from this Mac. The
# agent auto-starts at login and auto-restarts if the tunnel drops. Idempotent.
#
#   bash scripts/install-cost-tunnel-launchagent.sh
#   TUNNEL_SSH_TARGET=invoker@1.2.3.4 TUNNEL_LOCAL_PORT=8899 bash scripts/install-cost-tunnel-launchagent.sh
#
# Uninstall:
#   launchctl bootout gui/$(id -u)/com.session-metrics.cost-tunnel
#   rm ~/Library/LaunchAgents/com.session-metrics.cost-tunnel.plist
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer is macOS-only (uses launchd). On Linux, run the ssh -L command from a systemd unit instead." >&2
  exit 1
fi

LABEL="${TUNNEL_LABEL:-com.session-metrics.cost-tunnel}"
SSH_TARGET="${TUNNEL_SSH_TARGET:-invoker@157.230.133.215}"   # user@host of the dashboard server
LOCAL_PORT="${TUNNEL_LOCAL_PORT:-8899}"                       # local port you'll open in the browser
REMOTE_PORT="${TUNNEL_REMOTE_PORT:-8788}"                     # port the dashboard listens on (server-side)
SSH_KEY="${TUNNEL_SSH_KEY:-$HOME/.ssh/id_ed25519}"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/cost-tunnel.log"

if [[ ! -f "$SSH_KEY" ]]; then
  echo "SSH key not found: $SSH_KEY (set TUNNEL_SSH_KEY)." >&2
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/ssh</string>
        <string>-i</string>
        <string>$SSH_KEY</string>
        <string>-N</string>
        <string>-o</string>
        <string>StrictHostKeyChecking=accept-new</string>
        <string>-o</string>
        <string>BatchMode=yes</string>
        <string>-o</string>
        <string>ExitOnForwardFailure=yes</string>
        <string>-o</string>
        <string>ServerAliveInterval=30</string>
        <string>-o</string>
        <string>ServerAliveCountMax=3</string>
        <string>-L</string>
        <string>${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}</string>
        <string>$SSH_TARGET</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>$LOG</string>
    <key>StandardErrorPath</key>
    <string>$LOG</string>
</dict>
</plist>
PLIST_EOF

UID_NUM="$(id -u)"
# free the port if something already forwards it, then (re)load the agent
pkill -f "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" 2>/dev/null || true
sleep 1
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST"
launchctl kickstart -k "gui/$UID_NUM/$LABEL" 2>/dev/null || true
sleep 3

echo "Installed cost-dashboard tunnel LaunchAgent:"
echo "  $SSH_TARGET  ->  http://127.0.0.1:${LOCAL_PORT}/cost  (remote :${REMOTE_PORT})"
echo "  plist: $PLIST"
echo "  logs:  $LOG"
if curl -fsS -o /dev/null --max-time 8 "http://127.0.0.1:${LOCAL_PORT}/healthz" 2>/dev/null; then
  echo "  status: tunnel up, dashboard reachable"
else
  echo "  status: agent loaded; if not reachable yet, check $LOG (server up? key authorized?)"
fi
