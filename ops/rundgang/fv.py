"""Walkthrough for the teaser repo and buyer presentations: search, filter, profile, English, statistics with export,
saved searches, matchmaking (event, personal agenda, organiser view), phone, measured numbers.

Runs in the recorder image (Dockerfile.aufnahme, Playwright 1.56) with --network host against the app on datanuc.
Numbers on the cards are read from the running API at recording time, never typed in.
Writes frames + frames.txt to $AUS and screenshots to $AUS/bilder (also used in docs/bid/bilder/).

  env: APP_URL, AUS, DEMO_PW (test account), ADMIN_EMAIL + ADMIN_PW + ADMIN_TOTP (temporary, organiser view),
       MM_EVENT (slug), MM_TOKEN (agenda link of a fictional participant), INTL_N, INTL_COUNTRIES
"""

import base64
import datetime as dt
import hashlib
import hmac
import json
import os
import struct
import sys
import time
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent))
from rundgang import HELL, Rundgang, faktenkarte, telefonkarte, titelkarte  # noqa: E402

VIEWPORT = {'width': 1120, 'height': 700}
APP = os.environ.get('APP_URL', 'http://127.0.0.1:8601')
AUS = Path(os.environ.get('AUS', '/aus/fv'))
REPO = 'github.com/panoherbe/firmenverzeichnis-gesundheit'
FUSS = ('Prototyp · Apache 2.0', 'Panagiotis Herbe · panoherbe.com')
HEUTE = dt.date.today().strftime('%d.%m.%Y')


def api(path):
    with urllib.request.urlopen(APP + path) as r:
        return json.load(r)


def punkt(n):
    return f'{n:,}'.replace(',', '.')


