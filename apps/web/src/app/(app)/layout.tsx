/**
 * App layout — shared navigation sidebar for authenticated pages.
 *
 * Server Component. Navigation links are static; active state is handled
 * client-side by next/navigation's usePathname in NavLinks.
 */

import Link from 'next/link'
import { NavLinks } from './_components/nav-links'
import { LogoutButton } from './_components/logout-button'

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-screen">
      {/* Sidebar */}
      <aside className="w-60 shrink-0 border-r border-zinc-200 bg-white flex flex-col">
        <div className="px-6 py-5 border-b border-zinc-100">
          <Link href="/dashboard" className="font-semibold text-zinc-900 tracking-tight">
            Financial Data Hub
          </Link>
        </div>
        <nav className="flex-1 px-3 py-4">
          <NavLinks />
        </nav>
        <div className="px-3 py-4 border-t border-zinc-100">
          <LogoutButton />
        </div>
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-auto bg-zinc-50">
        <div className="mx-auto max-w-5xl px-6 py-8">
          {children}
        </div>
      </main>
    </div>
  )
}
