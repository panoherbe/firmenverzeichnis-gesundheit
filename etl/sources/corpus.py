"""
Winners of German health-sector awards from a public procurement notice database (TED / Bekanntmachungsservice), read-only.
Health = main CPV 33* (medical equipment, pharmaceuticals, personal care) or 4818* (medical software).
One CSV row per winner org with award counts, value, CPV families and buyer count.

    python -m etl.sources.corpus fetch   # raw/corpus/<day>/health_winners.csv
"""

from __future__ import annotations

import csv
import sys

from etl.common.db import connect_corpus
from etl.common.raw import run_dir

SQL = """
SELECT o.id AS org_id, o.name, o.city, o.post_code, o.country, o.nuts,
       count(*) AS awards,
       count(DISTINCT a.notice_id) AS notices,
       sum(a.value_amount) FILTER (WHERE a.value_currency = 'EUR') AS value_eur,
       min(n.publication_date) AS first_award, max(n.publication_date) AS last_award,
       array_to_string(array_agg(DISTINCT left(n.cpv_main, 4)), ' ') AS cpv4,
       count(DISTINCT r.org_id) AS buyers
FROM award a
JOIN notice n ON n.id = a.notice_id
JOIN org o ON o.id = a.winner_org_id
LEFT JOIN notice_org_role r ON r.notice_id = a.notice_id AND r.role = 'buyer'
WHERE o.country IN ('DE', 'DEU')
  AND (n.cpv_main LIKE '33%%' OR n.cpv_main LIKE '4818%%')
GROUP BY o.id
"""


def main() -> None:
    if sys.argv[1:] != ["fetch"]:
        sys.exit(__doc__)
    out = run_dir("corpus") / "health_winners.csv"
    with connect_corpus() as conn, out.open("w", newline="") as f:
        cur = conn.execute(SQL)
        w = csv.writer(f)
        w.writerow([d.name for d in cur.description])
        n = 0
        for row in cur:
            w.writerow(row)
            n += 1
    print(f"corpus: {n} winner orgs -> {out}")


if __name__ == "__main__":
    main()
