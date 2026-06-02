-- Source configuration seed data — Phase 1: SEC EDGAR (US regulatory filings).
-- Engineering Spec Part 1, Section 1.3 / Implementation Guide Section 3 Step 7.

INSERT INTO source_configs (
    name,
    source_type,
    country_code,
    tier,
    base_url,
    url_pattern,
    requires_api_key,
    rate_limit_rpm,
    is_active,
    config
) VALUES (
    'SEC EDGAR',
    'regulatory',
    'US',
    1,
    'https://efts.sec.gov',
    'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type={form_type}&dateb=&owner=include&count=10',
    false,
    600,    -- 10 requests per second = 600 per minute
    true,
    '{
        "full_text_search_url": "https://efts.sec.gov/LATEST/search-index?q=%22{company_name}%22&dateRange=custom&startdt={start_date}&enddt={end_date}&forms={form_type}",
        "filing_index_url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}&type=10-K&dateb=&owner=include&count=10&search_text=",
        "submission_url": "https://data.sec.gov/submissions/CIK{cik_padded}.json",
        "company_facts_url": "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_padded}.json",
        "required_user_agent": true,
        "notes": "User-Agent header REQUIRED per SEC fair access policy. Format: AppName contact@email.com",
        "rate_limit_notes": "Max 10 requests/second. Sustained bursts may trigger 429. Use token bucket.",
        "ixbrl_available_from_year": 2019,
        "primary_form_type": "10-K",
        "quarterly_form_type": "10-Q"
    }'
);
