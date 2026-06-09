/**
 * Acquisition Pipeline page — M3.6.
 *
 * Server Component (thin shell).
 *
 * Responsibility:
 *   1. Fetch the first page of acquisition jobs at SSR time so the page renders
 *      with real content on first load (no client-side spinner on the initial
 *      paint, SEO-friendly, bookmark-safe).
 *   2. Pass the initial job list to <AcquisitionJobsTable>, which is a Client
 *      Component that owns all interactive state: retry buttons, inline error
 *      expansion, optimistic status updates, and skeleton loading.
 *
 * Route: /acquisition/jobs
 *
 * Milestone: M3.6 — Data Acquisition Scraper Logs & Job Control Center
 */

import type { Metadata } from 'next'
import { serverGet } from '@/lib/server-api'
import type { AcquisitionJobListResponse } from '@/lib/types'
import { AcquisitionJobsTable } from './_components/acquisition-jobs-table'

export const metadata: Metadata = { title: 'Acquisition Pipeline' }

export default async function AcquisitionJobsPage() {
  let data: AcquisitionJobListResponse | null = null
  let fetchError: string | null = null

  try {
    // Fetch the 50 most-recent acquisition jobs for the initial view.
    // The table component supports client-side re-fetching for pagination.
    data = await serverGet<AcquisitionJobListResponse>(
      '/api/v1/acquisition/jobs?page_size=50&page=1',
    )
  } catch {
    fetchError =
      'Could not connect to the acquisition pipeline service. Please refresh.'
  }

  return (
    <div className="flex flex-col gap-8 max-w-5xl">
      {/* ── Page header ─────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-zinc-900">
            Acquisition Pipeline
          </h1>
          <p className="mt-1 text-sm text-zinc-500 max-w-xl">
            Monitor SEC filing scraper jobs, review execution history, and retry
            failed acquisitions. Each job represents one Celery worker run for a
            single ticker symbol.
          </p>
        </div>

        {/* Summary stats — only rendered when data is available */}
        {data && (
          <div className="shrink-0 ml-6 flex flex-col items-end gap-0.5">
            <span className="text-2xl font-bold text-zinc-900 tabular-nums">
              {data.total}
            </span>
            <span className="text-xs text-zinc-400 uppercase tracking-wide">
              {data.total === 1 ? 'job' : 'jobs'}
            </span>
            <span className="text-xs font-medium text-red-500">
              {data.items.filter((j) => j.status === 'failed').length} failed
            </span>
          </div>
        )}
      </div>

      {/* ── Fetch error banner ───────────────────────────────────────────── */}
      {fetchError && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700"
        >
          {fetchError}
        </div>
      )}

      {/* ── Interactive jobs table (client component) ─────────────────────── */}
      <AcquisitionJobsTable initialItems={data?.items ?? null} />
    </div>
  )
}
