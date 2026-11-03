"""
Parse the newest raw files of each source into `source_record` (and `gleif_de`). Re-runnable: each source's rows
are replaced in one transaction. No network.

    python -m etl.load schema            # apply db/schema.sql
    python -m etl.load eudamed|ema|corpus|gleif|all
"""

from __future__ import annotations

import csv
import io
import json
import re
import sys
import zipfile
from pathlib import Path

from psycopg.types.json import Jsonb

from etl.common import norm
from etl.common.db import connect
from etl.common.env import ROOT
from etl.common.raw import latest_dir, sha256_file

COLS = ["source", "source_id", "raw_path", "raw_sha256", "name", "name_norm", "legal_form", "natural_person",
        "street", "post_code", "city", "region", "country", "website", "domain", "email_role", "vat",
        "register_no", "register_ra", "lei", "srn", "roles", "active", "facts"]


def record(source, source_id, raw_path, sha, name, **kw) -> dict:
    name = " ".join(name.split())
    r = dict.fromkeys(COLS)
    r.update(source=source, source_id=str(source_id), raw_path=str(raw_path), raw_sha256=sha, name=name,
             name_norm=norm.name_norm(name), legal_form=norm.legal_form(name),
             natural_person=norm.is_natural_person(name), country="DE", roles=[], active=True, facts={})
    r.update(kw)
    r["facts"] = Jsonb(r["facts"])
    return r


def replace(conn, source: str, rows: list[dict]) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM source_record WHERE source = %s", (source,))
        with conn.cursor().copy(f"COPY source_record ({', '.join(COLS)}) FROM STDIN") as cp:
            for r in rows:
                cp.write_row([r[c] for c in COLS])
    print(f"{source}: {len(rows)} records loaded", flush=True)


# --- EUDAMED -----------------------------------------------------------------------------------------------------

ROLE = {"MF": "MF", "AR": "AR", "IM": "IM", "PR": "PR", "SPPP": "PR"}


def load_eudamed(conn) -> None:
    d = latest_dir("eudamed")
    details = {}
    det_path = d / "details.jsonl"
    det_sha = sha256_file(det_path) if det_path.exists() else ""
    if det_path.exists():
        for line in det_path.open():
            j = json.loads(line)
            if j.get("status") == 200:  # failed attempts are logged as lines too; the 200 line wins
                details[j["uuid"]] = j["data"].get("actorDataPublicView") or {}
    listed: dict[str, tuple] = {}
    seed = d / "list_seed.jsonl"
    if seed.exists():
        sha = sha256_file(seed)
        for a in map(json.loads, seed.open()):
            listed[a["uuid"]] = (a, seed, sha)
    for page in sorted(d.glob("list_*.json")):
        sha = sha256_file(page)
        for a in json.loads(page.read_text())["content"]:
            listed[a["uuid"]] = (a, page, sha)
    rows = []
    for uuid, (a, page, sha) in listed.items():
        if a.get("countryIso2Code") != "DE":
            continue
        det = details.get(uuid, {})
        addr = det.get("actorAddress") or {}
        street = " ".join(x for x in [addr.get("streetName"), addr.get("buildingNumber")] if x) or None
        srn = a.get("srn") or a.get("eudamedIdentifier")
        role = ROLE.get(srn.split("-")[1], "?")
        status = a.get("status") or (a.get("actorStatus") or {}).get("code", "")
        rows.append(record(
            "eudamed", uuid, det_path if det else page, det_sha if det else sha, a["name"],
            street=street, post_code=norm.post_code(addr.get("postalZone") or a.get("postalZone")),
            city=(addr.get("cityName") or a.get("cityName") or "").strip() or None,
            website=norm.website(det.get("website")), domain=norm.domain(det.get("website")),
            email_role=norm.role_email(det.get("electronicMail") or a.get("electronicMail")),
            vat=norm.vat(det.get("europeanVatNumber")),
            register_no=norm.register_no(det.get("tradeRegister")),
            srn=srn, roles=[role], active=status.endswith(".active"),
            facts={"registered": (a.get("dateOfRegistration") or "")[:10],
                   "validator": det.get("validatorName"), "detail": bool(det)},
        ))
    replace(conn, "eudamed", rows)


