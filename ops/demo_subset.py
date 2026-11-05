"""
Small demo database for a closed buyer preview (2026-11-05): the public demo host must not hold the full data set.
Picks N companies that are filled in best and together cover every filter value (sector, federal state, role,
product group, size class) and the example searches of the welcome page; K of them keep contact and register data,
the others are marked `preview_locked` and lose those fields (they show "im Vertrag"). Everything else that could
rebuild the data set is removed: other companies, their source records, the GLEIF table, logs, website checks,
samples; the international base shrinks to a small sample.

    DATABASE_URL=<…/firmen_demo> .venv/bin/python -m ops.demo_subset [N] [K]   # run ONLY on a copy of the database

The run refuses a database named "firmen" so it can never strip the full data set.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter

from etl.common.db import connect

EXAMPLES = [  # welcome-page searches that must find something: (label, SQL condition)
    ("Endoskop BW", "region = 'DE-BW' AND (name ILIKE '%endoskop%' OR emdn::text ILIKE '%endoscop%')"),
    ("Implantat/Prothese", "'medtech' = ANY(sectors) AND (name ILIKE '%implant%' OR name ILIKE '%prothes%' OR "
                           "emdn::text ILIKE '%implant%' OR emdn::text ILIKE '%prosthe%') AND name NOT ILIKE '%dental%'"),
    ("IVD mit Kontakt", "'W' = ANY(emdn_groups) AND (email_role IS NOT NULL OR contact_url IS NOT NULL)"),
    ("Pharma mit Produkten", "products IS NOT NULL"),
]
SCORE = """(website IS NOT NULL)::int + (contact_url IS NOT NULL)::int + (email_role IS NOT NULL)::int
  + (vat IS NOT NULL)::int + (register_no IS NOT NULL)::int + (lei IS NOT NULL)::int
  + (coalesce(cardinality(srns), 0) > 0)::int + (emdn_groups IS NOT NULL)::int + (products IS NOT NULL)::int
  + 2 * (size_class IS NOT NULL)::int + (street IS NOT NULL)::int + (coalesce(device_count, 0) > 0)::int"""


def main() -> None:
    dsn = os.environ["DATABASE_URL"]
    if dsn.rstrip("/").rsplit("/", 1)[-1].split("?")[0] == "firmen":
        sys.exit("refusing to strip the database 'firmen' – run on a copy (e.g. firmen_demo)")
    n_total = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    n_full = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    conn = connect()
    base = "verified AND hidden_reason IS NULL"
    cand = conn.execute(f"""SELECT id, name, sectors, region, tier, emdn_groups, size_class, products IS NOT NULL AS pharma,
        {SCORE} AS score FROM company WHERE {base} ORDER BY score DESC, public_awards DESC, id""").fetchall()
    by_id = {r[0]: r for r in cand}
    chosen: list[str] = []

    def add(cid: str) -> None:
        if cid not in chosen and len(chosen) < n_total:
            chosen.append(cid)

    for _label, cond in EXAMPLES:
        for (cid,) in conn.execute(f"SELECT id FROM company WHERE {base} AND {cond} "
                                   f"ORDER BY {SCORE} DESC, id LIMIT 3").fetchall():
            add(cid)
    # every filter value at least once (best-filled company that has it)
    keys = [("sector", lambda r: r[2] or []), ("region", lambda r: [r[3]]), ("tier", lambda r: [r[4]]),
            ("product", lambda r: r[5] or []), ("size", lambda r: [r[6]])]
    for _name, get in keys:
        values = {v for r in cand for v in get(r) if v}
        for v in sorted(values):
            if not any(v in get(by_id[c]) for c in chosen):
                hit = next((r[0] for r in cand if v in get(r)), None)
                if hit:
                    add(hit)
    # fill by completeness, at most 12 per federal state so the map of the selection stays spread
    per_region = Counter(by_id[c][3] for c in chosen)
    for r in cand:
        if len(chosen) >= n_total:
            break
        if r[0] not in chosen and per_region[r[3]] < 12:
            add(r[0])
            per_region[r[3]] += 1
    # full profiles: best-filled, at least one per sector present
    full: list[str] = []
    for s in ("medtech", "pharma", "digital_health", "biotech"):
        hit = next((c for c in sorted(chosen, key=lambda c: -by_id[c][8]) if s in (by_id[c][2] or [])), None)
        if hit and hit not in full:
            full.append(hit)
    for c in sorted(chosen, key=lambda c: -by_id[c][8]):
        if len(full) >= n_full:
            break
        if c not in full:
            full.append(c)

    keep, locked = chosen, [c for c in chosen if c not in full]
    conn.execute("ALTER TABLE company ADD COLUMN IF NOT EXISTS preview_locked boolean")
    conn.execute("DELETE FROM company WHERE id <> ALL(%s)", (keep,))
    conn.execute("DELETE FROM company_member WHERE company_id <> ALL(%s)", (keep,))
    conn.execute("""DELETE FROM source_record s WHERE NOT EXISTS
        (SELECT 1 FROM company_member m WHERE m.source = s.source AND m.source_id = s.source_id)""")
    conn.execute("""UPDATE company SET preview_locked = true, email_role = NULL, contact_url = NULL, vat = NULL,
        register_no = NULL, lei = NULL, srns = '{}' WHERE id = ANY(%s)""", (locked,))
    conn.execute("""UPDATE source_record s SET email_role = NULL, vat = NULL, register_no = NULL, register_ra = NULL,
        lei = NULL, srn = NULL FROM company_member m
        WHERE m.source = s.source AND m.source_id = s.source_id AND m.company_id = ANY(%s)""", (locked,))
    conn.execute("UPDATE source_record SET facts = '{}'::jsonb, raw_path = '', raw_sha256 = ''")
    # international base: a small sample, at most 3 companies per country
    conn.execute("""DELETE FROM intl_company WHERE id NOT IN (SELECT id FROM (SELECT id, row_number() OVER
        (PARTITION BY country ORDER BY website IS NULL, name) AS k FROM intl_company) x WHERE k <= 3)""")
    conn.execute("UPDATE intl_company SET facts = '{}'::jsonb, raw_path = '', raw_sha256 = ''")
    for t in ("gleif_de", "site_check", "cleanup_log", "quality_check", "access_log", "search_history",
              "suppression", "support_event", "support_ticket", "access_token", "access_usage"):
        if conn.execute("SELECT to_regclass(%s)", (t,)).fetchone()[0]:
            conn.execute(f"TRUNCATE {t} CASCADE")
    conn.commit()
    c = conn.execute("SELECT count(*), count(*) FILTER (WHERE NOT coalesce(preview_locked, false)) FROM company").fetchone()
    print(json.dumps({"companies": c[0], "full": c[1], "intl": conn.execute("SELECT count(*) FROM intl_company").fetchone()[0],
                      "source_records": conn.execute("SELECT count(*) FROM source_record").fetchone()[0],
                      "full_ids": full}))


if __name__ == "__main__":
    main()
