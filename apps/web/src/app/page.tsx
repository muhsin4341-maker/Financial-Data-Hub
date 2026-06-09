import { redirect } from 'next/navigation'

/**
 * Root route — proxy.ts handles auth-aware redirects.
 * Unauthenticated users land on /login; authenticated users on /dashboard.
 * This server-side redirect covers direct renders without proxy context.
 */
export default function RootPage() {
  redirect('/dashboard')
}
