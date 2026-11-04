"""Rundgang: scripted, captioned walkthrough videos of a web application.

A walkthrough is a list of PNG frames, each with a hold duration. Captions, a role chip, a pointer and title cards
are drawn into the page itself, so the video needs no audio and no subtitle track. `kodieren.sh` turns the frames
into MP4 (H.264), GIF and poster. Runs inside mcr.microsoft.com/playwright/python (image: Dockerfile.aufnahme next to this file).

Frames are captured at device scale factor 2 and scaled down by ffmpeg: text stays sharp in the 1280-px video, and
README screenshots (`bild()`) come from the same run at full resolution, without the caption overlay.
"""

import base64
import html
import json
from pathlib import Path

BREITE, HOEHE = 1280, 800
FPS = 25
SCHRIFT = "'Liberation Sans', 'DejaVu Sans', Arial, sans-serif"

_OVERLAY = r"""
(z) => {
  if (!document.getElementById('rg-untertitel')) {
    const st = document.createElement('style');
    st.textContent = `
      #rg-untertitel { position: fixed; left: 0; right: 0; bottom: 0; z-index: 2147483646; box-sizing: border-box;
        min-height: calc(86px * M); padding: calc(16px * M) calc(40px * M) calc(18px * M); display: flex;
        gap: calc(16px * M); align-items: center; background: rgba(15, 23, 42, .93); color: #fff;
        font: 600 calc(26px * M)/1.3 SCHRIFT; letter-spacing: .1px; box-shadow: 0 -4px 18px rgba(0,0,0,.22); }
      #rg-untertitel[hidden] { display: none; }
      #rg-untertitel .rg-rolle { flex: none; font: 700 calc(15px * M)/1 SCHRIFT; letter-spacing: .4px;
        text-transform: uppercase; color: #0f172a; background: #fbbf24; border-radius: 999px;
        padding: calc(8px * M) calc(12px * M); }
      #rg-untertitel .rg-klein { display: block; font-weight: 400; font-size: calc(18px * M); color: #cbd5e1;
        margin-top: calc(3px * M); }
      #rg-zeiger { position: fixed; z-index: 2147483647; width: calc(34px * M); height: calc(34px * M);
        pointer-events: none; margin: calc(-3px * M) 0 0 calc(-5px * M); filter: drop-shadow(0 2px 3px rgba(0,0,0,.35)); }
      #rg-zeiger svg { width: 100%; height: 100%; display: block; }
      #rg-zeiger[hidden], #rg-ring[hidden] { display: none; }
      #rg-ring { position: fixed; z-index: 2147483645; border: calc(4px * M) solid #f59e0b; border-radius: 50%;
        pointer-events: none; box-sizing: border-box; }
      html::after { content: ""; display: block; height: calc(96px * M); }  /* page end can scroll above the band */
    `.replaceAll('SCHRIFT', z.schrift).replaceAll('M)', z.m + ')');
    document.documentElement.appendChild(st);
    for (const id of ['rg-untertitel', 'rg-zeiger', 'rg-ring']) {
      const d = document.createElement('div'); d.id = id; d.setAttribute('aria-hidden', 'true');
      document.documentElement.appendChild(d);
    }
    document.getElementById('rg-zeiger').innerHTML = '<svg viewBox="0 0 24 24"><path d="M4 2.5 L4 19.5 L8.4 15.6 L11.3 22.2 L14.4 20.9 L11.6 14.4 L17.8 14.4 Z" fill="#0f172a" stroke="#fff" stroke-width="1.7" stroke-linejoin="round"/></svg>';
  }
  const u = document.getElementById('rg-untertitel');
  u.hidden = !z.text;
  u.innerHTML = z.text ? (z.rolle ? `<span class="rg-rolle">${z.rolle}</span>` : '') + `<span>${z.text}</span>` : '';
  const p = document.getElementById('rg-zeiger');
  p.hidden = !z.zeiger; p.style.left = z.x + 'px'; p.style.top = z.y + 'px';
  const r = document.getElementById('rg-ring');
  r.hidden = !z.ring;
  if (z.ring) { const q = z.ring * z.m; r.style.left = (z.x - q) + 'px'; r.style.top = (z.y - q) + 'px';
    r.style.width = r.style.height = (2 * q) + 'px'; r.style.opacity = z.ringAlpha; }
  return true;
}
"""

