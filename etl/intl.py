"""
International company base for matchmaking (`intl_company`): legal entities OUTSIDE Germany from EU registers.
Reads the same raw files as etl.load (no network) and replaces the table in one transaction.

    python -m etl.intl schema      # create intl_company (also part of web.matchmaking.SCHEMA)
    python -m etl.intl load        # ema + cordis + eudamed_intl (each only if its raw file exists)

Sources and rules (one row per source record; the UI groups rows with equal name_norm + country):
  ema          Article 57 XLSX. Marketing-authorisation holders none of whose pharmacovigilance master files
               (PSMF) is in Germany. The file has no MAH address: `country` is the
               most frequent PSMF location (facts.country_basis = 'psmf'), city/website stay empty. The PV e-mail and
               phone columns are never read.
  cordis       Horizon Europe CSV zip. Organisations with activityType PRC (private companies), country != DE, in
               health projects (same filter as load_cordis). Website from organizationURL.
  eudamed_intl raw/eudamed_intl/<day>/list_*.json from etl.sources.eudamed_intl. Active actors, country != DE.
Person filter for all: names flagged by norm.is_natural_person are dropped, and a name must carry a recognisable
company form (LEGAL_ENTITY) - sole traders without a form are left out. Measured coverage: none yet (no raw data
on the Mac); count after the first server run.
No person names, e-mails or phone numbers are stored.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import sys
import zipfile
from collections import Counter

from etl.common import norm
from etl.common.db import connect
from etl.common.raw import latest_dir, sha256_file

SCHEMA = """
CREATE TABLE IF NOT EXISTS intl_company (
  id text PRIMARY KEY,                      -- 'i' + 12 hex of sha1(source|source_id)
  source text NOT NULL, source_id text NOT NULL, licence text NOT NULL,
  raw_path text NOT NULL, raw_sha256 text NOT NULL, fetched_at date, loaded_at timestamptz NOT NULL DEFAULT now(),
  name text NOT NULL, name_norm text NOT NULL, country text NOT NULL, city text, website text,
  sectors text[] NOT NULL DEFAULT '{}', roles text[] NOT NULL DEFAULT '{}', facts jsonb NOT NULL DEFAULT '{}',
  UNIQUE (source, source_id));
