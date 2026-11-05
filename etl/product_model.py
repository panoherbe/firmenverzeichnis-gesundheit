"""
Product group (EMDN category letter) derived from the company's own website text by a text classifier, for companies
whose EUDAMED record lists no devices. Trained on the companies that do have EUDAMED codes; a prediction is kept only
above the probability threshold at which the cross-validated precision on those companies reached TARGET. The keyword
rules in etl/product_rules.py were measured first and rejected (47–62 % precision, 2026-11-05).

    .venv/bin/python -m etl.product_model dump          # app venv: texts + labels -> var/product/texts.jsonl.gz
    var/mlvenv/bin/python -m etl.product_model train    # scikit-learn venv: CV report + var/product/pred.json

pred.json: {cid: {"g": letter, "p": probability, "words": [terms that pushed the decision]}} plus "_meta" with the
measured precision, threshold and date. etl.enrich applies it (company.emdn_source = 'website').
"""

from __future__ import annotations

import gzip
import json
import sys
from collections import Counter
from pathlib import Path

OUT = Path("var/product")
TARGET = 0.80


def dump() -> None:
    from etl.common.db import connect
    from etl.product_rules import site_texts
    conn = connect()
    rows = conn.execute("SELECT id, emdn, verified, products IS NOT NULL FROM company").fetchall()
    texts = site_texts()
    OUT.mkdir(parents=True, exist_ok=True)
    n = 0
    with gzip.open(OUT / "texts.jsonl.gz", "wt") as f:
        for cid, emdn, verified, has_ema in rows:
            if cid not in texts:
                continue
            label = None
            if emdn:
                c = Counter(code[0] for code, _t in emdn if code)
                label = sorted(c)  # all groups seen in the sampled device details
            f.write(json.dumps({"cid": cid, "text": texts[cid][:40000].lower(), "groups": label,
                                "verified": verified, "ema": has_ema}) + "\n")
            n += 1
    print(f"dump: {n} companies with website text")


def train() -> None:
    import datetime as dt
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.model_selection import StratifiedKFold

    recs = [json.loads(l) for l in gzip.open(OUT / "texts.jsonl.gz", "rt")]
    lab = [r for r in recs if r["groups"]]
    # one training label per company: its most frequent group is not stored, so take the first of the sorted set only
    # when the set has one group; multi-group companies count as right if the prediction is any of their groups
    # training label: only companies whose sampled devices all fall in one group (multi-group labels are noise)
    y_first = [r["groups"][0] if len(r["groups"]) == 1 else None for r in lab]
    classes = sorted({g for g, n in Counter(y_first).items() if g and n >= 8})
    keep = [i for i, g in enumerate(y_first) if g in classes]
    lab = [lab[i] for i in keep]
    y = np.array([y_first[i] for i in keep])
    vec = TfidfVectorizer(min_df=3, max_df=0.5, ngram_range=(1, 2), max_features=60000, sublinear_tf=True,
                          token_pattern=r"(?u)\b[a-zäöüß][a-zäöüß\-]{2,}\b")
    X = vec.fit_transform([r["text"] for r in lab])
    oof = np.zeros((len(lab), len(classes)))
    for tr, te in StratifiedKFold(5, shuffle=True, random_state=1).split(X, y):
        m = LogisticRegression(max_iter=2000, C=float(sys.argv[2]) if len(sys.argv) > 2 else 10.0).fit(X[tr], y[tr])
        oof[te] = m.predict_proba(X[te])
    pred = np.array(classes)[oof.argmax(1)]
    prob = oof.max(1)
    right = np.array([p in r["groups"] for p, r in zip(pred, lab)])
    print(f"training: {len(lab)} companies, classes {classes}")
    thr = None
    for t in [0.0, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]:
        sel = prob >= t
        prec = right[sel].mean() if sel.any() else 0
        print(f"  threshold {t:.1f}: kept {sel.sum():4d}/{len(lab)} = {100 * sel.mean():3.0f} %, precision {100 * prec:.0f} %")
        if thr is None and sel.sum() >= 30 and prec >= TARGET:
            thr = t
    if thr is None:
        print(f"no threshold reaches {TARGET:.0%} precision: nothing written")
        return
    m = LogisticRegression(max_iter=2000, C=float(sys.argv[2]) if len(sys.argv) > 2 else 10.0).fit(X, y)
    todo = [r for r in recs if not r["groups"] and not r["ema"]]
    Xt = vec.transform([r["text"] for r in todo])
    P = m.predict_proba(Xt)
    terms = np.array(vec.get_feature_names_out())
    out, n_ver = {}, 0
    for i, r in enumerate(todo):
        k = P[i].argmax()
        if P[i, k] < thr:
            continue
        row = Xt[i].toarray()[0] * m.coef_[k]
        words = [str(terms[j]) for j in np.argsort(-row)[:5] if row[j] > 0]
        out[r["cid"]] = {"g": m.classes_[k], "p": round(float(P[i, k]), 2), "words": words}
        n_ver += bool(r["verified"])
    sel = prob >= thr
    out["_meta"] = {"date": dt.date.today().isoformat(), "threshold": thr, "cv_precision": round(float(right[sel].mean()), 3),
                    "cv_kept": int(sel.sum()), "cv_n": len(lab), "method": "TF-IDF + logistic regression, 5-fold CV"}
    (OUT / "pred.json").write_text(json.dumps(out))
    print(f"predicted {len(out) - 1} companies ({n_ver} register-verified) at threshold {thr}")


if __name__ == "__main__":
    {"dump": dump, "train": train}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: print(__doc__))()
