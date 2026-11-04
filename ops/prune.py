"""
Daily retention job on the PRIMARY (firmen-prune.timer, 04:45 Europe/Berlin):

- access_log rows older than ACCESS_LOG_DAYS (default 90; access logging, GDPR storage limitation)
- requester e-mails of support tickets resolved more than SUPPORT_RETENTION_DAYS (default 90) ago
- var/ops/alerts.jsonl trimmed to the last 400 days

Caddy's own access log (IPs masked to /24 and /48) rotates itself: roll_keep_for 2160h in the Caddyfile.

    python -m ops.prune
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import psycopg

from etl.common.env import load_env
from ops.alert import ops_dir


def main() -> None:
    load_env()
    days = int(os.environ.get("ACCESS_LOG_DAYS", "90"))
    sdays = int(os.environ.get("SUPPORT_RETENTION_DAYS", "90"))
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        n_log = c.execute("DELETE FROM access_log WHERE at < now() - make_interval(days => %s)", (days,)).rowcount \
            if c.execute("SELECT to_regclass('access_log')").fetchone()[0] else 0
        n_mail = c.execute("UPDATE support_ticket SET contact_email = NULL, email_purged_at = now() "
                           "WHERE contact_email IS NOT NULL AND resolved_at < now() - make_interval(days => %s)",
                           (sdays,)).rowcount \
            if c.execute("SELECT to_regclass('support_ticket')").fetchone()[0] else 0
    f = ops_dir() / "alerts.jsonl"
    kept = 0
    if f.exists():
        cut = datetime.now(timezone.utc) - timedelta(days=400)
        lines = [ln for ln in f.read_text().splitlines() if ln.strip()]
        keep = []
        for ln in lines:
            try:
                if datetime.fromisoformat(json.loads(ln)["at"]) >= cut:
                    keep.append(ln)
            except (ValueError, KeyError):
                keep.append(ln)
        f.write_text("\n".join(keep) + ("\n" if keep else ""))
        kept = len(keep)
    print(f"prune: access_log -{n_log} (>{days} d), requester e-mails -{n_mail} (>{sdays} d after resolve), "
          f"alerts kept {kept}")


if __name__ == "__main__":
    main()
