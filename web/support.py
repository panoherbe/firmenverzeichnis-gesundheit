"""
Support desk for the SLA (tender LB §2.1): public ticket form, secret status link, automatic acknowledgement,
phone alert for P1/P2, admin list with measurable reaction times, and the public /status page.

Routes:
    GET/POST /support                 ticket form (category, priority with plain-language criteria, description, e-mail)
    GET      /support/t/{token}       ticket status for the requester (secret link, no login)
    GET      /admin/support           ticket list (admin, 2FA via web.auth)
    GET/POST /admin/support/{id}      ticket detail: status, priority, public reply / internal note
    GET      /status, /status.json    status page written by the external monitor (ops/monitor.py), if present
    GET      /api/health/ops          disk, last backup (for the external monitor; token-protected if OPS_HEALTH_TOKEN)

Reaction time = first action by a human admin (status change or reply), NOT the automatic acknowledgement e-mail.
Deadlines are business hours (ops/bizhours.py: Mo–Fr 08–17 Europe/Berlin, Hamburg holidays).

Personal data: the requester's e-mail (customer staff; Art. 6(1)(b) GDPR, contract support) is stored only to answer
the request and is deleted RETENTION_DAYS (default 90) after the ticket is resolved (`python -m web.support purge`,
run daily by ops/prune.py). The ticket itself (number, priority, times) is kept for the SLA reports.

Env: SMTP_HOST, SMTP_PORT (587 STARTTLS / 465 SSL), SMTP_USER, SMTP_PASSWORD, SMTP_FROM, SUPPORT_NOTIFY_TO
(optional copy of every new ticket), PUBLIC_BASE_URL (for links in e-mails), STATUS_DIR, OPS_HEALTH_TOKEN,
SUPPORT_RETENTION_DAYS. Alert channels: see ops/alert.py.

    python -m web.support schema   # create tables
    python -m web.support purge    # delete requester e-mails of tickets resolved > RETENTION_DAYS ago
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import smtplib
import ssl
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from ops.alert import send_alert
from ops.bizhours import TARGET_HOURS, TZ, business_hours_between, deadline
from web.auth import ip_net, require_admin
from web.common import _, db, lang, register, templates

ROOT = Path(__file__).resolve().parents[1]
RETENTION_DAYS = int(os.environ.get("SUPPORT_RETENTION_DAYS", "90"))
STATUS_DIR = Path(os.environ.get("STATUS_DIR", ROOT / "var" / "status"))
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", ROOT / "var" / "backup"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS support_ticket (
  id serial PRIMARY KEY,
  number text UNIQUE,
  token_hash text UNIQUE NOT NULL,              -- sha256 of the secret status link; the token itself is never stored
  category text NOT NULL,
  priority_requested text NOT NULL CHECK (priority_requested IN ('P1','P2','P3','routine')),
  priority text NOT NULL CHECK (priority IN ('P1','P2','P3','routine')),  -- current (admin may reclassify)
  subject text NOT NULL,
  description text NOT NULL,
  contact_email text,                           -- personal data; NULLed RETENTION_DAYS after resolution
  lang text NOT NULL DEFAULT 'de',
  status text NOT NULL DEFAULT 'neu' CHECK (status IN ('neu','in_bearbeitung','wartet','geloest','geschlossen')),
  created_at timestamptz NOT NULL DEFAULT now(),
  due_at timestamptz NOT NULL,                  -- reaction deadline in business hours
  ack_sent_at timestamptz,                      -- automatic acknowledgement e-mail (not a reaction)
  alert_sent_at timestamptz,                    -- phone alert to the contractor (P1/P2)
  first_reaction_at timestamptz,                -- first human action: the SLA measure
  resolved_at timestamptz,
  email_purged_at timestamptz);
CREATE INDEX IF NOT EXISTS support_ticket_created ON support_ticket (created_at);
CREATE TABLE IF NOT EXISTS support_event (
  id bigserial PRIMARY KEY,
  ticket_id int NOT NULL REFERENCES support_ticket(id) ON DELETE CASCADE,
  at timestamptz NOT NULL DEFAULT now(),
  actor text NOT NULL,                          -- 'system', 'requester' or 'admin:<user id>'
  kind text NOT NULL,                           -- created, ack, alert, status, priority, reply, note
  old_value text, new_value text,
  note text,
  public boolean NOT NULL DEFAULT false);       -- shown on the requester's status page
CREATE INDEX IF NOT EXISTS support_event_ticket ON support_event (ticket_id, at);
"""

