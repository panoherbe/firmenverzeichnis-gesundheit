"""
Virtual / hybrid B2B matchmaking: event pages, opt-in registration, matching, meeting
requests, automatic slot scheduling, video rooms, personal agenda (.ics), organiser dashboard, follow-up survey.

Roles: organiser = admin account (web.auth.require_admin). Participants have no account: each gets a secret link
/matchmaking/p/<token> after registering (no password). German participants pick their company from the directory
(`company`); international ones pick from `intl_company` (etl.intl) or type their organisation.

Personal data: participants are persons who registered with explicit consent. They live ONLY in mm_* tables, never
in `company`. Retention: deleted 12 months after the event (`delete_after`), by `python -m web.matchmaking purge`
(daily timer) and opportunistically when the organiser dashboard opens; survey answers stay, unlinked and without
free text. No automatic e-mail: the organiser posts invitation texts in own channels (UWG 7: no cold e-mail).

Video: each meeting gets a room <MM_VIDEO_BASE><random name> (default https://meet.jit.si/); hybrid events give
on-site pairs a table number. No third-party script is embedded in our pages.

    .venv/bin/python -m web.matchmaking schema     # mm_* tables + intl_company
    .venv/bin/python -m web.matchmaking purge      # delete participants past their retention date
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import os
import re
import secrets
import sys
from typing import Annotated
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from etl.common import norm
from etl.intl import SCHEMA as INTL_SCHEMA
from web.auth import current_user, require_admin
from web.common import _, db, lang, register, templates

VIDEO_BASE = os.environ.get("MM_VIDEO_BASE", "https://meet.jit.si/")
ORGANISER = os.environ.get("MM_ORGANISER", "Veranstalter (Beispiel)")  # the customer sets its own name
RETENTION_MONTHS = 12
RESERVED = {"admin", "p", "neu", "api"}

SCHEMA = INTL_SCHEMA + """
CREATE TABLE IF NOT EXISTS mm_event (
  id serial PRIMARY KEY, slug text UNIQUE NOT NULL, title text NOT NULL, title_en text, intro text, intro_en text,
  organiser text NOT NULL, tz text NOT NULL DEFAULT 'Europe/Berlin', starts_at timestamptz NOT NULL,
  ends_at timestamptz NOT NULL, b2b_start timestamptz NOT NULL, b2b_end timestamptz NOT NULL,
  slot_minutes int NOT NULL DEFAULT 20, gap_minutes int NOT NULL DEFAULT 5, max_meetings int NOT NULL DEFAULT 8,
  format text NOT NULL DEFAULT 'virtual' CHECK (format IN ('virtual', 'hybrid')), venue text,
  markets text[] NOT NULL DEFAULT '{}', sectors text[] NOT NULL DEFAULT '{}', agenda text, agenda_en text,
  status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'open', 'closed', 'done')),
  created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE IF NOT EXISTS mm_participant (
  id serial PRIMARY KEY, event_id int NOT NULL REFERENCES mm_event(id) ON DELETE CASCADE,
  token text UNIQUE NOT NULL, side text NOT NULL CHECK (side IN ('de', 'intl')),
  company_id text, intl_company_id text, org_name text NOT NULL, org_type text NOT NULL DEFAULT 'company',
  country text NOT NULL, city text, website text,
  person_name text NOT NULL, job_title text, email text NOT NULL, ui_lang text NOT NULL DEFAULT 'de',
  tz text NOT NULL DEFAULT 'Europe/Berlin', mode text NOT NULL DEFAULT 'online' CHECK (mode IN ('online', 'onsite')),
  sectors text[] NOT NULL DEFAULT '{}', products text, markets text[] NOT NULL DEFAULT '{}', offer text, seek text,
  avail timestamptz[],                       -- NULL = available in every slot
  status text NOT NULL DEFAULT 'registered' CHECK (status IN ('registered', 'confirmed', 'cancelled')),
  attended boolean, consent_at timestamptz NOT NULL, consent_text text NOT NULL, delete_after date NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now());
CREATE INDEX IF NOT EXISTS mm_participant_event ON mm_participant (event_id, side);
CREATE TABLE IF NOT EXISTS mm_meeting (
  id serial PRIMARY KEY, event_id int NOT NULL REFERENCES mm_event(id) ON DELETE CASCADE,
  requester_id int REFERENCES mm_participant(id) ON DELETE SET NULL,
  partner_id int REFERENCES mm_participant(id) ON DELETE SET NULL,
  status text NOT NULL DEFAULT 'requested'
    CHECK (status IN ('requested', 'accepted', 'declined', 'cancelled', 'held', 'no_show')),
  message text, starts_at timestamptz, ends_at timestamptz, room_url text NOT NULL, table_no int,
  created_by text NOT NULL DEFAULT 'participant', created_at timestamptz NOT NULL DEFAULT now(), decided_at timestamptz);
CREATE UNIQUE INDEX IF NOT EXISTS mm_meeting_pair ON mm_meeting
  (event_id, LEAST(requester_id, partner_id), GREATEST(requester_id, partner_id))
  WHERE status NOT IN ('declined', 'cancelled');
-- one row per participant and slot: the primary key makes double booking impossible
CREATE TABLE IF NOT EXISTS mm_booking (
  participant_id int NOT NULL REFERENCES mm_participant(id) ON DELETE CASCADE, starts_at timestamptz NOT NULL,
  meeting_id int NOT NULL REFERENCES mm_meeting(id) ON DELETE CASCADE, PRIMARY KEY (participant_id, starts_at));
CREATE TABLE IF NOT EXISTS mm_survey (
  id serial PRIMARY KEY, event_id int NOT NULL REFERENCES mm_event(id) ON DELETE CASCADE,
  participant_id int UNIQUE REFERENCES mm_participant(id) ON DELETE SET NULL, side text NOT NULL,
  satisfaction int CHECK (satisfaction BETWEEN 1 AND 5), benefit int CHECK (benefit BETWEEN 1 AND 5),
  contacts int CHECK (contacts >= 0), revenue_band text, follow_up boolean, comment text,
  submitted_at timestamptz NOT NULL DEFAULT now());
