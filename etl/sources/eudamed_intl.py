"""
EUDAMED actors OUTSIDE Germany (all actor types), list endpoint only - feeds the international company base
(`etl.intl`), not the German directory. No detail calls.

Raw layout: raw/eudamed_intl/<day>/list_<CC>_<page>.json plus done_<CC> marker files. Resumable: an existing page
file is not fetched again, a country with a done_<CC> marker is skipped. A run continues in the newest day folder
until every country is done (marker `complete`), then the next run starts a new day folder. `--day` forces one.

Polite by design: one thread, `size=100`, pause >= 1.5 s after every call, long back-off on 429 (etl.common.fetch).
The list endpoint is slow (20-120 s per page measured 2026-11-02 for DE); expect a multi-night run.

DATA MINIMISATION: each page is passed through eudamed.strip_persons (PRRC block) and the actor contact keys
(e-mail, phone) are removed BEFORE the page is written to disk.

    python -m etl.sources.eudamed_intl fetch [--day YYYY-MM-DD] [--countries FR,IT,...]
"""

from __future__ import annotations

import json
import sys

from etl.common import fetch
from etl.common.env import raw_dir
from etl.common.raw import run_dir
from etl.sources.eudamed import BASE, strip_persons

CONTACT_KEYS = ("electronicMail", "telephone", "telephoneNumber", "phone", "email")
# EU/EEA + CH/GB/TR first (most EUDAMED actors), then the big non-EU manufacturing countries, then the rest.
PRIORITY = ("AT BE BG HR CY CZ DK EE FI FR GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE IS LI NO CH GB TR "
            "US CN JP KR IN IL CA AU TW BR SG MX ZA AE SA").split()
OTHER = ("AD AL AM AR AZ BA BD BH BY CL CO CR DZ EC EG GE HK ID IQ IR JO KE KW KZ LB LK MA MC MD ME MK MY NG NZ "
         "OM PE PH PK QA RS RU SM TH TN UA UY UZ VN").split()


def clean(obj):
    obj = strip_persons(obj)
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items() if k not in CONTACT_KEYS}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    return obj


def pick_day(forced: str | None):
    if forced:
        return run_dir("eudamed_intl", forced)
    base = raw_dir() / "eudamed_intl"
    days = sorted(p for p in base.iterdir() if p.is_dir()) if base.exists() else []
    if days and not (days[-1] / "complete").exists():
        return days[-1]
    return run_dir("eudamed_intl")


def fetch_country(c, out, cc: str) -> int:
    page, n = 0, 0
    while True:
        path = out / f"list_{cc}_{page:03d}.json"
        if path.exists():
            data = json.loads(path.read_text())
        else:
            resp = fetch.get(c, f"{BASE}/eos", params={"page": page, "size": 100, "countryIso2Code": cc,
                                                      "languageIso2Code": "en"}, pause=1.5, tries=6)
            resp.raise_for_status()
            data = clean(resp.json())
            path.write_text(json.dumps(data, ensure_ascii=False))
        n += len(data.get("content") or [])
        if data.get("last", True) or not data.get("content"):
            break
        page += 1
    (out / f"done_{cc}").write_text(str(n))
    return n


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] != "fetch":
        sys.exit(__doc__)
    day = args[args.index("--day") + 1] if "--day" in args else None
    countries = args[args.index("--countries") + 1].upper().split(",") if "--countries" in args else PRIORITY + OTHER
    out = pick_day(day)
    c = fetch.client()
    print(f"eudamed_intl -> {out}", flush=True)
    for cc in countries:
        if cc == "DE" or (out / f"done_{cc}").exists():
            continue
        try:
            n = fetch_country(c, out, cc)
        except Exception as exc:  # noqa: BLE001  one country must not end a multi-night run; retried next run
            print(f"  {cc}: failed ({str(exc)[:60]}), retry next run", flush=True)
            continue
        print(f"  {cc}: {n} actors", flush=True)
    if "--countries" not in args and all((out / f"done_{cc}").exists() for cc in PRIORITY + OTHER):
        (out / "complete").write_text("ok")
    print("eudamed_intl fetch done", flush=True)


if __name__ == "__main__":
    main()
