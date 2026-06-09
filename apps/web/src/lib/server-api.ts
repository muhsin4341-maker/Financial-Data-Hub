/**
 * Server-side API helpers for use in Server Components and Server Actions.
 *
 * Uses native `fetch` (not axios) so it runs in the Node.js runtime and
 * can read the httpOnly cookie session via getAuthHeader().
 */

import { getAuthHeader } from '@/lib/session'

// Server components/actions run inside Docker — use internal service name
const BASE_URL = process.env.API_INTERNAL_URL ?? process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

async function serverFetch<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const authHeaders = await getAuthHeader()
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...authHeaders,
      ...(options.headers as Record<string, string> | undefined),
    },
  })

  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    const message = body?.error?.message ?? `HTTP ${res.status}`
    const err = new Error(message) as Error & {
      statusCode: number
      apiCode: string
    }
    err.statusCode = res.status
    err.apiCode = body?.error?.code ?? 'UNKNOWN'
    throw err
  }

  if (res.status === 204) return undefined as unknown as T
  return res.json() as Promise<T>
}

export async function serverGet<T>(path: string): Promise<T> {
  return serverFetch<T>(path)
}

export async function serverPost<T>(
  path: string,
  body?: unknown,
): Promise<T> {
  return serverFetch<T>(path, {
    method: 'POST',
    body: body !== undefined ? JSON.stringify(body) : undefined,
  })
}
