#!/usr/bin/env bash
# Failover: make the STANDBY the live server. NOT automatic — a human (the operator or a deputy) runs it after the P1 alarm,
# from the phone over SSH if needed. Reason: an automatic switch on a monitor's opinion risks split-brain (both servers
# taking tickets/accounts) and silently loses everything since the last dump. Recovery target is 24 h; this takes
# ~5 minutes plus the time until someone reacts (P1 reaction target 8 h).
#
#   sudo /srv/firmen/app/ops/failover.sh status
#   sudo /srv/firmen/app/ops/failover.sh promote --yes
#
# promote:
#   1. try one last pull + restore of the newest dump (if the primary still answers over SSH; else use last night's)
#   2. mark var/ops/PROMOTED (the nightly standby restore then refuses to overwrite the now-live DB)
#   3. stop the standby timers, start the app + the primary timers (backup, restore test, refresh, prune, health)
#   4. move the traffic: FAILOVER_METHOD=floating_ip -> Hetzner Cloud API assigns the Floating IP to this server
#      (seconds, no DNS TTL); FAILOVER_METHOD=dns -> prints the record to change (TTL 300 => ≤ 5 min + resolver caches)
#   5. enable the app site in Caddy (Let's Encrypt issues the certificate as soon as traffic arrives here)
#   6. alert "failover done"
# Data loss: everything written on the old primary after its last dump (tickets, accounts, saved searches) — the
# directory data itself is rebuilt from registers anyway. Failback: the same steps in the other direction, after a fresh dump.
#
# Env (/srv/firmen/app/.env): FAILOVER_METHOD (floating_ip|dns), HCLOUD_TOKEN (Hetzner Cloud API token, project
# scoped, read+write), FLOATING_IP_ID, THIS_SERVER_ID, DOMAIN.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=ops/lib.sh
. "$ROOT/ops/lib.sh"
load_env
PY=$ROOT/.venv/bin/python
as_firmen() { if [ "$(id -un)" = firmen ]; then "$@"; else runuser -u firmen -- "$@"; fi; }

status() {
  echo "promoted: $([ -e "$ROOT/var/ops/PROMOTED" ] && cat "$ROOT/var/ops/PROMOTED" || echo no)"
  systemctl --no-pager list-timers 'firmen-*' || true
  systemctl is-active firmen-app caddy postgresql || true
  tail -n 2 "${BACKUP_DIR:-/srv/firmen/backup}/standby_restore.jsonl" 2>/dev/null || true
}

promote() {
  [ "${1:-}" = --yes ] || { echo "this switches live traffic to THIS server. Run: $0 promote --yes"; exit 2; }
  [ "$(id -u)" = 0 ] || { echo "run as root (sudo)"; exit 2; }
  echo "--- 1. last pull + restore (ok if the primary is gone)"
  as_firmen timeout 600 "$ROOT/ops/backup.sh" pull || echo "pull failed: using the dump restored last night"
  as_firmen "$ROOT/ops/restore_test.sh" standby || echo "restore failed: keeping last night's database"
  echo "--- 2. mark promoted"
  mkdir -p "$ROOT/var/ops"; echo "$(now_iso) by $(logname 2>/dev/null || echo root)" > "$ROOT/var/ops/PROMOTED"
  chown firmen: "$ROOT/var/ops/PROMOTED"
  echo "--- 3. timers"
  systemctl disable --now firmen-standby-restore.timer firmen-backup-pull.timer || true
  systemctl enable --now firmen-app.service firmen-app-health.timer firmen-backup.timer firmen-restore-test.timer \
    firmen-refresh-monthly.timer firmen-refresh-quarterly.timer firmen-prune.timer
  echo "--- 4. traffic"
  case "${FAILOVER_METHOD:-dns}" in
    floating_ip)
      : "${HCLOUD_TOKEN:?}" "${FLOATING_IP_ID:?}" "${THIS_SERVER_ID:?}"
      curl -fsS -X POST -H "Authorization: Bearer $HCLOUD_TOKEN" -H "Content-Type: application/json" \
        -d "{\"server\": $THIS_SERVER_ID}" "https://api.hetzner.cloud/v1/floating_ips/$FLOATING_IP_ID/actions/assign" \
        > /dev/null && echo "floating IP $FLOATING_IP_ID -> server $THIS_SERVER_ID" ;;
    *)
      echo "MANUAL STEP: set the A/AAAA record of ${DOMAIN:-<domain>} to this server:"
      ip -br addr show scope global | awk '{print "   ", $1, $3, $4}' ;;
  esac
  echo "--- 5. caddy"
  ln -sf /etc/caddy/sites-available/app.caddy /etc/caddy/sites-enabled/app.caddy
  caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy
  echo "--- 6. check"
  sleep 3; curl -fsS -m 10 http://127.0.0.1:8601/api/health && echo
  as_firmen "$PY" -c "from ops.alert import send_alert; send_alert('Failover ausgeführt', 'Standby ist jetzt live. Altes Primärsystem NICHT wieder starten, bevor Failback geplant ist.', level='P2', key='failover')" || true
  echo "done. The external monitor now watches this server through ${DOMAIN:-the domain}; the old primary must stay down."
}

case "${1:-}" in
  status) status ;;
  promote) shift; promote "${1:-}" ;;
  *) sed -n '2,25p' "$0"; exit 2 ;;
esac
