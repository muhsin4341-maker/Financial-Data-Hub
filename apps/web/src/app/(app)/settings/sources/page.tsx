/**
 * Source Registry Settings page — M3.5.
 *
 * Server Component.  Fetches the complete source registry from the backend
 * at render time (no client round-trip on initial load) and passes the list
 * to the SourcesTable client component, which owns all interactive state:
 * per-row toggle switches, optimistic UI updates, and skeleton loading.
 *
 * Route: /settings/sources
 *
 * Milestone: M3.5 — External Source Registry Management Dashboard
 */

import type { Metadata } from 'next'
import { serverGet } from '@/lib/server-api'
import type { SourceConfigListResponse } from '@/lib/types'
import { SourcesTable } from './_components/sources-table'

export const metadata: Metadata = { title: 'Source Registry' }

export default async function SourceRegistryPage() {
  let data: SourceConfigListResponse | null = null
  let fetchError: string | null = null

  try {
    // Fetch up to 100 sources in one call (platform-level list — unlikely to
    // exceed this in practice; add pagination if the registry grows large).
    data = await serverGet<SourceConfigListResponse>(
      '/api/v1/sources?page_size=100',
    )
  } catch {
    fetchError = 'Could not connect to the source registry. Please refresh.'
  }

  return (
    <div className="flex flex-col gap-8 max-w-4xl">
      {/* ── Page header ─────────────────────────────────────────────────── */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-zinc-900">Source Registry</h1>
          <p className="mt-1 text-sm text-zinc-500 max-w-xl">
            Manage external data acquisition pipelines. Toggle each source to
            control which connectors the extraction engine uses when processing
            new jobs. Changes take effect immediately for all users.
          </p>
        </div>

        {/* Stats summary — only when data loaded */}
        {data && (
          <div className="shrink-0 ml-6 flex flex-col items-end gap-0.5">
            <span className="text-2xl font-bold text-zinc-900 tabular-nums">
              {data.total}
            </span>
            <span className="text-xs text-zinc-400 uppercase tracking-wide">
              {data.total === 1 ? 'source' : 'sources'}
            </span>
            <span className="text-xs text-emerald-600 font-medium">
              {data.items.filter((s) => s.is_active).length} active
            </span>
          </div>
        )}
      </div>

      {/* ── Fetch error ──────────────────────────────────────────────────── */}
      {fetchError && (
        <div
          role="alert"
          className="rounded-lg border border-red-300 bg-red-50 px-4 py-3 text-sm text-red-700"
        >
          {fetchError}
        </div>
      )}

      {/* ── Interactive table (client component) ─────────────────────────── */}
      <SourcesTable initialItems={data?.items ?? null} />
    </div>
  )
}