CATEGORIES = {
    "stoerung": "Störung (etwas funktioniert nicht)",
    "daten": "Datenfehler in einem Profil",
    "konto": "Nutzerkonto / Anmeldung",
    "export_api": "Export oder API",
    "einbindung": "Einbindung (iframe) / Design",
    "frage": "Frage / Routineanfrage",
    "sonstiges": "Sonstiges",
}
PRIORITIES = {
    "P1": ("Priorität 1 – geschäftskritisch",
           "Das Verzeichnis oder die eingebettete Ansicht ist für alle Nutzenden nicht erreichbar oder nicht benutzbar."),
    "P2": ("Priorität 2 – wichtige Funktion gestört",
           "Eine wichtige Funktion liefert falsche Ergebnisse oder fällt aus, z. B. Suche, Export oder API; "
           "das Verzeichnis ist sonst erreichbar."),
    "P3": ("Priorität 3 – geringe Störung",
           "Kleiner Fehler mit Umweg, z. B. Darstellungsfehler, einzelnes falsches Profil, Tippfehler."),
    "routine": ("Routineanfrage",
                "Frage, Wunsch, Änderung oder Auskunft ohne Störung."),
}
STATUSES = {"neu": "Neu", "in_bearbeitung": "In Bearbeitung", "wartet": "Wartet auf Rückmeldung",
            "geloest": "Gelöst", "geschlossen": "Geschlossen"}
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[A-Za-z]{2,24}$")

