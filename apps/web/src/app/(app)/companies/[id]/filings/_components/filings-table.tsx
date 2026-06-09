'use client'

/**
 * FilingsTable — M2.5F Interactive Corporate Filing Browser.
 *
 * Client Component.  Owns:
 *   - Client-side filter state (All / 10-K / 10-Q)
 *   - React Query refetch for client-side retry
 *   - Skeleton shimmer on initial load
 *   - Empty state with refresh handler
 *   - Enterprise filing data grid
 *
 * Props are seeded from the Server Component SSR fetch (initialData) so the
 * table is fully populated on first paint with zero client round-trips.
 *
 * Route: /companies/[id]/filings
 * Milestone: M2.5F — Interactive Corporate Filing Browser
 */

import { useState, useMemo } from 'react'
import Link from 'next/link'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import type { Filing, FilingListResponse } from '@/lib/types'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface FilingsTableProps {
  companyId: string
  ticker: string
  initialData: FilingListResponse | null
}

type FilterType = 'all' | '10-K' | '10-Q'

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

const FILTER_LABELS: { value: FilterType; label: string }[] = [
  { value: 'all',  label: 'All Filings' },
  { value: '10-K', label: '10-K Annuals' },
  { value: '10-Q', label: '10-Q Quarterlies' },
]

/** Map filing_type string to a colour variant. */
function filingTypeBadge(type: string): { bg: string; text: string } {
  switch (type.toUpperCase()) {
    case '10-K':
      return { bg: 'bg-blue-50',   text: 'text-blue-700' }
    case '10-Q':
      return { bg: 'bg-violet-50', text: 'text-violet-700' }
    case '8-K':
      return { bg: 'bg-amber-50',  text: 'text-amber-700' }
    case 'S-1':
    case 'S-11':
      return { bg: 'bg-emerald-50', text: 'text-emerald-700' }
    default:
      return { bg: 'bg-zinc-100',  text: 'text-zinc-600' }
  }
}

/** "0000320193-23-000077" → "0000320193-23-000077" (already readable) */
function formatAccession(accession: string): string {
  return accession
}

/** ISO date string → "Nov 3, 2023" */
function formatDate(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleDateString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
    })
  } catch {
    return iso
  }
}

/** Build a compact fiscal context label, e.g. "2024 • Q3" or "2023 • FY" */
function fiscalLabel(year: number | null, period: string | null): string | null {
  if (!year && !period) return null
  if (year && period) return `${year} • ${period}`
  if (year) return `${year}`
  return period ?? null
}

/** SEC EDGAR viewer URL from accession number */
function edgarViewerUrl(accessionNumber: string, cik: string): string {
  const stripped = accessionNumber.replace(/-/g, '')
  return `https://www.sec.gov/Archives/edgar/data/${parseInt(cik, 10)}/${stripped}/`
}

// ---------------------------------------------------------------------------
// Skeleton row
// ---------------------------------------------------------------------------

