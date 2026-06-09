'use server'

/**
 * Auth Server Actions — login, register, logout.
 *
 * Called from Client Components via useActionState (React 19).
 * Each action returns a FormState describing success or field errors
 * so the form can display inline feedback without a page reload.
 *
 * On success: set session cookie, redirect to /dashboard.
 * On failure: return error state (no redirect).
 */

import { redirect } from 'next/navigation'
import * as z from 'zod'
import { setSessionToken, deleteSessionToken } from '@/lib/session'
import type { AuthResponse } from '@/lib/types'

// Server actions run inside Docker — use internal service name, not localhost
const API_BASE = process.env.API_INTERNAL_URL ?? process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

// ---------------------------------------------------------------------------
// Shared form-state type
// ---------------------------------------------------------------------------

export type FormState = {
  errors?: Record<string, string[]>
  message?: string
} | undefined

// ---------------------------------------------------------------------------
// Zod schemas (Zod v4 — uses { error: '...' } not { message: '...' })
// ---------------------------------------------------------------------------

const LoginSchema = z.object({
  email: z.email({ error: 'Please enter a valid email address.' }),
  password: z.string().min(1, { error: 'Password is required.' }),
})

const RegisterSchema = z.object({
  email: z.email({ error: 'Please enter a valid email address.' }),
  password: z
    .string()
    .min(12, { error: 'Password must be at least 12 characters.' })
    .regex(/[A-Z]/, { error: 'Password must contain at least one uppercase letter.' })
    .regex(/[a-z]/, { error: 'Password must contain at least one lowercase letter.' })
    .regex(/\d/, { error: 'Password must contain at least one number.' })
    .regex(/[^A-Za-z0-9]/, { error: 'Password must contain at least one special character.' }),
  full_name: z.string().min(1, { error: 'Full name is required.' }).trim(),
  workspace_name: z.string().min(1, { error: 'Workspace name is required.' }).trim(),
})

// ---------------------------------------------------------------------------
// Internal helpers
// ---------------------------------------------------------------------------

async function callApi<T>(
  path: string,
  body: unknown,
): Promise<{ data: T; error: null } | { data: null; error: string }> {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    })
    const json = await res.json()
    if (!res.ok) {
      return {
        data: null,
        error: json?.error?.message ?? `Request failed (${res.status})`,
      }
    }
    return { data: json as T, error: null }
  } catch {
    return { data: null, error: 'Could not reach the server. Please try again.' }
  }
}

// ---------------------------------------------------------------------------
// Login
// ---------------------------------------------------------------------------

export async function login(
  _prevState: FormState,
  formData: FormData,
): Promise<FormState> {
  const raw = {
    email: formData.get('email') as string,
    password: formData.get('password') as string,
  }

  const validated = LoginSchema.safeParse(raw)
  if (!validated.success) {
    return { errors: validated.error.flatten().fieldErrors }
  }

  const result = await callApi<AuthResponse>('/api/v1/auth/login', {
    email: validated.data.email,
    password: validated.data.password,
  })

  if (result.error || !result.data) {
    return { message: result.error ?? 'Login failed.' }
  }

  await setSessionToken(result.data.access_token)
  redirect('/dashboard')
}

// ---------------------------------------------------------------------------
// Register
// ---------------------------------------------------------------------------

export async function register(
  _prevState: FormState,
  formData: FormData,
): Promise<FormState> {
  const raw = {
    email: formData.get('email') as string,
    password: formData.get('password') as string,
    full_name: formData.get('full_name') as string,
    workspace_name: formData.get('workspace_name') as string,
  }

  const validated = RegisterSchema.safeParse(raw)
  if (!validated.success) {
    return { errors: validated.error.flatten().fieldErrors }
  }

  const result = await callApi<AuthResponse>('/api/v1/auth/register', {
    email: validated.data.email,
    password: validated.data.password,
    full_name: validated.data.full_name,
    workspace_name: validated.data.workspace_name,
  })

  if (result.error || !result.data) {
    return { message: result.error ?? 'Registration failed.' }
  }

  await setSessionToken(result.data.access_token)
  redirect('/dashboard')
}

// ---------------------------------------------------------------------------
// Logout
// ---------------------------------------------------------------------------

export async function logout(): Promise<void> {
  await deleteSessionToken()
  redirect('/login')
}
