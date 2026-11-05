"""
Named user accounts (3 to MAX_USERS, default 8), login, two-factor authentication (TOTP, RFC 6238) for admins,
saved search profiles and search history, and the access log.

Stdlib only: scrypt password hashes, HMAC-signed session cookie, TOTP. Interface for other modules:

    from web.auth import current_user, require_user, require_admin
    user = current_user(request)            # dict(id, email, name, role) or None
    @router.get("/x", dependencies=[Depends(require_admin)])

Create the first admin on the server (prints the TOTP secret once):

    .venv/bin/python -m web.auth create-user admin@example.org "Vorname Nachname" admin

Accounts hold the customer's staff names and e-mail addresses (contract data, Art. 6(1)(b) GDPR); they are never part
of the directory. The access log stores the IP truncated to /24 (IPv4) or /48 (IPv6) and is kept 90 days.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import struct
import sys
import time
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web.common import _, db, register, templates

SECRET = os.environ.get("SECRET_KEY") or secrets.token_hex(32)  # set SECRET_KEY in .env, else sessions end on restart
MAX_USERS = int(os.environ.get("MAX_USERS", "8"))
COOKIE = "fv_session"
SESSION_SECONDS = 8 * 3600
ISSUER = "Firmenverzeichnis Gesundheitswirtschaft"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_user (
  id serial PRIMARY KEY, email text UNIQUE NOT NULL, name text NOT NULL,
  role text NOT NULL CHECK (role IN ('admin', 'user')), pw_hash text NOT NULL, totp_secret text,
  active boolean NOT NULL DEFAULT true, created_at timestamptz NOT NULL DEFAULT now(), last_login timestamptz);
CREATE TABLE IF NOT EXISTS saved_search (
  id serial PRIMARY KEY, user_id int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE, name text NOT NULL,
  query text NOT NULL, created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS search_history (
  id bigserial PRIMARY KEY, user_id int NOT NULL REFERENCES app_user(id) ON DELETE CASCADE, query text NOT NULL,
  hits int, at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS search_history_user ON search_history (user_id, at DESC);
CREATE TABLE IF NOT EXISTS access_log (
  id bigserial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now(), user_id int, ip_net text, method text,
  path text, status int, ms int, ua text);
CREATE INDEX IF NOT EXISTS access_log_at ON access_log (at);
"""

register({
    "Anmelden": "Sign in", "Abmelden": "Sign out", "E-Mail": "E-mail", "Passwort": "Password",
    "Code aus der Authenticator-App": "Code from your authenticator app",
    "Anmeldung fehlgeschlagen.": "Sign-in failed.", "Mein Konto": "My account",
    "Gespeicherte Suchen": "Saved searches", "Suchverlauf": "Search history", "Suche speichern": "Save search",
    "Name der Suche": "Name of the search", "Löschen": "Delete", "Öffnen": "Open", "Treffer": "Results",
    "Noch keine gespeicherten Suchen.": "No saved searches yet.", "Noch kein Suchverlauf.": "No search history yet.",
    "Nutzerverwaltung": "User management", "Nutzer anlegen": "Create user", "Rolle": "Role",
    "Zugriffsprotokoll": "Access log", "Aktiv": "Active", "Name": "Name",
    "Anmelden, um Suchen zu speichern": "Sign in to save searches",
})


# ---------- primitives ----------

def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _algo, salt, h = stored.split("$")
        return hmac.compare_digest(hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=2**14, r=8, p=1).hex(), h)
    except ValueError:
        return False


