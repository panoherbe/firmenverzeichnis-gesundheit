"""
One-click test access for evaluators of a demo installation: /test signs in a demo account without typing a password.

Active only with env DEMO_LOGIN=1 (never on a customer installation). Two demo accounts (created by ops.demo_seed):
  demo@example.org        role user  - saved searches, search history, exports
  demo-admin@example.org  role admin - matchmaking organiser view
Guards: demo accounts cannot open user management, the access log or the support admin (403); the page is not
linked anywhere and sends `X-Robots-Tag: noindex`; ops/demo_reset.sh restores the demo data every night, so nothing a
tester changes lasts. The one-click sign-in skips the second factor for these two demo accounts only.
"""

from __future__ import annotations

import os
import time

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web import auth
from web.common import db, register, templates

ENABLED = os.environ.get("DEMO_LOGIN") == "1"
ACCOUNTS = {"nutzerin": ("demo@example.org", "/konto"), "veranstalterin": ("demo-admin@example.org", "/matchmaking/admin")}
DEMO_EMAILS = {e for e, _ in ACCOUNTS.values()}
BLOCKED = ("/admin/nutzer", "/admin/support")

register({
    "Testzugang": "Test access", "Als GTAI-Nutzerin testen": "Test as a GTAI user",
    "Als Veranstalterin testen": "Test as event organiser",
    "Ein Klick meldet ein Demo-Konto an. Kein Passwort nötig.": "One click signs in a demo account. No password needed.",
    "In zwei Minuten": "In two minutes",
    "Gespeicherte Suche: oben „Endoskop*“ suchen, links „Baden-Württemberg“ wählen, über der Liste „Suche speichern“.":
        "Saved search: search “Endoskop*” at the top, choose “Baden-Württemberg” on the left, then “Save search” above the list.",
    "Export: „Treffer als Excel“ oder „Statistik“ und dort eine Grafik als JPG.":
        "Export: “Results as Excel” or “Statistics” and a chart as JPG.",
    "Matchmaking: als Veranstalterin die Beispielveranstaltung öffnen, Reiter „Gespräche“ und „Auswertung“.":
        "Matchmaking: as organiser open the example event, tabs “Meetings” and “Report”.",
    "Nur Demodaten. Änderungen werden jede Nacht zurückgesetzt. Nutzerverwaltung und Protokolle sind für Demo-Konten gesperrt.":
        "Demo data only. Changes are reset every night. User management and logs are locked for demo accounts.",
    "In der Demo gesperrt.": "Locked in the demo.",
})

router = APIRouter()


@router.get("/test", response_class=HTMLResponse)
def test_page(request: Request):
    if not ENABLED:
        raise HTTPException(404)
    resp = templates.TemplateResponse(request, "test.html", dict(embed=False))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


@router.post("/test/anmelden")
def test_login(request: Request, als: str = Form(...)):
    if not ENABLED or als not in ACCOUNTS:
        raise HTTPException(404)
    email, target = ACCOUNTS[als]
    with db() as conn:
        u = conn.execute("SELECT id FROM app_user WHERE email = %s AND active", (email,)).fetchone()
    if not u:
        raise HTTPException(503, "demo account missing")
    resp = RedirectResponse(target, status_code=303)
    resp.set_cookie(auth.COOKIE, auth.sign({"uid": u["id"], "exp": time.time() + 2 * 3600}), max_age=2 * 3600,
                    httponly=True, secure=request.url.scheme == "https", samesite="lax")
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def blocked(request: Request) -> bool:
    """True for a demo account on a path the demo locks (user management, access log, support admin)."""
    if not ENABLED or not request.url.path.startswith(BLOCKED):
        return False
    user = auth.current_user(request)
    return bool(user and user["email"] in DEMO_EMAILS)
