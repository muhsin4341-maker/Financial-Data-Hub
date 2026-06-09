'use client'

/**
 * AnalyticsView — M7.1F: Interactive Company Analytics & Financial Trend Charts.
 *
 * Renders:
 *   1. Breadcrumb + page header
 *   2. Four KPI summary cards — most-recent period values + YoY Δ% indicator
 *   3. Revenue & Net Income chart  — ComposedChart (bars + line)
 *   4. Gross Profit & Operating Cash Flow chart — dual-line AreaChart
 *   5. Skeleton shimmer overlays while data is loading
 *   6. Error banner with manual "Retry" action
 *
 * ─────────────────────────────────────────────────────────────────────────
 * Data strategy
 * ─────────────────────────────────────────────────────────────────────────
 * Receives `initialTrends` from the Server Component (SSR pre-fetch).
 * React Query is seeded with that initial data, so on first render the
 * charts are hydrated immediately — no client-side spinner on load.
 *
 * If the SSR fetch failed (`ssrError !== null`), React Query fires the
 * request client-side automatically.  The error banner's "Retry" button
 * calls `refetch()` manually.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * Chart layout
 * ─────────────────────────────────────────────────────────────────────────
 * Chart 1 — Revenue vs Net Income:
 *   ComposedChart: Revenue as grouped bars (zinc-700), Net Income as a
 *   line+dots overlay (blue-500).  Dual Y-axes are avoided; both metrics
 *   share one axis so relative magnitude is preserved.
 *
 * Chart 2 — Gross Profit vs Operating Cash Flow:
 *   AreaChart with two semi-transparent fills: Gross Profit (indigo-400),
 *   Operating Cash Flow (emerald-500).  Area fills communicate volume
 *   while the stroke lines show trend direction cleanly.
 *
 * ─────────────────────────────────────────────────────────────────────────
 * Number formatting
 * ─────────────────────────────────────────────────────────────────────────
 * compact()     — axis tick labels: $1.5T / $394.3B / $4.5M / $450K
 * full()        — tooltip body: $394,328,000,000
 * delta()       — YoY Δ%: +12.4% / −3.1% with colour coding
 *
 * Milestone: M7.1F — Interactive Company Analytics & Financial Trend Charts
 */

import { useState } from 'react'
import Link from 'next/link'
import { useQuery } from '@tanstack/react-query'
import {
  ComposedChart,
  AreaChart,
  Area,
  Bar,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
  ResponsiveContainer,
} from 'recharts'
import { clsx } from 'clsx'
import { apiGet } from '@/lib/api'
import type { Company, CompanyTrendsResponse, TrendDataPoint } from '@/lib/types'

// ---------------------------------------------------------------------------
// Number formatters
// ---------------------------------------------------------------------------

/** Compact axis label: 394.3B, 96.9B, 1.5T, 450K */
function compact(value: number | null | undefined): string {
  if (value == null) return '—'
  const abs = Math.abs(value)
  const sign = value < 0 ? '-' : ''
  if (abs >= 1e12) return `${sign}$${(abs / 1e12).toFixed(1)}T`
  if (abs >= 1e9)  return `${sign}$${(abs / 1e9).toFixed(1)}B`
  if (abs >= 1e6)  return `${sign}$${(abs / 1e6).toFixed(1)}M`
  if (abs >= 1e3)  return `${sign}$${(abs / 1e3).toFixed(1)}K`
  return `${sign}$${abs.toFixed(0)}`
}

/** Full tooltip value: $394,328,000,000 */
function full(value: number | null | undefined, currency = 'USD'): string {
  if (value == null) return '—'
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
    maximumFractionDigits: 0,
  }).format(value)
}

/** YoY delta percentage string: +12.4% / −3.1% */
function delta(current: number | null, previous: number | null): string | null {
  if (current == null || previous == null || previous === 0) return null
  const pct = ((current - previous) / Math.abs(previous)) * 100
  const sign = pct >= 0 ? '+' : ''
  return `${sign}${pct.toFixed(1)}%`
}

// ---------------------------------------------------------------------------
// Colour tokens (Tailwind-aligned hex values — safe for SVG attributes)
// ---------------------------------------------------------------------------

