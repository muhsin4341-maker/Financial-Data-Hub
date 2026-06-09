/**
 * Shared TypeScript types mirroring the FastAPI backend schemas.
 * Generated manually from the OpenAPI contract; run `openapi-typescript`
 * in CI to keep in sync.
 */

// ---------------------------------------------------------------------------
// Auth
// ---------------------------------------------------------------------------

export interface AuthResponse {
  access_token: string
  token_type: string
}

export interface RegisterRequest {
  email: string
  password: string
  full_name: string
  workspace_name: string
}

export interface LoginRequest {
  email: string
  password: string
}

// ---------------------------------------------------------------------------
// Companies
// ---------------------------------------------------------------------------

export interface Company {
  id: string
  tenant_id: string
  name: string
  ticker: string
  cik: string | null
  exchange: string | null
  sector: string | null
  industry: string | null
  description: string | null
  website: string | null
  is_active: boolean
  created_at: string
  updated_at: string
}

export interface CompanyListResponse {
  items: Company[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface CompanyCreate {
  name: string
  ticker: string
  cik?: string
  exchange?: string
  sector?: string
  industry?: string
  description?: string
  website?: string
}

// ---------------------------------------------------------------------------
// Jobs
// ---------------------------------------------------------------------------

export type JobStatus =
  | 'pending'
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'

export interface Job {
  id: string
  tenant_id: string
  company_id: string
  created_by: string | null
  status: JobStatus
  job_type: string
  fiscal_year: number | null
  document_url: string | null
  result_url: string | null
  error_message: string | null
  celery_task_id: string | null
  started_at: string | null
  completed_at: string | null
  is_terminal: boolean
  is_cancellable: boolean
  created_at: string
  updated_at: string
}

export interface JobListResponse {
  items: Job[]
  total: number
  page: number
  page_size: number
  pages: number
}

export interface UploadUrlResponse {
  url: string
  key: string
  expires_in: number
}

/**
 * Lightweight status-only snapshot returned by GET /api/v1/jobs/{id}/status.
 * Used by the client-side polling engine — cheaper than fetching the full Job.
 */
export interface JobStatusPoll {
  id: string
  status: JobStatus
  started_at: string | null
  completed_at: string | null
  error_message: string | null
}

// ---------------------------------------------------------------------------
// Invitations
// ---------------------------------------------------------------------------

export type InvitationStatus = 'pending' | 'accepted' | 'cancelled' | 'expired'
export type UserRole = 'owner' | 'admin' | 'analyst' | 'viewer'

export interface Invitation {
  id: string
  tenant_id: string
  invitee_email: string
  role: string
  status: InvitationStatus
  expires_at: string
  accepted_at: string | null
  invited_by_id: string | null
  created_at: string
  updated_at: string
}

// ---------------------------------------------------------------------------
// Source Registry — M3.5
// ---------------------------------------------------------------------------

/** Provider category — matches backend ProviderType enum values. */
export type ProviderType = 'regulatory' | 'exchange' | 'manual' | 'broker'

/**
 * A single data acquisition source registered in the platform source registry.
 * Source configs are global (no tenant_id); admin access is required to mutate.
 */
export interface SourceConfig {
  id: string
  code: string                        // Machine-readable ID, e.g. "SEC_EDGAR"
  name: string                        // Human-readable, e.g. "SEC EDGAR"
  description: string | null
  provider_type: ProviderType
  country_code: string | null         // ISO 3166-1 alpha-2, null = global
  base_url: string | null
  rate_limit_per_minute: number
  is_active: boolean
  config: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface SourceConfigListResponse {
  items: SourceConfig[]
  total: number
  page: number
  page_size: number
  pages: number
}

// ---------------------------------------------------------------------------
// Financials — M5.7
// ---------------------------------------------------------------------------

/**
 * A single extracted financial data point stored in financial_line_items.
 * Monetary fields are serialised as strings to preserve NUMERIC(26,2) precision.
 */
export interface FinancialLineItem {
  id: string
  company_id: string
  fiscal_year: number
  fiscal_period: string
  reporting_standard: string
  filing_date: string        // ISO 8601 date (YYYY-MM-DD)
  is_restated: boolean
  canonical_field: string    // XBRL concept tag, e.g. "us-gaap:NetIncomeLoss"
  statement_type: string     // "IS" | "BS" | "CF"
  value_usd: string | null   // NUMERIC(26,2) as string
  value_reported: string | null
  reported_currency: string | null  // ISO 4217
  fx_rate_used: string | null       // NUMERIC(38,10) as string
  source_file_hash: string | null   // SHA-256 hex digest
  extraction_method: string | null  // "ai" | "xbrl" | "pdf" | "ocr"
  derived_expression_formula: string | null
  created_at: string
  updated_at: string
}

export interface FinancialsListResponse {
  items: FinancialLineItem[]
  total: number
  offset: number
  limit: number
}

// ---------------------------------------------------------------------------
// Analytics — M7.1
// ---------------------------------------------------------------------------

/**
 * One fiscal period's headline financial metrics.
 * Mirrors TrendDataPoint from apps/api/routers/analytics.py.
 * All monetary fields are floats; null means no data extracted (not zero).
 */
export interface TrendDataPoint {
  period: string           // Human-readable label, e.g. "FY 2024", "Q3 2023"
  fiscal_year: number
  fiscal_period: string    // Q1 | Q2 | Q3 | Q4 | H1 | H2 | FY
  currency: string         // ISO 4217, e.g. "USD"
  revenue: number | null
  gross_profit: number | null
  net_income: number | null
  operating_cash_flow: number | null
}

export interface CompanyTrendsResponse {
  company_id: string
  target_currency: string
  periods_covered: number
  data: TrendDataPoint[]   // Chronologically ordered, oldest first
}

// ---------------------------------------------------------------------------
// Filings — M2.5F
// ---------------------------------------------------------------------------

/**
 * A single SEC EDGAR corporate filing record.
 * Mirrors FilingRead from apps/api/schemas/filings.py.
 */
export interface Filing {
  id: string
  company_id: string | null
  source_config_id: string | null
  filing_type: string          // "10-K" | "10-Q" | "8-K" | ...
  accession_number: string     // "0000320193-23-000077"
  filing_date: string          // ISO 8601 date
  period_end_date: string | null
  cik: string                  // 10-digit zero-padded
  ticker: string | null
  title: string | null
  filing_url: string | null
  document_url: string | null
  status: string               // "discovered" | "downloaded" | "processed" | ...
  fiscal_year: number | null
  fiscal_period: string | null // "FY" | "Q1" | "Q2" | "Q3" | "Q4"
  filing_metadata: Record<string, unknown> | null
  created_at: string
  updated_at: string
}

export interface FilingListResponse {
  items: Filing[]
  total: number
  page: number
  page_size: number
  pages: number
}

// ---------------------------------------------------------------------------
// Acquisition Jobs — M3.6
// ---------------------------------------------------------------------------

/**
 * A single SEC filing acquisition job dispatched to the Celery worker queue.
 * Mirrors AcquisitionJobRead from apps/api/schemas/acquisition_jobs.py.
 */
export interface AcquisitionJob {
  id: string
  ticker: string
  cik: string | null
  company_name: string | null
  job_type: string
  /** Lifecycle status: pending | queued | running | completed | failed | cancelled */
  status: string
  error_message: string | null
  filings_discovered: number
  filings_new: number
  documents_fetched: number
  documents_stored: number
  started_at: string | null
  completed_at: string | null
  created_at: string
  updated_at: string
}

export interface AcquisitionJobListResponse {
  items: AcquisitionJob[]
  total: number
  page: number
  page_size: number
  pages: number
}

// ---------------------------------------------------------------------------
// Validation — M4.4F
// ---------------------------------------------------------------------------

/** One rule check result from the dual-dimension validation engine. */
export interface ValidationFinding {
  rule_id: string           // "VAL-001" | "XST-002" | ...
  severity: string          // "CRITICAL" | "WARNING" | "INFO"
  message: string
  expected: number | null
  actual: number | null
  delta: number | null
}

/** One confidence-score deduction entry (Amendment V1.2 §1.8). */
export interface ValidationDeduction {
  rule_id: string
  points: number
  reason: string
}

/**
 * Full validation audit record for one extraction run.
 * Mirrors ValidationResultResponse from apps/api/routers/jobs.py.
 */
export interface ValidationResult {
  id: string
  job_id: string | null
  accession_number: string
  company_id: string | null
  fiscal_year: number | null
  fiscal_period: string | null
  items_validated: number
  is_exportable: boolean
  critical_count: number
  warning_count: number
  confidence_score: number    // [0, 100]
  findings: ValidationFinding[]
  deductions: ValidationDeduction[]
  summary_text: string | null
  created_at: string
}

// ---------------------------------------------------------------------------
// Company Resolver — M2.6F (mirrors CompanyResolveResponse from companies.py)
// ---------------------------------------------------------------------------

/**
 * Resolved canonical company identification data from the SEC EDGAR resolver.
 * Mirrors CompanyResolveResponse from apps/api/schemas/companies.py.
 * Returned by GET /api/v1/companies/resolve?ticker={ticker}.
 */
export interface CompanyResolveResponse {
  ticker: string          // Normalised uppercase ticker symbol, e.g. "AAPL"
  company_name: string    // Full legal company name, e.g. "Apple Inc."
  cik: string             // SEC CIK — 10-digit zero-padded, e.g. "0000320193"
  exchange: string | null // Primary listing exchange, e.g. "Nasdaq", or null
  country: string | null  // ISO 3166-1 alpha-2 country code, or null
}

// ---------------------------------------------------------------------------
// Async Excel Export — D2/B4/B5/F6
// ---------------------------------------------------------------------------

/** Lifecycle state of an asynchronous Excel export job. Mirrors ExcelExportStatus enum. */
export type ExcelExportStatus = 'PENDING' | 'GENERATING' | 'SUCCESS' | 'FAILED'

/**
 * Returned immediately by POST /api/v1/jobs/{job_id}/export/async.
 * The export_job_id is then passed to the status polling endpoint.
 */
export interface AsyncExportTriggerResponse {
  export_job_id: string
  status: ExcelExportStatus
  message: string
  job_id: string
  queue: string
}

/**
 * Returned by GET /api/v1/jobs/export/{export_job_id}/status.
 * download_url is populated only when status === 'SUCCESS'.
 * error_message is populated only when status === 'FAILED'.
 */
export interface ExportStatusResponse {
  id: string
  job_id: string
  tenant_id: string
  status: ExcelExportStatus
  download_url: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

// ---------------------------------------------------------------------------
// API error envelope
// ---------------------------------------------------------------------------

export interface ApiErrorDetail {
  code: string
  message: string
  details: Record<string, unknown>
  request_id: string
}

export interface ApiErrorResponse {
  error: ApiErrorDetail
}
