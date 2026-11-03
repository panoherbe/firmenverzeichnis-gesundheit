"""
One alert function for everything that must reach Pano's phone: the monitor (site down / recovered), the support
form (P1/P2 tickets) and failed systemd jobs (OnFailure=).

    from ops.alert import send_alert
    send_alert("Firmenverzeichnis nicht erreichbar", "seit 6 min, /api/health: timeout", level="P1")

Channels (env, all optional, never hardcoded — put them in the server's .env):

    NTFY_TOPIC   topic name (long random string; the topic name IS the secret on ntfy.sh)
    NTFY_URL     server, default https://ntfy.sh (or a self-hosted ntfy)
    NTFY_TOKEN   access token, only if the ntfy server requires one
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID   bot from @BotFather, chat id of Pano's chat with the bot

Levels: P1 (ntfy priority 5 = "urgent": rings through Do-Not-Disturb if the ntfy app allows it), P2 (priority 4),
P3/info (priority 3), ok (recovered, priority 3). Telegram cannot force a ring; ntfy is the phone-alarm channel,
Telegram the readable log. Every alert is also appended to var/ops/alerts.jsonl (so the SLA report and a cold reader
can see what was sent, and nothing is lost when no channel is configured).

CLI (used by systemd OnFailure=firmen-alert@%n.service):

    python -m ops.alert --unit firmen-backup.service      # alert "job failed" with the unit's last log lines
    python -m ops.alert --test                            # send a test alert on every configured channel
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
NTFY_PRIORITY = {"P1": 5, "P2": 4, "P3": 3, "info": 3, "ok": 3}
TAGS = {"P1": ["rotating_light"], "P2": ["warning"], "P3": ["information_source"], "info": ["information_source"],
        "ok": ["white_check_mark"]}


def _env() -> None:
    try:
        from etl.common.env import load_env
        load_env()
    except Exception:  # noqa: BLE001 - alerting must work even if the repo's env loader is broken
        pass


def ops_dir() -> Path:
    d = Path(os.environ.get("OPS_LOG_DIR", ROOT / "var" / "ops"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def send_alert(title: str, message: str, level: str = "P3", key: str | None = None) -> dict:
    """Send to every configured channel. Never raises; returns {channel: "ok" | error text}."""
    _env()
    level = level if level in NTFY_PRIORITY else "P3"
    host = os.environ.get("ALERT_HOST_LABEL") or socket.gethostname()
    full_title = f"[{level}] {title}"
    body = f"{message}\n— {host}, {datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M %Z}"
    result: dict[str, str] = {}

    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        try:
            headers = {"Authorization": f"Bearer {os.environ['NTFY_TOKEN']}"} if os.environ.get("NTFY_TOKEN") else {}
            # JSON publishing: titles with umlauts work (HTTP headers would need RFC 2047 encoding)
            r = httpx.post(os.environ.get("NTFY_URL", "https://ntfy.sh").rstrip("/"), timeout=15, headers=headers,
                           json={"topic": topic, "title": full_title, "message": body,
                                 "priority": NTFY_PRIORITY[level], "tags": TAGS[level]})
            result["ntfy"] = "ok" if r.status_code < 300 else f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            result["ntfy"] = f"error {type(e).__name__}: {e}"

    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            r = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=15,
                           json={"chat_id": chat, "text": f"{full_title}\n{body}", "disable_web_page_preview": True})
            result["telegram"] = "ok" if r.status_code < 300 else f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001
            result["telegram"] = f"error {type(e).__name__}"  # never echo the URL: it contains the bot token

    if not result:
        result["none"] = "no channel configured (NTFY_TOPIC / TELEGRAM_*)"
        print(f"ALERT (no channel): {full_title}: {message}", file=sys.stderr)
    try:
        with (ops_dir() / "alerts.jsonl").open("a") as f:
            f.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "level": level,
                                "title": title, "message": message[:2000], "key": key, "result": result},
                               ensure_ascii=False) + "\n")
    except OSError:
        pass
    return result


def _unit_failed(unit: str) -> None:
    try:
        tail = subprocess.run(["journalctl", "-u", unit, "-n", "15", "--no-pager", "-o", "cat"],
                              capture_output=True, text=True, timeout=20).stdout[-1500:]
    except Exception:  # noqa: BLE001
        tail = "(journalctl not available)"
    # A failed backup/restore/refresh is not an outage: P3 (no night-time ring). The app itself is restarted by
    # systemd; an outage is detected and escalated by the external monitor.
    send_alert(f"Job fehlgeschlagen: {unit}", tail or "(no log)", level="P3", key=f"unit:{unit}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--unit"] and len(sys.argv) > 2:
        _unit_failed(sys.argv[2])
    elif sys.argv[1:2] == ["--test"]:
        print(send_alert("Testalarm", "Wenn du das liest, funktioniert der Alarmweg.", level="P1", key="test"))
    else:
        print(__doc__)
