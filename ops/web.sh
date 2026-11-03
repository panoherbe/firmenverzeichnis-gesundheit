#!/usr/bin/env bash
# (Re)start the web app on datanuc, loopback only. Reach it from the Mac with:
#   ssh -N -L 8601:127.0.0.1:8601 datanuc   ->  http://127.0.0.1:8601
cd "$(dirname "$0")/.."
pkill -f "uvicorn web.app:app" 2>/dev/null
setsid nohup .venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8601 --proxy-headers < /dev/null > var/log/web.log 2>&1 &
sleep 2
curl -s http://127.0.0.1:8601/api/health
