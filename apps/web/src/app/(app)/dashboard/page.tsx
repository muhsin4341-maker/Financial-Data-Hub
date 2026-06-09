/**
 * Executive Command Center Dashboard — F5
 *
 * Production-grade entry point for the Financial Data Hub platform.
 * Server-rendered KPI cards + recent activity feed.
 *
 * KPI Cards
 * ─────────
 *   1. Total Monitored Companies  — distinct company entries in the workspace
 *   2. Active Pipeline Jobs       — running + queued extractions right now
 *   3. Pipeline Success Rate      — completed / (completed + failed) [%]
 *   4. Total Jobs Processed       — all-time job count for the workspace
 *
 * Activity Feed
 * ─────────────
 *   Last 10 jobs across all statuses, with status badge, company hint,
 *   fiscal year tag, and relative timestamp.
 *
 * Architecture
 * ────────────
 *   Server Component — all data fetching happens server-side via `serverGet`.
 *   No client-side hydration required; refresh is a full-page reload.
 *   Individual fetch failures are caught and rendered as "—" to prevent the
 *   entire page from breaking on partial API unavailability.
 *
 * Milestone: F5 — Executive Command Center Dashboard
 */

import type { Metadata } from 'next'
import Link from 'next/link'
import { serverGet } from '@/lib/server-api'
import type { CompanyListResponse, JobListResponse, Job } from '@/lib/types'
import { Card, Badge, JobStatusBadge } from '@/app/_components/ui'

export const metadata: Metadata = {
  title: 'Dashboard — Financial Data Hub',
}

// ---------------------------------------------------------------------------
// Data fetching helpers
// ---------------------------------------------------------------------------

async function safeGet<T>(path: string): Promise<T | null> {
  try {
    return await serverGet<T>(path)
  } catch {
    return null
  }
}

interface DashboardStats {
  totalCompanies:   number | null
  activeJobs:       number | null   // running + queued
  totalJobs:        number | null   // all-time
  completedJobs:    number | null
  failedJobs:       number | null
  recentJobs:       Job[]
}

async function getDashboardStats(): Promise<DashboardStats> {
  const [
    companiesRes,
    runningRes,
    queuedRes,
    allJobsRes,
    completedRes,
    failedRes,
    recentRes,
  ] = await Promise.all([
    safeGet<CompanyListResponse>('/api/v1/companies?page_size=1'),
    safeGet<JobListResponse>('/api/v1/jobs?status=running&page_size=1'),
    safeGet<JobListResponse>('/api/v1/jobs?status=queued&page_size=1'),
    safeGet<JobListResponse>('/api/v1/jobs?page_size=1'),
    safeGet<JobListResponse>('/api/v1/jobs?status=completed&page_size=1'),
    safeGet<JobListResponse>('/api/v1/jobs?status=failed&page_size=1'),
    safeGet<JobListResponse>('/api/v1/jobs?page_size=10'),
  ])

  const runningCount  = runningRes?.total  ?? 0
  const queuedCount   = queuedRes?.total   ?? 0

  return {
    totalCompanies: companiesRes?.total ?? null,
    activeJobs:     runningRes !== null && queuedRes !== null
      ? runningCount + queuedCount
      : null,
    totalJobs:      allJobsRes?.total      ?? null,
    completedJobs:  completedRes?.total    ?? null,
    failedJobs:     failedRes?.total       ?? null,
    recentJobs:     recentRes?.items       ?? [],
  }
}

// ---------------------------------------------------------------------------
// Derived metrics
// ---------------------------------------------------------------------------

