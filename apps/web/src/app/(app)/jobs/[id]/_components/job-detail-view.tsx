'use client'

/**
 * JobDetailView — M4.3: Client-side polling engine + full job detail UI.
 *
 * Receives the server-rendered initial job record and re-renders reactively
 * as the backend advances the job through its lifecycle.
 *
 * ─────────────────────────────────────────────────────────────────
 * Polling engine design
 * ─────────────────────────────────────────────────────────────────
 *
 * A single setInterval (POLL_INTERVAL_MS = 4 s) is started in useEffect
 * on mount, but ONLY when the initial status is non-terminal.
 *
 * Each tick calls GET /api/v1/jobs/{id}/status — the lightweight endpoint
 * that returns status + timestamps without the full job payload.  On each
 * successful response:
 *   • The local job state is updated (status, timestamps, error_message,
 *     computed is_terminal and is_cancellable).
 *   • If the returned status is terminal, clearInterval() is called and
 *     the polling indicator is hidden.
 *
 * Network failure handling:
 *   • Individual failures are counted; the interval is NOT cleared.
 *   • After MAX_FAILURES (3) consecutive failures a soft warning banner
 *     appears.  On the next success the counter resets and the banner hides.
 *   • This means a brief network blip silently retries; only a sustained
 *     outage surfaces feedback.
 *
 * Cleanup:
 *   • The useEffect cleanup function always calls clearInterval and sets a
 *     `cancelled` flag that prevents any in-flight fetch from writing state
 *     after the component unmounts (React StrictMode safe).
 *
 * Export reveal:
 *   • The Excel Export card is conditionally rendered on `job.status === 'completed'`.
 *   • Because that field lives in local React state, it appears automatically
 *     the moment the polling engine receives a "completed" response — no
 *     page reload required.
 *
 * ─────────────────────────────────────────────────────────────────
 * Milestone: M4.3 — Client-Side Polling Engine for Real-Time Job Status Updates
 */

import { useState, useEffect, useRef } from 'react'
import Link from 'next/link'
import { apiGet } from '@/lib/api'
import type { Job, JobStatusPoll } from '@/lib/types'
import { Card, JobStatusBadge } from '@/app/_components/ui'
import { UploadDocument } from './upload-document'
import { CancelJobButton } from './cancel-job-button'
import { ExportButton } from './export-button'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Poll the status endpoint every 4 seconds (within the 3–5 s spec window). */
const POLL_INTERVAL_MS = 4_000

/**
 * How many consecutive API failures to absorb silently before surfacing
 * the network-warning banner.
 */
const MAX_FAILURES = 3

/**
 * Statuses in which the job is still progressing — polling must run.
 * Must mirror the backend JobStatus enum non-terminal values.
 */
const ACTIVE_STATUSES = new Set<string>(['pending', 'queued', 'running'])

/**
 * Terminal statuses — polling must stop and will not restart.
 * Must mirror the backend JobStatus enum terminal values.
 */
const TERMINAL_STATUSES = new Set<string>(['completed', 'failed', 'cancelled'])

/**
 * Statuses in which the user is allowed to cancel the job.
 * Mirrors JobRepository.is_cancellable logic on the backend.
 */
const CANCELLABLE_STATUSES = new Set<string>(['pending', 'queued', 'running'])

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface JobDetailViewProps {
  initialJob: Job
}