CREATE INDEX IF NOT EXISTS intl_company_country ON intl_company (country);
CREATE INDEX IF NOT EXISTS intl_company_norm ON intl_company (name_norm, country);
"""

LICENCE = {
    "ema": "EMA legal notice: reproduction authorised, source acknowledged (Quelle: European Medicines Agency)",
    "cordis": "CC BY 4.0 (Quelle: Europäische Kommission, CORDIS)",
    "eudamed_intl": "Commission Decision 2011/833/EU (assumed; Quelle: Europäische Kommission, EUDAMED)",
}

# Company forms across Europe and main markets, matched on the folded name (norm.fold).
LEGAL_ENTITY = re.compile(
    r"\b(gmbh|ag|se|kg|eeig|ewiv|geie|sa|s\.a|sas|s\.a\.s|sarl|s\.a\.r\.l|srl|s\.r\.l|spa|s\.p\.a|sl|s\.l|slu|bv|b\.v|nv|n\.v|"
    r"ltd|limited|plc|llp|llc|inc|corp|corporation|company|co|pty|ab|oy|oyj|as|a/s|asa|aps|hf|ehf|sp\.? z o\.?o|"
    r"s\.?a\.?|kft|zrt|nyrt|s\.r\.o|a\.s|d\.o\.o|d\.d|ad|ead|ood|eood|sia|uab|ou|as|lda|s\.a\.u|sae|ae|ike|epe|"
    r"kk|k\.k|pte|bhd|sdn|pvt|private|holding|group|pharma\w*|labs?|laborator\w*|industr\w*|international|"
    r"medical|healthcare|biotech\w*|therapeutics|diagnostics|technolog\w*)\b",
)
CORDIS_COUNTRY = {"EL": "GR", "UK": "GB"}
EMA_COUNTRY = {  # PSMF location names in the Art. 57 file -> ISO2
    "Austria": "AT", "Belgium": "BE", "Bulgaria": "BG", "Croatia": "HR", "Cyprus": "CY", "Czech Republic": "CZ",
    "Czechia": "CZ", "Denmark": "DK", "Estonia": "EE", "Finland": "FI", "France": "FR", "Greece": "GR",
    "Hungary": "HU", "Iceland": "IS", "Ireland": "IE", "Italy": "IT", "Latvia": "LV", "Liechtenstein": "LI",
    "Lithuania": "LT", "Luxembourg": "LU", "Malta": "MT", "Netherlands": "NL", "Norway": "NO", "Poland": "PL",
    "Portugal": "PT", "Romania": "RO", "Slovakia": "SK", "Slovenia": "SI", "Spain": "ES", "Sweden": "SE",
    "United Kingdom": "GB", "Switzerland": "CH", "Northern Ireland": "GB",
}


PERSON_SHOP = re.compile(r"pharmacie|farmacia|pharmacy|apteka|apotek|\bcabinet\b|\bdr\.?\s|\bdott\.|\bprof\.")


def is_entity(name: str) -> bool:
    f = norm.fold(name)
    return not norm.is_natural_person(name) and not PERSON_SHOP.search(f) and bool(LEGAL_ENTITY.search(f))


def row(source, source_id, raw_path, sha, name, country, **kw) -> dict:
    name = " ".join(name.split())
    return dict(id="i" + hashlib.sha1(f"{source}|{source_id}".encode()).hexdigest()[:12], source=source,
                source_id=str(source_id), licence=LICENCE[source], raw_path=str(raw_path), raw_sha256=sha,
                fetched_at=raw_path.parent.name if hasattr(raw_path, "parent") else None, name=name,
                name_norm=norm.name_norm(name), country=country, city=kw.get("city"), website=kw.get("website"),
                sectors=kw.get("sectors", []), roles=kw.get("roles", []), facts=json.dumps(kw.get("facts", {})))


def from_ema() -> list[dict]:
    import openpyxl

    path = latest_dir("ema") / "article-57-product-data.xlsx"
    sha = sha256_file(path)
    ws = openpyxl.load_workbook(path, read_only=True).worksheets[0]
    header, mah = None, {}
    for r in ws.iter_rows(values_only=True):
        if header is None:
            header = r if r[0] and str(r[0]).startswith("Product name") else None
            continue
        _product, _subst, _route, auth_country, holder, psmf = r[:6]  # cols 7/8 (PV e-mail, phone) are never read
        if not holder:
            continue
        m = mah.setdefault(holder.strip(), {"n": 0, "psmf": Counter(), "auth": Counter()})
        m["n"] += 1
        if psmf:
            m["psmf"][str(psmf).strip()] += 1
        if auth_country:
            m["auth"][str(auth_country).strip()] += 1
    out = []
    for holder, m in mah.items():
        # German MAHs are etl.load's job: any PSMF in Germany excludes the holder. Austrian/Swiss GmbH/AG stay in.
        if "Germany" in m["psmf"] or not is_entity(holder):
            continue
        cc = next((EMA_COUNTRY[c] for c, _n in m["psmf"].most_common() if c in EMA_COUNTRY), None)
        if not cc:
            continue
        out.append(row("ema", norm.name_norm(holder) + "|" + holder, path, sha, holder, cc, sectors=["pharma"],
                       roles=["MAH"], facts={"products": m["n"], "country_basis": "psmf",
                                             "auth_countries": len(m["auth"])}))
    return out


def from_cordis() -> list[dict]:
    from etl.load import HEALTH_TOPIC, SCIVOC_SECTOR

    path = latest_dir("cordis") / "cordis-HORIZONprojects-csv.zip"
    sha = sha256_file(path)

    def table(z, name):
        member = next(n for n in z.namelist() if n.endswith(name))
        return csv.DictReader(io.TextIOWrapper(z.open(member), encoding="utf-8", newline=""), delimiter=";")

    with zipfile.ZipFile(path) as z:
        scivoc: dict[str, list[str]] = {}
        for r in table(z, "euroSciVoc.csv"):
            scivoc.setdefault(r["projectID"], []).append(r["euroSciVocPath"])
        health = {}
        for r in table(z, "project.csv"):
            paths = scivoc.get(r["id"], [])
            if r["topics"].startswith(HEALTH_TOPIC) or any(p.lstrip("/").startswith("medical and health sciences")
                                                           for p in paths):
                health[r["id"]] = (r["acronym"], paths)
        orgs: dict[str, dict] = {}
        for r in table(z, "organization.csv"):
            if r["country"] in ("DE", "") or r["activityType"] != "PRC" or r["projectID"] not in health:
                continue
            o = orgs.setdefault(r["organisationID"], {**r, "projects": [], "paths": set()})
            acronym, paths = health[r["projectID"]]
            o["projects"].append(acronym)
            o["paths"].update(paths)
    out = []
    for oid, o in orgs.items():
        if norm.is_natural_person(o["name"]):
            continue
        hints = sorted({sec for p in o["paths"] for sub, sec in SCIVOC_SECTOR if sub in p})
        out.append(row("cordis", oid, path, sha, o["name"], CORDIS_COUNTRY.get(o["country"], o["country"]),
                       city=(o["city"] or "").strip().title() or None, website=norm.website(o["organizationURL"]),
                       sectors=hints, roles=["RND"],
                       facts={"sme": o["SME"] == "true", "projects": sorted(set(o["projects"]))[:20]}))
    return out


ROLE = {"MF": "MF", "AR": "AR", "IM": "IM", "PR": "PR", "SPPP": "PR"}


def from_eudamed() -> list[dict]:
    d = latest_dir("eudamed_intl")
    out, seen = [], set()
    for page in sorted(d.glob("list_*.json")):
        sha = sha256_file(page)
        for a in json.loads(page.read_text()).get("content", []):
            cc = a.get("countryIso2Code")
            status = (a.get("actorStatus") or {}).get("code", "") or a.get("status") or ""
            if not cc or cc == "DE" or a["uuid"] in seen or not status.endswith(".active") or not is_entity(a["name"]):
                continue
            seen.add(a["uuid"])
            srn = a.get("srn") or a.get("eudamedIdentifier") or ""
            role = ROLE.get(srn.split("-")[1], "?") if srn.count("-") >= 1 else "?"
            out.append(row("eudamed_intl", a["uuid"], page, sha, a["name"], cc,
                           city=(a.get("cityName") or "").strip() or None, sectors=["medtech"], roles=[role],
                           facts={"srn": srn, "registered": (a.get("dateOfRegistration") or "")[:10]}))
    return out


COLS = ["id", "source", "source_id", "licence", "raw_path", "raw_sha256", "fetched_at", "name", "name_norm",
        "country", "city", "website", "sectors", "roles", "facts"]


def load() -> None:
    conn = connect()
    conn.execute(SCHEMA)
    for source, fn in (("ema", from_ema), ("cordis", from_cordis), ("eudamed_intl", from_eudamed)):
        try:
            rows = fn()
        except FileNotFoundError as exc:
            print(f"{source}: skipped ({exc})", flush=True)
            continue
        rows = list({r["id"]: r for r in rows}.values())
        with conn.transaction():
            conn.execute("DELETE FROM intl_company WHERE source = %s", (source,))
            with conn.cursor().copy(f"COPY intl_company ({', '.join(COLS)}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row([r[c] for c in COLS])
        print(f"{source}: {len(rows)} international companies loaded", flush=True)
    for r in conn.execute("SELECT source, count(*), count(DISTINCT country) FROM intl_company GROUP BY 1").fetchall():
        print(f"  {r[0]}: {r[1]} rows, {r[2]} countries")
    conn.execute("ANALYZE intl_company")


def main() -> None:
    if sys.argv[1:] == ["schema"]:
        connect().execute(SCHEMA)
        print("intl_company schema ok")
    elif sys.argv[1:] == ["load"]:
        load()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
