'use client'

/**
 * AcquisitionJobsTable — M3.6: Scraper Logs & Job Control Center.
 *
 * Receives the server-rendered initial job list and renders an interactive
 * data table with:
 *
 *   • Skeleton loaders  — when initialItems is null (SSR fetch failed), 6
 *     shimmer rows are shown until a client-side fetch completes.
 *   • Empty state       — illustrated placeholder when no jobs exist yet.
 *   • Status badges     — colour-coded chips for each lifecycle state.
 *   • Execution window  — shows started_at → completed_at span, or
 *     "created_at" when not yet started, relative-formatted for recency.
 *   • Progress counters — filings_discovered / filings_new / documents_fetched
 *     as a compact summary line on each row.
 *   • Inline error      — expandable monospace panel for failed jobs; toggle
 *     driven per-row without affecting siblings.
 *   • Retry button      — calls POST /api/v1/acquisition/jobs/{id}/retry for
 *     failed jobs; optimistically flips the row status to "queued"; reverts
 *     if the request fails.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * Optimistic retry design
 * ─────────────────────────────────────────────────────────────────────────
 * When the user clicks Retry on a failed job:
 *   1. The row immediately shows status="queued" (optimistic) and the Retry
 *      button is replaced by a spinner chip.
 *   2. POST /api/v1/acquisition/jobs/{id}/retry is called.
 *   3a. Success: the backend returns a new AcquisitionJob (fresh UUID for the
 *       retried job).  The existing row is updated with the new job's id,
 *       status, and cleared error_message — the table reflects the new job
 *       without a hard reload.
 *   3b. Failure: the row reverts to "failed" and the Retry button reappears
 *       with an inline error toast.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * Skeleton loader design
 * ─────────────────────────────────────────────────────────────────────────
 * `initialItems === null` signals that the server-side fetch failed and no
 * data is available.  In that state the component fires a client-side fetch
 * on mount (via useEffect) and displays skeleton shimmer rows while waiting.
 * Once the client fetch resolves (success or error), the skeleton is replaced.
 *
 * Milestone: M3.6 — Data Acquisition Scraper Logs & Job Control Center
 */

import { useState, useEffect, useCallback } from 'react'
import { clsx } from 'clsx'
import { apiGet, apiPost } from '@/lib/api'
import type { AcquisitionJob, AcquisitionJobListResponse } from '@/lib/types'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

/** Colour-coded status chip, matches the backend JobStatus enum. */
function StatusBadge({ status }: { status: string }) {
  const label = status.charAt(0).toUpperCase() + status.slice(1)

  const colours: Record<string, string> = {
    pending:   'bg-zinc-100 text-zinc-600 ring-zinc-200',
    queued:    'bg-sky-50 text-sky-700 ring-sky-200',
    running:   'bg-blue-50 text-blue-700 ring-blue-200',
    completed: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
    failed:    'bg-red-50 text-red-700 ring-red-200',
    cancelled: 'bg-zinc-100 text-zinc-500 ring-zinc-200',
  }

  return (
    <span
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full px-2 py-0.5',
        'text-xs font-medium ring-1 ring-inset',
        colours[status] ?? 'bg-zinc-100 text-zinc-600 ring-zinc-200',
      )}
    >
      {/* Pulse dot for active states */}
      {(status === 'running' || status === 'queued') && (
        <span
          className={clsx(
            'h-1.5 w-1.5 rounded-full',
            status === 'running' ? 'bg-blue-500 animate-pulse' : 'bg-sky-500',
          )}
          aria-hidden="true"
        />
      )}
      {label}
    </span>
  )
}

/** Compact relative timestamp — shows "just now", "3 m ago", "2 h ago", etc. */
function RelativeTime({ iso }: { iso: string }) {
  const ms = Date.now() - new Date(iso).getTime()
  const seconds = Math.floor(ms / 1000)

  let label: string
  if (seconds < 60)       label = 'just now'
  else if (seconds < 3600) label = `${Math.floor(seconds / 60)} m ago`
  else if (seconds < 86400) label = `${Math.floor(seconds / 3600)} h ago`
  else                     label = `${Math.floor(seconds / 86400)} d ago`

  return (
    <time
      dateTime={iso}
      title={new Date(iso).toLocaleString()}
      className="text-zinc-500"
    >
      {label}
    </time>
  )
}

