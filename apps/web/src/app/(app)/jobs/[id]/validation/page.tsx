/**
 * Job Validation Audit Page — M4.4F.
 *
 * Server Component (thin shell).
 *
 * Responsibilities:
 *   1. Resolve the job record from the UUID route param (SSR).
 *   2. SSR-fetch the validation result for this job so the dashboard
 *      hydrates with real data on first paint — zero client round-trips.
 *   3. Render breadcrumb + context header consistent with the job detail
 *      hierarchy.
 *   4. Pass job + initialData to <ValidationDashboard> (Client Component).
 *
 * Route: /jobs/[id]/validation
 * Milestone: M4.4F — Interactive Parsing Validation & QA Dashboard
 */

import type { Metadata } from 'next'
import Link from 'next/link'
import { notFound } from 'next/navigation'
import { serverGet } from '@/lib/server-api'
import type { Job, ValidationResult } from '@/lib/types'
import { ValidationDashboard } from './_components/validation-dashboard'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// Local interface — mirrors the pattern used by financials/page.tsx and
// analytics/page.tsx to avoid requiring a global AppRoutes entry.
interface ValidationPageProps {
  params: Promise<{ id: string }>
}

// ---------------------------------------------------------------------------
// Metadata
// ---------------------------------------------------------------------------

export async function generateMetadata(
  props: ValidationPageProps,
): Promise<Metadata> {
  const { id } = await props.params
  try {
    const job = await serverGet<Job>(`/api/v1/jobs/${id}`)
    return { title: `Validation — ${job.job_type ?? id}` }
  } catch {
    return { title: 'Validation Audit' }
  }
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function JobValidationPage(props: ValidationPageProps) {
  const { id } = await props.params

  // ── 1. Resolve job record ─────────────────────────────────────────────────
  let job: Job | null = null
  try {
    job = await serverGet<Job>(`/api/v1/jobs/${id}`)
  } catch (e) {
    const err = e as { statusCode?: number }
    if (err.statusCode === 404) notFound()
    // Non-404: fall through to graceful error state below.
  }

  // ── 2. Attempt SSR fetch of validation result ─────────────────────────────
  let initialValidation: ValidationResult | null = null
  let ssrError: string | null = null
  if (job) {
    try {
      initialValidation = await serverGet<ValidationResult>(
        `/api/v1/jobs/${id}/validation`,
      )
    } catch (e) {
      const err = e as { statusCode?: number; apiCode?: string }
      if (err.statusCode === 404 || err.apiCode === 'VALIDATION_NOT_FOUND') {
        // Job exists but no validation record yet — non-fatal, dashboard shows empty state.
        ssrError = 'VALIDATION_NOT_FOUND'
      } else {
        ssrError = 'FETCH_ERROR'
      }
    }
  }

  // ── Graceful error fallback (non-404 job fetch failure) ───────────────────
  if (!job) {
    return (
      <div className="py-12 text-center text-zinc-500">
        Failed to load job data. Please refresh the page or try again later.
      </div>
    )
  }

  // ── Derived display values ─────────────────────────────────────────────────
  const jobLabel = job.job_type
    ? job.job_type.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
    : `Job ${id.slice(0, 8)}…`

  return (
    <div className="flex flex-col gap-6">
      {/* ── Breadcrumb ──────────────────────────────────────────────────────── */}
      <div>
        <nav
          className="flex items-center gap-1.5 text-sm text-zinc-500"
          aria-label="Breadcrumb"
        >
          <Link
            href="/jobs"
            className="hover:text-zinc-700 transition-colors"
          >
            Jobs
          </Link>
          <span aria-hidden="true">›</span>
          <Link
            href={`/jobs/${id}`}
            className="hover:text-zinc-700 transition-colors"
          >
            {jobLabel}
          </Link>
          <span aria-hidden="true">›</span>
          <span className="text-zinc-800 font-medium">Validation Audit</span>
        </nav>

        <div className="mt-3 flex items-center justify-between gap-4 flex-wrap">
          <div>
            <h1 className="text-2xl font-bold text-zinc-900">
              Parsing Validation &amp; QA
            </h1>
            <p className="mt-1 text-sm text-zinc-500 max-w-2xl">
              Automated quality-assurance diagnostics for{' '}
              <strong className="font-medium text-zinc-700">{jobLabel}</strong>.
              Review extraction confidence, rule-level findings, and export
              eligibility before pushing data downstream.
            </p>
          </div>

          {/* Back to job detail link */}
          <Link
            href={`/jobs/${id}`}
            className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-200 bg-white px-3 py-1.5 text-sm font-medium text-zinc-600 hover:bg-zinc-50 transition-colors shrink-0"
          >
            <svg
              className="w-4 h-4"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth={1.75}
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M10.5 19.5L3 12m0 0l7.5-7.5M3 12h18"
              />
            </svg>
            Back to Job
          </Link>
        </div>

        {/* Job metadata strip */}
        <div className="mt-3 flex flex-wrap items-center gap-3">
          <span className="inline-flex items-center gap-1.5 rounded-full bg-zinc-100 px-3 py-1 text-xs font-medium text-zinc-600">
            <span className="font-mono text-zinc-400">#</span>
            {id.slice(0, 8)}…
          </span>
          {job.fiscal_year && (
            <span className="inline-flex items-center rounded-full bg-blue-50 px-3 py-1 text-xs font-medium text-blue-700">
              FY {job.fiscal_year}
            </span>
          )}
          <span
            className={[
              'inline-flex items-center rounded-full px-3 py-1 text-xs font-semibold',
              job.status === 'completed'
                ? 'bg-emerald-50 text-emerald-700'
                : job.status === 'failed'
                ? 'bg-red-50 text-red-700'
                : job.status === 'running'
                ? 'bg-amber-50 text-amber-700'
                : 'bg-zinc-100 text-zinc-600',
            ].join(' ')}
          >
            {job.status.charAt(0).toUpperCase() + job.status.slice(1)}
          </span>
        </div>
      </div>

      {/* ── Divider ─────────────────────────────────────────────────────────── */}
      <div className="border-t border-zinc-100" />

      {/* ── Interactive validation dashboard (Client Component) ─────────────── */}
      <ValidationDashboard
        job={job}
        initialData={initialValidation}
        ssrError={ssrError}
      />
    </div>
  )
}
