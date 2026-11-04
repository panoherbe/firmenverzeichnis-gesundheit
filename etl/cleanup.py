"""
Quarterly cleanup by fixed, documented criteria (codes B1–B8 below). Runs after etl.resolve + etl.enrich and is
deterministic: etl.resolve rebuilds `company` from the registers, so this step re-derives every mark each run.

  B1  no reachable contact path in two consecutive check cycles (site_check: the last two distinct days both not ok,
      and no company mailbox)                                   → hidden, reason 'B1'
  B2/B4/B6  inactive in EUDAMED / gone from EMA / seat abroad   → already excluded by etl.load (active DE records only)
  B3  lapsed/retired LEI                                       → a LEI never verifies alone (tiers A/B need EUDAMED or
      EMA), so a lapsed LEI cannot keep a profile alive; GLEIF loads ACTIVE entities only
  B7  the company asked for removal                             → table `suppression` (by company id or lower-case name)
  B8  duplicates by register no., VAT or LEI                    → merged by etl.resolve (exact keys)

Hidden profiles get `hidden_reason` and stay out of search, API and exports; `cleanup_log` keeps every decision with its
date for the quarterly report. Nothing is deleted here.

    python -m etl.cleanup            # apply
    python -m etl.cleanup --report   # print the last run's counts
"""

from __future__ import annotations

import datetime as dt
import sys

from etl.common.db import connect

SCHEMA = """
ALTER TABLE company ADD COLUMN IF NOT EXISTS hidden_reason text;
CREATE TABLE IF NOT EXISTS suppression (company_id text, name_norm text, reason text NOT NULL, ticket text,
  at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS cleanup_log (run date NOT NULL, company_id text NOT NULL, name text, criterion text NOT NULL,
  PRIMARY KEY (run, company_id, criterion));
"""


def main() -> None:
    conn = connect()
    conn.execute(SCHEMA)
    if "--report" in sys.argv:
        for r in conn.execute("SELECT run, criterion, count(*) FROM cleanup_log WHERE run = (SELECT max(run) FROM cleanup_log) "
                              "GROUP BY 1, 2 ORDER BY 2").fetchall():
            print(*r)
        return
    run = dt.date.today()
    conn.execute("UPDATE company SET hidden_reason = NULL")
    b1 = conn.execute("""
        WITH last AS (SELECT DISTINCT checked FROM site_check ORDER BY checked DESC LIMIT 2),
             bad AS (SELECT company_id FROM site_check WHERE checked IN (SELECT checked FROM last)
                     GROUP BY company_id HAVING count(*) = 2 AND NOT bool_or(ok))
        UPDATE company c SET hidden_reason = 'B1' FROM bad
        WHERE c.id = bad.company_id AND c.email_role IS NULL AND (SELECT count(*) FROM last) = 2
        RETURNING c.id, c.name""").fetchall()
    b7 = conn.execute("""
        UPDATE company c SET hidden_reason = 'B7' FROM suppression s
        WHERE s.company_id = c.id OR (s.name_norm IS NOT NULL AND s.name_norm = lower(c.name))
        RETURNING c.id, c.name""").fetchall()
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO cleanup_log VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                        [(run, cid, name, crit) for crit, rows in (("B1", b1), ("B7", b7)) for cid, name in rows])
    visible = conn.execute("SELECT count(*) FILTER (WHERE verified AND hidden_reason IS NULL) FROM company").fetchone()[0]
    print(f"cleanup {run}: B1 {len(b1)}, B7 {len(b7)} hidden; {visible} verified visible")


if __name__ == "__main__":
    main()