"""

SECTORS = {"medtech": "Medizintechnik", "pharma": "Pharma", "biotech": "Biotechnologie",
           "digital_health": "Digitale Gesundheit"}
ORG_TYPES = {"company": "Firma", "institution": "Institution / Cluster", "investor": "Investor",
             "multiplier": "Verband / Multiplikator"}
REVENUE = {"none": "Keine", "lt10k": "unter 10.000 €", "10k_50k": "10.000–50.000 €", "50k_250k": "50.000–250.000 €",
           "gt250k": "über 250.000 €", "open": "Noch nicht absehbar"}
STATUS = {"requested": "Angefragt", "accepted": "Bestätigt", "declined": "Abgelehnt", "cancelled": "Abgesagt",
          "held": "Stattgefunden", "no_show": "Nicht erschienen"}
EV_STATUS = {"draft": "Entwurf", "open": "Anmeldung offen", "closed": "Anmeldung geschlossen", "done": "Abgeschlossen"}
TZS = ["Europe/Berlin", "Europe/London", "Europe/Lisbon", "Europe/Paris", "Europe/Warsaw", "Europe/Athens",
       "Europe/Istanbul", "Asia/Dubai", "Asia/Riyadh", "Asia/Kolkata", "Asia/Singapore", "Asia/Shanghai",
       "Asia/Tokyo", "Asia/Seoul", "Australia/Sydney", "Africa/Lagos", "Africa/Cairo", "Africa/Nairobi",
       "Africa/Johannesburg", "America/New_York", "America/Chicago", "America/Los_Angeles", "America/Toronto",
       "America/Mexico_City", "America/Sao_Paulo", "UTC"]
COUNTRIES = {  # ISO2: (de, en)
    "AT": ("Österreich", "Austria"), "BE": ("Belgien", "Belgium"), "BG": ("Bulgarien", "Bulgaria"),
    "HR": ("Kroatien", "Croatia"), "CY": ("Zypern", "Cyprus"), "CZ": ("Tschechien", "Czechia"),
    "DK": ("Dänemark", "Denmark"), "EE": ("Estland", "Estonia"), "FI": ("Finnland", "Finland"),
    "FR": ("Frankreich", "France"), "DE": ("Deutschland", "Germany"), "GR": ("Griechenland", "Greece"),
    "HU": ("Ungarn", "Hungary"), "IE": ("Irland", "Ireland"), "IT": ("Italien", "Italy"), "LV": ("Lettland", "Latvia"),
    "LT": ("Litauen", "Lithuania"), "LU": ("Luxemburg", "Luxembourg"), "MT": ("Malta", "Malta"),
    "NL": ("Niederlande", "Netherlands"), "PL": ("Polen", "Poland"), "PT": ("Portugal", "Portugal"),
    "RO": ("Rumänien", "Romania"), "SK": ("Slowakei", "Slovakia"), "SI": ("Slowenien", "Slovenia"),
    "ES": ("Spanien", "Spain"), "SE": ("Schweden", "Sweden"), "NO": ("Norwegen", "Norway"),
    "IS": ("Island", "Iceland"), "CH": ("Schweiz", "Switzerland"), "GB": ("Vereinigtes Königreich", "United Kingdom"),
    "TR": ("Türkei", "Türkiye"), "UA": ("Ukraine", "Ukraine"), "RS": ("Serbien", "Serbia"), "IL": ("Israel", "Israel"),
    "AE": ("Vereinigte Arabische Emirate", "United Arab Emirates"), "SA": ("Saudi-Arabien", "Saudi Arabia"),
    "QA": ("Katar", "Qatar"), "EG": ("Ägypten", "Egypt"), "MA": ("Marokko", "Morocco"), "NG": ("Nigeria", "Nigeria"),
    "KE": ("Kenia", "Kenya"), "GH": ("Ghana", "Ghana"), "ZA": ("Südafrika", "South Africa"), "IN": ("Indien", "India"),
    "CN": ("China", "China"), "JP": ("Japan", "Japan"), "KR": ("Südkorea", "South Korea"), "TW": ("Taiwan", "Taiwan"),
    "SG": ("Singapur", "Singapore"), "VN": ("Vietnam", "Vietnam"), "TH": ("Thailand", "Thailand"),
    "ID": ("Indonesien", "Indonesia"), "MY": ("Malaysia", "Malaysia"), "PH": ("Philippinen", "Philippines"),
    "AU": ("Australien", "Australia"), "NZ": ("Neuseeland", "New Zealand"), "US": ("USA", "United States"),
    "CA": ("Kanada", "Canada"), "MX": ("Mexiko", "Mexico"), "BR": ("Brasilien", "Brazil"),
    "AR": ("Argentinien", "Argentina"), "CL": ("Chile", "Chile"), "CO": ("Kolumbien", "Colombia"),
    "KZ": ("Kasachstan", "Kazakhstan"), "UZ": ("Usbekistan", "Uzbekistan"),
}

register({
    # navigation, roles, statuses
    "Matchmaking": "Matchmaking", "Veranstaltungen": "Events", "Organisation": "Organiser area",
    "Medizintechnik": "Medical technology", "Biotechnologie": "Biotechnology",
    "Digitale Gesundheit": "Digital health", "Firma": "Company", "Institution / Cluster": "Institution / cluster",
    "Investor": "Investor", "Verband / Multiplikator": "Association / multiplier", "Keine": "None",
    "unter 10.000 €": "below EUR 10,000", "10.000–50.000 €": "EUR 10,000–50,000",
    "50.000–250.000 €": "EUR 50,000–250,000", "über 250.000 €": "above EUR 250,000",
    "Noch nicht absehbar": "Not yet foreseeable", "Angefragt": "Requested", "Bestätigt": "Confirmed",
    "Abgelehnt": "Declined", "Abgesagt": "Cancelled", "Stattgefunden": "Held", "Nicht erschienen": "No-show",
    "Entwurf": "Draft", "Anmeldung offen": "Registration open", "Anmeldung geschlossen": "Registration closed",
    "Abgeschlossen": "Completed", "virtuell": "virtual", "hybrid": "hybrid", "online": "online", "vor Ort": "on site",
    "Deutsche Unternehmen": "German companies", "Internationale Teilnehmende": "International participants",
    "Deutsches Unternehmen": "German company", "Internationaler Gast": "International participant",
    # event list / page
    "B2B-Matchmaking der Gesundheitswirtschaft": "B2B matchmaking for the health industry",
    "Deutsche Unternehmen treffen internationale Partner in kurzen, vorab vereinbarten Online-Gesprächen.":
        "German companies meet international partners in short, pre-arranged online meetings.",
    "So funktioniert es": "How it works",
    "Anmelden und Profil ausfüllen.": "Register and fill in your profile.",
    "Vorschläge ansehen und Gespräche anfragen.": "See suggestions and request meetings.",
    "Anfragen annehmen – der Termin wird automatisch eingeplant.": "Accept requests – the slot is scheduled automatically.",
    "Zum Termin den Videolink öffnen (oder an den Tisch vor Ort kommen).":
        "At the time, open the video link (or come to your table on site).",
    "Nach der Veranstaltung: kurze Umfrage.": "After the event: a short survey.",
    "Derzeit ist keine Veranstaltung geöffnet.": "No event is open at the moment.",
    "Details und Anmeldung": "Details and registration", "Veranstalter": "Organiser", "Datum": "Date",
    "Zeit": "Time", "Format": "Format", "Zielmärkte": "Target markets", "Branchen": "Sectors",
    "Programm": "Agenda", "B2B-Gespräche": "B2B meetings", "Gesprächsdauer": "Meeting length", "Minuten": "minutes",
    "Als deutsches Unternehmen anmelden": "Register as a German company",
    "Als internationaler Gast anmelden": "Register as an international participant",
    "Die Anmeldung ist geschlossen.": "Registration is closed.", "Angemeldet": "Registered",
    "Zeiten in": "Times in", "Alle Veranstaltungen": "All events",
    "Kosten: keine. Sprache der Gespräche: nach Absprache, meist Englisch.":
        "Cost: none. Meeting language: as agreed, usually English.",
    # registration
    "Anmeldung": "Registration", "Schritt 1: Ihre Organisation": "Step 1: your organisation",
    "Schritt 2: Sie als Ansprechperson": "Step 2: you as contact person",
    "Schritt 3: Interessen": "Step 3: interests", "Schritt 4: Verfügbarkeit": "Step 4: availability",
    "Schritt 5: Einwilligung": "Step 5: consent",
    "Firma im Verzeichnis suchen": "Search your company in the directory", "Suchen": "Search",
    "Name oder Ort": "Name or city", "Keine Treffer. Bitte anders schreiben oder Ort weglassen.":
        "No results. Try another spelling or leave out the city.",
    "Ihre Firma fehlt? Nur Unternehmen aus dem Verzeichnis können teilnehmen. Bitte melden Sie sich beim Organisationsteam.":
        "Company missing? Only companies listed in the directory can take part. Please contact the organisers.",
    "Organisation in unserer internationalen Datenbank suchen (optional)":
        "Search your organisation in our international database (optional)",
    "Nicht gefunden? Dann einfach unten eintragen.": "Not found? Just enter it below.",
    "Name der Organisation": "Organisation name", "Art": "Type", "Land": "Country", "Stadt": "City",
    "Website": "Website", "Ihr Name": "Your name", "Funktion": "Position", "E-Mail": "E-mail",
    "Ihre Zeitzone": "Your time zone", "Teilnahme": "Attendance", "Produkte / Produktgruppen": "Products / product groups",
    "z. B. Wundversorgung, Ultraschall, Generika": "e.g. wound care, ultrasound, generics",
    "Ihre Zielmärkte": "Your target markets", "Märkte, in denen Sie aktiv sind": "Markets where you are active",
    "Was bieten Sie?": "What do you offer?", "Was suchen Sie?": "What are you looking for?",
    "z. B. Vertriebspartner in Frankreich, Lizenzpartner, klinische Studien":
        "e.g. distribution partner in France, licensing partner, clinical trials",
    "Wann haben Sie Zeit? Abwählen, was nicht passt.": "When are you available? Untick what does not suit you.",
    "Ich willige ein, dass meine Angaben für diese Matchmaking-Veranstaltung verarbeitet werden.":
        "I consent to my data being processed for this matchmaking event.",
    "Jetzt anmelden": "Register now", "Pflichtfeld": "Required", "Bitte Firma auswählen.": "Please select your company.",
    "Bitte der Einwilligung zustimmen.": "Please give your consent.",
    "Bitte gültige E-Mail angeben.": "Please enter a valid e-mail address.",
    "Ausgewählt": "Selected", "Andere wählen": "Choose another", "Auswählen": "Select",
    "Ihre Organisation": "Your organisation", "(gelöscht)": "(deleted)", "Bietet": "Offers", "Sucht": "Seeks",
    "Gespräche bestätigen": "Confirm meetings", "Verfügbarkeit": "Availability",
    "Wir prüfen jede Anmeldung. Ihr persönlicher Link erscheint direkt nach dem Absenden.":
        "Every registration is reviewed. Your personal link appears right after you submit.",
    # personal page
    "Willkommen": "Welcome", "Ihr persönlicher Link": "Your personal link",
    "Bitte speichern Sie diesen Link (Lesezeichen). Er ist Ihr Zugang – ohne Passwort. Nicht weitergeben.":
        "Please save this link (bookmark). It is your access – no password. Do not share it.",
    "Ihr Zeitplan": "Your schedule", "Noch keine bestätigten Gespräche.": "No confirmed meetings yet.",
    "Kalender (.ics)": "Calendar (.ics)", "Videoraum öffnen": "Open video room", "Tisch": "Table",
    "Termin folgt": "Slot to follow", "Anfragen an Sie": "Requests to you", "Annehmen": "Accept", "Ablehnen": "Decline",
    "Ihre Anfragen": "Your requests", "Wartet auf Antwort": "Awaiting reply", "Absagen": "Cancel",
    "Vorschläge für Sie": "Suggestions for you", "Warum": "Why", "Gespräch anfragen": "Request meeting",
    "Kurze Nachricht (optional)": "Short message (optional)", "Alle anderen Teilnehmenden": "All other participants",
    "Noch keine passenden Teilnehmenden. Schauen Sie später wieder vorbei.":
        "No matching participants yet. Please check back later.",
    "Ihre Angaben": "Your details", "Verfügbarkeit speichern": "Save availability",
    "Meine Daten löschen": "Delete my data",
    "Löscht Ihre Anmeldung und alle Gespräche sofort. Das kann nicht rückgängig gemacht werden.":
        "Deletes your registration and all meetings immediately. This cannot be undone.",
    "Feedback geben": "Give feedback", "Partner": "Partner", "Wo": "Where", "Aktion": "Action",
    "Ihre Zeit": "Your time", "Veranstaltungszeit": "Event time",
    "Videolink: öffnet im Browser, keine Installation nötig. Kamera und Mikrofon erlauben.":
        "Video link: opens in the browser, no installation needed. Allow camera and microphone.",
    "Passende Partner stehen oben. Die Zeile „Warum“ zeigt, was Sie verbindet.":
        "Best matches are at the top. The “Why” line shows what you have in common.",
    "Gleiche Branche": "Same sector", "Zielmarkt": "Target market", "Gleiche Produkte": "Same products",
    "Bietet, was Sie suchen": "Offers what you seek", "Sucht, was Sie bieten": "Seeks what you offer",
    "Gespräch angefragt.": "Meeting requested.", "Gespräch bestätigt und eingeplant.": "Meeting confirmed and scheduled.",
    "Bestätigt – kein gemeinsamer freier Termin. Das Organisationsteam plant nach.":
        "Confirmed – no common free slot. The organisers will schedule it.",
    "Gespeichert.": "Saved.", "Abgesagt.": "Cancelled.", "Abgelehnt.": "Declined.",
    "Ihre Daten wurden gelöscht.": "Your data has been deleted.",
    "Link ungültig oder Anmeldung gelöscht.": "Link invalid or registration deleted.",
    "Maximale Zahl an Gesprächen erreicht.": "Maximum number of meetings reached.",
    "Diese Anfrage gibt es schon.": "This request already exists.",
    "Ihre Anmeldung ist eingegangen.": "Your registration has been received.",
    "Ihr Profil": "Your profile", "Schritte": "Steps", "Profil": "Profile", "Partner wählen": "Choose partners",
    "Gespräche führen": "Hold meetings", "Feedback": "Feedback",
    # survey
    "Umfrage": "Survey", "Wie zufrieden waren Sie insgesamt?": "How satisfied were you overall?",
    "Wie nützlich war die Veranstaltung für Ihr Geschäft?": "How useful was the event for your business?",
    "Wie viele neue Kontakte haben Sie geknüpft?": "How many new contacts did you make?",
    "Welche Umsatzchance erwarten Sie in den nächsten 12 Monaten?":
        "What revenue opportunity do you expect in the next 12 months?",
    "Planen Sie Folgegespräche?": "Are you planning follow-up talks?", "Ja": "Yes", "Nein": "No",
    "Was sollen wir besser machen? (optional)": "What should we improve? (optional)", "Absenden": "Submit",
    "1 = gar nicht, 5 = sehr": "1 = not at all, 5 = very much", "Danke für Ihr Feedback!": "Thank you for your feedback!",
    "Die Umfrage dauert 2 Minuten. Die Antworten werden ausgewertet, ohne Ihren Namen zu zeigen.":
        "The survey takes 2 minutes. Answers are evaluated without showing your name.",
    # admin
    "Neue Veranstaltung": "New event", "Bearbeiten": "Edit", "Teilnehmende": "Participants", "Gespräche": "Meetings",
    "Einladung": "Invitation", "Auswertung": "Results", "Einstellungen": "Settings", "Speichern": "Save",
    "Titel": "Title", "Titel (englisch)": "Title (English)", "Kurzbeschreibung": "Short description",
    "Kurzbeschreibung (englisch)": "Short description (English)", "Programm (englisch)": "Agenda (English)",
    "Eine Zeile pro Punkt, z. B. „10:00 Begrüßung“.": "One line per item, e.g. “10:00 Welcome”.",
    "Beginn": "Start", "Ende": "End", "B2B von": "B2B from", "B2B bis": "B2B until", "Zeitzone": "Time zone",
    "Pause zwischen Gesprächen": "Break between meetings", "Max. Gespräche je Person": "Max. meetings per person",
    "Status": "Status", "Veranstaltungsort (bei hybrid)": "Venue (for hybrid)",
    "Länderkürzel, mit Komma getrennt, z. B. FR, IT, US": "Country codes, comma-separated, e.g. FR, IT, US",
    "Seite ansehen": "View page", "Anmeldungen": "Registrations", "Länder": "Countries",
    "Personenlink": "Personal link", "Anwesend": "Attended", "Gesprächsmatrix": "Meeting matrix",
    "Gespräch anlegen": "Create meeting", "Freie Termine automatisch verteilen": "Schedule open meetings automatically",
    "CSV exportieren": "Export CSV", "Einladungstexte": "Invitation texts", "Anmeldelink": "Registration link",
    "Für Newsletter und Website": "For newsletter and website",
    "Für AHKs, Verbände und Multiplikatoren im Zielmarkt": "For AHKs, associations and multipliers in the target market",
    "Kurzpost für LinkedIn und Netzwerke": "Short post for LinkedIn and networks",
    "Keine Kalt-E-Mails: Einladungen nur über eigene Kanäle (Newsletter, Website, AHK-Netz, Verbände). Werbe-E-Mails ohne Einwilligung sind nach § 7 UWG unzulässig. Die Plattform verschickt keine E-Mails.":
        "No cold e-mails: invite only through your own channels (newsletter, website, AHK network, associations). Promotional e-mails without consent are not allowed under § 7 UWG. The platform sends no e-mails.",
    "Kennzahlen": "Key figures", "Anwesenheitsquote": "Show rate", "angefragt": "requested",
    "bestätigt": "confirmed", "stattgefunden": "held", "Antworten": "Responses", "Zufriedenheit": "Satisfaction",
    "Nutzen": "Benefit", "Kontakte je Person": "Contacts per person", "Folgegespräche geplant": "Follow-up planned",
    "Umsatzchancen": "Revenue opportunities", "Kommentare": "Comments", "noch nicht erfasst": "not recorded yet",
    "Internationale Datenbank": "International database",
    "Organisationen außerhalb Deutschlands aus EU-Registern (EMA, CORDIS, EUDAMED). Nur juristische Personen, keine Kontaktpersonen.":
        "Organisations outside Germany from EU registers (EMA, CORDIS, EUDAMED). Legal entities only, no contact persons.",
    "Ansprache über AHKs, Verbände und Partnernetzwerke – nicht per Kalt-E-Mail.":
        "Reach out via AHKs, associations and partner networks – not by cold e-mail.",
    "Quelle": "Source", "Alle Länder": "All countries", "Alle Branchen": "All sectors", "Treffer": "Results",
    "Organisationen": "Organisations",
    "Tipp: Gespräche entstehen durch Anfragen der Teilnehmenden. Hier können Sie zusätzlich Paare direkt anlegen.":
        "Tip: meetings come from participants' requests. Here you can also create pairs directly.",
    "Zeile = deutsches Unternehmen, Spalte = internationaler Gast.": "Row = German company, column = international participant.",
    "Daten werden 12 Monate nach der Veranstaltung gelöscht.": "Data is deleted 12 months after the event.",
    "Hilfe": "Help", "Zurück": "Back", "Personen": "People", "Programm herunterladen": "Download agenda",
    "Veranstaltung gespeichert.": "Event saved.", "Gespräch angelegt.": "Meeting created.",
    "eingeplant": "scheduled", "ohne Termin": "without slot",
    "Hier legen Sie Veranstaltungen an, sehen Anmeldungen und Gespräche und werten die Umfrage aus.":
        "Create events, see registrations and meetings, and evaluate the survey here.",
    "Status „Anmeldung offen“ macht die Seite öffentlich. Zeiten gelten in der gewählten Zeitzone.":
        "Status “Registration open” makes the page public. Times are in the selected time zone.",
    "Anwesenheit nach der Veranstaltung abhaken – daraus entsteht die Anwesenheitsquote.":
        "Tick attendance after the event – this gives the show rate.",
    "Den Link kopieren und in eigenen Kanälen veröffentlichen.": "Copy the link and publish it in your own channels.",
    "Teilnehmende sehen nur Name, Funktion und Organisation der anderen – nie die E-Mail.":
        "Participants only see the others' name, position and organisation – never the e-mail.",
})

CONSENT = {
    "de": ("Ich willige ein, dass {org} meine Angaben (Name, Funktion, E-Mail, Organisation, Interessen, "
           "Verfügbarkeit) für die Matchmaking-Veranstaltung „{title}“ verarbeitet: Vorschläge, Terminvergabe, "
           "Nachbereitung und eine Umfrage. Name, Funktion, Organisation und Interessen sehen die anderen "
           "Teilnehmenden dieser Veranstaltung, die E-Mail nur das Organisationsteam. Löschung 12 Monate nach der "
           "Veranstaltung ({until}); Widerruf jederzeit über „Meine Daten löschen“ auf meiner persönlichen Seite."),
    "en": ("I consent to {org} processing my details (name, position, e-mail, organisation, interests, "
           "availability) for the matchmaking event “{title}”: suggestions, scheduling, follow-up and a survey. "
           "Other participants of this event see my name, position, organisation and interests; only the "
           "organisers see my e-mail. Deleted 12 months after the event ({until}); I can withdraw at any time via "
           "“Delete my data” on my personal page."),
}


# ---------- small helpers ----------

def tr(row: dict, key: str) -> str:
    return (row.get(key + "_en") or row.get(key) or "") if lang() == "en" else (row.get(key) or "")


def cname(cc: str | None) -> str:
    c = COUNTRIES.get(cc or "")
    return (c[1] if lang() == "en" else c[0]) if c else (cc or "")


def fmt_dt(value, tz: str = "Europe/Berlin", style: str = "full") -> str:
    if not value:
        return ""
    v = value.astimezone(ZoneInfo(tz))
    if style == "time":
        return v.strftime("%H:%M")
    days = (("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So") if lang() == "de" else
            ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))
    d = f"{days[v.weekday()]} {v:%d.%m.%Y}" if lang() == "de" else f"{days[v.weekday()]} {v:%d %b %Y}"
    return d if style == "date" else f"{d}, {v:%H:%M}"


templates.env.globals.update(mm_tr=tr, mm_country=cname, mm_dt=fmt_dt, MM_SECTORS=SECTORS, MM_COUNTRIES=COUNTRIES,
                             MM_ORG_TYPES=ORG_TYPES, MM_REVENUE=REVENUE, MM_STATUS=STATUS, MM_EV_STATUS=EV_STATUS,
                             MM_TZS=TZS)


def page(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, dict(embed=False, **ctx))


def grid(ev: dict) -> list[dt.datetime]:
    step = dt.timedelta(minutes=ev["slot_minutes"] + ev["gap_minutes"])
    out, t = [], ev["b2b_start"]
    while t + dt.timedelta(minutes=ev["slot_minutes"]) <= ev["b2b_end"]:
        out.append(t)
        t += step
    return out


def markets(value: str | list | None) -> list[str]:
    items = value if isinstance(value, list) else re.split(r"[\s,;]+", value or "")
    return sorted({m.strip().upper() for m in items if re.fullmatch(r"[A-Za-z]{2}", m.strip())})


STOP = set("""und oder der die das den dem des ein eine einer eines mit für von aus bei nach sowie auch als auf
in im zu zum zur wir sie ihr unser unsere suchen bieten partner partnern unternehmen firma firmen produkte produkt
lösungen loesungen bereich markt märkte maerkte neue neuen deutschland germany german international the and or for
with from our we you your seek seeking looking offer offering partners company companies products product solutions
market markets new other into also more such services service""".split())
SYN = {"vertri": "distri", "haendl": "distri", "handel": "distri", "kooper": "cooper", "lizenz": "licens",
       "licenc": "licens", "zulass": "regula", "klinis": "clinic", "studie": "trial", "studien": "trial",
       "forsch": "resear", "entwic": "develo", "herste": "manufa", "fertig": "manufa", "produz": "manufa",
       "auftra": "contra", "beteil": "invest", "kapita": "invest", "finanz": "invest"}


def tokens(text: str | None) -> dict[str, str]:
    """Crude stem (first 6 folded letters, de/en synonyms) -> original word."""
    out = {}
    for w in re.findall(r"[^\W\d_]{4,}", (text or "").lower()):
        if w in STOP:
            continue
        s = norm.fold(w)[:6]
        out.setdefault(SYN.get(s, s), w)
    return out


def score(me: dict, other: dict) -> tuple[int, list[str]]:
    pts, why = 0, []
    sec = [s for s in me["sectors"] if s in other["sectors"]]
    if sec:
        pts += 3 * len(sec)
        why.append(_("Gleiche Branche") + ": " + ", ".join(_(SECTORS.get(s, s)) for s in sec))
    de, intl = (me, other) if me["side"] == "de" else (other, me)
    mk = [m for m in de["markets"] if m == intl["country"] or m in intl["markets"]]
    if mk:
        pts += 4
        why.append(_("Zielmarkt") + ": " + ", ".join(cname(m) for m in mk[:3]))
    for label, a, b, w in (("Gleiche Produkte", me["products"], other["products"], 2),
                           ("Bietet, was Sie suchen", me["seek"], other["offer"], 3),
                           ("Sucht, was Sie bieten", me["offer"], other["seek"], 3)):
        ta, tb = tokens(a), tokens(b)
        hit = [tb[k] for k in ta if k in tb][:3]
        if hit:
            pts += w * len(hit)
            why.append(_(label) + ": " + ", ".join(hit))
    return pts, why


def room_url() -> str:
    return VIDEO_BASE.rstrip("/") + "/B2B-" + secrets.token_hex(8)


def get_event(conn, slug_or_id, public: bool = True, request: Request | None = None) -> dict:
    col = "id" if isinstance(slug_or_id, int) else "slug"
    ev = conn.execute(f"SELECT * FROM mm_event WHERE {col} = %s", (slug_or_id,)).fetchone()
    if not ev or (public and ev["status"] == "draft" and not (request and (current_user(request) or {}).get("role") == "admin")):
        raise HTTPException(404)
    return ev


def get_me(conn, token: str) -> tuple[dict, dict]:
    me = conn.execute("SELECT * FROM mm_participant WHERE token = %s", (token,)).fetchone()
    if not me:
        raise HTTPException(404, _("Link ungültig oder Anmeldung gelöscht."))
    return me, get_event(conn, me["event_id"], public=False)


def schedule(conn, mid: int) -> bool:
    """Earliest slot where both sides are available and free. Bookings' primary key forbids double booking."""
    m = conn.execute("SELECT * FROM mm_meeting WHERE id = %s", (mid,)).fetchone()
    if not m or m["starts_at"] or m["status"] != "accepted" or not (m["requester_id"] and m["partner_id"]):
        return bool(m and m["starts_at"])
    ev = conn.execute("SELECT * FROM mm_event WHERE id = %s", (m["event_id"],)).fetchone()
    a, b = (conn.execute("SELECT * FROM mm_participant WHERE id = %s", (pid,)).fetchone()
            for pid in (m["requester_id"], m["partner_id"]))
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (ev["id"],))
        busy = {r["starts_at"].timestamp() for r in conn.execute(
            "SELECT starts_at FROM mm_booking WHERE participant_id IN (%s, %s)", (a["id"], b["id"])).fetchall()}
        for p in (a, b):
            n = conn.execute("SELECT count(*) AS n FROM mm_booking WHERE participant_id = %s", (p["id"],)).fetchone()["n"]
            if n >= ev["max_meetings"]:
                return False
        ok_a = {t.timestamp() for t in a["avail"]} if a["avail"] is not None else None
        ok_b = {t.timestamp() for t in b["avail"]} if b["avail"] is not None else None
        for s in grid(ev):
            ts = s.timestamp()
            if ts in busy or (ok_a is not None and ts not in ok_a) or (ok_b is not None and ts not in ok_b):
                continue
            table = None
            if ev["format"] == "hybrid" and a["mode"] == b["mode"] == "onsite":
                used = {r["table_no"] for r in conn.execute(
                    "SELECT table_no FROM mm_meeting WHERE event_id = %s AND starts_at = %s AND table_no IS NOT NULL",
                    (ev["id"], s)).fetchall()}
                table = next(i for i in range(1, 1000) if i not in used)
            for p in (a, b):
                conn.execute("INSERT INTO mm_booking (participant_id, starts_at, meeting_id) VALUES (%s, %s, %s)",
                             (p["id"], s, mid))
            conn.execute("UPDATE mm_meeting SET starts_at = %s, ends_at = %s, table_no = %s WHERE id = %s",
                         (s, s + dt.timedelta(minutes=ev["slot_minutes"]), table, mid))
            return True
    return False


