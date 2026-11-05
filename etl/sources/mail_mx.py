"""
Is the domain of a register role mailbox able to receive mail? A profile needs at least an e-mail address or a link
to the contact page; a role mailbox from the register (EUDAMED, EMA) counts on its own, also when the company's
website blocks bots. etl.enrich keeps it only when its domain has a real MX record (an A record alone often points at
a CDN; null MX and "localhost" mean no mail). Lookups over DNS-over-HTTPS (the ETL host has no DNS resolver), no mail is sent.

Raw before parse: raw/mail_mx/<day>/mx.jsonl {domain, mx: [...], a: bool, status}. etl.enrich applies it.

    python -m etl.sources.mail_mx
"""

from __future__ import annotations

import json
import ssl
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from etl.common.db import connect
from etl.common.raw import run_dir

ENDPOINTS = ("https://1.1.1.1/dns-query?name={name}&type={t}", "https://8.8.8.8/resolve?name={name}&type={t}")


def query(name: str, t: str) -> tuple[list[str], str]:
    ctx = ssl.create_default_context()
    for tpl in ENDPOINTS:
        req = urllib.request.Request(tpl.format(name=name, t=t), headers={"accept": "application/dns-json"})
        try:
            with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                p = json.load(resp)
        except Exception:  # noqa: BLE001  try the next endpoint
            continue
        if p.get("Status") == 3:
            return [], "NXDOMAIN"
        want = 15 if t == "MX" else 1
        return [a["data"] for a in p.get("Answer", []) if a.get("type") == want], "ok"
    return [], "lookup failed"


def one(domain: str) -> dict:
    mx, status = query(domain, "MX")
    a = bool(query(domain, "A")[0]) if not mx and status == "ok" else bool(mx)
    return {"domain": domain, "mx": mx[:3], "a": a, "status": status}


def main() -> None:
    with connect() as conn:
        domains = sorted({r[0].split("@")[1].lower() for r in conn.execute(
            "SELECT email_role FROM company WHERE verified AND email_role IS NOT NULL").fetchall()})
    out = run_dir("mail_mx") / "mx.jsonl"
    with out.open("w") as f, ThreadPoolExecutor(max_workers=8) as pool:
        for rec in pool.map(one, domains):
            f.write(json.dumps(rec) + "\n")
    recs = [json.loads(l) for l in out.open()]
    print(f"mail_mx: {len(recs)} domains, {sum(bool(r['mx']) for r in recs)} with MX, "
          f"{sum(not r['mx'] and r['a'] for r in recs)} only A, {sum(r['status'] == 'NXDOMAIN' for r in recs)} NXDOMAIN, "
          f"{sum(r['status'] == 'lookup failed' for r in recs)} lookup failed")


if __name__ == "__main__":
    main()
