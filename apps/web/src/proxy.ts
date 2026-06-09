/**
 * Next.js 16 Proxy (formerly Middleware).
 *
 * In Next.js 16, middleware was renamed to "Proxy". The file must be named
 * `proxy.ts` (not `middleware.ts`). See:
 *   node_modules/next/dist/docs/01-app/01-getting-started/16-proxy.md
 *
 * Performs optimistic auth checks only — reads cookie presence.
 * Does NOT make database or API calls (would be too slow on every request).
 *
 * Protected routes:  all routes under /dashboard, /companies, /jobs,
 *                    /invite/accept, /settings
 * Public routes:     /login, /register, /
 */

import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

const TOKEN_COOKIE = 'fdh_token'

const PUBLIC_PATHS = new Set(['/', '/login', '/register'])

function isPublicPath(pathname: string): boolean {
  return PUBLIC_PATHS.has(pathname)
}

function isProtectedPath(pathname: string): boolean {
  return (
    pathname.startsWith('/dashboard') ||
    pathname.startsWith('/companies') ||
    pathname.startsWith('/jobs') ||
    pathname.startsWith('/invite') ||
    pathname.startsWith('/settings')
  )
}

export default function proxy(request: NextRequest): NextResponse {
  const { pathname } = request.nextUrl
  const token = request.cookies.get(TOKEN_COOKIE)?.value

  // Unauthenticated user tries to access a protected route → /login
  if (isProtectedPath(pathname) && !token) {
    const loginUrl = new URL('/login', request.nextUrl)
    loginUrl.searchParams.set('next', pathname)
    return NextResponse.redirect(loginUrl)
  }

  // Authenticated user tries to access login/register → /dashboard
  if (isPublicPath(pathname) && token && pathname !== '/') {
    return NextResponse.redirect(new URL('/dashboard', request.nextUrl))
  }

  return NextResponse.next()
}

export const config = {
  // Run on all paths except Next.js internals and static files.
  matcher: ['/((?!_next/static|_next/image|favicon\\.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp)$).*)'],
}
