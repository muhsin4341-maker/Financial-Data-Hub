'use client'

/**
 * ValidationDashboard — M4.4F Interactive Parsing Validation & QA Dashboard.
 *
 * Client Component.  Owns:
 *   - React Query refetch for client-side retry
 *   - Skeleton shimmers during initial load
 *   - Scorecard summary cards (confidence score, finding counts, export status)
 *   - Itemized finding diagnostic ledger
 *   - Confidence deduction breakdown table
 *   - Zero-anomaly celebratory empty state
 *
 * Route: /jobs/[id]/validation
 * Milestone: M4.4F — Parsing Validation & Quality Assurance Dashboard
 */

import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import type { Job, ValidationResult, ValidationFinding } from '@/lib/types'

// ---------------------------------------------------------------------------
// Types / props
// ---------------------------------------------------------------------------

interface ValidationDashboardProps {
  job: Job
  initialData: ValidationResult | null
  ssrError: string | null
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Map confidence score to a colour class. Red <70, Amber 70–85, Emerald >85. */
function scoreColour(score: number): {
  ring: string
  text: string
  bg: string
  fill: string
} {
  if (score > 85)
    return {
      ring: 'ring-emerald-200',
      text: 'text-emerald-700',
      bg: 'bg-emerald-50',
      fill: 'bg-emerald-500',
    }
  if (score >= 70)
    return {
      ring: 'ring-amber-200',
      text: 'text-amber-700',
      bg: 'bg-amber-50',
      fill: 'bg-amber-500',
    }
  return {
    ring: 'ring-red-200',
    text: 'text-red-700',
    bg: 'bg-red-50',
    fill: 'bg-red-500',
  }
}

/** Map finding severity to display config. */
function severityConfig(severity: string): {
  dot: string
  badge: string
  label: string
  order: number
} {
  switch (severity.toUpperCase()) {
    case 'CRITICAL':
      return {
        dot: 'bg-red-500',
        badge: 'bg-red-50 text-red-700 ring-1 ring-red-200',
        label: 'Critical',
        order: 0,
      }
    case 'WARNING':
      return {
        dot: 'bg-amber-500',
        badge: 'bg-amber-50 text-amber-700 ring-1 ring-amber-200',
        label: 'Warning',
        order: 1,
      }
    default:
      return {
        dot: 'bg-sky-400',
        badge: 'bg-sky-50 text-sky-700 ring-1 ring-sky-200',
        label: 'Info',
        order: 2,
      }
  }
}

function formatValue(v: number | null): string {
  if (v === null || v === undefined) return '—'
  const abs = Math.abs(v)
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`
  if (abs >= 1e6) return `${(v / 1e6).toFixed(2)}M`
  if (abs >= 1e3) return `${(v / 1e3).toFixed(2)}K`
  return v.toLocaleString('en-US', { maximumFractionDigits: 2 })
}

function formatTs(iso: string): string {
  try {
    return new Date(iso).toLocaleString('en-US', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

// ---------------------------------------------------------------------------
// Skeleton sub-components
// ---------------------------------------------------------------------------

function ScoreCardSkeleton() {
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
      {Array.from({ length: 4 }).map((_, i) => (
        <div
          key={i}
          className="rounded-xl border border-zinc-100 bg-white p-5 flex flex-col gap-3"
        >
          <div className="h-3 w-24 rounded bg-zinc-200 animate-pulse" />
          <div className="h-8 w-16 rounded bg-zinc-200 animate-pulse" />
          <div className="h-2 w-32 rounded bg-zinc-200 animate-pulse" />
        </div>
      ))}
    </div>
  )
}

function FindingRowSkeleton() {
  return (
    <tr className="border-b border-zinc-100">
      <td className="px-4 py-3">
        <div className="h-5 w-16 rounded bg-zinc-200 animate-pulse" />
      </td>
      <td className="px-4 py-3">
        <div className="h-4 w-20 rounded bg-zinc-200 animate-pulse" />
      </td>
      <td className="px-4 py-3">
        <div className="h-4 w-48 rounded bg-zinc-200 animate-pulse" />
      </td>
      <td className="px-4 py-3">
        <div className="h-4 w-16 rounded bg-zinc-200 animate-pulse" />
      </td>
      <td className="px-4 py-3">
        <div className="h-4 w-16 rounded bg-zinc-200 animate-pulse" />
      </td>
      <td className="px-4 py-3">
        <div className="h-4 w-16 rounded bg-zinc-200 animate-pulse" />
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Score card
// ---------------------------------------------------------------------------

function ScoreCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string
  value: React.ReactNode
  sub?: string
  accent?: string
}) {
  return (
    <div className={`rounded-xl border border-zinc-100 bg-white p-5 ${accent ?? ''}`}>
      <p className="text-xs font-medium text-zinc-500 uppercase tracking-wider">
        {label}
      </p>
      <div className="mt-2 text-2xl font-bold text-zinc-900">{value}</div>
      {sub && <p className="mt-1 text-xs text-zinc-400">{sub}</p>}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Confidence ring (circular progress)
// ---------------------------------------------------------------------------

function ConfidenceRing({ score }: { score: number }) {
  const colours = scoreColour(score)
  const radius = 36
  const circ = 2 * Math.PI * radius
  const dash = (score / 100) * circ

  return (
    <div
      className={`relative rounded-xl border bg-white p-5 flex items-center gap-5 ${colours.ring} ${colours.bg}`}
    >
      <svg width={88} height={88} viewBox="0 0 88 88" className="shrink-0">
        {/* Track */}
        <circle
          cx={44}
          cy={44}
          r={radius}
          fill="none"
          stroke="#e4e4e7"
          strokeWidth={8}
        />
        {/* Progress */}
        <circle
          cx={44}
          cy={44}
          r={radius}
          fill="none"
          stroke={score > 85 ? '#10b981' : score >= 70 ? '#f59e0b' : '#ef4444'}
          strokeWidth={8}
          strokeLinecap="round"
          strokeDasharray={`${dash} ${circ}`}
          strokeDashoffset={circ / 4}
          transform="rotate(-90 44 44)"
          style={{ transition: 'stroke-dasharray 0.6s ease' }}
        />
        <text
          x={44}
          y={44}
          textAnchor="middle"
          dominantBaseline="central"
          className={`text-sm font-bold ${colours.text}`}
          style={{ fontSize: 15, fontWeight: 700 }}
          fill={score > 85 ? '#047857' : score >= 70 ? '#b45309' : '#b91c1c'}
        >
          {score}%
        </text>
      </svg>

      <div>
        <p className="text-xs font-medium text-zinc-500 uppercase tracking-wider">
          Confidence Score
        </p>
        <p className={`mt-1 text-2xl font-bold ${colours.text}`}>{score}%</p>
        <p className="mt-0.5 text-xs text-zinc-400">
          {score > 85
            ? 'High confidence extraction'
            : score >= 70
            ? 'Moderate confidence — review warnings'
            : 'Low confidence — critical issues found'}
        </p>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Export status banner
// ---------------------------------------------------------------------------

function ExportBanner({ isExportable }: { isExportable: boolean }) {
  return isExportable ? (
    <div className="flex items-center gap-3 rounded-xl border border-emerald-200 bg-emerald-50 px-5 py-4">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-emerald-500 text-white">
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M5 13l4 4L19 7" />
        </svg>
      </span>
      <div>
        <p className="text-sm font-semibold text-emerald-800 tracking-wide uppercase">
          ✓ Ready for Export
        </p>
        <p className="text-xs text-emerald-600 mt-0.5">
          No critical validation failures detected. Excel export pipeline is unblocked.
        </p>
      </div>
    </div>
  ) : (
    <div className="flex items-center gap-3 rounded-xl border border-red-200 bg-red-50 px-5 py-4">
      <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-red-500 text-white">
        <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </span>
      <div>
        <p className="text-sm font-semibold text-red-800 tracking-wide uppercase">
          ✗ Export Blocked
        </p>
        <p className="text-xs text-red-600 mt-0.5">
          Critical validation failures prevent automated Excel export. Review and resolve the findings below.
        </p>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Finding row
// ---------------------------------------------------------------------------

function FindingRow({ finding }: { finding: ValidationFinding }) {
  const cfg = severityConfig(finding.severity)
  return (
    <tr className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50 transition-colors">
      <td className="px-4 py-3 whitespace-nowrap">
        <span
          className={`inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold ${cfg.badge}`}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${cfg.dot}`} />
          {cfg.label}
        </span>
      </td>
      <td className="px-4 py-3 whitespace-nowrap">
        <span className="font-mono text-xs font-medium text-zinc-600 bg-zinc-100 rounded px-2 py-0.5">
          {finding.rule_id}
        </span>
      </td>
      <td className="px-4 py-3 text-sm text-zinc-700 leading-snug max-w-xs">
        {finding.message}
      </td>
      <td className="px-4 py-3 text-right text-xs font-mono text-zinc-500 whitespace-nowrap">
        {finding.expected !== null ? formatValue(finding.expected) : '—'}
      </td>
      <td className="px-4 py-3 text-right text-xs font-mono text-zinc-500 whitespace-nowrap">
        {finding.actual !== null ? formatValue(finding.actual) : '—'}
      </td>
      <td className="px-4 py-3 text-right text-xs font-mono whitespace-nowrap">
        {finding.delta !== null ? (
          <span className={Math.abs(finding.delta) > 0 ? 'text-red-600' : 'text-zinc-400'}>
            {formatValue(finding.delta)}
          </span>
        ) : '—'}
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// Zero anomalies empty state
// ---------------------------------------------------------------------------

function ZeroAnomaliesState() {
  return (
    <div className="flex flex-col items-center gap-4 py-12">
      {/* Checkmark trophy SVG */}
      <div className="flex h-16 w-16 items-center justify-center rounded-full bg-emerald-100">
        <svg
          className="w-8 h-8 text-emerald-600"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          strokeWidth={2}
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
          />
        </svg>
      </div>
      <div className="text-center">
        <p className="text-sm font-semibold text-emerald-700">
          Zero Parsing Anomalies Detected
        </p>
        <p className="mt-1 text-xs text-zinc-400 max-w-xs">
          All validation rules passed. The extraction engine found no mathematical
          inconsistencies, statement imbalances, or cross-statement mismatches.
        </p>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main dashboard
// ---------------------------------------------------------------------------

export function ValidationDashboard({
  job,
  initialData,
  ssrError,
}: ValidationDashboardProps) {
  const {
    data,
    isLoading,
    isFetching,
    isError,
    error,
    refetch,
  } = useQuery<ValidationResult>({
    queryKey: ['job-validation', job.id],
    queryFn: () => apiGet<ValidationResult>(`/api/v1/jobs/${job.id}/validation`),
    initialData: initialData ?? undefined,
    staleTime: 60 * 1000,  // 1 min — validation results rarely change after creation
    retry: 1,
  })

  const showSkeleton = isLoading || (isFetching && !data)

  // ── Error state ────────────────────────────────────────────────────────────
  const displayError = isError || (ssrError && !data)
  if (displayError && !data) {
    const msg =
      (error as { apiCode?: string })?.apiCode === 'VALIDATION_NOT_FOUND'
        ? 'No validation record found. The extraction pipeline may not have run yet, or the job is still processing.'
        : ssrError ?? 'Could not load validation results.'
    return (
      <div className="rounded-xl border border-amber-200 bg-amber-50 p-6 text-center">
        <p className="text-sm font-medium text-amber-800 mb-1">
          Validation Results Unavailable
        </p>
        <p className="text-xs text-amber-600 mb-4">{msg}</p>
        {!((error as { apiCode?: string })?.apiCode === 'VALIDATION_NOT_FOUND') && (
          <button
            onClick={() => refetch()}
            className="rounded-lg px-4 py-2 text-sm font-medium bg-amber-600 text-white hover:bg-amber-700 transition-colors"
          >
            Retry
          </button>
        )}
      </div>
    )
  }

  // ── Skeleton ───────────────────────────────────────────────────────────────
  if (showSkeleton) {
    return (
      <div className="flex flex-col gap-6">
        <ScoreCardSkeleton />
        <div className="h-14 rounded-xl bg-zinc-100 animate-pulse" />
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white">
          <table className="w-full text-sm">
            <tbody>
              {Array.from({ length: 5 }).map((_, i) => (
                <FindingRowSkeleton key={i} />
              ))}
            </tbody>
          </table>
        </div>
      </div>
    )
  }

  if (!data) return null

  // Sort findings: CRITICAL first, then WARNING, then INFO
  const sortedFindings = [...data.findings].sort(
    (a, b) =>
      severityConfig(a.severity).order - severityConfig(b.severity).order,
  )

  const infoCount =
    data.findings.length - data.critical_count - data.warning_count

  return (
    <div className="flex flex-col gap-6">
      {/* ── Refresh button row ─────────────────────────────────────────────── */}
      <div className="flex items-center justify-between gap-4">
        <p className="text-xs text-zinc-400">
          Run recorded {formatTs(data.created_at)} ·{' '}
          {data.items_validated.toLocaleString()} items validated
          {data.accession_number && (
            <> · accession <span className="font-mono">{data.accession_number}</span></>
          )}
        </p>
        <button
          onClick={() => refetch()}
          disabled={isFetching}
          className="inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs font-medium text-zinc-600 bg-white border border-zinc-200 hover:bg-zinc-50 transition-colors disabled:opacity-50"
        >
          <svg
            className={['w-3 h-3', isFetching ? 'animate-spin' : ''].join(' ')}
            fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}
          >
            <path strokeLinecap="round" strokeLinejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          {isFetching ? 'Refreshing…' : 'Refresh'}
        </button>
      </div>

      {/* ── Scorecard panel ────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {/* Confidence ring card spans full width at smallest breakpoint */}
        <div className="sm:col-span-2">
          <ConfidenceRing score={data.confidence_score} />
        </div>

        <ScoreCard
          label="Critical Errors"
          value={
            <span className={data.critical_count > 0 ? 'text-red-600' : 'text-zinc-900'}>
              {data.critical_count}
            </span>
          }
          sub="Blocks Excel export pipeline"
          accent={data.critical_count > 0 ? 'border-red-100' : ''}
        />

        <ScoreCard
          label="Warnings"
          value={
            <span className={data.warning_count > 0 ? 'text-amber-600' : 'text-zinc-900'}>
              {data.warning_count}
            </span>
          }
          sub="Export permitted with caveats"
          accent={data.warning_count > 0 ? 'border-amber-100' : ''}
        />
      </div>

      {/* Info + summary row */}
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
        <ScoreCard
          label="Info Remarks"
          value={infoCount < 0 ? 0 : infoCount}
          sub="Informational only — no action required"
        />
        <ScoreCard
          label="Items Validated"
          value={data.items_validated.toLocaleString()}
          sub="ParsedLineItem objects processed"
        />
        {data.fiscal_year && (
          <ScoreCard
            label="Fiscal Period"
            value={`${data.fiscal_year}${data.fiscal_period ? ` · ${data.fiscal_period}` : ''}`}
            sub="Period covered by this validation run"
          />
        )}
      </div>

      {/* ── Export status banner ────────────────────────────────────────────── */}
      <ExportBanner isExportable={data.is_exportable} />

      {/* ── Summary text (if present) ──────────────────────────────────────── */}
      {data.summary_text && (
        <div className="rounded-xl border border-zinc-100 bg-zinc-50 px-5 py-4">
          <p className="text-xs font-semibold text-zinc-500 uppercase tracking-wider mb-1">
            Engine Summary
          </p>
          <p className="text-sm text-zinc-700 font-mono leading-relaxed">
            {data.summary_text}
          </p>
        </div>
      )}

      {/* ── Itemized finding diagnostic ledger ─────────────────────────────── */}
      <div>
        <h2 className="text-base font-semibold text-zinc-900 mb-3">
          Validation Findings
          {sortedFindings.length > 0 && (
            <span className="ml-2 text-sm font-normal text-zinc-400">
              ({sortedFindings.length})
            </span>
          )}
        </h2>

        {sortedFindings.length === 0 ? (
          <div className="rounded-xl border border-zinc-100 bg-white">
            <ZeroAnomaliesState />
          </div>
        ) : (
          <div className="overflow-x-auto rounded-xl border border-zinc-200 bg-white">
            <table className="w-full text-sm min-w-[640px]">
              <thead>
                <tr className="border-b border-zinc-100 bg-zinc-50">
                  <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider">Severity</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider">Rule</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider">Description</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-zinc-500 uppercase tracking-wider">Expected</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-zinc-500 uppercase tracking-wider">Actual</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-zinc-500 uppercase tracking-wider">Delta</th>
                </tr>
              </thead>
              <tbody>
                {sortedFindings.map((finding, i) => (
                  <FindingRow key={`${finding.rule_id}-${i}`} finding={finding} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* ── Confidence deduction log ────────────────────────────────────────── */}
      {data.deductions.length > 0 && (
        <div>
          <h2 className="text-base font-semibold text-zinc-900 mb-3">
            Confidence Deductions
          </h2>
          <div className="overflow-x-auto rounded-xl border border-zinc-200 bg-white">
            <table className="w-full text-sm min-w-[480px]">
              <thead>
                <tr className="border-b border-zinc-100 bg-zinc-50">
                  <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider">Rule</th>
                  <th className="px-4 py-3 text-right text-xs font-semibold text-zinc-500 uppercase tracking-wider">Points Deducted</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold text-zinc-500 uppercase tracking-wider">Reason</th>
                </tr>
              </thead>
              <tbody>
                {data.deductions.map((d, i) => (
                  <tr
                    key={`${d.rule_id}-${i}`}
                    className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50 transition-colors"
                  >
                    <td className="px-4 py-3">
                      <span className="font-mono text-xs font-medium text-zinc-600 bg-zinc-100 rounded px-2 py-0.5">
                        {d.rule_id}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <span className="text-sm font-semibold text-red-600">
                        −{d.points}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm text-zinc-600">{d.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="mt-2 text-xs text-zinc-400 text-right">
            Starting score 100 → deductions applied → final score{' '}
            <span className={`font-semibold ${scoreColour(data.confidence_score).text}`}>
              {data.confidence_score}
            </span>
          </p>
        </div>
      )}
    </div>
  )
}