/** Duration string between two ISO timestamps (or "—" when end is null). */
function duration(start: string, end: string | null): string {
  if (!end) return '—'
  const ms = new Date(end).getTime() - new Date(start).getTime()
  if (ms < 0) return '—'
  const s = Math.floor(ms / 1000)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${s % 60}s`
  return `${Math.floor(m / 60)}h ${m % 60}m`
}

/** Single shimmer skeleton row. */
function SkeletonRow() {
  return (
    <tr className="border-b border-zinc-100">
      {[40, 24, 32, 28, 20, 20].map((w, i) => (
        <td key={i} className="px-4 py-3">
          <div
            className={clsx(
              'h-4 rounded bg-zinc-200 animate-pulse',
              `w-${w}` ,
            )}
            style={{ width: `${w * 4}px` }}
          />
        </td>
      ))}
      <td className="px-4 py-3">
        <div className="h-7 w-16 rounded bg-zinc-200 animate-pulse" />
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface AcquisitionJobsTableProps {
  /** Jobs from SSR.  null means the server fetch failed — trigger a
   *  client-side fetch and show skeleton until it completes. */
  initialItems: AcquisitionJob[] | null
}

export function AcquisitionJobsTable({
  initialItems,
}: AcquisitionJobsTableProps) {
  // ── State ──────────────────────────────────────────────────────────────
  const [items, setItems] = useState<AcquisitionJob[] | null>(initialItems)
  const [clientFetchError, setClientFetchError] = useState<string | null>(null)

  /** Per-row retry loading state:  jobId → true while POST is in flight */
  const [retrying, setRetrying] = useState<Record<string, boolean>>({})

  /** Per-row retry error:  jobId → error message string */
  const [retryErrors, setRetryErrors] = useState<Record<string, string>>({})

  /** Per-row error expansion toggle:  jobId → true when expanded */
  const [expandedErrors, setExpandedErrors] = useState<Record<string, boolean>>({})

  // ── Client-side fallback fetch ─────────────────────────────────────────
  // Triggered only when SSR returned no data (initialItems === null).
  useEffect(() => {
    if (initialItems !== null) return   // SSR data is present — skip

    let cancelled = false

    async function fetchJobs() {
      try {
        const data = await apiGet<AcquisitionJobListResponse>(
          '/api/v1/acquisition/jobs?page_size=50&page=1',
        )
        if (!cancelled) setItems(data.items)
      } catch {
        if (!cancelled)
          setClientFetchError(
            'Failed to load acquisition jobs. Please refresh the page.',
          )
      }
    }

    void fetchJobs()
    return () => { cancelled = true }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Toggle inline error expansion ─────────────────────────────────────
  const toggleError = useCallback((jobId: string) => {
    setExpandedErrors((prev) => ({ ...prev, [jobId]: !prev[jobId] }))
  }, [])

  // ── Retry handler ──────────────────────────────────────────────────────
  const handleRetry = useCallback(async (jobId: string) => {
    // 1. Optimistic update — flip status to "queued" immediately
    setItems((prev) =>
      prev?.map((job) =>
        job.id === jobId
          ? { ...job, status: 'queued', error_message: null }
          : job,
      ) ?? prev,
    )
    setRetrying((prev) => ({ ...prev, [jobId]: true }))
    setRetryErrors((prev) => {
      const next = { ...prev }
      delete next[jobId]
      return next
    })

    try {
      const newJob = await apiPost<AcquisitionJob>(
        `/api/v1/acquisition/jobs/${jobId}/retry`,
        {},
      )

      // 2a. Success — replace the old row data with the new job record.
      //     The new job has a fresh UUID but we keep it in the same row
      //     position by replacing the matched entry's full data.
      setItems((prev) =>
        prev?.map((job) =>
          job.id === jobId ? { ...newJob } : job,
        ) ?? prev,
      )
    } catch (err) {
      // 2b. Failure — revert to "failed" and surface the error inline
      const msg =
        (err as { message?: string })?.message ??
        'Retry failed. Please try again.'

      setItems((prev) =>
        prev?.map((job) =>
          job.id === jobId
            ? { ...job, status: 'failed' }
            : job,
        ) ?? prev,
      )
      setRetryErrors((prev) => ({ ...prev, [jobId]: msg }))
    } finally {
      setRetrying((prev) => {
        const next = { ...prev }
        delete next[jobId]
        return next
      })
    }
  }, [])

  // ── Derived state ──────────────────────────────────────────────────────
  const isLoading = items === null && clientFetchError === null
  const isEmpty   = Array.isArray(items) && items.length === 0

  // ── Loading skeleton ───────────────────────────────────────────────────
  if (isLoading) {
    return (
      <div className="rounded-xl border border-zinc-200 overflow-hidden">
        <table className="w-full text-sm">
          <TableHead />
          <tbody>
            {Array.from({ length: 6 }).map((_, i) => (
              <SkeletonRow key={i} />
            ))}
          </tbody>
        </table>
      </div>
    )
  }

  // ── Client fetch error ─────────────────────────────────────────────────
  if (clientFetchError) {
    return (
      <div
        role="alert"
        className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700"
      >
        {clientFetchError}
      </div>
    )
  }

  // ── Empty state ────────────────────────────────────────────────────────
  if (isEmpty) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 rounded-xl border border-dashed border-zinc-300 bg-zinc-50 py-20 text-center">
        {/* Icon — abstract pipeline / data flow symbol */}
        <svg
          className="h-12 w-12 text-zinc-300"
          viewBox="0 0 48 48"
          fill="none"
          stroke="currentColor"
          strokeWidth="1.5"
          aria-hidden="true"
        >
          <rect x="4" y="10" width="12" height="8" rx="2" />
          <rect x="18" y="20" width="12" height="8" rx="2" />
          <rect x="32" y="30" width="12" height="8" rx="2" />
          <path d="M16 14h2M28 24h2M4 38h40" strokeLinecap="round" />
        </svg>

        <div>
          <p className="text-base font-semibold text-zinc-700">
            No acquisition jobs yet
          </p>
          <p className="mt-1 text-sm text-zinc-400 max-w-xs mx-auto">
            Jobs appear here when the platform starts fetching SEC filings.
            Trigger your first acquisition to populate this view.
          </p>
        </div>
      </div>
    )
  }

  // ── Populated table ────────────────────────────────────────────────────
  return (
    <div className="rounded-xl border border-zinc-200 overflow-hidden">
      <table className="w-full text-sm">
        <TableHead />
        <tbody className="divide-y divide-zinc-100">
          {(items ?? []).map((job) => {
            const isRetrying    = retrying[job.id] ?? false
            const retryError    = retryErrors[job.id] ?? null
            const errorExpanded = expandedErrors[job.id] ?? false
            const isTerminal    = TERMINAL_STATUSES.has(job.status)
            const canRetry      = job.status === 'failed'
            const execWindow    = job.started_at
              ? `${new Date(job.started_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} → ${job.completed_at ? new Date(job.completed_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : '…'}`
              : null

            return (
              <>
                <tr
                  key={job.id}
                  className={clsx(
                    'group transition-colors',
                    job.status === 'failed'
                      ? 'bg-red-50/40 hover:bg-red-50/70'
                      : 'hover:bg-zinc-50',
                  )}
                >
                  {/* ── Job ID + type ──────────────────────────────────── */}
                  <td className="px-4 py-3 font-mono text-xs text-zinc-400 whitespace-nowrap">
                    <span title={job.id}>{job.id.slice(0, 8)}…</span>
                    <div className="text-zinc-400 font-sans text-[10px] mt-0.5 uppercase tracking-wide">
                      {job.job_type.replace(/_/g, ' ')}
                    </div>
                  </td>

                  {/* ── Ticker + company name ──────────────────────────── */}
                  <td className="px-4 py-3 whitespace-nowrap">
                    <span className="font-semibold text-zinc-900">
                      {job.ticker}
                    </span>
                    {job.company_name && (
                      <div className="text-xs text-zinc-400 mt-0.5 truncate max-w-[12rem]">
                        {job.company_name}
                      </div>
                    )}
                    {job.cik && (
                      <div className="text-[10px] text-zinc-300 font-mono mt-0.5">
                        CIK {job.cik}
                      </div>
                    )}
                  </td>

                  {/* ── Execution window ──────────────────────────────── */}
                  <td className="px-4 py-3 text-xs text-zinc-500 whitespace-nowrap">
                    {execWindow ? (
                      <>
                        <div>{execWindow}</div>
                        {job.started_at && (
                          <div className="text-zinc-400 mt-0.5">
                            {duration(job.started_at, job.completed_at)}
                          </div>
                        )}
                      </>
                    ) : (
                      <RelativeTime iso={job.created_at} />
                    )}
                  </td>

                  {/* ── Status badge ───────────────────────────────────── */}
                  <td className="px-4 py-3 whitespace-nowrap">
                    <StatusBadge status={job.status} />
                  </td>

                  {/* ── Progress counters ─────────────────────────────── */}
                  <td className="px-4 py-3 text-xs text-zinc-500 whitespace-nowrap">
                    {isTerminal ? (
                      <div className="flex flex-col gap-0.5">
                        <span>
                          <span className="font-medium text-zinc-700">
                            {job.filings_discovered}
                          </span>{' '}
                          discovered
                        </span>
                        <span>
                          <span className="font-medium text-zinc-700">
                            {job.filings_new}
                          </span>{' '}
                          new
                        </span>
                        <span>
                          <span className="font-medium text-zinc-700">
                            {job.documents_fetched}
                          </span>{' '}
                          fetched
                        </span>
                      </div>
                    ) : (
                      <span className="text-zinc-300">—</span>
                    )}
                  </td>

                  {/* ── Error toggle ───────────────────────────────────── */}
                  <td className="px-4 py-3">
                    {job.error_message ? (
                      <button
                        type="button"
                        onClick={() => toggleError(job.id)}
                        className={clsx(
                          'rounded px-2 py-0.5 text-xs font-medium transition-colors',
                          errorExpanded
                            ? 'bg-red-100 text-red-700 hover:bg-red-200'
                            : 'bg-red-50 text-red-600 hover:bg-red-100',
                        )}
                        aria-expanded={errorExpanded}
                        aria-label={
                          errorExpanded ? 'Collapse error' : 'View error'
                        }
                      >
                        {errorExpanded ? 'Hide' : 'Error ↓'}
                      </button>
                    ) : (
                      <span className="text-zinc-200 text-xs">—</span>
                    )}
                  </td>

                  {/* ── Retry / status action ─────────────────────────── */}
                  <td className="px-4 py-3 text-right">
                    {isRetrying ? (
                      <span className="inline-flex items-center gap-1.5 text-xs text-zinc-400">
                        <span
                          className="h-3 w-3 rounded-full border-2 border-zinc-300 border-t-zinc-500 animate-spin"
                          aria-hidden="true"
                        />
                        Queuing…
                      </span>
                    ) : canRetry ? (
                      <button
                        type="button"
                        onClick={() => void handleRetry(job.id)}
                        className={clsx(
                          'rounded-md px-2.5 py-1.5 text-xs font-medium',
                          'border border-zinc-300 bg-white text-zinc-700',
                          'hover:bg-zinc-50 hover:border-zinc-400',
                          'transition-colors focus-visible:outline-none',
                          'focus-visible:ring-2 focus-visible:ring-blue-500',
                        )}
                      >
                        ↺ Retry
                      </button>
                    ) : null}
                  </td>
                </tr>

                {/* ── Inline error diagnostics row (expandable) ───────── */}
                {errorExpanded && job.error_message && (
                  <tr
                    key={`${job.id}-error`}
                    className="bg-red-50 border-b border-red-100"
                  >
                    <td colSpan={7} className="px-4 py-3">
                      <div className="rounded-md bg-white border border-red-200 px-3 py-2.5">
                        <p className="text-xs font-semibold text-red-600 mb-1.5">
                          Error Diagnostics
                        </p>
                        <pre className="font-mono text-xs text-red-700 whitespace-pre-wrap break-all leading-relaxed">
                          {job.error_message}
                        </pre>
                      </div>
                    </td>
                  </tr>
                )}

                {/* ── Inline retry error toast row ─────────────────────── */}
                {retryError && (
                  <tr
                    key={`${job.id}-retry-error`}
                    className="bg-amber-50 border-b border-amber-100"
                  >
                    <td colSpan={7} className="px-4 py-2">
                      <p className="text-xs text-amber-700">
                        <span className="font-semibold">Retry failed:</span>{' '}
                        {retryError}
                      </p>
                    </td>
                  </tr>
                )}
              </>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

// ---------------------------------------------------------------------------
// TableHead — extracted to avoid duplication in skeleton path
// ---------------------------------------------------------------------------

function TableHead() {
  return (
    <thead className="bg-zinc-50 border-b border-zinc-200">
      <tr>
        {[
          'Job ID / Type',
          'Ticker / Company',
          'Execution Window',
          'Status',
          'Progress',
          'Diagnostics',
          '',
        ].map((col) => (
          <th
            key={col}
            scope="col"
            className="px-4 py-2.5 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wide whitespace-nowrap"
          >
            {col}
          </th>
        ))}
      </tr>
    </thead>
  )
}