function calcSuccessRate(completed: number | null, failed: number | null): number | null {
  if (completed === null || failed === null) return null
  const total = completed + failed
  if (total === 0) return null
  return Math.round((completed / total) * 100)
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function KpiCard({
  label,
  value,
  sub,
  icon,
  accentClass,
  href,
}: {
  label:        string
  value:        string
  sub?:         string
  icon:         React.ReactNode
  accentClass:  string
  href?:        string
}) {
  const inner = (
    <Card className="p-6 flex flex-col gap-4 hover:shadow-md transition-shadow">
      <div className="flex items-start justify-between">
        <p className="text-sm font-medium text-zinc-500">{label}</p>
        <div className={`flex h-9 w-9 items-center justify-center rounded-lg ${accentClass}`}>
          {icon}
        </div>
      </div>
      <div>
        <p className="text-3xl font-bold text-zinc-900 tabular-nums">{value}</p>
        {sub && <p className="mt-1 text-xs text-zinc-400">{sub}</p>}
      </div>
      {href && (
        <p className="text-xs text-blue-600 group-hover:text-blue-500 font-medium">
          View details →
        </p>
      )}
    </Card>
  )

  if (href) {
    return (
      <Link href={href} className="group block">
        {inner}
      </Link>
    )
  }
  return inner
}

function SuccessRingMicro({ rate }: { rate: number | null }) {
  if (rate === null) return <span className="text-zinc-400 text-sm">—</span>

  const radius   = 14
  const circ     = 2 * Math.PI * radius
  const filled   = (rate / 100) * circ
  const color    = rate >= 80 ? '#10b981' : rate >= 50 ? '#f59e0b' : '#ef4444'

  return (
    <div className="flex items-center gap-2">
      <svg width="36" height="36" viewBox="0 0 36 36" className="-rotate-90" aria-hidden="true">
        <circle cx="18" cy="18" r={radius} fill="none" stroke="#e4e4e7" strokeWidth="3" />
        <circle
          cx="18" cy="18" r={radius}
          fill="none" stroke={color} strokeWidth="3"
          strokeDasharray={`${filled} ${circ - filled}`}
          strokeLinecap="round"
        />
      </svg>
      <span className="text-2xl font-bold text-zinc-900 tabular-nums">{rate}%</span>
    </div>
  )
}

function ActivityFeedRow({ job, index }: { job: Job; index: number }) {
  const isEven = index % 2 === 0

  const relTime = (iso: string): string => {
    const diff = Date.now() - new Date(iso).getTime()
    const mins = Math.floor(diff / 60_000)
    if (mins < 1)  return 'just now'
    if (mins < 60) return `${mins}m ago`
    const hrs = Math.floor(mins / 60)
    if (hrs < 24)  return `${hrs}h ago`
    return `${Math.floor(hrs / 24)}d ago`
  }

  return (
    <Link
      href={`/jobs/${job.id}`}
      className={[
        'flex items-center gap-4 px-5 py-3.5 transition-colors',
        isEven ? 'bg-white' : 'bg-zinc-50/60',
        'hover:bg-blue-50/40',
      ].join(' ')}
    >
      {/* Status badge */}
      <div className="shrink-0 w-24">
        <JobStatusBadge status={job.status} />
      </div>

      {/* Job info */}
      <div className="flex-1 min-w-0">
        <p className="text-sm font-medium text-zinc-800 truncate">
          {job.job_type.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase())}
        </p>
        <p className="text-xs text-zinc-400 truncate font-mono">
          {job.id.slice(0, 8)}…
        </p>
      </div>

      {/* Fiscal year tag */}
      {job.fiscal_year && (
        <span className="shrink-0 inline-flex items-center rounded-md bg-zinc-100 px-2 py-0.5 text-xs font-medium text-zinc-600">
          FY {job.fiscal_year}
        </span>
      )}

      {/* Error indicator */}
      {job.status === 'failed' && job.error_message && (
        <span
          className="shrink-0 max-w-[140px] text-xs text-red-500 truncate"
          title={job.error_message}
        >
          {job.error_message.length > 40
            ? job.error_message.slice(0, 40) + '…'
            : job.error_message}
        </span>
      )}

      {/* Timestamp */}
      <span className="shrink-0 text-xs text-zinc-400 tabular-nums">
        {relTime(job.updated_at)}
      </span>
    </Link>
  )
}

// ---------------------------------------------------------------------------
// Pipeline health sparkline (pure SVG, no chart library needed)
// ---------------------------------------------------------------------------

