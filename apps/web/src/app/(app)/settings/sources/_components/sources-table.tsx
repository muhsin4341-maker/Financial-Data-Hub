'use client'

/**
 * SourcesTable — M3.5: Interactive Source Registry data grid.
 *
 * Client Component.  Receives server-rendered initial data and provides:
 *
 *  • Skeleton loader — 5 animated placeholder rows shown while `initialItems`
 *    is null (server fetch failed or still loading in Suspense).
 *  • Per-row toggle switch — fires POST /api/v1/sources/{id}/enable|disable
 *    against the authenticated API client.  The row updates optimistically:
 *    the badge flips immediately; on error it snaps back with a message.
 *  • Refresh — `router.refresh()` re-triggers the Server Component fetch and
 *    re-initialises all rows from the server's current state.
 *  • Empty state — custom illustration + help text when the registry is empty.
 *  • Provider-type filter — client-side filter pill bar; no extra API call.
 *
 * Toggle security note:
 *   The enable/disable endpoints require ADMIN role on the backend.  If a
 *   non-admin user calls them, the API returns 403 FORBIDDEN.  The toggle
 *   catches this and surfaces "Insufficient permissions" in the row error.
 *
 * Milestone: M3.5 — External Source Registry Management Dashboard
 */

import { useState, useCallback } from 'react'
import { useRouter } from 'next/navigation'
import { clsx } from 'clsx'
import { apiPost } from '@/lib/api'
import type { SourceConfig, ProviderType } from '@/lib/types'
import { Badge } from '@/app/_components/ui'

// ---------------------------------------------------------------------------
// Constants & metadata
// ---------------------------------------------------------------------------

type BadgeVariant = 'info' | 'default' | 'success' | 'warning' | 'danger'

const PROVIDER_META: Record<
  ProviderType,
  { label: string; variant: BadgeVariant }
> = {
  regulatory: { label: 'Regulatory', variant: 'danger' },
  exchange:   { label: 'Exchange',   variant: 'info' },
  manual:     { label: 'Manual',     variant: 'warning' },
  broker:     { label: 'Broker',     variant: 'default' },
}

const ALL_TYPES: Array<{ value: ProviderType | ''; label: string }> = [
  { value: '',           label: 'All types' },
  { value: 'regulatory', label: 'Regulatory' },
  { value: 'exchange',   label: 'Exchange' },
  { value: 'manual',     label: 'Manual' },
  { value: 'broker',     label: 'Broker' },
]

// ---------------------------------------------------------------------------
// ToggleSwitch
// ---------------------------------------------------------------------------

interface ToggleSwitchProps {
  checked: boolean
  loading: boolean
  disabled?: boolean
  onToggle: () => void
  label: string
}

function ToggleSwitch({
  checked,
  loading,
  disabled = false,
  onToggle,
  label,
}: ToggleSwitchProps) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      onClick={onToggle}
      disabled={disabled || loading}
      className={clsx(
        // Base pill shape
        'relative inline-flex h-6 w-11 shrink-0 items-center rounded-full',
        'border-2 border-transparent transition-colors duration-200 ease-in-out',
        // Focus ring
        'focus:outline-none focus:ring-2 focus:ring-blue-500 focus:ring-offset-2',
        // Disabled / loading
        'disabled:cursor-not-allowed disabled:opacity-50',
        // Active / inactive colours
        checked ? 'bg-emerald-500' : 'bg-zinc-300',
      )}
    >
      {loading ? (
        /* Spinner replaces the knob while the API call is in-flight */
        <span className="absolute inset-0 flex items-center justify-center">
          <svg
            className="h-3.5 w-3.5 animate-spin text-white"
            viewBox="0 0 24 24"
            fill="none"
            aria-hidden="true"
          >
            <circle
              className="opacity-30"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="4"
            />
            <path
              className="opacity-80"
              fill="currentColor"
              d="M4 12a8 8 0 018-8v8H4z"
            />
          </svg>
        </span>
      ) : (
        /* Sliding knob */
        <span
          aria-hidden="true"
          className={clsx(
            'pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow-sm ring-0 transition duration-200 ease-in-out',
            checked ? 'translate-x-5' : 'translate-x-0',
          )}
        />
      )}
    </button>
  )
}

// ---------------------------------------------------------------------------
// SkeletonRow
// ---------------------------------------------------------------------------

