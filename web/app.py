"""
Search UI, statistics, JSON API and exports over the `company` table, in German and English.

    .venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8601

Routes: / (search; ?embed=1 drops the page chrome for iframes) · /firma/{id} · /statistik · /hilfe · /quellen ·
/api/v1/companies · /api/v1/companies/{id} · /api/v1/stats · /export.csv · /export.xlsx · /export.pdf ·
/statistik.pdf · /api/health · plus the routers in web/auth.py, web/matchmaking.py, web/support.py.
Iframe embedding is controlled by the CSP header `frame-ancestors`, from env FRAME_ANCESTORS (default "'self'");
set it to the buyer's origin, e.g. "https://www.example.org". No X-Frame-Options header is sent.
Corporate design: env THEME_FILE points at a JSON file {"name", "logo", "accent", "fg", "bg", "soft", "font"}
(see web/themes/); `?theme=<name>` previews a theme from web/themes/ without changing the default.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import unquote_plus, urlencode

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from markupsafe import Markup

from web import auth, demo, gate, i18n_app, pdf
from web.common import LANG, _, db, lang, pick_lang, register, templates
from web.query import parse

FRAME_ANCESTORS = os.environ.get("FRAME_ANCESTORS", "'self'")
THEMES = Path(__file__).parent / "themes"

SECTORS = {"medtech": "Medizintechnik", "pharma": "Pharma", "biotech": "Biotechnologie",
           "digital_health": "Digitale Gesundheit"}
TIERS = {"A": "Hersteller / Zulassungsinhaber", "B": "Bevollmächtigte / Importeure",
         "C": "Lieferanten öffentlicher Auftraggeber"}
SIZES = {"micro": "Kleinst (< 10 Beschäftigte)", "small": "Klein (10–49)", "medium": "Mittel (50–249)",
         "large": "Groß (ab 250)"}
REVENUES = {"r1": "bis 2 Mio. €", "r2": "2–10 Mio. €", "r3": "10–50 Mio. €", "r4": "über 50 Mio. €"}
# revenue classes come from licensed data that is switched on at contract start; until then filter and field are shown
# locked, with no values anywhere (page, export, API). REVENUE_UNLOCKED=1 once the licensed data are loaded.
REVENUE_LOCKED = os.environ.get("REVENUE_UNLOCKED") != "1"
EMDN = {"A": "Verabreichung, Entnahme, Sammlung", "B": "Hämatologie und Transfusion", "C": "Herz-Kreislauf",
        "D": "Desinfektion und Sterilisation", "F": "Dialyse", "G": "Gastroenterologie und Urologie",
        "H": "Naht- und Wundversorgung", "J": "Aktive Implantate", "K": "Minimalinvasive Chirurgie",
        "L": "Chirurgische Instrumente (wiederverwendbar)", "M": "Wundauflagen und Verbandmittel",
        "N": "Nervensystem", "P": "Implantate", "Q": "Zahnmedizin, HNO, Augenheilkunde",
        "R": "Atemwege und Anästhesie", "S": "Sterilisationsbedarf", "T": "Schutz- und Hilfsmittel für Patienten",
        "U": "Urogenitalsystem", "V": "Sonstige Medizinprodukte", "W": "In-vitro-Diagnostika",
        "X": "Produkte ohne medizinische Zweckbestimmung", "Y": "Hilfsmittel für Menschen mit Behinderung",
        "Z": "Medizintechnische Geräte und Software"}
REGIONS = {"DE-BW": "Baden-Württemberg", "DE-BY": "Bayern", "DE-BE": "Berlin", "DE-BB": "Brandenburg",
           "DE-HB": "Bremen", "DE-HH": "Hamburg", "DE-HE": "Hessen", "DE-MV": "Mecklenburg-Vorpommern",
           "DE-NI": "Niedersachsen", "DE-NW": "Nordrhein-Westfalen", "DE-RP": "Rheinland-Pfalz",
           "DE-SL": "Saarland", "DE-SN": "Sachsen", "DE-ST": "Sachsen-Anhalt", "DE-SH": "Schleswig-Holstein",
           "DE-TH": "Thüringen"}
EXPORT_FIELDS = ["id", "name", "legal_form", "street", "post_code", "city", "region", "website", "contact_url",
                 "email_role", "vat", "register_no", "lei", "srns", "sectors", "emdn_groups", "emdn_source", "size_class",
                 "employees", "tier", "public_awards", "public_last_award", "verified", "sources"]
PUBLIC_FIELDS = EXPORT_FIELDS + ["sector_evidence", "roles", "verified_reason", "public_buyers", "emdn",
                                 "device_count", "markets", "employees_source", "revenue_class"]
FILTERS = ("q", "sector", "tier", "region", "size", "revenue", "product", "contact", "alle")

APP_EN = {
    # navigation, search
    "Firmenverzeichnis Gesundheitswirtschaft": "Directory of the German Health Industry",
    "Suche": "Search", "Statistik": "Statistics", "Hilfe": "Help", "Quellen & Lizenzen": "Sources & licences",
    "Matchmaking": "Matchmaking", "Support": "Support", "API": "API",
    "Suchbegriff": "Search term", "Branche": "Sector", "Rolle": "Role", "Bundesland": "Federal state",
    "Größe (Beschäftigte)": "Size (employees)", "Umsatz": "Revenue",
    "wird zum Vertragsstart freigeschaltet": "switched on at contract start", "Produktgruppe (EMDN)": "Product group (EMDN)",
    "abgeleitet aus der eigenen Website": "derived from the company's own website", "Umsatzklassen: wird zum Vertragsstart freigeschaltet.": "Revenue classes: switched on at contract start.", "Umsatzklasse": "Revenue class", "Produktgruppe": "Product group",
    "alle": "all", "Suchen": "Search", "Zurücksetzen": "Reset", "Unternehmen": "Companies",
    "Ort": "Location", "Öff. Aufträge": "Public contracts", "Keine Treffer.": "No results.",
    "auch nicht registergeprüfte": "include not register-verified", "nur mit Kontaktweg": "only with contact path",
    "Seite": "Page", "von": "of", "zurück": "back", "weiter": "next", "Weitere Filter": "More filters",
    "z. B. Endoskop UND Tuttlingen": "e.g. endoscope AND Tuttlingen",
    "Suchhilfe": "Search help", "Gespeicherte Suchen": "Saved searches", "Suche speichern": "Save search",
    "Anmelden": "Sign in", "Abmelden": "Sign out", "Mein Konto": "My account", "Nutzerverwaltung": "User management",
    "Sortierung": "Sort", "Relevanz": "Relevance", "Name": "Name", "Öffentliche Aufträge": "Public contracts",
    "Export": "Export", "Treffer als": "Results as", "Ihre letzten Suchen": "Your recent searches",
    "Filter": "Filters", "Kontakt": "Contact", "Kontaktseite": "Contact page",
    # sectors, tiers, sizes
    "Medizintechnik": "Medical technology", "Pharma": "Pharmaceuticals", "Biotechnologie": "Biotechnology",
    "Digitale Gesundheit": "Digital health", "Hersteller / Zulassungsinhaber": "Manufacturers / marketing authorisation holders",
    "Bevollmächtigte / Importeure": "Authorised representatives / importers",
    "Lieferanten öffentlicher Auftraggeber": "Suppliers to public buyers",
    "Kleinst (< 10 Beschäftigte)": "Micro (< 10 employees)", "Klein (10–49)": "Small (10–49)",
    "Mittel (50–249)": "Medium (50–249)", "Groß (ab 250)": "Large (250+)",
    "bis 2 Mio. €": "up to €2m", "2–10 Mio. €": "€2–10m", "10–50 Mio. €": "€10–50m", "über 50 Mio. €": "over €50m",
    "Verabreichung, Entnahme, Sammlung": "Administration, withdrawal and collection", "Hämatologie und Transfusion": "Haematology and transfusion",
    "Herz-Kreislauf": "Cardiocirculatory system", "Desinfektion und Sterilisation": "Disinfection and sterilisation",
    "Dialyse": "Dialysis", "Gastroenterologie und Urologie": "Gastrointestinal and urological",
    "Naht- und Wundversorgung": "Suture and wound closure", "Aktive Implantate": "Active implantable devices",
    "Minimalinvasive Chirurgie": "Minimally invasive surgery", "Chirurgische Instrumente (wiederverwendbar)": "Reusable surgical instruments",
    "Wundauflagen und Verbandmittel": "Wound dressings", "Nervensystem": "Nervous system",
    "Implantate": "Implantable prosthetic devices", "Zahnmedizin, HNO, Augenheilkunde": "Dental, ENT and ophthalmic",
    "Atemwege und Anästhesie": "Respiratory and anaesthesia", "Sterilisationsbedarf": "Sterilisation supplies",
    "Schutz- und Hilfsmittel für Patienten": "Patient protection and aids", "Urogenitalsystem": "Urogenital system",
    "Sonstige Medizinprodukte": "Other medical devices", "In-vitro-Diagnostika": "In vitro diagnostics",
    "Produkte ohne medizinische Zweckbestimmung": "Products without a medical purpose",
    "Hilfsmittel für Menschen mit Behinderung": "Aids for persons with disabilities",
    "Medizintechnische Geräte und Software": "Medical equipment and software",
    # profile
    "zur Suche": "back to search", "Rechtsform": "Legal form", "Anschrift": "Address", "Website": "Website",
    "E-Mail (Funktionspostfach)": "E-mail (company mailbox)", "USt-IdNr.": "VAT ID", "Handelsregister": "Trade register",
    "Produkte (EMDN)": "Products (EMDN)", "Medizinprodukte in EUDAMED": "Medical devices in EUDAMED",
    "erhältlich in": "available in", "Beschäftigte": "Employees", "Größenklasse": "Size class",
    "Bekanntmachungen": "notices", "Auftraggeber": "buyers", "zuletzt": "latest",
    "Begründung Branche": "Why this sector", "Herkunft der Daten": "Data provenance", "Quelle": "Source",
    "Eintrag": "Record", "Verknüpft über": "Linked by", "Lizenz": "Licence", "nicht registergeprüft": "not register-verified",
    "registergeprüft": "register-verified", "Ähnliche Unternehmen": "Similar companies", "Profil": "Profile",
    "Fehler gefunden oder Eintrag entfernen?": "Found an error or want the entry removed?", "Melden": "Report",
    "Angabe des Unternehmens auf der eigenen Website": "stated by the company on its own website",
    "Kontaktweg": "Contact path", "Daten exportieren": "Export data", "Profil als PDF": "Profile as PDF",
    "Unternehmen im Verzeichnis": "companies in the directory",
    "Unternehmen der deutschen Gesundheitswirtschaft aus amtlichen Registern": "Companies of the German health industry from official registers",
    "Im Verzeichnis keine personenbezogenen Daten.": "No personal data in the directory.", "Datenschutz": "Privacy", "Prototyp": "Prototype",
    # statistics
    "nach Branche": "by sector", "nach Bundesland": "by federal state", "nach Größenklasse": "by size class",
    "nach Produktgruppe (Medizintechnik)": "by product group (medical technology)",
    "Grafik als JPG": "Chart as JPG", "Grafik als PNG": "Chart as PNG", "Statistik als PDF": "Statistics as PDF",
    "Tabelle als CSV": "Table as CSV", "Die Statistik folgt den gewählten Filtern.": "The statistics follow the chosen filters.",
    "unbekannt": "unknown", "Anzahl": "Count", "Zum Inhalt": "Skip to content", "Hauptnavigation": "Main navigation",
    "und öffentlichen Auftragsbekanntmachungen.": "and public procurement notices.", "Status": "Status",
    "Beispiel-Kundendesign": "Example corporate design",
}


def theme(request: Request) -> dict:
    name = request.query_params.get("theme") or request.cookies.get("theme")
    path = (THEMES / f"{name}.json") if name and name.isidentifier() else Path(os.environ.get("THEME_FILE", THEMES / "default.json"))
    # closed demo: visitors with an access link see the buyer's corporate-design preview (file only on that server)
    if f := gate.theme_file(request):
        path = Path(f)
    try:
        th = json.loads(path.read_text())
    except (OSError, ValueError):
        th = json.loads((THEMES / "default.json").read_text())
    base = json.loads((THEMES / "default.json").read_text())
    # values go into a <style> block unescaped; "provider" is the copyright/provider note in the footer: allow only characters CSS values need
    return {k: Markup(re.sub(r"[^#\w\s,.\-\"'()%/:]", "", str(th.get(k, v)))) for k, v in base.items()}


app = FastAPI(
    title="German Health Industry Directory – API",
    description=("Read-only REST API of the directory of German health-industry companies (medical technology, "
                 "pharmaceuticals, biotechnology, digital health). Data come from official registers (EUDAMED, EMA, "
                 "GLEIF, CORDIS) and public procurement notices; the directory holds legal entities only, no personal data. Every record "
                 "lists its sources. Search syntax: AND/OR/NOT, \"phrases\", prefix*. Licence: see /quellen."),
    version="1.0", docs_url=None, redoc_url=None)  # own /api/docs page: Swagger UI would load from a CDN
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
if (Path(__file__).parent.parent / "medien").is_dir():  # walkthrough video and screenshots (help page)
    app.mount("/medien", StaticFiles(directory=str(Path(__file__).parent.parent / "medien")), name="medien")
templates.env.globals.update(SECTORS=SECTORS, TIERS=TIERS, REGIONS=REGIONS, SIZES=SIZES, REVENUES=REVENUES, EMDN=EMDN,
                             REVENUE_LOCKED=REVENUE_LOCKED)
templates.env.globals["current_user"] = auth.current_user
templates.env.globals["theme"] = theme
templates.env.filters["urldecode"] = unquote_plus
RULE_LABELS = {"anchor": "Registereintrag", "transitive": "über verknüpften Eintrag", "vat": "USt-IdNr.",
               "name+city": "Name und Ort", "name+plz": "Name und PLZ", "name+plz2": "Name und PLZ-Region",
               "name+cityhead": "Name und Ort", "register+plz2": "Handelsregister und PLZ-Region", "lei": "LEI",
               "name_distinct": "eindeutiger Name", "name_unique": "eindeutiger Name", "name_only": "Name"}
templates.env.filters["rule_label"] = lambda r: ", ".join(dict.fromkeys(_(RULE_LABELS.get(x.strip(), x.strip())) for x in (r or "").split(",")))
templates.env.filters["qs_with"] = lambda params, **kw: urlencode({**dict(params), **kw})
app.include_router(auth.router)
for mod in ("matchmaking", "support", "demo", "gate"):  # optional modules; the app runs without them
    try:
        app.include_router(__import__(f"web.{mod}", fromlist=["router"]).router)
    except ImportError:
        pass
APP_EN.update(i18n_app.EN)
register(APP_EN)  # after the modules, so the directory's own wording wins


@app.middleware("http")
async def frame_policy(request: Request, call_next):
    t0 = time.monotonic()
    lng = pick_lang(request.query_params.get("lang"), request.cookies.get("lang"), request.headers.get("accept-language"))
    LANG.set(lng)
    if gate.rate_limited(request):
        resp = HTMLResponse(f"<p>{_('Zu viele Anfragen. Bitte kurz warten.')}</p>", status_code=429)
    elif gate.needs_access(request):  # closed demo: strangers get the neutral start page only
        if request.url.path == "/":
            resp = gate.landing(request)
        elif request.url.path.startswith(("/api/", "/openapi.json", "/export")):
            resp = JSONResponse({"detail": "access required"}, status_code=401)
        else:
            resp = RedirectResponse("/", status_code=303)
    elif demo.blocked(request):  # demo accounts on a demo installation: no user management, logs, support admin
        resp = HTMLResponse(f"<p>{_('In der Demo gesperrt.')}</p><p><a href='/'>←</a></p>", status_code=403)
    else:
        resp = await call_next(request)
    if request.query_params.get("lang") in ("de", "en"):
        resp.set_cookie("lang", lng, max_age=365 * 86400, samesite="lax")
    if request.query_params.get("theme"):
        resp.set_cookie("theme", request.query_params["theme"], max_age=86400, samesite="lax")
    resp.headers["Content-Security-Policy"] = f"frame-ancestors {FRAME_ANCESTORS}"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Content-Language"] = lng
    if not request.url.path.startswith("/static"):
        try:
            auth.log_access(request, resp.status_code, int((time.monotonic() - t0) * 1000))
        except Exception:  # noqa: BLE001  logging must never take the site down
            pass
    return resp


# ---------- search ----------

def where(q=None, sector=None, tier=None, region=None, size=None, revenue=None, product=None, contact=False,
          verified=True):
    clauses, params = ["hidden_reason IS NULL"], {}
    if verified:
        clauses.append("verified")
    if q:
        tsq, plain = parse(q)
        if tsq:
            params["tsq"] = tsq
            c = "search @@ to_tsquery('simple', %(tsq)s)"
            if plain:
                c = f"({c} OR name ILIKE %(like)s)"
                params["like"] = f"%{' '.join(plain)}%"
            clauses.append(c)
    if sector in SECTORS:
        clauses.append("%(sector)s = ANY(sectors)")
        params["sector"] = sector
    if tier in TIERS:
        clauses.append("tier = %(tier)s")
        params["tier"] = tier
    if region in REGIONS:
        clauses.append("region = %(region)s")
        params["region"] = region
    if size in SIZES:
        clauses.append("size_class = %(size)s")
        params["size"] = size
    if revenue in REVENUES and not REVENUE_LOCKED:
        clauses.append("revenue_class = %(revenue)s")
        params["revenue"] = revenue
    if product in EMDN:
        clauses.append("%(product)s = ANY(emdn_groups)")
        params["product"] = product
    if contact:
        clauses.append("(email_role IS NOT NULL OR contact_url IS NOT NULL)")  # a bare website is no contact path
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


ORDERS = {"rel": None, "name": "name", "awards": "public_awards DESC, name"}


def search(f: dict, limit: int, offset: int, fields: str = "*", order: str = "rel"):
    w, params = where(**f)
    by = ORDERS.get(order) or (("ts_rank(search, to_tsquery('simple', %(tsq)s)) DESC, " if "tsq" in params else "")
                               + "tier, public_awards DESC, name")
    with db() as conn:
        total = conn.execute(f"SELECT count(*) AS n FROM company{w}", params).fetchone()["n"]
        rows = conn.execute(f"SELECT {fields} FROM company{w} ORDER BY {by} LIMIT %(limit)s OFFSET %(offset)s",
                            {**params, "limit": limit, "offset": offset}).fetchall()
        facets = conn.execute(f"""
            SELECT 'sector' AS kind, s AS value, count(*) AS n FROM company, unnest(sectors) s{w} GROUP BY s
            UNION ALL SELECT 'tier', tier, count(*) FROM company{w} GROUP BY tier
            UNION ALL SELECT 'region', region, count(*) FROM company{w} GROUP BY region
            UNION ALL SELECT 'size', size_class, count(*) FROM company{w} GROUP BY size_class
            UNION ALL SELECT 'revenue', revenue_class, count(*) FROM company{w} GROUP BY revenue_class
            UNION ALL SELECT 'product', g, count(*) FROM company, unnest(emdn_groups) g{w} GROUP BY g""",
                              params).fetchall()
    fc: dict[str, dict] = {k: {} for k in ("sector", "tier", "region", "size", "revenue", "product")}
    for x in facets:
        if x["value"]:
            fc[x["kind"]][x["value"]] = x["n"]
    if REVENUE_LOCKED:
        fc.pop("revenue")
        for r in rows:
            r.pop("revenue_class", None)
    return total, rows, fc


def filters(q="", sector="", tier="", region="", size="", revenue="", product="", contact=False, alle=False) -> dict:
    return dict(q=q.strip() or None, sector=sector or None, tier=tier or None, region=region or None, size=size or None,
                revenue=revenue or None, product=product or None, contact=contact, verified=not alle)


def query_string(request: Request, **override) -> str:
    p = {k: v for k, v in request.query_params.items() if k in FILTERS + ("embed", "sort") and v}
    p.update({k: v for k, v in override.items() if v is not None})
    return urlencode({k: v for k, v in p.items() if v != ""})


@app.get("/", response_class=HTMLResponse)
def index(request: Request, q: str = "", sector: str = "", tier: str = "", region: str = "", size: str = "",
          revenue: str = "", product: str = "", contact: bool = False, alle: bool = False, sort: str = "rel",
          page: int = Query(1, ge=1), embed: bool = False):
    f = filters(q, sector, tier, region, size, revenue, product, contact, alle)
    total, rows, facets = search(f, limit=25, offset=(page - 1) * 25, order=sort)
    qs = query_string(request, page="")
    if any(v for k, v in request.query_params.items() if k in FILTERS):
        auth.record_search(request, qs, total)
    user = auth.current_user(request)
    recent = []
    if user:
        with db() as conn:
            recent = conn.execute("SELECT name, query FROM saved_search WHERE user_id = %s ORDER BY id DESC LIMIT 6",
                                  (user["id"],)).fetchall()
    return templates.TemplateResponse(request, "index.html", dict(
        rows=rows, total=total, facets=facets, page=page, pages=max(1, (total + 24) // 25), embed=embed, qs=qs,
        q=q, sector=sector, tier=tier, region=region, size=size, revenue=revenue, product=product, contact=contact,
        alle=alle, sort=sort, recent=recent, more=bool(size or revenue or product or tier or contact or alle)))


def load_company(cid: str):
    with db() as conn:
        c = conn.execute("SELECT * FROM company WHERE id = %s AND hidden_reason IS NULL", (cid,)).fetchone()
        if not c:
            raise HTTPException(404)
        members = conn.execute("""
            SELECT m.source, string_agg(DISTINCT m.rule, ', ') AS rule, s.name, s.city, s.srn, src.title,
                   src.licence, count(*) AS n, min(s.source_id) AS source_id
            FROM company_member m JOIN source_record s USING (source, source_id) JOIN source src ON src.id = m.source
            WHERE m.company_id = %s
            GROUP BY m.source, s.name, s.city, s.srn, src.title, src.licence
            ORDER BY m.source, n DESC, s.name""", (cid,)).fetchall()
        similar = conn.execute("""
            SELECT id, name, city FROM company
            WHERE verified AND id <> %(id)s AND sectors && %(sectors)s
              AND (region = %(region)s OR emdn_groups && %(groups)s)
            ORDER BY (emdn_groups && %(groups)s) DESC, (region = %(region)s) DESC, tier, public_awards DESC LIMIT 6""",
                               {"id": cid, "sectors": c["sectors"], "region": c["region"],
                                "groups": c.get("emdn_groups") or []}).fetchall()
    return c, members, similar


@app.get("/firma/{cid}", response_class=HTMLResponse)
def detail(request: Request, cid: str, embed: bool = False):
    c, members, similar = load_company(cid)
    return templates.TemplateResponse(request, "detail.html", dict(c=c, members=members, similar=similar, embed=embed))


@app.get("/quellen", response_class=HTMLResponse)
def sources(request: Request, embed: bool = False):
    with db() as conn:
        rows = conn.execute("""SELECT s.*, (SELECT count(*) FROM company_member m WHERE m.source = s.id) AS n
                               FROM source s ORDER BY id""").fetchall()
        gleif = conn.execute("SELECT count(*) AS n FROM company WHERE lei IS NOT NULL").fetchone()["n"]
    return templates.TemplateResponse(request, "sources.html", dict(rows=rows, gleif=gleif, embed=embed))


@app.get("/datenschutz", response_class=HTMLResponse)
def privacy(request: Request, embed: bool = False):
    return templates.TemplateResponse(request, "privacy.html", dict(embed=embed))


@app.get("/impressum")
def imprint():
    from fastapi.responses import RedirectResponse
    from web.common import OPERATOR
    return RedirectResponse(OPERATOR["imprint_url"], status_code=302)


@app.get("/hilfe", response_class=HTMLResponse)
def help_page(request: Request, embed: bool = False):
    return templates.TemplateResponse(request, "help.html", dict(embed=embed))


# ---------- statistics ----------

def stats(f: dict) -> dict:
    w, params = where(**f)
    with db() as conn:
        def group(expr: str, frm: str = "company") -> list[dict]:
            return conn.execute(f"SELECT {expr} AS k, count(*) AS n FROM {frm}{w} GROUP BY 1 ORDER BY 2 DESC",
                                params).fetchall()
        total = conn.execute(f"SELECT count(*) AS n FROM company{w}", params).fetchone()["n"]
        out = {"total": total,
               "sector": group("s", "company, unnest(sectors) s"),
               "region": group("region"),
               "size": group("size_class"),
               "product": group("g", "company, unnest(emdn_groups) g")}
    names = {"sector": SECTORS, "region": REGIONS, "size": SIZES, "product": EMDN}
    for k, m in names.items():
        out[k] = [{"key": r["k"], "label": _(m.get(r["k"], "unbekannt")) if r["k"] else _("unbekannt"), "n": r["n"]}
                  for r in out[k]]
    return out


CHARTS = [("sector", "nach Branche"), ("region", "nach Bundesland"), ("size", "nach Größenklasse"),
          ("product", "nach Produktgruppe (Medizintechnik)")]


@app.get("/statistik", response_class=HTMLResponse)
def statistics(request: Request, q: str = "", sector: str = "", tier: str = "", region: str = "", size: str = "",
               revenue: str = "", product: str = "", contact: bool = False, alle: bool = False, embed: bool = False):
    s = stats(filters(q, sector, tier, region, size, revenue, product, contact, alle))
    return templates.TemplateResponse(request, "stats.html", dict(s=s, charts=CHARTS, embed=embed,
                                                                 qs=query_string(request)))


@app.get("/api/v1/stats")
def api_stats(q: str = "", sector: str = "", tier: str = "", region: str = "", size: str = "", revenue: str = "",
              product: str = "", contact: bool = False, alle: bool = False):
    """Counts by sector, federal state, size class and EMDN product group for the given filters."""
    return stats(filters(q, sector, tier, region, size, revenue, product, contact, alle))


@app.get("/statistik.pdf")
def statistics_pdf(q: str = "", sector: str = "", tier: str = "", region: str = "", size: str = "", revenue: str = "",
                   product: str = "", contact: bool = False, alle: bool = False):
    s = stats(filters(q, sector, tier, region, size, revenue, product, contact, alle))
    doc = pdf.Doc(_("Firmenverzeichnis Gesundheitswirtschaft") + " – " + _("Statistik"))
    doc.text(f"{s['total']:,}".replace(",", ".") + " " + _("Unternehmen") + (f" · {_('Suchbegriff')}: {q}" if q else ""), 10)
    for key, title in CHARTS:
        doc.bars(_(title), [(r["label"], r["n"]) for r in s[key]][:16])
    doc.text(ATTRIBUTION, 7)
    return Response(doc.render(), media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="statistik.pdf"'})


# ---------- API ----------

def clean(row: dict) -> dict:
    out = {k: row.get(k) for k in PUBLIC_FIELDS if k in row}
    if out.get("public_last_award"):
        out["public_last_award"] = out["public_last_award"].isoformat()
    return out


@app.get("/api/v1/companies")
def api_list(q: str = "", sector: str = "", tier: str = "", region: str = "", size: str = "", revenue: str = "",
             product: str = "", contact: bool = False, alle: bool = False,
             limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
    """Search companies. `q` supports AND/OR/NOT (also UND/ODER/NICHT), brackets, "phrases" and prefix*.
    Filters: sector (medtech, pharma, biotech, digital_health), tier (A, B, C), region (ISO 3166-2, e.g. DE-BW),
    size (micro, small, medium, large), revenue (r1–r4), product (EMDN category letter, e.g. Z), contact=true,
    alle=true includes records that are not register-verified."""
    total, rows, facets = search(filters(q, sector, tier, region, size, revenue, product, contact, alle), limit, offset)
    return {"total": total, "limit": limit, "offset": offset, "facets": facets, "items": [clean(r) for r in rows]}


@app.get("/api/v1/companies/{cid}")
def api_one(cid: str):
    """One company with all fields and its sector evidence."""
    with db() as conn:
        c = conn.execute("SELECT * FROM company WHERE id = %s AND hidden_reason IS NULL", (cid,)).fetchone()
    if not c:
        raise HTTPException(404)
    if REVENUE_LOCKED:
        c = {k: v for k, v in c.items() if k not in ("revenue_eur", "revenue_class")}
    return clean(c)


# ---------- exports ----------

def export_rows(f: dict) -> list[dict]:
    _t, rows, _f = search(f, limit=100000, offset=0, fields=", ".join(EXPORT_FIELDS))
    for r in rows:
        r["sectors"] = ", ".join(_(SECTORS.get(x, x)) for x in r["sectors"])
        r["emdn_groups"] = ", ".join(_(EMDN.get(x, x)) for x in (r["emdn_groups"] or []))
        r["srns"] = ", ".join(r["srns"])
        r["sources"] = ", ".join(r["sources"])
        r["region"] = REGIONS.get(r["region"], r["region"])
        r["tier"] = _(TIERS.get(r["tier"], r["tier"]))
        r["size_class"] = _(SIZES[r["size_class"]]) if r["size_class"] else None
    return rows


ATTRIBUTION = ("Quellen: Europäische Kommission – EUDAMED und TED (Weiterverwendung nach Beschluss 2011/833/EU), "
               "CORDIS (CC BY 4.0); Source: European Medicines Agency (Art. 57); GLEIF (CC0); oeffentlichevergabe.de "
               "(amtliche Bekanntmachungen); Websites der Unternehmen (Kontaktseite, Beschäftigtenzahl); eigene Auswertung. "
               "Keine personenbezogenen Daten.")


def ef(q="", sector="", tier="", region="", size="", revenue="", product="", contact=False, alle=False):
    return filters(q, sector, tier, region, size, revenue, product, contact, alle)


@app.get("/export.csv")
def export_csv(request: Request):
    rows = export_rows(ef(**{k: v for k, v in request.query_params.items() if k in FILTERS}))
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=EXPORT_FIELDS, delimiter=";")
    w.writeheader()
    w.writerows(rows)
    buf.write(f"# {ATTRIBUTION}\n")
    return Response("﻿" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="firmenverzeichnis.csv"'})


@app.get("/export.xlsx")
def export_xlsx(request: Request):
    from openpyxl import Workbook

    rows = export_rows(ef(**{k: v for k, v in request.query_params.items() if k in FILTERS}))
    wb = Workbook()
    ws = wb.active
    ws.title = "Firmen" if lang() == "de" else "Companies"
    ws.append(EXPORT_FIELDS)
    for r in rows:
        ws.append([r[k] for k in EXPORT_FIELDS])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    wb.create_sheet("Quellen" if lang() == "de" else "Sources").append([ATTRIBUTION])
    buf = io.BytesIO()
    wb.save(buf)
    return Response(buf.getvalue(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="firmenverzeichnis.xlsx"'})


@app.get("/export.pdf")
def export_pdf(request: Request):
    """Result list as PDF (up to 2,000 rows) — for presentations and print."""
    f = ef(**{k: v for k, v in request.query_params.items() if k in FILTERS})
    total, rows, _f = search(f, limit=2000, offset=0)
    doc = pdf.Doc(_("Firmenverzeichnis Gesundheitswirtschaft"))
    doc.text(f"{total:,}".replace(",", ".") + " " + _("Unternehmen") + (f" · {f['q']}" if f["q"] else ""), 10)
    doc.table([_("Name"), _("Ort"), _("Branche"), _("Kontakt")], [
        (r["name"], f"{r['post_code'] or ''} {r['city'] or ''}".strip(), ", ".join(_(SECTORS[s]) for s in r["sectors"]),
         r["domain"] or r["email_role"] or "") for r in rows], [0.42, 0.2, 0.18, 0.2])
    doc.text(ATTRIBUTION, 7)
    return Response(doc.render(), media_type="application/pdf",
                    headers={"Content-Disposition": 'attachment; filename="firmenverzeichnis.pdf"'})


@app.get("/firma/{cid}/pdf")
def detail_pdf(cid: str):
    c, members, _s = load_company(cid)
    doc = pdf.Doc(c["name"])
    kv = [(_("Rechtsform"), c["legal_form"]), (_("Anschrift"), f"{c['street'] or ''}, {c['post_code'] or ''} {c['city'] or ''}"),
          (_("Branche"), ", ".join(_(SECTORS[s]) for s in c["sectors"])), (_("Website"), c["website"]),
          (_("Kontaktseite"), c.get("contact_url")), (_("E-Mail (Funktionspostfach)"), c["email_role"]),
          (_("Produkte (EMDN)"), ", ".join(_(EMDN.get(g, g)) for g in (c.get("emdn_groups") or []))),
          (_("USt-IdNr."), c["vat"]), (_("Handelsregister"), c["register_no"]), ("LEI", c["lei"]),
          ("EUDAMED SRN", ", ".join(c["srns"]))]
    doc.table(["", ""], [(k, v or "–") for k, v in kv], [0.3, 0.7], header=False)
    doc.text(_("Herkunft der Daten") + ": " + "; ".join(f"{m['title']} ({m['licence']})" for m in members), 8)
    return Response(doc.render(), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{cid}.pdf"'})


@app.get("/api/docs", response_class=HTMLResponse, include_in_schema=False)
def api_docs(request: Request):
    """English API reference rendered from the OpenAPI schema (no external scripts)."""
    spec = app.openapi()
    ops = [(path, m.upper(), op) for path, item in spec["paths"].items() for m, op in item.items()
           if path.startswith("/api/v1") or path.startswith("/export")]
    return templates.TemplateResponse(request, "api.html", dict(ops=ops, base=str(request.base_url).rstrip("/"), embed=False))


@app.get("/api/health")
def health():
    with db() as conn:
        r = conn.execute("""SELECT count(*) AS companies, count(*) FILTER (WHERE verified) AS verified,
                                   max(built_at) AS built_at FROM company""").fetchone()
    return JSONResponse({"ok": True, **{k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}})