def totp(secret_b32: str, at: float | None = None, step: int = 30) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    digest = hmac.new(key, struct.pack(">Q", int((at or time.time()) // step)), hashlib.sha1).digest()
    o = digest[-1] & 15
    return f"{(struct.unpack('>I', digest[o:o + 4])[0] & 0x7FFFFFFF) % 1_000_000:06d}"


def check_totp(secret_b32: str, code: str) -> bool:
    code = (code or "").strip().replace(" ", "")
    return any(hmac.compare_digest(totp(secret_b32, time.time() + d * 30), code) for d in (-1, 0, 1))


def sign(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()
    return raw + "." + hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()


def unsign(value: str | None) -> dict | None:
    if not value or "." not in value:
        return None
    raw, sig = value.rsplit(".", 1)
    if not hmac.compare_digest(sig, hmac.new(SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()):
        return None
    data = json.loads(base64.urlsafe_b64decode(raw))
    return data if data.get("exp", 0) > time.time() else None


def ip_net(ip: str | None) -> str | None:
    try:
        a = ipaddress.ip_address(ip or "")
    except ValueError:
        return None
    return str(ipaddress.ip_network(f"{a}/{24 if a.version == 4 else 48}", strict=False))


# ---------- request helpers ----------

def current_user(request: Request) -> dict | None:
    if "user" in request.state._state:
        return request.state.user
    s = unsign(request.cookies.get(COOKIE))
    user = None
    if s:
        with db() as conn:
            user = conn.execute("SELECT id, email, name, role FROM app_user WHERE id = %s AND active",
                                (s["uid"],)).fetchone()
    request.state.user = user
    return user


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(303, headers={"Location": "/login?next=" + quote(request.url.path)})
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if user["role"] != "admin":
        raise HTTPException(403)
    return user


def redact(path: str) -> str:
    """Never write an access token (web.gate, /zugang/<token>) into a log."""
    return "/zugang/***" if path.startswith("/zugang/") else path


def log_access(request: Request, status: int, ms: int) -> None:
    user = request.state.user if "user" in request.state._state else None
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None)
    with db() as conn:
        aid = request.state.access_id if "access_id" in request.state._state else None
        conn.execute("INSERT INTO access_log (user_id, ip_net, method, path, status, ms, ua, access_id) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                     (user["id"] if user else None, ip_net(ip), request.method, redact(request.url.path)[:300], status, ms,
                      (request.headers.get("user-agent") or "")[:200], aid))


def record_search(request: Request, query: str, hits: int) -> None:
    user = current_user(request)
    if user and query:
        with db() as conn:
            conn.execute("INSERT INTO search_history (user_id, query, hits) VALUES (%s, %s, %s)", (user["id"], query, hits))


# ---------- routes ----------

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, next: str = "/", error: bool = False):
    return templates.TemplateResponse(request, "login.html", dict(next=next, error=error, embed=False))


FAILS: dict[str, list[float]] = {}  # ip_net -> failure times; per process, enough for one app instance


@router.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), code: str = Form(""),
          next: str = Form("/")):
    ip = ip_net(request.headers.get("x-forwarded-for", "").split(",")[0].strip()
                or (request.client.host if request.client else "")) or "?"
    recent = [t for t in FAILS.get(ip, []) if t > time.time() - 600]
    if len(recent) >= 10:  # 10 failed sign-ins per network and 10 minutes, then wait
        raise HTTPException(429, "too many failed sign-ins, try again in 10 minutes")
    with db() as conn:
        u = conn.execute("SELECT * FROM app_user WHERE lower(email) = lower(%s) AND active", (email.strip(),)).fetchone()
        ok = bool(u) and check_password(password, u["pw_hash"]) and (
            u["role"] != "admin" or (u["totp_secret"] and check_totp(u["totp_secret"], code)))
        if ok:
            conn.execute("UPDATE app_user SET last_login = now() WHERE id = %s", (u["id"],))
    if not ok:
        FAILS[ip] = recent + [time.time()]
        time.sleep(1)
        return RedirectResponse("/login?error=1&next=" + quote(next), status_code=303)
    resp = RedirectResponse(next if next.startswith("/") and not next.startswith("//") else "/", status_code=303)
    resp.set_cookie(COOKIE, sign({"uid": u["id"], "exp": time.time() + SESSION_SECONDS}), max_age=SESSION_SECONDS,
                    httponly=True, secure=request.url.scheme == "https", samesite="lax")
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/konto", response_class=HTMLResponse)
def account(request: Request, user: dict = Depends(require_user)):
    with db() as conn:
        saved = conn.execute("SELECT * FROM saved_search WHERE user_id = %s ORDER BY name", (user["id"],)).fetchall()
        hist = conn.execute("SELECT query, max(hits) AS hits, max(at) AS at FROM search_history WHERE user_id = %s "
                            "GROUP BY query ORDER BY max(at) DESC LIMIT 30", (user["id"],)).fetchall()
    return templates.TemplateResponse(request, "account.html", dict(saved=saved, hist=hist, embed=False))