const COLORS = {
  revenue:            '#3f3f46', // zinc-700
  netIncome:          '#3b82f6', // blue-500
  grossProfit:        '#818cf8', // indigo-400
  operatingCashFlow:  '#10b981', // emerald-500
  grid:               '#e4e4e7', // zinc-200
  axis:               '#a1a1aa', // zinc-400
  tooltipBg:          '#ffffff',
  tooltipBorder:      '#e4e4e7',
} as const

// ---------------------------------------------------------------------------
// Custom tooltip
// ---------------------------------------------------------------------------

/** Payload entry shape from Recharts — defined explicitly to avoid version-specific TooltipProps drift. */
interface TooltipEntry {
  dataKey: string
  value: number | null | undefined
  color?: string
  name?: string
}

interface ChartTooltipProps {
  active?: boolean
  payload?: TooltipEntry[]
  label?: string
  currency?: string
  metricLabels: Record<string, string>
}

function ChartTooltip({
  active,
  payload,
  label,
  currency = 'USD',
  metricLabels,
}: ChartTooltipProps) {
  if (!active || !payload?.length) return null

  return (
    <div
      className="rounded-xl border border-zinc-200 bg-white px-4 py-3 shadow-lg text-sm min-w-[200px]"
      style={{ borderColor: COLORS.tooltipBorder }}
    >
      <p className="font-semibold text-zinc-800 mb-2">{label}</p>
      {payload.map((entry: TooltipEntry) => {
        const displayLabel = metricLabels[entry.dataKey] ?? entry.dataKey
        const value = entry.value ?? null
        return (
          <div key={entry.dataKey} className="flex items-center justify-between gap-4 py-0.5">
            <div className="flex items-center gap-1.5">
              <span
                className="h-2.5 w-2.5 rounded-sm shrink-0"
                style={{ backgroundColor: entry.color ?? '#888' }}
              />
              <span className="text-zinc-500">{displayLabel}</span>
            </div>
            <span className={clsx(
              'font-medium tabular-nums',
              value != null && value < 0 ? 'text-red-600' : 'text-zinc-900',
            )}>
              {full(value, currency)}
            </span>
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------------------
// KPI metric card
// ---------------------------------------------------------------------------

interface MetricCardProps {
  label: string
  value: number | null
  previousValue: number | null
  currency: string
  accent: string   // Tailwind class for the accent dot
  loading?: boolean
}

function MetricCard({
  label,
  value,
  previousValue,
  currency,
  accent,
  loading = false,
}: MetricCardProps) {
  const yoyDelta = delta(value, previousValue)
  const isPositive = yoyDelta != null && !yoyDelta.startsWith('-')

  if (loading) {
    return (
      <div className="rounded-xl border border-zinc-200 bg-white p-5">
        <div className="h-3 w-24 rounded bg-zinc-200 animate-pulse mb-3" />
        <div className="h-7 w-36 rounded bg-zinc-200 animate-pulse mb-2" />
        <div className="h-3 w-16 rounded bg-zinc-200 animate-pulse" />
      </div>
    )
  }

  return (
    <div className="rounded-xl border border-zinc-200 bg-white p-5">
      <div className="flex items-center gap-2 mb-3">
        <span className={clsx('h-2 w-2 rounded-full shrink-0', accent)} />
        <p className="text-xs font-medium text-zinc-500 uppercase tracking-wide">
          {label}
        </p>
      </div>

      <p className={clsx(
        'text-2xl font-bold tabular-nums',
        value == null
          ? 'text-zinc-300'
          : value < 0
          ? 'text-red-600'
          : 'text-zinc-900',
      )}>
        {value == null ? '—' : compact(value)}
      </p>

      {yoyDelta != null ? (
        <p className={clsx(
          'mt-1.5 text-xs font-medium',
          isPositive ? 'text-emerald-600' : 'text-red-500',
        )}>
          {yoyDelta} vs prior period
        </p>
      ) : (
        <p className="mt-1.5 text-xs text-zinc-300">—</p>
      )}

      {value != null && (
        <p className="mt-1 text-[11px] text-zinc-400 font-mono tabular-nums">
          {full(value, currency)}
        </p>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Skeleton chart block
// ---------------------------------------------------------------------------

function ChartSkeleton({ height = 320 }: { height?: number }) {
  return (
    <div
      className="rounded-xl border border-zinc-200 bg-white p-5"
      style={{ height: height + 56 }}
    >
      <div className="h-4 w-40 rounded bg-zinc-200 animate-pulse mb-1.5" />
      <div className="h-3 w-60 rounded bg-zinc-100 animate-pulse mb-5" />
      <div
        className="rounded-lg bg-zinc-100 animate-pulse"
        style={{ height }}
      />
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

interface AnalyticsViewProps {
  company: Company
  initialTrends: CompanyTrendsResponse | null
  ssrError: string | null
}

export function AnalyticsView({
  company,
  initialTrends,
  ssrError,
}: AnalyticsViewProps) {
  const [currency] = useState('USD')

  // ── React Query — seeded with SSR data; client retries on demand ──────────
  const {
    data: trends,
    isLoading,
    isError,
    error,
    refetch,
    isFetching,
  } = useQuery<CompanyTrendsResponse, Error>({
    queryKey: ['analytics', 'trends', company.id, currency],
    queryFn: () =>
      apiGet<CompanyTrendsResponse>(
        `/api/v1/analytics/companies/${company.id}/trends?target_currency=${currency}`,
      ),
    initialData: initialTrends ?? undefined,
    // If SSR returned an error, attempt the fetch on the client immediately.
    enabled: initialTrends === null,
    staleTime: 5 * 60 * 1000,    // 5 min — financial data doesn't change per keystroke
    retry: 1,
  })

  // Determine loading/error state accounting for both SSR and client paths
  const showLoading = isLoading || (isFetching && !trends)
  const showError   = (isError || ssrError != null) && !trends
  const hasData     = trends != null && trends.data.length > 0
  const isEmpty     = trends != null && trends.data.length === 0

  // ── Derived KPI values (most-recent and second-most-recent periods) ───────
  const dataPoints: TrendDataPoint[] = trends?.data ?? []
  const latest   = dataPoints.at(-1) ?? null
  const previous = dataPoints.at(-2) ?? null
  const effectiveCurrency = latest?.currency ?? currency

  // ── Chart data: map to Recharts-friendly flat objects ─────────────────────
  const chartData = dataPoints.map((p) => ({
    name:               p.period,
    revenue:            p.revenue,
    net_income:         p.net_income,
    gross_profit:       p.gross_profit,
    operating_cash_flow: p.operating_cash_flow,
  }))

  // ── Render ────────────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-8 max-w-5xl">

      {/* ── Breadcrumb + header ─────────────────────────────────────────── */}
      <div>
        <div className="flex items-center gap-2 text-sm text-zinc-400 mb-2">
          <Link href="/companies" className="hover:text-zinc-600 transition-colors">
            Companies
          </Link>
          <span>›</span>
          <Link
            href={`/companies/${company.id}`}
            className="hover:text-zinc-600 transition-colors"
          >
            {company.name}
          </Link>
          <span>›</span>
          <span className="text-zinc-600 font-medium">Analytics</span>
        </div>

        <div className="flex items-start justify-between flex-wrap gap-3">
          <div>
            <h1 className="text-2xl font-bold text-zinc-900">{company.name}</h1>
            <p className="mt-1 text-sm text-zinc-500">
              Headline financial metrics · {effectiveCurrency} · Non-restated data only
            </p>
          </div>
          {trends && (
            <span className="inline-flex items-center gap-1.5 rounded-full bg-zinc-100 px-3 py-1 text-xs font-medium text-zinc-600">
              {trends.periods_covered} {trends.periods_covered === 1 ? 'period' : 'periods'}
            </span>
          )}
        </div>
      </div>

      {/* ── Error banner ────────────────────────────────────────────────── */}
      {showError && (
        <div
          role="alert"
          className="flex items-start gap-3 rounded-xl border border-red-200 bg-red-50 px-4 py-3.5"
        >
          <svg
            className="h-4 w-4 text-red-500 mt-0.5 shrink-0"
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
          <div className="flex-1">
            <p className="text-sm font-medium text-red-700">
              {ssrError ??
                ((error as Error)?.message || 'Failed to load analytics data.')}
            </p>
            <p className="mt-0.5 text-xs text-red-500">
              Analytics data may still be processing. Retry when ready.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refetch()}
            disabled={isFetching}
            className={clsx(
              'shrink-0 rounded-lg px-3 py-1.5 text-xs font-medium',
              'border border-red-300 bg-white text-red-600',
              'hover:bg-red-50 transition-colors',
              'disabled:opacity-50 disabled:cursor-not-allowed',
            )}
          >
            {isFetching ? 'Retrying…' : 'Retry Fetch'}
          </button>
        </div>
      )}

      {/* ── Empty state ─────────────────────────────────────────────────── */}
      {isEmpty && !showLoading && (
        <div className="flex flex-col items-center gap-4 rounded-xl border border-dashed border-zinc-300 bg-zinc-50 py-20 text-center">
          <svg
            className="h-12 w-12 text-zinc-300"
            viewBox="0 0 48 48"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            aria-hidden="true"
          >
            <path d="M6 36 L14 24 L22 30 L30 16 L38 20 L42 10" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx="14" cy="24" r="2" fill="currentColor" stroke="none" />
            <circle cx="22" cy="30" r="2" fill="currentColor" stroke="none" />
            <circle cx="30" cy="16" r="2" fill="currentColor" stroke="none" />
            <circle cx="38" cy="20" r="2" fill="currentColor" stroke="none" />
          </svg>
          <div>
            <p className="text-base font-semibold text-zinc-700">
              No analytics data yet
            </p>
            <p className="mt-1 text-sm text-zinc-400 max-w-xs mx-auto">
              Extraction jobs may still be processing. Data appears here once
              financial documents have been ingested for this company.
            </p>
          </div>
          <button
            type="button"
            onClick={() => void refetch()}
            className="rounded-lg border border-zinc-300 bg-white px-4 py-2 text-sm font-medium text-zinc-600 hover:bg-zinc-50 transition-colors"
          >
            Refresh
          </button>
        </div>
      )}

      {/* ── KPI Summary Cards ────────────────────────────────────────────── */}
      {(hasData || showLoading) && (
        <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
          <MetricCard
            label="Revenue"
            value={latest?.revenue ?? null}
            previousValue={previous?.revenue ?? null}
            currency={effectiveCurrency}
            accent="bg-zinc-700"
            loading={showLoading}
          />
          <MetricCard
            label="Gross Profit"
            value={latest?.gross_profit ?? null}
            previousValue={previous?.gross_profit ?? null}
            currency={effectiveCurrency}
            accent="bg-indigo-400"
            loading={showLoading}
          />
          <MetricCard
            label="Net Income"
            value={latest?.net_income ?? null}
            previousValue={previous?.net_income ?? null}
            currency={effectiveCurrency}
            accent="bg-blue-500"
            loading={showLoading}
          />
          <MetricCard
            label="Operating Cash Flow"
            value={latest?.operating_cash_flow ?? null}
            previousValue={previous?.operating_cash_flow ?? null}
            currency={effectiveCurrency}
            accent="bg-emerald-500"
            loading={showLoading}
          />
        </div>
      )}

      {/* ── Chart skeletons while loading ────────────────────────────────── */}
      {showLoading && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
          <ChartSkeleton />
          <ChartSkeleton />
        </div>
      )}

      {/* ── Charts ───────────────────────────────────────────────────────── */}
      {hasData && !showLoading && (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">

          {/* Chart 1 — Revenue vs Net Income */}
          <div className="rounded-xl border border-zinc-200 bg-white p-5">
            <h2 className="text-sm font-semibold text-zinc-900">
              Revenue vs Net Income
            </h2>
            <p className="text-xs text-zinc-400 mt-0.5 mb-5">
              Bar = Revenue · Line = Net Income · All figures in {effectiveCurrency}
            </p>
            <ResponsiveContainer width="100%" height={300}>
              <ComposedChart
                data={chartData}
                margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
              >
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke={COLORS.grid}
                  vertical={false}
                />
                <XAxis
                  dataKey="name"
                  tick={{ fontSize: 11, fill: COLORS.axis }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tickFormatter={(v: number) => compact(v)}
                  tick={{ fontSize: 11, fill: COLORS.axis }}
                  axisLine={false}
                  tickLine={false}
                  width={64}
                />
                <Tooltip
                  content={
                    <ChartTooltip
                      currency={effectiveCurrency}
                      metricLabels={{
                        revenue: 'Revenue',
                        net_income: 'Net Income',
                      }}
                    />
                  }
                />
                <Legend
                  iconType="square"
                  iconSize={10}
                  formatter={(value: string) =>
                    value === 'revenue' ? 'Revenue' : 'Net Income'
                  }
                  wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
                />
                <Bar
                  dataKey="revenue"
                  fill={COLORS.revenue}
                  radius={[3, 3, 0, 0]}
                  maxBarSize={40}
                />
                <Line
                  type="monotone"
                  dataKey="net_income"
                  stroke={COLORS.netIncome}
                  strokeWidth={2}
                  dot={{ r: 3, fill: COLORS.netIncome, strokeWidth: 0 }}
                  activeDot={{ r: 5, strokeWidth: 0 }}
                />
              </ComposedChart>
            </ResponsiveContainer>
          </div>

          {/* Chart 2 — Gross Profit & Operating Cash Flow */}
          <div className="rounded-xl border border-zinc-200 bg-white p-5">
            <h2 className="text-sm font-semibold text-zinc-900">
              Gross Profit &amp; Operating Cash Flow
            </h2>
            <p className="text-xs text-zinc-400 mt-0.5 mb-5">
              Operating efficiency trend · All figures in {effectiveCurrency}
            </p>
            <ResponsiveContainer width="100%" height={300}>
              <AreaChart
                data={chartData}
                margin={{ top: 4, right: 8, left: 0, bottom: 0 }}
              >
                <defs>
                  <linearGradient id="fillGrossProfit" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor={COLORS.grossProfit} stopOpacity={0.18} />
                    <stop offset="95%" stopColor={COLORS.grossProfit} stopOpacity={0.02} />
                  </linearGradient>
                  <linearGradient id="fillOCF" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="5%"  stopColor={COLORS.operatingCashFlow} stopOpacity={0.22} />
                    <stop offset="95%" stopColor={COLORS.operatingCashFlow} stopOpacity={0.02} />
                  </linearGradient>
                </defs>
                <CartesianGrid
                  strokeDasharray="3 3"
                  stroke={COLORS.grid}
                  vertical={false}
                />
                <XAxis
                  dataKey="name"
                  tick={{ fontSize: 11, fill: COLORS.axis }}
                  axisLine={false}
                  tickLine={false}
                />
                <YAxis
                  tickFormatter={(v: number) => compact(v)}
                  tick={{ fontSize: 11, fill: COLORS.axis }}
                  axisLine={false}
                  tickLine={false}
                  width={64}
                />
                <Tooltip
                  content={
                    <ChartTooltip
                      currency={effectiveCurrency}
                      metricLabels={{
                        gross_profit: 'Gross Profit',
                        operating_cash_flow: 'Operating Cash Flow',
                      }}
                    />
                  }
                />
                <Legend
                  iconType="square"
                  iconSize={10}
                  formatter={(value: string) =>
                    value === 'gross_profit' ? 'Gross Profit' : 'Operating Cash Flow'
                  }
                  wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
                />
                <Area
                  type="monotone"
                  dataKey="gross_profit"
                  stroke={COLORS.grossProfit}
                  strokeWidth={2}
                  fill="url(#fillGrossProfit)"
                  dot={{ r: 3, fill: COLORS.grossProfit, strokeWidth: 0 }}
                  activeDot={{ r: 5, strokeWidth: 0 }}
                />
                <Area
                  type="monotone"
                  dataKey="operating_cash_flow"
                  stroke={COLORS.operatingCashFlow}
                  strokeWidth={2}
                  fill="url(#fillOCF)"
                  dot={{ r: 3, fill: COLORS.operatingCashFlow, strokeWidth: 0 }}
                  activeDot={{ r: 5, strokeWidth: 0 }}
                />
              </AreaChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      {/* ── Footer context note ──────────────────────────────────────────── */}
      {hasData && (
        <p className="text-xs text-zinc-400 text-right">
          Source: extracted financial filings · Non-restated rows only ·{' '}
          {trends?.periods_covered} fiscal{' '}
          {trends?.periods_covered === 1 ? 'period' : 'periods'} shown
        </p>
      )}

    </div>
  )
}
