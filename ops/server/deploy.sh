#!/usr/bin/env bash
# From the operator's workstation: ship a code change to a server and restart the app (no bootstrap). Units/timers are re-copied.
#   ops/server/deploy.sh root@<ip>
# Restart takes ~2 s; the monitor tolerates it (down alert only after 2 failed minutes).
set -euo pipefail
cd "$(dirname "$0")/../.."
HOST=${1:?root@ip}
python3 -m py_compile web/*.py ops/*.py etl/*.py
rsync -az --delete --exclude .git --exclude '.env*' --exclude .venv --exclude .wheels --exclude raw --exclude var \
  --exclude __pycache__ --exclude .DS_Store ./ "$HOST:/srv/firmen/app/"
ssh "$HOST" 'set -e; chown -R firmen:firmen /srv/firmen/app; cd /srv/firmen/app;
  runuser -u firmen -- .venv/bin/pip install -q -r requirements.txt python-multipart;
  runuser -u firmen -- .venv/bin/python -m ops.schema;
  cp ops/systemd/firmen-*.service ops/systemd/firmen-*.timer /etc/systemd/system/; systemctl daemon-reload;
  if systemctl is-enabled --quiet firmen-app; then systemctl restart firmen-app; sleep 3; curl -fsS http://127.0.0.1:8601/api/health; echo; fi'
