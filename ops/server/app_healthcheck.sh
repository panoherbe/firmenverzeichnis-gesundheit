#!/usr/bin/env bash
# PRIMARY, every minute as root (firmen-app-health.timer). systemd's Restart=always only catches a crashed process;
# this catches a hung one: 3 consecutive failed local /api/health calls -> restart postgresql (if down) and the app.
# The external monitor on the standby decides about alerts; this script only heals and logs to the journal.
set -uo pipefail
STATE=/run/firmen-health.fails
URL=${HEALTH_URL:-http://127.0.0.1:8601/api/health}
fails=$(cat "$STATE" 2>/dev/null || echo 0)
if curl -fsS -m 10 "$URL" | grep -q '"ok":true'; then
  [ "$fails" != 0 ] && echo "health ok again after $fails failures"
  echo 0 > "$STATE"; exit 0
fi
fails=$((fails + 1)); echo "$fails" > "$STATE"
echo "health check failed ($fails in a row)"
if [ "$fails" -ge "${HEALTH_FAILS_BEFORE_RESTART:-3}" ]; then
  systemctl is-active --quiet postgresql || { echo "postgresql not active -> restart"; systemctl restart postgresql; }
  echo "restarting firmen-app"; systemctl restart firmen-app
  echo 0 > "$STATE"
fi
