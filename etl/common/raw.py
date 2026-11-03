"""Raw-before-parse: every payload lands on disk first; Postgres only keeps path + sha256."""

from __future__ import annotations

import datetime as dt
import hashlib
from pathlib import Path

from etl.common.env import raw_dir


def run_dir(source: str, day: str | None = None) -> Path:
    path = raw_dir() / source / (day or dt.date.today().isoformat())
    path.mkdir(parents=True, exist_ok=True)
    return path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def latest_dir(source: str) -> Path:
    base = raw_dir() / source
    days = sorted(p for p in base.iterdir() if p.is_dir())
    if not days:
        raise FileNotFoundError(f"no raw data for {source}")
    return days[-1]
