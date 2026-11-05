"""
Second pass for companies whose website check found no contact link: the legally required imprint (§ 5 DDG names an
e-mail address) or a contact page, found by (1) a link whose text or path says Impressum/Imprint/Kontakt/Contact on the
stored home page of the newest websites run, else (2) the usual paths (/kontakt, /contact, /impressum, /imprint, also
with /de/ and /en/). A candidate counts only if it answers 200 with HTML on the company's own host and robots.txt
allows it. Raw before parse: raw/contact_probe/<day>/probe.jsonl {cid, url, kind, status}; etl.enrich reads it.

    python -m etl.sources.contact_probe fetch            # first pass
    python -m etl.sources.contact_probe fetch --retry    # second pass over the still missing ones (probe2.jsonl)
    python -m etl.sources.contact_probe check            # content check of every candidate -> verified_ok.json

The check (2026-11-05): a candidate counts only if the page text has an imprint/contact keyword, an e-mail address or
a phone number, and a distinctive word of the company name (no legal form, at least 4 letters). It covers the probe
hits and every contact link found on a website reached through a register e-mail domain (no website in the register).
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import threading
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlparse

import httpx

from etl.common import resolver
from etl.common.db import connect
from etl.common.fetch import USER_AGENT
from etl.common.raw import latest_dir, run_dir
from etl.sources.websites import links

LINK = re.compile(r"(impressum|imprint|kontakt|contact|legal-notice)", re.I)
PATHS = ["/kontakt", "/kontakt/", "/contact", "/contact/", "/impressum", "/impressum/", "/imprint", "/de/kontakt",
         "/en/contact", "/de/impressum", "/en/imprint"]
# second pass (--retry): more spellings, host variants and a fresh home page for the companies the first pass missed
MORE = PATHS + ["/impressum.html", "/impressum.php", "/imprint.html", "/kontakt.html", "/kontakt.php", "/contact.html",
                "/de/impressum.html", "/de/kontakt.html", "/legal", "/legal-notice", "/rechtliches", "/impressum-datenschutz"]


def variants(url: str) -> list[str]:
    host = urlparse(url).netloc.removeprefix("www.")
    return list(dict.fromkeys([url] + [f"{sch}://{w}{host}/" for sch in ("https", "http") for w in ("www.", "")]))


KW = re.compile(r"impressum|imprint|kontakt|contact|legal notice|angaben gem", re.I)
CT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\btel\.?|telefon|phone|\+49", re.I)
STOP = {"gmbh", "mbh", "co", "kg", "ag", "se", "kgaa", "ohg", "ug", "haftungsbeschränkt", "und", "the", "and", "für",
        "deutschland", "germany", "gesellschaft", "medical", "medizintechnik", "pharma", "health", "care", "service",
        "services", "group", "holding", "international", "vertrieb", "technik", "systems", "products", "produkte"}


def name_words(name: str) -> list[str]:
    words = re.findall(r"[a-zäöüß]{4,}", name.lower())
    return [w for w in words if w not in STOP] or [w for w in words if w not in {"gmbh", "kgaa"}]


def check() -> None:
    resolver.install()
    pdir = latest_dir("contact_probe")
    wdir = latest_dir("websites")
    with connect() as conn:
        comp = {cid: (name, website) for cid, name, website in
                conn.execute("SELECT id, name, website FROM company WHERE verified AND hidden_reason IS NULL").fetchall()}
    cands: dict[str, str] = {}
    for line in (wdir / "sites.jsonl").open():
        j = json.loads(line)
        if j.get("contact") and j["cid"] in comp and not comp[j["cid"]][1]:  # site reached via e-mail domain
            cands[j["cid"]] = j["contact"]
    for name in ("probe.jsonl", "probe2.jsonl"):
        if (pdir / name).exists():
            for line in (pdir / name).open():
                r = json.loads(line)
                if r.get("status") == 200 and r["cid"] in comp:
                    cands.setdefault(r["cid"], r["url"])
    c = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True)
    ok: dict[str, str] = {}
    reasons: dict[str, int] = {}

    robots: dict[str, urllib.robotparser.RobotFileParser] = {}
    rlock = threading.Lock()

    def allowed(url: str) -> bool:
        """robots.txt of the page's host, fetched once per host (endcheck 2026-11-05: the check fetched without it)."""
        host = urlparse(url).scheme + "://" + urlparse(url).netloc
        with rlock:
            rp = robots.get(host)
        if rp is None:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = c.get(host + "/robots.txt")
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            with rlock:
                robots[host] = rp
        return rp.can_fetch(USER_AGENT, url)

    def one(item):
        cid, url = item
        try:
            if not allowed(url):
                return cid, url, "robots"
            html = c.get(url).text
        except Exception:  # noqa: BLE001
            return cid, url, "fetch"
        text = re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)).lower()
        if not KW.search(text):
            return cid, url, "no keyword"
        if not CT.search(text):
            return cid, url, "no e-mail/phone"
        if not any(w in text for w in name_words(comp[cid][0])):
            return cid, url, "company name missing"
        return cid, url, "ok"

    with ThreadPoolExecutor(max_workers=12) as pool:
        for cid, url, why in pool.map(one, cands.items()):
            reasons[why] = reasons.get(why, 0) + 1
            if why == "ok":
                ok[cid] = url
    (pdir / "verified_ok.json").write_text(json.dumps(ok, indent=0))
    print(f"  check: {len(cands)} candidates, {reasons}")


