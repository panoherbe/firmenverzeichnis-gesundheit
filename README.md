# Firmenverzeichnis Gesundheitswirtschaft

**Ein Verzeichnis der deutschen Gesundheitswirtschaft, das sich selbst pflegt:** Medizintechnik, Pharma,
Biotechnologie und digitale Gesundheit, gebaut aus amtlichen Registern. Jede Angabe nennt ihre Quelle. Dazu gibt es
Suche, Export, API und virtuelles Matchmaking mit internationalen Partnern. Es läuft heute; die Zahlen unten sind
an der laufenden Installation gemessen.

![Rundgang: Suche, Profil, Statistik, Matchmaking](medien/rundgang.gif)

▶ **[Rundgang als Video (MP4, ohne Ton, mit Untertiteln)](medien/rundgang.mp4)**

## Was es für Sie tut

- **Ausländische Interessenten finden deutsche Hersteller.** Sie suchen nach Produkt, Ort oder Firma, filtern nach
  Branche, Bundesland, Produktgruppe und Größe und kommen mit zwei Klicks zur Kontaktseite. Das geht auf Deutsch und
  Englisch, am Rechner und auf dem Telefon.
- **Ihr Team arbeitet mit den Daten.** Suchen lassen sich speichern, der Verlauf bleibt erhalten. Excel, CSV und PDF
  gehen in einem Klick heraus, Diagramme als JPG oder PNG für Präsentationen. Eine API liefert dieselben Daten
  automatisch.
- **Sie bringen Firmen ins Gespräch.** Das virtuelle oder hybride Matchmaking macht Vorschläge mit Begründung, plant
  die Termine automatisch, öffnet Videoräume und wertet am Ende eine Umfrage zu Nutzen und Umsatzchancen aus.
- **Datenschutz ist eingebaut.** Im Verzeichnis gibt es keine Personendaten: nur Unternehmen, Kontaktseiten und
  Funktionspostfächer, keine gekauften Listen. Matchmaking, Nutzerkonten und Support speichern Name, Funktion und
  E-Mail-Adresse mit Einwilligung bzw. auf Vertragsgrundlage und mit Löschfrist (Matchmaking: 12 Monate nach der
  Veranstaltung).

| Suche mit Filtern | Profil mit Quellen | Auf dem Telefon |
|---|---|---|
| ![Suche](medien/bilder/suche.png) | ![Profil](medien/bilder/profil.png) | ![Telefon](medien/bilder/mobil.png) |

| Statistik und Export | Matchmaking: persönliche Agenda | Matchmaking: Veranstalter |
|---|---|---|
| ![Statistik](medien/bilder/statistik.png) | ![Agenda](medien/bilder/matchmaking-agenda.png) | ![Veranstalter](medien/bilder/matchmaking-dashboard.png) |

## Was Sie brauchen → läuft hier schon

Gemessen am 04.11.2026 an der laufenden Installation; Abfragen und Ausgaben: [`nachweis/2026-11-04/`](nachweis/2026-11-04/).

| Was Sie brauchen | Stand |
|---|---|
| ein großer, geprüfter Bestand | **4.787 registergeprüfte Unternehmen** (EUDAMED, EMA, LEI), 9.744 insgesamt suchbar |
| ein Kontaktweg zu jedem Unternehmen | **4.118** davon mit Kontaktseite, Website oder Funktionspostfach; im Verzeichnis keine Personendaten |
| was die Firma macht und herstellt | Branche mit Begründung; Produktgruppen nach EU-Nomenklatur (EMDN) aus EUDAMED, heute für 2.015 Firmen (der Abruf läuft noch); Arzneimittel aus EMA |
| schnell das Richtige finden | Volltextsuche mit UND/ODER/NICHT, Klammern, Phrasen, Wortanfang; Filter Branche, Bundesland, Produktgruppe, Größe, Umsatz, Rolle |
| im Team arbeiten | namentliche Konten, gespeicherte Suchen und Suchverlauf, Admins mit zweitem Faktor (TOTP), Zugriffsprotokoll |
| Daten und Grafiken weiterverwenden | Excel, CSV, PDF, JSON, Diagramme als JPG/PNG, REST-API mit englischer Dokumentation |
| internationales Publikum | Oberfläche und Profile auf Deutsch und Englisch |
| in die eigene Website einbetten | `?embed=1`, Farben, Schrift und Logo per Theme-Datei |
| Geschäftskontakte anbahnen | Matchmaking eingebaut; internationale Firmenbasis mit **6.450 Unternehmen aus 80 Ländern** (EMA, CORDIS) |
| verlässlicher Betrieb | Ticketformular mit sofortiger Ticketnummer, Statusseite, monatlicher SLA-Bericht; tägliche Backups mit Wiederherstellungstest und Kopie an einen zweiten Standort (Skripte in `ops/`; auf der Demo laufen Backup, Wiederherstellungstest und Selbstprüfung) |