_KARTE = """<!doctype html><html lang="de"><head><meta charset="utf-8"><style>
  html, body {{ margin: 0; width: {b}px; height: {h}px; overflow: hidden; }}
  body {{ font-family: {schrift}; background: {hintergrund}; color: {farbe}; display: flex; flex-direction: column;
    justify-content: center; padding: 0 96px; box-sizing: border-box; }}
  .oben {{ font: 700 20px/1 {schrift}; letter-spacing: 2px; text-transform: uppercase; color: {akzent}; margin-bottom: 26px; }}
  h1 {{ font: 700 64px/1.08 {schrift}; margin: 0 0 22px; letter-spacing: -.5px; }}
  .unter {{ font: 400 31px/1.35 {schrift}; margin: 0 0 34px; max-width: 1040px; opacity: .92; }}
  .zeile {{ font: 600 22px/1.4 {schrift}; opacity: .8; }}
  .kacheln {{ display: grid; grid-template-columns: repeat({spalten}, 1fr); gap: 22px; margin-top: 8px; }}
  .kachel {{ background: {kachel}; border-radius: 16px; padding: 26px 26px 24px; }}
  .kachel b {{ display: block; font: 700 44px/1.05 {schrift}; color: {akzent2}; margin-bottom: 10px; }}
  .kachel span {{ display: block; font: 400 21px/1.35 {schrift}; }}
  .fuss {{ position: absolute; left: 96px; right: 96px; bottom: 54px; display: flex; justify-content: space-between;
    font: 600 21px/1 {schrift}; opacity: .75; }}
  .geraet {{ display: flex; gap: 64px; align-items: center; }}
  .geraet .text {{ flex: 1; }}
  .telefon {{ flex: none; width: 262px; height: 566px; margin-bottom: 40px; border-radius: 38px; background: #0f172a; padding: 12px;
    box-shadow: 0 18px 40px rgba(15,23,42,.35); }}
  .telefon img {{ width: 100%; height: 100%; object-fit: cover; object-position: top; border-radius: 27px; display: block; }}
</style></head><body>{inhalt}</body></html>"""

DUNKEL = dict(hintergrund='#0f172a', farbe='#f8fafc', akzent='#fbbf24', akzent2='#fbbf24', kachel='#1e293b')
HELL = dict(hintergrund='#f8fafc', farbe='#0f172a', akzent='#b45309', akzent2='#1d4ed8', kachel='#e2e8f0')


