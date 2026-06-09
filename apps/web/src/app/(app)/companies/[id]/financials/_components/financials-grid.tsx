'use client'

/**
 * FinancialsGrid — M5.7: Interactive financial line-item data grid.
 *
 * Client Component.  Receives server-rendered initial data and manages
 * client-side state for filter changes and pagination, re-fetching via
 * the shared axios API client (which attaches the JWT token automatically).
 *
 * Architecture:
 *   - Filters: fiscal year (free number input), period (select), statement
 *     type (select), and a "show restated" toggle.  Any filter change resets
 *     the page offset to 0.
 *   - Pagination: offset-based, PAGE_LIMIT rows per page.  Prev/Next buttons
 *     with "Page N of M" indicator.
 *   - Loading: inline spinner next to the row count; table stays visible
 *     (skeleton-free) to avoid layout shift during filter changes.
 *   - Concept prettification: strips XBRL namespace prefix, splits camelCase
 *     to words.  Full canonical tag shown as a monospace subtitle and tooltip.
 *   - Money formatting: compact notation (B/M/K suffix) with sign colouring
 *     (negative → red, USD positive → emerald, reported positive → zinc-900).
 *
 * Milestone: M5.7 — Financial Line-Item Data Ledger & UI Viewer
 */

import { useState, useCallback, useEffect, useRef } from 'react'
import { clsx } from 'clsx'
import { apiGet } from '@/lib/api'
import type { FinancialLineItem, FinancialsListResponse } from '@/lib/types'
import { Badge, Card } from '@/app/_components/ui'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PAGE_LIMIT = 50

// ---------------------------------------------------------------------------
// Formatting utilities
// ---------------------------------------------------------------------------

/**
 * Strip XBRL namespace prefix and split camelCase into readable words.
 * "us-gaap:NetIncomeLoss"  → "Net Income Loss"
 * "ifrs-full:GrossProfit"  → "Gross Profit"
 * "net_income"             → "Net Income"
 */
function formatConcept(canonical: string): string {
  const tag = canonical.includes(':')
    ? canonical.substring(canonical.indexOf(':') + 1)
    : canonical
  return tag
    .replace(/_/g, ' ')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .replace(/([a-z\d])([A-Z])/g, '$1 $2')
    .replace(/\s{2,}/g, ' ')
    .trim()
}

/** Extract just the namespace prefix, e.g. "us-gaap" from "us-gaap:Revenues". */
function formatNamespace(canonical: string): string {
  return canonical.includes(':') ? canonical.split(':')[0] : ''
}

/**
 * Format a monetary string value in compact notation with sign colouring.
 * Returns "—" for null.  Precision examples:
 *   394328000000  →  "USD 394.33B"
 *   -82959000000  →  "USD -82.96B"
 *   1234567       →  "USD 1.23M"
 */
function formatMoney(value: string | null, currency: string | null): string {
  if (value === null || value === undefined) return '—'
  const num = parseFloat(value)
  if (isNaN(num)) return value
  const abs = Math.abs(num)
  const sign = num < 0 ? '-' : ''
  const sym = currency ? `${currency} ` : ''
  if (abs >= 1e12) return `${sign}${sym}${(abs / 1e12).toFixed(2)}T`
  if (abs >= 1e9)  return `${sign}${sym}${(abs / 1e9).toFixed(2)}B`
  if (abs >= 1e6)  return `${sign}${sym}${(abs / 1e6).toFixed(2)}M`
  if (abs >= 1e3)  return `${sign}${sym}${(abs / 1e3).toFixed(2)}K`
  return `${sign}${sym}${num.toFixed(2)}`
}

/** Format an FX rate to 6 decimal places, or "—" for null. */
function formatFxRate(rate: string | null): string {
  if (rate === null || rate === undefined) return '—'
  const num = parseFloat(rate)
  return isNaN(num) ? rate : num.toFixed(6)
}

/** Determine if a monetary string is negative (for colour coding). */
function isNegative(value: string | null): boolean {
  if (!value) return false
  return parseFloat(value) < 0
}

// ---------------------------------------------------------------------------
// Statement type metadata
// ---------------------------------------------------------------------------

type BadgeVariant = 'info' | 'default' | 'success' | 'warning' | 'danger'

const STATEMENT_META: Record<string, { label: string; variant: BadgeVariant }> = {
  IS: { label: 'Income Stmt', variant: 'info' },
  BS: { label: 'Balance Sheet', variant: 'default' },
  CF: { label: 'Cash Flow', variant: 'success' },
}

const METHOD_LABELS: Record<string, string> = {
  ai:   'AI',
  xbrl: 'XBRL',
  pdf:  'PDF',
  ocr:  'OCR',
}

