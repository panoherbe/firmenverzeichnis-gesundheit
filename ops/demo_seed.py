"""
Demo content for the public demo and the screenshots: one test account (role user) with two saved searches, and a
FICTIONAL example matchmaking event whose participants are invented and labelled "(fiktiv)" — no real company or
person is shown as a participant. Idempotent: an existing demo event (slug starting with "beispiel-") is removed first.

    DEMO_PASSWORD=… ADMIN_EMAIL=… .venv/bin/python -m ops.demo_seed

Prints the demo login. The admin account named by ADMIN_EMAIL must exist (web.auth create-user …); its TOTP secret is
read from the database to drive the organiser forms, nothing is printed about it.
"""

from __future__ import annotations

import os
import re
import secrets

from fastapi.testclient import TestClient

from web import auth
from web.app import app
from web.common import db

DE = [  # fictional German companies (not in the directory)
    ("Beispiel Medizintechnik GmbH (fiktiv)", "Tuttlingen", ["medtech"], "chirurgische Instrumente, Endoskopie",
     "Chirurgische Instrumente und Endoskope, CE-zertifiziert", "Distributoren für Kliniken in Skandinavien"),
    ("Muster Diagnostik AG (fiktiv)", "Berlin", ["medtech", "biotech"], "In-vitro-Diagnostik, Schnelltests",
     "Schnelltests für Labor und Praxis", "Labore und Einkaufsgemeinschaften"),
    ("Demo Health Software GmbH (fiktiv)", "Hamburg", ["digital_health"], "Software, Telemonitoring",
     "Telemonitoring-Plattform für Herzpatienten", "Kliniken und Versorgungsnetze, Pilotpartner"),
]
INTL = [  # fictional international participants
    ("Nordic Hospital Supply Oy (fiktiv)", "FI", "Helsinki", ["medtech"], "surgical instruments, endoscopy",
     "distribution to 40 hospitals in Finland and Sweden", "surgical instrument manufacturers", "Europe/Helsinki"),
    ("Scandi Lab Partners AB (fiktiv)", "SE", "Göteborg", ["medtech", "biotech"], "diagnostics, rapid tests",
     "laboratory network and procurement", "in vitro diagnostics suppliers", "Europe/Stockholm"),
    ("Copenhagen Care Network ApS (fiktiv)", "DK", "København", ["digital_health"], "telemonitoring, software",
     "regional care network, pilot projects", "digital health solutions for cardiology", "Europe/Copenhagen"),
]


