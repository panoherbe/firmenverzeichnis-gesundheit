"""
Apply every idempotent schema in order (bootstrap, deploy, after a restore of an older dump):

    db/schema.sql · etl.enrich.COLUMNS · etl.cleanup.SCHEMA · web.auth.SCHEMA · web.support.SCHEMA · web.matchmaking.SCHEMA

Modules that do not exist (yet) are skipped with a note. All statements are CREATE/ALTER … IF NOT EXISTS.

    python -m ops.schema
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import psycopg

from etl.common.env import load_env

ROOT = Path(__file__).resolve().parents[1]
PARTS = [("etl.enrich", "COLUMNS"), ("etl.cleanup", "SCHEMA"), ("web.auth", "SCHEMA"), ("web.support", "SCHEMA"), ("web.matchmaking", "SCHEMA"), ("web.gate", "SCHEMA")]


def main() -> None:
    load_env()
    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as c:
        c.execute((ROOT / "db" / "schema.sql").read_text())
        print("schema: db/schema.sql")
        for mod, attr in PARTS:
            try:
                sql = getattr(importlib.import_module(mod), attr)
            except (ImportError, AttributeError) as e:
                print(f"schema: {mod}.{attr} skipped ({type(e).__name__})")
                continue
            c.execute(sql)
            print(f"schema: {mod}.{attr}")


if __name__ == "__main__":
    main()
