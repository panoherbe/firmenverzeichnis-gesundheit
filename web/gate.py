"""
Closed demo: with env ACCESS_GATE=1 strangers see only a neutral start page; search, profiles, API, exports and
matchmaking need an access cookie, set by a personal one-click link /zugang/<token>.

  python -m web.gate create "<label>" [<host>]   # prints the link once; only the SHA-256 of the token is stored
  python -m web.gate list | revoke <id>

Buyers (env ACCESS_TENANTS_FILE, a JSON file kept outside the repo, so no buyer name is in the code): one entry per
own address, e.g. {"<buyer>.example.com": {"name": "...", "theme": "/srv/.../theme.json", "welcome": "...",
"expires": "2027-03-31"}}. A new buyer costs one entry, one DNS name (or a wildcard) and `create "<label>" <host>`.
A token works only on its own address (the default address for tokens without host) and stops after "expires".
Without a valid token every address shows the same neutral start page, without the buyer's name.

The cookie is HMAC-signed (SECRET_KEY), carries the token id and expires after 30 days; a revoked token stops working
at once. Every request with access updates last_used/uses; the access log (web.auth) records the token id.
A per-network rate limit protects the link, the API and the exports (also without the gate). Without ACCESS_GATE the
app behaves as before (customer installations are opened by contract, not by a link).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import secrets
import sys
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from web import auth
from web.common import db, register, templates

ENABLED = os.environ.get("ACCESS_GATE") == "1"
COOKIE = "fv_access"
DAYS = 30
PUBLIC = ("/static/", "/medien/", "/impressum", "/datenschutz", "/zugang/", "/api/health", "/favicon")
SCHEMA = """
CREATE TABLE IF NOT EXISTS access_token (
  id serial PRIMARY KEY, label text NOT NULL, token_sha256 text UNIQUE NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), revoked_at timestamptz, last_used timestamptz, uses int NOT NULL DEFAULT 0);
ALTER TABLE access_token ADD COLUMN IF NOT EXISTS host text;
ALTER TABLE access_log ADD COLUMN IF NOT EXISTS access_id int;
ALTER TABLE company ADD COLUMN IF NOT EXISTS preview_locked boolean;
CREATE TABLE IF NOT EXISTS access_usage (access_id int NOT NULL, day date NOT NULL, kind text NOT NULL,
  n int NOT NULL DEFAULT 0, PRIMARY KEY (access_id, day, kind));
"""

# ---------- preview limits (closed demo only) ----------
# Everything can be tried, but the data set cannot be carried away: exports and API pages hold at most PREVIEW_ROWS
# rows, the result list pages to PREVIEW_PAGES, the API offset stops at PREVIEW_OFFSET, and one access link opens
# at most PREVIEW_PROFILES profiles a day. Counts and statistics stay complete. Enforced on the server; usage is
# counted per access link and day (access_usage). Customer installations (no ACCESS_GATE) have no such limits.
PREVIEW_ROWS = int(os.environ.get("PREVIEW_ROWS", "100"))
PREVIEW_PAGES = int(os.environ.get("PREVIEW_PAGES", "5"))
PREVIEW_OFFSET = int(os.environ.get("PREVIEW_OFFSET", "100"))
PREVIEW_PROFILES = int(os.environ.get("PREVIEW_PROFILES", "100"))
# caps per link, per day and over the link's whole lifetime: normal testing never reaches them, collecting the data
# set does (contact and register fields are shown only in the first PREVIEW_FULL rows of an export or API page)
PREVIEW_DAILY = {"export": int(os.environ.get("PREVIEW_EXPORTS", "15")), "api": int(os.environ.get("PREVIEW_API", "50")),
                 "search": int(os.environ.get("PREVIEW_SEARCHES", "300")), "profile": PREVIEW_PROFILES}
PREVIEW_TOTAL = {"export": int(os.environ.get("PREVIEW_EXPORTS_TOTAL", "100")),
                 "api": int(os.environ.get("PREVIEW_API_TOTAL", "300")),
                 "search": int(os.environ.get("PREVIEW_SEARCHES_TOTAL", "3000")),
                 "profile": int(os.environ.get("PREVIEW_PROFILES_TOTAL", "800"))}
PREVIEW_FULL = int(os.environ.get("PREVIEW_FULL", "5"))
TEASE_FIELDS = ("email_role", "contact_url", "vat", "register_no", "lei", "srns", "domain")
IN_CONTRACT = "im Vertrag"


def over(request: Request, kind: str) -> bool:
    """Count one use; True when today's or the lifetime cap for this link is exceeded (closed preview only)."""
    if not ENABLED:
        return False
    if count(request, kind) > PREVIEW_DAILY[kind]:
        return True
    with db() as conn:
        total = conn.execute("SELECT coalesce(sum(n), 0) AS n FROM access_usage WHERE access_id = %s AND kind = %s",
                             (access_id(request), kind)).fetchone()["n"]
    return total > PREVIEW_TOTAL[kind]


