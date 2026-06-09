'use client'

/**
 * Minimal shared UI primitives.
 *
 * Purpose-built for the M2 scope — no external component library dependency.
 * All components are thin wrappers with consistent styling derived from the
 * installed Tailwind v4 and Radix UI primitives in package.json.
 */

import { clsx } from 'clsx'
import * as React from 'react'

// ---------------------------------------------------------------------------
// Button
// ---------------------------------------------------------------------------

interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
  size?: 'sm' | 'md' | 'lg'
  loading?: boolean
}

export function Button({
  variant = 'primary',
  size = 'md',
  loading = false,
  disabled,
  className,
  children,
  ...props
}: ButtonProps) {
  const base =
    'inline-flex items-center justify-center font-medium rounded-lg transition-colors focus:outline-none focus:ring-2 focus:ring-offset-2 disabled:opacity-50 disabled:cursor-not-allowed'

  const variants = {
    primary:
      'bg-blue-600 text-white hover:bg-blue-700 focus:ring-blue-500',
    secondary:
      'bg-white text-zinc-700 border border-zinc-300 hover:bg-zinc-50 focus:ring-zinc-400',
    danger:
      'bg-red-600 text-white hover:bg-red-700 focus:ring-red-500',
    ghost:
      'text-zinc-600 hover:bg-zinc-100 focus:ring-zinc-400',
  }

  const sizes = {
    sm: 'px-3 py-1.5 text-sm gap-1.5',
    md: 'px-4 py-2 text-sm gap-2',
    lg: 'px-5 py-2.5 text-base gap-2',
  }

  return (
    <button
      {...props}
      disabled={disabled ?? loading}
      className={clsx(base, variants[variant], sizes[size], className)}
    >
      {loading && (
        <svg
          className="animate-spin -ml-0.5 h-4 w-4"
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
      )}
      {children}
    </button>
  )
}

// ---------------------------------------------------------------------------
// Input
// ---------------------------------------------------------------------------

interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  label: string
  error?: string[]
}

export function Input({ label, error, id, className, ...props }: InputProps) {
  const inputId = id ?? label.toLowerCase().replace(/\s+/g, '-')
  return (
    <div className="flex flex-col gap-1">
      <label
        htmlFor={inputId}
        className="text-sm font-medium text-zinc-700"
      >
        {label}
      </label>
      <input
        id={inputId}
        {...props}
        className={clsx(
          'w-full rounded-lg border px-3 py-2 text-sm shadow-sm',
          'focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500',
          error?.length
            ? 'border-red-400 bg-red-50'
            : 'border-zinc-300 bg-white',
          className,
        )}
      />
      {error?.map((e) => (
        <p key={e} className="text-xs text-red-600">
          {e}
        </p>
      ))}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Alert
// ---------------------------------------------------------------------------

interface AlertProps {
  variant?: 'error' | 'success' | 'info'
  children: React.ReactNode
}

export function Alert({ variant = 'error', children }: AlertProps) {
  const styles = {
    error: 'bg-red-50 border-red-300 text-red-700',
    success: 'bg-green-50 border-green-300 text-green-700',
    info: 'bg-blue-50 border-blue-300 text-blue-700',
  }
  return (
    <div
      role="alert"
      className={clsx(
        'rounded-lg border px-4 py-3 text-sm',
        styles[variant],
      )}
    >
      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Card
// ---------------------------------------------------------------------------

export function Card({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <div
      className={clsx(
        'rounded-xl border border-zinc-200 bg-white shadow-sm',
        className,
      )}
    >
      {children}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Spinner
// ---------------------------------------------------------------------------

export function Spinner({ className }: { className?: string }) {
  return (
    <svg
      className={clsx('animate-spin h-5 w-5 text-blue-600', className)}
      xmlns="http://www.w3.org/2000/svg"
      fill="none"
      viewBox="0 0 24 24"
      aria-label="Loading"
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
  )
}

// ---------------------------------------------------------------------------
// Badge
// ---------------------------------------------------------------------------

interface BadgeProps {
  children: React.ReactNode
  variant?: 'default' | 'success' | 'warning' | 'danger' | 'info'
}

export function Badge({ children, variant = 'default' }: BadgeProps) {
  const styles = {
    default: 'bg-zinc-100 text-zinc-700',
    success: 'bg-green-100 text-green-700',
    warning: 'bg-yellow-100 text-yellow-700',
    danger: 'bg-red-100 text-red-700',
    info: 'bg-blue-100 text-blue-700',
  }
  return (
    <span
      className={clsx(
        'inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium',
        styles[variant],
      )}
    >
      {children}
    </span>
  )
}

// ---------------------------------------------------------------------------
// JobStatusBadge
// ---------------------------------------------------------------------------

import type { JobStatus } from '@/lib/types'

export function JobStatusBadge({ status }: { status: JobStatus }) {
  const map: Record<JobStatus, BadgeProps['variant']> = {
    pending: 'default',
    queued: 'info',
    running: 'info',
    completed: 'success',
    failed: 'danger',
    cancelled: 'warning',
  }
  return <Badge variant={map[status]}>{status}</Badge>
}
