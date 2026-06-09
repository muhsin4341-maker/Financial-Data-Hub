-- Source configuration seed data — Phase 1: SEC EDGAR (US regulatory filings).
-- M3 Execution Plan, M3.1 — Source Registry.
--
-- This seed file populates the source_configs table with the initial set of
-- known acquisition providers.  Run after: alembic upgrade head
--
-- Table schema (migration 004):
--   id, code, name, description, provider_type, country_code,
--   base_url, rate_limit_per_minute, is_active, config, created_at, updated_at
--
-- id is intentionally omitted — PostgreSQL will use the gen_uuid7() Python
-- default on first INSERT via the ORM, or you can let the server default handle it.
-- Since this is a raw SQL seed, we allow the server default to supply NOW() for
-- timestamps.  id must be supplied in raw SQL (no Python default here).
-- Use gen_random_uuid() from pgcrypto (installed in migration 001).

INSERT INTO source_configs (
    id,
    code,
    name,
    description,
    provider_type,
    country_code,
    base_url,
    rate_limit_per_minute,
    is_active,
    config
) VALUES (
    gen_random_uuid(),
    'SEC_EDGAR',
    'SEC EDGAR',
    'U.S. Securities and Exchange Commission — Electronic Data Gathering, Analysis, and Retrieval system. '
    'Primary source for US public company filings (10-K, 10-Q, 8-K, etc.).',
    'regulatory',
    'US',
    'https://efts.sec.gov',
    600,    -- 10 requests per second = 600 per minute (SEC fair-use policy)
    true,
    '{
        "full_text_search_url": "https://efts.sec.gov/LATEST/search-index?q=%22{company_name}%22&dateRange=custom&startdt={start_date}&enddt={end_date}&forms={form_type}",
        "submission_url": "https://data.sec.gov/submissions/CIK{cik_padded}.json",
        "company_facts_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json",
        "filing_index_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}&dateb=&owner=include&count=10",
        "archive_base_url": "https://www.sec.gov/Archives/edgar/data",
        "required_user_agent": true,
        "user_agent_notes": "User-Agent header REQUIRED per SEC fair access policy. Format: AppName/Version contact@email.com",
        "rate_limit_notes": "Max 10 requests/second sustained. Bursts may trigger 429. Use Redis token bucket.",
        "ixbrl_available_from_year": 2019,
        "primary_form_type": "10-K",
        "quarterly_form_type": "10-Q",
        "supports_full_text_search": true,
        "supports_xbrl": true
    }'::jsonb
)
ON CONFLICT (code) DO NOTHING;
