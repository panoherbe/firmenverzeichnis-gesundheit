"""Load the repo's .env (KEY=VALUE lines) into os.environ without overriding."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def load_env() -> None:
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def raw_dir() -> Path:
    load_env()
    return Path(os.environ.get("RAW_DIR", ROOT / "raw"))