def tease(rows: list[dict]) -> list[dict]:
    """Preview: contact and register fields only for the fully shown companies (ops/demo_subset.py marks the others
    `preview_locked`, their fields are already empty on the demo host); the columns stay so the format can be checked."""
    if ENABLED:
        for r in rows:
            if r.pop("preview_locked", False):
                for k in TEASE_FIELDS:
                    if k in r:
                        r[k] = IN_CONTRACT
    return rows

register({
    "Zugang": "Access", "Willkommen": "Welcome",
    "Dieser Link ist nicht (mehr) gültig.": "This link is not (or no longer) valid.",
    "Zu viele Anfragen. Bitte kurz warten.": "Too many requests. Please wait a moment.",
})

# ---------- rate limit (per /24 network, sliding window, per process) ----------
LIMITS = {"/zugang/": (10, 600), "/export": (20, 60), "/api/": (120, 60), "/statistik.pdf": (20, 60)}
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def rate_limited(request: Request) -> bool:
    path = request.url.path
    rule = next(((p, lim) for p, lim in LIMITS.items() if path.startswith(p)), None)
    if not rule or path == "/api/health":
        return False
    prefix, (n, window) = rule
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "")
    key = (auth.ip_net(ip) or "?", prefix)
    q, now = _hits[key], time.monotonic()
    while q and q[0] < now - window:
        q.popleft()
    q.append(now)
    return len(q) > n


# ---------- buyers (own address, name, theme, welcome, expiry) ----------
_tenants: tuple[float, dict] = (0.0, {})


def tenants() -> dict:
    global _tenants
    path = os.environ.get("ACCESS_TENANTS_FILE")
    if not path:
        return {}
    try:
        mtime = os.path.getmtime(path)
        if mtime != _tenants[0]:
            _tenants = (mtime, {k.lower(): v for k, v in json.loads(open(path).read()).items()})
    except (OSError, ValueError, AttributeError):
        return {}
    return _tenants[1]


def host_of(request: Request) -> str:
    return (request.headers.get("host") or "").split(":")[0].lower()


def tenant(request: Request) -> dict | None:
    """The buyer entry of this address, or None (default address or unknown name)."""
    t = tenants().get(host_of(request))
    if t and t.get("expires") and dt.date.today() > dt.date.fromisoformat(t["expires"]):
        return None
    return t


def _host_ok(request: Request, token_host: str | None) -> bool:
    # a buyer's token only on its own (unexpired) address; a token without host only on non-buyer addresses
    if token_host:
        return token_host == host_of(request) and tenant(request) is not None
    return host_of(request) not in tenants()


# ---------- access cookie ----------
def access_id(request: Request) -> int | None:
    if "access_id" in request.state._state:
        return request.state.access_id
    data = auth.unsign(request.cookies.get(COOKIE))
    aid = None
    if data and "tid" in data:
        with db() as conn:
            row = conn.execute("SELECT id, host FROM access_token WHERE id = %s AND revoked_at IS NULL",
                               (data["tid"],)).fetchone()
            if row and _host_ok(request, row["host"]):
                conn.execute("UPDATE access_token SET last_used = now(), uses = uses + 1 WHERE id = %s", (row["id"],))
                aid = row["id"]
    request.state.access_id = aid
    return aid


def count(request: Request, kind: str) -> int:
    """Add one use of `kind` (profile, export, api) for this access link today; returns today's total."""
    aid = access_id(request)
    if not aid:
        return 0
    with db() as conn:
        return conn.execute("""INSERT INTO access_usage (access_id, day, kind, n) VALUES (%s, current_date, %s, 1)
            ON CONFLICT (access_id, day, kind) DO UPDATE SET n = access_usage.n + 1 RETURNING n""", (aid, kind)).fetchone()["n"]


