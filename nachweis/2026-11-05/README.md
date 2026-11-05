# Nachweis 05.11.2026

Woher die Zahlen in der README kommen. Gemessen am 05.11.2026 gegen 05:00 MEZ an der Datenbank der laufenden
Installation; die öffentliche Demo (https://firmen.panoherbe.com) läuft mit einem Abzug derselben Datenbank.

| Datei | Inhalt |
|---|---|
| [`zaehlungen.sql`](zaehlungen.sql) | die Zählabfragen (PostgreSQL) |
| [`zaehlungen-ausgabe.txt`](zaehlungen-ausgabe.txt) | ihre Ausgabe |
| [`api-health-demo.json`](api-health-demo.json) | `GET https://firmen.panoherbe.com/api/health` zum selben Zeitpunkt |

## Ergebnisse

| Zahl in der README | gemessen | Abfrage |
|---|---|---|
| 9.744 Unternehmen, 4.787 registergeprüft | 9.744 / 4.787 (Hersteller und Zulassungsinhaber 3.399, Bevollmächtigte und Importeure 1.388) | 1 |
| 4.144 mit Funktionspostfach oder Kontakt-/Impressumsseite | 4.144; dazu 222 nur mit Website, 421 ohne Kontaktweg; 2.919 mit Link zur Kontakt- oder Impressumsseite | 2 |
| 2.013 mit Produktkennung | 2.013 (EMDN-Produktgruppe 1.734, Arzneimittel aus EMA 331) | 3 |
| 422 mit Größenklasse | 422 registergeprüfte (636 Firmen mit einer Beschäftigtenzahl insgesamt) | 3 |
| 6.450 internationale Unternehmen aus 80 Ländern | 6.450 / 80 (CORDIS 3.711, EMA 2.739) | 5 |
| Stichprobe Hersteller 43 von 44 | 43/44 | 6 |
| Stichprobe Importeure 15 von 20 | am 02.11. 15/20; gegen die heutigen Firmen-IDs 14/19, weil ein Stichprobeneintrag inzwischen mit einem anderen zusammengeführt wurde | 6 |

Korrektur gegenüber dem Nachweis vom 04.11.: Dort zählte die Abfrage leere Produktlisten mit („2.015 mit EMDN“); richtig
waren 1.354. Seitdem ist der EUDAMED-Abruf fertig, und für 219 Profile nur mit Website wurde die Kontakt- oder
Impressumsseite nachgesucht (zwei Läufe, robots.txt beachtet). Gezählt wird eine gefundene Seite nur, wenn ihr Text
wirklich eine Impressums- oder Kontaktseite ist (Stichwort und E-Mail-Adresse oder Telefon): 94 von 116 Treffern;
die übrigen 22 waren Login-Seiten, „Seite nicht gefunden“ mit Status 200 oder Seiten, deren Inhalt nur per Skript lädt.

## Kontaktwege über die Register-E-Mail (05.11.2026, Nachmittag)

Hat ein Unternehmen im Register (EUDAMED, EMA) keine Website, aber eine E-Mail-Adresse der eigenen Firma, führt deren
Domain zur Website. Gespeichert wird nur die Domain, nie die Adresse; Freemail und Behörden-Domains (z. B. die der
prüfenden Landesbehörde in EUDAMED) zählen nicht. Auf dieser Website wird die Kontakt- oder Impressumsseite gesucht
(robots.txt beachtet). Eine so gefundene Seite zählt nur, wenn ihr Text ein Impressum/Kontakt-Stichwort, eine E-Mail-
Adresse oder Telefonnummer **und** ein unterscheidendes Wort des Firmennamens enthält: 593 von 791 Kandidaten bestanden
(104 ohne Firmennamen, 61 ohne E-Mail/Telefon, 32 ohne Stichwort, 1 nicht abrufbar). Stichprobe von 20 der so gefundenen
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
