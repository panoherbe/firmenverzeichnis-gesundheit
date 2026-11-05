"""
Strict re-check of every counted contact path of the register-verified companies (2026-11-05 night). Why: a random
hand-checked sample of 100 found contact links picked from product names ("Kontaktlinsen", "kontaktlose Thermometer",
a newsletter page), dead sites and e-mail domains without a website of the company (70/100 fully right).

A contact path counts only if, today:
  contact page  the URL's last path segment IS a contact/imprint word (not merely contains it), it answers 200 with
                HTML on the company's host, robots.txt allows it, and the text has an imprint/contact keyword, an
                e-mail address or phone number, and a distinctive word of the company name;
  else          the usual contact/imprint paths of the company's site are tried with the same test (search again);
  e-mail only   the mailbox's domain serves a page (home or imprint) that names the company.
Raw before parse: raw/contact_recheck/<day>/recheck.jsonl {cid, kind, url, ok, why}; pages/ holds the HTML (gzip,
sha256 in the record). etl.enrich applies the result: contact_url only from ok rows, email_role dropped when its
domain did not pass.

    python -m etl.sources.contact_recheck            # first pass, 16 threads
    python -m etl.sources.contact_recheck --retry    # failures from blocking/timeouts again, 3 threads, 2 s pause
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sys
import time
import threading
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import httpx

from etl.common import resolver
from etl.common.db import connect
from etl.common.fetch import USER_AGENT
from etl.common.raw import latest_dir, run_dir
from etl.sources.contact_probe import CT, KW, MORE, name_words

STRICT = re.compile(r"^(?:kontakt|contact|impressum|imprint|legal|legal-notice|rechtliches|impressum-datenschutz)"
                    r"(?:[-_](?:us|uns|formular|form|page|seite|zu-uns|de|en))?(?:\.(?:html?|php|aspx?))?$", re.I)
TAG = re.compile(r"<(script|style|noscript)[^>]*>.*?</\1>|<[^>]+>", re.I | re.S)


def strict_url(url: str) -> bool:
    segs = [s for s in urlparse(url).path.split("/") if s]
    return bool(segs) and bool(STRICT.match(segs[-1]))


def host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def main() -> None:
    resolver.install()
    retry = "--retry" in sys.argv
    with connect() as conn:
        rows = conn.execute("""SELECT id, name, website, contact_url, email_role, post_code FROM company
            WHERE verified AND hidden_reason IS NULL AND (contact_url IS NOT NULL OR email_role IS NOT NULL)""").fetchall()
    out = latest_dir("contact_recheck") if retry else run_dir("contact_recheck")  # retry writes next to the first pass
    if retry:  # only rows that failed for reasons a slower second visit can change (blocking, timeouts, 5xx)
        again = {r["cid"] for r in map(json.loads, (out / "recheck.jsonl").open())
                 if not r["ok"] and re.search(r"429|403|202|5\d\d|Timeout|ConnectError|ReadError|RemoteProtocol|company name missing", r["why"])}
        # a company with a register role mailbox already counts (etl.sources.mail_mx); the slow pass only matters
        # for the others (2026-11-05: 789 -> the few hundred without a mailbox)
        rows = [r for r in rows if r[0] in again and not r[4]]
        print(f"retry: {len(rows)} companies", flush=True)
    (out / "pages").mkdir(exist_ok=True)
    lock, local = threading.Lock(), threading.local()
    robots: dict[str, urllib.robotparser.RobotFileParser] = {}

    def client() -> httpx.Client:
        if not hasattr(local, "c"):
            local.c = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "text/html"}, timeout=12,
                                   follow_redirects=True)
        return local.c

    def allowed(url: str) -> bool:
        base = urlparse(url).scheme + "://" + urlparse(url).netloc
        with lock:
            rp = robots.get(base)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = client().get(base + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except Exception:  # noqa: BLE001
                rp.parse([])
            with lock:
                robots[base] = rp
        return rp.can_fetch(USER_AGENT, url)

    def page(url: str) -> tuple[str | None, str, str]:
        """(visible lower-case text or None, final url, why)."""
        if retry:
            time.sleep(2)
        if not allowed(url):
            return None, url, "robots"
        try:
            r = client().get(url)
        except Exception as exc:  # noqa: BLE001  unreachable sites are data
            return None, url, type(exc).__name__
        if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
            return None, str(r.url), f"http {r.status_code}"
        raw = r.content
        sha = hashlib.sha256(raw).hexdigest()
        (out / "pages" / f"{sha}.html.gz").write_bytes(gzip.compress(raw))
        return re.sub(r"\s+", " ", TAG.sub(" ", r.text)).lower(), str(r.url) + "#sha=" + sha, "ok"

    def passes(text: str, name: str, post_code: str | None = None) -> str:
        if not KW.search(text):
            return "no keyword"
        if not CT.search(text):
            return "no e-mail/phone"
        # the page must be this company's: a distinctive word of its name, or (second pass) its register postcode,
        # which an imprint states with the address
        if not any(w in text for w in name_words(name)) and not (retry and post_code and re.search(rf"\b{post_code}\b", text)):
            return "company name missing"
        return "ok"

    def one(row) -> dict:
        cid, name, website, contact_url, email, post_code = row
        rec = {"cid": cid}
        tried = []
        if contact_url:
            if strict_url(contact_url):
                text, final, why = page(contact_url)
                why = passes(text, name, post_code) if text is not None else why
                if why == "ok" and host(final) == host(contact_url):
                    return {**rec, "kind": "contact", "url": final.split("#sha=")[0], "sha": final.split("#sha=")[-1],
                            "ok": True, "why": "ok"}
                tried.append(f"stored: {why}")
            else:
                tried.append("stored: not a contact/imprint address")
        base = website or contact_url or (f"https://{email.split('@')[1]}" if email else None)
        if base:
            root = urlparse(base).scheme + "://" + urlparse(base).netloc
            for p in MORE:
                u = urljoin(root, p)
                text, final, why = page(u)
                if text is None:
                    if why not in ("http 404", "http 410", "http 403", "robots"):
                        tried.append(f"{p}: {why}")
                        break  # host down: do not hammer it with every path
                    continue
                if host(final.split("#sha=")[0]) != host(root):
                    continue
                if passes(text, name, post_code) == "ok":
                    return {**rec, "kind": "contact", "url": final.split("#sha=")[0], "sha": final.split("#sha=")[-1],
                            "ok": True, "why": "found again"}
        if email:
            d = email.split("@")[1]
            for u in (f"https://{d}/", f"https://www.{d}/"):
                text, final, why = page(u)
                if text is not None and any(w in text for w in name_words(name)):
                    return {**rec, "kind": "email", "url": email, "ok": True, "why": "domain names company"}
                tried.append(f"{u}: {why if text is None else 'company name missing'}")
        return {**rec, "kind": None, "url": None, "ok": False, "why": "; ".join(tried)[:300]}

    n_ok = 0
    with (out / ("recheck2.jsonl" if retry else "recheck.jsonl")).open("w") as f, \
            ThreadPoolExecutor(max_workers=3 if retry else 16) as pool:
        for rec in pool.map(one, rows):
            n_ok += rec["ok"]
            f.write(json.dumps(rec) + "\n")
    print(f"contact recheck: {len(rows)} companies, {n_ok} with a contact path that passed", flush=True)


if __name__ == "__main__":
    main()