# --- EMA Article 57 ------------------------------------------------------------------------------------------------

def load_ema(conn) -> None:
    import openpyxl

    path = latest_dir("ema") / "article-57-product-data.xlsx"
    sha = sha256_file(path)
    ws = openpyxl.load_workbook(path, read_only=True).worksheets[0]
    header = None
    mah: dict[str, dict] = {}
    for row in ws.iter_rows(values_only=True):
        if header is None:
            if row[0] and str(row[0]).startswith("Product name"):
                header = row
            continue
        product, _subst, _route, country, holder, psmf, _mail, _tel = row[:8]
        if not holder:
            continue
        m = mah.setdefault(holder.strip(), {"products_de": 0, "products_all": 0, "psmf": set(), "names": set()})
        m["products_all"] += 1
        if product and len(m["names"]) < 40:
            m["names"].add(str(product).strip()[:80])
        if country == "Germany":
            m["products_de"] += 1
        if psmf:
            m["psmf"].add(psmf)
    rows = []
    for holder, m in mah.items():
        # German companies only: a German legal form, plus at least one DE authorisation or a German PSMF
        if not norm.legal_form(holder) or not (m["products_de"] or "Germany" in m["psmf"]):
            continue
        if re.search(r"austria|österreich|oesterreich|schweiz|suisse|switzerland|nederland|polska|españa|italia",
                     holder, re.I):
            continue
        if not (norm.legal_form(holder) in {"GmbH", "AG", "SE", "KG", "KGaA", "UG", "OHG", "GmbH & Co. KG",
                                              "AG & Co. KG", "SE & Co. KG", "SE & Co. KGaA", "AG & Co. KGaA",
                                              "GmbH & Co. KGaA"}):
            continue
        rows.append(record("ema", norm.name_norm(holder) + "|" + holder, path, sha, holder, roles=["MAH"],
                           facts={"products_de": m["products_de"], "products_all": m["products_all"],
                                  "psmf_de": "Germany" in m["psmf"], "products": sorted(m["names"])}))
    replace(conn, "ema", rows)


# --- Tender corpus -------------------------------------------------------------------------------------------------

def load_corpus(conn) -> None:
    path = latest_dir("corpus") / "health_winners.csv"
    sha = sha256_file(path)
    rows = []
    for r in csv.DictReader(path.open()):
        facts = {"awards": int(r["awards"]), "notices": int(r["notices"]), "buyers": int(r["buyers"]),
                 "value_eur": float(r["value_eur"]) if r["value_eur"] else None,
                 "first_award": r["first_award"], "last_award": r["last_award"], "cpv4": r["cpv4"].split()}
        members = norm.split_group(r["name"])
        if members is None:
            names = [(r["org_id"], r["name"], facts)]
        else:  # consortium or multi-winner string: one record per member, flagged
            names = [(f"{r['org_id']}#{i}", m, {**facts, "via_group": True}) for i, m in enumerate(members)]
        for sid, name, f in names:
            if norm.is_non_industry(name):
                continue
            rows.append(record(
                "corpus", sid, path, sha, name,
                post_code=norm.post_code(r["post_code"]) if members is None else None,
                city=((r["city"] or "").strip() or None) if members is None else None,
                roles=["SUPPLIER"], facts=f,
            ))
    replace(conn, "corpus", rows)


# --- CORDIS Horizon Europe -----------------------------------------------------------------------------------------

HEALTH_TOPIC = ("HORIZON-HLTH", "HORIZON-JU-IHI", "HORIZON-JU-IHI", "IMI")
SCIVOC_SECTOR = [  # (substring of the EuroSciVoc path, sector)
    ("biotechnology", "biotech"), ("genetic", "biotech"), ("pharmacology and pharmacy", "pharma"),
    ("medical engineering", "medtech"), ("medical laboratory technology", "medtech"), ("diagnostic", "medtech"),
    ("computer and information sciences", "digital_health"), ("software", "digital_health"),
    ("artificial intelligence", "digital_health"), ("data science", "digital_health"),
    ("telemedicine", "digital_health"), ("medical informatics", "digital_health"),
]


