#!/usr/bin/env bash
# Records the walkthrough on the ETL host against the running app (127.0.0.1:8601) and encodes it.
#   bash ops/rundgang/run.sh            → var/rundgang/{rundgang.mp4,rundgang.gif,poster.png,bilder/*.png,kapitel.json}
# The demo admin gets a temporary random password for the organiser view and a new random one afterwards;
# neither is printed. Needs the image rundgang-aufnahme:1.56.0 (Dockerfile.aufnahme) and ffmpeg on the host.
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=.venv/bin/python
OUT="$PWD/var/rundgang"; rm -rf "$OUT/aufnahme"; mkdir -p "$OUT/aufnahme"
read -r EVENT TOKEN < <($PY - <<'EOF'
from web.common import db
with db() as c:
    e = c.execute("SELECT id, slug FROM mm_event WHERE slug LIKE 'beispiel-%' ORDER BY id DESC LIMIT 1").fetchone()
    t = c.execute("SELECT token FROM mm_participant WHERE event_id = %s AND side = 'de' ORDER BY id LIMIT 1", (e["id"],)).fetchone()
print(e["slug"], t["token"])
EOF
)
read -r INTL_N INTL_C < <(docker exec firmen-db psql -U firmen firmen -AtF ' ' -c "select count(*), count(distinct country) from intl_company")
ADMIN_PW=$(openssl rand -hex 12)
ADMIN_TOTP=$($PY - "$ADMIN_PW" <<'EOF'
import sys
from web import auth
from web.common import db
with db() as c:
    c.execute("UPDATE app_user SET pw_hash = %s WHERE email = 'demo-admin@example.org'", (auth.hash_password(sys.argv[1]),))
    print(c.execute("SELECT totp_secret FROM app_user WHERE email = 'demo-admin@example.org'").fetchone()["totp_secret"])
EOF
)
reset_admin() { $PY -c "
import secrets
from web import auth
from web.common import db
with db() as c: c.execute(\"UPDATE app_user SET pw_hash = %s WHERE email = 'demo-admin@example.org'\", (auth.hash_password(secrets.token_urlsafe(18)),))"; }
trap reset_admin EXIT
docker run --rm --network host -u "$(id -u):$(id -g)" -e HOME=/tmp -e AUS=/aus -e APP_URL=http://127.0.0.1:8601 \
  -e DEMO_PW="${DEMO_PW:?set DEMO_PW to the demo account password}" -e ADMIN_EMAIL=demo-admin@example.org -e ADMIN_PW="$ADMIN_PW" \
  -e ADMIN_TOTP="$ADMIN_TOTP" -e MM_EVENT="$EVENT" -e MM_TOKEN="$TOKEN" -e INTL_N="$INTL_N" -e INTL_COUNTRIES="$INTL_C" \
  -v "$PWD/ops/rundgang:/rundgang:ro" -v "$OUT/aufnahme:/aus" rundgang-aufnahme:1.56.0 python /rundgang/fv.py
bash ops/rundgang/kodieren.sh "$OUT/aufnahme" "$OUT"
rm -rf "$OUT/bilder"; cp -r "$OUT/aufnahme/bilder" "$OUT/bilder"; cp "$OUT/aufnahme/kapitel.json" "$OUT/"
ls -la "$OUT" "$OUT/bilder"