function SkeletonRow() {
  return (
    <tr className="border-b border-zinc-100">
      {[20, 32, 44, 32, 36].map((w, i) => (
        <td key={i} className="px-4 py-3">
          <div
            className={`h-4 rounded bg-zinc-200 animate-pulse`}
            style={{ width: `${w}%` }}
          />
        </td>
      ))}
      <td className="px-4 py-3">
        <div className="flex gap-2">
          <div className="h-4 w-14 rounded bg-zinc-200 animate-pulse" />
          <div className="h-4 w-14 rounded bg-zinc-200 animate-pulse" />
        </div>
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Filing row
// ---------------------------------------------------------------------------

interface FilingRowProps {
  filing: Filing
  companyId: string
}

function FilingRow({ filing, companyId }: FilingRowProps) {
  const badge = filingTypeBadge(filing.filing_type)
  const fiscal = fiscalLabel(filing.fiscal_year, filing.fiscal_period)
  const edgarUrl = edgarViewerUrl(filing.accession_number, filing.cik)

  return (
    <tr className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50 transition-colors group">
      {/* Form Type Badge */}
      <td className="px-4 py-3 whitespace-nowrap">
        <span
          className={`inline-flex items-center rounded px-2 py-0.5 text-xs font-semibold tracking-wide ${badge.bg} ${badge.text}`}
        >
          {filing.filing_type}
        </span>
      </td>

      {/* Fiscal Context */}
      <td className="px-4 py-3">
        {fiscal ? (
          <div className="flex flex-col gap-0.5">
            <span className="text-sm font-medium text-zinc-800">{fiscal}</span>
            {filing.period_end_date && (
              <span className="text-xs text-zinc-400">
                Period end {formatDate(filing.period_end_date)}
              </span>
            )}
          </div>
        ) : (
          <span className="text-sm text-zinc-400">—</span>
        )}
      </td>

      {/* Title */}
      <td className="px-4 py-3 max-w-xs">
        {filing.title ? (
          <span className="text-sm text-zinc-700 line-clamp-2 leading-snug">
            {filing.title}
          </span>
        ) : (
          <span className="text-sm text-zinc-400">—</span>
        )}
      </td>

      {/* Accession Number */}
      <td className="px-4 py-3">
        <span className="font-mono text-xs text-zinc-500 select-all">
          {formatAccession(filing.accession_number)}
        </span>
      </td>

      {/* Filing Date */}
      <td className="px-4 py-3 whitespace-nowrap">
        <span className="text-sm text-zinc-600">{formatDate(filing.filing_date)}</span>
      </td>

      {/* Status */}
      <td className="px-4 py-3 whitespace-nowrap">
        <StatusPip status={filing.status} />
      </td>

      {/* Actions */}
      <td className="px-4 py-3 whitespace-nowrap">
        <div className="flex items-center gap-2 opacity-60 group-hover:opacity-100 transition-opacity">
          {/* Open raw source document */}
          {(filing.document_url || filing.filing_url) && (
            <a
              href={filing.document_url ?? filing.filing_url ?? edgarUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium text-zinc-700 bg-zinc-100 hover:bg-zinc-200 transition-colors"
              title="Open raw filing document"
            >
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
              Source
            </a>
          )}
          {/* EDGAR viewer fallback */}
          {!filing.document_url && !filing.filing_url && (
            <a
              href={edgarUrl}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium text-zinc-700 bg-zinc-100 hover:bg-zinc-200 transition-colors"
              title="View on SEC EDGAR"
            >
              <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
              </svg>
              EDGAR
            </a>
          )}
          {/* Jump to job extraction profile */}
          <Link
            href={`/companies/${companyId}/financials?accession=${filing.accession_number}`}
            className="inline-flex items-center gap-1 rounded px-2.5 py-1 text-xs font-medium text-blue-700 bg-blue-50 hover:bg-blue-100 transition-colors"
            title="View extracted financials for this filing"
          >
            <svg className="w-3 h-3" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
            </svg>
            Ledger
          </Link>
        </div>
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Status pip
// ---------------------------------------------------------------------------

function StatusPip({ status }: { status: string }) {
  const map: Record<string, { dot: string; label: string }> = {
    discovered: { dot: 'bg-zinc-400',     label: 'Discovered' },
    downloading: { dot: 'bg-amber-400 animate-pulse', label: 'Downloading' },
    downloaded:  { dot: 'bg-sky-500',     label: 'Downloaded' },
    processing:  { dot: 'bg-blue-400 animate-pulse',  label: 'Processing' },
    processed:   { dot: 'bg-emerald-500', label: 'Processed' },
    failed:      { dot: 'bg-red-500',     label: 'Failed' },
    skipped:     { dot: 'bg-zinc-300',    label: 'Skipped' },
  }
  const cfg = map[status.toLowerCase()] ?? { dot: 'bg-zinc-400', label: status }
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={`inline-block w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
      <span className="text-xs text-zinc-500 capitalize">{cfg.label}</span>
    </span>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function FilingsTable({ companyId, ticker, initialData }: FilingsTableProps) {
  const [filter, setFilter] = useState<FilterType>('all')

  // React Query — seeded from SSR initialData; client refetch on demand
  const {
    data,
    isLoading,
    isFetching,
    isError,
    refetch,
  } = useQuery<FilingListResponse>({
    queryKey: ['company-filings', companyId, ticker],
    queryFn: () =>
      apiGet<FilingListResponse>(
        `/api/v1/companies/${ticker}/filings?page_size=100`,
      ),
    initialData: initialData ?? undefined,
    staleTime: 2 * 60 * 1000,   // 2 minutes
    retry: 2,
  })

  // Client-side filter — no re-fetch needed
  const filteredItems: Filing[] = useMemo(() => {
    const items = data?.items ?? []
    if (filter === 'all') return items
    return items.filter(
      (f) => f.filing_type.toUpperCase() === filter.toUpperCase(),
    )
  }, [data?.items, filter])

  const showSkeleton = isLoading || (isFetching && !data)

  // ── Error state ────────────────────────────────────────────────────────────
  if (isError && !data) {
    return (
      <div className="rounded-xl border border-red-200 bg-red-50 p-6 text-center">
        <p className="text-sm text-red-700 font-medium mb-3">
          Could not load filing index. The API may be temporarily unavailable.
        </p>
        <button
          onClick={() => refetch()}
          className="rounded-lg px-4 py-2 text-sm font-medium bg-red-600 text-white hover:bg-red-700 transition-colors"
        >
          Retry
        </button>
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-4">
      {/* ── Filter bar ────────────────────────────────────────────────────── */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        <div className="flex items-center gap-1 rounded-lg bg-zinc-100 p-1">
          {FILTER_LABELS.map(({ value, label }) => (
            <button
              key={value}
              onClick={() => setFilter(value)}
              className={[
                'rounded-md px-3 py-1.5 text-sm font-medium transition-all',
                filter === value
                  ? 'bg-white text-zinc-900 shadow-sm'
                  : 'text-zinc-500 hover:text-zinc-700',
              ].join(' ')}
            >
              {label}
              {value !== 'all' && data && (
                <span className="ml-1.5 text-xs text-zinc-400">
                  ({data.items.filter(
                    (f) => f.filing_type.toUpperCase() === value.toUpperCase(),
                  ).length})
                </span>
              )}
              {value === 'all' && data && (
                <span className="ml-1.5 text-xs text-zinc-400">
                  ({data.total})
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Refresh button */}
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm font-medium text-zinc-600 bg-white border border-zinc-200 hover:bg-zinc-50 transition-colors disabled:opacity-50"
          title="Refresh filing index"
        >
          <svg
            className={['w-3.5 h-3.5', isFetching ? 'animate-spin' : ''].join(' ')}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          {isFetching ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {/* ── Table ─────────────────────────────────────────────────────────── */}
      <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-100 bg-zinc-50">
              <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider whitespace-nowrap">Form</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider whitespace-nowrap">Fiscal Period</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider">Title</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider whitespace-nowrap">Accession #</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider whitespace-nowrap">Filed</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider">Status</th>
              <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider">Actions</th>
            </tr>
          </thead>
          <tbody>
            {showSkeleton
              ? Array.from({ length: 6 }).map((_, i) => <SkeletonRow key={i} />)
              : filteredItems.length === 0
              ? (
                <tr>
                  <td colSpan={7} className="px-4 py-16 text-center">
                    <EmptyState filter={filter} onRefresh={() => refetch()} />
                  </td>
                </tr>
              )
              : filteredItems.map((filing) => (
                <FilingRow
                  key={filing.id}
                  filing={filing}
                  companyId={companyId}
                />
              ))
            }
          </tbody>
        </table>
      </div>

      {/* ── Footer count ──────────────────────────────────────────────────── */}
      {!showSkeleton && filteredItems.length > 0 && (
        <p className="text-xs text-zinc-400 text-right">
          Showing {filteredItems.length}
          {filter !== 'all' ? ` ${filter}` : ''} filing
          {filteredItems.length !== 1 ? 's' : ''}
          {data && data.total > filteredItems.length && filter === 'all'
            ? ` of ${data.total} total`
            : ''}
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Empty state
// ---------------------------------------------------------------------------

function EmptyState({
  filter,
  onRefresh,
}: {
  filter: FilterType
  onRefresh: () => void
}) {
  return (
    <div className="flex flex-col items-center gap-4 py-4">
      {/* Filing cabinet SVG icon */}
      <svg
        className="w-12 h-12 text-zinc-300"
        fill="none" viewBox="0 0 48 48" stroke="currentColor" strokeWidth={1.5}
      >
        <rect x="8" y="6" width="32" height="36" rx="3" strokeLinecap="round" strokeLinejoin="round" />
        <line x1="8" y1="20" x2="40" y2="20" strokeLinecap="round" />
        <line x1="8" y1="34" x2="40" y2="34" strokeLinecap="round" />
        <line x1="20" y1="14" x2="28" y2="14" strokeLinecap="round" />
        <line x1="20" y1="27" x2="28" y2="27" strokeLinecap="round" />
      </svg>
      <div className="text-center">
        <p className="text-sm font-medium text-zinc-600">
          {filter === 'all'
            ? 'No Corporate Filings Indexed'
            : `No ${filter} Filings Found`}
        </p>
        <p className="mt-1 text-xs text-zinc-400 max-w-xs">
          {filter === 'all'
            ? 'SEC EDGAR filings for this company have not been acquired yet. Run an acquisition job to populate the filing index.'
            : `Try switching to "All Filings" to see other form types, or run an acquisition job to discover new filings.`}
        </p>
      </div>
      <button
        onClick={onRefresh}
        className="mt-1 rounded-lg px-4 py-2 text-sm font-medium text-zinc-700 bg-zinc-100 hover:bg-zinc-200 transition-colors"
      >
        Refresh Index
      </button>
    </div>
  )
}
