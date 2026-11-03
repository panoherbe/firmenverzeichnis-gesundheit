"""
EUDAMED devices per German manufacturer / procedure-pack producer: how many devices, risk classes, trade names,
and for a sample of up to DETAILS devices the EMDN product codes (nomenclature) and the EU markets where they are
available. This is the "Produktkennung" of a MedTech profile.

Raw layout: raw/eudamed_devices/<day>/devices.jsonl, one line per SRN:
  {srn, status, total, rows: [first page, up to 100 slim device rows], details: [{uuid, emdn: [[code, text]],
   markets: [iso2], risk}]}
Resumable: SRNs already in the file with status 200 are skipped. Device records hold no person data.

4 threads, 1.5 s pause, 2 detail calls per SRN (5 details on 2 threads managed ~70 SRNs an hour, measured 2026-11-03); 429 backs off. Measured 2026-11-03: the list endpoint
takes `srn` (B. Braun Melsungen DE-MF-000000201: 1,714 devices) and returns `cndNomenclatures` only in the detail.

    python -m etl.sources.eudamed_devices fetch
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from etl.common import fetch
from etl.common.db import connect
from etl.common.raw import run_dir

BASE = "https://ec.europa.eu/tools/eudamed/api/devices/udiDiData"
DETAILS = 2


def slim(row: dict) -> dict:
    return {"uuid": row.get("uuid"), "basicUdi": row.get("basicUdi"), "tradeName": row.get("tradeName"),
            "risk": (row.get("riskClass") or {}).get("code", "").rsplit(".", 1)[-1],
            "status": (row.get("deviceStatusType") or {}).get("code", "").rsplit(".", 1)[-1]}


def detail(d: dict) -> dict:
    emdn = []
    for n in d.get("cndNomenclatures") or []:
        emdn.append([n.get("code"), ((n.get("description") or {}).get("textByDefaultLanguage") or "")[:160]])
    markets = [(m.get("country") or {}).get("iso2Code") for m in (d.get("marketInfoLink") or {}).get("msWhereAvailable") or []]
    return {"uuid": d.get("uuid"), "emdn": emdn, "markets": [m for m in markets if m],
            "placed": (d.get("placedOnTheMarket") or {}).get("iso2Code")}


def srns() -> list[str]:
    with connect() as conn:
        rows = conn.execute("""SELECT DISTINCT srn FROM source_record
                               WHERE source = 'eudamed' AND active AND srn ~ '^DE-(MF|PR)-' ORDER BY srn""").fetchall()
    return [r[0] for r in rows]


def main() -> None:
    if sys.argv[1:] != ["fetch"]:
        sys.exit(__doc__)
    out = run_dir("eudamed_devices")
    path = out / "devices.jsonl"
    done = set()
    if path.exists():
        for line in path.open():
            j = json.loads(line)
            if j.get("status") == 200:
                done.add(j["srn"])
    todo = [s for s in srns() if s not in done]
    print(f"  devices: {len(done)} done, {len(todo)} to fetch", flush=True)
    lock, local, count = threading.Lock(), threading.local(), [0]

    def one(srn: str) -> None:
        if not hasattr(local, "c"):
            local.c = fetch.client()
        try:
            resp = fetch.get(local.c, BASE, params={"page": 0, "size": 100, "srn": srn, "languageIso2Code": "en"},
                             pause=1.5, tries=6)
            if resp.status_code != 200:
                rec = {"srn": srn, "status": resp.status_code}
            else:
                data = resp.json()
                rows = [slim(r) for r in data.get("content") or []]
                seen, pick = set(), []
                for r in rows:  # one device per Basic UDI first: different products, not package sizes
                    if r["basicUdi"] not in seen and r["status"] != "no-longer-placed-on-the-market":
                        seen.add(r["basicUdi"])
                        pick.append(r["uuid"])
                details = []
                for u in pick[:DETAILS]:
                    d = fetch.get(local.c, f"{BASE}/{u}", params={"languageIso2Code": "en"}, pause=1.5, tries=6)
                    if d.status_code == 200:
                        details.append(detail(d.json()))
                rec = {"srn": srn, "status": 200, "total": data.get("totalElements"), "rows": rows, "details": details}
        except Exception as exc:  # noqa: BLE001  one SRN must not kill an overnight run
            rec = {"srn": srn, "status": str(exc)[:40]}
        with lock:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count[0] += 1
            if count[0] % 100 == 0:
                f.flush()
                print(f"  devices {count[0]}/{len(todo)}", flush=True)

    with path.open("a") as f, ThreadPoolExecutor(max_workers=4) as pool:
        for _ in pool.map(one, todo):
            pass
    print("eudamed_devices fetch done", flush=True)


if __name__ == "__main__":
    main()
