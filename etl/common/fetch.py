"""One polite HTTP client for every collector. Installs the DoH resolver first (for hosts without DNS)."""

from __future__ import annotations

import time

import httpx

from etl.common import resolver

USER_AGENT = "firmenverzeichnis-gesundheit/0.1 (research crawler; contact: kontakt@panoherbe.com)"


def client(timeout: float = 60.0) -> httpx.Client:
    resolver.install()
    return httpx.Client(
        headers={"User-Agent": USER_AGENT, "Accept": "application/json, */*"},
        timeout=timeout,
        follow_redirects=True,
    )


def get(c: httpx.Client, url: str, *, params: dict | None = None, tries: int = 4, pause: float = 0.5) -> httpx.Response:
    """GET with backoff on 429/5xx and network errors. Sleeps `pause` after every call."""
    for attempt in range(tries):
        try:
            resp = c.get(url, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise httpx.HTTPStatusError(str(resp.status_code), request=resp.request, response=resp)
            time.sleep(pause)
            return resp
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if attempt == tries - 1:
                raise
            # 429 (rate limit) waits much longer than a transient 5xx
            status = getattr(getattr(exc, "response", None), "status_code", 0)
            time.sleep(30 * (attempt + 1) if status == 429 else 2 ** attempt * 2)
    raise RuntimeError("unreachable")
