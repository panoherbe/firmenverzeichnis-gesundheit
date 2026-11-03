"""
External monitor: runs every minute on the STANDBY server (other data centre) and watches the PRIMARY from outside,
the way a user sees it. It only alerts; restarting is the primary's job (systemd Restart=always + firmen-app-health).

    python -m ops.monitor check                    # one check round (systemd timer, every minute)
    python -m ops.monitor status                   # rebuild the status page only
    python -m ops.monitor maintenance 30 "Grund"   # suppress alerts for 30 min (failures are STILL counted)
    python -m ops.monitor watchdog                 # on the PRIMARY every 5 min: is the monitor itself alive?

Checks per round: GET /api/health (HTTP 200 and "ok": true), the search page (HTTP 200 and MONITOR_SEARCH_MARKER in the
body); one retry after 5 s before a round counts as failed. Hourly: TLS certificate expiry. Every round: the
primary's disk and last backup via /api/health/ops, and this server's own disk.

Down logic (each round = one minute of measurement):
  - a failed round opens an incident (start = first failed round)
  - after MONITOR_DOWN_AFTER_CHECKS failed rounds (default 2): alert "down" (P2)
  - incident open longer than MONITOR_P1_AFTER_MIN (default 5): alert P1 (phone alarm, ntfy priority 5)
  - first good round closes the incident and sends "recovered" with the duration (if a down alert went out)
Availability = good rounds / measured rounds. Minutes in which the monitor itself did not run are "unknown" and are
reported separately (never silently counted as up).

Storage: SQLite (MONITOR_DB, default var/monitor/monitor.sqlite), kept 400 days.
Status page: STATUS_DIR (default var/status): status.json, status.de.html, status.en.html (static, no JS, no CDN),
served by Caddy on the standby (status.<domain>) and, if the files are present, by the app's /status route.

Env: MONITOR_BASE_URL (required, e.g. https://verzeichnis.example.de), MONITOR_SEARCH_PATH (/?q=pharma),
MONITOR_SEARCH_MARKER (/firma/), OPS_HEALTH_TOKEN, TLS_WARN_DAYS (14), DISK_WARN_PCT (85), BACKUP_MAX_AGE_H (26),
HEARTBEAT_URL (optional dead-man's switch, pinged after every round), MONITOR_STATUS_URL (watchdog: URL of status.json).
"""

from __future__ import annotations

import html
import json
import os
import shutil
import socket
import sqlite3
import ssl
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from ops.alert import send_alert

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo("Europe/Berlin")


def _env() -> None:
    try:
        from etl.common.env import load_env
        load_env()
    except Exception:  # noqa: BLE001
        pass