register({
    "Support": "Support", "Support-Anfrage": "Support request", "Anfrage senden": "Send request",
    "Kategorie": "Category", "Priorität": "Priority", "Betreff": "Subject", "Beschreibung": "Description",
    "Ihre E-Mail-Adresse (dienstlich)": "Your e-mail address (work)",
    "Was ist passiert? Was haben Sie erwartet? Welche Seite, welche Suche?":
        "What happened? What did you expect? Which page, which search?",
    "Bitte keine personenbezogenen Daten Dritter in die Beschreibung schreiben.":
        "Please do not include personal data of third parties in the description.",
    "Datenschutzhinweis": "Privacy notice",
    "Wir speichern Ihre E-Mail-Adresse nur, um diese Anfrage zu beantworten (Art. 6 Abs. 1 lit. b DSGVO, "
    "Vertragserfüllung). Sie wird 90 Tage nach Abschluss der Anfrage gelöscht. Nummer, Priorität und Zeitstempel "
    "der Anfrage bleiben ohne E-Mail-Adresse für den monatlichen SLA-Bericht erhalten.":
        "We store your e-mail address only to answer this request (Art. 6(1)(b) GDPR, performance of the contract). "
        "It is deleted 90 days after the request is closed. Number, priority and timestamps of the request are kept "
        "without the e-mail address for the monthly SLA report.",
    "Ich habe den Datenschutzhinweis gelesen.": "I have read the privacy notice.",
    "Wie dringend ist es?": "How urgent is it?",
    "Antwortzeiten (Servicezeit Mo–Fr 08–17 Uhr, ohne Feiertage in Hamburg)":
        "Reaction times (service hours Mon–Fri 08:00–17:00 CET, excluding Hamburg public holidays)",
    "8 Stunden": "8 hours", "12 Stunden": "12 hours", "24 Stunden": "24 hours", "3 Arbeitstage": "3 working days",
    "Bei Priorität 1 und 2 wird der Bereitschaftsdienst sofort per Telefon-Alarm benachrichtigt, auch außerhalb der Servicezeit.":
        "For priority 1 and 2 the on-call contact is alerted by phone immediately, also outside service hours.",
    "Bitte füllen Sie alle Pflichtfelder aus.": "Please fill in all required fields.",
    "Bitte eine gültige E-Mail-Adresse angeben.": "Please enter a valid e-mail address.",
    "Zu viele Anfragen. Bitte später erneut versuchen.": "Too many requests. Please try again later.",
    "Anfrage eingegangen": "Request received", "Ihre Ticketnummer": "Your ticket number",
    "Statuslink (bitte speichern – nur wer den Link kennt, sieht die Anfrage)":
        "Status link (please save it – only people who have the link can see the request)",
    "Eine Bestätigung wurde an Ihre E-Mail-Adresse gesendet.": "A confirmation was sent to your e-mail address.",
    "Es wurde keine Bestätigungs-E-Mail versendet. Bitte notieren Sie Ticketnummer und Statuslink.":
        "No confirmation e-mail was sent. Please note the ticket number and the status link.",
    "Reaktion spätestens": "Reaction due by", "Status": "Status", "Eingegangen": "Received",
    "Erste Reaktion": "First reaction", "Gelöst": "Resolved", "Verlauf": "History", "offen": "open",
    "Neu": "New", "In Bearbeitung": "In progress", "Wartet auf Rückmeldung": "Waiting for your reply",
    "Geschlossen": "Closed", "Anfrage nicht gefunden.": "Request not found.",
    "Antwort / Notiz": "Reply / note", "öffentlich (für Anfragende sichtbar, per E-Mail)":
        "public (visible to the requester, sent by e-mail)", "Speichern": "Save", "Ticket": "Ticket",
    "Frist": "Due", "eingehalten": "met", "überschritten": "missed", "Alle": "All", "Offene": "Open",
    "Support-Tickets": "Support tickets", "angefragt": "requested", "Servicestatus": "Service status",
    "Status nicht verfügbar.": "Status not available.",
    "Störung (etwas funktioniert nicht)": "Incident (something does not work)",
    "Datenfehler in einem Profil": "Data error in a profile", "Nutzerkonto / Anmeldung": "User account / sign-in",
    "Export oder API": "Export or API", "Einbindung (iframe) / Design": "Embedding (iframe) / design",
    "Frage / Routineanfrage": "Question / routine request", "Sonstiges": "Other",
    "Priorität 1 – geschäftskritisch": "Priority 1 – business-critical",
    "Das Verzeichnis oder die eingebettete Ansicht ist für alle Nutzenden nicht erreichbar oder nicht benutzbar.":
        "The directory or the embedded view is unreachable or unusable for all users.",
    "Priorität 2 – wichtige Funktion gestört": "Priority 2 – major function broken",
    "Eine wichtige Funktion liefert falsche Ergebnisse oder fällt aus, z. B. Suche, Export oder API; "
    "das Verzeichnis ist sonst erreichbar.":
        "A major function returns wrong results or fails, e.g. search, export or API; the directory is otherwise "
        "reachable.",
    "Priorität 3 – geringe Störung": "Priority 3 – minor issue",
    "Kleiner Fehler mit Umweg, z. B. Darstellungsfehler, einzelnes falsches Profil, Tippfehler.":
        "Minor fault with a workaround, e.g. display error, a single wrong profile, a typo.",
    "Routineanfrage": "Routine request",
    "Frage, Wunsch, Änderung oder Auskunft ohne Störung.": "Question, wish, change or information, no fault.",
})


# ---------- helpers ----------

def now() -> datetime:
    return datetime.now(timezone.utc)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def base_url(request: Request) -> str:
    return os.environ.get("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")


_hits: dict[str, deque] = defaultdict(deque)


def rate_limited(request: Request, limit: int = 5, window: int = 3600) -> bool:
    """In-process limit per /24 network (per worker; good enough against form spam, not a DoS defence)."""
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
    key = ip_net(ip) or "?"
    q, t = _hits[key], time.time()
    while q and q[0] < t - window:
        q.popleft()
    if len(q) >= limit:
        return True
    q.append(t)
    return False


def smtp_configured() -> bool:
    return bool(os.environ.get("SMTP_HOST") and os.environ.get("SMTP_FROM"))


def send_mail(to: str, subject: str, body: str) -> bool:
    """Plain-text mail via the configured SMTP relay. Returns False (never raises) when not configured or failing."""
    if not (smtp_configured() and to):
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = os.environ["SMTP_FROM"], to, subject
    msg.set_content(body)
    host, port = os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", "587"))
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, context=ctx, timeout=20)
        else:
            s = smtplib.SMTP(host, port, timeout=20)
            s.starttls(context=ctx)
        with s:
            if user:
                s.login(user, pw or "")
            s.send_message(msg)
        return True
    except Exception as e:  # noqa: BLE001
        print(f"support: mail to requester failed: {type(e).__name__}", file=sys.stderr)
        return False


