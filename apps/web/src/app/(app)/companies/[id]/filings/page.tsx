/**
 * Company Filing Browser — M2.5F.
 *
 * Server Component (thin shell).
 *
 * Responsibilities:
 *   1. Resolve the company record from the UUID route param (→ ticker).
 *   2. SSR-fetch the full filing index for that ticker so the table
 *      hydrates with real data on first paint (zero client round-trips).
 *   3. Render breadcrumb + sub-navigation tab strip consistent with the
 *      Overview, Financial Ledger, and Analytics sibling pages.
 *   4. Pass company + initialData to <FilingsTable> (Client Component).
 *
 * Route: /companies/[id]/filings
 * Milestone: M2.5F — Interactive Corporate Filing Browser
 */

import type { Metadata } from 'next'
import Link from 'next/link'
import { notFound } from 'next/navigation'
import { serverGet } from '@/lib/server-api'
import type { Company, FilingListResponse } from '@/lib/types'
import { FilingsTable } from './_components/filings-table'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

// Local interface — avoids dependency on the auto-generated global AppRoutes
// type (same pattern as financials/page.tsx and analytics/page.tsx).
interface FilingsPageProps {
  params: Promise<{ id: string }>
}

// ---------------------------------------------------------------------------
// Metadata
// ---------------------------------------------------------------------------

export async function generateMetadata(
  props: FilingsPageProps,
): Promise<Metadata> {
  const { id } = await props.params
  try {
    const company = await serverGet<Company>(`/api/v1/companies/${id}`)
    return { title: `${company.name} — Filings` }
  } catch {
    return { title: 'Corporate Filings' }
  }
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function CompanyFilingsPage(props: FilingsPageProps) {
  const { id } = await props.params

  // ── 1. Resolve company (ticker required for the filings endpoint) ─────────
  let company: Company | null = null
  try {
    company = await serverGet<Company>(`/api/v1/companies/${id}`)
  } catch (e) {
    const err = e as { statusCode?: number }
    if (err.statusCode === 404) notFound()
    // Non-404: render graceful fallback further down.
  }

  // ── 2. Attempt SSR fetch of filing index ──────────────────────────────────
  let initialFilings: FilingListResponse | null = null
  if (company?.ticker) {
    try {
      initialFilings = await serverGet<FilingListResponse>(
        `/api/v1/companies/${company.ticker}/filings?page_size=100`,
      )
    } catch {
      // Non-fatal: FilingsTable renders an empty state with a refresh button.
    }
  }

  // ── Graceful error fallback ───────────────────────────────────────────────
  if (!company) {
    return (
      <div className="py-12 text-center text-zinc-500">
        Failed to load company data. Please refresh or try again later.
      </div>
    )
  }

  // ── 3. Sub-nav tabs (consistent with sibling pages) ───────────────────────
  const tabs = [
    { href: `/companies/${id}`,            label: 'Overview' },
    { href: `/companies/${id}/filings`,    label: 'Filings' },
    { href: `/companies/${id}/financials`, label: 'Financial Ledger' },
    { href: `/companies/${id}/analytics`,  label: '📈 Analytics' },
  ]

  const filingCount =
    initialFilings && initialFilings.total > 0
      ? ` (${initialFilings.total})`
      : ''

  return (
    <div className="flex flex-col gap-6">
      {/* ── Breadcrumb ────────────────────────────────────────────────────── */}
      <div>
        <nav
          className="flex items-center gap-1.5 text-sm text-zinc-500"
          aria-label="Breadcrumb"
        >
          <Link
            href="/companies"
            className="hover:text-zinc-700 transition-colors"
          >
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
          <span className="text-zinc-800 font-medium">Filings</span>
        </nav>

        <div className="mt-3 flex items-baseline gap-3">
          <h1 className="text-2xl font-bold text-zinc-900">
            Corporate Filings{filingCount}
          </h1>
          <span className="font-mono text-sm text-zinc-400">
            {company.ticker}
          </span>
        </div>

        <p className="mt-1 text-sm text-zinc-500 max-w-2xl">
          SEC EDGAR filing index for{' '}
          <strong className="font-medium text-zinc-700">{company.name}</strong>.
          Filter by form type and open the raw source document or jump directly
          to the extracted financial ledger for any filing.
        </p>
      </div>

      {/* ── Sub-navigation tab strip ─────────────────────────────────────── */}
      <div className="flex items-center gap-1 border-b border-zinc-200 -mb-2">
        {tabs.map(({ href, label }) => {
          const isActive = href === `/companies/${id}/filings`
          return isActive ? (
            <span
              key={href}
              className="px-4 py-2 text-sm font-medium text-blue-600 border-b-2 border-blue-600 -mb-px"
            >
              {label}
            </span>
          ) : (
            <Link
              key={href}
              href={href}
              className="px-4 py-2 text-sm font-medium text-zinc-500 hover:text-zinc-800 border-b-2 border-transparent hover:border-zinc-300 transition-colors"
            >
              {label}
            </Link>
          )
        })}
      </div>

      {/* ── Interactive filing data grid (Client Component) ──────────────── */}
      <FilingsTable
        companyId={id}
        ticker={company.ticker}
        initialData={initialFilings}
      />
    </div>
  )
}
