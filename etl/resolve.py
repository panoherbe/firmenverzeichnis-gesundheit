"""
Build `company` from `source_record` + `gleif_de`. Deterministic, no fuzzy scores (repo rule): records join only on
exact keys. Rebuilds both tables on every run; company ids are stable hashes of an anchor record.

Join keys, strongest first:
  vat            identical DE VAT id
  register+plz2  identical HRB/HRA number and the same 2-digit postcode region (HRB numbers repeat across courts)
  name+plz       identical normalised name and postcode
  name+city      identical normalised name and city
  name+cityhead  identical normalised name and the same first word of the city
  name+plz2      identical normalised name and the same 2-digit postcode region ("Ulm" vs "Ulm-Donautal")
  lei            the record matched the same GLEIF entity (register+plz2, name+plz, name+city; for records without
                 postcode: the name is carried by exactly one active German LEI and the city's first word agrees)
  name_distinct  identical normalised name that is DISTINCTIVE: it has a word outside the generic/surname list below
                 and at most one active German LEI carries it. Needed because procurement notices spell one seat
                 many ways ("Laichingen", "Lachingen", "Laichinigen" for ALIUD PHARMA, measured 2026-11-02).
                 Exact name equality, not a similarity score; wrong merges are measured in the quality sample.
  name_unique    a record WITHOUT location (EMA, consortium members) joins the one located cluster with that name;
                 if several clusters share the name it stays alone (counted in the run report)

Sectors are evidence-based (`sector_evidence` keeps why):
  medtech         EUDAMED manufacturer / procedure-pack producer / authorised rep / importer; or award CPV 331-339
  pharma          EMA marketing-authorisation holder; or award CPV 336x
  (CORDIS EuroSciVoc fields are project-level, not company-level: they gave a research consultancy three wrong
  sectors in sample v1, so they are kept in facts.sector_hints but no longer assign sectors.)
  digital_health  award CPV 4818 (medical software); or name keyword (software, digital, app, KI/AI, telemed ...)
  biotech         name keyword (bio-, therapeutics, antibod-, cell ...) — weakest signal, measured in the sample

Verified = tier A or B, legal person with a German location (postcode or city) AND an official register entry:
active in EUDAMED, EMA marketing-authorisation holder, or an active LEI in GLEIF. Tier C is never verified
(sample v1: 4/10 in scope). (GLEIF alone is a weak backbone: measured 2026-11-02,
Hexal AG, Aliud Pharma, 1 A Pharma, Eisai GmbH carry no active German LEI.)

Tier: A = manufacturer / MA holder (EUDAMED MF/PR, EMA MAH); B = EUDAMED authorised rep / importer only;
C = public-sector supplier or EU health-research participant only (public procurement awards, CORDIS).
CORDIS participants count as verified: the Commission validates every participant as a legal entity.

    python -m etl.resolve
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict

from psycopg.types.json import Jsonb

from etl.common import norm
from etl.common.db import connect

SOURCE_RANK = {"eudamed": 0, "corpus": 1, "cordis": 2, "ema": 3}

DIGITAL = re.compile(r"\b(software|digital|digit|app|apps|health ?tech|e-?health|telemed|telehealth|ki|ai|"
                     r"data|daten|cloud|informatik|it[- ]solutions?|it[- ]services?|systems?)\b|\.ai$", re.I)
# Narrow on purpose: a bare "bio" prefix tagged blood-gas analyser makers and incubators as biotech (sample v1).
BIOTECH = re.compile(r"biotech|therapeutics|biologics|bioscience|biopharm|genomic|antibod|antikörper|vakzin|vaccin|"
                     r"zelltherap|cell therap|peptide|biologika|gentherap|gene therap", re.I)
MEDTECH_CPV = tuple(f"33{i}" for i in "1234789")   # 331x-334x, 337x-339x: devices, equipment, consumables
PHARMA_CPV = ("336",)


GENERIC_WORDS = set("""
medical medizin medizintechnik medizintechnische medizinische medtech pharma pharmaceutical pharmaceuticals
pharmazeutische arzneimittel health healthcare gesundheit care service services dienst dienste handel handels
vertrieb vertriebs technik technologie technologies technology systems system solutions dental labor laboratory
diagnostics diagnostik orthopaedie orthopedics sanitaetshaus reha rehatechnik deutschland germany international
europe euro group gruppe holding management consulting beratung logistik logistics hospital krankenhaus klinik
clinic produkte products produktions production supply partner partners plus pro med medi tech bio life science
sciences hygiene gesellschaft fuer und the of de der die das bau hilfsmittel praxisbedarf apparatebau geraete
instrumente instruments optik optics labs lab
mueller schmidt schneider fischer weber meyer meier wagner becker schulz hoffmann schaefer koch bauer richter
klein wolf schroeder neumann schwarz zimmermann braun krueger hofmann hartmann lange schmitt werner schmitz krause
meier lehmann schmid schulze maier koehler herrmann koenig walter mayer huber kaiser fuchs peters lang scholz
moeller weiss jung hahn schubert vogel friedrich keller guenther frank berger winkler roth beck lorenz baumann
franke albrecht schuster simon ludwig boehm winter kraus martin schumacher kraemer vogt stein jaeger otto sommer
gross seidel heinrich brandt haas schreiber graf schulte dietrich ziegler kuhn kuehn pohl engel horn busch bergmann
thomas voigt sauer arnold wolff pfeiffer dr
""".split())


def distinctive(name_norm: str) -> bool:
    words = re.findall(r"[a-z0-9]+", name_norm)
    return any(len(w) >= 3 and w not in GENERIC_WORDS and not w.isdigit() for w in words)


def city_head(city: str) -> str:
    words = re.findall(r"[a-z]+", norm.fold(city))
    return words[0] if words else ""


class UF:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def fix_case(name: str) -> str:
    """EMA writes 'Ratiopharm Gmbh'; restore the usual legal-form casing."""
    for bad, good in [("Gmbh", "GmbH"), ("Mbh", "mbH"), ("Kgaa", "KGaA"), (" Ag", " AG"), (" Kg", " KG"),
                      (" Se", " SE"), ("& Co.Kg", "& Co. KG"), (" Ohg", " OHG"), (" Ug", " UG")]:
        name = re.sub(re.escape(bad) + r"\b", good, name)
    return name


def main() -> None:
    conn = connect()
    recs = conn.execute("""
        SELECT source, source_id, name, name_norm, legal_form, street, post_code, city, website, domain,
               email_role, vat, register_no, srn, roles, active, facts
        FROM source_record
        WHERE NOT natural_person
          AND (source <> 'eudamed' OR active)
          AND (source <> 'corpus' OR legal_form IS NOT NULL)
    """).fetchall()
    cols = ["source", "source_id", "name", "name_norm", "legal_form", "street", "post_code", "city", "website",
            "domain", "email_role", "vat", "register_no", "srn", "roles", "active", "facts"]
    recs = [dict(zip(cols, r)) for r in recs]
    key = lambda r: f"{r['source']}:{r['source_id']}"  # noqa: E731

    # GLEIF lookups
    g_by_reg, g_by_name_plz, g_by_name_city, g_by_name = {}, {}, {}, defaultdict(list)
    plz_region: dict[str, Counter] = defaultdict(Counter)
    for lei, name, nn, reg, street, plz, city, region in conn.execute(
            "SELECT lei, name, name_norm, register_no, street, post_code, city, region FROM gleif_de"):
        g = {"lei": lei, "name": name, "street": street, "post_code": plz, "city": city, "region": region}
        if reg and plz:
            g_by_reg.setdefault((reg, plz[:2]), g)
        if plz:
            g_by_name_plz.setdefault((nn, plz), g)
            if region:
                plz_region[plz][region] += 1
        if city:
            g_by_name_city.setdefault((nn, norm.fold(city).strip()), g)
        g_by_name[nn].append(g)

    def gleif_match(r):
        if r["register_no"] and r["post_code"] and (r["register_no"], r["post_code"][:2]) in g_by_reg:
            return g_by_reg[(r["register_no"], r["post_code"][:2])], "register+plz2"
        if r["post_code"] and (r["name_norm"], r["post_code"]) in g_by_name_plz:
            return g_by_name_plz[(r["name_norm"], r["post_code"])], "name+plz"
        if r["city"] and (r["name_norm"], norm.fold(r["city"]).strip()) in g_by_name_city:
            return g_by_name_city[(r["name_norm"], norm.fold(r["city"]).strip())], "name+city"
        cands = g_by_name.get(r["name_norm"], [])
        if not r["post_code"] and len(cands) == 1:
            # the name is carried by exactly one active German LEI; a stated city must agree on its first word
            # ("Ulm-Donautal" ~ "Ulm"), compared exactly
            if not r["city"] or city_head(r["city"]) == city_head(cands[0]["city"] or ""):
                return cands[0], "name_unique"
        return None, None

    gleif_of: dict[str, tuple] = {}
    for r in recs:
        g, how = gleif_match(r)
        if g:
            gleif_of[key(r)] = (g, how)

    uf = UF()
    rule: dict[str, str] = {}
    index: dict[tuple, str] = {}

    def join(r, kind, value):
        if not value:
            return
        k = (kind, value)
        if k in index:
            if uf.find(index[k]) != uf.find(key(r)):
                uf.union(index[k], key(r))
                rule.setdefault(key(r), kind)
        else:
            index[k] = key(r)

    for r in recs:
        uf.find(key(r))
        city = norm.fold(r["city"]).strip() if r["city"] else None
        join(r, "vat", r["vat"])
        if r["register_no"] and r["post_code"]:
            join(r, "register+plz2", (r["register_no"], r["post_code"][:2]))
        if r["post_code"]:
            join(r, "name+plz", (r["name_norm"], r["post_code"]))
        if city:
            join(r, "name+city", (r["name_norm"], city))
            join(r, "name+cityhead", (r["name_norm"], city_head(city)))
        if r["post_code"]:
            join(r, "name+plz2", (r["name_norm"], r["post_code"][:2]))
        if key(r) in gleif_of:
            join(r, "lei", gleif_of[key(r)][0]["lei"])
        if r["legal_form"] and distinctive(r["name_norm"]) and len(g_by_name.get(r["name_norm"], [])) <= 1:
            join(r, "name_distinct", r["name_norm"])

    # name_unique: location-less records join the single located cluster carrying that name
    located: dict[str, set] = defaultdict(set)
    for r in recs:
        if r["post_code"] or r["city"]:
            located[r["name_norm"]].add(uf.find(key(r)))
    ambiguous = 0
    for r in recs:
        if r["post_code"] or r["city"]:
            continue
        roots = {uf.find(x) for x in located.get(r["name_norm"], set())}
        if len(roots) == 1:
            uf.union(next(iter(roots)), key(r))
            rule.setdefault(key(r), "name_unique")
        elif len(roots) > 1:
            ambiguous += 1
    # location-less records sharing a name (EMA + consortium member) form one cluster together
    for r in recs:
        if not (r["post_code"] or r["city"]) and len(located.get(r["name_norm"], ())) == 0:
            join(r, "name_only", r["name_norm"])

    clusters: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        clusters[uf.find(key(r))].append(r)

    companies, members = [], []
    stats = Counter()
    for root, rs in clusters.items():
        rs.sort(key=lambda r: (SOURCE_RANK[r["source"]], r["source_id"]))
        anchor = rs[0]
        first = lambda f: next((r[f] for r in rs if r[f]), None)  # noqa: E731
        located_rs = [r for r in rs if r["post_code"]]
        if located_rs:
            addr = located_rs[0]
        else:  # most frequent city wins over typos
            cities = Counter(r["city"] for r in rs if r["city"])
            top = cities.most_common(1)[0][0] if cities else None
            addr = next((r for r in rs if r["city"] == top), anchor)

        gleif, gleif_rule = next((gleif_of[key(r)] for r in rs if key(r) in gleif_of), (None, None))
        if gleif:
            stats[f"gleif_{gleif_rule}"] += 1

        sources = sorted({r["source"] for r in rs} | ({"gleif"} if gleif else set()))
        roles = sorted({x for r in rs for x in r["roles"]})
        eud_roles = {x for r in rs if r["source"] == "eudamed" for x in r["roles"]}
        cpv4 = {c for r in rs if r["source"] == "corpus" for c in r["facts"].get("cpv4", [])}
        names = " ".join(r["name"] for r in rs)

        ev: dict[str, list[str]] = defaultdict(list)
        if eud_roles:
            ev["medtech"].append("EUDAMED " + "/".join(sorted(eud_roles)))
        if any(c.startswith(MEDTECH_CPV) for c in cpv4):
            ev["medtech"].append("Aufträge CPV 331-339")
        if "MAH" in roles:
            ev["pharma"].append("EMA Zulassungsinhaber")
        if any(c.startswith(PHARMA_CPV) for c in cpv4):
            ev["pharma"].append("Aufträge CPV 336")
        if any(c.startswith("4818") for c in cpv4):
            ev["digital_health"].append("Aufträge CPV 4818 (Medizinsoftware)")
        if DIGITAL.search(names) or (anchor["domain"] or "").endswith(".ai"):
            ev["digital_health"].append("Name")
        if BIOTECH.search(names):
            ev["biotech"].append("Name")
        sectors = sorted(ev)

        post_code = addr["post_code"] or (gleif or {}).get("post_code")
        legal = first("legal_form") or norm.legal_form((gleif or {}).get("name") or "")
        hrb = next((r["register_no"] for r in rs if (r["register_no"] or "").startswith("HRB")), None)
        if not legal and hrb:
            legal = "Kapitalgesellschaft (HRB)"
        if not legal:
            # no legal form and no HRB entry: possibly a natural person (sole trader, dental technician ...).
            # GDPR: such clusters never enter `company`, not even unverified.
            stats["excluded_no_legal_form"] += 1
            continue
        region = (gleif or {}).get("region")
        if not region and post_code and plz_region.get(post_code):
            region = plz_region[post_code].most_common(1)[0][0]
        verified_by = [s for s, ok in [("EUDAMED", bool(eud_roles)), ("EMA", "MAH" in roles),
                                       ("GLEIF", bool(gleif)), ("EU-validiert (CORDIS)", "RND" in roles)] if ok]
        city = addr["city"] or (gleif or {}).get("city")
        tier = "A" if (eud_roles & {"MF", "PR"} or "MAH" in roles) else "B" if eud_roles else "C"
        # Tier C is never "verified": sample v1 found only 4/10 tier-C firms in the health industry (hospitals,
        # IT resellers with one medical lot). They stay searchable as candidates.
        verified = bool(legal and (post_code or city) and verified_by and tier in ("A", "B"))

        corpus_facts = [r["facts"] for r in rs if r["source"] == "corpus"]
        awards = sum(f.get("notices", 0) for f in corpus_facts)
        last = max((f.get("last_award") or "" for f in corpus_facts), default="") or None

        cid = "c" + hashlib.sha1(key(anchor).encode()).hexdigest()[:12]
        name = anchor["name"]
        if anchor["source"] == "ema" or name.isupper():  # EMA title case, CORDIS/GLEIF all caps
            name = fix_case(name.title() if name.isupper() else name)
        companies.append({
            "id": cid, "name": name, "legal_form": legal,
            "street": addr["street"] or (gleif or {}).get("street"),
            "post_code": post_code, "city": city, "region": region, "tier": tier,
            "website": first("website"), "domain": first("domain"), "email_role": first("email_role"),
            "vat": first("vat"), "register_no": first("register_no"), "lei": (gleif or {}).get("lei"),
            "srns": sorted({r["srn"] for r in rs if r["srn"]}), "sectors": sectors,
            "sector_evidence": Jsonb(ev), "roles": roles, "sources": sources,
            "public_awards": awards, "public_buyers": sum(f.get("buyers", 0) for f in corpus_facts),
            "public_last_award": last, "verified": verified,
            "verified_reason": ("Register: " + ", ".join(verified_by)) if verified else None,
        })
        for r in rs:
            members.append((cid, r["source"], r["source_id"], rule.get(key(r), "anchor" if r is anchor else "transitive")))

    ccols = list(companies[0])
    with conn.transaction():
        conn.execute("TRUNCATE company, company_member")
        with conn.cursor().copy(f"COPY company ({', '.join(ccols)}) FROM STDIN") as cp:
            for c in companies:
                cp.write_row([c[k] for k in ccols])
        with conn.cursor().copy("COPY company_member (company_id, source, source_id, rule) FROM STDIN") as cp:
            for m in members:
                cp.write_row(m)
        conn.execute("""
            UPDATE company SET search =
              setweight(to_tsvector('simple', coalesce(name, '')), 'A') ||
              setweight(to_tsvector('german', coalesce(name, '')), 'A') ||
              setweight(to_tsvector('simple', coalesce(city, '') || ' ' || coalesce(post_code, '') || ' ' ||
                        coalesce(region, '') || ' ' || coalesce(domain, '')), 'B') ||
              setweight(to_tsvector('simple', array_to_string(sectors, ' ') || ' ' ||
                        array_to_string(srns, ' ')), 'C')
        """)
    conn.execute("ANALYZE company")
    n_ver = Counter((c["tier"], c["verified"]) for c in companies)
    print(f"records {len(recs)} -> companies {len(companies)}, verified {n_ver}; "
          f"name_unique ambiguous skipped {ambiguous}; {dict(stats)}")
    print("join rules:", Counter(m[3] for m in members))


if __name__ == "__main__":
    main()