def main() -> None:
    if sys.argv[1:2] == ["check"]:
        return check()
    if sys.argv[1:2] != ["fetch"]:
        sys.exit(__doc__)
    retry = "--retry" in sys.argv
    resolver.install()
    wdir = latest_dir("websites")
    sites = {}
    for line in (wdir / "sites.jsonl").open():
        j = json.loads(line)
        sites[j["cid"]] = j
    with connect() as conn:
        todo = [r[0] for r in conn.execute("""SELECT id FROM company WHERE verified AND hidden_reason IS NULL
                   AND contact_url IS NULL AND email_role IS NULL AND website IS NOT NULL""").fetchall()]
    out = run_dir("contact_probe") / ("probe2.jsonl" if retry else "probe.jsonl")
    print(f"  contact probe: {len(todo)} companies", flush=True)
    lock, local = threading.Lock(), threading.local()

    def one(cid: str) -> None:
        if not hasattr(local, "c"):
            local.c = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "text/html"}, timeout=15,
                                   follow_redirects=True)
        s = sites.get(cid) or {}
        home = next((p for p in s.get("pages", []) if p.get("kind") == "home" and p.get("file")), None)
        rec = {"cid": cid, "url": None, "kind": None, "status": "none"}
        if retry:
            rec.update(retry_one(local.c, cid, s))
            with lock:
                f.write(json.dumps(rec) + "\n")
            return
        try:
            base = (home or {}).get("final") or s.get("start")
            if not base or s.get("robots") != "ok":
                rec["status"] = "no site" if not base else "robots"
            else:
                rp = urllib.robotparser.RobotFileParser()
                try:
                    r = local.c.get(urljoin(base, "/robots.txt"))
                    rp.parse(r.text.splitlines() if r.status_code == 200 else [])
                except httpx.HTTPError:
                    rp.parse([])
                cands = []
                if home:
                    html = gzip.decompress((wdir / home["file"]).read_bytes()).decode("utf-8", "replace")
                    cands = [u for u, text in links(html, base) if LINK.search(urlparse(u).path) or LINK.search(text)]
                cands += [urljoin(base, p) for p in PATHS]
                host = urlparse(base).netloc.removeprefix("www.")
                for u in dict.fromkeys(cands):
                    if not rp.can_fetch(USER_AGENT, u):
                        continue
                    resp = local.c.get(u)
                    if (resp.status_code == 200 and "html" in resp.headers.get("content-type", "")
                            and urlparse(str(resp.url)).netloc.removeprefix("www.") == host):
                        kind = "imprint" if re.search(r"impressum|imprint|legal", u, re.I) else "contact"
                        rec.update(url=str(resp.url), kind=kind, status=200)
                        break
        except Exception as exc:  # noqa: BLE001  unreachable sites are data
            rec["status"] = type(exc).__name__
        with lock:
            f.write(json.dumps(rec) + "\n")

    def retry_one(c: httpx.Client, cid: str, s: dict) -> dict:
        start = (next((p for p in s.get("pages", []) if p.get("kind") == "home"), {}) or {}).get("final") or s.get("start")
        if not start:
            return {"status": "no site"}
        last = "none"
        for base in variants(start):
            try:
                home = c.get(base)
            except httpx.HTTPError as exc:
                last = type(exc).__name__
                continue
            if home.status_code >= 400:
                last = f"http {home.status_code}"
                continue
            base = str(home.url)
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = c.get(urljoin(base, "/robots.txt"))
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            if not rp.can_fetch(USER_AGENT, base):
                return {"status": "robots"}
            host = urlparse(base).netloc.removeprefix("www.")
            cands = [u for u, text in links(home.text, base) if LINK.search(urlparse(u).path) or LINK.search(text)]
            for u in dict.fromkeys(cands + [urljoin(base, p) for p in MORE]):
                if not rp.can_fetch(USER_AGENT, u):
                    continue
                try:
                    resp = c.get(u)
                except httpx.HTTPError:
                    continue
                if (resp.status_code == 200 and "html" in resp.headers.get("content-type", "")
                        and urlparse(str(resp.url)).netloc.removeprefix("www.") == host):
                    kind = "imprint" if re.search(r"impressum|imprint|legal|rechtlich", u, re.I) else "contact"
                    return {"url": str(resp.url), "kind": kind, "status": 200}
            return {"status": "none"}
        return {"status": last}

    with out.open("w") as f, ThreadPoolExecutor(max_workers=12) as pool:
        for _ in pool.map(one, todo):
            pass
    print("contact probe done", flush=True)


if __name__ == "__main__":
    main()