def event(conn, tid: int, actor: str, kind: str, old=None, new=None, note=None, public=False) -> None:
    conn.execute("INSERT INTO support_event (ticket_id, actor, kind, old_value, new_value, note, public) "
                 "VALUES (%s,%s,%s,%s,%s,%s,%s)", (tid, actor, kind, old, new, note, public))


def sla_view(t: dict) -> dict:
    """Reaction time in business hours and whether the deadline was met (None while open and not yet due)."""
    end = t.get("first_reaction_at")
    hours = business_hours_between(t["created_at"], end or now())
    met = (end <= t["due_at"]) if end else (False if now() > t["due_at"] else None)
    return dict(reaction_hours=round(hours, 2), target_hours=TARGET_HOURS[t["priority"]], met=met)


# ---------- background work after a new ticket ----------

def after_create(tid: int, number: str, email: str, prio: str, subject: str, category: str,
                 description: str, link: str, due: datetime, lang_: str) -> None:
    due_local = due.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    if prio in ("P1", "P2"):
        res = send_alert(f"Ticket {number} {prio}: {subject[:80]}",
                         f"{CATEGORIES.get(category, category)}\n{description[:600]}\nReaktion bis {due_local}",
                         level=prio, key=f"ticket:{number}")
        if any(v == "ok" for v in res.values()):
            with db() as conn:
                conn.execute("UPDATE support_ticket SET alert_sent_at = now() WHERE id = %s", (tid,))
                event(conn, tid, "system", "alert", note=json.dumps(res))
    en = lang_ == "en"
    body = (f"{'Thank you, we have received your request.' if en else 'Vielen Dank, wir haben Ihre Anfrage erhalten.'}\n\n"
            f"{'Ticket number' if en else 'Ticketnummer'}: {number}\n"
            f"{'Priority' if en else 'Priorität'}: {prio}\n"
            f"{'Reaction due by' if en else 'Reaktion spätestens'}: {due_local} (Europe/Berlin)\n"
            f"{'Status' if en else 'Status'}: {link}\n\n"
            f"{'This is an automatic acknowledgement. Please do not reply to this e-mail; use the status link.' if en else 'Dies ist eine automatische Eingangsbestätigung. Bitte antworten Sie nicht auf diese E-Mail, sondern nutzen Sie den Statuslink.'}\n")
    if send_mail(email, f"[{number}] {'Request received' if en else 'Anfrage eingegangen'}: {subject[:80]}", body):
        with db() as conn:
            conn.execute("UPDATE support_ticket SET ack_sent_at = now() WHERE id = %s", (tid,))
            event(conn, tid, "system", "ack")
    notify = os.environ.get("SUPPORT_NOTIFY_TO")
    if notify:
        send_mail(notify, f"[{number}] neues Ticket {prio}: {subject[:80]}",
                  f"{CATEGORIES.get(category, category)}\nFrist: {due_local}\n\n{description}\n")


# ---------- public routes ----------

router = APIRouter()


def _form(request: Request, **kw):
    ctx = dict(categories=CATEGORIES, priorities=PRIORITIES, errors=[], values={}, embed=False)
    ctx.update(kw)
    return templates.TemplateResponse(request, "support_form.html", ctx, status_code=400 if ctx["errors"] else 200)


@router.get("/support", response_class=HTMLResponse)
def support_form(request: Request, embed: bool = False):
    return _form(request, embed=embed)


