"""Normalisation: legal forms, name join key, VAT, register numbers, web domains, role mailboxes."""

from __future__ import annotations

import re
import unicodedata

UMLAUT = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue"})

# (pattern on the folded name, canonical label). Order matters: longest first.
LEGAL_FORMS = [
    (r"\bse\s*(&|und|\+)\s*co\.?\s*kgaa", "SE & Co. KGaA"),
    (r"\bag\s*(&|und|\+)\s*co\.?\s*kgaa", "AG & Co. KGaA"),
    (r"gmbh\s*(&|und|\+)\s*co\.?\s*kgaa", "GmbH & Co. KGaA"),
    (r"\bse\s*(&|und|\+)\s*co\.?\s*kg", "SE & Co. KG"),
    (r"\bag\s*(&|und|\+)\s*co\.?\s*kg", "AG & Co. KG"),
    (r"(gmbh|ug|ltd\.?|b\.?v\.?)\s*(&|und|\+)\s*co\.?\s*(kg|ohg)", "GmbH & Co. KG"),
    (r"kgaa", "KGaA"),
    (r"gesellschaft mit beschraenkter haftung|g\.?m\.?b\.?h\.?|mbh", "GmbH"),
    (r"ug\s*\(?haftungsbeschraenkt\)?", "UG"),
    (r"aktiengesellschaft|\bag\b", "AG"),
    (r"\bse\b|societas europaea", "SE"),
    (r"kommanditgesellschaft|\bkg\b", "KG"),
    (r"\bohg\b", "OHG"),
    (r"\bug\b", "UG"),
    (r"\be\.?\s?g\.?$|eingetragene genossenschaft", "eG"),
    (r"\be\.?\s?v\.?$|eingetragener verein", "e.V."),
    (r"stiftung", "Stiftung"),
    (r"\b(ltd|limited|inc|llc|corp|corporation|b\.\s?v|n\.\s?v|s\.\s?a|s\.\s?a\.\s?s|s\.\s?r\.\s?l|sp\.?\s?z\s?o\.?\s?o|a/s|ab|oy|plc)\b\.?",
     "ausländische Rechtsform"),
    (r"\bkdoer\b|\baoer\b|koerperschaft des oeffentlichen rechts|anstalt des oeffentlichen rechts", "öffentlich-rechtlich"),
]

# Signals that the "company" is a natural person or a person-named partnership (GDPR: personal data).
NATURAL_PERSON = re.compile(
    r"\be\.?\s?k\.?(fm|fr)?\b|\beingetragene[rn]? kauf(mann|frau)\b|\binh\.|\binhaber(in)?\b|apotheke|"
    r"\bdr\.\s?med\b|\bpraxis\b"
)

STRIP_FORMS = re.compile(
    r"\b(gmbh\s*(&|und|\+)\s*co\.?\s*(kgaa|kg|ohg)|gesellschaft mit beschraenkter haftung|g\.?m\.?b\.?h\.?|mbh|"
    r"aktiengesellschaft|ag|se|kgaa|kg|ohg|ug\s*\(?haftungsbeschraenkt\)?|ug|e\.?\s?k\.?|e\.?\s?v\.?|e\.?\s?g\.?|"
    r"ltd|limited|inc|b\.?v\.?|s\.?a\.?|s\.?r\.?l\.?|co\.?|& co|deutschland|germany)\b\.?"
)


def fold(value: str) -> str:
    value = value.translate(UMLAUT)
    value = unicodedata.normalize("NFKD", value)
    value = "".join(c for c in value if not unicodedata.combining(c))
    return value.casefold()


def legal_form(name: str) -> str | None:
    f = fold(name)
    for pattern, label in LEGAL_FORMS:
        if re.search(pattern, f):
            return label
    return None


def is_natural_person(name: str) -> bool:
    return bool(NATURAL_PERSON.search(fold(name)))


GROUP = re.compile(r"bietergemeinschaft|^bg\b|\bbg\s|\barge\b|\bgbr\b", re.I)
PART_SPLIT = re.compile(r"\s*(?:;|/|,(?!\s*(?:co\b|kg\b|ag\b))|\bund\b(?!\s*co)|\bvertreten durch( die| den)?\b|\(|\)|:)\s*", re.I)


def split_group(name: str) -> list[str] | None:
    """
    Award winners are often bidding consortia ("BG TEVA GmbH, ratiopharm GmbH, AbZ-Pharma GmbH") or lists of
    several winners. Returns the member companies that carry a legal form, or None if `name` is a single entity.
    A GbR consortium itself is never a company in the directory.
    """
    if not GROUP.search(name) and ";" not in name:
        return None
    parts = []
    for p in PART_SPLIT.split(GROUP.sub(" ", name)):
        p = (p or "").strip(" .-,")
        if p and legal_form(p) and len(p) > 4 and not GROUP.search(p):
            parts.append(p)
    return list(dict.fromkeys(parts))


def name_norm(name: str) -> str:
    """Join key: folded, legal forms and 'Deutschland/Germany' removed, punctuation collapsed."""
    v = fold(name)
    v = re.sub(r"[.,;:()\[\]/\\\"'`´’\-]+", " ", v)
    v = re.sub(r"\s+", " ", v)
    prev = None
    while prev != v:
        prev = v
        v = STRIP_FORMS.sub(" ", v)
        v = re.sub(r"\s+", " ", v).strip(" &+")
    return v or fold(name).strip()


def vat(value: str | None) -> str | None:
    if not value:
        return None
    v = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if re.fullmatch(r"\d{9}", v):
        v = "DE" + v
    return v if re.fullmatch(r"DE\d{9}", v) else None


