/**
 * Job detail page — M4.3 refactor.
 *
 * Server Component (thin shell).
 *
 * Responsibility:
 *   1. Fetch the full job record at SSR time so the page renders with content
 *      on first load (no client spinner, no layout shift, good for bookmarks).
 *   2. Pass the initial job data to <JobDetailView>, which is a Client Component
 *      that owns ALL dynamic UI: live status badge, polling loop, export reveal,
 *      cancel button, and upload flow.
 *
 * Why split at this boundary?
 *   - Server Component: can use `await serverGet()` (reads httpOnly cookie auth),
 *     drives generateMetadata, handles 404 via notFound().
 *   - Client Component: can use useEffect, setInterval, and React state — which
 *     are needed for the polling engine added in M4.3.
 *
 * Milestone: M4.3 — Client-Side Polling Engine for Real-Time Job Status Updates
 */

import type { Metadata } from 'next'
import { notFound } from 'next/navigation'
import { serverGet } from '@/lib/server-api'
import type { Job } from '@/lib/types'
import { JobDetailView } from './_components/job-detail-view'

export async function generateMetadata(
  props: PageProps<'/jobs/[id]'>,
): Promise<Metadata> {
  const { id } = await props.params
  try {
    const job = await serverGet<Job>(`/api/v1/jobs/${id}`)
    return { title: `Job — ${job.job_type}` }
  } catch {
    return { title: 'Job' }
  }
}

export default async function JobDetailPage(props: PageProps<'/jobs/[id]'>) {
  const { id } = await props.params

  let job: Job | null = null

  try {
    job = await serverGet<Job>(`/api/v1/jobs/${id}`)
  } catch (e) {
    const err = e as { statusCode?: number }
    if (err.statusCode === 404) notFound()
    // Non-404 error: fall through to the null check below.
  }

  if (!job) {
    return (
      <div className="text-center py-12 text-zinc-500">Failed to load job.</div>
    )
  }

  // Delegate all rendering and interactive behaviour to the Client Component.
  return <JobDetailView initialJob={job} />
}
