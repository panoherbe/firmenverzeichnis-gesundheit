# Nachweis 05.11.2026

Woher die Zahlen in der README kommen. Gemessen am 05.11.2026 gegen 17:00 MEZ an der Datenbank der laufenden
Installation; die Demo-Installation (seit 05.11.2026 abends nur mit Zugangslink, auf Anfrage) läuft mit einem Abzug derselben Datenbank.

| Datei | Inhalt |
|---|---|
| [`zaehlungen.sql`](zaehlungen.sql) | die Zählabfragen (PostgreSQL) |
| [`zaehlungen-ausgabe.txt`](zaehlungen-ausgabe.txt) | ihre Ausgabe |
| [`api-health-demo.json`](api-health-demo.json) | `GET /api/health` der Demo-Installation zum selben Zeitpunkt |

## Ergebnisse

| Zahl in der README | gemessen | Abfrage |
|---|---|---|
| 9.744 Unternehmen, 4.787 registergeprüft | 9.744 / 4.787 (Hersteller und Zulassungsinhaber 3.399, Bevollmächtigte und Importeure 1.388) | 1 |
| 4.012 mit geprüftem Kontaktweg (Funktionspostfach oder Kontakt-/Impressumsseite), Neuzählung 05.11. nachts | 4.012; dazu 321 nur mit Website, 454 ohne Kontaktweg. Davon Funktionspostfach aus dem Register mit Mailserver 3.372, geprüfte Kontakt-/Impressumsseite 2.904, beides 2.264 | 2 |
| 2.013 mit Produktkennung | 2.013 (EMDN-Produktgruppe 1.734, Arzneimittel aus EMA 331) | 3 |
| 405 mit Größenklasse | 405 registergeprüfte (614 Firmen mit einer Beschäftigtenzahl insgesamt), nach den Lesefehler-Regeln vom 05.11. nachts | 3 |
| 6.450 internationale Unternehmen aus 80 Ländern | 6.450 / 80 (CORDIS 3.711, EMA 2.739) | 5 |
| Stichprobe Hersteller 43 von 44 | 43/44 | 6 |
| Stichprobe Importeure 15 von 20 | am 02.11. 15/20; gegen die heutigen Firmen-IDs 14/19, weil ein Stichprobeneintrag inzwischen mit einem anderen zusammengeführt wurde | 6 |

Korrektur gegenüber dem Nachweis vom 04.11.: Dort zählte die Abfrage leere Produktlisten mit („2.015 mit EMDN“); richtig
waren 1.354. Seitdem ist der EUDAMED-Abruf fertig, und für 219 Profile nur mit Website wurde die Kontakt- oder
Impressumsseite nachgesucht (zwei Läufe, robots.txt beachtet). Gezählt wird eine gefundene Seite nur, wenn ihr Text
wirklich eine Impressums- oder Kontaktseite ist (Stichwort und E-Mail-Adresse oder Telefon): 94 von 116 Treffern;
die übrigen 22 waren Login-Seiten, „Seite nicht gefunden“ mit Status 200 oder Seiten, deren Inhalt nur per Skript lädt.

## Strenge Neuprüfung aller Kontaktwege (05.11.2026 nachts)

Anlass: Eine Zufallsstichprobe von 100 der damals 4.139 gezählten Firmen, von Hand im Web geprüft
([`stichprobe-kontakt-100.csv`](stichprobe-kontakt-100.csv), Ziehung `ORDER BY md5(id || 'stichprobe-2026-11-05-v2')`):

| | ja | ja, Konzernseite | nein | offen |
|---|---|---|---|---|
| gehört zur Gesundheitswirtschaft | 81 | – | 2 | 17 |
| Kontaktweg funktioniert und gehört der Firma | 72 | 8 | 6 | 14 |

Beides zugleich: 70 von 100. Gefundener Fehler: Das Wort „kontakt“ wurde auch in „Kontaktlinsen“, „kontaktlose
Thermometer“ und Newsletter-Seiten erkannt; dazu tote Seiten. „Offen“ heißt: Website nicht erreichbar oder ohne
Produktangabe; die Firmen sind in EUDAMED als Wirtschaftsakteure registriert.

Danach wurde **jeder** gezählte Kontaktweg neu geprüft (`etl/sources/contact_recheck.py`, `etl/sources/mail_mx.py`):