export function JobDetailView({ initialJob }: JobDetailViewProps) {
  // ── State ──────────────────────────────────────────────────────────────
  const [job, setJob] = useState<Job>(initialJob)
  const [isPolling, setIsPolling] = useState(false)
  const [networkWarning, setNetworkWarning] = useState(false)

  // Refs readable inside the async tick callback without stale closures.
  const jobIdRef = useRef(initialJob.id)
  const failCountRef = useRef(0)

  // ── Polling engine ─────────────────────────────────────────────────────
  useEffect(() => {
    // If the server already rendered a terminal status, there is nothing to
    // poll — the job is done and will never change again.
    if (TERMINAL_STATUSES.has(initialJob.status)) return

    // `cancelled` is set to true by the cleanup function.  Any async tick
    // that arrives after unmount checks this flag before updating state.
    let cancelled = false

    // intervalId is captured in closure so tick() can clear it on terminal.
    let intervalId: ReturnType<typeof setInterval>

    async function tick() {
      if (cancelled) return

      try {
        const data = await apiGet<JobStatusPoll>(
          `/api/v1/jobs/${jobIdRef.current}/status`,
        )

        // Guard: component may have unmounted during the await
        if (cancelled) return

        // Reset failure counter and hide any network warning
        failCountRef.current = 0
        setNetworkWarning(false)

        // Merge the polled fields into the full job state.
        // We recompute is_terminal and is_cancellable from the new status
        // so the UI reacts instantly without waiting for a full job refetch.
        setJob((prev) => ({
          ...prev,
          status: data.status,
          started_at: data.started_at,
          completed_at: data.completed_at,
          error_message: data.error_message,
          is_terminal: TERMINAL_STATUSES.has(data.status),
          is_cancellable: CANCELLABLE_STATUSES.has(data.status),
        }))

        // ── Terminal state reached: stop the loop ──────────────────────
        if (TERMINAL_STATUSES.has(data.status)) {
          clearInterval(intervalId)
          if (!cancelled) setIsPolling(false)
        }
      } catch {
        // Individual failure — count it; keep the interval running so we
        // retry automatically on the next tick.
        if (cancelled) return
        failCountRef.current += 1
        if (failCountRef.current >= MAX_FAILURES) {
          setNetworkWarning(true)
        }
      }
    }

    setIsPolling(true)
    // The first tick fires after one full interval (not immediately) so the
    // SSR-rendered data is visible for at least POLL_INTERVAL_MS before the
    // first network call.
    intervalId = setInterval(tick, POLL_INTERVAL_MS)

    // ── Cleanup ────────────────────────────────────────────────────────
    return () => {
      cancelled = true
      clearInterval(intervalId)
      setIsPolling(false)
    }
    // Empty dep array: the effect runs exactly once on mount.
    // initialJob.status is read synchronously before the interval starts;
    // jobIdRef provides stable access to the job ID without re-running.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // ── Derived display flags ──────────────────────────────────────────────
  // Use local job state (not initialJob) so flags respond to polled updates.
  const liveIsTerminal = TERMINAL_STATUSES.has(job.status)
  const liveIsNonTerminal = ACTIVE_STATUSES.has(job.status)
  const liveIsCancellable = CANCELLABLE_STATUSES.has(job.status)

  // ── Render ─────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-6 max-w-2xl">
      {/* ── Page header ─────────────────────────────────────────────────── */}
      <div>
        <Link
          href={`/companies/${job.company_id}`}
          className="text-sm text-zinc-500 hover:text-zinc-700 transition-colors"
        >
          ← Company
        </Link>

        <div className="mt-2 flex items-center gap-3 flex-wrap">
          <h1 className="text-2xl font-bold text-zinc-900 font-mono">
            {job.job_type}
          </h1>

          {/* Live status badge — updates without page reload */}
          <JobStatusBadge status={job.status} />

          {/* Polling indicator — shown while the loop is active */}
          {isPolling && (
            <span
              className="flex items-center gap-1.5 text-xs text-zinc-400"
              aria-live="polite"
              aria-label="Monitoring job status"
            >
              <span
                className="h-1.5 w-1.5 rounded-full bg-blue-400 animate-pulse"
                aria-hidden="true"
              />
              Monitoring
            </span>
          )}
        </div>
      </div>

      {/* ── Network warning banner ──────────────────────────────────────── */}
      {networkWarning && (
        <div
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-xs text-amber-700"
        >
          <svg
            className="h-3.5 w-3.5 shrink-0"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z"
              clipRule="evenodd"
            />
          </svg>
          Connection unstable — status updates may be delayed. Retrying automatically…
        </div>
      )}

      {/* ── Status card ─────────────────────────────────────────────────── */}
      <Card className="p-6">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-base font-semibold text-zinc-900">Status</h2>

          {/* Subtle "Live" tag inside the card while polling is active */}
          {liveIsNonTerminal && (
            <span className="flex items-center gap-1.5 text-[11px] font-medium text-zinc-400 uppercase tracking-wide">
              <span
                className="h-1.5 w-1.5 rounded-full bg-blue-400 animate-pulse"
                aria-hidden="true"
              />
              Live
            </span>
          )}
        </div>

        <dl className="grid grid-cols-2 gap-4 text-sm">
          <Detail
            label="Status"
            value={<JobStatusBadge status={job.status} />}
          />
          <Detail label="Fiscal year" value={job.fiscal_year ?? '—'} />
          <Detail
            label="Started"
            value={
              job.started_at
                ? new Date(job.started_at).toLocaleString()
                : '—'
            }
          />
          <Detail
            label="Completed"
            value={
              job.completed_at
                ? new Date(job.completed_at).toLocaleString()
                : '—'
            }
          />
          {job.error_message && (
            <div className="col-span-2">
              <dt className="text-zinc-500">Error</dt>
              <dd className="mt-0.5 rounded bg-red-50 px-3 py-2 font-mono text-xs text-red-700 break-all">
                {job.error_message}
              </dd>
            </div>
          )}
        </dl>
      </Card>

      {/* ── Source document upload ───────────────────────────────────────── */}
      {/* Shown while the job is active (pre-terminal).  Hidden on completion
          so the page cleans up and only shows the export card. */}
      {!liveIsTerminal && (
        <Card className="p-6">
          <h2 className="text-base font-semibold text-zinc-900 mb-2">
            Source document
          </h2>
          <p className="text-sm text-zinc-500 mb-4">
            Upload the source document (PDF, HTML, XBRL) for extraction.
          </p>
          <UploadDocument
            jobId={job.id}
            currentDocumentUrl={job.document_url}
          />
        </Card>
      )}

      {/* ── Result URL ──────────────────────────────────────────────────── */}
      {job.result_url && (
        <Card className="p-6">
          <h2 className="text-base font-semibold text-zinc-900 mb-2">
            Result
          </h2>
          <a
            href={job.result_url}
            className="text-sm text-blue-600 hover:underline font-mono"
          >
            {job.result_url}
          </a>
        </Card>
      )}

      {/* ── Excel export ────────────────────────────────────────────────── */}
      {/* This card is conditionally rendered on job.status === 'completed'.
          Because job.status lives in local React state that the polling engine
          updates, this card appears automatically the moment the backend
          signals completion — no manual page reload required. */}
      {job.status === 'completed' && (
        <Card className="p-6">
          <h2 className="text-base font-semibold text-zinc-900 mb-1">
            Excel Export
          </h2>
          <p className="text-sm text-zinc-500 mb-4">
            Download a fully styled multi-period workbook containing Income
            Statement, Balance Sheet, Cash Flow, and Audit Log sheets with
            dual-currency (reported + USD) columns.
          </p>
          <ExportButton jobId={job.id} />
        </Card>
      )}

      {/* ── Cancel action ───────────────────────────────────────────────── */}
      {/* Mirrors liveIsCancellable so the button disappears the instant
          the polling engine receives a terminal status. */}
      {liveIsCancellable && (
        <div className="flex gap-3">
          <CancelJobButton jobId={job.id} />
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Detail helper (same as original page.tsx)
// ---------------------------------------------------------------------------

function Detail({
  label,
  value,
}: {
  label: string
  value: React.ReactNode
}) {
  return (
    <div>
      <dt className="text-zinc-500 text-sm">{label}</dt>
      <dd className="mt-0.5 font-medium text-zinc-900">{value}</dd>
    </div>
  )
}
