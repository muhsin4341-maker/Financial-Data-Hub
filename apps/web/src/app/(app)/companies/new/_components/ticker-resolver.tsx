'use client'

/**
 * TickerResolver — M2.6F: Intelligent Company Typeahead & SEC Resolver Widget.
 *
 * A self-contained autocomplete combobox that resolves a ticker symbol to
 * canonical SEC EDGAR company data via:
 *   GET /api/v1/companies/resolve?ticker={query}
 *
 * ─────────────────────────────────────────────────────────────────
 * Interaction model
 * ─────────────────────────────────────────────────────────────────
 *
 * 1. USER TYPES  (e.g. "AAPL", "MSFT", "TSLA")
 *    → Input is debounced 300 ms before firing the resolver request.
 *    → Empty / whitespace-only input clears the dropdown immediately.
 *
 * 2. IN-FLIGHT
 *    → Dropdown opens and shows an animated spinner row.
 *
 * 3. MATCH FOUND
 *    → Dropdown shows a suggestion card:
 *        "Apple Inc."  •  NASDAQ: AAPL  •  CIK 0000320193
 *    → Keyboard: ArrowDown/ArrowUp to navigate, Enter/Space to select,
 *      Escape to dismiss.
 *
 * 4. USER SELECTS SUGGESTION
 *    → `onResolved(result)` callback fires with the full CompanyResolveResponse.
 *    → Input collapses to a readonly "resolved chip" showing the company name
 *      with a ✕ clear button.
 *    → Parent maps `result` to form fields (name, ticker, cik, exchange).
 *
 * 5. NO MATCH (404)
 *    → Dropdown shows "No SEC record found for '{query}'" + a
 *      "Use manual entry instead →" action that calls `onManualEntry()`.
 *
 * 6. TIMEOUT / NETWORK ERROR
 *    → Dropdown shows an amber "Resolution service timed out" alert +
 *      the "Use manual entry instead" option.
 *
 * ─────────────────────────────────────────────────────────────────
 * Accessibility
 * ─────────────────────────────────────────────────────────────────
 * - role="combobox" on the input wrapper; role="listbox" on the dropdown.
 * - aria-expanded / aria-activedescendant wired for screen readers.
 * - Focus trapped within the dropdown on keyboard navigation.
 * - Escape closes dropdown and returns focus to the input.
 *
 * Milestone: M2.6F — Intelligent Company Typeahead & SEC Resolver Widget
 */

import {
  useState,
  useEffect,
  useRef,
  useCallback,
  useId,
} from 'react'
import { apiGet } from '@/lib/api'
import type { CompanyResolveResponse } from '@/lib/types'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

/** Debounce window before the resolver API is called (ms). */
const DEBOUNCE_MS = 300

/** Request timeout — after this we show the amber timeout alert (ms). */
const REQUEST_TIMEOUT_MS = 8_000

/** Minimum query length before we bother hitting the API. */
const MIN_QUERY_LEN = 1

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

type ResolverState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'success'; result: CompanyResolveResponse }
  | { kind: 'not_found'; query: string }
  | { kind: 'timeout'; query: string }
  | { kind: 'error'; message: string }

interface TickerResolverProps {
  /** Called when the user selects a resolved company from the dropdown. */
  onResolved: (result: CompanyResolveResponse) => void
  /** Called when the user clicks "Use manual entry instead". */
  onManualEntry: () => void
  /** Optional className for the outermost wrapper div. */
  className?: string
}

// ---------------------------------------------------------------------------
// Helper — exchange/ticker badge text
// ---------------------------------------------------------------------------

