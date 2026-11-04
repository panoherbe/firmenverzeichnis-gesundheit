#!/usr/bin/env bash
# Scheduled data refresh on the PRIMARY (monthly data update, quarterly cleanup).
#
#   ops/refresh.sh monthly     2nd of each month 00:30 Europe/Berlin (firmen-refresh-monthly.timer):
#                              pre-refresh dump -> fetch EUDAMED, EMA, GLEIF -> etl.load schema + all -> etl.resolve
#                              -> etl.enrich (if present)
#                              -> plausibility check (verified companies must not drop by more than REFRESH_MAX_DROP_PCT)
#   ops/refresh.sh quarterly   10th of Jan/Apr/Jul/Oct 03:00 (firmen-refresh-quarterly.timer): python -m etl.cleanup
#
# A failed fetch keeps the previous raw snapshot (etl.load always reads the newest raw per source), so a refresh with
# one broken source still rebuilds consistently from older data for that source — the log line says which one failed.
# EUDAMED is slow and rate-limited (~1 day for the full detail fetch), so the unit has no timeout.
# The procurement awards (source `corpus`) come from a separate database; their raw snapshot is copied by hand.
# Revert: the pre-refresh dump `firmen-<ts>-pre-refresh.dump` -> ops/restore_test.sh standby <dump> with the app
# stopped. Result line: $BACKUP_DIR/refresh.jsonl (travels to the standby with the backups).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=ops/lib.sh
. "$ROOT/ops/lib.sh"
load_env
cd "$ROOT"
PY=${PYTHON:-$ROOT/.venv/bin/python}
BACKUP_DIR=${BACKUP_DIR:-$ROOT/var/backup}
LOG=$BACKUP_DIR/refresh.jsonl
MAXDROP=${REFRESH_MAX_DROP_PCT:-10}
mkdir -p "$BACKUP_DIR" "$ROOT/var/log"

verified() { "$PY" - <<'PY'
import psycopg, os
from etl.common.env import load_env
load_env()
with psycopg.connect(os.environ["DATABASE_URL"]) as c:
    print(c.execute("SELECT count(*) FILTER (WHERE verified) FROM company").fetchone()[0])
PY
}

monthly() {
  local t0 before after failed="" s
  t0=$(date +%s)
  before=$(verified)
  "$ROOT/ops/backup.sh" dump pre-refresh
  for s in ${REFRESH_SOURCES:-eudamed ema gleif}; do
    echo "--- fetch $s"
    if ! "$PY" -m "etl.sources.$s" fetch >> "$ROOT/var/log/refresh-$s.log" 2>&1; then failed+="$s "; fi
  done
  "$PY" -m etl.load schema
  "$PY" -m etl.load all
  "$PY" -m etl.resolve
  if [ -f "$ROOT/etl/enrich.py" ]; then "$PY" -m etl.enrich; fi   # sizes, products, EMDN (idempotent, after resolve)
  after=$(verified)
  local ok=true
  if [ "$before" -gt 0 ] && [ $(( (before - after) * 100 / before )) -gt "$MAXDROP" ]; then
    ok=false
    "$PY" -c "import sys; from ops.alert import send_alert; send_alert('Datenaktualisierung auffällig', sys.argv[1], level='P3')" \
      "Verifizierte Firmen $before -> $after (mehr als $MAXDROP % weniger). Prüfen; Revert: pre-refresh-Dump zurückspielen" || true
  fi
  log_json "$LOG" ok=$ok kind=monthly verified_before="$before" verified_after="$after" \
           failed_fetch="${failed% }" seconds="$(( $(date +%s) - t0 ))"
  echo "refresh monthly: verified $before -> $after; failed fetch: ${failed:-none}"
  [ -z "$failed" ] && [ $ok = true ]
}

quarterly() {
  local t0 rc=0; t0=$(date +%s)
  if "$PY" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('etl.cleanup') else 1)"; then
    "$PY" -m etl.cleanup || rc=$?
  else
    echo "etl.cleanup not present yet"; rc=4
  fi
  log_json "$LOG" ok="$([ $rc = 0 ] && echo true || echo false)" kind=quarterly rc="$rc" seconds="$(( $(date +%s) - t0 ))"
  return $rc
}

case "${1:-}" in
  monthly) monthly ;;
  quarterly) quarterly ;;
  *) sed -n '2,15p' "$0"; exit 2 ;;
esac
