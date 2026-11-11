"""
Shared pieces for all web modules: DB connection, templates, language (de/en), translations.

Every router module (web/app.py, web/auth.py, web/matchmaking.py, web/support.py) imports from here, so they share
one template environment and one translation table.

Language: `?lang=en|de` sets a cookie `lang`; otherwise the cookie, otherwise the browser's Accept-Language, default
German. In templates `{{ _("Deutscher Text") }}` returns the English text when lang is en and a translation exists;
a module adds its own pairs with `register({"Deutsch": "English", ...})` at import time.
"""

from __future__ import annotations

import contextvars
import os
from contextlib import contextmanager
from pathlib import Path

import psycopg
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row

from etl.common.env import load_env

load_env()
DSN = os.environ["DATABASE_URL"]

# operator of this installation (imprint § 5 DDG, privacy notice Art. 13 GDPR); a customer installation sets its own
OPERATOR = {
    "name": os.environ.get("OPERATOR_NAME", "Panagiotis Herbe"),
    "address": os.environ.get("OPERATOR_ADDRESS", "Stockhausenstr. 10, 22305 Hamburg, Deutschland"),
    # joined at runtime so no plain address sits in the public source (bot spam, 2026-11-11); the pages still show it
    "email": os.environ.get("OPERATOR_EMAIL", "@".join(("kontakt", "panoherbe.com"))),
    "imprint_url": os.environ.get("IMPRINT_URL", "https://www.panoherbe.com/impressum"),
    "authority": os.environ.get("PRIVACY_AUTHORITY", "Der Hamburgische Beauftragte für Datenschutz und "
                                "Informationsfreiheit, Ludwig-Erhard-Str. 22, 20459 Hamburg"),
}

LANG: contextvars.ContextVar[str] = contextvars.ContextVar("lang", default="de")
EN: dict[str, str] = {}


def register(pairs: dict[str, str]) -> None:
    EN.update(pairs)


def _(text: str) -> str:
    return EN.get(text, text) if LANG.get() == "en" else text


def lang() -> str:
    return LANG.get()


def pick_lang(query: str | None, cookie: str | None, accept: str | None) -> str:
    for v in (query, cookie):
        if v in ("de", "en"):
            return v
    return "en" if accept and not accept.lower().startswith("de") and "en" in accept.lower()[:5] else "de"


@contextmanager
def db():
    with psycopg.connect(DSN, autocommit=True, row_factory=dict_row) as conn:
        yield conn


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.globals.update(_=_, lang=lang, OPERATOR=OPERATOR)