def totp(secret):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    d = hmac.new(key, struct.pack('>Q', int(time.time() // 30)), hashlib.sha1).digest()
    o = d[-1] & 15
    return f"{(struct.unpack('>I', d[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def main():
    health = api('/api/health')
    kontakt = api('/api/v1/companies?contact=true&limit=1')
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2, locale='de-DE', accept_downloads=True,
                                  bypass_csp=True)
        page = ctx.new_page()
        rg = Rundgang(page, AUS)

        rg.karte(titelkarte('Prototyp · läuft heute', 'Firmenverzeichnis Gesundheitswirtschaft',
                            f'{punkt(health["verified"])} Unternehmen aus Medizintechnik, Pharma, Biotech und digitaler '
                            'Gesundheit – aus amtlichen Registern, jede Angabe mit Quelle. Mit Suche, Export, API und '
                            'virtuellem Matchmaking.',
                            f'Rundgang ohne Ton · aufgenommen am {HEUTE} an der laufenden Installation', REPO, FUSS[1]), 4.5)

        # --- Suche ---------------------------------------------------------------------------------------------------
        page.goto(APP + '/?lang=de')
        page.wait_for_selector('#q')
        rg.untertitel('Ein Suchfeld für alles: Produkt, Firma, Ort, Postleitzahl oder Registernummer.', rolle='Suche')
        rg.halte(2.0)
        rg.tippe(page.locator('#q'), 'Endoskop* ODER Laparoskop*', je_zeichen=0.05)
        with page.expect_navigation():
            rg.klick(page.get_by_role('button', name='Suchen', exact=True), danach=0)
        page.wait_for_selector('table.res')
        rg.untertitel('UND, ODER, NICHT, Klammern und Wortanfänge funktionieren – auf Deutsch und Englisch.')
        rg.halte(2.6)
        rg.bild('suche')
        rg.untertitel('Filter lassen sich kombinieren; die Zahl in Klammern zeigt vorher, wie viele Treffer bleiben.')
        rg.zeige_auf(page.locator('#region'))
        with page.expect_navigation():
            page.select_option('#region', 'DE-BW')
        page.wait_for_selector('table.res')
        rg.halte(2.6)

        # --- Profil --------------------------------------------------------------------------------------------------
        with page.expect_navigation():
            rg.klick(page.locator('table.res a.name').first, danach=0)
        page.wait_for_selector('h1')
        rg.untertitel('Zwei Klicks bis zum Profil: Kontaktseite, Produkte nach EU-Nomenklatur, Register- und Steuernummern.',
                      rolle='Profil')
        rg.halte(3.4)
        rg.bild('profil')
        rg.scrolle(520, 0.8)
        rg.untertitel('Jede Angabe nennt ihre Quelle und Lizenz. Keine Personendaten, nur Funktionspostfächer und Kontaktseiten.')
        rg.halte(3.0)
        page.evaluate('window.scrollTo(0, 0)')
        with page.expect_navigation():
            rg.klick(page.locator('.langsw a', has_text='EN'), danach=0)
        page.wait_for_selector('h1')
        rg.untertitel('Dasselbe Profil auf Englisch – für Interessenten im Ausland. Oberfläche und Profile sind zweisprachig.')
        rg.halte(3.0)

        # --- Statistik und Export ------------------------------------------------------------------------------------
        page.goto(APP + '/statistik?lang=de&sector=medtech')
        page.wait_for_selector('svg')
        rg.untertitel('Statistik zur aktuellen Auswahl. Jede Grafik geht als JPG oder PNG heraus, die Seite als PDF, '
                      'die Trefferliste als Excel, CSV oder PDF.', rolle='Export')
        rg.halte(3.8)
        rg.bild('statistik')

        # --- Konto ---------------------------------------------------------------------------------------------------
        page.goto(APP + '/login?next=/konto')
        rg.untertitel('Namentliche Konten für das Team, Administratoren mit zweitem Faktor.', rolle='Konto')
        rg.tippe(page.locator('input[name=email]'), 'demo@example.org', je_zeichen=0.02, danach=0)
        page.fill('input[name=password]', os.environ['DEMO_PW'])
        with page.expect_navigation():
            rg.klick(page.get_by_role('button', name='Anmelden'), danach=0)
        page.wait_for_selector('h1')
        rg.untertitel('Suchen mit allen Filtern speichern und wieder aufrufen; der Suchverlauf ist immer da.')
        rg.halte(3.0)
        rg.bild('konto')
        page.goto(APP + '/logout')

        # --- Matchmaking -----------------------------------------------------------------------------------------------
        page.goto(APP + f'/matchmaking/{os.environ["MM_EVENT"]}?lang=de')
        page.wait_for_selector('h1')
        rg.untertitel('Virtuelles Matchmaking: Veranstaltungsseite mit Programm und Anmeldung, hier ein fiktives Beispiel.',
                      rolle='Matchmaking')
        rg.halte(3.4)
        rg.bild('matchmaking-veranstaltung')
        page.goto(APP + f'/matchmaking/p/{os.environ["MM_TOKEN"]}?lang=de')
        page.wait_for_selector('h1')
        rg.untertitel('Jede teilnehmende Firma bekommt Vorschläge mit Begründung, fragt Gespräche an, und der Termin wird '
                      'automatisch eingeplant – mit Videoraum und Kalenderdatei.')
        rg.halte(4.2)
        rg.bild('matchmaking-agenda')
        if os.environ.get('ADMIN_PW'):
            page.goto(APP + '/login?next=/matchmaking/admin')
            page.fill('input[name=email]', os.environ['ADMIN_EMAIL'])
            page.fill('input[name=password]', os.environ['ADMIN_PW'])
            page.fill('input[name=code]', totp(os.environ['ADMIN_TOTP']))
            page.get_by_role('button', name='Anmelden').click()
            page.wait_for_selector('h1')
            hrefs = page.eval_on_selector_all('a[href^="/matchmaking/admin/"]', 'as => as.map(a => a.getAttribute("href"))')
            ziel = next((h for h in hrefs if h.rstrip('/').split('/')[-1].isdigit()), None)
            if ziel:
                page.goto(APP + ziel.rstrip('/') + '/gespraeche')
                page.wait_for_selector('h1')
            rg.untertitel('Die Veranstalterin sieht Anmeldungen, Gespräche und nach der Veranstaltung die Umfrage zu '
                          'Zufriedenheit, Nutzen und Umsatzchancen.', rolle='Veranstalter')
            rg.halte(4.0)
            rg.bild('matchmaking-dashboard')
            page.goto(APP + '/logout')

        # --- Telefon ---------------------------------------------------------------------------------------------------
        handy_ctx = browser.new_context(viewport={'width': 390, 'height': 780}, device_scale_factor=2, locale='de-DE',
                                        is_mobile=True, has_touch=True)
        hp = handy_ctx.new_page()
        hp.goto(APP + '/?lang=de&q=Hörgerät*')
        hp.wait_for_selector('table.res')
        tel = rg.bilder / 'mobil.png'
        hp.screenshot(path=str(tel))
        handy_ctx.close()
        rg.ohne_untertitel()
        rg.karte(telefonkarte('Mobil', 'Auf dem Telefon',
                              'Dieselbe Suche im Browser des Telefons. Einbettung per iframe in jede Website, Farben '
                              'und Logo nach Corporate Design.', Rundgang.als_data_url(tel), REPO, FUSS[1]), 3.6, stil=HELL)

        rg.karte(faktenkarte(f'Gemessen am {HEUTE}', 'Was heute schon läuft', [
            (punkt(health['verified']), 'registergeprüfte Unternehmen (EUDAMED, EMA, LEI)'),
            (punkt(kontakt['total']), 'davon mit Kontaktweg: Kontaktseite, Website oder Funktionspostfach'),
            (punkt(int(os.environ.get('INTL_N', '0'))), f'internationale Unternehmen aus {os.environ.get("INTL_COUNTRIES", "?")} Ländern für das Matchmaking'),
            ('0', 'Personendaten im Verzeichnis'),
        ], 'Quelle: /api/health und /api/v1 der laufenden Installation', FUSS[1]), 5.0)
        rg.karte(titelkarte('Selbst prüfen', 'In zwei Minuten ausprobieren',
                            'Demo öffnen, „Endoskop*“ suchen, ein Profil öffnen, oben auf EN schalten. '
                            'Quellcode offen, Apache 2.0.', REPO, 'Panagiotis Herbe', 'panoherbe.com'), 4.0)
        rg.schreibe()
        browser.close()


if __name__ == '__main__':
    main()
