"""
Company websites: home page plus up to two linked pages (about/company, careers), for three facts only:
  - the contact page link (profile field "Verweis auf die Kontaktseite"),
  - employee figures the company states about itself (size-class filter), with the sentence as evidence,
  - whether the site is reachable (cleanup rule: unreachable in two consecutive cycles).
Text and data mining under § 44b UrhG: robots.txt is honoured (our user agent and "*"); a site that disallows "/"
is recorded as `robots` and not fetched. No page text is republished; only the link and the parsed number.

Raw layout: raw/websites/<day>/pages/<sha1(url)>.html.gz and raw/websites/<day>/sites.jsonl (one line per company:
  {cid, start, robots, pages: [{url, status, final, file, sha256, kind}]}). Resumable by cid.
Only companies with a website or an e-mail domain; one request at a time per host, 12 hosts in parallel.

    python -m etl.sources.websites fetch
"""

from __future__ import annotations

import gzip
import hashlib
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
from etl.common.raw import run_dir

FREEMAIL = {"gmail.com", "gmx.de", "gmx.net", "web.de", "t-online.de", "yahoo.com", "yahoo.de", "outlook.com",
            "hotmail.com", "hotmail.de", "icloud.com", "posteo.de", "freenet.de", "aol.com", "online.de", "arcor.de"}
ABOUT = re.compile(r"(ueber-uns|über-uns|uber-uns|about|unternehmen|company|profil|wir-ueber|who-we-are|firma)", re.I)
CAREER = re.compile(r"(karriere|career|jobs|stellen)", re.I)
CONTACT = re.compile(r"(kontakt|contact)", re.I)
HREF = re.compile(r"""<a\s[^>]*href=["']([^"'#]+)["'][^>]*>(.*?)</a>""", re.I | re.S)
MAX_BYTES = 2_000_000


def start_url(website: str | None, domain: str | None) -> str | None:
    if website:
        return website if website.startswith("http") else "https://" + website
    if domain and domain not in FREEMAIL:
        return "https://www." + domain if domain.count(".") == 1 else "https://" + domain
    return None


def links(html: str, base: str) -> list[tuple[str, str]]:
    host = urlparse(base).netloc.removeprefix("www.")
    out = []
    for href, text in HREF.findall(html):
        url = urljoin(base, href.strip())
        if urlparse(url).scheme in ("http", "https") and urlparse(url).netloc.removeprefix("www.") == host:
            out.append((url, re.sub(r"<[^>]+>", " ", text)[:80]))
    return out


def pick(cands: list[tuple[str, str]], rx: re.Pattern) -> str | None:
    for url, text in cands:
        if rx.search(urlparse(url).path) or rx.search(text):
            return url
    return None


def main() -> None:
    if sys.argv[1:] != ["fetch"]:
        sys.exit(__doc__)
    resolver.install()
    out = run_dir("websites")
    (out / "pages").mkdir(exist_ok=True)
    index = out / "sites.jsonl"
    done = {json.loads(l)["cid"] for l in index.open()} if index.exists() else set()
    with connect() as conn:
        rows = conn.execute("SELECT id, website, domain FROM company ORDER BY verified DESC, tier, id").fetchall()
    todo = [(cid, start_url(w, d)) for cid, w, d in rows if cid not in done and start_url(w, d)]
    print(f"  websites: {len(done)} done, {len(todo)} to fetch", flush=True)
    lock, local, count = threading.Lock(), threading.local(), [0]

    def save(url: str, resp: httpx.Response, kind: str) -> dict:
        body = resp.content[:MAX_BYTES]
        name = hashlib.sha1(url.encode()).hexdigest() + ".html.gz"
        (out / "pages" / name).write_bytes(gzip.compress(body))
        return {"url": url, "status": resp.status_code, "final": str(resp.url), "file": f"pages/{name}",
                "sha256": hashlib.sha256(body).hexdigest(), "kind": kind}

    def one(item: tuple[str, str]) -> None:
        cid, url = item
        if not hasattr(local, "c"):
            local.c = httpx.Client(headers={"User-Agent": USER_AGENT, "Accept": "text/html,*/*;q=0.5",
                                            "Accept-Language": "de,en;q=0.8"}, timeout=15, follow_redirects=True)
        rec: dict = {"cid": cid, "start": url, "robots": "ok", "pages": []}
        try:
            rp = urllib.robotparser.RobotFileParser()
            try:
                r = local.c.get(urljoin(url, "/robots.txt"))
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            if not rp.can_fetch(USER_AGENT, url):
                rec["robots"] = "disallowed"
            else:
                home = local.c.get(url)
                rec["pages"].append(save(url, home, "home"))
                if home.status_code == 200 and "html" in home.headers.get("content-type", "html"):
                    cands = links(home.text, str(home.url))
                    rec["contact"] = pick(cands, CONTACT)
                    for kind, rx in (("about", ABOUT), ("career", CAREER)):
                        u = pick(cands, rx)
                        if u and rp.can_fetch(USER_AGENT, u):
                            try:
                                rec["pages"].append(save(u, local.c.get(u), kind))
                            except httpx.HTTPError as exc:
                                rec["pages"].append({"url": u, "status": type(exc).__name__, "kind": kind})
        except Exception as exc:  # noqa: BLE001  unreachable sites are data, not errors
            rec["pages"].append({"url": url, "status": type(exc).__name__, "kind": "home"})
        with lock:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            count[0] += 1
            if count[0] % 250 == 0:
                f.flush()
                print(f"  websites {count[0]}/{len(todo)}", flush=True)

    with index.open("a") as f, ThreadPoolExecutor(max_workers=12) as pool:
        for _ in pool.map(one, todo):
            pass
    print("websites fetch done", flush=True)


if __name__ == "__main__":
    main()