@router.post("/support", response_class=HTMLResponse)
def support_submit(request: Request, background: BackgroundTasks, category: str = Form(""),
                   priority: str = Form(""), subject: str = Form(""), description: str = Form(""),
                   email: str = Form(""), consent: str = Form(""), website: str = Form(""), embed: bool = Form(False)):
    values = dict(category=category, priority=priority, subject=subject, description=description, email=email)
    if website:  # honeypot: humans never see this field; answer like a success, store nothing
        return templates.TemplateResponse(request, "support_done.html", dict(
            number="—", link="", mailed=False, due=None, embed=embed))
    errors = []
    if category not in CATEGORIES or priority not in PRIORITIES or not subject.strip() or \
            len(description.strip()) < 10 or not consent:
        errors.append(_("Bitte füllen Sie alle Pflichtfelder aus."))
    if not EMAIL_RE.match(email.strip()):
        errors.append(_("Bitte eine gültige E-Mail-Adresse angeben."))
    if errors:
        return _form(request, errors=errors, values=values, embed=embed)
    if rate_limited(request):
        return _form(request, errors=[_("Zu viele Anfragen. Bitte später erneut versuchen.")], values=values,
                     embed=embed)
    token = secrets.token_urlsafe(24)
    created = now()
    due = deadline(created, priority)
    with db() as conn:
        with conn.transaction():
            t = conn.execute(
                "INSERT INTO support_ticket (token_hash, category, priority_requested, priority, subject, description,"
                " contact_email, lang, created_at, due_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (token_hash(token), category, priority, priority, subject.strip()[:200], description.strip()[:8000],
                 email.strip()[:254], lang(), created, due)).fetchone()
            number = f"{created.astimezone(TZ).year}-{t['id']:04d}"
            conn.execute("UPDATE support_ticket SET number = %s WHERE id = %s", (number, t["id"]))
            event(conn, t["id"], "requester", "created", new=priority)
    link = f"{base_url(request)}/support/t/{token}"
    background.add_task(after_create, t["id"], number, email.strip(), priority, subject.strip(), category,
                        description.strip(), link, due, lang())
    return templates.TemplateResponse(request, "support_done.html", dict(
        number=number, link=link, mailed=smtp_configured(), due=due.astimezone(TZ), priority=priority, embed=embed))


@router.get("/support/t/{token}", response_class=HTMLResponse)
def ticket_status(request: Request, token: str, embed: bool = False):
    with db() as conn:
        t = conn.execute("SELECT * FROM support_ticket WHERE token_hash = %s", (token_hash(token),)).fetchone()
        if not t:
            raise HTTPException(404, _("Anfrage nicht gefunden."))
        events = conn.execute("SELECT at, kind, new_value, note FROM support_event WHERE ticket_id = %s "
                              "AND public ORDER BY at", (t["id"],)).fetchall()
    return templates.TemplateResponse(request, "support_ticket.html", dict(
        t=t, events=events, statuses=STATUSES, categories=CATEGORIES, priorities=PRIORITIES, tz=TZ, embed=embed))


# ---------- admin ----------