def register_no(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"\b(HR\s?[AB]|GnR|PR|VR)\s*[:.]?\s*(\d{1,6})\s*([A-Z]{1,3})?\b", value.upper().replace("HRB-", "HRB "))
    if not m:
        return None
    return re.sub(r"\s", "", m.group(1)) + m.group(2) + (m.group(3) or "")


GENERIC_DOMAINS = {
    "gmail.com", "googlemail.com", "gmx.de", "gmx.net", "web.de", "t-online.de", "outlook.com", "hotmail.com",
    "yahoo.com", "yahoo.de", "icloud.com", "aol.com", "freenet.de", "arcor.de", "posteo.de", "mail.de",
    "online.de", "live.de", "me.com",
}


# e-mail domains of authorities and public bodies (EUDAMED validators, Länder offices): never a company's domain
AUTHORITY_DOMAIN = re.compile(r"(\.bund\.de|\.bwl\.de|\.nrw\.de|\.bayern\.de|\.berlin\.de|\.hamburg\.de|"
                              r"\.niedersachsen\.de|\.hessen\.de|\.sachsen\.de|\.thueringen\.de|\.brandenburg\.de|"
                              r"\.landsh\.de|\.rlp\.de|\.saarland\.de|\.mv-regierung\.de|\.sachsen-anhalt\.de|"
                              r"\.bremen\.de|\.europa\.eu|\.gov(\.[a-z]{2})?)$")


def company_domain_from_email(email: str | None) -> str | None:
    """Domain of a company's register e-mail (the address itself is not kept), unless free mail or an authority."""
    d = domain(email) if email and "@" in email else None
    return None if not d or AUTHORITY_DOMAIN.search("." + d) else d


def domain(url: str | None) -> str | None:
    if not url:
        return None
    v = url.strip().lower()
    v = re.sub(r"^[a-z]+://", "", v)
    v = v.split("/")[0].split("?")[0].split("#")[0].split(":")[0]
    v = re.sub(r"^www\d?\.", "", v)
    if "@" in v:
        v = v.split("@", 1)[1]
    if not re.fullmatch(r"[a-z0-9.\-]+\.[a-z]{2,}", v) or v in GENERIC_DOMAINS:
        return None
    parts = v.split(".")
    # registrable domain: last two labels, three for co.uk-style
    if len(parts) > 2 and parts[-2] in {"co", "com", "org", "ac", "gv"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def website(url: str | None) -> str | None:
    d = domain(url)
    if not d:
        return None
    v = url.strip()
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    return v


ROLE_LOCALPARTS = re.compile(
    r"^(info|kontakt|contact|office|mail|post|service|support|sales|vertrieb|verkauf|zentrale|empfang|"
    r"regulatory|ra|qm|qa|quality|qualitaet|medical|medizinprodukte|mdr|ivdr|vigilance|vigilanz|pv|"
    r"pharmacovigilance|drugsafety|arzneimittelsicherheit|order|orders|bestellung|hello|hallo|team|"
    r"eu[-_.]?(importer|rep|ar)|ear|ec[-_.]?rep|importer|import|export|customer|kundenservice|"
    r"compliance|legal|admin|buero|praxis|labor|lab|marketing|presse|press|hr|jobs|karriere|"
    r"information|administration|bestellungen|contactus|labortechnik|medicaldevices|postmarket|qualitaetsmanagement|"
    r"qualitaetssicherung|medizinproduktesicherheit|eu[-_.]?representative)"
    # appendix only after a separator (info-de, regulatory.affairs) or from a fixed list of compound roles (qmb, qmra,
    # regulatoryaffairs, customerservice, infoeu); "ra"+"mazan" was a person (endcheck 2026-11-05)
    r"([-_.][a-z]{1,20}|affairs|service|services|care|desk|team|b|mb|ra|rb|re|eu|de|ger|emea|europe|germany|"
    r"deutschland|office|support|center|centre|line|safety|management|abteilung|box|stelle|gmbh|regulatory|s)?\d*$"
)


def role_email(value: str | None) -> str | None:
    """Return the address only if its local part is a role (info@, kontakt@ ...). Named addresses -> None."""
    if not value or "@" not in value:
        return None
    v = value.strip().lower()
    local, _, dom = v.partition("@")
    if dom in GENERIC_DOMAINS or AUTHORITY_DOMAIN.search("." + dom):  # poststelle@bra.nrw.de is an authority
        return None
    return v if ROLE_LOCALPARTS.match(local) else None


def post_code(value: str | None) -> str | None:
    if not value:
        return None
    m = re.search(r"\b(\d{5})\b", value)
    return m.group(1) if m else None


# Public bodies and care providers win health-CPV lots too (hospitals buying for each other, municipal firms).
# They are buyers, not industry (sample v1: a municipal hospital gGmbH in tier C).
NON_INDUSTRY = re.compile(
    r"klinik|krankenhaus|hospital|spital|universit|hochschule|\bstadt\b|stadtwerke|landkreis|\bkreis\b|gemeinde|"
    r"\bggmbh\b|\bkdoer\b|\baoer\b|\bdrk\b|rotes kreuz|caritas|diakon|johanniter|malteser|arbeiter-samariter|"
    r"pflegeheim|seniorenheim|\bpraxis\b|\bmvz\b|zahnarzt|apotheke|bundeswehr|landesamt|ministerium"
)


def is_non_industry(name: str) -> bool:
    return bool(NON_INDUSTRY.search(fold(name)))
