/**
 * Company Analytics page — M7.1F.
 *
 * Server Component (thin shell).
 *
 * Responsibility:
 *   1. Fetch company metadata for the breadcrumb and page title.
 *   2. Attempt an SSR fetch of the trends payload so the page renders with
 *      real chart data on the first paint (no client spinner for the initial load).
 *   3. Pass both to <AnalyticsView>, which is a Client Component that owns
 *      all interactive state: chart hover tooltips, retry logic, error banners,
 *      and the React Query refetch mechanism.
 *
 * Why split?
 *   Server Component: can call serverGet() (reads httpOnly cookie auth),
 *   drives generateMetadata, handles 404 via notFound().
 *   Client Component: uses Recharts (browser-only SVG APIs), ResizeObserver
 *   for responsiveness, and React Query's useQuery for the retry capability.
 *
 * Route: /companies/[id]/analytics
 * Milestone: M7.1F — Interactive Company Analytics & Financial Trend Charts
 */

import type { Metadata } from 'next'
import { notFound } from 'next/navigation'
import { serverGet } from '@/lib/server-api'
import type { Company, CompanyTrendsResponse } from '@/lib/types'
import { AnalyticsView } from './_components/analytics-view'

// Local params interface — mirrors the pattern used by financials/page.tsx.
// The global PageProps<> generic requires the route to be registered in the
// auto-generated AppRoutes type; a local interface avoids that dependency.
interface AnalyticsPageProps {
  params: Promise<{ id: string }>
}

export async function generateMetadata(
  props: AnalyticsPageProps,
): Promise<Metadata> {
  const { id } = await props.params
  try {
    const company = await serverGet<Company>(`/api/v1/companies/${id}`)
    return { title: `${company.name} — Analytics` }
  } catch {
    return { title: 'Analytics' }
  }
}

export default async function CompanyAnalyticsPage(
  props: AnalyticsPageProps,
) {
  const { id } = await props.params

  // ── Fetch company record ──────────────────────────────────────────────────
  let company: Company | null = null
  try {
    company = await serverGet<Company>(`/api/v1/companies/${id}`)
  } catch (e) {
    const err = e as { statusCode?: number }
    if (err.statusCode === 404) notFound()
  }
  if (!company) {
    return (
      <div className="py-12 text-center text-zinc-500">
        Failed to load company.
      </div>
    )
  }

  // ── Attempt SSR trends fetch (best-effort; client retries on failure) ─────
  let initialTrends: CompanyTrendsResponse | null = null
  let ssrError: string | null = null
  try {
    initialTrends = await serverGet<CompanyTrendsResponse>(
      `/api/v1/analytics/companies/${id}/trends?target_currency=USD`,
    )
  } catch (e) {
    const err = e as { statusCode?: number; apiCode?: string }
    // 404 ANALYTICS_NO_DATA is a known "no data yet" state — not an error.
    // Any other status code surfaces as an error the client can retry.
    if (err.statusCode !== 404) {
      ssrError = 'Could not load analytics data.'
    }
    // Leave initialTrends null; AnalyticsView handles both empty and errored states.
  }

  return (
    <AnalyticsView
      company={company}
      initialTrends={initialTrends}
      ssrError={ssrError}
    />
  )
}
