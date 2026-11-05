-- Counting queries behind the numbers in README.md (run against the directory database, PostgreSQL 16).
-- psql -At -F ' | ' -f nachweis/2026-11-05/zaehlungen.sql
\echo 'companies total | register-verified | verified tier A | verified tier B'
SELECT count(*), count(*) FILTER (WHERE verified), count(*) FILTER (WHERE verified AND tier = 'A'),
       count(*) FILTER (WHERE verified AND tier = 'B') FROM company WHERE hidden_reason IS NULL;
\echo 'verified: e-mail (company mailbox) or contact/imprint page | website only | no contact path | with contact/imprint page link'
SELECT count(*) FILTER (WHERE email_role IS NOT NULL OR contact_url IS NOT NULL),
       count(*) FILTER (WHERE email_role IS NULL AND contact_url IS NULL AND website IS NOT NULL),
       count(*) FILTER (WHERE email_role IS NULL AND contact_url IS NULL AND website IS NULL),
       count(*) FILTER (WHERE contact_url IS NOT NULL) FROM company WHERE verified AND hidden_reason IS NULL;
\echo 'verified with size class | with product identifier (EMDN group or EMA product) | EMDN | EMA | employee figure (all)'
SELECT count(*) FILTER (WHERE verified AND size_class IS NOT NULL),
       count(*) FILTER (WHERE verified AND (cardinality(emdn_groups) > 0 OR cardinality(products) > 0)),
       count(*) FILTER (WHERE verified AND cardinality(emdn_groups) > 0),
       count(*) FILTER (WHERE verified AND cardinality(products) > 0),
       count(*) FILTER (WHERE employees IS NOT NULL) FROM company WHERE hidden_reason IS NULL;
\echo 'verified by sector'
SELECT s, count(*) FROM company, unnest(sectors) s WHERE verified AND hidden_reason IS NULL GROUP BY s ORDER BY 2 DESC;
\echo 'international base: companies | countries | by source'
SELECT count(*), count(DISTINCT country) FROM intl_company;
SELECT source, count(*) FROM intl_company GROUP BY 1 ORDER BY 1;
\echo 'quality sample v1 (hand check): tier | in health industry | of decided'
SELECT c.tier, count(*) FILTER (WHERE q.in_scope), count(q.in_scope) FROM quality_check q JOIN company c ON c.id = q.company_id
WHERE q.sample = 'v1' GROUP BY 1 ORDER BY 1;