- **Kontakt-/Impressumsseite:** Die Adresse muss eine Kontakt- oder Impressumsadresse sein, nicht nur das Wort
  enthalten. Die Seite muss heute laden (robots.txt beachtet) und eine Kontaktangabe sowie den Firmennamen oder die
  Postleitzahl aus dem Register enthalten. Sonst wird auf der eigenen Website nachgesucht. Ein zweiter, langsamer
  Durchgang lief für blockierte und zeitüberschrittene Seiten. Ergebnis: **2.904 Seiten**.
- **Funktionspostfach aus dem Register** (EUDAMED, EMA; LB: „mindestens eine E-Mail-Adresse“): zählt, wenn seine
  Domain einen Mailserver hat (MX- oder A-Eintrag, über DNS-over-HTTPS). Es wurde keine Mail gesendet.
  47 Postfächer ohne Mailserver fielen weg. Ergebnis: **3.372 Postfächer**.
- **Vereinigung: 4.012** von 4.787 registergeprüften Firmen (Hersteller und Zulassungsinhaber 2.919,
  Bevollmächtigte und Importeure 1.093).

## Kontaktwege über die Register-E-Mail (05.11.2026, Nachmittag)

Hat ein Unternehmen im Register (EUDAMED, EMA) keine Website, aber eine E-Mail-Adresse der eigenen Firma, führt deren
Domain zur Website. Gespeichert wird nur die Domain, nie die Adresse; Freemail und Behörden-Domains (z. B. die der
prüfenden Landesbehörde in EUDAMED) zählen nicht. Auf dieser Website wird die Kontakt- oder Impressumsseite gesucht
(robots.txt beachtet). Eine so gefundene Seite zählt nur, wenn ihr Text ein Impressum/Kontakt-Stichwort, eine E-Mail-
Adresse oder Telefonnummer **und** ein unterscheidendes Wort des Firmennamens enthält; robots.txt gilt auch für diese
Prüfabrufe: 594 von 791 Kandidaten bestanden (104 ohne Firmennamen, 60 ohne E-Mail/Telefon, 31 ohne Stichwort,
1 von robots.txt ausgeschlossen, 1 nicht abrufbar). Ein Teil der bestandenen Firmen hatte schon vorher ein
Funktionspostfach; neu dazu kamen dadurch rund 150 Firmen mit Kontaktweg.

Korrektur am Nachmittag: Die Regel für Funktionspostfächer ließ nach kurzen Rollenwörtern („ra“) Namen ohne
Trenner durch (z. B. „ra“ + Nachname) und zählte Poststellen von Behörden mit. Jetzt gilt ein Anhang nur nach
Trenner oder aus einer festen Liste zusammengesetzter Rollen (qmb, regulatoryaffairs …), Behörden-Domains zählen
nicht. Dadurch fielen 16 Adressen weg (darunter 5 nach Namen aussehende und 2 Behörden-Poststellen), 5 Rollenadressen
mit Trenner kamen dazu. Stichprobe von 20 der so gefundenen
Seiten, von Hand angesehen: 18 sind die Kontaktseite des Unternehmens selbst, 2 die Investor-Relations-Kontaktseite des
Mutterkonzerns.

## Stichproben: wie sie gemacht wurden

- **Branche (v1, 02.11.2026):** 100 zufällig gezogene registergeprüfte Firmen, von Hand im Web geprüft: gehört das
  Unternehmen zur Gesundheitswirtschaft, stimmt die Branche? 27 der 100 ließen sich mit zwei Suchen nicht
  entscheiden und zählen nicht mit (kleine EUDAMED-Akteure).
- **Beschäftigtenzahl (03.11.2026):** 30 zufällig gezogene Firmen mit Größenklasse; für jede wurde der Satz
  gelesen, aus dem die Zahl stammt.
  - Erste Regelfassung: 18 von 25 richtig. Die Fehler waren vor allem Zahlen aus der Firmengeschichte
    („2004 … 10 Mitarbeiter“).
  - Nach der Regeländerung (Zahlen mit Jahreszahl davor zählen nicht): **27 von 30 richtig**, 95-%-Konfidenzintervall
    etwa 74–97 %.
  - Die drei Fehler: eine historische Zahl, ein nummerierter Schritt („04 Mitarbeitende“; danach eine weitere Regel,
    nicht erneut gemessen) und ein EU-Bevollmächtigter mit der Zahl seines ausländischen Mutterkonzerns.

Die geprüften Einzelfälle liegen beim Betreiber und werden auf Anfrage gezeigt. Sie sind nicht öffentlich, weil sie
Textauszüge fremder Websites enthalten.
