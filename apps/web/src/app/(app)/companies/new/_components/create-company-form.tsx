'use client'

/**
 * CreateCompanyForm — M2.6F: Upgraded with intelligent SEC typeahead.
 *
 * Two-mode form:
 *
 * ── MODE 1: Resolver mode (default) ─────────────────────────────────────────
 *   The TickerResolver widget sits at the top.  When the user selects a
 *   company from the SEC suggestion, the form fields below are auto-inflated:
 *     • Company name   → locked read-only field (company_name from resolver)
 *     • Ticker symbol  → locked read-only field
 *     • Exchange       → locked read-only field
 *     • CIK            → hidden field (forwarded to server action for DB storage)
 *   Only Sector and Website remain freely editable.
 *   A "Clear and re-search" link resets the resolved data.
 *
 * ── MODE 2: Manual mode ──────────────────────────────────────────────────────
 *   Activated by:
 *     (a) clicking "Use manual entry instead" in the resolver dropdown, OR
 *     (b) clearing the locked chip in the resolver widget.
 *   All fields revert to free-text inputs.  A "Search by ticker instead" link
 *   re-enables the resolver widget.
 *
 * ── Server action ────────────────────────────────────────────────────────────
 *   `createCompany` (apps/web/src/app/actions/companies.ts) is unchanged in
 *   contract — cik is forwarded as a hidden input when available.
 *
 * Milestone: M2.6F — Intelligent Company Typeahead & SEC Resolver Widget
 */

import { useActionState, useState, useEffect } from 'react'
import { useRouter } from 'next/navigation'
import { createCompany } from '@/app/actions/companies'
import type { CompanyFormState } from '@/app/actions/companies'
import type { CompanyResolveResponse } from '@/lib/types'
import { Input, Button, Alert } from '@/app/_components/ui'
import { TickerResolver } from './ticker-resolver'

// ---------------------------------------------------------------------------
// Locked field display — read-only with a "resolved" indicator
// ---------------------------------------------------------------------------

