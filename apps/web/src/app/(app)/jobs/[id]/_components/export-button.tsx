'use client'

/**
 * ExportButton — F6: Asynchronous Excel Export UX.
 *
 * Milestone: D2/B4/B5/F6 — Async Excel Export Pipeline
 *
 * ─────────────────────────────────────────────────────────────────
 * Async export lifecycle
 * ─────────────────────────────────────────────────────────────────
 *
 * 1. USER CLICKS "Export to Excel"
 *    → POST /api/v1/jobs/{jobId}/export/async
 *    → Receives { export_job_id, status: 'PENDING' }
 *    → Button transitions to disabled "Generating Workbook…" with spinner
 *
 * 2. POLLING LOOP (every POLL_INTERVAL_MS = 2 500 ms)
 *    → GET /api/v1/jobs/export/{export_job_id}/status
 *    → Renders animated status messages to keep the UX alive:
 *        PENDING    → "Queuing export…"
 *        GENERATING → "Building workbook…"
 *
 * 3. STATUS = SUCCESS
 *    → clearInterval; button morphs to a green "Download Excel Report"
 *      anchor (<a href={download_url} download>) — no auth header needed
 *      because the URL is a pre-signed S3 GET link.
 *    → Auto-click the anchor so the browser opens the Save dialog
 *      immediately without requiring a second click.
 *
 * 4. STATUS = FAILED
 *    → clearInterval; show crimson error banner with the diagnostic
 *      message from the worker.
 *
 * ─────────────────────────────────────────────────────────────────
 * Network failure handling
 * ─────────────────────────────────────────────────────────────────
 * Individual poll failures are counted; after MAX_POLL_FAILURES
 * consecutive failures the loop stops and an amber "connection lost"
 * banner is shown with a manual Retry button.
 *
 * ─────────────────────────────────────────────────────────────────
 * Legacy sync export
 * ─────────────────────────────────────────────────────────────────
 * The original synchronous GET /api/v1/jobs/{jobId}/export endpoint
 * remains available and is preserved intact in export.py.  This
 * component replaces the client-side trigger only; the sync endpoint
 * can still be called directly for programmatic / CI use.
 */

import { useState, useEffect, useRef, useCallback } from 'react'
import { apiGet, apiPost } from '@/lib/api'
import { Button } from '@/app/_components/ui'
import type {
  AsyncExportTriggerResponse,
  ExportStatusResponse,
  ExcelExportStatus,
} from '@/lib/types'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Poll the status endpoint every 2.5 seconds (within the 2–3 s spec window). */
const POLL_INTERVAL_MS = 2_500

/**
 * Maximum consecutive poll failures before we stop looping and show the
 * "connection lost" banner.
 */
const MAX_POLL_FAILURES = 4

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function statusMessage(status: ExcelExportStatus): string {
  switch (status) {
    case 'PENDING':
      return 'Queuing export…'
    case 'GENERATING':
      return 'Building workbook…'
    case 'SUCCESS':
      return 'Ready to download'
    case 'FAILED':
      return 'Export failed'
  }
}

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------