_env()
E = os.environ.get
BASE = (E("MONITOR_BASE_URL") or "").rstrip("/")
DB_PATH = Path(E("MONITOR_DB", str(ROOT / "var" / "monitor" / "monitor.sqlite")))
STATUS_DIR = Path(E("STATUS_DIR", str(ROOT / "var" / "status")))
DOWN_AFTER = int(E("MONITOR_DOWN_AFTER_CHECKS", "2"))
P1_AFTER_MIN = float(E("MONITOR_P1_AFTER_MIN", "5"))
TLS_WARN_DAYS = int(E("TLS_WARN_DAYS", "14"))
DISK_WARN_PCT = float(E("DISK_WARN_PCT", "85"))
BACKUP_MAX_AGE_H = float(E("BACKUP_MAX_AGE_H", "26"))
WARN_REPEAT_S = 12 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (ts INTEGER PRIMARY KEY, ok INTEGER NOT NULL, health_ok INTEGER, search_ok INTEGER,
  ms INTEGER, maint INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS incidents (id INTEGER PRIMARY KEY, start_ts INTEGER NOT NULL, end_ts INTEGER,
  down_alerted INTEGER NOT NULL DEFAULT 0, p1_alerted INTEGER NOT NULL DEFAULT 0, cause TEXT);
CREATE TABLE IF NOT EXISTS warnings (key TEXT PRIMARY KEY, last_ts INTEGER NOT NULL, message TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def meta(c, key, value=None):
    if value is None:
        r = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r["value"] if r else None
    c.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              (key, str(value)))


def warn(c, key: str, title: str, message: str, level: str = "P3") -> None:
    """Non-outage warning, sent at most every WARN_REPEAT_S per key."""
    r = c.execute("SELECT last_ts FROM warnings WHERE key = ?", (key,)).fetchone()
    t = int(time.time())
    if r and t - r["last_ts"] < WARN_REPEAT_S:
        return
    send_alert(title, message, level=level, key=key)
    c.execute("INSERT INTO warnings (key, last_ts, message) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET "
              "last_ts = excluded.last_ts, message = excluded.message", (key, t, message))


def clear_warn(c, key: str) -> None:
    c.execute("DELETE FROM warnings WHERE key = ?", (key,))


# ---------- probes ----------

def probe(client: httpx.Client) -> tuple[bool, bool, int, str | None]:
    t0 = time.monotonic()
    health_ok = search_ok = False
    err = None
    try:
        r = client.get(f"{BASE}/api/health")
        health_ok = r.status_code == 200 and r.json().get("ok") is True
        if not health_ok:
            err = f"/api/health HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        err = f"/api/health {type(e).__name__}"
    try:
        r = client.get(BASE + E("MONITOR_SEARCH_PATH", "/?q=pharma"))
        search_ok = r.status_code == 200 and E("MONITOR_SEARCH_MARKER", "/firma/") in r.text
        if not search_ok:
            err = (err + "; " if err else "") + f"search HTTP {r.status_code}" + ("" if r.status_code != 200 else " marker missing")
    except Exception as e:  # noqa: BLE001
        err = (err + "; " if err else "") + f"search {type(e).__name__}"
    return health_ok, search_ok, int((time.monotonic() - t0) * 1000), err


def tls_days_left(url: str) -> float:
    u = urlparse(url)
    ctx = ssl.create_default_context()
    with socket.create_connection((u.hostname, u.port or 443), timeout=10) as s:
        with ctx.wrap_socket(s, server_hostname=u.hostname) as ss:
            cert = ss.getpeercert()
    return (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400


def side_checks(c, client: httpx.Client) -> None:
    t = time.time()
    if BASE.startswith("https://") and t - float(meta(c, "tls_checked") or 0) > 3600:
        meta(c, "tls_checked", t)
        try:
            days = tls_days_left(BASE)
            meta(c, "tls_days_left", round(days, 1))
            if days < TLS_WARN_DAYS:
                warn(c, "tls", "TLS-Zertifikat läuft ab", f"{BASE}: noch {days:.1f} Tage (Caddy erneuert normalerweise "
                     "30 Tage vorher automatisch — prüfen: journalctl -u caddy)", level="P2" if days < 7 else "P3")
            else:
                clear_warn(c, "tls")
        except Exception as e:  # noqa: BLE001
            meta(c, "tls_days_left", "")
            meta(c, "tls_error", f"{type(e).__name__}")
    try:
        hdr = {"x-ops-token": E("OPS_HEALTH_TOKEN")} if E("OPS_HEALTH_TOKEN") else {}
        r = client.get(f"{BASE}/api/health/ops", headers=hdr)
        if r.status_code == 200:
            o = r.json()
            meta(c, "primary_disk_pct", o.get("disk_used_pct"))
            meta(c, "primary_last_backup", o.get("last_backup_at") or "")
            if (o.get("disk_used_pct") or 0) > DISK_WARN_PCT:
                warn(c, "disk_primary", "Festplatte Primärserver fast voll", f"{o['disk_used_pct']} % belegt")
            else:
                clear_warn(c, "disk_primary")
            last = o.get("last_backup_at")
            age_h = (t - datetime.fromisoformat(last).timestamp()) / 3600 if last else None
            if age_h is None or age_h > BACKUP_MAX_AGE_H or o.get("last_backup_ok") is False:
                warn(c, "backup_age", "Backup fehlt oder fehlerhaft",
                     f"letztes Backup: {last or 'keins'} (ok={o.get('last_backup_ok')})")
            else:
                clear_warn(c, "backup_age")
        else:
            meta(c, "ops_error", f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001 - the main probe already covers reachability
        meta(c, "ops_error", f"{type(e).__name__}: {e}"[:200])
    du = shutil.disk_usage(ROOT)
    pct = 100 * du.used / du.total
    meta(c, "standby_disk_pct", round(pct, 1))
    if pct > DISK_WARN_PCT:
        warn(c, "disk_standby", "Festplatte Standby fast voll", f"{pct:.1f} % belegt")
    else:
        clear_warn(c, "disk_standby")


# ---------- one round ----------

def check() -> int:
    if not BASE:
        print("MONITOR_BASE_URL not set", file=sys.stderr)
        return 2
    c = connect()
    ts = int(time.time()) // 60 * 60  # one row per minute
    maint = float(meta(c, "maint_until") or 0) > time.time()
    with httpx.Client(timeout=10, follow_redirects=True, headers={"User-Agent": "firmen-monitor/1"}) as client:
        h, s, ms, err = probe(client)
        if not (h and s):
            time.sleep(5)
            h, s, ms, err2 = probe(client)
            err = err2 if not (h and s) else None
        ok = h and s
        c.execute("INSERT OR REPLACE INTO checks (ts, ok, health_ok, search_ok, ms, maint, error) VALUES (?,?,?,?,?,?,?)",
                  (ts, int(ok), int(h), int(s), ms, int(maint), None if ok else err))
        inc = c.execute("SELECT * FROM incidents WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if not ok:
            if not inc:
                c.execute("INSERT INTO incidents (start_ts, cause) VALUES (?, ?)", (ts, err))
                inc = c.execute("SELECT * FROM incidents WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
            fails = c.execute("SELECT count(*) AS n FROM checks WHERE ts >= ? AND ok = 0", (inc["start_ts"],)).fetchone()["n"]
            down_min = (ts - inc["start_ts"]) / 60 + 1
            if not maint and fails >= DOWN_AFTER and not inc["down_alerted"]:
                send_alert("Firmenverzeichnis nicht erreichbar", f"{BASE}: {err} (seit {down_min:.0f} min)",
                           level="P2", key=f"incident:{inc['id']}")
                c.execute("UPDATE incidents SET down_alerted = 1 WHERE id = ?", (inc["id"],))
            if not maint and down_min > P1_AFTER_MIN and not inc["p1_alerted"]:
                send_alert("P1: Verzeichnis seit über 5 min down",
                           f"{BASE}: {err}. Reaktion innerhalb 8 h nötig. Erste Schritte: ssh primary → "
                           "systemctl status firmen-app caddy postgresql; Failover: docs/bid/BETRIEB.md §7",
                           level="P1", key=f"incident:{inc['id']}")
                c.execute("UPDATE incidents SET p1_alerted = 1 WHERE id = ?", (inc["id"],))
        elif inc:
            c.execute("UPDATE incidents SET end_ts = ? WHERE id = ?", (ts, inc["id"]))
            if inc["down_alerted"]:
                send_alert("Firmenverzeichnis wieder erreichbar",
                           f"{BASE}: Ausfall {(ts - inc['start_ts']) / 60:.0f} min "
                           f"({datetime.fromtimestamp(inc['start_ts'], TZ):%d.%m. %H:%M}–"
                           f"{datetime.fromtimestamp(ts, TZ):%H:%M})", level="ok", key=f"incident:{inc['id']}")
        side_checks(c, client)
    c.execute("DELETE FROM checks WHERE ts < ?", (ts - 400 * 86400,))
    c.commit()
    write_status(c)
    if E("HEARTBEAT_URL"):
        try:
            httpx.get(E("HEARTBEAT_URL"), timeout=10)
        except Exception:  # noqa: BLE001
            pass
    c.close()
    return 0


# ---------- availability + status page ----------

def availability(c, start_ts: int, end_ts: int) -> dict:
    r = c.execute("SELECT count(*) AS n, coalesce(sum(ok), 0) AS up FROM checks WHERE ts >= ? AND ts < ?",
                  (start_ts, end_ts)).fetchone()
    first = c.execute("SELECT min(ts) AS t FROM checks").fetchone()["t"]
    start_eff = max(start_ts, first) if first else end_ts  # before the first measurement nothing is "unknown"
    expected = max(0, (min(end_ts, int(time.time())) - start_eff) // 60)
    n, up = r["n"], r["up"]
    return {"measured_min": n, "up_min": up, "down_min": n - up, "unknown_min": max(0, expected - n),
            "pct": round(100 * up / n, 3) if n else None}


def write_status(c) -> dict:
    now = int(time.time())
    last = c.execute("SELECT * FROM checks ORDER BY ts DESC LIMIT 1").fetchone()
    open_inc = c.execute("SELECT * FROM incidents WHERE end_ts IS NULL ORDER BY id DESC LIMIT 1").fetchone()
    today = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    days = []
    for i in range(89, -1, -1):
        d0 = today - timedelta(days=i)
        a = availability(c, int(d0.timestamp()), int((d0 + timedelta(days=1)).timestamp()))
        days.append({"date": d0.date().isoformat(), **a})
    a90 = availability(c, int((today - timedelta(days=89)).timestamp()), now + 60)
    m0 = today.replace(day=1)
    amonth = availability(c, int(m0.timestamp()), now + 60)
    incs = [dict(r) for r in c.execute("SELECT * FROM incidents WHERE start_ts >= ? ORDER BY start_ts DESC LIMIT 20",
                                         (now - 90 * 86400,))]
    stale = not last or now - last["ts"] > 300
    state = "unknown" if stale else ("down" if open_inc and not last["ok"] else "up")
    data = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "base_url": BASE, "state": state,
            "last_check": datetime.fromtimestamp(last["ts"], timezone.utc).isoformat() if last else None,
            "last_ms": last["ms"] if last else None, "uptime_90d_pct": a90["pct"], "uptime_month_pct": amonth["pct"],
            "unknown_90d_min": a90["unknown_min"], "tls_days_left": meta(c, "tls_days_left"),
            "maintenance_until": meta(c, "maint_until"), "days": days,
            "incidents": [{"start": datetime.fromtimestamp(i["start_ts"], timezone.utc).isoformat(),
                           "end": datetime.fromtimestamp(i["end_ts"], timezone.utc).isoformat() if i["end_ts"] else None,
                           "minutes": ((i["end_ts"] or now) - i["start_ts"]) // 60 or 1} for i in incs]}
    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    _atomic(STATUS_DIR / "status.json", json.dumps(data, indent=1))
    for lg in ("de", "en"):
        _atomic(STATUS_DIR / f"status.{lg}.html", render(data, lg))
    _atomic(STATUS_DIR / "index.html", render(data, "de"))
    return data


def _atomic(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


T = {
    "de": dict(title="Servicestatus", up="Alle Systeme verfügbar", down="Störung – das Verzeichnis ist nicht erreichbar",
               unknown="Status unbekannt (Messung ausgesetzt)", last="Letzte Prüfung", uptime90="Verfügbarkeit 90 Tage",
               month="Verfügbarkeit laufender Monat", target="Zusage: mindestens 99,5 % im Monatsmittel",
               incidents="Störungen (90 Tage)", none="Keine Störungen.", minutes="min", ongoing="andauernd",
               nodata="keine Messung", other="English", other_href="status.en.html", support="Störung melden",
               note="Gemessen jede Minute von einem zweiten Rechenzentrum aus (Suche und API). Zeiten in Europe/Berlin.",
               tls="TLS-Zertifikat gültig noch", days="Tage", maint="Wartung bis"),
    "en": dict(title="Service status", up="All systems operational", down="Outage – the directory is unreachable",
               unknown="Status unknown (measurement paused)", last="Last check", uptime90="Availability 90 days",
               month="Availability this month", target="Commitment: at least 99.5 % monthly average",
               incidents="Incidents (90 days)", none="No incidents.", minutes="min", ongoing="ongoing",
               nodata="no data", other="Deutsch", other_href="status.de.html", support="Report a problem",
               note="Measured every minute from a second data centre (search and API). Times in Europe/Berlin.",
               tls="TLS certificate valid for", days="days", maint="Maintenance until"),
}


def render(d: dict, lg: str) -> str:
    t = T[lg]
    color = {"up": "#1a7f37", "down": "#b42318", "unknown": "#8a6d00"}[d["state"]]
    bars = []
    for day in d["days"]:
        p = day["pct"]
        c = "#d0d5dd" if p is None else "#1a7f37" if p >= 99.95 else "#7cb342" if p >= 99.5 else "#f59e0b" if p >= 98 else "#b42318"
        label = f"{day['date']}: " + (t["nodata"] if p is None else f"{p:.2f} %, {day['down_min']} {t['minutes']} down")
        bars.append(f'<span class="b" style="background:{c}" title="{html.escape(label)}"></span>')
    incs = "".join(
        f"<li>{datetime.fromisoformat(i['start']).astimezone(TZ):%d.%m.%Y %H:%M} – "
        f"{(datetime.fromisoformat(i['end']).astimezone(TZ).strftime('%H:%M') if i['end'] else t['ongoing'])} "
        f"({i['minutes']} {t['minutes']})</li>" for i in d["incidents"]) or f"<li>{t['none']}</li>"
    last = datetime.fromisoformat(d["last_check"]).astimezone(TZ).strftime("%d.%m.%Y %H:%M") if d["last_check"] else "–"
    fmt = lambda p: "–" if p is None else f"{p:.3f} %".replace(".", "," if lg == "de" else ".")  # noqa: E731
    tls = f'<p class="s">{t["tls"]} {d["tls_days_left"]} {t["days"]}</p>' if d.get("tls_days_left") else ""
    maint = ""
    if d.get("maintenance_until") and float(d["maintenance_until"]) > time.time():
        maint = f'<p class="s">{t["maint"]} {datetime.fromtimestamp(float(d["maintenance_until"]), TZ):%d.%m. %H:%M}</p>'
    return f"""<!doctype html>
<html lang="{lg}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="60"><title>{t['title']} – Firmenverzeichnis Gesundheitswirtschaft</title>
<style>
body{{margin:0;font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif;color:#1d2433;background:#fff}}
main{{max-width:860px;margin:0 auto;padding:20px 16px}} a{{color:#0b5cad}}
.state{{padding:14px 16px;border-radius:8px;color:#fff;background:{color};font-weight:600;font-size:18px}}
.bars{{display:flex;gap:2px;margin:12px 0 4px;height:34px}} .b{{flex:1;border-radius:2px}}
.s{{color:#5b6475;font-size:13px}} .kpi{{display:flex;gap:28px;flex-wrap:wrap;margin:16px 0}}
.kpi div strong{{display:block;font-size:22px}}
</style></head><body><main>
<p class="s"><a href="{t['other_href']}">{t['other']}</a> · <a href="{html.escape(d['base_url'])}/support">{t['support']}</a></p>
<h1>{t['title']}</h1>
<div class="state">{t[d['state']]}</div>{maint}
<p class="s">{t['last']}: {last}{f" · {d['last_ms']} ms" if d.get('last_ms') else ''}</p>
<div class="kpi"><div>{t['uptime90']}<strong>{fmt(d['uptime_90d_pct'])}</strong></div>
<div>{t['month']}<strong>{fmt(d['uptime_month_pct'])}</strong></div></div>
<p class="s">{t['target']}</p>
<div class="bars" aria-label="{t['uptime90']}">{''.join(bars)}</div>
<p class="s">−90 … {datetime.now(TZ):%d.%m.%Y}</p>
<h2>{t['incidents']}</h2><ul>{incs}</ul>
{tls}<p class="s">{t['note']}</p>
</main></body></html>
"""


# ---------- maintenance + watchdog ----------

def maintenance(minutes: float, reason: str) -> None:
    c = connect()
    meta(c, "maint_until", time.time() + minutes * 60)
    meta(c, "maint_reason", reason)
    c.commit()
    write_status(c)
    print(f"alerts suppressed for {minutes:.0f} min ({reason}); failures are still counted")


def watchdog() -> int:
    """On the primary: alert if the standby's status.json is stale (the monitor itself stopped)."""
    url = E("MONITOR_STATUS_URL")
    if not url:
        print("MONITOR_STATUS_URL not set", file=sys.stderr)
        return 2
    state_f = DB_PATH.parent / "watchdog.json"
    state_f.parent.mkdir(parents=True, exist_ok=True)
    st = json.loads(state_f.read_text()) if state_f.exists() else {"fails": 0, "alerted": 0}
    try:
        d = httpx.get(url, timeout=15).json()
        age = time.time() - datetime.fromisoformat(d["generated_at"]).timestamp()
        ok, why = age < 600, f"status.json {age / 60:.0f} min alt"
    except Exception as e:  # noqa: BLE001
        ok, why = False, f"{type(e).__name__}"
    st["fails"] = 0 if ok else st["fails"] + 1
    if st["fails"] >= 2 and time.time() - st["alerted"] > WARN_REPEAT_S:
        send_alert("Monitor auf dem Standby läuft nicht", f"{url}: {why}. Verfügbarkeit wird gerade nicht gemessen.",
                   level="P3", key="watchdog")
        st["alerted"] = time.time()
    if ok:
        st["alerted"] = 0
    state_f.write_text(json.dumps(st))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "check":
        sys.exit(check())
    elif cmd == "status":
        print(json.dumps({k: v for k, v in write_status(connect()).items() if k != "days"}, indent=1))
    elif cmd == "maintenance":
        maintenance(float(sys.argv[2]), " ".join(sys.argv[3:]) or "Wartung")
    elif cmd == "watchdog":
        sys.exit(watchdog())
    else:
        print(__doc__)
