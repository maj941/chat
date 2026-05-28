#!/usr/bin/env bash
# Captain Chat bootstrap.
#
# Usage:
#   ./bootstrap.sh
#
# Copies all sources into /home/.local_chat/, installs Flask, downloads
# cloudflared if needed, starts the watchdog supervisor (which then starts
# Flask + the public tunnel), and waits for a public URL to appear.
set -e

SRC_DIR=$(cd "$(dirname "$(readlink -f "$0")")" && pwd)
BASE=/home/.local_chat

mkdir -p "$BASE/uploads" "$BASE/static"

# Install flask if missing
if ! python3 -c "import flask" >/dev/null 2>&1; then
  pip install --break-system-packages flask >/dev/null 2>&1 || pip install flask >/dev/null 2>&1
fi

# Copy sources (idempotent overwrite)
cp -f "$SRC_DIR"/app.py "$SRC_DIR"/chat.py "$SRC_DIR"/watchdog.py "$SRC_DIR"/remote.py "$BASE/"
cp -f "$SRC_DIR"/index.html "$BASE/"
chmod +x "$BASE"/*.py

# Pages-gateway config (optional). Env wins; otherwise persist a default.
if [ -n "$PAGES_URL" ]; then
  echo "$PAGES_URL" > "$BASE/pages_url.txt"
elif [ ! -s "$BASE/pages_url.txt" ]; then
  echo "https://maj941.github.io/chat/" > "$BASE/pages_url.txt"
fi

# cloudflared
if [ ! -x /tmp/cloudflared ]; then
  curl -fsSL -o /tmp/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x /tmp/cloudflared
fi

# Kill any previous instances by PID (avoid pkill matching this shell)
APP_PID=$(pgrep -f "$BASE/app.py" 2>/dev/null | head -1 || true)
[ -n "$APP_PID" ] && kill -TERM "$APP_PID" 2>/dev/null || true
WD_PID=$(pgrep -f "$BASE/watchdog.py" 2>/dev/null | head -1 || true)
[ -n "$WD_PID" ] && kill -TERM "$WD_PID" 2>/dev/null || true
CF_PID=$(pgrep -f "cloudflared tunnel --url http://localhost:8765" 2>/dev/null | head -1 || true)
[ -n "$CF_PID" ] && kill -TERM "$CF_PID" 2>/dev/null || true
sleep 1

# Start watchdog (detached, will spawn Flask + cloudflared)
nohup setsid python3 "$BASE/watchdog.py" > /dev/null 2>&1 < /dev/null &
disown

# Wait for URL to appear (up to 30s)
for i in $(seq 1 30); do
  if [ -s "$BASE/public_url.txt" ] && curl -fsS --max-time 2 http://127.0.0.1:8765/health >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

echo "=== URL ==="
cat "$BASE/public_url.txt" 2>/dev/null || echo "(no URL yet)"
echo
echo "=== HEALTH ==="
curl -s http://127.0.0.1:8765/health 2>/dev/null || echo "(server not responding)"
echo
echo "=== AUTH TOKEN ==="
cat "$BASE/auth_token.txt" 2>/dev/null || echo "(no token)"
echo
