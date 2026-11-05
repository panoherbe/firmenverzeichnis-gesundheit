"""
Search syntax → Postgres tsquery (config 'simple').

    endoskop UND (tuttlingen ODER berlin) NICHT dental      German or English operators: UND/AND, ODER/OR, NICHT/NOT
    "b braun"                                              phrase (words in this order)
    implant*                                               prefix (a plain word of 4+ letters also matches as a word
                                                           stem: "implantat" finds "Implantate", 2026-11-06)
    -dental                                                same as NICHT dental
Two words without an operator mean AND. Unbalanced brackets or stray operators are repaired, never an error.
Returns (tsquery text, list of plain terms for ILIKE on the name) or (None, []) when nothing searchable is left.
"""

from __future__ import annotations

import re
import unicodedata

TOKEN = re.compile(r'"([^"]*)"|(\()|(\))|([^\s()"]+)')
OPS = {"und": "&", "and": "&", "oder": "|", "or": "|", "nicht": "!", "not": "!"}


def clean(word: str) -> str:
    word = unicodedata.normalize("NFKC", word).lower()
    return re.sub(r"[^\w.-]", "", word).strip(".-")


def term(word: str) -> str | None:
    prefix = word.endswith("*") or (len(clean(word)) >= 4 and clean(word).isalpha())
    parts = [clean(p) for p in re.split(r"[.\-/]", word.rstrip("*"))]
    parts = [p for p in parts if p]
    if not parts:
        return None
    t = " <-> ".join(parts)
    if prefix:
        t = (t[: t.rfind(parts[-1])] + parts[-1] + ":*") if len(parts) > 1 else parts[0] + ":*"
    return f"({t})" if len(parts) > 1 else t


def parse(q: str) -> tuple[str | None, list[str]]:
    out: list[str] = []          # tokens: term, '&', '|', '!', '(', ')'
    plain: list[str] = []
    for phrase, lp, rp, word in TOKEN.findall(q or ""):
        if lp:
            out.append("(")
        elif rp:
            out.append(")")
        elif phrase:
            parts = [clean(p) for p in phrase.split()]
            parts = [p for p in parts if p]
            if parts:
                out.append("(" + " <-> ".join(parts) + ")" if len(parts) > 1 else parts[0])
                plain.append(" ".join(parts))
        elif word.lower() in OPS:
            out.append(OPS[word.lower()])
        else:
            neg = word.startswith("-") and len(word) > 1
            t = term(word[1:] if neg else word)
            if t:
                if neg:
                    out.append("!")
                else:
                    plain.append(clean(word.rstrip("*")))
                out.append(t)
    # repair: implicit AND, drop dangling operators, balance brackets
    res: list[str] = []
    depth = 0
    for tok in out:
        is_operand_start = tok not in ("&", "|", ")")
        if res and is_operand_start and res[-1] not in ("&", "|", "!", "("):
            res.append("&")
        if tok in ("&", "|"):
            if not res or res[-1] in ("&", "|", "!", "("):
                continue
        if tok == ")":
            if depth == 0 or res[-1] in ("&", "|", "!", "("):
                if depth and res[-1] == "(":
                    res.pop()
                    depth -= 1
                continue
            depth -= 1
        if tok == "(":
            depth += 1
        res.append(tok)
    while res and res[-1] in ("&", "|", "!", "("):
        if res.pop() == "(":
            depth -= 1
    res += [")"] * depth
    text = " ".join(res).replace("( ", "(").replace(" )", ")")
    if any(t in ("&", "|", "!", "(") for t in out):  # name ILIKE fallback only for plain word searches
        plain = []
    return (text or None), plain


if __name__ == "__main__":
    for s in ['endoskop UND (tuttlingen ODER berlin) NICHT dental', '"b braun" melsungen', 'implant*', 'a OR',
              '((x', '-dental orthopädie', 'AND', 'gmbh & co.', 'e-health app*']:
        print(f"{s!r:55} -> {parse(s)}")