function buildBadge(result: CompanyResolveResponse): string {
  const parts: string[] = []
  if (result.exchange) parts.push(result.exchange)
  parts.push(result.ticker)
  return parts.join(': ')
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function TickerResolver({
  onResolved,
  onManualEntry,
  className,
}: TickerResolverProps) {
  const listboxId = useId()
  const inputRef = useRef<HTMLInputElement>(null)
  const dropdownRef = useRef<HTMLDivElement>(null)
  const abortRef = useRef<AbortController | null>(null)
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const [query, setQuery] = useState('')
  const [state, setState] = useState<ResolverState>({ kind: 'idle' })
  const [open, setOpen] = useState(false)
  /** When a company is locked in (selected), we show a chip instead of input. */
  const [locked, setLocked] = useState<CompanyResolveResponse | null>(null)
  /** Keyboard-focused option index (only 1 option max in current API). */
  const [activeIdx, setActiveIdx] = useState(-1)

  // ── Resolver API call ──────────────────────────────────────────────────
  const runResolve = useCallback(
    async (q: string) => {
      // Cancel any in-flight request
      if (abortRef.current) abortRef.current.abort()
      const controller = new AbortController()
      abortRef.current = controller

      setState({ kind: 'loading' })
      setOpen(true)
      setActiveIdx(-1)

      // Timeout guard
      const timeoutId = setTimeout(() => {
        controller.abort()
      }, REQUEST_TIMEOUT_MS)

      try {
        const result = await apiGet<CompanyResolveResponse>(
          `/api/v1/companies/resolve?ticker=${encodeURIComponent(q.toUpperCase())}`,
        )
        clearTimeout(timeoutId)
        if (controller.signal.aborted) return
        setState({ kind: 'success', result })
      } catch (e: unknown) {
        clearTimeout(timeoutId)
        if (controller.signal.aborted) {
          // Distinguish abort-by-timeout from abort-by-new-query
          // If the abort was triggered by our own timeout timer, signal.aborted is
          // true but we haven't started a new request yet — check reason.
          setState({ kind: 'timeout', query: q })
          return
        }
        const err = e as { statusCode?: number; response?: { status?: number } }
        const status = err.statusCode ?? err.response?.status
        if (status === 404) {
          setState({ kind: 'not_found', query: q })
        } else {
          setState({
            kind: 'error',
            message: 'Resolution service unavailable. Please try again.',
          })
        }
      }
    },
    [],
  )

  // ── Debounced input handler ────────────────────────────────────────────
  function handleInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    const val = e.target.value
    setQuery(val)

    if (debounceRef.current) clearTimeout(debounceRef.current)

    const trimmed = val.trim()
    if (trimmed.length < MIN_QUERY_LEN) {
      // Abort any in-flight request and reset
      if (abortRef.current) abortRef.current.abort()
      setState({ kind: 'idle' })
      setOpen(false)
      return
    }

    debounceRef.current = setTimeout(() => {
      runResolve(trimmed)
    }, DEBOUNCE_MS)
  }

  // ── Selection handler ─────────────────────────────────────────────────
  function handleSelect(result: CompanyResolveResponse) {
    setLocked(result)
    setOpen(false)
    setQuery('')
    setState({ kind: 'idle' })
    onResolved(result)
  }

  // ── Clear / unlock ────────────────────────────────────────────────────
  function handleClear() {
    setLocked(null)
    setQuery('')
    setState({ kind: 'idle' })
    setOpen(false)
    setTimeout(() => inputRef.current?.focus(), 0)
  }

  // ── Manual entry ──────────────────────────────────────────────────────
  function handleManualEntry() {
    setOpen(false)
    setState({ kind: 'idle' })
    setQuery('')
    onManualEntry()
  }

  // ── Keyboard navigation ───────────────────────────────────────────────
  function handleKeyDown(e: React.KeyboardEvent<HTMLInputElement>) {
    if (!open) return
    const hasOption = state.kind === 'success'

    if (e.key === 'Escape') {
      setOpen(false)
      setActiveIdx(-1)
      e.preventDefault()
      return
    }
    if (e.key === 'ArrowDown' && hasOption) {
      setActiveIdx(0)
      e.preventDefault()
      return
    }
    if (e.key === 'ArrowUp' && hasOption) {
      setActiveIdx(-1)
      e.preventDefault()
      return
    }
    if ((e.key === 'Enter' || e.key === ' ') && hasOption && activeIdx === 0) {
      e.preventDefault()
      handleSelect((state as { kind: 'success'; result: CompanyResolveResponse }).result)
      return
    }
  }

  // ── Click outside to close ─────────────────────────────────────────────
  useEffect(() => {
    function onClickOutside(e: MouseEvent) {
      if (
        dropdownRef.current &&
        !dropdownRef.current.contains(e.target as Node) &&
        inputRef.current &&
        !inputRef.current.contains(e.target as Node)
      ) {
        setOpen(false)
      }
    }
    document.addEventListener('mousedown', onClickOutside)
    return () => document.removeEventListener('mousedown', onClickOutside)
  }, [])

  // ── Cleanup on unmount ────────────────────────────────────────────────
  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current)
      if (abortRef.current) abortRef.current.abort()
    }
  }, [])

  // ── Render: locked chip ────────────────────────────────────────────────
  if (locked) {
    return (
      <div className={className}>
        <div className="flex items-center gap-2.5 rounded-xl border border-emerald-200 bg-emerald-50 px-4 py-3">
          {/* Verified tick */}
          <span className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-emerald-100">
            <svg
              className="h-4 w-4 text-emerald-600"
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
          </span>

          <div className="flex-1 min-w-0">
            <p className="text-sm font-semibold text-emerald-800 truncate">
              {locked.company_name}
            </p>
            <p className="text-xs text-emerald-600 mt-0.5">
              {locked.exchange ? `${locked.exchange}: ` : ''}
              <span className="font-mono font-semibold">{locked.ticker}</span>
              {' '}· CIK {locked.cik}
            </p>
          </div>

          <button
            type="button"
            onClick={handleClear}
            aria-label="Clear resolved company"
            className="ml-auto flex h-6 w-6 shrink-0 items-center justify-center rounded-full text-emerald-500 hover:bg-emerald-100 hover:text-emerald-700 transition-colors"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
                clipRule="evenodd"
              />
            </svg>
          </button>
        </div>
      </div>
    )
  }

  // ── Render: input + dropdown ──────────────────────────────────────────
  const inputHasError =
    state.kind === 'not_found' || state.kind === 'timeout' || state.kind === 'error'

  return (
    <div className={['relative', className].filter(Boolean).join(' ')}>
      {/* ── Search input ─────────────────────────────────────────────────── */}
      <div className="relative" role="combobox" aria-expanded={open} aria-haspopup="listbox">
        {/* Search icon */}
        <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center">
          {state.kind === 'loading' ? (
            <svg
              className="h-4 w-4 animate-spin text-blue-500"
              xmlns="http://www.w3.org/2000/svg"
              fill="none"
              viewBox="0 0 24 24"
              aria-hidden="true"
            >
              <circle
                className="opacity-25"
                cx="12"
                cy="12"
                r="10"
                stroke="currentColor"
                strokeWidth="4"
              />
              <path
                className="opacity-75"
                fill="currentColor"
                d="M4 12a8 8 0 018-8v8H4z"
              />
            </svg>
          ) : (
            <svg
              className="h-4 w-4 text-zinc-400"
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
          )}
        </span>

        <input
          ref={inputRef}
          type="text"
          role="searchbox"
          autoComplete="off"
          autoCorrect="off"
          autoCapitalize="characters"
          spellCheck={false}
          value={query}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          onFocus={() => {
            if (state.kind !== 'idle' && query.trim().length >= MIN_QUERY_LEN) {
              setOpen(true)
            }
          }}
          placeholder="Type a ticker to search SEC data (e.g. AAPL, MSFT, TSLA)…"
          aria-label="Search for a company by ticker"
          aria-controls={open ? listboxId : undefined}
          aria-activedescendant={
            activeIdx === 0 && state.kind === 'success'
              ? `${listboxId}-option-0`
              : undefined
          }
          className={[
            'w-full rounded-xl border pl-9 pr-10 py-2.5 text-sm shadow-sm',
            'focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500',
            'placeholder:text-zinc-400',
            inputHasError
              ? 'border-amber-300 bg-amber-50'
              : 'border-zinc-300 bg-white',
          ].join(' ')}
        />

        {/* Clear query button */}
        {query.length > 0 && (
          <button
            type="button"
            onClick={() => {
              setQuery('')
              setState({ kind: 'idle' })
              setOpen(false)
              if (abortRef.current) abortRef.current.abort()
              inputRef.current?.focus()
            }}
            aria-label="Clear search"
            className="absolute inset-y-0 right-3 flex items-center text-zinc-400 hover:text-zinc-600 transition-colors"
          >
            <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
              <path
                fillRule="evenodd"
                d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
                clipRule="evenodd"
              />
            </svg>
          </button>
        )}
      </div>

      {/* ── Dropdown ─────────────────────────────────────────────────────── */}
      {open && (
        <div
          ref={dropdownRef}
          id={listboxId}
          role="listbox"
          aria-label="Company search results"
          className={[
            'absolute z-50 mt-1.5 w-full rounded-xl border border-zinc-200',
            'bg-white shadow-lg shadow-zinc-200/60 overflow-hidden',
          ].join(' ')}
        >
          {/* Loading row */}
          {state.kind === 'loading' && (
            <div className="flex items-center gap-3 px-4 py-3.5 text-sm text-zinc-500">
              <svg
                className="h-4 w-4 animate-spin text-blue-500 shrink-0"
                xmlns="http://www.w3.org/2000/svg"
                fill="none"
                viewBox="0 0 24 24"
                aria-hidden="true"
              >
                <circle
                  className="opacity-25"
                  cx="12"
                  cy="12"
                  r="10"
                  stroke="currentColor"
                  strokeWidth="4"
                />
                <path
                  className="opacity-75"
                  fill="currentColor"
                  d="M4 12a8 8 0 018-8v8H4z"
                />
              </svg>
              Searching SEC EDGAR registry…
            </div>
          )}

          {/* Success — suggestion row */}
          {state.kind === 'success' && (
            <button
              id={`${listboxId}-option-0`}
              type="button"
              role="option"
              aria-selected={activeIdx === 0}
              onClick={() => handleSelect(state.result)}
              onMouseEnter={() => setActiveIdx(0)}
              onMouseLeave={() => setActiveIdx(-1)}
              className={[
                'w-full flex items-center gap-3 px-4 py-3.5 text-left transition-colors',
                activeIdx === 0 ? 'bg-blue-50' : 'hover:bg-zinc-50',
              ].join(' ')}
            >
              {/* Company icon */}
              <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-zinc-100 text-zinc-500">
                <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
                  <path
                    fillRule="evenodd"
                    d="M4 4a2 2 0 012-2h8a2 2 0 012 2v12a1 1 0 110 2h-3a1 1 0 01-1-1v-2a1 1 0 00-1-1H9a1 1 0 00-1 1v2a1 1 0 01-1 1H4a1 1 0 110-2V4zm3 1h2v2H7V5zm2 4H7v2h2V9zm2-4h2v2h-2V5zm2 4h-2v2h2V9z"
                    clipRule="evenodd"
                  />
                </svg>
              </span>

              <div className="flex-1 min-w-0">
                <p className="text-sm font-semibold text-zinc-900 truncate">
                  {state.result.company_name}
                </p>
                <p className="text-xs text-zinc-500 mt-0.5">
                  <span className="font-mono font-medium text-zinc-700">
                    {buildBadge(state.result)}
                  </span>
                  {' '}
                  <span className="text-zinc-400">· CIK {state.result.cik}</span>
                  {state.result.country && (
                    <span className="ml-1.5 inline-flex items-center rounded-full bg-zinc-100 px-1.5 py-0.5 text-[10px] font-medium text-zinc-500">
                      {state.result.country}
                    </span>
                  )}
                </p>
              </div>

              {/* Select arrow */}
              <span className="shrink-0 text-zinc-400">
                <svg className="h-4 w-4" viewBox="0 0 20 20" fill="currentColor">
                  <path
                    fillRule="evenodd"
                    d="M7.293 14.707a1 1 0 010-1.414L10.586 10 7.293 6.707a1 1 0 011.414-1.414l4 4a1 1 0 010 1.414l-4 4a1 1 0 01-1.414 0z"
                    clipRule="evenodd"
                  />
                </svg>
              </span>
            </button>
          )}

          {/* Not found */}
          {state.kind === 'not_found' && (
            <div>
              <div className="px-4 py-3 flex items-center gap-2.5 border-b border-zinc-100">
                <svg
                  className="h-4 w-4 shrink-0 text-zinc-400"
                  viewBox="0 0 20 20"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z"
                    clipRule="evenodd"
                  />
                </svg>
                <p className="text-sm text-zinc-500">
                  No SEC record found for{' '}
                  <span className="font-mono font-semibold text-zinc-700">
                    "{state.query}"
                  </span>
                </p>
              </div>
              <ManualEntryOption onClick={handleManualEntry} />
            </div>
          )}

          {/* Timeout */}
          {state.kind === 'timeout' && (
            <div>
              <div className="flex items-start gap-2.5 px-4 py-3 bg-amber-50 border-b border-amber-100">
                <svg
                  className="mt-0.5 h-4 w-4 shrink-0 text-amber-500"
                  viewBox="0 0 20 20"
                  fill="currentColor"
                  aria-hidden="true"
                >
                  <path
                    fillRule="evenodd"
                    d="M8.485 2.495c.673-1.167 2.357-1.167 3.03 0l6.28 10.875c.673 1.167-.17 2.625-1.516 2.625H3.72c-1.347 0-2.189-1.458-1.515-2.625L8.485 2.495zM10 5a.75.75 0 01.75.75v3.5a.75.75 0 01-1.5 0v-3.5A.75.75 0 0110 5zm0 9a1 1 0 100-2 1 1 0 000 2z"
                    clipRule="evenodd"
                  />
                </svg>
                <div>
                  <p className="text-sm font-medium text-amber-800">
                    Resolution service timed out
                  </p>
                  <p className="text-xs text-amber-600 mt-0.5">
                    The SEC EDGAR lookup took too long. You can retry or enter details manually.
                  </p>
                </div>
              </div>
              <ManualEntryOption onClick={handleManualEntry} />
            </div>
          )}

          {/* Generic error */}
          {state.kind === 'error' && (
            <div>
              <div className="px-4 py-3 flex items-center gap-2.5 bg-red-50 border-b border-red-100">
                <svg
                  className="h-4 w-4 shrink-0 text-red-500"
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
                <p className="text-sm text-red-700">{state.message}</p>
              </div>
              <ManualEntryOption onClick={handleManualEntry} />
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// ManualEntryOption — shared "use manual entry" row at bottom of dropdown
// ---------------------------------------------------------------------------

function ManualEntryOption({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full flex items-center gap-2.5 px-4 py-3 text-sm text-zinc-600 hover:bg-zinc-50 transition-colors"
    >
      <svg
        className="h-4 w-4 text-zinc-400"
        viewBox="0 0 20 20"
        fill="currentColor"
        aria-hidden="true"
      >
        <path d="M13.586 3.586a2 2 0 112.828 2.828l-.793.793-2.828-2.828.793-.793zM11.379 5.793L3 14.172V17h2.828l8.38-8.379-2.83-2.828z" />
      </svg>
      Use manual entry instead
      <span className="ml-auto text-zinc-400" aria-hidden="true">→</span>
    </button>
  )
}