@router.post("/konto/suche")
def save_search(request: Request, name: str = Form(...), query: str = Form(...), user: dict = Depends(require_user)):
    with db() as conn:
        conn.execute("INSERT INTO saved_search (user_id, name, query) VALUES (%s, %s, %s)",
                     (user["id"], name.strip()[:120] or query[:120], query.lstrip("?")[:2000]))
    return RedirectResponse("/konto", status_code=303)


@router.post("/konto/suche/{sid}/loeschen")
def delete_search(sid: int, user: dict = Depends(require_user)):
    with db() as conn:
        conn.execute("DELETE FROM saved_search WHERE id = %s AND user_id = %s", (sid, user["id"]))
    return RedirectResponse("/konto", status_code=303)


@router.get("/admin/nutzer", response_class=HTMLResponse)
def users(request: Request, created: str = "", admin: dict = Depends(require_admin)):
    with db() as conn:
        rows = conn.execute("SELECT id, email, name, role, active, created_at, last_login FROM app_user ORDER BY id").fetchall()
        log = conn.execute("SELECT a.at, u.email, a.ip_net, a.method, a.path, a.status, a.ms FROM access_log a "
                           "LEFT JOIN app_user u ON u.id = a.user_id ORDER BY a.id DESC LIMIT 50").fetchall()
    return templates.TemplateResponse(request, "users.html", dict(rows=rows, log=log, created=created,
                                                                 max_users=MAX_USERS, embed=False))


@router.post("/admin/nutzer")
def add_user(request: Request, email: str = Form(...), name: str = Form(...), role: str = Form("user"),
             admin: dict = Depends(require_admin)):
    pw, secret = create_user(email, name, role)
    return RedirectResponse("/admin/nutzer?" + urlencode({"created": f"{email}: {pw}" + (f" · TOTP {secret}" if secret else "")}),
                            status_code=303)


def create_user(email: str, name: str, role: str = "user", password: str | None = None) -> tuple[str, str | None]:
    with db() as conn:
        n = conn.execute("SELECT count(*) AS n FROM app_user WHERE active").fetchone()["n"]
        if n >= MAX_USERS:
            raise HTTPException(409, f"max {MAX_USERS} accounts")
        pw = password or secrets.token_urlsafe(12)
        secret = base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=") if role == "admin" else None
        conn.execute("INSERT INTO app_user (email, name, role, pw_hash, totp_secret) VALUES (%s,%s,%s,%s,%s)",
                     (email.strip(), name.strip(), role, hash_password(pw), secret))
    return pw, secret


def otpauth_url(email: str, secret: str) -> str:
    return f"otpauth://totp/{quote(ISSUER)}:{quote(email)}?secret={secret}&issuer={quote(ISSUER)}"


if __name__ == "__main__":
    if sys.argv[1:2] == ["schema"]:
        with db() as conn:
            conn.execute(SCHEMA)
        print("auth schema ok")
    elif sys.argv[1:2] == ["create-user"]:
        email, name, role = sys.argv[2], sys.argv[3], (sys.argv[4] if len(sys.argv) > 4 else "user")
        pw, secret = create_user(email, name, role, os.environ.get("NEW_USER_PASSWORD"))
        print(f"user {email} ({role}) password: {pw}")
        if secret:
            print("TOTP secret:", secret, "\n", otpauth_url(email, secret))
    else:
        print(__doc__)
