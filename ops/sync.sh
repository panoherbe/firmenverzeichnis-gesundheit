#!/usr/bin/env bash
# Mac -> datanuc code sync. Code only: raw/, var/, .env, .venv, .wheels on datanuc are never touched.
set -euo pipefail
cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude .git --exclude .env --exclude .venv --exclude .wheels --exclude raw --exclude var \
  --exclude __pycache__ --exclude .DS_Store \
  ./ datanuc:projects/private/firmenverzeichnis-gesundheit/
