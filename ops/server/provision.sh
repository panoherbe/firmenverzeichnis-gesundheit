#!/usr/bin/env bash
# From the operator's workstation: one command per server after buying it (Hetzner Cloud, Ubuntu 24.04, your SSH key added at creation).
#
#   ops/server/provision.sh primary root@<primary-ip> [--seed path/to/firmen.dump]
#   ops/server/provision.sh standby root@<standby-ip>
#
# Copies the code (excluding .git, .env*, .venv, .wheels, raw, var) to /srv/firmen/app, the filled `.env.<role>` from the repo root to
# /srv/firmen/app/.env, runs ops/server/bootstrap.sh <role>, and with --seed restores an initial database dump
# (make it on the ETL host: `docker exec firmen-db pg_dump -U firmen -Fc firmen > seed.dump`, copy it to the workstation).
# Also copy the raw snapshots the monthly refresh does not fetch (corpus, cordis) into raw/ on the server.
# ONE ssh/rsync call at a time (some hosts block IPs on bursts of SSH logins); nothing runs in parallel.
set -euo pipefail
cd "$(dirname "$0")/../.."
ROLE=${1:?primary|standby}; HOST=${2:?root@ip}; SEED=""
[ "${3:-}" = --seed ] && SEED=${4:?dump file}
ENVF=.env.$ROLE
[ -f "$ENVF" ] || { echo "$ENVF missing: cp ops/server/server.env.example $ENVF and fill it in"; exit 2; }
grep -q CHANGE_ME "$ENVF" && { echo "$ENVF still contains CHANGE_ME"; exit 2; }
grep -q "^ROLE=$ROLE$" "$ENVF" || { echo "$ENVF: ROLE is not $ROLE"; exit 2; }

echo "--- code -> $HOST:/srv/firmen/app"
rsync -az --delete --rsync-path="mkdir -p /srv/firmen/app && rsync" \
  --exclude .git --exclude '.env*' --exclude .venv --exclude .wheels --exclude raw --exclude var \
  --exclude __pycache__ --exclude .DS_Store ./ "$HOST:/srv/firmen/app/"
echo "--- env"
rsync -a --chmod=F600 "$ENVF" "$HOST:/srv/firmen/app/.env"
if [ -n "$SEED" ]; then
  echo "--- seed dump"
  rsync -a --partial "$SEED" "$HOST:/srv/firmen/seed.dump"
fi
echo "--- bootstrap (several minutes)"
ssh "$HOST" "bash /srv/firmen/app/ops/server/bootstrap.sh $ROLE$( [ -n "$SEED" ] && echo " && install -o firmen -g firmen -m 600 /srv/firmen/seed.dump /srv/firmen/backup/firmen-seed.dump && runuser -u firmen -- env ALLOW_NO_CHECKSUM=1 /srv/firmen/app/ops/restore_test.sh standby /srv/firmen/backup/firmen-seed.dump && systemctl restart firmen-app" )"
