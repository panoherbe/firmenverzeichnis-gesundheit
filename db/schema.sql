-- firmenverzeichnis-gesundheit: one schema, rebuilt idempotently (CREATE IF NOT EXISTS).
-- Invariant: no person data. No column holds a person name, a named e-mail or a direct dial.

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- Every source with its licence; drives the "Quellen & Lizenzen" page and export footers.
CREATE TABLE IF NOT EXISTS source (
    id           text PRIMARY KEY,
    title        text NOT NULL,
    url          text NOT NULL,
    licence      text NOT NULL,
    attribution  text NOT NULL,
    share_alike  boolean NOT NULL DEFAULT false,
    note         text
);

-- One row per entity per source, normalised. raw_path/raw_sha256 point at the file on datanuc.
CREATE TABLE IF NOT EXISTS source_record (
    source          text NOT NULL REFERENCES source(id),
    source_id       text NOT NULL,
    raw_path        text NOT NULL,
    raw_sha256      text NOT NULL,
    loaded_at       timestamptz NOT NULL DEFAULT now(),
    name            text NOT NULL,
    name_norm       text NOT NULL,
    legal_form      text,            -- GmbH, AG, SE, GmbH & Co. KG, ... (NULL = not recognised)
    natural_person  boolean NOT NULL DEFAULT false,  -- e.K., Inh., Apotheke, GbR: excluded from company
    street          text,
    post_code       text,
    city            text,
    region          text,            -- ISO 3166-2 (DE-BY) where a source states it
    country         text,
    website         text,
    domain          text,
    email_role      text,            -- only role mailboxes (info@, kontakt@ ...); named addresses are dropped
    vat             text,
    register_no     text,            -- 'HRB 12345' normalised to 'HRB12345'
    register_ra     text,            -- GLEIF registration authority code (identifies the court)
    lei             text,
    srn             text,            -- EUDAMED single registration number
    roles           text[] NOT NULL DEFAULT '{}',  -- eudamed: MF/AR/IM/PR; ema: MAH; corpus: SUPPLIER
    active          boolean NOT NULL DEFAULT true,
    facts           jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS source_record_name_idx ON source_record (name_norm);
CREATE INDEX IF NOT EXISTS source_record_vat_idx ON source_record (vat);
CREATE INDEX IF NOT EXISTS source_record_reg_idx ON source_record (register_no);

-- The resolved company. Rebuilt by etl.resolve from source_record + match decisions.
CREATE TABLE IF NOT EXISTS company (
    id              text PRIMARY KEY,   -- stable: 'c' + first 12 hex of sha1(anchor record key)
    name            text NOT NULL,
    legal_form      text,
    street          text,
    post_code       text,
    city            text,
    region          text,
    website         text,
    domain          text,
    email_role      text,
    vat             text,
    register_no     text,
    lei             text,
    srns            text[] NOT NULL DEFAULT '{}',
    sectors         text[] NOT NULL DEFAULT '{}',   -- medtech, pharma, biotech, digital_health
    sector_evidence jsonb NOT NULL DEFAULT '{}',
    roles           text[] NOT NULL DEFAULT '{}',
    sources         text[] NOT NULL DEFAULT '{}',
    public_awards   integer NOT NULL DEFAULT 0,
    public_buyers   integer NOT NULL DEFAULT 0,
    public_last_award date,
    verified        boolean NOT NULL DEFAULT false,
    verified_reason text,
    search          tsvector,
    built_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS company_search_idx ON company USING gin (search);
CREATE INDEX IF NOT EXISTS company_sectors_idx ON company USING gin (sectors);
CREATE INDEX IF NOT EXISTS company_name_trgm_idx ON company USING gin (name gin_trgm_ops);

-- A = manufacturer / marketing-authorisation holder (EUDAMED MF/PR, EMA MAH)
-- B = authorised representative / importer only (EUDAMED AR/IM)
-- C = supplier to public buyers only (tender corpus; distributors, service firms, manufacturers not yet confirmed)
ALTER TABLE company ADD COLUMN IF NOT EXISTS tier text;
CREATE INDEX IF NOT EXISTS company_tier_idx ON company (tier);

CREATE TABLE IF NOT EXISTS company_member (
    company_id  text NOT NULL,
    source      text NOT NULL,
    source_id   text NOT NULL,
    rule        text NOT NULL,       -- which key joined this record (vat, lei, register, name+plz, ...)
    PRIMARY KEY (source, source_id)
);
CREATE INDEX IF NOT EXISTS company_member_company_idx ON company_member (company_id);

-- Hand-checked quality sample (etl.sample): one row per sampled company and check.
CREATE TABLE IF NOT EXISTS quality_check (
    sample      text NOT NULL,
    company_id  text NOT NULL,
    is_company  boolean,      -- real, existing legal person in Germany
    in_scope    boolean,      -- belongs to the health industry (any of the four sectors)
    sector_ok   boolean,      -- our sector labels are right
    note        text,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sample, company_id)
);

INSERT INTO source (id, title, url, licence, attribution, share_alike, note) VALUES
 ('eudamed', 'EUDAMED – Actor registration (European Commission)', 'https://ec.europa.eu/tools/eudamed',
  'Re-use under Commission Decision 2011/833/EU (assumed; EUDAMED-specific notice not found)',
  'Quelle: Europäische Kommission, EUDAMED', false, 'PRRC person data stripped before raw write'),
 ('ema', 'EMA – Article 57 product data', 'https://www.ema.europa.eu/en/human-regulatory-overview/post-authorisation/data-medicines-iso-idmp-standards-post-authorisation/public-data-article-57-database',
  'EMA legal notice: reproduction authorised with source acknowledged (assumed, verify)',
  'Quelle: European Medicines Agency, Article 57 database', false, 'MAH names only'),
 ('corpus', 'Öffentliche Aufträge (TED + Bekanntmachungsservice), eigene Auswertung', 'https://ted.europa.eu',
  'TED: re-use under Decision 2011/833/EU; BKMS: open data', 'Quelle: TED / oeffentlichevergabe.de, eigene Auswertung', false, 'Award winners with health CPV'),
 ('cordis', 'CORDIS – Horizon-Europe-Projekte (Europäische Kommission)', 'https://cordis.europa.eu/about/legal',
  'CC BY 4.0', 'Quelle: Europäische Kommission, CORDIS (CC BY 4.0)', false, 'German companies in health projects'),
 ('gleif', 'GLEIF – LEI Golden Copy', 'https://www.gleif.org/en/lei-data/gleif-golden-copy', 'CC0 1.0',
  'Quelle: GLEIF (CC0)', false, 'Identity check only')
ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title, url = EXCLUDED.url, licence = EXCLUDED.licence,
  attribution = EXCLUDED.attribution, share_alike = EXCLUDED.share_alike, note = EXCLUDED.note;