// ---------------------------------------------------------------------------
// Filter bar sub-component
// ---------------------------------------------------------------------------

interface FilterState {
  fiscalYear: string          // "" = no filter; string to allow partial typing
  fiscalPeriod: string        // "" | "Q1" | "Q2" | "Q3" | "Q4" | "FY"
  statementType: string       // "" | "IS" | "BS" | "CF"
  includeRestated: boolean
}

interface FilterBarProps {
  filters: FilterState
  loading: boolean
  onChange: (next: Partial<FilterState>) => void
}

function FilterBar({ filters, loading, onChange }: FilterBarProps) {
  const selectClass =
    'rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50'

  return (
    <div className="flex flex-wrap items-end gap-4">
      {/* Fiscal year */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-zinc-600" htmlFor="filter-year">
          Fiscal Year
        </label>
        <input
          id="filter-year"
          type="number"
          placeholder="e.g. 2024"
          min={1900}
          max={2100}
          value={filters.fiscalYear}
          disabled={loading}
          onChange={(e) => onChange({ fiscalYear: e.target.value })}
          className="w-28 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 disabled:opacity-50 [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
        />
      </div>

      {/* Fiscal period */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-zinc-600" htmlFor="filter-period">
          Period
        </label>
        <select
          id="filter-period"
          value={filters.fiscalPeriod}
          disabled={loading}
          onChange={(e) => onChange({ fiscalPeriod: e.target.value })}
          className={selectClass}
        >
          <option value="">All periods</option>
          <option value="FY">FY — Full Year</option>
          <option value="Q1">Q1</option>
          <option value="Q2">Q2</option>
          <option value="Q3">Q3</option>
          <option value="Q4">Q4</option>
        </select>
      </div>

      {/* Statement type */}
      <div className="flex flex-col gap-1">
        <label className="text-xs font-medium text-zinc-600" htmlFor="filter-statement">
          Statement
        </label>
        <select
          id="filter-statement"
          value={filters.statementType}
          disabled={loading}
          onChange={(e) => onChange({ statementType: e.target.value })}
          className={selectClass}
        >
          <option value="">All statements</option>
          <option value="IS">IS — Income Statement</option>
          <option value="BS">BS — Balance Sheet</option>
          <option value="CF">CF — Cash Flow</option>
        </select>
      </div>

      {/* Include restated toggle */}
      <div className="flex items-center gap-2 pb-1.5">
        <input
          id="filter-restated"
          type="checkbox"
          checked={filters.includeRestated}
          disabled={loading}
          onChange={(e) => onChange({ includeRestated: e.target.checked })}
          className="h-4 w-4 rounded border-zinc-300 text-blue-600 focus:ring-blue-500 disabled:opacity-50 cursor-pointer"
        />
        <label
          htmlFor="filter-restated"
          className="text-sm text-zinc-600 cursor-pointer select-none"
        >
          Show restated
        </label>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Table row sub-component
// ---------------------------------------------------------------------------

function FinancialRow({ item }: { item: FinancialLineItem }) {
  const stmtMeta = STATEMENT_META[item.statement_type] ?? {
    label: item.statement_type,
    variant: 'default' as BadgeVariant,
  }

  const periodLabel =
    item.fiscal_period === 'FY'
      ? `FY ${item.fiscal_year}`
      : `${item.fiscal_period} ${item.fiscal_year}`

  return (
    <tr className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50/70 transition-colors">
      {/* Concept */}
      <td className="px-4 py-3 max-w-0">
        <div
          className="font-medium text-zinc-900 truncate"
          title={item.canonical_field}
        >
          {formatConcept(item.canonical_field)}
        </div>
        <div
          className="mt-0.5 text-[11px] font-mono text-zinc-400 truncate"
          title={item.canonical_field}
        >
          {formatNamespace(item.canonical_field)}
        </div>
        {item.is_restated && (
          <span className="mt-1 inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold tracking-wide bg-amber-100 text-amber-700">
            RESTATED
          </span>
        )}
      </td>

      {/* Statement type */}
      <td className="px-4 py-3 whitespace-nowrap">
        <Badge variant={stmtMeta.variant}>{stmtMeta.label}</Badge>
      </td>

      {/* Period */}
      <td className="px-4 py-3 whitespace-nowrap">
        <span className="font-mono text-xs text-zinc-700">{periodLabel}</span>
      </td>

      {/* Filing date */}
      <td className="px-4 py-3 whitespace-nowrap">
        <span className="text-xs text-zinc-500">
          {new Date(item.filing_date).toLocaleDateString('en-US', {
            year: 'numeric',
            month: 'short',
            day: 'numeric',
          })}
        </span>
      </td>

      {/* Reported value */}
      <td className="px-4 py-3 text-right whitespace-nowrap">
        <span
          className={clsx(
            'font-mono text-xs tabular-nums',
            item.value_reported === null
              ? 'text-zinc-300'
              : isNegative(item.value_reported)
              ? 'text-red-600'
              : 'text-zinc-900',
          )}
          title={item.value_reported ?? undefined}
        >
          {formatMoney(item.value_reported, item.reported_currency)}
        </span>
      </td>

      {/* USD value */}
      <td className="px-4 py-3 text-right whitespace-nowrap">
        <span
          className={clsx(
            'font-mono text-xs tabular-nums',
            item.value_usd === null
              ? 'text-zinc-300'
              : isNegative(item.value_usd)
              ? 'text-red-600'
              : 'text-emerald-700',
          )}
          title={item.value_usd ?? undefined}
        >
          {formatMoney(item.value_usd, 'USD')}
        </span>
      </td>

      {/* FX rate */}
      <td className="px-4 py-3 text-right whitespace-nowrap">
        <span
          className="font-mono text-[11px] text-zinc-400"
          title={item.fx_rate_used ?? undefined}
        >
          {formatFxRate(item.fx_rate_used)}
        </span>
      </td>

      {/* Extraction method */}
      <td className="px-4 py-3 whitespace-nowrap">
        {item.extraction_method ? (
          <Badge variant="default">
            {METHOD_LABELS[item.extraction_method] ??
              item.extraction_method.toUpperCase()}
          </Badge>
        ) : (
          <span className="text-zinc-300 text-xs">—</span>
        )}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Main export: FinancialsGrid
// ---------------------------------------------------------------------------

export interface FinancialsGridProps {
  companyId: string
  initialData: FinancialsListResponse
}

export function FinancialsGrid({ companyId, initialData }: FinancialsGridProps) {
  const [filters, setFilters] = useState<FilterState>({
    fiscalYear: '',
    fiscalPeriod: '',
    statementType: '',
    includeRestated: false,
  })
  const [offset, setOffset] = useState(0)
  const [data, setData] = useState<FinancialsListResponse>(initialData)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  // Skip the initial client-side fetch — the server already provided initialData.
  const isFirstRender = useRef(true)

  // ── Fetch function ─────────────────────────────────────────────────────
  const fetchData = useCallback(
    async (activeFilters: FilterState, activeOffset: number) => {
      setLoading(true)
      setError(null)
      try {
        const params = new URLSearchParams()
        params.set('limit', String(PAGE_LIMIT))
        params.set('offset', String(activeOffset))

        // Only include non-empty filter values so the backend default kicks in.
        if (activeFilters.fiscalYear.trim()) {
          params.set('fiscal_year', activeFilters.fiscalYear.trim())
        }
        if (activeFilters.fiscalPeriod) {
          params.set('fiscal_period', activeFilters.fiscalPeriod)
        }
        if (activeFilters.statementType) {
          params.set('statement_type', activeFilters.statementType)
        }
        if (activeFilters.includeRestated) {
          params.set('include_restated', 'true')
        }

        const result = await apiGet<FinancialsListResponse>(
          `/api/v1/companies/${companyId}/financials?${params.toString()}`,
        )
        setData(result)
      } catch (e: unknown) {
        const err = e as { apiMessage?: string; message?: string }
        setError(err.apiMessage ?? err.message ?? 'Failed to load financial data.')
      } finally {
        setLoading(false)
      }
    },
    [companyId],
  )

  // ── Re-fetch on filter/pagination changes (skip first render) ──────────
  useEffect(() => {
    if (isFirstRender.current) {
      isFirstRender.current = false
      return
    }
    fetchData(filters, offset)
  }, [filters, offset, fetchData])

  // ── Filter change handler — resets to page 0 ──────────────────────────
  function handleFilterChange(next: Partial<FilterState>) {
    setFilters((prev) => ({ ...prev, ...next }))
    setOffset(0)
  }

  // ── Pagination helpers ─────────────────────────────────────────────────
  const { items, total } = data
  const pageStart = total === 0 ? 0 : offset + 1
  const pageEnd = Math.min(offset + PAGE_LIMIT, total)
  const totalPages = Math.ceil(total / PAGE_LIMIT)
  const currentPage = Math.floor(offset / PAGE_LIMIT) + 1
  const canPrev = offset > 0 && !loading
  const canNext = offset + PAGE_LIMIT < total && !loading

  // ── Render ─────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-4">
      {/* Filter bar */}
      <Card className="p-4">
        <FilterBar filters={filters} loading={loading} onChange={handleFilterChange} />
      </Card>

      {/* Summary / loading indicator */}
      <div className="flex items-center justify-between min-h-5">
        <span className="text-sm text-zinc-500">
          {total === 0
            ? 'No records found'
            : `Showing ${pageStart}–${pageEnd} of ${total.toLocaleString()} records`}
        </span>
        {loading && (
          <span className="flex items-center gap-1.5 text-xs text-blue-600">
            <svg
              className="animate-spin h-3.5 w-3.5"
              viewBox="0 0 24 24"
              fill="none"
              aria-hidden="true"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8v8H4z"
              />
            </svg>
            Loading…
          </span>
        )}
      </div>

      {/* Error banner */}
      {error && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700"
        >
          <strong className="font-semibold">Error:</strong> {error}
          <button
            onClick={() => fetchData(filters, offset)}
            className="ml-3 underline hover:no-underline text-red-700"
          >
            Retry
          </button>
        </div>
      )}

      {/* Empty state */}
      {!loading && items.length === 0 && !error && (
        <div className="rounded-xl border-2 border-dashed border-zinc-200 bg-white p-12 text-center">
          <svg
            className="mx-auto h-10 w-10 text-zinc-300"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.5}
              d="M9 17v-2a4 4 0 014-4h2m-6-4V5a4 4 0 014-4h2M7 20H5a2 2 0 01-2-2V6a2 2 0 012-2h2M12 20h7a2 2 0 002-2V8l-5-5h-4"
            />
          </svg>
          <p className="mt-3 text-sm font-medium text-zinc-600">
            {filters.fiscalYear || filters.fiscalPeriod || filters.statementType
              ? 'No records match the current filters'
              : 'No financial data extracted yet'}
          </p>
          <p className="mt-1 text-sm text-zinc-400">
            {filters.fiscalYear || filters.fiscalPeriod || filters.statementType
              ? 'Try adjusting or clearing the filters above.'
              : 'Complete an extraction job for this company to populate the ledger.'}
          </p>
        </div>
      )}

      {/* Data table */}
      {items.length > 0 && (
        <div className="overflow-x-auto rounded-xl border border-zinc-200 bg-white shadow-sm">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-100 bg-zinc-50 text-left">
                <th
                  scope="col"
                  className="px-4 py-3 font-medium text-zinc-600 w-56 max-w-56"
                >
                  Concept
                </th>
                <th scope="col" className="px-4 py-3 font-medium text-zinc-600 whitespace-nowrap">
                  Statement
                </th>
                <th scope="col" className="px-4 py-3 font-medium text-zinc-600 whitespace-nowrap">
                  Period
                </th>
                <th scope="col" className="px-4 py-3 font-medium text-zinc-600 whitespace-nowrap">
                  Filed
                </th>
                <th
                  scope="col"
                  className="px-4 py-3 font-medium text-zinc-600 text-right whitespace-nowrap"
                >
                  Reported Value
                </th>
                <th
                  scope="col"
                  className="px-4 py-3 font-medium text-zinc-600 text-right whitespace-nowrap"
                >
                  USD Value
                </th>
                <th
                  scope="col"
                  className="px-4 py-3 font-medium text-zinc-600 text-right whitespace-nowrap"
                >
                  FX Rate
                </th>
                <th scope="col" className="px-4 py-3 font-medium text-zinc-600 whitespace-nowrap">
                  Source
                </th>
              </tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <FinancialRow key={item.id} item={item} />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination controls */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between pt-1">
          <button
            onClick={() => setOffset(Math.max(0, offset - PAGE_LIMIT))}
            disabled={!canPrev}
            aria-label="Previous page"
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-lg border px-4 py-2 text-sm font-medium transition-colors',
              canPrev
                ? 'border-zinc-300 text-zinc-700 hover:bg-zinc-50 active:bg-zinc-100'
                : 'border-zinc-200 text-zinc-300 cursor-not-allowed',
            )}
          >
            ← Previous
          </button>

          <span className="text-sm text-zinc-500">
            Page {currentPage} of {totalPages}
          </span>

          <button
            onClick={() => setOffset(offset + PAGE_LIMIT)}
            disabled={!canNext}
            aria-label="Next page"
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-lg border px-4 py-2 text-sm font-medium transition-colors',
              canNext
                ? 'border-zinc-300 text-zinc-700 hover:bg-zinc-50 active:bg-zinc-100'
                : 'border-zinc-200 text-zinc-300 cursor-not-allowed',
            )}
          >
            Next →
          </button>
        </div>
      )}
    </div>
  )
}