function SkeletonRow() {
  return (
    <tr className="border-b border-zinc-100 last:border-0">
      {/* Source identity */}
      <td className="px-4 py-4">
        <div className="h-4 w-36 rounded bg-zinc-200 animate-pulse" />
        <div className="mt-1.5 h-3 w-20 rounded bg-zinc-100 animate-pulse" />
      </td>
      {/* Type badge */}
      <td className="px-4 py-4">
        <div className="h-5 w-20 rounded-full bg-zinc-200 animate-pulse" />
      </td>
      {/* Region */}
      <td className="px-4 py-4">
        <div className="h-3.5 w-8 rounded bg-zinc-100 animate-pulse" />
      </td>
      {/* Rate limit */}
      <td className="px-4 py-4">
        <div className="h-3.5 w-14 rounded bg-zinc-100 animate-pulse" />
      </td>
      {/* Last updated */}
      <td className="px-4 py-4">
        <div className="h-3.5 w-24 rounded bg-zinc-100 animate-pulse" />
      </td>
      {/* Status badge */}
      <td className="px-4 py-4">
        <div className="h-5 w-14 rounded-full bg-zinc-200 animate-pulse" />
      </td>
      {/* Toggle */}
      <td className="px-4 py-4">
        <div className="h-6 w-11 rounded-full bg-zinc-200 animate-pulse" />
      </td>
    </tr>
  )
}

// ---------------------------------------------------------------------------
// SourceRow
// ---------------------------------------------------------------------------

interface SourceRowProps {
  source: SourceConfig
  onUpdated: (updated: SourceConfig) => void
}

function SourceRow({ source: initialSource, onUpdated }: SourceRowProps) {
  const [source, setSource] = useState<SourceConfig>(initialSource)
  const [toggling, setToggling] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleToggle = useCallback(async () => {
    setError(null)
    setToggling(true)

    // Optimistic update — flip the badge immediately for instant feedback.
    const optimistic = { ...source, is_active: !source.is_active }
    setSource(optimistic)

    const endpoint = optimistic.is_active
      ? `/api/v1/sources/${source.id}/enable`
      : `/api/v1/sources/${source.id}/disable`

    try {
      const updated = await apiPost<SourceConfig>(endpoint)
      setSource(updated)
      onUpdated(updated)
    } catch (e: unknown) {
      // Revert optimistic update on failure
      setSource(source)
      const err = e as {
        apiMessage?: string
        statusCode?: number
        message?: string
      }
      if (err.statusCode === 403) {
        setError('Insufficient permissions. Admin role required.')
      } else {
        setError(err.apiMessage ?? err.message ?? 'Toggle failed — please retry.')
      }
    } finally {
      setToggling(false)
    }
  }, [source, onUpdated])

  const providerMeta =
    PROVIDER_META[source.provider_type as ProviderType] ?? {
      label: source.provider_type,
      variant: 'default' as BadgeVariant,
    }

  return (
    <>
      <tr className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50/60 transition-colors">
        {/* Source identity */}
        <td className="px-4 py-3.5">
          <div className="font-semibold text-zinc-900 text-sm">{source.name}</div>
          <div className="mt-0.5 font-mono text-[11px] text-zinc-400 tracking-wide">
            {source.code}
          </div>
          {source.description && (
            <div
              className="mt-0.5 text-xs text-zinc-500 line-clamp-1 max-w-xs"
              title={source.description}
            >
              {source.description}
            </div>
          )}
        </td>

        {/* Provider type */}
        <td className="px-4 py-3.5 whitespace-nowrap">
          <Badge variant={providerMeta.variant}>{providerMeta.label}</Badge>
        </td>

        {/* Region */}
        <td className="px-4 py-3.5 whitespace-nowrap">
          {source.country_code ? (
            <span className="inline-flex items-center gap-1 text-sm font-medium text-zinc-700">
              <span className="text-base" aria-hidden="true">
                {/* Country flag emoji derived from ISO alpha-2 code */}
                {source.country_code
                  .toUpperCase()
                  .split('')
                  .map((c) => String.fromCodePoint(c.charCodeAt(0) + 127397))
                  .join('')}
              </span>
              {source.country_code}
            </span>
          ) : (
            <span className="text-xs text-zinc-400 italic">Global</span>
          )}
        </td>

        {/* Rate limit */}
        <td className="px-4 py-3.5 whitespace-nowrap">
          <span className="font-mono text-xs text-zinc-600 tabular-nums">
            {source.rate_limit_per_minute.toLocaleString()}
            <span className="text-zinc-400">/min</span>
          </span>
        </td>

        {/* Last updated */}
        <td className="px-4 py-3.5 whitespace-nowrap">
          <span className="text-xs text-zinc-500">
            {new Date(source.updated_at).toLocaleDateString('en-US', {
              year: 'numeric',
              month: 'short',
              day: 'numeric',
            })}
          </span>
        </td>

        {/* Status badge */}
        <td className="px-4 py-3.5 whitespace-nowrap">
          <span
            className={clsx(
              'inline-flex items-center gap-1.5 rounded-full px-2.5 py-0.5 text-xs font-medium',
              source.is_active
                ? 'bg-emerald-100 text-emerald-700'
                : 'bg-zinc-100 text-zinc-500',
            )}
          >
            {/* Dot indicator */}
            <span
              className={clsx(
                'h-1.5 w-1.5 rounded-full',
                source.is_active ? 'bg-emerald-500' : 'bg-zinc-400',
              )}
              aria-hidden="true"
            />
            {source.is_active ? 'Active' : 'Inactive'}
          </span>
        </td>

        {/* Toggle switch */}
        <td className="px-4 py-3.5">
          <ToggleSwitch
            checked={source.is_active}
            loading={toggling}
            onToggle={handleToggle}
            label={`${source.is_active ? 'Disable' : 'Enable'} ${source.name}`}
          />
        </td>
      </tr>

      {/* Per-row error — spans all columns */}
      {error && (
        <tr className="border-b border-zinc-100">
          <td colSpan={7} className="px-4 py-2">
            <p className="text-xs text-red-600">
              <strong className="font-semibold">Error:</strong> {error}{' '}
              <button
                className="underline hover:no-underline"
                onClick={() => setError(null)}
              >
                Dismiss
              </button>
            </p>
          </td>
        </tr>
      )}
    </>
  )
}

