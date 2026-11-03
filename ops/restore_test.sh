#!/usr/bin/env bash
# Prove that the backups can be restored (a backup that was never restored is an assumption, not a backup).
#
#   ops/restore_test.sh scratch [dump]   PRIMARY, weekly Sun 05:00 Europe/Berlin (firmen-restore-test.timer):
#                                        restore the newest dump into a scratch DB `firmen_restoretest`, check row
#                                        counts against the dump's .counts.json, drop it again
#   ops/restore_test.sh standby [dump]   STANDBY, nightly 04:15 (firmen-standby-restore.timer): restore the newest
#                                        dump into `firmen_new`, check, then swap it in as `firmen` so the standby
#                                        can take over with yesterday's data (refuses after promotion)
#
# Checks: sha256 matches; pg_restore exits 0; company, source_record and source counts equal the counts recorded at
# dump time; company >= RESTORE_MIN_COMPANIES (default 3000 = tender minimum of profiles).
# Result line (read by ops/sla_report.py): $BACKUP_DIR/restore_test.jsonl (scratch) or standby_restore.jsonl.
# Needs: DATABASE_URL whose role may CREATE DATABASE (bootstrap grants CREATEDB to role firmen), PG_BIN optional.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
# shellcheck source=ops/lib.sh
. "$ROOT/ops/lib.sh"
load_env
: "${DATABASE_URL:?DATABASE_URL not set}"
BACKUP_DIR=${BACKUP_DIR:-$ROOT/var/backup}
MIN=${RESTORE_MIN_COMPANIES:-3000}
PGB=${PG_BIN:+$PG_BIN/}
export PGOPTIONS="${PGOPTIONS:--c client_min_messages=warning}"
MODE=${1:-}
DUMP=${2:-$(ls -1t "$BACKUP_DIR"/firmen-*.dump 2>/dev/null | head -1 || true)}
ADMIN=$(db_url_for postgres)
LIVE=${DATABASE_URL##*/}

case "$MODE" in
  scratch) TARGET=${LIVE}_restoretest; RESULT=$BACKUP_DIR/restore_test.jsonl ;;
  standby) TARGET=${LIVE}_new; RESULT=$BACKUP_DIR/standby_restore.jsonl
           if [ -e "$ROOT/var/ops/PROMOTED" ]; then
             echo "this standby was promoted ($ROOT/var/ops/PROMOTED): refusing to overwrite the live DB" >&2; exit 3
           fi ;;
  *) sed -n '2,14p' "$0"; exit 2 ;;
esac

fail() {
  log_json "$RESULT" ok=false mode="$MODE" dump="$(basename "${DUMP:-none}")" error="$1"
  echo "restore $MODE FAILED: $1" >&2
  [ "$MODE" = scratch ] && "${PGB}psql" -d "$ADMIN" -Xq -c "DROP DATABASE IF EXISTS \"$TARGET\"" >/dev/null 2>&1 || true
  exit 1
}
q() { "${PGB}psql" -d "$1" -XAtq -v ON_ERROR_STOP=1 -c "$2"; }

[ -n "$DUMP" ] && [ -f "$DUMP" ] || fail "no dump found in $BACKUP_DIR"
SIDE=${DUMP%.dump}
if [ -f "$SIDE.sha256" ]; then
  [ "$(sha256 "$DUMP")" = "$(cut -d' ' -f1 "$SIDE.sha256")" ] || fail "checksum mismatch"
elif [ "$MODE" = scratch ] || [ -z "${ALLOW_NO_CHECKSUM:-}" ]; then
  fail "no checksum file (seed dump? set ALLOW_NO_CHECKSUM=1 with mode standby)"
fi

t0=$(date +%s)
q "$ADMIN" "DROP DATABASE IF EXISTS \"$TARGET\"" >/dev/null || fail "drop $TARGET"
q "$ADMIN" "CREATE DATABASE \"$TARGET\"" >/dev/null || fail "create $TARGET"
TURL=$(db_url_for "$TARGET")
"${PGB}pg_restore" --no-owner --no-acl --exit-on-error -j "${RESTORE_JOBS:-2}" -d "$TURL" "$DUMP" || fail "pg_restore exit $?"

got=$(q "$TURL" "SELECT (SELECT count(*) FROM company)||' '||(SELECT count(*) FROM source_record)||' '||(SELECT count(*) FROM source)") \
  || fail "count query"
read -r c_company c_records c_source <<< "$got"
if [ -f "$SIDE.counts.json" ]; then
  counts_checked=true
  want_company=$(sed -E 's/.*"company" ?: ?([0-9]+).*/\1/' "$SIDE.counts.json")
  want_records=$(sed -E 's/.*"source_record" ?: ?([0-9]+).*/\1/' "$SIDE.counts.json")
  want_source=$(sed -E 's/.*"source" ?: ?([0-9]+).*/\1/' "$SIDE.counts.json")
else  # e.g. a seed dump made with plain pg_dump: only the minimum check applies
  counts_checked=false; want_company=$c_company; want_records=$c_records; want_source=$c_source
fi
secs=$(( $(date +%s) - t0 ))

ok=true; why=""
[ "$c_company" = "$want_company" ] || { ok=false; why+="company $c_company != $want_company; "; }
[ "$c_records" = "$want_records" ] || { ok=false; why+="source_record $c_records != $want_records; "; }
[ "$c_source" = "$want_source" ] || { ok=false; why+="source $c_source != $want_source; "; }
[ "$c_company" -ge "$MIN" ] 2>/dev/null || { ok=false; why+="company $c_company < $MIN; "; }
[ $ok = true ] || fail "${why%; }"

if [ "$MODE" = scratch ]; then
  q "$ADMIN" "DROP DATABASE IF EXISTS \"$TARGET\"" >/dev/null || true
else
  # swap: firmen -> firmen_old, firmen_new -> firmen, drop firmen_old (nothing may be connected; app is stopped here)
  q "$ADMIN" "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$LIVE', '$TARGET') AND pid <> pg_backend_pid()" >/dev/null || true
  q "$ADMIN" "DROP DATABASE IF EXISTS \"${LIVE}_old\"" >/dev/null
  if [ "$(q "$ADMIN" "SELECT count(*) FROM pg_database WHERE datname = '$LIVE'")" = 1 ]; then
    q "$ADMIN" "ALTER DATABASE \"$LIVE\" RENAME TO \"${LIVE}_old\"" >/dev/null || fail "rename live"
  fi
  q "$ADMIN" "ALTER DATABASE \"$TARGET\" RENAME TO \"$LIVE\"" >/dev/null || fail "rename new"
  q "$ADMIN" "DROP DATABASE IF EXISTS \"${LIVE}_old\"" >/dev/null || true
fi
log_json "$RESULT" ok=true mode="$MODE" dump="$(basename "$DUMP")" company="$c_company" source_record="$c_records" \
         source="$c_source" counts_checked="$counts_checked" seconds="$secs"
echo "restore $MODE ok: $(basename "$DUMP") company=$c_company source_record=$c_records source=$c_source (${secs}s)"
