"""Connections: our own database (`firmen`) and the read-only tender corpus (`ausschreibungen`)."""

from __future__ import annotations

import os
from pathlib import Path

import psycopg

from etl.common.env import load_env


def connect() -> psycopg.Connection:
    load_env()
    # autocommit: psycopg3 otherwise never commits unless told to (trap in the ausschreibungen handover)
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def connect_corpus() -> psycopg.Connection:
    """
    Read-only session on the tender corpus. Either CORPUS_DATABASE_URL, or CORPUS_ENV_FILE pointing at another
    project's .env whose DATABASE_URL is used (so the password lives in one place only). We never write there.
    """
    load_env()
    url = os.environ.get("CORPUS_DATABASE_URL")
    if not url and os.environ.get("CORPUS_ENV_FILE"):
        for line in Path(os.environ["CORPUS_ENV_FILE"]).expanduser().read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                url = line.split("=", 1)[1].strip()
    if not url:
        raise RuntimeError("set CORPUS_DATABASE_URL or CORPUS_ENV_FILE")
    conn = psycopg.connect(url, autocommit=True)
    conn.execute("SET default_transaction_read_only = on")
    return conn