def main() -> None:
    c = TestClient(app)
    pw = os.environ.get("DEMO_PASSWORD") or secrets.token_urlsafe(10)
    with db() as conn:
        u = conn.execute("SELECT id FROM app_user WHERE email = 'demo@example.org'").fetchone()
        if u:
            conn.execute("UPDATE app_user SET pw_hash = %s, active = true WHERE id = %s", (auth.hash_password(pw), u["id"]))
            uid = u["id"]
        else:
            auth.create_user("demo@example.org", "Testzugang", "user", pw)
            uid = conn.execute("SELECT id FROM app_user WHERE email = 'demo@example.org'").fetchone()["id"]
        conn.execute("DELETE FROM saved_search WHERE user_id = %s", (uid,))
        for name, q in (("Medizintechnik Baden-Württemberg", "sector=medtech&region=DE-BW"),
                        ("In-vitro-Diagnostik mit Kontakt", "product=W&contact=true")):
            conn.execute("INSERT INTO saved_search (user_id, name, query) VALUES (%s, %s, %s)", (uid, name, q))
        admin = conn.execute("SELECT * FROM app_user WHERE email = %s AND role = 'admin'",
                             (os.environ["ADMIN_EMAIL"],)).fetchone()
        conn.execute("DELETE FROM mm_event WHERE slug LIKE 'beispiel-%'")
    admin_pw = secrets.token_urlsafe(12)
    with db() as conn:  # temporary password for this run only, restored afterwards
        old = admin["pw_hash"]
        conn.execute("UPDATE app_user SET pw_hash = %s WHERE id = %s", (auth.hash_password(admin_pw), admin["id"]))
    try:
        r = c.post("/login", data={"email": admin["email"], "password": admin_pw, "code": auth.totp(admin["totp_secret"]),
                                   "next": "/matchmaking/admin"}, follow_redirects=False)
        assert r.status_code == 303 and "error" not in r.headers["location"], r.headers
        r = c.post("/matchmaking/admin/speichern", follow_redirects=False, data={
            "title": "Beispiel: Matchmaking Medizintechnik & Digital Health – Nordeuropa",
            "title_en": "Example: matchmaking medical technology & digital health – Northern Europe",
            "intro": "Fiktive Beispielveranstaltung für die Demo. 20-minütige Videogespräche zwischen deutschen Herstellern "
                     "und Partnern aus Finnland, Schweden und Dänemark.",
            "intro_en": "Fictional example event for the demo. 20-minute video meetings between German manufacturers and "
                        "partners from Finland, Sweden and Denmark.",
            "tz": "Europe/Berlin", "date": "2027-03-10", "starts": "09:30", "ends": "12:30", "b2b_start": "10:00",
            "b2b_end": "12:00", "slot_minutes": "20", "gap_minutes": "5", "max_meetings": "6", "format": "hybrid",
            "venue": "optional vor Ort: Messe-Lounge, Tisch nach Plan", "markets": "FI, SE, DK",
            "sectors": ["medtech", "biotech", "digital_health"],
            "agenda": "09:30 Begrüßung und Marktüberblick Nordeuropa\n10:00 B2B-Gespräche (20 Minuten)\n12:00 Abschluss",
            "agenda_en": "09:30 Welcome and market overview Northern Europe\n10:00 B2B meetings (20 minutes)\n12:00 Close",
            "status": "open"})
        eid = int(re.search(r"/admin/(\d+)", r.headers["location"]).group(1))
        with db() as conn:
            slug = conn.execute("SELECT slug FROM mm_event WHERE id = %s", (eid,)).fetchone()["slug"]
            if not slug.startswith("beispiel-"):
                conn.execute("UPDATE mm_event SET slug = %s WHERE id = %s", ("beispiel-nordeuropa", eid))
                slug = "beispiel-nordeuropa"
        tokens = []
        for name, city, sectors, products, offer, seek in DE:
            with db() as conn:  # German side normally picks a directory company; fictional ones go in directly
                tok = secrets.token_urlsafe(16)
                conn.execute("""INSERT INTO mm_participant (event_id, token, side, org_name, country, city, person_name, email,
                    sectors, products, markets, offer, seek, consent_at, consent_text, delete_after, mode)
                    VALUES (%s,%s,'de',%s,'DE',%s,'Demo-Teilnehmende (fiktiv)','demo@example.org',%s,%s,
                    ARRAY['FI','SE','DK'],%s,%s,now(),'Demo-Datensatz, fiktiv','2028-03-10','online')""",
                             (eid, tok, name, city, sectors, products, offer, seek))
                tokens.append(tok)
        for name, cc, city, sectors, products, offer, seek, tz in INTL:
            r = c.post(f"/matchmaking/{slug}/anmelden", follow_redirects=False, data={
                "side": "intl", "org_name": name, "country": cc, "city": city, "org_type": "company", "sectors": sectors,
                "products": products, "markets": ["DE"], "offer": offer, "seek": seek, "person_name": "Demo participant (fictional)",
                "email": "demo@example.org", "consent": "yes", "tz": tz, "mode": "online"})
            tokens.append(r.headers["location"].split("/p/")[1].split("?")[0])
        with db() as conn:
            ids = {p["token"]: p["id"] for p in conn.execute("SELECT id, token FROM mm_participant WHERE event_id = %s", (eid,))}
        de, intl = tokens[:3], tokens[3:]
        for a, b in ((0, 0), (1, 1), (2, 2), (0, 1)):
            c.post(f"/matchmaking/p/{de[a]}/anfrage", data={"partner_id": ids[intl[b]], "message": "Gern kennenlernen"},
                   follow_redirects=False)
        with db() as conn:
            meetings = conn.execute("SELECT id, partner_id FROM mm_meeting WHERE event_id = %s ORDER BY id", (eid,)).fetchall()
        tok_by_id = {v: k for k, v in ids.items()}
        for m in meetings[:3]:
            c.post(f"/matchmaking/p/{tok_by_id[m['partner_id']]}/antwort", data={"meeting_id": m["id"], "decision": "accept"},
                   follow_redirects=False)
        c.post(f"/matchmaking/admin/{eid}/planen", follow_redirects=False)
    finally:
        with db() as conn:
            conn.execute("UPDATE app_user SET pw_hash = %s WHERE id = %s", (old, admin["id"]))
    print(f"demo login: demo@example.org / {pw}")
    print(f"demo event: /matchmaking/{slug}  · agenda of a fictional participant: /matchmaking/p/{de[0]}")


if __name__ == "__main__":
    main()
