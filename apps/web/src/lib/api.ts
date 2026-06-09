/**
 * Axios API client for client-side requests.
 *
 * The JWT access token is stored in a cookie named `fdh_token` (set by the
 * login/register Server Actions). The interceptor reads it on every request
 * so the token is always current without requiring a context re-render.
 *
 * Usage (Client Components only — use fetch() in Server Components):
 *   import { api } from '@/lib/api'
 *   const companies = await api.get<CompanyListResponse>('/api/v1/companies')
 */

import axios, { AxiosError } from 'axios'
import type { ApiErrorResponse } from '@/lib/types'

const BASE_URL = process.env.NEXT_PUBLIC_API_URL ?? 'http://localhost:8000'

function getTokenFromCookie(): string | null {
  if (typeof document === 'undefined') return null
  const match = document.cookie.match(/(?:^|;\s*)fdh_token=([^;]*)/)
  return match ? decodeURIComponent(match[1]) : null
}

export const api = axios.create({
  baseURL: BASE_URL,
  headers: { 'Content-Type': 'application/json' },
  withCredentials: false,
})

// Attach Bearer token from cookie before every request.
api.interceptors.request.use((config) => {
  const token = getTokenFromCookie()
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// Normalise errors into a predictable shape.
api.interceptors.response.use(
  (response) => response,
  (error: AxiosError<ApiErrorResponse>) => {
    const status = error.response?.status
    const apiError = error.response?.data?.error

    // Let callers inspect the status code.
    const enriched = Object.assign(error, {
      statusCode: status,
      apiCode: apiError?.code,
      apiMessage: apiError?.message ?? error.message,
    })
    return Promise.reject(enriched)
  },
)

/** Convenience: extract typed data from an axios response. */
export async function apiGet<T>(path: string): Promise<T> {
  const res = await api.get<T>(path)
  return res.data
}

export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  const res = await api.post<T>(path, body)
  return res.data
}

export async function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  const res = await api.patch<T>(path, body)
  return res.data
}

export async function apiDelete<T = void>(path: string): Promise<T> {
  const res = await api.delete<T>(path)
  return res.data
}
