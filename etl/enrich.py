"""
Enrich `company` after etl.resolve with facts from the newest raw runs (idempotent; run after every resolve):

  devices   raw/eudamed_devices  → device_count, emdn [[code, text]], emdn_groups (EMDN category letters), markets
  websites  raw/websites         → contact_url, site_ok (home page answered 2xx/3xx), site_checked
                                   employees + employees_source + employees_evidence (sentence the company wrote)
  ema       source_record facts  → products (medicinal product names, up to 20) for EMA marketing-authorisation holders
  wikidata  raw/wikidata         → employees / revenue for companies matched by LEI (CC0; optional, if fetched)
  size      employees → size_class (EU SME: micro < 10, small < 50, medium < 250, large ≥ 250);
            revenue → revenue_class r1 ≤ 2 M€, r2 ≤ 10 M€, r3 ≤ 50 M€, r4 > 50 M€
  search    appends EMDN texts, product names and the contact domain to the tsvector

Employee figures from websites: regex over the visible text of home/about/career pages; the number must sit next to
"Mitarbeiter*/Mitarbeitende/Beschäftigte/employees/staff". Precision is measured on a hand-checked sample before
the field is shown (sample 1 of 2026-11-03: 18/25 right, errors mostly company-history figures,
so a figure with a year in the 90 characters before it is skipped unless "heute/currently/…" is there too;
sample 2 after that rule: 27/30 right, 95 % CI ≈ 74–97 %); a figure from a group page ("weltweit", "worldwide", "Gruppe", "group") is kept
but marked as group-level and not used for the size class.

    python -m etl.enrich
"""

from __future__ import annotations

import gzip
import html
import json
import re
from collections import Counter

from etl.common.db import connect
from etl.common.raw import latest_dir

COLUMNS = """
ALTER TABLE company ADD COLUMN IF NOT EXISTS contact_url text;
ALTER TABLE company ADD COLUMN IF NOT EXISTS site_ok boolean;
ALTER TABLE company ADD COLUMN IF NOT EXISTS site_checked date;
ALTER TABLE company ADD COLUMN IF NOT EXISTS employees int;
ALTER TABLE company ADD COLUMN IF NOT EXISTS employees_source text;
ALTER TABLE company ADD COLUMN IF NOT EXISTS employees_evidence text;
ALTER TABLE company ADD COLUMN IF NOT EXISTS size_class text;
ALTER TABLE company ADD COLUMN IF NOT EXISTS revenue_eur numeric;
ALTER TABLE company ADD COLUMN IF NOT EXISTS revenue_class text;
ALTER TABLE company ADD COLUMN IF NOT EXISTS device_count int;
ALTER TABLE company ADD COLUMN IF NOT EXISTS emdn jsonb;
ALTER TABLE company ADD COLUMN IF NOT EXISTS emdn_groups text[];
ALTER TABLE company ADD COLUMN IF NOT EXISTS emdn_source text;
ALTER TABLE company ADD COLUMN IF NOT EXISTS emdn_evidence text;
ALTER TABLE company ADD COLUMN IF NOT EXISTS markets text[];
ALTER TABLE company ADD COLUMN IF NOT EXISTS products text[];
CREATE TABLE IF NOT EXISTS site_check (company_id text, checked date, ok boolean, PRIMARY KEY (company_id, checked));
"""

NUM = r"(\d{1,3}(?:[.,\s]\d{3})+|\d{1,6})"
EMP_WORDS = r"(?:Mitarbeiter(?:innen|n)?|Mitarbeitende[n]?|Beschäftigte[n]?|Angestellte[n]?|employees|staff members|people)"
EMP = re.compile(rf"(?:(über|mehr als|rund|ca\.?|circa|etwa|knapp|more than|over|about|around|approximately|nearly)\s+)?"
                 rf"{NUM}\s*\+?\s*(?:engagierte[n]?\s+|motivierte[n]?\s+|qualifizierte[n]?\s+|dedicated\s+|highly\s+qualified\s+)?"
                 rf"{EMP_WORDS}\b", re.I)
GROUP = re.compile(r"weltweit|worldwide|global|gruppe|group|konzern|international", re.I)
YEAR = re.compile(r"\b(19[5-9]\d|20[0-2]\d)\b")
RATIO = re.compile(r"\d\s*(?:in|von|of|out of)\s*$|\d\s*[–-]\s*$", re.I)
JOB = re.compile(r"Aufgaben|Stellenangebot|wir suchen|your tasks", re.I)
NOW = re.compile(r"heute|today|derzeit|currently|aktuell|inzwischen|mittlerweile|\bnow\b", re.I)
TAG = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


