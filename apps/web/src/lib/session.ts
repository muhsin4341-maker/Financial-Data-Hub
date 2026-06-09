/**
 * Server-side session helpers — cookie read/write.
 *
 * Only call these from Server Actions, Route Handlers, or Server Components.
 * Uses `next/headers` cookies() API which is async in Next.js 16.
 *
 * Cookie: `fdh_token`
 *   - httpOnly: false  (must be readable by client-side axios interceptor)
 *   - sameSite: lax
 *   - secure: production only
 *   - path: /
 *   - maxAge: 15 minutes (matches JWT access token lifetime)
 */

import { cookies } from 'next/headers'

const COOKIE_NAME = 'fdh_token'
/** JWT access token lifetime matches backend setting (15 min). */
const TOKEN_MAX_AGE_SECONDS = 15 * 60

export async function setSessionToken(token: string): Promise<void> {
  const cookieStore = await cookies()
  cookieStore.set(COOKIE_NAME, token, {
    httpOnly: false,            // client-side axios must read it
    secure: process.env.NODE_ENV === 'production',
    sameSite: 'lax',
    path: '/',
    maxAge: TOKEN_MAX_AGE_SECONDS,
  })
}

export async function getSessionToken(): Promise<string | null> {
  const cookieStore = await cookies()
  return cookieStore.get(COOKIE_NAME)?.value ?? null
}

export async function deleteSessionToken(): Promise<void> {
  const cookieStore = await cookies()
  cookieStore.delete(COOKIE_NAME)
}

/**
 * Build Authorization header value for server-side fetch calls.
 * Returns undefined if no token is present so callers can skip the header.
 */
export async function getAuthHeader(): Promise<Record<string, string>> {
  const token = await getSessionToken()
  if (!token) return {}
  return { Authorization: `Bearer ${token}` }
}