---

## Technik

- **Quellen** (Rohablage vor dem Einlesen unter `raw/<quelle>/<tag>/`, Postgres hält nur Pfad und SHA-256):
  EUDAMED-Akteure und -Produkte (EU-Kommission), EMA Article-57-Daten, GLEIF Golden Copy (CC0), CORDIS (CC BY 4.0),
  Zuschläge öffentlicher Aufträge (TED, Bekanntmachungsservice), Websites der Firmen (nur Kontaktseite, Erreichbarkeit
  und Beschäftigtenzahl; robots.txt wird beachtet, § 44b UrhG).
- **Datenlizenzen:** EUDAMED, TED und CORDIS: Europäische Kommission bzw. Union, Weiterverwendung nach Beschluss
  2011/833/EU (CC BY 4.0, Quellenangabe, Änderungen kenntlich). EMA: Vervielfältigung erlaubt mit der Angabe
  „Source: European Medicines Agency“. GLEIF: CC0. oeffentlichevergabe.de: amtliche Bekanntmachungen, genutzt werden
  nur Firmenname und Zuschlagsdaten. Die Anwendung nennt die Quelle je Eintrag (`/quellen`, „Herkunft der Daten“).
- **Keine Personendaten im Verzeichnis:** Die verantwortliche Person aus EUDAMED (PRRC) wird vor dem Schreiben entfernt. E-Mail-Adressen
  bleiben nur, wenn sie Funktionspostfächer sind. Einzelkaufleute, Apotheken und Namen ohne Rechtsform fliegen raus.
- **Entity Resolution ohne Ähnlichkeitswerte:** nur exakte Schlüssel (USt-IdNr., Handelsregister + PLZ-Region,
  Name + Ort, LEI). Jede Verknüpfung speichert ihre Regel.
- **Anwendung:** FastAPI, serverseitig gerendert, ohne fremde Skripte oder CDNs; `web/app.py` (Suche, Statistik,
  Export, API), `web/auth.py` (Konten, TOTP, Protokoll), `web/matchmaking.py`, `web/support.py`.
- **Betrieb:** `ops/` mit Bootstrap für Ubuntu 24.04, Caddy (TLS), systemd-Timern für Backup, Wiederherstellungstest,
  Monitoring, Datenaktualisierung und SLA-Bericht.

```bash
cp .env.example .env            # Zugangsdaten eintragen
python -m venv .venv && .venv/bin/pip install -r requirements.txt
docker run -d --name firmen-db -e POSTGRES_USER=firmen -e POSTGRES_DB=firmen -e POSTGRES_PASSWORD=... \
  -p 127.0.0.1:5434:5432 postgres:16
.venv/bin/python -m etl.load schema && .venv/bin/python -m ops.schema
.venv/bin/python -m etl.sources.eudamed fetch     # mehrere Stunden: die API ist langsam und drosselt
.venv/bin/python -m etl.sources.ema fetch
.venv/bin/python -m etl.sources.gleif fetch       # ~500 MB
.venv/bin/python -m etl.load all && .venv/bin/python -m etl.resolve
.venv/bin/python -m etl.sources.eudamed_devices fetch && .venv/bin/python -m etl.sources.websites fetch
.venv/bin/python -m etl.enrich && .venv/bin/python -m etl.cleanup
.venv/bin/uvicorn web.app:app --host 127.0.0.1 --port 8601
```

Lizenz: GNU AGPL-3.0 für den Code (`LICENSE`). Ab 04.11.2026 AGPL-3.0; frühere Stände bis Commit `be86abe`
Apache-2.0. Auftraggeber erhalten die vertraglich vereinbarte Lizenz. Die gesammelten Daten sind nicht Teil des
Repositorys; ihre Quellen und Lizenzen
stehen unter `/quellen` in der Anwendung.

Panagiotis Herbe · Hamburg · [panoherbe.com](https://panoherbe.com)
