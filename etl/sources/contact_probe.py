"""
Second pass for companies whose website check found no contact link: the legally required imprint (§ 5 DDG names an
e-mail address) or a contact page, found by (1) a link whose text or path says Impressum/Imprint/Kontakt/Contact on the
stored home page of the newest websites run, else (2) the usual paths (/kontakt, /contact, /impressum, /imprint, also
with /de/ and /en/). A candidate counts only if it answers 200 with HTML on the company's own host and robots.txt
allows it. Raw before parse: raw/contact_probe/<day>/probe.jsonl {cid, url, kind, status}; etl.enrich reads it.

    python -m etl.sources.contact_probe fetch            # first pass
    python -m etl.sources.contact_probe fetch --retry    # second pass over the still missing ones (probe2.jsonl)
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


def main() -> None:
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
