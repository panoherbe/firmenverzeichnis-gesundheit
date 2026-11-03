"""
EUDAMED actor registration: every German actor (manufacturer, authorised rep, importer, system/procedure-pack
producer), list pages + one public detail record per actor.

Raw layout: raw/eudamed/<day>/list_<type>_<page>.json and raw/eudamed/<day>/details.jsonl (one detail per line).
Resumable: details already in details.jsonl are skipped.

DATA MINIMISATION (deliberate deviation from "raw = exact payload"): the detail record names the PRRC
(`regulatoryComplianceResponsibles`: first/family name, e-mail, phone). That block is removed BEFORE the record
is written to disk, so no person data from EUDAMED ever lands anywhere.

Details only for ACTIVE actors (inactive ones never enter the directory). The API answers a detail call in ~5 s,
so details run on 2 threads with 1.5 s pauses (8 threads drew HTTP 429 within two minutes and 4 threads within
fifteen, measured 2026-11-02; expect an overnight run for ~6,250 active actors). A failed detail is written with its status and retried on the next run. The list endpoint takes `size`, not `pageSize`
(`pageSize` is silently ignored and gives 20 per page).

Seed list: the list endpoint is very slow (measured 2026-11-02: 20-120 s per 100-row page, 500s in between).
If raw/eudamed/<day>/list_seed.jsonl exists (one slim actor per line: uuid, name, srn, postalZone, cityName,
electronicMail, type, status — written by the research agent from the same API on 2026-11-02), paging is skipped.

    python -m etl.sources.eudamed fetch
"""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor

from etl.common import fetch
from etl.common.raw import run_dir

BASE = "https://ec.europa.eu/tools/eudamed/api"
TYPES = ["manufacturer", "authorised-representative", "importer", "system-procedure-pack-producer"]
PERSON_KEYS = ("regulatoryComplianceResponsibles",)


def strip_persons(obj):
    if isinstance(obj, dict):
        return {k: strip_persons(v) for k, v in obj.items() if k not in PERSON_KEYS}
    if isinstance(obj, list):
        return [strip_persons(v) for v in obj]
    return obj


def fetch_lists(c, out) -> list[str]:
    """Returns the uuids of ACTIVE actors."""
    uuids: list[str] = []
    for t in TYPES:
        page = 0
        while True:
            path = out / f"list_{t}_{page:03d}.json"
            if path.exists():
                data = json.loads(path.read_text())
            else:
                resp = fetch.get(c, f"{BASE}/eos", params={
                    "page": page, "size": 100, "countryIso2Code": "DE",
                    "actorTypeCode": f"refdata.actor-type.{t}", "languageIso2Code": "en",
                })
                resp.raise_for_status()
                data = resp.json()
                path.write_text(json.dumps(data, ensure_ascii=False))
            uuids += [a["uuid"] for a in data["content"]
                      if (a.get("actorStatus") or {}).get("code", "").endswith(".active")]
            if data.get("last") or not data["content"]:
                break
            page += 1
        print(f"  list {t}: {page + 1} pages, {len(uuids)} uuids so far", flush=True)
    return uuids


def fetch_details(c, out, uuids: list[str]) -> None:
    path = out / "details.jsonl"
    done = set()
    if path.exists():
        for line in path.open():
            j = json.loads(line)
            if j.get("status") == 200:
                done.add(j["uuid"])
    todo = [u for u in dict.fromkeys(uuids) if u not in done]
    print(f"  details: {len(done)} done, {len(todo)} to fetch", flush=True)
    lock = threading.Lock()
    clients = threading.local()
    count = [0]

    def one(u: str) -> None:
        if not hasattr(clients, "c"):
            clients.c = fetch.client()
        try:
            resp = fetch.get(clients.c, f"{BASE}/actors/{u}/publicInformation",
                             params={"languageIso2Code": "en"}, pause=1.5, tries=6)
        except Exception as exc:  # noqa: BLE001  one actor must not kill a 4-hour run
            with lock:
                f.write(json.dumps({"uuid": u, "status": str(exc)[:40]}) + "\n")
            return
        if resp.status_code == 200:
            line = json.dumps({"uuid": u, "status": 200, "data": strip_persons(resp.json())}, ensure_ascii=False)
        else:
            line = json.dumps({"uuid": u, "status": resp.status_code})
        with lock:
            f.write(line + "\n")
            count[0] += 1
            if count[0] % 250 == 0:
                f.flush()
                print(f"  details {count[0]}/{len(todo)}", flush=True)

    with path.open("a") as f, ThreadPoolExecutor(max_workers=2) as pool:
        for _ in pool.map(one, todo):
            pass


def main() -> None:
    if sys.argv[1:] != ["fetch"]:
        sys.exit(__doc__)
    out = run_dir("eudamed")
    c = fetch.client()
    seed = out / "list_seed.jsonl"
    if seed.exists():
        actors = [a for a in map(json.loads, seed.open()) if a.get("status", "").endswith(".active")]
        # manufacturers and procedure-pack producers first: they make tier A; reps/importers can wait
        actors.sort(key=lambda a: {"MF": 0, "PR": 1, "AR": 2, "IM": 3}.get(a.get("type"), 4))
        uuids = [a["uuid"] for a in actors]
        print(f"  seed list: {len(uuids)} active actors", flush=True)
    else:
        uuids = fetch_lists(c, out)
    fetch_details(c, out, uuids)
    print("eudamed fetch done", flush=True)


if __name__ == "__main__":
    main()