class Rundgang:
    """Collects frames from a Playwright page. All coordinates are CSS pixels of the viewport.

    `pixel_breite` is the frame width in device pixels (viewport width x device scale factor). Cards are laid out
    at 1280x800 CSS px in their own context whose scale factor gives the same frame size; the overlay is scaled
    by viewport width / 1280, so captions have the same size relative to the frame everywhere.
    """

    def __init__(self, page, ausgabe, *, bilder=None):
        self.page = page
        vp = page.viewport_size
        self.massstab = vp['width'] / BREITE
        self.pixel_dsf = page.evaluate('window.devicePixelRatio') * self.massstab  # card DSF
        self.aus = Path(ausgabe)
        (self.aus / 'frames').mkdir(parents=True, exist_ok=True)
        self.bilder = Path(bilder) if bilder else self.aus / 'bilder'
        self.bilder.mkdir(parents=True, exist_ok=True)
        self.frames = []  # (datei, sekunden)
        self.zustand = dict(text='', rolle='', zeiger=False, x=vp['width'] // 2, y=vp['height'] // 2, ring=0,
                            ringAlpha=1, schrift=SCHRIFT, m=round(self.massstab, 4))
        self.kapitel = []  # (sekunde, text) for the caption list in README/medien

    # --- drawing -------------------------------------------------------------------------------------------------
    def _zeichne(self, seite=None):
        (seite or self.page).evaluate(_OVERLAY, self.zustand)

    def foto(self, sekunden, seite=None):
        """One frame, held for `sekunden`."""
        seite = seite or self.page
        if seite is self.page:
            self._zeichne()
        datei = self.aus / 'frames' / f'f{len(self.frames):05d}.png'
        datei.write_bytes(seite.screenshot(type='png'))
        self.frames.append((datei.name, round(sekunden, 3)))

    def halte(self, sekunden):
        self.foto(sekunden)

    def untertitel(self, text, rolle=None, klein=None, halte=0.0):
        t = html.escape(text)
        if klein:
            t += f'<span class="rg-klein">{html.escape(klein)}</span>'
        self.zustand['text'] = t
        if rolle is not None:
            self.zustand['rolle'] = html.escape(rolle)
        self.kapitel.append((round(self.dauer(), 1), text + (f' ({klein})' if klein else '')))
        if halte:
            self.foto(halte)

    def ohne_untertitel(self):
        self.zustand['text'] = ''

    # --- pointer -------------------------------------------------------------------------------------------------
    def zeige_zeiger(self, an=True):
        self.zustand['zeiger'] = an

    def bewege(self, x, y, sekunden=0.35):
        self.zustand['zeiger'] = True
        x0, y0 = self.zustand['x'], self.zustand['y']
        schritte = max(3, round(sekunden * FPS))
        for i in range(1, schritte + 1):
            t = i / schritte
            t = t * t * (3 - 2 * t)  # smoothstep
            self.zustand['x'], self.zustand['y'] = round(x0 + (x - x0) * t), round(y0 + (y - y0) * t)
            self.foto(1 / FPS)

    def _mitte(self, locator):
        locator.scroll_into_view_if_needed()
        b = locator.bounding_box()
        return b['x'] + min(b['width'] / 2, 60), b['y'] + b['height'] / 2

    def zeige_auf(self, locator, sekunden=0.35):
        x, y = self._mitte(locator)
        self.bewege(x, y, sekunden)

    def klick(self, locator, danach=0.5, warte=None):
        """Move the pointer to the element, show a ripple, click for real, wait for `warte` (selector or callable)."""
        self.zeige_auf(locator)
        for r, a in ((10, 1), (17, .85), (24, .6), (30, .3)):
            self.zustand['ring'], self.zustand['ringAlpha'] = r, a
            self.foto(1 / FPS)
        self.zustand['ring'] = 0
        locator.click()
        if callable(warte):
            warte()
        elif warte:
            self.page.wait_for_selector(warte)
        self.page.wait_for_timeout(120)
        if danach:
            self.foto(danach)

    def tippe(self, locator, text, je_zeichen=0.07, danach=0.3):
        self.klick(locator, danach=0)
        for ch in text:
            self.page.keyboard.type(ch)
            self.foto(je_zeichen)
        if danach:
            self.foto(danach)

    def scrolle(self, y, sekunden=0.8):
        y0 = self.page.evaluate('window.scrollY')
        schritte = max(3, round(sekunden * FPS))
        for i in range(1, schritte + 1):
            t = i / schritte
            t = t * t * (3 - 2 * t)
            self.page.evaluate(f'window.scrollTo(0, {round(y0 + (y - y0) * t)})')
            self.foto(1 / FPS)

    # --- cards and stills ----------------------------------------------------------------------------------------
    def karte(self, inhalt, sekunden, stil=DUNKEL, spalten=2, seite=None):
        """A full-frame card (title, facts, end) rendered on its own page."""
        s = seite or self._kartenseite()
        s.set_content(_KARTE.format(b=BREITE, h=HOEHE, schrift=SCHRIFT, spalten=spalten, inhalt=inhalt, **stil))
        s.wait_for_timeout(80)
        self.kapitel.append((round(self.dauer(), 1), '[Karte] ' + html.unescape(_nur_text(inhalt))))
        self.foto(sekunden, seite=s)

    def _kartenseite(self):
        if not hasattr(self, '_karten'):
            ctx = self.page.context.browser.new_context(viewport={'width': BREITE, 'height': HOEHE},
                                                        device_scale_factor=self.pixel_dsf, locale='de-DE')
            self._karten = ctx.new_page()
        return self._karten

    def bild(self, name, seite=None, ganz=False):
        """README screenshot without overlay, at the context's device scale factor."""
        seite = seite or self.page
        if seite is self.page:
            alt = dict(self.zustand)
            self.zustand.update(text='', zeiger=False, ring=0)
            self._zeichne()
        pfad = self.bilder / f'{name}.png'
        seite.screenshot(path=str(pfad), full_page=ganz)
        if seite is self.page:
            self.zustand.update(alt)
        return pfad

    @staticmethod
    def als_data_url(pfad):
        return 'data:image/png;base64,' + base64.b64encode(Path(pfad).read_bytes()).decode()

    # --- output --------------------------------------------------------------------------------------------------
    def dauer(self):
        return sum(d for _, d in self.frames)

    def schreibe(self):
        zeilen = ['ffconcat version 1.0']
        for datei, d in self.frames:
            zeilen += [f"file 'frames/{datei}'", f'duration {d}']
        zeilen.append(f"file 'frames/{self.frames[-1][0]}'")  # concat demuxer: last file once more
        (self.aus / 'frames.txt').write_text('\n'.join(zeilen) + '\n')
        (self.aus / 'kapitel.json').write_text(json.dumps(
            {'dauer_s': round(self.dauer(), 1), 'frames': len(self.frames), 'kapitel': self.kapitel},
            ensure_ascii=False, indent=1))
        print(f'{len(self.frames)} Frames, {self.dauer():.1f} s')


def _nur_text(s):
    import re
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', s)).strip()[:160]


def titelkarte(oben, titel, unter, zeile, fuss_links, fuss_rechts):
    e = html.escape
    return (f'<div class="oben">{e(oben)}</div><h1>{e(titel)}</h1><p class="unter">{e(unter)}</p>'
            f'<div class="zeile">{e(zeile)}</div>'
            f'<div class="fuss"><span>{e(fuss_links)}</span><span>{e(fuss_rechts)}</span></div>')


def faktenkarte(oben, titel, kacheln, fuss_links, fuss_rechts):
    e = html.escape
    k = ''.join(f'<div class="kachel"><b>{e(z)}</b><span>{e(t)}</span></div>' for z, t in kacheln)
    return (f'<div class="oben">{e(oben)}</div><h1 style="font-size:46px">{e(titel)}</h1><div class="kacheln">{k}</div>'
            f'<div class="fuss"><span>{e(fuss_links)}</span><span>{e(fuss_rechts)}</span></div>')


def telefonkarte(oben, titel, unter, bild_data_url, fuss_links, fuss_rechts):
    e = html.escape
    return (f'<div class="geraet"><div class="text"><div class="oben">{e(oben)}</div><h1 style="font-size:52px">{e(titel)}</h1>'
            f'<p class="unter">{e(unter)}</p></div><div class="telefon"><img src="{bild_data_url}" alt=""></div></div>'
            f'<div class="fuss"><span>{e(fuss_links)}</span><span>{e(fuss_rechts)}</span></div>')