def load_cordis(conn) -> None:
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
            if r["topics"].startswith(HEALTH_TOPIC) or any(p.startswith("/medical and health sciences")
                                                           or p.startswith("medical and health sciences")
                                                           for p in paths):
                health[r["id"]] = (r["acronym"], paths)
        orgs: dict[str, dict] = {}
        for r in table(z, "organization.csv"):
            if r["country"] != "DE" or r["activityType"] != "PRC" or r["projectID"] not in health:
                continue
            o = orgs.setdefault(r["organisationID"], {**r, "projects": [], "paths": set()})
            acronym, paths = health[r["projectID"]]
            o["projects"].append(acronym)
            o["paths"].update(paths)
    rows = []
    for oid, o in orgs.items():
        hints = sorted({sec for p in o["paths"] for sub, sec in SCIVOC_SECTOR if sub in p})
        rows.append(record(
            "cordis", oid, path, sha, o["name"], street=o["street"] or None, post_code=norm.post_code(o["postCode"]),
            city=o["city"] or None, website=norm.website(o["organizationURL"]), domain=norm.domain(o["organizationURL"]),
            vat=norm.vat(o["vatNumber"]), roles=["RND"],
            facts={"sme": o["SME"] == "true", "projects": sorted(set(o["projects"])), "sector_hints": hints},
        ))
    replace(conn, "cordis", rows)


# --- GLEIF (reference table, not a company source) ----------------------------------------------------------------

GLEIF_SQL = """
CREATE TABLE IF NOT EXISTS gleif_de (
    lei text PRIMARY KEY, name text NOT NULL, name_norm text NOT NULL, legal_form_code text,
    register_ra text, register_no text, street text, post_code text, city text, region text,
    status text, raw_sha256 text NOT NULL
);
CREATE INDEX IF NOT EXISTS gleif_de_name_idx ON gleif_de (name_norm);
CREATE INDEX IF NOT EXISTS gleif_de_reg_idx ON gleif_de (register_no);
"""


def load_gleif(conn) -> None:
    path = latest_dir("gleif") / "lei2-golden-copy.csv.zip"
    sha = sha256_file(path)
    conn.execute(GLEIF_SQL)
    n = 0
    with zipfile.ZipFile(path) as z, conn.transaction():
        conn.execute("TRUNCATE gleif_de")
        member = z.namelist()[0]
        reader = csv.DictReader(io.TextIOWrapper(z.open(member), encoding="utf-8", newline=""))
        with conn.cursor().copy("COPY gleif_de FROM STDIN") as cp:
            for r in reader:
                if r["Entity.LegalAddress.Country"] != "DE" or r.get("Entity.EntityStatus") != "ACTIVE":
                    continue
                street = " ".join(x for x in [r["Entity.LegalAddress.FirstAddressLine"],
                                              r.get("Entity.LegalAddress.AdditionalAddressLine.1")] if x)
                cp.write_row([
                    r["LEI"], r["Entity.LegalName"], norm.name_norm(r["Entity.LegalName"]),
                    r.get("Entity.LegalForm.EntityLegalFormCode"),
                    r.get("Entity.RegistrationAuthority.RegistrationAuthorityID"),
                    norm.register_no(r.get("Entity.RegistrationAuthority.RegistrationAuthorityEntityID")),
                    street or None, norm.post_code(r["Entity.LegalAddress.PostalCode"]),
                    r["Entity.LegalAddress.City"] or None, r["Entity.LegalAddress.Region"] or None,
                    r.get("Registration.RegistrationStatus"), sha,
                ])
                n += 1
    conn.execute("ANALYZE gleif_de")
    print(f"gleif: {n} active German LEIs loaded", flush=True)


def main() -> None:
    what = sys.argv[1:] or ["all"]
    conn = connect()
    if what == ["schema"]:
        conn.execute((ROOT / "db/schema.sql").read_text())
        print("schema applied")
        return
    loaders = {"eudamed": load_eudamed, "ema": load_ema, "corpus": load_corpus, "cordis": load_cordis,
               "gleif": load_gleif}
    for name in (loaders if what == ["all"] else what):
        loaders[name](conn)
    conn.execute("ANALYZE source_record")


if __name__ == "__main__":
    main()
