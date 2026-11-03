"""
Monthly SLA report (tender LB §2.1 "monatliche Berichterstattung über die SLA-Einhaltung").

Runs on the STANDBY on the 1st of each month at 06:00 Europe/Berlin (firmen-sla-report.timer), where everything it
needs is local: the monitor's SQLite (availability, incidents), the nightly-restored database (tickets up to the
night before), and the backup result lines pulled from the primary.

    python -m ops.sla_report            # previous month
    python -m ops.sla_report 2027-01    # a given month

Writes var/reports/sla-YYYY-MM.{md,html,pdf} (REPORT_DIR). The PDF is written with a tiny built-in writer (stdlib only,
Courier, cp1252) — plain but sendable. No requester e-mails or names appear in the report.
Sending it to the customer is a human step (Pano), see docs/bid/BETRIEB.md §5.

Measured vs. assumed: every number below comes from a log line or a table row; where a source is missing the report
says "keine Daten" instead of assuming success.
"""

from __future__ import annotations

import html
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ops.bizhours import TARGET_HOURS, TZ, business_hours_between

ROOT = Path(__file__).resolve().parents[1]


def _env() -> None:
    try:
        from etl.common.env import load_env
        load_env()
    except Exception:  # noqa: BLE001
        pass


_env()
E = os.environ.get
BACKUP_DIR = Path(E("BACKUP_DIR", str(ROOT / "var" / "backup")))
MONITOR_DB = Path(E("MONITOR_DB", str(ROOT / "var" / "monitor" / "monitor.sqlite")))
REPORT_DIR = Path(E("REPORT_DIR", str(ROOT / "var" / "reports")))
OPS_DIR = Path(E("OPS_LOG_DIR", str(ROOT / "var" / "ops")))
AVAIL_TARGET = 99.5
RECOVERY_H = 24


def month_bounds(ym: str | None) -> tuple[datetime, datetime]:
    if ym:
        y, m = map(int, ym.split("-"))
    else:
        first = datetime.now(TZ).date().replace(day=1) - timedelta(days=1)
        y, m = first.year, first.month
    start = datetime(y, m, 1, tzinfo=TZ)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=TZ)
    return start, end