// ---------------------------------------------------------------------------
// SourcesTable (main export)
// ---------------------------------------------------------------------------

export interface SourcesTableProps {
  /**
   * Pre-fetched items from the Server Component.
   * `null`  = server fetch failed — show skeleton + retry.
   * `[]`    = fetch succeeded but registry is empty — show empty state.
   * `[...]` = normal list render.
   */
  initialItems: SourceConfig[] | null
}

export function SourcesTable({ initialItems }: SourcesTableProps) {
  const router = useRouter()
  const [items, setItems] = useState<SourceConfig[]>(initialItems ?? [])
  const [typeFilter, setTypeFilter] = useState<ProviderType | ''>('')

  // Called by SourceRow when a toggle API call succeeds — keep parent list
  // in sync so the stats in the filter bar reflect the new state.
  const handleRowUpdated = useCallback((updated: SourceConfig) => {
    setItems((prev) =>
      prev.map((s) => (s.id === updated.id ? updated : s)),
    )
  }, [])

  // Client-side filter — no API call
  const filtered =
    typeFilter === ''
      ? items
      : items.filter((s) => s.provider_type === typeFilter)

  const activeCount = items.filter((s) => s.is_active).length

  // ── Skeleton state ──────────────────────────────────────────────────────
  // Show when the server failed to fetch (initialItems === null).
  if (initialItems === null) {
    return (
      <div className="flex flex-col gap-4">
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-100 bg-zinc-50">
                <th className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide">
                  Source
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide">
                  Type
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide">
                  Region
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide">
                  Rate Limit
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide">
                  Last Updated
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide">
                  Status
                </th>
                <th className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide">
                  Active
                </th>
              </tr>
            </thead>
            <tbody>
              {Array.from({ length: 5 }).map((_, i) => (
                <SkeletonRow key={i} />
              ))}
            </tbody>
          </table>
        </div>
        <div className="text-center">
          <button
            onClick={() => router.refresh()}
            className="text-sm text-blue-600 hover:text-blue-500 underline"
          >
            Retry loading sources
          </button>
        </div>
      </div>
    )
  }

  // ── Empty state ─────────────────────────────────────────────────────────
  if (items.length === 0) {
    return (
      <div className="rounded-xl border-2 border-dashed border-zinc-200 bg-white p-16 text-center">
        {/* Icon */}
        <svg
          className="mx-auto h-12 w-12 text-zinc-300"
          xmlns="http://www.w3.org/2000/svg"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={1.5}
            d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z"
          />
        </svg>
        <h3 className="mt-4 text-sm font-semibold text-zinc-700">
          No Sources Active in Registry
        </h3>
        <p className="mt-2 text-sm text-zinc-400 max-w-sm mx-auto">
          The source registry is empty. Add your first data acquisition source
          via the API or ask your platform administrator to seed the initial
          connectors.
        </p>
        <button
          onClick={() => router.refresh()}
          className="mt-6 inline-flex items-center gap-1.5 text-sm text-blue-600 hover:text-blue-500 font-medium"
        >
          <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
            <path
              fillRule="evenodd"
              d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z"
              clipRule="evenodd"
            />
          </svg>
          Refresh
        </button>
      </div>
    )
  }

  // ── Normal table ────────────────────────────────────────────────────────
  return (
    <div className="flex flex-col gap-4">
      {/* Toolbar: type filter pills + refresh + stats */}
      <div className="flex items-center justify-between gap-4 flex-wrap">
        {/* Filter pills */}
        <div className="flex items-center gap-1.5 flex-wrap">
          {ALL_TYPES.map(({ value, label }) => (
            <button
              key={value}
              onClick={() => setTypeFilter(value as ProviderType | '')}
              className={clsx(
                'rounded-full px-3 py-1 text-xs font-medium transition-colors',
                typeFilter === value
                  ? 'bg-zinc-900 text-white'
                  : 'bg-zinc-100 text-zinc-600 hover:bg-zinc-200',
              )}
            >
              {label}
              {value === '' && (
                <span className="ml-1.5 tabular-nums text-zinc-400">
                  ({items.length})
                </span>
              )}
            </button>
          ))}
        </div>

        {/* Refresh + active indicator */}
        <div className="flex items-center gap-3">
          <span className="text-xs text-zinc-400">
            <span className="text-emerald-600 font-semibold">{activeCount}</span>
            {' / '}
            {items.length} active
          </span>
          <button
            onClick={() => router.refresh()}
            className="inline-flex items-center gap-1.5 rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-xs font-medium text-zinc-600 hover:bg-zinc-50 transition-colors"
            aria-label="Refresh source list from server"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fillRule="evenodd"
                d="M4 2a1 1 0 011 1v2.101a7.002 7.002 0 0111.601 2.566 1 1 0 11-1.885.666A5.002 5.002 0 005.999 7H9a1 1 0 010 2H4a1 1 0 01-1-1V3a1 1 0 011-1zm.008 9.057a1 1 0 011.276.61A5.002 5.002 0 0014.001 13H11a1 1 0 110-2h5a1 1 0 011 1v5a1 1 0 11-2 0v-2.101a7.002 7.002 0 01-11.601-2.566 1 1 0 01.61-1.276z"
                clipRule="evenodd"
              />
            </svg>
            Refresh
          </button>
        </div>
      </div>

      {/* Filtered result count when a filter is active */}
      {typeFilter !== '' && (
        <p className="text-xs text-zinc-500">
          Showing {filtered.length} {typeFilter} source{filtered.length !== 1 ? 's' : ''}
          {' — '}
          <button
            onClick={() => setTypeFilter('')}
            className="text-blue-600 hover:text-blue-500 underline"
          >
            clear filter
          </button>
        </p>
      )}

      {/* Table */}
      <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-sm">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-zinc-100 bg-zinc-50">
              <th
                scope="col"
                className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide"
              >
                Source
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide"
              >
                Type
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide"
              >
                Region
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide"
              >
                Rate Limit
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide"
              >
                Last Updated
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide"
              >
                Status
              </th>
              <th
                scope="col"
                className="px-4 py-3 text-left font-medium text-zinc-500 text-xs uppercase tracking-wide"
                title="Click to enable or disable this source"
              >
                Active
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td
                  colSpan={7}
                  className="px-4 py-8 text-center text-sm text-zinc-400"
                >
                  No {typeFilter} sources registered.{' '}
                  <button
                    onClick={() => setTypeFilter('')}
                    className="text-blue-600 hover:text-blue-500 underline"
                  >
                    Clear filter
                  </button>
                </td>
              </tr>
            ) : (
              filtered.map((source) => (
                <SourceRow
                  key={source.id}
                  source={source}
                  onUpdated={handleRowUpdated}
                />
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Footer note */}
      <p className="text-xs text-zinc-400 text-right">
        Mutations require admin role. Changes apply to all tenants immediately.
      </p>
    </div>
  )
}
