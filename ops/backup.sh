#!/usr/bin/env bash
# Daily database backup (tender LB §2.1: daily automatic backups, 30 days retention, geo-redundant copy).
#
#   ops/backup.sh dump [tag]   PRIMARY, 02:30 Europe/Berlin (firmen-backup.timer): pg_dump -> $BACKUP_DIR,
#                              sha256 + row-count sidecar, `pg_restore --list` sanity check, 30-day rotation
#   ops/backup.sh pull         STANDBY, 03:15 (firmen-backup-pull.timer): pull the dumps from the primary over SSH,
#                              verify every checksum, rotate, then mirror the whole directory to the off-site
#                              Storage Box (third location)
#
# Why rsync over SSH and not restic: the backups are plain pg_dump files plus a .sha256 next to each. Restoring needs
# nothing but pg_restore — no repository format, no repository password that can be lost, no extra binary to keep
# current. Dumps are already compressed (custom format), so restic's dedup would save little. The cost: no
# client-side encryption. Acceptable because all three locations are Hetzner (one ISO 27001 provider, DE/EU) and the
# database holds public company data plus few requester e-mails (deleted 90 days after ticket close).
# If encryption at rest off-site becomes a requirement, switch OFFSITE_DEST to `restic -r sftp:…` in pull().
#
# Why PULL (standby fetches from primary) instead of PUSH: the primary holds no credentials for any other location.
# A compromised primary cannot delete the copies on the standby or the Storage Box. The standby's key is restricted
# on the primary to read-only rsync of $BACKUP_DIR (authorized_keys: command="rrsync -ro …",restrict).
#
# Env (from the repo's .env, unless already set): DATABASE_URL, BACKUP_DIR (default <repo>/var/backup),
# BACKUP_RETENTION_DAYS (30), PG_BIN (dir of pg_dump, default: PATH),
# pull only: PRIMARY_BACKUP_SRC (user@primary:/srv/firmen/backup/ — or a local dir for tests),
#            OFFSITE_DEST (uXXXXX@uXXXXX.your-storagebox.de:firmen-backup/ — or a local dir; empty = skip),
#            OFFSITE_SSH_PORT (23 for Hetzner Storage Box), BACKUP_SSH_KEY (default ~/.ssh/id_ed25519)
# Result lines (read by ops/sla_report.py): $BACKUP_DIR/backup.jsonl (dump), $BACKUP_DIR/offsite.jsonl (pull).
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)

# shellcheck source=ops/lib.sh
. "$ROOT/ops/lib.sh"
load_env

BACKUP_DIR=${BACKUP_DIR:-$ROOT/var/backup}
RETENTION=${BACKUP_RETENTION_DAYS:-30}
PGB=${PG_BIN:+$PG_BIN/}
mkdir -p "$BACKUP_DIR"

rotate() {  # delete dumps (and their sidecars) older than RETENTION days; never the newest one
  local newest; newest=$(ls -1t "$BACKUP_DIR"/firmen-*.dump 2>/dev/null | head -1 || true)
  find "$BACKUP_DIR" -maxdepth 1 -name 'firmen-*.dump' -mtime +"$((RETENTION - 1))" -print 2>/dev/null |
  while read -r f; do
    [ "$f" = "$newest" ] && continue
    rm -f -- "$f" "${f%.dump}.sha256" "${f%.dump}.counts.json" "$f.verified"
    echo "rotated: $(basename "$f")"
  done
}

dump() {
  : "${DATABASE_URL:?DATABASE_URL not set}"
  local tag=${1:-} ts name f t0 counts rc=0
  ts=$(date -u +%Y%m%dT%H%M%SZ); name="firmen-$ts${tag:+-$tag}"; f="$BACKUP_DIR/$name.dump"
  t0=$(date +%s)
  # row counts in the same moment (not the same snapshot: access_log/search_history may differ by a few rows;
  # restore_test.sh compares only the stable tables company/source_record/source)
  counts=$("${PGB}psql" -d "$DATABASE_URL" -XAtq -v ON_ERROR_STOP=1 -c "SELECT json_build_object(
      'company', (SELECT count(*) FROM company), 'source_record', (SELECT count(*) FROM source_record),
      'source', (SELECT count(*) FROM source),
      'support_ticket', (SELECT CASE WHEN to_regclass('support_ticket') IS NULL THEN NULL ELSE (SELECT count(*) FROM support_ticket) END),
      'app_user', (SELECT CASE WHEN to_regclass('app_user') IS NULL THEN NULL ELSE (SELECT count(*) FROM app_user) END))") || rc=$?
  if [ $rc = 0 ]; then
    "${PGB}pg_dump" -d "$DATABASE_URL" -Fc -Z 6 --no-owner --no-acl -f "$f.part" || rc=$?
  fi
  if [ $rc = 0 ]; then
    "${PGB}pg_restore" --list "$f.part" > /dev/null || rc=$?   # the archive's table of contents is readable
  fi
  if [ $rc != 0 ]; then
    rm -f "$f.part"
    log_json "$BACKUP_DIR/backup.jsonl" ok=false file="$name.dump" error="exit $rc"
    echo "backup FAILED (exit $rc)" >&2; exit 1
  fi
  mv "$f.part" "$f"
  echo "$(sha256 "$f")  $name.dump" > "$BACKUP_DIR/$name.sha256"
  echo "$counts" > "$BACKUP_DIR/$name.counts.json"
  rotate
  local n; n=$(ls -1 "$BACKUP_DIR"/firmen-*.dump | wc -l | tr -d ' ')
  log_json "$BACKUP_DIR/backup.jsonl" ok=true file="$name.dump" bytes="$(filesize "$f")" seconds="$(( $(date +%s) - t0 ))" kept="$n"
  echo "backup ok: $f ($(filesize "$f") bytes, $n dumps kept)"
}