def jsonl(path: Path, start: datetime, end: datetime) -> list[dict]:
    out = []
    if not path.exists():
        return out
    for ln in path.read_text().splitlines():
        try:
            d = json.loads(ln)
            at = datetime.fromisoformat(d["at"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        if start <= at < end:
            d["_at"] = at
            out.append(d)
    return out


def fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M") if dt else "–"


def pct(x: float | None) -> str:
    return "keine Daten" if x is None else f"{x:.3f} %".replace(".", ",")


# ---------- collectors ----------

def availability(start: datetime, end: datetime) -> dict:
    if not MONITOR_DB.exists():
        return {"available": False}
    c = sqlite3.connect(MONITOR_DB)
    c.row_factory = sqlite3.Row
    s, e = int(start.timestamp()), int(end.timestamp())
    r = c.execute("SELECT count(*) n, coalesce(sum(ok),0) up, coalesce(sum(maint * (1-ok)),0) maint_down "
                  "FROM checks WHERE ts >= ? AND ts < ?", (s, e)).fetchone()
    first = c.execute("SELECT min(ts) FROM checks").fetchone()[0]
    s_eff = max(s, first) if first else e
    expected = max(0, (min(e, int(datetime.now(timezone.utc).timestamp())) - s_eff) // 60)
    incs = [dict(x) for x in c.execute(
        "SELECT * FROM incidents WHERE start_ts < ? AND coalesce(end_ts, ?) >= ? ORDER BY start_ts", (e, e, s))]
    c.close()
    n, up = r["n"], r["up"]
    unknown = max(0, expected - n)
    for i in incs:
        i["minutes"] = max(1, ((i["end_ts"] or e) - i["start_ts"]) // 60)
        i["recovery_ok"] = i["minutes"] <= RECOVERY_H * 60
    return {"available": True, "since": datetime.fromtimestamp(s_eff, TZ) if s_eff > s else None, "expected": expected, "measured": n, "up": up, "down": n - up, "unknown": unknown,
            "maint_down": r["maint_down"],
            "pct": 100 * up / n if n else None,
            "pct_conservative": 100 * up / expected if expected else None,
            "incidents": incs}


def tickets(start: datetime, end: datetime) -> dict:
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row, connect_timeout=10) as conn:
            if not conn.execute("SELECT to_regclass('support_ticket') AS t").fetchone()["t"]:
                return {"available": False, "why": "Tabelle support_ticket fehlt"}
            rows = conn.execute(
                "SELECT number, priority, priority_requested, category, created_at, due_at, first_reaction_at, "
                "resolved_at, status FROM support_ticket WHERE created_at >= %s AND created_at < %s ORDER BY created_at",
                (start, end)).fetchall()
            carry = conn.execute("SELECT count(*) AS n FROM support_ticket WHERE created_at < %s AND "
                                 "(resolved_at IS NULL OR resolved_at >= %s)", (start, start)).fetchone()["n"]
    except Exception as e:  # noqa: BLE001
        return {"available": False, "why": f"Datenbank nicht erreichbar ({type(e).__name__})"}
    cutoff = min(end, datetime.now(timezone.utc))
    for t in rows:
        t["hours"] = business_hours_between(t["created_at"], t["first_reaction_at"] or cutoff)
        if t["first_reaction_at"]:
            t["met"] = t["first_reaction_at"] <= t["due_at"]
        else:
            t["met"] = False if cutoff > t["due_at"] else None   # unanswered past the deadline counts as missed
    by = {}
    for p in TARGET_HOURS:
        ts = [t for t in rows if t["priority"] == p]
        by[p] = {"n": len(ts), "met": sum(t["met"] is True for t in ts), "missed": sum(t["met"] is False for t in ts),
                 "open": sum(t["met"] is None for t in ts),
                 "avg_h": (sum(t["hours"] for t in ts if t["first_reaction_at"]) /
                           sum(1 for t in ts if t["first_reaction_at"])) if any(t["first_reaction_at"] for t in ts) else None}
    return {"available": True, "rows": rows, "by": by, "carry": carry}


def backups(start: datetime, end: datetime) -> dict:
    dumps = jsonl(BACKUP_DIR / "backup.jsonl", start, end)
    off = jsonl(BACKUP_DIR / "offsite.jsonl", start, end)
    rt = jsonl(BACKUP_DIR / "restore_test.jsonl", start, end)
    sr = jsonl(BACKUP_DIR / "standby_restore.jsonl", start, end)
    rf = jsonl(BACKUP_DIR / "refresh.jsonl", start, end)
    days, d = [], start.date()
    last_day = min(end.date(), datetime.now(TZ).date() + timedelta(days=1))
    while d < last_day:
        days.append(d)
        d += timedelta(days=1)
    ok_days = {x["_at"].astimezone(TZ).date() for x in dumps if x.get("ok")}
    off_days = {x["_at"].astimezone(TZ).date() for x in off if x.get("ok") and x.get("offsite") == "ok"}
    present = sorted(BACKUP_DIR.glob("firmen-*.dump"))
    oldest = min((datetime.fromtimestamp(p.stat().st_mtime, TZ) for p in present), default=None)
    return {"days": days, "dump_ok_days": ok_days, "missing": [x for x in days if x not in ok_days],
            "offsite_ok_days": off_days, "offsite_missing": [x for x in days if x not in off_days],
            "dumps": dumps, "restore_tests": rt, "standby_restores": sr, "refresh": rf,
            "kept": len(present), "oldest": oldest}


def alerts(start: datetime, end: datetime) -> list[dict]:
    return jsonl(OPS_DIR / "alerts.jsonl", start, end)


# ---------- report model -> md / html / pdf ----------

def build(ym: str | None) -> tuple[str, list]:
    start, end = month_bounds(ym)
    label = f"{start:%Y-%m}"
    a, t, b, al = availability(start, end), tickets(start, end), backups(start, end), alerts(start, end)
    S: list = []  # list of ("h", text) | ("p", text) | ("t", header, rows)

    S.append(("h1", f"SLA-Bericht {start:%m/%Y} – Firmenverzeichnis Gesundheitswirtschaft"))
    S.append(("p", f"Zeitraum {start:%d.%m.%Y} 00:00 bis {(end - timedelta(seconds=1)):%d.%m.%Y} 24:00 (Europe/Berlin). "
                   f"Erstellt {datetime.now(TZ):%d.%m.%Y %H:%M} automatisch aus Messdaten."))

    # summary
    rows = []
    if a["available"]:
        rows.append(["Verfügbarkeit (gemessen)", f"≥ {AVAIL_TARGET} %".replace(".", ","), pct(a["pct"]),
                     "erfüllt" if a["pct"] is not None and a["pct"] >= AVAIL_TARGET else "NICHT erfüllt"])
    else:
        rows.append(["Verfügbarkeit", f"≥ {AVAIL_TARGET} %", "keine Daten", "offen"])
    if a["available"]:
        worst = max((i["minutes"] for i in a["incidents"]), default=0)
        rows.append(["Wiederherstellung je Ausfall", "≤ 24 h", f"längster Ausfall {worst} min",
                     "erfüllt" if worst <= RECOVERY_H * 60 else "NICHT erfüllt"])
    if t["available"]:
        miss = sum(v["missed"] for v in t["by"].values())
        rows.append(["Reaktionszeiten Support", "P1 8 h · P2 12 h · P3 24 h · Routine 3 AT",
                     f"{len(t['rows'])} Anfragen, {miss} überschritten", "erfüllt" if miss == 0 else "NICHT erfüllt"])
    else:
        rows.append(["Reaktionszeiten Support", "P1 8 h · P2 12 h · P3 24 h · Routine 3 AT", t["why"], "offen"])
    rows.append(["Tägliche Sicherung", "täglich, 30 Tage", f"{len(b['dump_ok_days'])}/{len(b['days'])} Tage",
                 "erfüllt" if not b["missing"] else "NICHT erfüllt"])
    rows.append(["Georedundante Kopie", "täglich, 2. Standort", f"{len(b['offsite_ok_days'])}/{len(b['days'])} Tage",
                 "erfüllt" if not b["offsite_missing"] else "NICHT erfüllt"])
    rt_ok = [x for x in b["restore_tests"] if x.get("ok")]
    rows.append(["Wiederherstellungstest", "wöchentlich", f"{len(rt_ok)} erfolgreich / {len(b['restore_tests'])}",
                 "erfüllt" if rt_ok and len(rt_ok) == len(b["restore_tests"]) else "prüfen"])
    S.append(("h2", "1. Übersicht"))
    S.append(("t", ["Zusage", "Ziel", "Ist", "Ergebnis"], rows))

    # availability
    S.append(("h2", "2. Verfügbarkeit"))
    if a["available"]:
        if a.get("since"):
            S.append(("p", f"Messung erst seit {fmt_dt(a['since'])} (Inbetriebnahme des Monitors)."))
        S.append(("p", f"Gemessen jede Minute von einem zweiten Rechenzentrum aus (Suche und API, je ein Abruf, ein "
                       f"Wiederholungsversuch nach 5 s). Messminuten {a['measured']} von {a['expected']}; "
                       f"verfügbar {a['up']}, nicht verfügbar {a['down']}"
                       + (f" (davon {a['maint_down']} in angekündigter Wartung, trotzdem mitgezählt)" if a["maint_down"] else "")
                       + f"; ohne Messung {a['unknown']} min."))
        S.append(("p", f"Verfügbarkeit gemessen: {pct(a['pct'])}. Konservativ (Minuten ohne Messung als Ausfall "
                       f"gezählt): {pct(a['pct_conservative'])}."))
        if a["incidents"]:
            S.append(("t", ["Beginn", "Ende", "Dauer", "Ursache (erste Fehlermeldung)", "≤ 24 h"],
                      [[fmt_dt(datetime.fromtimestamp(i["start_ts"], timezone.utc)),
                        fmt_dt(datetime.fromtimestamp(i["end_ts"], timezone.utc)) if i["end_ts"] else "andauernd",
                        f"{i['minutes']} min", (i["cause"] or "")[:60], "ja" if i["recovery_ok"] else "nein"]
                       for i in a["incidents"]]))
        else:
            S.append(("p", "Keine Ausfälle."))
    else:
        S.append(("p", "keine Daten (Monitor-Datenbank fehlt)."))

    # tickets
    S.append(("h2", "3. Support und Reaktionszeiten"))
    S.append(("p", "Servicezeit Mo–Fr 08:00–17:00 Uhr (Europe/Berlin), ohne gesetzliche Feiertage in Hamburg. "
                   "Reaktionszeit = Zeit bis zur ersten Bearbeitung durch eine Person (nicht die automatische "
                   "Eingangsbestätigung), gezählt in Service-Stunden."))
    if t["available"]:
        S.append(("t", ["Priorität", "Ziel", "Anzahl", "eingehalten", "überschritten", "offen/nicht fällig", "Ø Reaktion"],
                  [[p, f"{TARGET_HOURS[p]} h" if p != "routine" else "3 AT", v["n"], v["met"], v["missed"], v["open"],
                    "–" if v["avg_h"] is None else f"{v['avg_h']:.1f} h".replace(".", ",")] for p, v in t["by"].items()]))
        if t["rows"]:
            S.append(("t", ["Ticket", "Prio", "Eingang", "Frist", "Erste Reaktion", "Std.", "Ergebnis"],
                      [[r["number"], r["priority"] + ("" if r["priority"] == r["priority_requested"] else f" ({r['priority_requested']})"),
                        fmt_dt(r["created_at"]), fmt_dt(r["due_at"]), fmt_dt(r["first_reaction_at"]),
                        f"{r['hours']:.1f}".replace(".", ","),
                        {True: "eingehalten", False: "überschritten", None: "offen"}[r["met"]]] for r in t["rows"]]))
        S.append(("p", f"Aus Vormonaten noch offen zu Monatsbeginn: {t['carry']}. Priorität in Klammern = vom "
                       "Anfragenden gewählt, wenn nach den vereinbarten Kriterien umgestuft."))
    else:
        S.append(("p", f"keine Daten ({t['why']})."))

    # backups
    S.append(("h2", "4. Datensicherung und Wiederherstellung"))
    S.append(("p", f"Sicherungen mit Erfolg an {len(b['dump_ok_days'])} von {len(b['days'])} Tagen"
                   + (f" (fehlend: {', '.join(d.strftime('%d.%m.') for d in b['missing'])})" if b["missing"] else "")
                   + f". Kopie am zweiten Standort und extern an {len(b['offsite_ok_days'])} Tagen"
                   + (f" (fehlend: {', '.join(d.strftime('%d.%m.') for d in b['offsite_missing'])})" if b["offsite_missing"] else "")
                   + f". Derzeit vorgehalten: {b['kept']} Sicherungen"
                   + (f", älteste vom {b['oldest']:%d.%m.%Y}" if b["oldest"] else "") + "."))
    if b["restore_tests"]:
        S.append(("t", ["Wiederherstellungstest", "Sicherung", "Firmen", "Dauer", "Ergebnis"],
                  [[fmt_dt(x["_at"]), x.get("dump", ""), x.get("company", "–"), f"{x.get('seconds', '–')} s",
                    "ok" if x.get("ok") else f"FEHLER {x.get('error', '')}"] for x in b["restore_tests"]]))
    sr_ok = sum(1 for x in b["standby_restores"] if x.get("ok"))
    S.append(("p", f"Standby-System nächtlich aus der neuesten Sicherung wiederhergestellt: {sr_ok} von "
                   f"{len(b['standby_restores'])} Läufen erfolgreich."))
    if b["refresh"]:
        S.append(("t", ["Datenaktualisierung", "Art", "verifizierte Firmen", "Ergebnis"],
                  [[fmt_dt(x["_at"]), x.get("kind", ""),
                    f"{x.get('verified_before', '–')} → {x.get('verified_after', '–')}" if x.get("kind") == "monthly" else "–",
                    "ok" if x.get("ok") else "prüfen" + (f" (Quelle fehlgeschlagen: {x['failed_fetch']})" if x.get("failed_fetch") else "")]
                   for x in b["refresh"]]))

    # alerts
    S.append(("h2", "5. Alarme"))
    lv = {}
    for x in al:
        lv[x.get("level")] = lv.get(x.get("level"), 0) + 1
    S.append(("p", "Versendete Alarme: " + (", ".join(f"{k} {v}" for k, v in sorted(lv.items())) if lv else "keine") + "."))
    return label, S


def to_md(S) -> str:
    out = []
    for s in S:
        if s[0] == "h1":
            out.append(f"# {s[1]}\n")
        elif s[0] == "h2":
            out.append(f"\n## {s[1]}\n")
        elif s[0] == "p":
            out.append(s[1] + "\n")
        else:
            out.append("| " + " | ".join(map(str, s[1])) + " |")
            out.append("|" + "---|" * len(s[1]))
            out += ["| " + " | ".join(str(c).replace("|", "/") for c in r) + " |" for r in s[2]]
            out.append("")
    return "\n".join(out) + "\n"


def to_html(S, title: str) -> str:
    body = []
    for s in S:
        if s[0] in ("h1", "h2"):
            body.append(f"<{s[0]}>{html.escape(s[1])}</{s[0]}>")
        elif s[0] == "p":
            body.append(f"<p>{html.escape(s[1])}</p>")
        else:
            body.append("<table><thead><tr>" + "".join(f"<th>{html.escape(str(h))}</th>" for h in s[1]) +
                        "</tr></thead><tbody>" + "".join(
                "<tr>" + "".join(f"<td{' class=bad' if str(c).startswith(('NICHT', 'FEHLER', 'überschritten')) else ''}>"
                                 f"{html.escape(str(c))}</td>" for c in r) + "</tr>" for r in s[2]) + "</tbody></table>")
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>body{{font:14px/1.5 system-ui,sans-serif;max-width:1000px;margin:24px auto;padding:0 16px;color:#1d2433}}
table{{border-collapse:collapse;width:100%;margin:8px 0 16px}}th,td{{border-bottom:1px solid #dde1e8;text-align:left;
padding:5px 6px;vertical-align:top}}th{{font-size:12px;color:#5b6475}}td.bad{{color:#b42318;font-weight:600}}
@media print{{body{{margin:0}}}}</style></head><body>{''.join(body)}</body></html>
"""


def to_text_lines(S, width: int = 100) -> list[str]:
    import textwrap
    lines: list[str] = []
    for s in S:
        if s[0] == "h1":
            lines += [s[1], "=" * min(width, len(s[1])), ""]
        elif s[0] == "h2":
            lines += ["", s[1], "-" * len(s[1])]
        elif s[0] == "p":
            lines += textwrap.wrap(s[1], width) + [""]
        else:
            cols = [list(map(str, s[1]))] + [[str(c) for c in r] for r in s[2]]
            w = [min(36, max(len(r[i]) for r in cols)) for i in range(len(s[1]))]
            for k, r in enumerate(cols):
                lines.append("  ".join(c[:w[i]].ljust(w[i]) for i, c in enumerate(r)).rstrip()[:width + 20])
                if k == 0:
                    lines.append("  ".join("-" * x for x in w)[:width + 20])
            lines.append("")
    return lines


def to_pdf(lines: list[str], path: Path) -> None:
    """Minimal PDF 1.4: A4 landscape-free portrait, Courier 8 pt, 80 lines/page, WinAnsi (cp1252) text."""
    def esc(s: str) -> bytes:
        b = s.replace("≥", ">=").replace("≤", "<=").replace("→", "->").replace("Ø", "avg").encode("cp1252", "replace")
        return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    per = 80
    pages = [lines[i:i + per] for i in range(0, len(lines), per)] or [[]]
    objs: list[bytes] = []
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{4 + 2 * i} 0 R" for i in range(len(pages)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    for n, pg in enumerate(pages):
        content = b"BT /F1 8 Tf 9.6 TL 36 806 Td " + b"".join(b"(" + esc(ln) + b") '" for ln in pg) + \
            f" ET BT /F1 7 Tf 500 20 Td (Seite {n + 1}/{len(pages)}) Tj ET".encode()
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> "
                    f"/Contents {5 + 2 * n} 0 R >>".encode())
        objs.append(f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offs = []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offs)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    path.write_bytes(bytes(out))


def main(ym: str | None = None) -> list[Path]:
    label, S = build(ym)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    base = REPORT_DIR / f"sla-{label}"
    title = S[0][1]
    base.with_suffix(".md").write_text(to_md(S), encoding="utf-8")
    base.with_suffix(".html").write_text(to_html(S, title), encoding="utf-8")
    to_pdf(to_text_lines(S), base.with_suffix(".pdf"))
    files = [base.with_suffix(x) for x in (".md", ".html", ".pdf")]
    print("written:", *files)
    return files


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