def visible(raw: bytes) -> str:
    text = raw.decode("utf-8", "replace")
    return re.sub(r"\s+", " ", html.unescape(TAG.sub(" ", text)))


def employees_from(text: str) -> tuple[int, str, bool] | None:
    best = None
    for m in EMP.finditer(text):
        if m.group(2).startswith("0"):  # numbered steps ("04 Mitarbeitende begleiten …")
            continue
        n = int(re.sub(r"[.,\s]", "", m.group(2)))
        if n < 2 or n > 500_000 or 1900 <= n <= 2030 and not m.group(1):  # years and tiny numbers are noise
            continue
        before = text[max(0, m.start() - 90): m.start()]
        if YEAR.search(before) and not NOW.search(before):  # company history ("2004 … 10 Mitarbeiter") is not today
            continue
        # misreads found by hand on 2026-11-05 (14 companies under 10 employees, 7 wrong): ratios ("1 in 4 people"),
        # ranges in job ads ("ein Team von 2–5 Mitarbeitenden"), decimals ("SEKbn 138,5 Employees"), and a history
        # sentence whose year comes after the figure ("… 3 Mitarbeitern entwickelt und es stand 2003 …")
        if RATIO.search(before) or JOB.search(before[-60:]) or re.search(r"\d[.,]$", before):
            continue
        if YEAR.search(text[m.end(): m.end() + 60]) and not NOW.search(before):
            continue
        ctx = text[max(0, m.start() - 80): m.end() + 40]
        group = bool(GROUP.search(ctx))
        cand = (n, ctx.strip()[:200], group)
        if best is None or (best[2] and not group) or (best[2] == group and n > best[0]):
            best = cand
    return best


def size_class(n: int | None) -> str | None:
    if not n:
        return None
    return "micro" if n < 10 else "small" if n < 50 else "medium" if n < 250 else "large"


def revenue_class(eur: float | None) -> str | None:
    if not eur:
        return None
    return "r1" if eur <= 2e6 else "r2" if eur <= 10e6 else "r3" if eur <= 50e6 else "r4"