function PipelineHealthBar({
  completed,
  failed,
  active,
  total,
}: {
  completed: number | null
  failed:    number | null
  active:    number | null
  total:     number | null
}) {
  if (!total) return null
  const t = total || 1
  const c = completed ?? 0
  const f = failed    ?? 0
  const a = active    ?? 0
  const pending = Math.max(0, t - c - f - a)

  const pctCompleted = (c / t) * 100
  const pctFailed    = (f / t) * 100
  const pctActive    = (a / t) * 100
  const pctPending   = (pending / t) * 100

  return (
    <div className="flex flex-col gap-2">
      <p className="text-xs font-medium text-zinc-500 uppercase tracking-wider">
        Pipeline Composition
      </p>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-zinc-100">
        {pctCompleted > 0 && (
          <div className="h-full bg-emerald-500 transition-all" style={{ width: `${pctCompleted}%` }} />
        )}
        {pctActive > 0 && (
          <div className="h-full bg-blue-400 transition-all" style={{ width: `${pctActive}%` }} />
        )}
        {pctFailed > 0 && (
          <div className="h-full bg-red-400 transition-all" style={{ width: `${pctFailed}%` }} />
        )}
        {pctPending > 0 && (
          <div className="h-full bg-zinc-300 transition-all" style={{ width: `${pctPending}%` }} />
        )}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-emerald-500" />
          Completed ({c.toLocaleString()})
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-blue-400" />
          Active ({a.toLocaleString()})
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-2 w-2 rounded-full bg-red-400" />
          Failed ({f.toLocaleString()})
        </span>
        {pending > 0 && (
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-full bg-zinc-300" />
            Pending ({pending.toLocaleString()})
          </span>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default async function DashboardPage() {
  const stats = await getDashboardStats()
  const successRate = calcSuccessRate(stats.completedJobs, stats.failedJobs)

  return (
    <div className="flex flex-col gap-8">

      {/* Page header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-bold text-zinc-900">Command Center</h1>
          <p className="mt-1 text-sm text-zinc-500">
            Real-time overview of your Financial Data Hub workspace.
          </p>
        </div>
        <Link
          href="/companies/new"
          className="inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-700 transition-colors shadow-sm"
        >
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path d="M10.75 4.75a.75.75 0 00-1.5 0v4.5h-4.5a.75.75 0 000 1.5h4.5v4.5a.75.75 0 001.5 0v-4.5h4.5a.75.75 0 000-1.5h-4.5v-4.5z" />
          </svg>
          Add Company
        </Link>
      </div>

      {/* ── KPI grid ──────────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">

        {/* 1. Total Companies */}
        <KpiCard
          label="Monitored Companies"
          value={stats.totalCompanies !== null ? stats.totalCompanies.toLocaleString() : '—'}
          sub="Companies tracked in this workspace"
          href="/companies"
          accentClass="bg-blue-50 text-blue-600"
          icon={
            <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fillRule="evenodd" d="M4 16.5v-13h-.25a.75.75 0 010-1.5h12.5a.75.75 0 010 1.5H16v13h.25a.75.75 0 010 1.5h-3.5a.75.75 0 01-.75-.75v-2.5a.75.75 0 00-.75-.75h-2.5a.75.75 0 00-.75.75v2.5a.75.75 0 01-.75.75h-3.5a.75.75 0 010-1.5H4zm3-11a.5.5 0 01.5-.5h1a.5.5 0 01.5.5v1a.5.5 0 01-.5.5h-1a.5.5 0 01-.5-.5v-1zM7.5 9a.5.5 0 00-.5.5v1a.5.5 0 00.5.5h1a.5.5 0 00.5-.5v-1a.5.5 0 00-.5-.5h-1zM11 5.5a.5.5 0 01.5-.5h1a.5.5 0 01.5.5v1a.5.5 0 01-.5.5h-1a.5.5 0 01-.5-.5v-1zm.5 3.5a.5.5 0 00-.5.5v1a.5.5 0 00.5.5h1a.5.5 0 00.5-.5v-1a.5.5 0 00-.5-.5h-1z" clipRule="evenodd" />
            </svg>
          }
        />

        {/* 2. Active Extractions */}
        <KpiCard
          label="Active Extractions"
          value={stats.activeJobs !== null ? stats.activeJobs.toLocaleString() : '—'}
          sub="Running + queued pipeline jobs"
          accentClass={
            (stats.activeJobs ?? 0) > 0
              ? 'bg-amber-50 text-amber-600'
              : 'bg-emerald-50 text-emerald-600'
          }
          icon={
            <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path fillRule="evenodd" d="M15.312 11.424a5.5 5.5 0 01-9.201 2.466l-.312-.311h2.433a.75.75 0 000-1.5H3.989a.75.75 0 00-.75.75v4.242a.75.75 0 001.5 0v-2.43l.31.31a7 7 0 0011.712-3.138.75.75 0 00-1.449-.39zm1.23-3.723a.75.75 0 00.219-.53V2.929a.75.75 0 00-1.5 0V5.36l-.31-.31A7 7 0 003.239 8.188a.75.75 0 101.448.389A5.5 5.5 0 0113.89 6.11l.311.31h-2.432a.75.75 0 000 1.5h4.243a.75.75 0 00.53-.219z" clipRule="evenodd" />
            </svg>
          }
        />

        {/* 3. Pipeline Success Rate */}
        <Card className="p-6 flex flex-col gap-4">
          <div className="flex items-start justify-between">
            <p className="text-sm font-medium text-zinc-500">Pipeline Success Rate</p>
            <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600">
              <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.857-9.809a.75.75 0 00-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 10-1.06 1.061l2.5 2.5a.75.75 0 001.137-.089l4-5.5z" clipRule="evenodd" />
              </svg>
            </div>
          </div>
          <SuccessRingMicro rate={successRate} />
          <p className="text-xs text-zinc-400">
            {stats.completedJobs !== null && stats.failedJobs !== null
              ? `${(stats.completedJobs).toLocaleString()} completed · ${(stats.failedJobs).toLocaleString()} failed`
              : 'Based on all completed and failed jobs'}
          </p>
        </Card>

        {/* 4. Total Jobs Processed */}
        <KpiCard
          label="Total Jobs Processed"
          value={stats.totalJobs !== null ? stats.totalJobs.toLocaleString() : '—'}
          sub="All-time extraction pipeline runs"
          accentClass="bg-violet-50 text-violet-600"
          icon={
            <svg className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path d="M2 10a8 8 0 018-8v8l5.658 5.657a8 8 0 11-13.657-5.657z" />
              <path d="M9.008 2.004A8.003 8.003 0 0120 10h-8l-2.992-7.996z" />
            </svg>
          }
        />
      </div>

      {/* ── Pipeline composition bar ───────────────────────────────────────── */}
      {stats.totalJobs !== null && stats.totalJobs > 0 && (
        <Card className="p-6">
          <PipelineHealthBar
            completed={stats.completedJobs}
            failed={stats.failedJobs}
            active={stats.activeJobs}
            total={stats.totalJobs}
          />
        </Card>
      )}

      {/* ── Two-column lower section ───────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">

        {/* Activity feed — takes 2/3 width on large screens */}
        <div className="lg:col-span-2 flex flex-col gap-0">
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-base font-semibold text-zinc-900">
              Operational Activity
            </h2>
            <Link
              href="/companies"
              className="text-xs text-blue-600 hover:text-blue-500 font-medium"
            >
              View all jobs →
            </Link>
          </div>
          <Card className="overflow-hidden divide-y divide-zinc-100 p-0">
            {stats.recentJobs.length === 0 ? (
              <div className="flex flex-col items-center gap-3 py-12 px-6 text-center">
                <svg className="h-10 w-10 text-zinc-300" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm.75-11.25a.75.75 0 00-1.5 0v2.5h-2.5a.75.75 0 000 1.5h2.5v2.5a.75.75 0 001.5 0v-2.5h2.5a.75.75 0 000-1.5h-2.5v-2.5z" clipRule="evenodd" />
                </svg>
                <div>
                  <p className="text-sm font-medium text-zinc-500">No jobs yet</p>
                  <p className="text-xs text-zinc-400 mt-1">
                    Add a company and trigger your first extraction to get started.
                  </p>
                </div>
                <Link
                  href="/companies/new"
                  className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-blue-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 transition-colors"
                >
                  Add your first company
                </Link>
              </div>
            ) : (
              stats.recentJobs.map((job, i) => (
                <ActivityFeedRow key={job.id} job={job} index={i} />
              ))
            )}
          </Card>
        </div>

        {/* Quick actions + system links — 1/3 width */}
        <div className="flex flex-col gap-4">
          <h2 className="text-base font-semibold text-zinc-900">Quick Actions</h2>

          <Card className="p-5 flex flex-col gap-3">
            <QuickActionLink
              href="/companies/new"
              label="Add Company"
              description="Register a new company for data extraction"
              icon={
                <svg className="h-5 w-5 text-blue-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path d="M10.75 4.75a.75.75 0 00-1.5 0v4.5h-4.5a.75.75 0 000 1.5h4.5v4.5a.75.75 0 001.5 0v-4.5h4.5a.75.75 0 000-1.5h-4.5v-4.5z" />
                </svg>
              }
            />
            <QuickActionLink
              href="/companies"
              label="Browse Companies"
              description="View all monitored companies and their filings"
              icon={
                <svg className="h-5 w-5 text-violet-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path fillRule="evenodd" d="M4 16.5v-13h-.25a.75.75 0 010-1.5h12.5a.75.75 0 010 1.5H16v13h.25a.75.75 0 010 1.5h-3.5a.75.75 0 01-.75-.75v-2.5a.75.75 0 00-.75-.75h-2.5a.75.75 0 00-.75.75v2.5a.75.75 0 01-.75.75h-3.5a.75.75 0 010-1.5H4z" clipRule="evenodd" />
                </svg>
              }
            />
            <QuickActionLink
              href="/settings/team"
              label="Invite Team Member"
              description="Add collaborators to your workspace"
              icon={
                <svg className="h-5 w-5 text-emerald-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path d="M11 5a3 3 0 11-6 0 3 3 0 016 0zM2.615 16.428a1.224 1.224 0 01-.569-1.175 6.002 6.002 0 0111.908 0c.058.467-.172.92-.57 1.174A9.953 9.953 0 018 18a9.953 9.953 0 01-5.385-1.572zM16.25 5.75a.75.75 0 00-1.5 0v2h-2a.75.75 0 000 1.5h2v2a.75.75 0 001.5 0v-2h2a.75.75 0 000-1.5h-2v-2z" />
                </svg>
              }
            />
            <QuickActionLink
              href="/settings/sources"
              label="Manage Data Sources"
              description="Configure SEC EDGAR and other data providers"
              icon={
                <svg className="h-5 w-5 text-amber-600" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
                  <path fillRule="evenodd" d="M3.5 2A1.5 1.5 0 002 3.5V5c0 1.149.15 2.263.43 3.326a13.022 13.022 0 009.244 9.244c1.063.28 2.177.43 3.326.43h1.5a1.5 1.5 0 001.5-1.5v-1.148a1.5 1.5 0 00-1.175-1.465l-3.223-.716a1.5 1.5 0 00-1.767 1.052l-.267.933c-.117.41-.555.643-.95.48a11.542 11.542 0 01-6.254-6.254c-.163-.395.07-.833.48-.95l.933-.267a1.5 1.5 0 001.052-1.767l-.716-3.223A1.5 1.5 0 004.648 2H3.5z" clipRule="evenodd" />
                </svg>
              }
            />
          </Card>

          {/* System status indicators */}
          <div className="rounded-xl border border-zinc-200 bg-white p-5">
            <p className="text-sm font-semibold text-zinc-700 mb-3">System Status</p>
            <div className="flex flex-col gap-2">
              <StatusIndicator
                label="API Service"
                status="operational"
              />
              <StatusIndicator
                label="Celery Workers"
                status={stats.activeJobs !== null ? 'operational' : 'unknown'}
              />
              <StatusIndicator
                label="Database"
                status={stats.totalJobs !== null ? 'operational' : 'degraded'}
              />
              <StatusIndicator
                label="S3 Storage"
                status="operational"
              />
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Helper sub-components
// ---------------------------------------------------------------------------

function QuickActionLink({
  href,
  label,
  description,
  icon,
}: {
  href:        string
  label:       string
  description: string
  icon:        React.ReactNode
}) {
  return (
    <Link
      href={href}
      className="flex items-start gap-3 rounded-lg p-2.5 hover:bg-zinc-50 transition-colors group"
    >
      <div className="shrink-0 mt-0.5">{icon}</div>
      <div className="min-w-0">
        <p className="text-sm font-semibold text-zinc-800 group-hover:text-blue-700 transition-colors">
          {label}
        </p>
        <p className="text-xs text-zinc-400 mt-0.5 leading-relaxed">{description}</p>
      </div>
    </Link>
  )
}

function StatusIndicator({
  label,
  status,
}: {
  label:  string
  status: 'operational' | 'degraded' | 'unknown'
}) {
  const dotClass =
    status === 'operational' ? 'bg-emerald-500'
    : status === 'degraded'  ? 'bg-red-500 animate-pulse'
    : 'bg-zinc-400'

  const textClass =
    status === 'operational' ? 'text-emerald-600'
    : status === 'degraded'  ? 'text-red-600'
    : 'text-zinc-400'

  const statusLabel =
    status === 'operational' ? 'Operational'
    : status === 'degraded'  ? 'Degraded'
    : 'Unknown'

  return (
    <div className="flex items-center justify-between">
      <span className="text-xs text-zinc-600">{label}</span>
      <span className="flex items-center gap-1.5">
        <span className={`h-2 w-2 rounded-full ${dotClass}`} />
        <span className={`text-xs font-medium ${textClass}`}>{statusLabel}</span>
      </span>
    </div>
  )
}
