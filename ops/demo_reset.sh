#!/usr/bin/env bash
# Demo installations only: every night, restore the newest seed dump (firmen-seed*.dump in BACKUP_DIR) and the demo
# content, so nothing a tester changes through /test lasts. Run as root by firmen-demo-reset.timer (04:10).
set -euo pipefail
APP=/srv/firmen/app
. "$APP/ops/lib.sh" 2>/dev/null || true
BACKUP_DIR=${BACKUP_DIR:-/srv/firmen/backup}
SEED=$(ls -1t "$BACKUP_DIR"/firmen-seed*.dump 2>/dev/null | head -1)
[ -n "$SEED" ] || { echo "no seed dump in $BACKUP_DIR"; exit 3; }
cd "$APP"
# personal access links of the closed demo (web/gate.py) are not in the seed: keep them across the reset
TOKENS=$(mktemp); chmod 644 "$TOKENS"
runuser -u postgres -- pg_dump -Fc --data-only -t access_token firmen > "$TOKENS" 2>/dev/null || true
runuser -u firmen -- env ALLOW_NO_CHECKSUM=1 ops/restore_test.sh standby "$SEED"
runuser -u firmen -- .venv/bin/python -m ops.schema > /dev/null
[ -s "$TOKENS" ] && runuser -u postgres -- pg_restore --data-only -d firmen < "$TOKENS"
rm -f "$TOKENS"
systemctl restart firmen-app
sleep 3
# the seed drives the app in-process; the access gate would send it to the start page
runuser -u firmen -- env ACCESS_GATE=0 DEMO_PASSWORD="$(openssl rand -hex 12)" ADMIN_EMAIL=demo-admin@example.org \
  .venv/bin/python -m ops.demo_seed > /dev/null
echo "demo reset ok: $(basename "$SEED")"