pull() {
  : "${PRIMARY_BACKUP_SRC:?PRIMARY_BACKUP_SRC not set (user@primary:/srv/firmen/backup/)}"
  local key=${BACKUP_SSH_KEY:-$HOME/.ssh/id_ed25519} t0 rc=0 bad=0 checked=0 offsite="skipped"
  local sshcmd="ssh -i $key -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new"
  t0=$(date +%s)
  # no --delete: the standby keeps its own 30 days even if the primary's directory is emptied.
  # Dumps are immutable (new name per day): --ignore-existing, so a later change on the primary (corruption,
  # tampering) never overwrites a copy that was already verified here. --partial-dir keeps half-transferred files
  # out of the final name. The result logs (*.jsonl) are append-only and are synced normally.
  rsync -a --ignore-existing --partial-dir=.rsync-partial --timeout=600 -e "$sshcmd" \
        --include='firmen-*' --exclude='*' "${PRIMARY_BACKUP_SRC%/}/" "$BACKUP_DIR/" || rc=$?
  rsync -a --timeout=120 -e "$sshcmd" --include='backup.jsonl' --include='restore_test.jsonl' \
        --include='refresh.jsonl' --exclude='*' "${PRIMARY_BACKUP_SRC%/}/" "$BACKUP_DIR/" || rc=$?
  for s in "$BACKUP_DIR"/firmen-*.sha256; do
    [ -e "$s" ] || continue
    local d="${s%.sha256}.dump"
    [ -e "$d.verified" ] && continue
    checked=$((checked + 1))
    if [ -e "$d" ] && [ "$(sha256 "$d")" = "$(cut -d' ' -f1 "$s")" ]; then touch "$d.verified"; else
      bad=$((bad + 1)); echo "CHECKSUM MISMATCH: $d (removed here; fetched again next run)" >&2
      rm -f -- "$d"; fi
  done
  rotate
  if [ -n "${OFFSITE_DEST:-}" ] && [ $bad != 0 ]; then
    offsite=held   # never mirror (with --delete) while a copy here failed its checksum
  elif [ -n "${OFFSITE_DEST:-}" ]; then
    # mirror incl. rotation (--delete); Storage Box snapshots (enable in Hetzner Robot) protect against deletion
    if rsync -a --delete --exclude=.rsync-partial --timeout=600 \
         -e "ssh -i $key -p ${OFFSITE_SSH_PORT:-23} -o BatchMode=yes -o ConnectTimeout=20 -o StrictHostKeyChecking=accept-new" \
         "$BACKUP_DIR/" "${OFFSITE_DEST%/}/"; then offsite=ok; else offsite=failed; fi
  fi
  local newest; newest=$(ls -1t "$BACKUP_DIR"/firmen-*.dump 2>/dev/null | head -1 || true)
  local ok=true; [ $rc = 0 ] && [ $bad = 0 ] && [ "$offsite" != failed ] && [ "$offsite" != held ] && [ -n "$newest" ] || ok=false
  log_json "$BACKUP_DIR/offsite.jsonl" ok=$ok pull_rc="$rc" checked="$checked" bad="$bad" offsite="$offsite" \
           newest="$(basename "${newest:-none}")" seconds="$(( $(date +%s) - t0 ))"
  echo "pull: rc=$rc checked=$checked bad=$bad offsite=$offsite newest=$(basename "${newest:-none}")"
  [ $ok = true ]
}

case "${1:-}" in
  dump) shift; dump "${1:-}" ;;
  pull) pull ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