@router.get("/admin/support", response_class=HTMLResponse)
def admin_list(request: Request, alle: bool = False, admin: dict = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute(
            "SELECT id, number, created_at, priority, priority_requested, category, subject, status, due_at, "
            "first_reaction_at, resolved_at, alert_sent_at, ack_sent_at FROM support_ticket "
            + ("" if alle else "WHERE status NOT IN ('geloest','geschlossen') ")
            + "ORDER BY (status IN ('geloest','geschlossen')), due_at LIMIT 500").fetchall()
    for r in rows:
        r.update(sla_view(r))
    return templates.TemplateResponse(request, "support_admin.html", dict(
        rows=rows, alle=alle, statuses=STATUSES, tz=TZ, embed=False))


@router.get("/admin/support/{tid}", response_class=HTMLResponse)
def admin_ticket(request: Request, tid: int, admin: dict = Depends(require_admin)):
    with db() as conn:
        t = conn.execute("SELECT * FROM support_ticket WHERE id = %s", (tid,)).fetchone()
        if not t:
            raise HTTPException(404)
        events = conn.execute("SELECT * FROM support_event WHERE ticket_id = %s ORDER BY at", (tid,)).fetchall()
    t.update(sla_view(t))
    return templates.TemplateResponse(request, "support_admin_ticket.html", dict(
        t=t, events=events, statuses=STATUSES, categories=CATEGORIES, priorities=PRIORITIES, tz=TZ, embed=False))


@router.post("/admin/support/{tid}")
def admin_update(request: Request, background: BackgroundTasks, tid: int, status: str = Form(""),
                 priority: str = Form(""), note: str = Form(""), public: str = Form(""),
                 admin: dict = Depends(require_admin)):
    actor = f"admin:{admin['id']}"
    mail = None
    with db() as conn:
        with conn.transaction():
            t = conn.execute("SELECT * FROM support_ticket WHERE id = %s FOR UPDATE", (tid,)).fetchone()
            if not t:
                raise HTTPException(404)
            acted = False
            if status in STATUSES and status != t["status"]:
                event(conn, tid, actor, "status", t["status"], status, public=True)
                conn.execute("UPDATE support_ticket SET status = %s, resolved_at = CASE WHEN %s IN ('geloest',"
                             "'geschlossen') THEN coalesce(resolved_at, now()) ELSE NULL END WHERE id = %s",
                             (status, status, tid))
                acted = True
            if priority in PRIORITIES and priority != t["priority"]:
                # reclassification by the agreed criteria; the deadline follows the new priority from receipt
                event(conn, tid, actor, "priority", t["priority"], priority, public=True)
                conn.execute("UPDATE support_ticket SET priority = %s, due_at = %s WHERE id = %s",
                             (priority, deadline(t["created_at"], priority), tid))
            if note.strip():
                is_public = bool(public)
                event(conn, tid, actor, "reply" if is_public else "note", note=note.strip()[:8000], public=is_public)
                acted = acted or is_public
                if is_public and t["contact_email"]:
                    mail = (t["contact_email"], f"[{t['number']}] {t['subject'][:80]}",
                            f"{note.strip()}\n\nStatus: {base_url(request)}/support (Statuslink aus der "
                            f"Eingangsbestätigung)\n")
            if acted and not t["first_reaction_at"]:
                conn.execute("UPDATE support_ticket SET first_reaction_at = now() WHERE id = %s", (tid,))
    if mail:
        background.add_task(send_mail, *mail)
    return RedirectResponse(f"/admin/support/{tid}", status_code=303)


# ---------- status page (written by ops/monitor.py on the standby) and ops health ----------

@router.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    f = STATUS_DIR / f"status.{'en' if lang() == 'en' else 'de'}.html"
    if f.exists():
        return FileResponse(f, media_type="text/html; charset=utf-8", headers={"Cache-Control": "max-age=30"})
    if os.environ.get("STATUS_URL"):
        return RedirectResponse(os.environ["STATUS_URL"], status_code=302)
    return HTMLResponse(f"<p>{_('Status nicht verfügbar.')}</p>", status_code=404)


@router.get("/status.json")
def status_json():
    f = STATUS_DIR / "status.json"
    if f.exists():
        return FileResponse(f, media_type="application/json", headers={"Cache-Control": "max-age=30"})
    return JSONResponse({"available": False}, status_code=404)


def _last_jsonl(path: Path) -> dict | None:
    try:
        lines = path.read_text().strip().splitlines()
        return json.loads(lines[-1]) if lines else None
    except (OSError, ValueError):
        return None


@router.get("/api/health/ops")
def ops_health(request: Request):
    expected = os.environ.get("OPS_HEALTH_TOKEN")
    if expected and not hmac.compare_digest(request.headers.get("x-ops-token", ""), expected):
        raise HTTPException(403)
    du = shutil.disk_usage(ROOT)
    last = _last_jsonl(BACKUP_DIR / "backup.jsonl")
    with db() as conn:
        conn.execute("SELECT 1")
    return {"ok": True, "disk_used_pct": round(100 * du.used / du.total, 1),
            "disk_free_gb": round(du.free / 1e9, 1),
            "last_backup_at": last.get("at") if last else None, "last_backup_ok": last.get("ok") if last else None}


# ---------- CLI ----------

def purge() -> int:
    with db() as conn:
        n = conn.execute(
            "UPDATE support_ticket SET contact_email = NULL, email_purged_at = now() WHERE contact_email IS NOT NULL "
            "AND resolved_at < now() - make_interval(days => %s)", (RETENTION_DAYS,)).rowcount
    return n


if __name__ == "__main__":
    if sys.argv[1:2] == ["schema"]:
        with db() as conn:
            conn.execute(SCHEMA)
        print("support schema ok")
    elif sys.argv[1:2] == ["purge"]:
        print(f"support: {purge()} requester e-mails deleted (resolved > {RETENTION_DAYS} days ago)")
    else:
        print(__doc__)