def unschedule(conn, mid: int, status: str) -> None:
    conn.execute("DELETE FROM mm_booking WHERE meeting_id = %s", (mid,))
    conn.execute("UPDATE mm_meeting SET status = %s, starts_at = NULL, ends_at = NULL, table_no = NULL, "
                 "decided_at = now() WHERE id = %s", (status, mid))


def purge() -> int:
    with db() as conn:
        conn.execute("UPDATE mm_survey s SET comment = NULL FROM mm_participant p "
                     "WHERE s.participant_id = p.id AND p.delete_after < current_date")
        n = conn.execute("DELETE FROM mm_participant WHERE delete_after < current_date").rowcount
    return n


def slugify(title: str, year: int) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", norm.fold(title)).strip("-")[:50] or "event"
    return f"{s}-{year}"


def csv_response(rows: list[dict], fields: list[str], name: str) -> Response:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, delimiter=";", extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    return Response("\ufeff" + buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


def search_companies(conn, side: str, q: str) -> list[dict]:
    if len(q.strip()) < 2:
        return []
    if side == "de":
        return conn.execute("""SELECT id, name, city, website, sectors, 'DE' AS country FROM company
            WHERE name ILIKE %(l)s OR city ILIKE %(l)s OR search @@ websearch_to_tsquery('simple', %(q)s)
            ORDER BY verified DESC, similarity(name, %(q)s) DESC LIMIT 12""", {"q": q, "l": f"%{q}%"}).fetchall()
    return conn.execute("""SELECT DISTINCT ON (name_norm, country) id, name, city, website, sectors, country
        FROM intl_company WHERE name ILIKE %(l)s ORDER BY name_norm, country, website IS NULL, city IS NULL
        LIMIT 12""", {"l": f"%{q}%"}).fetchall()


# ---------- public: events and registration ----------

router = APIRouter(prefix="/matchmaking")


@router.get("", response_class=HTMLResponse)
def events(request: Request):
    with db() as conn:
        rows = conn.execute("""SELECT e.*, (SELECT count(*) FROM mm_participant p WHERE p.event_id = e.id
                               AND p.status <> 'cancelled') AS n FROM mm_event e WHERE status <> 'draft'
                               ORDER BY (ends_at < now()), starts_at""").fetchall()
    return page(request, "mm_list.html", rows=rows)


@router.get("/p/{token}", response_class=HTMLResponse)
def me_page(request: Request, token: str, msg: str = "", neu: bool = False):
    with db() as conn:
        me, ev = get_me(conn, token)
        others = conn.execute("SELECT * FROM mm_participant WHERE event_id = %s AND side <> %s AND status <> 'cancelled'",
                              (ev["id"], me["side"])).fetchall()
        meetings = conn.execute("""SELECT m.*, p.org_name, p.person_name, p.job_title, p.country, p.id AS other_id
            FROM mm_meeting m JOIN mm_participant p ON p.id = CASE WHEN m.requester_id = %(me)s THEN m.partner_id
            ELSE m.requester_id END WHERE %(me)s IN (m.requester_id, m.partner_id)
            ORDER BY m.starts_at NULLS LAST, m.created_at""", {"me": me["id"]}).fetchall()
        survey = conn.execute("SELECT 1 FROM mm_survey WHERE participant_id = %s", (me["id"],)).fetchone()
    active = {m["other_id"]: m for m in meetings if m["status"] not in ("declined", "cancelled")}
    scored = []
    for o in others:
        pts, why = score(me, o)
        scored.append((pts, o, why))
    scored.sort(key=lambda x: (-x[0], x[1]["org_name"].lower()))
    suggestions = [(o, why) for pts, o, why in scored if pts > 0 and o["id"] not in active][:10]
    shown = {o["id"] for o, _why in suggestions} | set(active)
    rest = [o for _pts, o, _why in scored if o["id"] not in shown]
    now = dt.datetime.now(dt.timezone.utc)
    return page(request, "mm_me.html", me=me, ev=ev, meetings=meetings, suggestions=suggestions, rest=rest, msg=msg,
                neu=neu, survey_done=bool(survey), started=now >= ev["starts_at"], slots=grid(ev),
                avail={t.timestamp() for t in me["avail"]} if me["avail"] is not None else None,
                link=str(request.base_url).rstrip("/") + f"/matchmaking/p/{token}",
                counts={k: sum(1 for m in meetings if m["status"] == k) for k in STATUS})


@router.post("/p/{token}/anfrage")
def request_meeting(token: str, partner_id: int = Form(...), message: str = Form("")):
    with db() as conn:
        me, ev = get_me(conn, token)
        other = conn.execute("SELECT * FROM mm_participant WHERE id = %s AND event_id = %s AND side <> %s "
                             "AND status <> 'cancelled'", (partner_id, ev["id"], me["side"])).fetchone()
        if not other or ev["status"] == "done":
            raise HTTPException(400)
        try:
            conn.execute("INSERT INTO mm_meeting (event_id, requester_id, partner_id, message, room_url) "
                         "VALUES (%s, %s, %s, %s, %s)", (ev["id"], me["id"], other["id"], message.strip()[:500] or None,
                                                         room_url()))
            msg = "Gespräch angefragt."
        except Exception:  # noqa: BLE001  unique pair index
            msg = "Diese Anfrage gibt es schon."
    return RedirectResponse(f"/matchmaking/p/{token}?msg={quote(msg)}#anfragen", status_code=303)


@router.post("/p/{token}/antwort")
def answer(token: str, meeting_id: int = Form(...), decision: str = Form(...)):
    with db() as conn:
        me, _ev = get_me(conn, token)
        m = conn.execute("SELECT * FROM mm_meeting WHERE id = %s AND %s IN (requester_id, partner_id)",
                         (meeting_id, me["id"])).fetchone()
        if not m:
            raise HTTPException(404)
        if decision == "accept" and m["status"] == "requested" and m["partner_id"] == me["id"]:
            conn.execute("UPDATE mm_meeting SET status = 'accepted', decided_at = now() WHERE id = %s", (m["id"],))
            msg = ("Gespräch bestätigt und eingeplant." if schedule(conn, m["id"]) else
                   "Bestätigt – kein gemeinsamer freier Termin. Das Organisationsteam plant nach.")
        elif decision == "decline" and m["status"] == "requested" and m["partner_id"] == me["id"]:
            unschedule(conn, m["id"], "declined")
            msg = "Abgelehnt."
        elif decision == "cancel" and m["status"] in ("requested", "accepted"):
            unschedule(conn, m["id"], "cancelled")
            msg = "Abgesagt."
        else:
            raise HTTPException(400)
    return RedirectResponse(f"/matchmaking/p/{token}?msg={quote(msg)}#plan", status_code=303)


@router.post("/p/{token}/verfuegbarkeit")
def set_avail(token: str, slot: Annotated[list[float], Form()] = [], mode: str = Form("")):  # noqa: B006
    with db() as conn:
        me, ev = get_me(conn, token)
        conn.execute("UPDATE mm_participant SET avail = %s, mode = COALESCE(NULLIF(%s, ''), mode) WHERE id = %s",
                     (avail_value(ev, slot), mode if mode in ("online", "onsite") else "", me["id"]))
    return RedirectResponse(f"/matchmaking/p/{token}?msg={quote('Gespeichert.')}#profil", status_code=303)


def avail_value(ev: dict, slot: list[float]) -> list[dt.datetime] | None:
    all_slots = grid(ev)
    chosen = {round(s) for s in slot}
    picked = [s for s in all_slots if round(s.timestamp()) in chosen]
    return None if len(picked) == len(all_slots) else picked


@router.post("/p/{token}/loeschen")
def delete_me(request: Request, token: str):
    with db() as conn:
        me, ev = get_me(conn, token)
        for m in conn.execute("SELECT id FROM mm_meeting WHERE %s IN (requester_id, partner_id) AND status IN "
                              "('requested', 'accepted')", (me["id"],)).fetchall():
            unschedule(conn, m["id"], "cancelled")
        conn.execute("UPDATE mm_survey SET comment = NULL WHERE participant_id = %s", (me["id"],))
        conn.execute("DELETE FROM mm_participant WHERE id = %s", (me["id"],))
    return page(request, "mm_msg.html", title=_("Ihre Daten wurden gelöscht."), ev=ev)


@router.get("/p/{token}/agenda.ics")
def ics(token: str):
    with db() as conn:
        me, ev = get_me(conn, token)
        rows = conn.execute("""SELECT m.*, p.org_name, p.person_name FROM mm_meeting m JOIN mm_participant p
            ON p.id = CASE WHEN m.requester_id = %(me)s THEN m.partner_id ELSE m.requester_id END
            WHERE %(me)s IN (m.requester_id, m.partner_id) AND m.status IN ('accepted', 'held')
            AND m.starts_at IS NOT NULL ORDER BY m.starts_at""", {"me": me["id"]}).fetchall()

    def esc(s: str) -> str:
        return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")

    def stamp(t: dt.datetime) -> str:
        return t.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    now = stamp(dt.datetime.now(dt.timezone.utc))
    title = ev["title_en"] if me["ui_lang"] == "en" and ev["title_en"] else ev["title"]
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Firmenverzeichnis Gesundheitswirtschaft//Matchmaking//DE",
           "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
           "BEGIN:VEVENT", f"UID:mm-event-{ev['id']}-{me['id']}@firmenverzeichnis", f"DTSTAMP:{now}",
           f"DTSTART:{stamp(ev['starts_at'])}", f"DTEND:{stamp(ev['ends_at'])}", f"SUMMARY:{esc(title)}",
           f"LOCATION:{esc(ev['venue'] or 'Online')}", "END:VEVENT"]
    for m in rows:
        where = (f"Tisch {m['table_no']} – {ev['venue']}" if m["table_no"] else m["room_url"])
        out += ["BEGIN:VEVENT", f"UID:mm-meeting-{m['id']}@firmenverzeichnis", f"DTSTAMP:{now}",
                f"DTSTART:{stamp(m['starts_at'])}", f"DTEND:{stamp(m['ends_at'])}",
                f"SUMMARY:{esc('B2B: ' + m['org_name'] + ' (' + m['person_name'] + ')')}",
                f"LOCATION:{esc(where)}", f"DESCRIPTION:{esc(title + chr(10) + m['room_url'])}",
                f"URL:{m['room_url']}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    lines = []
    for line in out:  # RFC 5545 folding at 75 octets
        while len(line.encode()) > 75:
            cut = 74
            while len(line[:cut].encode()) > 74:
                cut -= 1
            lines.append(line[:cut])
            line = " " + line[cut:]
        lines.append(line)
    return Response("\r\n".join(lines) + "\r\n", media_type="text/calendar; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="matchmaking.ics"'})


@router.get("/p/{token}/umfrage", response_class=HTMLResponse)
def survey_form(request: Request, token: str, done: bool = False):
    with db() as conn:
        me, ev = get_me(conn, token)
        prev = conn.execute("SELECT * FROM mm_survey WHERE participant_id = %s", (me["id"],)).fetchone()
        held = conn.execute("SELECT count(*) AS n FROM mm_meeting WHERE %s IN (requester_id, partner_id) "
                            "AND status IN ('accepted', 'held')", (me["id"],)).fetchone()["n"]
    return page(request, "mm_survey.html", me=me, ev=ev, prev=prev or {}, done=done, held=held)


@router.post("/p/{token}/umfrage")
def survey_save(token: str, satisfaction: int = Form(...), benefit: int = Form(...), contacts: int = Form(0),
                revenue_band: str = Form("open"), follow_up: str = Form(""), comment: str = Form("")):
    if not (1 <= satisfaction <= 5 and 1 <= benefit <= 5 and 0 <= contacts <= 500) or revenue_band not in REVENUE:
        raise HTTPException(400)
    with db() as conn:
        me, ev = get_me(conn, token)
        conn.execute("""INSERT INTO mm_survey (event_id, participant_id, side, satisfaction, benefit, contacts,
            revenue_band, follow_up, comment) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (participant_id) DO UPDATE SET satisfaction = EXCLUDED.satisfaction, benefit = EXCLUDED.benefit,
            contacts = EXCLUDED.contacts, revenue_band = EXCLUDED.revenue_band, follow_up = EXCLUDED.follow_up,
            comment = EXCLUDED.comment, submitted_at = now()""",
                     (ev["id"], me["id"], me["side"], satisfaction, benefit, contacts, revenue_band,
                      {"yes": True, "no": False}.get(follow_up), comment.strip()[:2000] or None))
    return RedirectResponse(f"/matchmaking/p/{token}/umfrage?done=1", status_code=303)


# ---------- organiser ----------

admin = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


def kpis(conn, eid: int) -> dict:
    k = conn.execute("""SELECT
        count(*) FILTER (WHERE status <> 'cancelled') AS reg,
        count(*) FILTER (WHERE side = 'de' AND status <> 'cancelled') AS reg_de,
        count(*) FILTER (WHERE side = 'intl' AND status <> 'cancelled') AS reg_intl,
        count(DISTINCT country) FILTER (WHERE side = 'intl') AS countries,
        count(*) FILTER (WHERE attended) AS attended, count(attended) AS marked
        FROM mm_participant WHERE event_id = %s""", (eid,)).fetchone()
    k.update(conn.execute("""SELECT count(*) AS m_req,
        count(*) FILTER (WHERE status IN ('accepted', 'held', 'no_show')) AS m_acc,
        count(*) FILTER (WHERE status = 'held') AS m_held, count(*) FILTER (WHERE status = 'declined') AS m_dec,
        count(*) FILTER (WHERE status IN ('accepted', 'held') AND starts_at IS NULL) AS m_unplanned
        FROM mm_meeting WHERE event_id = %s""", (eid,)).fetchone())
    k["show_rate"] = round(100 * k["attended"] / k["marked"]) if k["marked"] else None
    k["survey"] = conn.execute("""SELECT side, count(*) AS n, round(avg(satisfaction), 1) AS sat,
        round(avg(benefit), 1) AS ben, round(avg(contacts), 1) AS contacts,
        round(100.0 * count(*) FILTER (WHERE follow_up) / NULLIF(count(follow_up), 0)) AS follow
        FROM mm_survey WHERE event_id = %s GROUP BY ROLLUP (side) ORDER BY side NULLS FIRST""", (eid,)).fetchall()
    k["revenue"] = {r["revenue_band"]: r["n"] for r in conn.execute(
        "SELECT revenue_band, count(*) AS n FROM mm_survey WHERE event_id = %s GROUP BY 1", (eid,)).fetchall()}
    return k


@admin.get("", response_class=HTMLResponse)
def admin_home(request: Request):
    purged = purge()
    with db() as conn:
        rows = conn.execute("SELECT * FROM mm_event ORDER BY starts_at DESC").fetchall()
        for r in rows:
            r["k"] = kpis(conn, r["id"])
        intl = conn.execute("SELECT count(*) AS n, count(DISTINCT country) AS c FROM intl_company").fetchone()
    return page(request, "mm_admin.html", rows=rows, intl=intl, purged=purged)


def event_form_ctx(ev: dict | None) -> dict:
    tz = (ev or {}).get("tz", "Europe/Berlin")
    z = ZoneInfo(tz)
    if ev:
        loc = {k: ev[k].astimezone(z) for k in ("starts_at", "ends_at", "b2b_start", "b2b_end")}
        return dict(ev=ev, date=loc["starts_at"].strftime("%Y-%m-%d"),
                    t={k: v.strftime("%H:%M") for k, v in loc.items()}, mk=", ".join(ev["markets"]))
    d = dt.date.today() + dt.timedelta(days=42)
    return dict(ev={"format": "virtual", "tz": tz, "slot_minutes": 20, "gap_minutes": 5, "max_meetings": 8,
                    "sectors": list(SECTORS), "status": "draft", "organiser": ORGANISER},
                date=d.isoformat(), t={"starts_at": "09:30", "ends_at": "13:00", "b2b_start": "10:15",
                                       "b2b_end": "12:45"}, mk="")


@admin.get("/neu", response_class=HTMLResponse)
def event_new(request: Request):
    return page(request, "mm_admin_event.html", tab="edit", **event_form_ctx(None))


@admin.get("/international", response_class=HTMLResponse)
def intl_base(request: Request, q: str = "", country: str = "", sector: str = "", fmt: str = ""):
    clauses, params = ["true"], {}
    if q:
        clauses.append("name ILIKE %(l)s")
        params["l"] = f"%{q}%"
    if country:
        clauses.append("country = %(c)s")
        params["c"] = country.upper()
    if sector in SECTORS:
        clauses.append("%(s)s = ANY(sectors)")
        params["s"] = sector
    w = " AND ".join(clauses)
    with db() as conn:
        rows = conn.execute(f"""SELECT min(name) AS name, country, min(city) AS city, min(website) AS website,
            array_agg(DISTINCT source) AS sources, array_agg(DISTINCT s) FILTER (WHERE s IS NOT NULL) AS sectors
            FROM intl_company LEFT JOIN LATERAL unnest(sectors) s ON true WHERE {w}
            GROUP BY name_norm, country ORDER BY country, min(name) LIMIT %(lim)s""",
                            {**params, "lim": 20000 if fmt == "csv" else 300}).fetchall()
        by_country = conn.execute(f"SELECT country, count(DISTINCT name_norm) AS n FROM intl_company WHERE {w} "
                                  "GROUP BY 1 ORDER BY 2 DESC", params).fetchall()
    if fmt == "csv":
        for r in rows:
            r["sources"], r["sectors"] = ", ".join(r["sources"]), ", ".join(r["sectors"] or [])
        return csv_response(rows, ["name", "country", "city", "website", "sectors", "sources"], "international.csv")
    return page(request, "mm_admin_intl.html", tab="", rows=rows, by_country=by_country, q=q, country=country,
                sector=sector, total=sum(r["n"] for r in by_country), qs=str(request.url.query))


@admin.get("/{eid}", response_class=HTMLResponse)
def event_edit(request: Request, eid: int, msg: str = ""):
    with db() as conn:
        ev = get_event(conn, eid, public=False)
    return page(request, "mm_admin_event.html", tab="edit", msg=msg, **event_form_ctx(ev))


@admin.post("/speichern")
def event_save(eid: int = Form(0), title: str = Form(...), title_en: str = Form(""), intro: str = Form(""),
               intro_en: str = Form(""), organiser: str = Form(ORGANISER), tz: str = Form("Europe/Berlin"),
               date: str = Form(...), starts: str = Form(...), ends: str = Form(...), b2b_start: str = Form(...),
               b2b_end: str = Form(...), slot_minutes: int = Form(20), gap_minutes: int = Form(5),
               max_meetings: int = Form(8), format: str = Form("virtual"), venue: str = Form(""),
               markets_: str = Form("", alias="markets"), sectors: Annotated[list[str], Form()] = [],  # noqa: B006
               agenda: str = Form(""), agenda_en: str = Form(""), status: str = Form("draft")):
    if tz not in TZS or format not in ("virtual", "hybrid") or status not in EV_STATUS or not 5 <= slot_minutes <= 120:
        raise HTTPException(400)
    z = ZoneInfo(tz)
    d = dt.date.fromisoformat(date)

    def at(hhmm: str) -> dt.datetime:
        h, m = map(int, hhmm.split(":")[:2])
        return dt.datetime.combine(d, dt.time(h, m), tzinfo=z)

    vals = dict(title=title.strip(), title_en=title_en.strip() or None, intro=intro.strip() or None,
                intro_en=intro_en.strip() or None, organiser=organiser.strip() or ORGANISER, tz=tz,
                starts_at=at(starts), ends_at=at(ends), b2b_start=at(b2b_start), b2b_end=at(b2b_end),
                slot_minutes=slot_minutes, gap_minutes=max(0, gap_minutes), max_meetings=max(1, max_meetings),
                format=format, venue=venue.strip() or None, markets=markets(markets_),
                sectors=[s for s in sectors if s in SECTORS], agenda=agenda.strip() or None,
                agenda_en=agenda_en.strip() or None, status=status)
    if vals["ends_at"] <= vals["starts_at"] or vals["b2b_end"] <= vals["b2b_start"]:
        raise HTTPException(400, "end before start")
    with db() as conn:
        if eid:
            sets = ", ".join(f"{k} = %({k})s" for k in vals)
            conn.execute(f"UPDATE mm_event SET {sets} WHERE id = %(id)s", {**vals, "id": eid})
            conn.execute("UPDATE mm_participant SET delete_after = %s WHERE event_id = %s",
                         (retention(vals["ends_at"]), eid))
        else:
            slug, i = slugify(vals["title"], d.year), 1
            while conn.execute("SELECT 1 FROM mm_event WHERE slug = %s", (slug,)).fetchone() or slug in RESERVED:
                i += 1
                slug = f"{slugify(vals['title'], d.year)}-{i}"
            cols = ", ".join(vals)
            eid = conn.execute(f"INSERT INTO mm_event (slug, {cols}) VALUES (%(slug)s, "
                               f"{', '.join(f'%({k})s' for k in vals)}) RETURNING id", {**vals, "slug": slug}).fetchone()["id"]
    return RedirectResponse(f"/matchmaking/admin/{eid}?msg={quote('Veranstaltung gespeichert.')}", status_code=303)


def retention(ends_at: dt.datetime) -> dt.date:
    d = ends_at.date()
    y, m = divmod(d.month - 1 + RETENTION_MONTHS, 12)
    return dt.date(d.year + y, m + 1, min(d.day, 28))


@admin.get("/{eid}/teilnehmende", response_class=HTMLResponse)
def people(request: Request, eid: int):
    with db() as conn:
        ev = get_event(conn, eid, public=False)
        rows = conn.execute("""SELECT p.*, (SELECT count(*) FROM mm_meeting m WHERE p.id IN (m.requester_id, m.partner_id)
            AND m.status IN ('accepted', 'held')) AS n_meet FROM mm_participant p WHERE event_id = %s
            ORDER BY side, org_name""", (eid,)).fetchall()
    return page(request, "mm_admin_people.html", tab="people", ev=ev, rows=rows, base=str(request.base_url).rstrip("/"))


@admin.post("/{eid}/teilnehmende/{pid}")
def people_set(eid: int, pid: int, status: str = Form(""), attended: str = Form("")):
    with db() as conn:
        if status in ("registered", "confirmed", "cancelled"):
            conn.execute("UPDATE mm_participant SET status = %s WHERE id = %s AND event_id = %s", (status, pid, eid))
            if status == "cancelled":
                for m in conn.execute("SELECT id FROM mm_meeting WHERE %s IN (requester_id, partner_id) AND status IN "
                                      "('requested', 'accepted')", (pid,)).fetchall():
                    unschedule(conn, m["id"], "cancelled")
        if attended in ("yes", "no", "unknown"):
            conn.execute("UPDATE mm_participant SET attended = %s WHERE id = %s AND event_id = %s",
                         ({"yes": True, "no": False}.get(attended), pid, eid))
    return RedirectResponse(f"/matchmaking/admin/{eid}/teilnehmende#p{pid}", status_code=303)


@admin.get("/{eid}/gespraeche", response_class=HTMLResponse)
def meetings(request: Request, eid: int, msg: str = ""):
    with db() as conn:
        ev = get_event(conn, eid, public=False)
        ps = conn.execute("SELECT id, side, org_name, person_name, country FROM mm_participant WHERE event_id = %s "
                          "AND status <> 'cancelled' ORDER BY org_name", (eid,)).fetchall()
        ms = conn.execute("""SELECT m.*, a.org_name AS a_org, a.side AS a_side, b.org_name AS b_org
            FROM mm_meeting m LEFT JOIN mm_participant a ON a.id = m.requester_id
            LEFT JOIN mm_participant b ON b.id = m.partner_id WHERE m.event_id = %s
            ORDER BY m.starts_at NULLS LAST, m.id""", (eid,)).fetchall()
    de = [p for p in ps if p["side"] == "de"]
    intl = [p for p in ps if p["side"] == "intl"]
    cell = {}
    for m in ms:
        if m["status"] not in ("declined", "cancelled") and m["requester_id"] and m["partner_id"]:
            cell[(m["requester_id"], m["partner_id"])] = cell[(m["partner_id"], m["requester_id"])] = m
    return page(request, "mm_admin_meetings.html", tab="meetings", ev=ev, de=de, intl=intl, ms=ms, cell=cell, msg=msg)


@admin.post("/{eid}/gespraeche")
def meeting_create(eid: int, de_id: int = Form(...), intl_id: int = Form(...)):
    with db() as conn:
        ok = conn.execute("SELECT count(*) AS n FROM mm_participant WHERE event_id = %s AND ((id = %s AND side = 'de') "
                          "OR (id = %s AND side = 'intl'))", (eid, de_id, intl_id)).fetchone()["n"] == 2
        if not ok:
            raise HTTPException(400)
        try:
            mid = conn.execute("INSERT INTO mm_meeting (event_id, requester_id, partner_id, status, room_url, created_by, "
                               "decided_at) VALUES (%s,%s,%s,'accepted',%s,'organiser',now()) RETURNING id",
                               (eid, de_id, intl_id, room_url())).fetchone()["id"]
        except Exception:  # noqa: BLE001
            return RedirectResponse(f"/matchmaking/admin/{eid}/gespraeche?msg={quote('Diese Anfrage gibt es schon.')}",
                                    status_code=303)
        msg = ("Gespräch angelegt." if schedule(conn, mid) else
               "Bestätigt – kein gemeinsamer freier Termin. Das Organisationsteam plant nach.")
    return RedirectResponse(f"/matchmaking/admin/{eid}/gespraeche?msg={quote(msg)}", status_code=303)


@admin.post("/{eid}/planen")
def plan_all(eid: int):
    with db() as conn:
        ids = [r["id"] for r in conn.execute("SELECT id FROM mm_meeting WHERE event_id = %s AND status = 'accepted' "
                                             "AND starts_at IS NULL ORDER BY decided_at, id", (eid,)).fetchall()]
        n = sum(schedule(conn, i) for i in ids)
    return RedirectResponse(f"/matchmaking/admin/{eid}/gespraeche?msg={quote(f'{n}/{len(ids)} eingeplant')}",
                            status_code=303)


@admin.post("/{eid}/gespraeche/{mid}")
def meeting_set(eid: int, mid: int, status: str = Form(...)):
    if status not in STATUS:
        raise HTTPException(400)
    with db() as conn:
        if status in ("declined", "cancelled"):
            unschedule(conn, mid, status)
        else:
            conn.execute("UPDATE mm_meeting SET status = %s, decided_at = COALESCE(decided_at, now()) "
                         "WHERE id = %s AND event_id = %s", (status, mid, eid))
            if status == "accepted":
                schedule(conn, mid)
    return RedirectResponse(f"/matchmaking/admin/{eid}/gespraeche#m{mid}", status_code=303)


@admin.get("/{eid}/einladung", response_class=HTMLResponse)
def invite(request: Request, eid: int):
    with db() as conn:
        ev = get_event(conn, eid, public=False)
    url = str(request.base_url).rstrip("/") + f"/matchmaking/{ev['slug']}"
    when_de = f"{fmt_dt(ev['starts_at'], ev['tz'], 'date')}, {fmt_dt(ev['starts_at'], ev['tz'], 'time')}–" \
              f"{fmt_dt(ev['ends_at'], ev['tz'], 'time')} ({ev['tz']})"
    mk_de = ", ".join(COUNTRIES.get(m, (m, m))[0] for m in ev["markets"]) or "internationale Märkte"
    mk_en = ", ".join(COUNTRIES.get(m, (m, m))[1] for m in ev["markets"]) or "international markets"
    sec_en = ", ".join({"medtech": "medical technology", "pharma": "pharma", "biotech": "biotech",
                        "digital_health": "digital health"}[s] for s in ev["sectors"]) or "health industry"
    fmt_de = "online" if ev["format"] == "virtual" else f"hybrid – online und vor Ort in {ev['venue'] or '…'}"
    fmt_en = "online" if ev["format"] == "virtual" else f"hybrid – online and on site in {ev['venue'] or '…'}"
    v = ev["starts_at"].astimezone(ZoneInfo(ev["tz"]))
    texts = [
        ("Für Newsletter und Website",
         f"{ev['title']}\n\n{ev['intro'] or ''}\n\nWann: {when_de}\nFormat: {fmt_de}\nZielmärkte: {mk_de}\n\n"
         f"Treffen Sie in {ev['slot_minutes']}-minütigen B2B-Gesprächen ausgewählte Partner aus {mk_de}. Sie melden "
         f"sich an, erhalten passende Vorschläge und vereinbaren Ihre Gespräche selbst. Die Teilnahme ist kostenfrei.\n\n"
         f"Jetzt anmelden: {url}\n\nVeranstalter: {ev['organiser']}"),
        ("Für AHKs, Verbände und Multiplikatoren im Zielmarkt",
         f"{ev['title_en'] or ev['title']}\n\n{ev['intro_en'] or ''}\n\nWhen: {v:%d %B %Y}, "
         f"{fmt_dt(ev['starts_at'], ev['tz'], 'time')}–{fmt_dt(ev['ends_at'], ev['tz'], 'time')} ({ev['tz']})\n"
         f"Format: {fmt_en}\nSectors: {sec_en}\n\nMeet German companies in pre-arranged {ev['slot_minutes']}-minute "
         f"B2B meetings. Register, see matching German partners and book your meetings. Free of charge.\n\n"
         f"Please share this invitation with your members in {mk_en}.\nRegister: {url}?lang=en\n\n"
         f"Organised by {ev['organiser']}"),
        ("Kurzpost für LinkedIn und Netzwerke",
         f"B2B matchmaking {sec_en}: German companies × partners from {mk_en}, {v:%d %b %Y}, {fmt_en}. "
         f"Free registration: {url}?lang=en"),
    ]
    return page(request, "mm_admin_invite.html", tab="invite", ev=ev, url=url, texts=texts)


@admin.get("/{eid}/auswertung", response_class=HTMLResponse)
def report(request: Request, eid: int):
    with db() as conn:
        ev = get_event(conn, eid, public=False)
        k = kpis(conn, eid)
        comments = conn.execute("SELECT side, comment FROM mm_survey WHERE event_id = %s AND comment IS NOT NULL "
                                "ORDER BY submitted_at DESC", (eid,)).fetchall()
    total = sum(k["revenue"].values()) or 1
    return page(request, "mm_admin_report.html", tab="report", ev=ev, k=k, comments=comments, total=total)


P_FIELDS = ["id", "side", "org_name", "org_type", "country", "city", "website", "company_id", "intl_company_id",
            "person_name", "job_title", "email", "mode", "sectors", "markets", "products", "offer", "seek", "status",
            "attended", "created_at", "delete_after"]
M_FIELDS = ["id", "status", "starts_at", "ends_at", "a_org", "b_org", "room_url", "table_no", "created_by",
            "created_at", "decided_at"]
S_FIELDS = ["side", "satisfaction", "benefit", "contacts", "revenue_band", "follow_up", "comment", "submitted_at"]


@admin.get("/{eid}/teilnehmende.csv")
def people_csv(eid: int):
    with db() as conn:
        rows = conn.execute("SELECT * FROM mm_participant WHERE event_id = %s ORDER BY side, org_name", (eid,)).fetchall()
    for r in rows:
        r["sectors"], r["markets"] = ", ".join(r["sectors"]), ", ".join(r["markets"])
    return csv_response(rows, P_FIELDS, f"teilnehmende-{eid}.csv")


@admin.get("/{eid}/gespraeche.csv")
def meetings_csv(eid: int):
    with db() as conn:
        rows = conn.execute("""SELECT m.*, a.org_name AS a_org, b.org_name AS b_org FROM mm_meeting m
            LEFT JOIN mm_participant a ON a.id = m.requester_id LEFT JOIN mm_participant b ON b.id = m.partner_id
            WHERE m.event_id = %s ORDER BY m.starts_at NULLS LAST, m.id""", (eid,)).fetchall()
    return csv_response(rows, M_FIELDS, f"gespraeche-{eid}.csv")


@admin.get("/{eid}/umfrage.csv")
def survey_csv(eid: int):
    with db() as conn:
        rows = conn.execute("SELECT * FROM mm_survey WHERE event_id = %s ORDER BY submitted_at", (eid,)).fetchall()
    return csv_response(rows, S_FIELDS, f"umfrage-{eid}.csv")


router.include_router(admin)


# ---------- public event page and registration (after admin: /{slug} would swallow /admin) ----------

@router.get("/{slug}", response_class=HTMLResponse)
def event_page(request: Request, slug: str):
    with db() as conn:
        ev = get_event(conn, slug, request=request)
        n = {r["side"]: r["n"] for r in conn.execute("SELECT side, count(*) AS n FROM mm_participant WHERE event_id = %s "
                                                     "AND status <> 'cancelled' GROUP BY side", (ev["id"],)).fetchall()}
    return page(request, "mm_event.html", ev=ev, n=n, slots=grid(ev))


@router.get("/{slug}/anmelden", response_class=HTMLResponse)
def register_form(request: Request, slug: str, side: str = "de", q: str = "", pick: str = "", error: str = ""):
    if side not in ("de", "intl"):
        raise HTTPException(400)
    with db() as conn:
        ev = get_event(conn, slug, request=request)
        hits = search_companies(conn, side, q) if q else []
        chosen = None
        if pick:
            chosen = conn.execute("SELECT id, name, city, website, sectors, 'DE' AS country FROM company WHERE id = %s"
                                  if side == "de" else
                                  "SELECT id, name, city, website, sectors, country FROM intl_company WHERE id = %s",
                                  (pick,)).fetchone()
    until = retention(ev["ends_at"])
    consent = CONSENT[lang()].format(org=ev["organiser"], title=tr(ev, "title"), until=until.strftime("%d.%m.%Y"))
    return page(request, "mm_register.html", ev=ev, side=side, q=q, hits=hits, chosen=chosen, error=error,
                slots=grid(ev), consent=consent)


@router.post("/{slug}/anmelden")
def register_save(request: Request, slug: str, side: str = Form(...), pick: str = Form(""), org_name: str = Form(""),
                  org_type: str = Form("company"), country: str = Form(""), city: str = Form(""),
                  website: str = Form(""), person_name: str = Form(...), job_title: str = Form(""),
                  email: str = Form(...), tz: str = Form("Europe/Berlin"), mode: str = Form("online"),
                  sectors: Annotated[list[str], Form()] = [], products: str = Form(""),  # noqa: B006
                  markets_: Annotated[list[str], Form(alias="markets")] = [], markets_extra: str = Form(""),  # noqa: B006
                  offer: str = Form(""), seek: str = Form(""), slot: Annotated[list[float], Form()] = [],  # noqa: B006
                  consent: str = Form("")):
    back = f"/matchmaking/{slug}/anmelden?side={side}&pick={quote(pick)}&error="
    if side not in ("de", "intl"):
        raise HTTPException(400)
    if not consent:
        return RedirectResponse(back + quote("Bitte der Einwilligung zustimmen."), status_code=303)
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", email.strip()):
        return RedirectResponse(back + quote("Bitte gültige E-Mail angeben."), status_code=303)
    with db() as conn:
        ev = get_event(conn, slug, request=request)
        if ev["status"] != "open":
            raise HTTPException(403, _("Die Anmeldung ist geschlossen."))
        company_id = intl_id = None
        if side == "de":
            c = conn.execute("SELECT * FROM company WHERE id = %s", (pick,)).fetchone() if pick else None
            if not c:
                return RedirectResponse(back + quote("Bitte Firma auswählen."), status_code=303)
            company_id, org_name, country, city, website = c["id"], c["name"], "DE", c["city"], c["website"]
            org_type = "company"
        else:
            c = conn.execute("SELECT * FROM intl_company WHERE id = %s", (pick,)).fetchone() if pick else None
            if c:
                intl_id, org_name = c["id"], c["name"]
                country, city, website = c["country"], city or c["city"], website or c["website"]
            country = country.upper().strip()
            if not org_name.strip() or not re.fullmatch(r"[A-Z]{2}", country) or country == "DE":
                return RedirectResponse(back + quote("Pflichtfeld") + ": " + quote(_("Name der Organisation")) + ", "
                                        + quote(_("Land")), status_code=303)
        until = retention(ev["ends_at"])
        consent_text = CONSENT[lang()].format(org=ev["organiser"], title=tr(ev, "title"), until=until.strftime("%d.%m.%Y"))
        token = secrets.token_urlsafe(24)
        conn.execute("""INSERT INTO mm_participant (event_id, token, side, company_id, intl_company_id, org_name, org_type,
            country, city, website, person_name, job_title, email, ui_lang, tz, mode, sectors, products, markets, offer,
            seek, avail, consent_at, consent_text, delete_after)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s)""", (
            ev["id"], token, side, company_id, intl_id, org_name.strip()[:200],
            org_type if org_type in ORG_TYPES else "company", country, (city or "").strip()[:100] or None,
            norm.website(website) if website else None, person_name.strip()[:120], job_title.strip()[:120] or None,
            email.strip()[:200], lang(), tz if tz in TZS else "Europe/Berlin",
            mode if ev["format"] == "hybrid" and mode in ("online", "onsite") else "online",
            [s for s in sectors if s in SECTORS], products.strip()[:500] or None,
            markets(list(markets_) + re.split(r"[\s,;]+", markets_extra)), offer.strip()[:1000] or None,
            seek.strip()[:1000] or None, avail_value(ev, slot) if slot else None, consent_text, until))
    return RedirectResponse(f"/matchmaking/p/{token}?neu=1", status_code=303)


if __name__ == "__main__":
    if sys.argv[1:2] == ["schema"]:
        with db() as conn:
            conn.execute(SCHEMA)
        print("matchmaking schema ok")
    elif sys.argv[1:2] == ["purge"]:
        print(f"purged {purge()} participants past retention")
    else:
        print(__doc__)