interface ExportButtonProps {
  jobId: string
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function ExportButton({ jobId }: ExportButtonProps) {
  // ── State ────────────────────────────────────────────────────────────────
  /** null = idle, 'triggering' = POST in flight, 'polling' = waiting for worker */
  const [phase, setPhase] = useState<'idle' | 'triggering' | 'polling' | 'success' | 'failed'>(
    'idle',
  )
  const [exportStatus, setExportStatus] = useState<ExcelExportStatus | null>(null)
  const [downloadUrl, setDownloadUrl] = useState<string | null>(null)
  const [errorMessage, setErrorMessage] = useState<string | null>(null)
  const [connectionLost, setConnectionLost] = useState(false)

  // Refs for stable access inside async callbacks without stale closures
  const exportJobIdRef = useRef<string | null>(null)
  const failCountRef = useRef(0)
  const downloadAnchorRef = useRef<HTMLAnchorElement | null>(null)

  // ── Polling loop ────────────────────────────────────────────────────────
  const startPolling = useCallback((exportJobId: string) => {
    exportJobIdRef.current = exportJobId
    failCountRef.current = 0
    setConnectionLost(false)
    setPhase('polling')

    let cancelled = false
    let intervalId: ReturnType<typeof setInterval>

    async function tick() {
      if (cancelled || !exportJobIdRef.current) return

      try {
        const data = await apiGet<ExportStatusResponse>(
          `/api/v1/jobs/export/${exportJobIdRef.current}/status`,
        )

        if (cancelled) return

        // Reset failure counter
        failCountRef.current = 0
        setConnectionLost(false)
        setExportStatus(data.status)

        if (data.status === 'SUCCESS') {
          clearInterval(intervalId)
          setDownloadUrl(data.download_url)
          setPhase('success')
          // Auto-trigger browser download — eliminates the second click
          if (data.download_url) {
            const anchor = document.createElement('a')
            anchor.href = data.download_url
            anchor.download = ''
            anchor.style.display = 'none'
            document.body.appendChild(anchor)
            anchor.click()
            anchor.remove()
          }
          return
        }

        if (data.status === 'FAILED') {
          clearInterval(intervalId)
          setErrorMessage(
            data.error_message ??
              'The export worker encountered an unexpected error. Please try again.',
          )
          setPhase('failed')
          return
        }

        // PENDING or GENERATING — keep polling
      } catch {
        if (cancelled) return
        failCountRef.current += 1
        if (failCountRef.current >= MAX_POLL_FAILURES) {
          clearInterval(intervalId)
          setConnectionLost(true)
        }
      }
    }

    intervalId = setInterval(tick, POLL_INTERVAL_MS)

    // Cleanup function returned to useEffect
    return () => {
      cancelled = true
      clearInterval(intervalId)
    }
  }, [])

  // ── Trigger handler ─────────────────────────────────────────────────────
  async function handleTrigger() {
    if (phase !== 'idle' && phase !== 'failed') return

    setPhase('triggering')
    setErrorMessage(null)
    setDownloadUrl(null)
    setExportStatus(null)
    setConnectionLost(false)

    try {
      const data = await apiPost<AsyncExportTriggerResponse>(
        `/api/v1/jobs/${jobId}/export/async`,
        {},
      )
      setExportStatus(data.status)
      startPolling(data.export_job_id)
    } catch (e: unknown) {
      const err = e as { response?: { data?: { error?: { message?: string } } }; message?: string }
      const msg =
        err.response?.data?.error?.message ??
        err.message ??
        'Failed to start export. Please try again.'
      setErrorMessage(msg)
      setPhase('failed')
    }
  }

  // ── Manual retry (after connection lost) ───────────────────────────────
  function handleRetry() {
    if (!exportJobIdRef.current) {
      // Restart from the trigger step
      setPhase('idle')
      return
    }
    setConnectionLost(false)
    failCountRef.current = 0
    startPolling(exportJobIdRef.current)
  }

  // ── Idle / failed state ─────────────────────────────────────────────────
  if (phase === 'idle' || (phase === 'failed' && !errorMessage)) {
    return (
      <Button variant="secondary" size="sm" onClick={handleTrigger}>
        <svg
          xmlns="http://www.w3.org/2000/svg"
          className="h-4 w-4"
          viewBox="0 0 20 20"
          fill="currentColor"
          aria-hidden="true"
        >
          <path
            fillRule="evenodd"
            d="M3 17a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm3.293-7.707a1 1 0 011.414 0L9 10.586V3a1 1 0 112 0v7.586l1.293-1.293a1 1 0 111.414 1.414l-3 3a1 1 0 01-1.414 0l-3-3a1 1 0 010-1.414z"
            clipRule="evenodd"
          />
        </svg>
        Export to Excel
      </Button>
    )
  }

  // ── Triggering state ────────────────────────────────────────────────────
  if (phase === 'triggering') {
    return (
      <Button variant="secondary" size="sm" disabled loading>
        Queuing export…
      </Button>
    )
  }

  // ── Polling / generating state ──────────────────────────────────────────
  if (phase === 'polling') {
    return (
      <div className="flex flex-col gap-2">
        <button
          disabled
          className="inline-flex items-center gap-2 rounded-lg border border-zinc-200 bg-zinc-50 px-4 py-2 text-sm font-medium text-zinc-500 cursor-not-allowed"
          aria-busy="true"
        >
          {/* Spinner */}
          <svg
            className="h-4 w-4 animate-spin text-zinc-400"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
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
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
            />
          </svg>
          {exportStatus ? statusMessage(exportStatus) : 'Generating Workbook…'}
        </button>

        {/* Connection lost banner */}
        {connectionLost && (
          <div className="flex items-center gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
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
            <span>Connection lost. The export may still be running.</span>
            <button
              onClick={handleRetry}
              className="ml-auto font-medium text-amber-800 underline underline-offset-2 hover:no-underline"
            >
              Resume polling
            </button>
          </div>
        )}
      </div>
    )
  }

  // ── Success state ───────────────────────────────────────────────────────
  if (phase === 'success' && downloadUrl) {
    return (
      <div className="flex flex-col gap-3">
        {/* Green download button */}
        <a
          ref={downloadAnchorRef}
          href={downloadUrl}
          download
          className="inline-flex items-center gap-2 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-2 text-sm font-semibold text-emerald-700 hover:bg-emerald-100 transition-colors"
        >
          {/* Download check icon */}
          <svg
            className="h-4 w-4"
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M3 17a1 1 0 011-1h12a1 1 0 110 2H4a1 1 0 01-1-1zm3.293-7.707a1 1 0 011.414 0L9 10.586V3a1 1 0 112 0v7.586l1.293-1.293a1 1 0 111.414 1.414l-3 3a1 1 0 01-1.414 0l-3-3a1 1 0 010-1.414z"
              clipRule="evenodd"
            />
          </svg>
          Download Excel Report
        </a>

        {/* Subtle re-export option */}
        <button
          onClick={() => {
            setPhase('idle')
            setDownloadUrl(null)
            setExportStatus(null)
            exportJobIdRef.current = null
          }}
          className="text-xs text-zinc-400 hover:text-zinc-600 underline underline-offset-2 transition-colors self-start"
        >
          Re-generate export
        </button>
      </div>
    )
  }

  // ── Failed state ────────────────────────────────────────────────────────
  if (phase === 'failed') {
    return (
      <div className="flex flex-col gap-3">
        {/* Error banner */}
        <div
          role="alert"
          className="flex items-start gap-2.5 rounded-lg border border-red-200 bg-red-50 px-4 py-3"
        >
          <svg
            className="mt-0.5 h-4 w-4 shrink-0 text-red-500"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M10 18a8 8 0 100-16 8 8 0 000 16zM8.28 7.22a.75.75 0 00-1.06 1.06L8.94 10l-1.72 1.72a.75.75 0 101.06 1.06L10 11.06l1.72 1.72a.75.75 0 101.06-1.06L11.06 10l1.72-1.72a.75.75 0 00-1.06-1.06L10 8.94 8.28 7.22z"
              clipRule="evenodd"
            />
          </svg>
          <div className="flex-1 min-w-0">
            <p className="text-sm font-medium text-red-800">Export Failed</p>
            {errorMessage && (
              <p className="mt-1 text-xs text-red-600 font-mono break-words">
                {errorMessage.length > 300
                  ? errorMessage.slice(0, 300) + '…'
                  : errorMessage}
              </p>
            )}
          </div>
        </div>

        {/* Retry trigger */}
        <Button
          variant="secondary"
          size="sm"
          onClick={handleTrigger}
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            className="h-4 w-4"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={2}
            aria-hidden="true"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
            />
          </svg>
          Retry Export
        </Button>
      </div>
    )
  }

  // Fallback (unreachable in practice)
  return null
}
