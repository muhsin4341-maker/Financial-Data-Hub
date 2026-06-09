/**
 * Company Financial Ledger page — M5.7.
 *
 * Server Component.  Fetches the company record and the first page of
 * financial line items server-side (zero client round-trips on initial load),
 * then delegates all filter interaction to the FinancialsGrid client component.
 *
 * Route: /companies/[id]/financials
 *
 * Milestone: M5.7 — Financial Line-Item Data Ledger & UI Viewer
 */

import type { Metadata } from 'next'
import Link from 'next/link'
import { notFound } from 'next/navigation'
import { serverGet } from '@/lib/server-api'
import type { Company, FinancialsListResponse } from '@/lib/types'
import { FinancialsGrid } from './_components/financials-grid'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface FinancialsPageProps {
  params: Promise<{ id: string }>
}

// ---------------------------------------------------------------------------
// Metadata
// ---------------------------------------------------------------------------

export async function generateMetadata(
  props: FinancialsPageProps,
): Promise<Metadata> {
  const { id } = await props.params
  try {
    const company = await serverGet<Company>(`/api/v1/companies/${id}`)
    return { title: `${company.name} — Financial Ledger` }
  } catch {
    return { title: 'Financial Ledger' }
  }
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function CompanyFinancialsPage(props: FinancialsPageProps) {
  const { id } = await props.params

  // ── Fetch company ────────────────────────────────────────────────────────
  let company: Company | null = null

  try {
    company = await serverGet<Company>(`/api/v1/companies/${id}`)
  } catch (e) {
    const err = e as { statusCode?: number }
    if (err.statusCode === 404) notFound()
    // Non-404 error: render a graceful fallback below.
  }

  // ── Fetch initial financial data (first page, no filters) ────────────────
  let initialData: FinancialsListResponse = {
    items: [],
    total: 0,
    offset: 0,
    limit: 50,
  }

  if (company) {
    try {
      initialData = await serverGet<FinancialsListResponse>(
        `/api/v1/companies/${id}/financials?limit=50&offset=0`,
      )
    } catch {
      // Non-fatal: the grid will render an empty state and allow retries.
    }
  }

  // ── Error fallback ───────────────────────────────────────────────────────
  if (!company) {
    return (
      <div className="text-center py-12 text-zinc-500">
        Failed to load company data. Please refresh or try again later.
      </div>
    )
  }

  // ── Render ───────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-6">
      {/* ── Breadcrumb + page title ─────────────────────────────────────── */}
      <div>
        <nav className="flex items-center gap-1.5 text-sm text-zinc-500" aria-label="Breadcrumb">
          <Link href="/companies" className="hover:text-zinc-700 transition-colors">
            Companies
          </Link>
          <span aria-hidden="true">›</span>
          <Link
            href={`/companies/${id}`}
            className="hover:text-zinc-700 transition-colors"
          >
            {company.name}
          </Link>
          <span aria-hidden="true">›</span>
          <span className="text-zinc-800 font-medium">Financial Ledger</span>
        </nav>

        <div className="mt-3 flex items-baseline gap-3">
          <h1 className="text-2xl font-bold text-zinc-900">Financial Ledger</h1>
          <span className="font-mono text-sm text-zinc-400">{company.ticker}</span>
        </div>

        <p className="mt-1 text-sm text-zinc-500 max-w-2xl">
          All extracted financial line items for <strong className="font-medium text-zinc-700">{company.name}</strong>,
          with original reported values and FX-normalised USD equivalents from the M5 translation pipeline.
          Use the filters below to slice by year, period, or statement type.
        </p>
      </div>

      {/* ── Tab-style link back to company overview ─────────────────────── */}
      <div className="flex gap-1 border-b border-zinc-200 -mb-2">
        <Link
          href={`/companies/${id}`}
          className="px-4 py-2 text-sm font-medium text-zinc-500 hover:text-zinc-700 transition-colors"
        >
          Overview
        </Link>
        <span className="px-4 py-2 text-sm font-medium text-blue-600 border-b-2 border-blue-600 -mb-px">
          Financial Ledger
        </span>
      </div>

      {/* ── Interactive data grid (client component) ────────────────────── */}
      <FinancialsGrid companyId={id} initialData={initialData} />
    </div>
  )
}
