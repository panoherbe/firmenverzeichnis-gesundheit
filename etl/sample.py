"""
Quality sample: a seeded random draw of companies, written to CSV for a hand check, and the checked CSV read back
into `quality_check`. Results are reported per tier with 95 % Wilson intervals.

    python -m etl.sample draw  <name> [n=100] [--all]   # var/sample_<name>.csv (verified only unless --all)
    python -m etl.sample load  <name> <checked.csv>     # columns: company_id,is_company,in_scope,sector_ok,note
    python -m etl.sample report <name>
"""

from __future__ import annotations

import csv
import math
import sys

from etl.common.db import connect
from etl.common.env import ROOT


def wilson(k: int, n: int) -> str:
    if not n:
        return "–"
    p, z = k / n, 1.96
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return f"{k}/{n} = {100 * p:.0f} % (95 %-KI {100 * (c - h):.0f}–{100 * (c + h):.0f} %)"


def draw(name: str, n: int, everything: bool) -> None:
    conn = connect()
    conn.execute("SELECT setseed(0.42)")
    rows = conn.execute(f"""
        SELECT id, name, legal_form, street, post_code, city, website, tier, sectors, roles, sources,
               sector_evidence::text, public_awards
        FROM company {'' if everything else 'WHERE verified'} ORDER BY random() LIMIT %s""", (n,)).fetchall()
    out = ROOT / "var" / f"sample_{name}.csv"
    out.parent.mkdir(exist_ok=True)
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company_id", "name", "legal_form", "street", "post_code", "city", "website", "tier", "sectors",
                    "roles", "sources", "evidence", "public_awards", "is_company", "in_scope", "sector_ok", "note"])
        for r in rows:
            w.writerow([*r[:8], " ".join(r[8]), " ".join(r[9]), " ".join(r[10]), r[11], r[12], "", "", "", ""])
    print(f"{len(rows)} rows -> {out}")


def b(v: str):
    v = (v or "").strip().lower()
    return True if v in {"1", "y", "j", "ja", "yes", "true"} else False if v in {"0", "n", "nein", "no", "false"} else None


def load(name: str, path: str) -> None:
    conn = connect()
    rows = list(csv.DictReader(open(path)))
    with conn.transaction():
        conn.execute("DELETE FROM quality_check WHERE sample = %s", (name,))
        for r in rows:
            conn.execute("INSERT INTO quality_check (sample, company_id, is_company, in_scope, sector_ok, note) "
                         "VALUES (%s, %s, %s, %s, %s, %s)",
                         (name, r["company_id"], b(r["is_company"]), b(r["in_scope"]), b(r["sector_ok"]), r.get("note")))
    print(f"{len(rows)} checks loaded")


def report(name: str) -> None:
    conn = connect()
    rows = conn.execute("""
        SELECT c.tier, q.is_company, q.in_scope, q.sector_ok FROM quality_check q JOIN company c ON c.id = q.company_id
        WHERE q.sample = %s""", (name,)).fetchall()
    for tier in ["A", "B", "C", "alle"]:
        rs = [r for r in rows if tier == "alle" or r[0] == tier]
        if not rs:
            continue
        print(f"Stufe {tier}: n={len(rs)}")
        for i, label in [(1, "existierende Firma"), (2, "gehört zur Gesundheitswirtschaft"), (3, "Branche richtig")]:
            judged = [r[i] for r in rs if r[i] is not None]
            print(f"  {label}: {wilson(sum(judged), len(judged))}")


def main() -> None:
    a = sys.argv[1:]
    if a[:1] == ["draw"]:
        draw(a[1], int(a[2]) if len(a) > 2 and a[2].isdigit() else 100, "--all" in a)
    elif a[:1] == ["load"]:
        load(a[1], a[2])
    elif a[:1] == ["report"]:
        report(a[1])
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