function LockedField({
  label,
  value,
  name,
}: {
  label: string
  value: string
  name: string
}) {
  return (
    <div className="flex flex-col gap-1">
      <label className="text-sm font-medium text-zinc-700">{label}</label>
      <div className="relative flex items-center">
        <input
          type="hidden"
          name={name}
          value={value}
        />
        <div
          className={[
            'w-full rounded-lg border border-emerald-200 bg-emerald-50',
            'px-3 py-2 text-sm font-medium text-emerald-800',
            'flex items-center gap-2',
          ].join(' ')}
          aria-label={`${label}: ${value} (auto-filled from SEC resolver)`}
        >
          <svg
            className="h-3.5 w-3.5 shrink-0 text-emerald-500"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
              clipRule="evenodd"
            />
          </svg>
          <span className="truncate">{value}</span>
        </div>
      </div>
      <p className="text-xs text-emerald-600">Auto-filled from SEC EDGAR</p>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Main form component
// ---------------------------------------------------------------------------

export function CreateCompanyForm() {
  const router = useRouter()
  const [state, action, pending] = useActionState<CompanyFormState, FormData>(
    createCompany,
    undefined,
  )

  // ── Resolved data from the typeahead widget ───────────────────────────────
  const [resolved, setResolved] = useState<CompanyResolveResponse | null>(null)

  /**
   * manual=true: user clicked "use manual entry" or cleared resolver widget.
   * manual=false: show the resolver widget + lock fields on selection.
   */
  const [manual, setManual] = useState(false)

  // Navigate to company detail on success
  useEffect(() => {
    if (state && 'success' in state && state.success) {
      router.push(`/companies/${state.company.id}`)
    }
  }, [state, router])

  const errors = !state || state.success ? undefined : state.errors
  const message = !state || state.success ? undefined : state.message

  // ── Resolver callbacks ────────────────────────────────────────────────────

  function handleResolved(result: CompanyResolveResponse) {
    setResolved(result)
    setManual(false)
  }

  function handleManualEntry() {
    setManual(true)
    setResolved(null)
  }

  function handleClearResolved() {
    setResolved(null)
    setManual(false)
  }

  // ── Whether resolver auto-filled the primary fields ───────────────────────
  const isAutoFilled = resolved !== null && !manual

  return (
    <form action={action} className="flex flex-col gap-6">
      {/* ── Global error message ─────────────────────────────────────────── */}
      {message && <Alert variant="error">{message}</Alert>}

      {/* ── SEC Resolver widget ──────────────────────────────────────────── */}
      {!manual && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-sm font-semibold text-zinc-800">
                Search by ticker
              </p>
              <p className="text-xs text-zinc-500 mt-0.5">
                Type a ticker (e.g. AAPL, MSFT) to auto-fill company details
                from SEC EDGAR.
              </p>
            </div>
            {/* Offer manual entry as an escape hatch */}
            <button
              type="button"
              onClick={handleManualEntry}
              className="shrink-0 text-xs text-zinc-400 hover:text-zinc-600 underline underline-offset-2 transition-colors ml-4"
            >
              Skip — enter manually
            </button>
          </div>

          <TickerResolver
            onResolved={handleResolved}
            onManualEntry={handleManualEntry}
          />
        </div>
      )}

      {/* "Return to resolver" link when in manual mode */}
      {manual && (
        <div className="flex items-center gap-2 rounded-lg border border-zinc-200 bg-zinc-50 px-3 py-2.5">
          <svg
            className="h-4 w-4 shrink-0 text-zinc-400"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M8 4a4 4 0 100 8 4 4 0 000-8zM2 8a6 6 0 1110.89 3.476l4.817 4.817a1 1 0 01-1.414 1.414l-4.816-4.816A6 6 0 012 8z"
              clipRule="evenodd"
            />
          </svg>
          <p className="text-xs text-zinc-500 flex-1">
            Entering details manually.
          </p>
          <button
            type="button"
            onClick={() => {
              setManual(false)
              setResolved(null)
            }}
            className="text-xs text-blue-600 hover:text-blue-700 font-medium underline underline-offset-2 transition-colors"
          >
            Search by ticker instead
          </button>
        </div>
      )}

      {/* ── Divider (only shown when resolver produced a result) ─────────── */}
      {isAutoFilled && (
        <div className="flex items-center gap-2">
          <div className="h-px flex-1 bg-zinc-100" />
          <span className="text-xs font-medium text-zinc-400 uppercase tracking-wider">
            Company details
          </span>
          <div className="h-px flex-1 bg-zinc-100" />
        </div>
      )}

      {/* ── Form fields ──────────────────────────────────────────────────── */}
      <div className="grid grid-cols-1 gap-5 sm:grid-cols-2">

        {/* Company name */}
        <div className="sm:col-span-2">
          {isAutoFilled ? (
            <LockedField
              label="Company name"
              name="name"
              value={resolved!.company_name}
            />
          ) : (
            <Input
              label="Company name"
              name="name"
              required
              placeholder="Apple Inc."
              error={errors?.name}
              defaultValue={resolved?.company_name ?? ''}
            />
          )}
        </div>

        {/* Ticker */}
        {isAutoFilled ? (
          <LockedField
            label="Ticker symbol"
            name="ticker"
            value={resolved!.ticker}
          />
        ) : (
          <Input
            label="Ticker symbol"
            name="ticker"
            required
            placeholder="AAPL"
            error={errors?.ticker}
            defaultValue={resolved?.ticker ?? ''}
          />
        )}

        {/* Exchange */}
        {isAutoFilled && resolved!.exchange ? (
          <LockedField
            label="Exchange"
            name="exchange"
            value={resolved!.exchange}
          />
        ) : (
          <Input
            label="Exchange"
            name="exchange"
            placeholder="NASDAQ"
            error={errors?.exchange}
            defaultValue={resolved?.exchange ?? ''}
          />
        )}

        {/* CIK — hidden when auto-filled so the server action can store it */}
        {isAutoFilled && (
          <input type="hidden" name="cik" value={resolved!.cik} />
        )}

        {/* Sector — always editable */}
        <Input
          label="Sector"
          name="sector"
          placeholder="Technology"
          error={errors?.sector}
        />

        {/* Website — always editable */}
        <Input
          label="Website"
          name="website"
          type="url"
          placeholder="https://apple.com"
          error={errors?.website}
        />
      </div>

      {/* ── CIK info pill (visible confirmation when auto-filled) ─────────── */}
      {isAutoFilled && (
        <div className="flex items-center gap-2 rounded-lg bg-zinc-50 border border-zinc-200 px-3 py-2">
          <svg
            className="h-3.5 w-3.5 shrink-0 text-zinc-400"
            viewBox="0 0 20 20"
            fill="currentColor"
            aria-hidden="true"
          >
            <path
              fillRule="evenodd"
              d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7-4a1 1 0 11-2 0 1 1 0 012 0zM9 9a1 1 0 000 2v3a1 1 0 001 1h1a1 1 0 100-2v-3a1 1 0 00-1-1H9z"
              clipRule="evenodd"
            />
          </svg>
          <p className="text-xs text-zinc-500 flex-1">
            SEC CIK{' '}
            <span className="font-mono font-semibold text-zinc-700">
              {resolved!.cik}
            </span>{' '}
            will be stored with this company.
          </p>
          <button
            type="button"
            onClick={handleClearResolved}
            className="text-xs text-zinc-400 hover:text-zinc-600 underline underline-offset-2 transition-colors"
          >
            Clear &amp; re-search
          </button>
        </div>
      )}

      {/* ── Actions ──────────────────────────────────────────────────────── */}
      <div className="flex gap-3 pt-1">
        <Button type="submit" loading={pending}>
          {pending ? 'Creating…' : 'Create company'}
        </Button>
        <Button
          type="button"
          variant="secondary"
          onClick={() => router.back()}
        >
          Cancel
        </Button>
      </div>
    </form>
  )
}
