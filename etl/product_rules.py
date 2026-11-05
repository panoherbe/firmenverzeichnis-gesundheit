"""
Product group (EMDN category letter) derived from the company's own website text, for companies whose EUDAMED record
lists no devices (measured 2026-11-05: 1,797 of 3,612 German SRNs have 0 registered devices; importers and authorised
representatives have none of their own). Shown as "abgeleitet aus der eigenen Website" with the matched words, never
mixed up with EUDAMED codes (company.emdn_source says which one it is).

Rules: per EMDN category a list of specific product words (German and English stems). A category counts when at least
MIN_WORDS different words of it occur on the company's stored pages (home, about, products) and it is among the
TOP strongest; generic words (software, labor, medical) are not in the lists. The thresholds were chosen on the
companies that have EUDAMED codes (ground truth, `eval`), before the rules were applied to anyone else.

    python -m etl.product_rules eval      # precision/recall against EUDAMED groups, per threshold
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from collections import Counter

from etl.common.raw import latest_dir

WORDS = {
    "A": ["spritze", "kanüle", "kanuele", "infusion", "injektion", "syringe", "needle", "cannula", "venenkatheter",
          "venenverweil", "punktion", "blutentnahme", "insulinpen", "pen-nadel", "transferset", "überleitungs"],
    "B": ["transfusion", "blutbeutel", "blood bag", "apherese", "apheresis", "hämatologie", "haematology", "hematology",
          "zentrifuge für blut", "blutkomponent"],
    "C": ["kardio", "cardio", "herzklappe", "heart valve", "stent", "herzkatheter", "angiograph", "gefäß", "vascular",
          "ekg", "ecg", "defibrillat", "kreislauf", "katheterablation", "ballonkatheter", "embolie"],
    "D": ["desinfektion", "disinfect", "antisept", "händedesinfektion", "flächendesinfektion", "instrumentendesinfektion"],
    "F": ["dialyse", "dialysis", "hämodialyse", "haemodialysis", "dialysator"],
    "G": ["gastro", "endoskop", "endoscop", "koloskop", "colonoscop", "magensonde", "ernährungssonde", "enteral",
          "stoma", "ostomy"],
    "H": ["nahtmaterial", "suture", "wundverschluss", "wound closure", "klammernaht", "stapler", "hautkleber"],
    "J": ["herzschrittmacher", "pacemaker", "neurostimulat", "cochlea", "implantierbar", "implantable pulse",
          "aktive implantat", "active implant"],
    "K": ["laparoskop", "laparoscop", "minimalinvasiv", "minimally invasive", "trokar", "trocar", "arthroskop",
          "arthroscop", "endotherap"],
    "L": ["chirurgische instrumente", "surgical instruments", "skalpell", "scalpel", "pinzette", "forceps", "schere",
          "klemme", "retraktor", "retractor", "instrumentarium", "op-instrument"],
    "M": ["wundauflage", "wound dressing", "verbandmittel", "verband", "bandage", "kompresse", "pflaster", "dressing",
          "wundversorgung", "wound care"],
    "N": ["neurolog", "neurochirurg", "neurosurg", "eeg", "elektroenzephal", "nervenstimul", "spinal", "liquor"],
    "P": ["implantat", "implant", "prothese", "prosthes", "endoprothe", "osteosynthese", "osteosynthesis", "hüft",
          "knie", "wirbelsäul", "spine", "orthopädie-implant", "brustimplant", "knochenschraube", "bone screw"],
    "Q": ["dental", "zahn", "dentist", "kieferorthop", "orthodont", "ophthalm", "augen", "intraokular", "intraocular",
          "kontaktlinse", "contact lens", "hno", "otolaryn", "hörgerät", "hearing aid", "audiolog"],
    "R": ["beatmung", "ventilat", "anästhesie", "anaesthesi", "anesthesi", "atemweg", "respirat", "sauerstoff",
          "oxygen", "inhalat", "intubation", "cpap", "schlafapnoe", "sleep apnea"],
    "S": ["sterilisation", "sterilization", "steriliz", "sterilgut", "autoklav", "autoclave", "sterilisierverpack"],
    "T": ["schutzkleidung", "protective clothing", "op-mäntel", "surgical gown", "op-abdeck", "drape", "mundschutz",
          "atemschutz", "face mask", "gesichtsmask", "inkontinenz", "incontinence", "untersuchungshandschuh",
          "examination glove", "op-handschuh"],
    "U": ["urolog", "urologie", "katheter für die blase", "harnkatheter", "urinary catheter", "urinbeutel", "gynäkolog",
          "gynecolog", "geburtshilf", "obstetric"],
    "W": ["in-vitro", "in vitro", "ivd", "diagnostika", "reagenz", "reagent", "assay", "schnelltest", "rapid test",
          "immunoassay", "elisa", "pcr", "laboranalys", "clinical chemistry", "klinische chemie", "teststreifen"],
    "Y": ["rollstuhl", "wheelchair", "rollator", "gehhilfe", "walking aid", "orthese", "orthosis", "orthotic",
          "bandage für", "kompressionsstr", "compression stocking", "pflegebett", "patientenlifter", "patient lift",
          "rehatechnik", "rehabilitation aid", "hilfsmittel", "einlagen", "orthopädieschuh"],
    "Z": ["röntgen", "x-ray", "ultraschall", "ultrasound", "sonograph", "magnetresonanz", "mrt", "computertomograph",
          "ct-", "patientenmonitor", "patient monitor", "elektromedizin", "electromedical", "laser", "op-tisch",
          "operating table", "op-leuchte", "surgical light", "infusionspumpe", "infusion pump", "medizinische software",
          "medical software", "medizingerät", "medical equipment", "therapiegerät", "diagnosegerät", "endoskopiesystem"],
}
_RE = {g: [re.compile(r"(?<![a-zäöü])" + re.escape(w), re.I) for w in ws] for g, ws in WORDS.items()}
MIN_WORDS = 3
TOP = 2


def scores(text: str) -> dict[str, list[str]]:
    hits = {}
    for g, pats in _RE.items():
        found = [WORDS[g][i] for i, p in enumerate(pats) if p.search(text)]
        if found:
            hits[g] = found
    return hits


def derive(text: str, min_words: int = MIN_WORDS, top: int = TOP) -> list[tuple[str, list[str]]]:
    hits = scores(text)
    ranked = sorted(((g, w) for g, w in hits.items() if len(w) >= min_words), key=lambda x: -len(x[1]))
    return ranked[:top]


def site_texts() -> dict[str, str]:
    """Visible text of all stored pages per company, from every websites run (newest first wins per page)."""
    from etl.enrich import visible
    out: dict[str, list[str]] = {}
    root = latest_dir("websites").parent
    for run in sorted(root.iterdir(), reverse=True):
        f = run / "sites.jsonl"
        if not f.exists():
            continue
        for line in f.open():
            s = json.loads(line)
            if s["cid"] in out:
                continue
            parts = []
            for p in s["pages"]:
                if p.get("file") and isinstance(p.get("status"), int) and p["status"] < 400:
                    try:
                        parts.append(visible(gzip.decompress((run / p["file"]).read_bytes()))[:60000])
                    except OSError:
                        pass
            if parts:
                out[s["cid"]] = parts
    return {cid: " ".join(p) for cid, p in out.items()}


def evaluate() -> None:
    from etl.common.db import connect
    conn = connect()
    truth = {cid: set(g) for cid, g in conn.execute(
        "SELECT id, emdn_groups FROM company WHERE emdn IS NOT NULL AND emdn_groups IS NOT NULL").fetchall()}
    texts = site_texts()
    common = [c for c in truth if c in texts]
    print(f"ground truth: {len(truth)} companies with EUDAMED groups, {len(common)} with stored website text")
    for mw in (2, 3, 4, 5):
        for top in (1, 2):
            n = right = any_right = groups = 0
            for cid in common:
                d = derive(texts[cid], mw, top)
                if not d:
                    continue
                n += 1
                gs = {g for g, _ in d}
                groups += len(gs)
                right += len(gs & truth[cid])
                any_right += bool(gs & truth[cid])
            print(f"  min_words={mw} top={top}: assigned {n}/{len(common)}, group precision "
                  f"{right}/{groups} = {100 * right / max(groups, 1):.0f} %, company has >=1 right group "
                  f"{any_right}/{n} = {100 * any_right / max(n, 1):.0f} %")


if __name__ == "__main__":
    if sys.argv[1:2] == ["eval"]:
        evaluate()
    else:
        print(__doc__)