def limit_page(request: Request, title: str, text: str, status: int = 429) -> HTMLResponse:
    """Friendly page instead of an error when a preview limit is reached."""
    return templates.TemplateResponse(request, "preview_limit.html", dict(embed=False, title=title, text=text),
                                      status_code=status)


def theme_file(request: Request) -> str | None:
    """Buyer theme for visitors with access on the buyer's address; else the optional ACCESS_THEME_FILE."""
    if not ENABLED or not access_id(request):
        return None
    t = tenant(request)
    return (t or {}).get("theme") or os.environ.get("ACCESS_THEME_FILE")



def needs_access(request: Request) -> bool:
    """True when the gate is on, the path is not public and the visitor has no valid access."""
    if not ENABLED or request.url.path.startswith(PUBLIC):
        return False
    return access_id(request) is None


# templates hide navigation that would only bounce a visitor without access back to the start page
templates.env.globals["gate_locked"] = lambda request: ENABLED and access_id(request) is None
templates.env.globals["gate_enabled"] = ENABLED
templates.env.globals.update(PREVIEW_PAGES=PREVIEW_PAGES, PREVIEW_ROWS=PREVIEW_ROWS)

router = APIRouter()


@router.get("/zugang/{token}")
def enter(request: Request, token: str):
    if not ENABLED:
        raise HTTPException(404)
    with db() as conn:
        row = conn.execute("SELECT id, host FROM access_token WHERE token_sha256 = %s AND revoked_at IS NULL",
                           (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if not row or not _host_ok(request, row["host"]):
        return HTMLResponse(templates.get_template("gate_invalid.html").render(request=request, embed=False),
                            status_code=404)
    resp = RedirectResponse("/willkommen", status_code=303)
    resp.set_cookie(COOKIE, auth.sign({"tid": row["id"], "exp": time.time() + DAYS * 86400}), max_age=DAYS * 86400,
                    httponly=True, secure=request.url.scheme == "https", samesite="lax")
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp


@router.get("/willkommen", response_class=HTMLResponse)
def welcome(request: Request):
    if not ENABLED:
        raise HTTPException(404)
    t = tenant(request) or {}
    from web.common import lang
    welcome = t.get("welcome_en" if lang() == "en" else "welcome", "") or t.get("welcome", "")
    resp = templates.TemplateResponse(request, "willkommen.html",
                                      dict(embed=False, demo_org=t.get("name", ""), welcome=welcome))
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def landing(request: Request) -> HTMLResponse:
    resp = templates.TemplateResponse(request, "landing.html", dict(embed=False))
    resp.headers["X-Robots-Tag"] = "noindex"
    return resp


def create(label: str, host: str | None = None) -> tuple[int, str]:
    token = secrets.token_urlsafe(32)
    with db() as conn:
        tid = conn.execute("INSERT INTO access_token (label, token_sha256, host) VALUES (%s, %s, %s) RETURNING id",
                           (label, hashlib.sha256(token.encode()).hexdigest(), host and host.lower())).fetchone()["id"]
    return tid, token


if __name__ == "__main__":
    cmd = sys.argv[1:2]
    with db() as conn:
        conn.execute(SCHEMA)
    if cmd == ["create"]:
        host = sys.argv[3] if len(sys.argv) > 3 else None
        if host and host.lower() not in tenants():
            sys.exit(f"{host} has no entry in ACCESS_TENANTS_FILE")
        tid, token = create(sys.argv[2], host)
        base = f"https://{host.lower()}" if host else os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
        print(f"{base}/zugang/{token}")
    elif cmd == ["list"]:
        with db() as conn:
            for r in conn.execute("SELECT id, label, host, created_at, revoked_at, last_used, uses FROM access_token ORDER BY id"):
                print(r)
    elif cmd == ["revoke"]:
        with db() as conn:
            conn.execute("UPDATE access_token SET revoked_at = now() WHERE id = %s", (int(sys.argv[2]),))
        print("revoked", sys.argv[2])
    else:
        print(__doc__)