def main() -> None:
    conn = connect()
    conn.execute(COLUMNS)
    companies = {r[0]: r for r in conn.execute("SELECT id, srns, lei FROM company").fetchall()}
    website_of = dict(conn.execute("SELECT id, website FROM company").fetchall())
    by_srn = {s: cid for cid, srns, _l in companies.values() for s in (srns or [])}
    up: dict[str, dict] = {cid: {} for cid in companies}

    # devices
    try:
        path = latest_dir("eudamed_devices") / "devices.jsonl"
        recs = [json.loads(l) for l in path.open()]
    except FileNotFoundError:
        recs = []
    agg: dict[str, dict] = {}
    for r in recs:
        cid = by_srn.get(r["srn"])
        if r.get("status") != 200 or not cid:
            continue
        a = agg.setdefault(cid, {"n": 0, "emdn": Counter(), "text": {}, "markets": Counter()})
        a["n"] += r.get("total") or 0
        for d in r.get("details") or []:
            for code, text in d.get("emdn") or []:
                if code:
                    a["emdn"][code] += 1
                    a["text"][code] = text
            a["markets"].update(d.get("markets") or [])
    for cid, a in agg.items():
        codes = [c for c, _n in a["emdn"].most_common(12)]
        # a device list without EMDN codes in the sampled details gives no product groups: NULL, not an empty array
        up[cid].update(device_count=a["n"], emdn=json.dumps([[c, a["text"][c]] for c in codes]) if codes else None,
                       emdn_groups=sorted({c[0] for c in codes}) or None, markets=sorted(a["markets"])[:30] or None)
    for u in up.values():
        if u.get("emdn_groups"):
            u["emdn_source"] = "eudamed"
    print(f"  devices: {len(recs)} SRN records, {len(agg)} companies with device data")

    # product group derived from the own website by the classifier (etl.product_model), only where EUDAMED has none;
    # kept apart by emdn_source = 'website' and shown as derived, with the measured precision
    try:
        pred = json.loads(open("var/product/pred.json").read())
        meta = pred.pop("_meta")
        n_pred = 0
        for cid, p in pred.items():
            if cid in up and not up[cid].get("emdn_groups"):
                up[cid].update(emdn_groups=[p["g"]], emdn_source="website",
                               emdn_evidence=f"{', '.join(p['words'])} · Trefferquote {round(100 * meta['cv_precision'])} % "
                                             f"(Prüfung an {meta['cv_kept']} Firmen mit EUDAMED-Angabe, {meta['date']})")
                n_pred += 1
        print(f"  product groups derived from websites: {n_pred}")
    except FileNotFoundError:
        pass

    # websites
    try:
        wdir = latest_dir("websites")
        sites = [json.loads(l) for l in (wdir / "sites.jsonl").open()]
    except FileNotFoundError:
        sites, wdir = [], None
    day = wdir.name if wdir else None
    n_emp = 0
    for s in sites:
        cid = s["cid"]
        if cid not in up:
            continue
        home = next((p for p in s["pages"] if p.get("kind") == "home"), {})
        ok = isinstance(home.get("status"), int) and home["status"] < 400
        up[cid].update(site_ok=ok if s.get("robots") == "ok" else None, site_checked=day)
        if s.get("contact"):
            up[cid]["contact_url"] = s["contact"]
        best = None
        for p in s["pages"]:
            if p.get("file") and isinstance(p.get("status"), int) and p["status"] < 400:
                found = employees_from(visible(gzip.decompress((wdir / p["file"]).read_bytes())))
                if found and (best is None or (best[2] and not found[2])):
                    best = found
        if best:
            n_emp += 1
            up[cid].update(employees=best[0], employees_evidence=best[1],
                           employees_source="Angabe des Unternehmens auf der eigenen Website" + (" (Gruppe)" if best[2] else ""))
            if not best[2]:
                up[cid]["size_class"] = size_class(best[0])
        conn.execute("INSERT INTO site_check VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (cid, day, ok))
    print(f"  websites: {len(sites)} sites, {sum(1 for u in up.values() if u.get('contact_url'))} contact pages, "
          f"{n_emp} with an employee figure")

    # second pass: imprint (§ 5 DDG, names an e-mail) or contact page found by etl.sources.contact_probe
    try:
        n_probe = 0
        pdir = latest_dir("contact_probe")
        # only pages whose text really is an imprint/contact page (keyword plus e-mail or phone), checked 2026-11-05:
        # 22 of 116 path hits were soft-404s, login pages or script-only pages
        checked = set(json.loads((pdir / "verified_ok.json").read_text())) if (pdir / "verified_ok.json").exists() else set()
        # a contact link from a website reached only through a register e-mail domain counts only if it passed the check
        for cid, u in up.items():
            if u.get("contact_url") and not website_of.get(cid) and cid not in checked:
                u.pop("contact_url")
        for line in (l for name in ("probe.jsonl", "probe2.jsonl") if (pdir / name).exists() for l in (pdir / name).open()):
            r = json.loads(line)
            if (r.get("status") == 200 and r["cid"] in checked and r["cid"] in up
                    and not up[r["cid"]].get("contact_url")):
                up[r["cid"]]["contact_url"] = r["url"]
                n_probe += 1
        print(f"  contact probe: {n_probe} contact or imprint pages added")
    except FileNotFoundError:
        pass

    # strict re-check of every counted contact path (etl.sources.contact_recheck, 2026-11-05): from then on only
    # contact pages that passed it count, and a role mailbox whose domain does not name the company is dropped
    dead_mail: list[str] = []
    try:
        rdir = latest_dir("contact_recheck")
        n_ok = 0
        recs = {}
        for name in ("recheck.jsonl", "recheck2.jsonl"):  # the slower second pass overrides the first
            if (rdir / name).exists():
                recs.update({r["cid"]: r for r in map(json.loads, (rdir / name).open())})
        for r in recs.values():
            if r["cid"] not in up:
                continue
            u = up[r["cid"]]
            u["contact_url"] = r["url"] if r["ok"] and r["kind"] == "contact" else None
            n_ok += r["ok"] and r["kind"] == "contact"
        print(f"  contact recheck: {n_ok} contact or imprint pages passed")
    except FileNotFoundError:
        pass
    # a register role mailbox counts on its own (LB: "mindestens eine E-Mail-Adresse oder ein Verweis auf die
    # Kontaktseite"); it is dropped only when its domain measurably cannot receive mail (etl.sources.mail_mx)
    try:
        dead = {r["domain"] for r in map(json.loads, (latest_dir("mail_mx") / "mx.jsonl").open())
                if r["status"] == "NXDOMAIN" or (r["status"] == "ok" and not r["mx"] and not r["a"])}
        dead_mail = [cid for cid, mail in conn.execute(
            "SELECT id, email_role FROM company WHERE email_role IS NOT NULL").fetchall()
            if mail.split("@")[1].lower() in dead]
        print(f"  mail domains without mail server: {len(dead)} ({len(dead_mail)} companies)")
    except FileNotFoundError:
        pass

    # EMA products
    for cid, names in conn.execute("""
            SELECT m.company_id, array_agg(DISTINCT p) FROM company_member m
            JOIN source_record s USING (source, source_id), jsonb_array_elements_text(coalesce(s.facts->'products', '[]')) p
            WHERE m.source = 'ema' GROUP BY 1""").fetchall():
        up[cid]["products"] = sorted(names)[:20]

    cols = ["contact_url", "site_ok", "site_checked", "employees", "employees_source", "employees_evidence", "size_class",
            "revenue_eur", "revenue_class", "device_count", "emdn", "emdn_groups", "emdn_source", "emdn_evidence",
            "markets", "products"]
    conn.execute("UPDATE company SET " + ", ".join(f"{c} = NULL" for c in cols))
    with conn.cursor() as cur:
        cur.executemany("UPDATE company SET " + ", ".join(f"{c} = %({c})s" for c in cols) + " WHERE id = %(id)s",
                        [{**{c: u.get(c) for c in cols}, "id": cid} for cid, u in up.items() if u])
    if dead_mail:
        conn.execute("UPDATE company SET email_role = NULL WHERE id = ANY(%s)", (dead_mail,))
    # placeholder postcodes from procurement notices ("99999", "00000") are not addresses
    conn.execute("UPDATE company SET post_code = NULL WHERE post_code IN ('00000', '99999')")
    # city names without umlauts ("Munchen", "Muenchen" from GLEIF/EUDAMED) take the umlaut spelling that the
    # registers use for the same name; a city with no umlaut variant in any source stays as it is
    key = "replace(replace(replace(translate(replace(lower({0}), 'ß', 'ss'), 'äöü', 'aou'), 'ae', 'a'), 'oe', 'o'), 'ue', 'u')"
    n_city = conn.execute(f"""
        WITH v AS (SELECT city, count(*) AS n FROM source_record WHERE city ~ '[äöüÄÖÜß]' GROUP BY city),
             k AS (SELECT DISTINCT ON ({key.format('city')}) {key.format('city')} AS k, city FROM v
                   ORDER BY {key.format('city')}, n DESC)
        UPDATE company c SET city = k.city FROM k
        WHERE c.city !~ '[äöüÄÖÜß]' AND {key.format('c.city')} = k.k""").rowcount
    print(f"  cities: {n_city} spelled with umlauts as in the registers")
    groups_de = {"A": "Verabreichung Entnahme", "B": "Hämatologie Transfusion", "C": "Herz Kreislauf Kardiologie",
                 "D": "Desinfektion Sterilisation", "F": "Dialyse", "G": "Gastroenterologie Urologie Endoskopie",
                 "H": "Naht Wundverschluss", "J": "aktive Implantate", "K": "minimalinvasive Chirurgie Endoskopie",
                 "L": "chirurgische Instrumente", "M": "Wundauflagen Verbandmittel", "N": "Nervensystem Neurologie",
                 "P": "Implantate Prothesen", "Q": "Zahnmedizin Dental HNO Augenheilkunde", "R": "Atemwege Anästhesie",
                 "S": "Sterilisation", "T": "Schutz Hilfsmittel", "U": "Urogenital", "V": "Medizinprodukte",
                 "W": "In-vitro-Diagnostik Labor Diagnostika", "Y": "Hilfsmittel Behinderung", "Z": "Medizintechnik Geräte Software"}
    conn.execute("CREATE TEMP TABLE emdn_de (g text, words text)")
    with conn.cursor() as cur:
        cur.executemany("INSERT INTO emdn_de VALUES (%s, %s)", list(groups_de.items()))
    conn.execute("""UPDATE company SET search = search ||
        setweight(to_tsvector('simple', coalesce((SELECT string_agg(e->>1, ' ') FROM jsonb_array_elements(emdn) e), '') || ' ' ||
                                        coalesce(array_to_string(products, ' '), '') || ' ' ||
                                        coalesce((SELECT string_agg(words, ' ') FROM emdn_de WHERE g = ANY(emdn_groups)), '')), 'C')
        WHERE emdn IS NOT NULL OR products IS NOT NULL""")
    print("  enrich:", conn.execute("""SELECT count(*) FILTER (WHERE verified),
        count(*) FILTER (WHERE verified AND (email_role IS NOT NULL OR contact_url IS NOT NULL OR website IS NOT NULL)),
        count(*) FILTER (WHERE verified AND size_class IS NOT NULL), count(*) FILTER (WHERE verified AND emdn_groups IS NOT NULL)
        FROM company""").fetchone(), "(verified, with contact path, with size class, with EMDN)")


if __name__ == "__main__":
    main()
