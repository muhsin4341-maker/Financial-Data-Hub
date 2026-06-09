'use client'

import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { clsx } from 'clsx'

const links = [
  { href: '/dashboard',         label: 'Dashboard' },
  { href: '/companies',         label: 'Companies' },
  { href: '/acquisition/jobs',  label: 'Pipeline' },
  { href: '/settings/sources',  label: 'Sources' },
  { href: '/settings/team',     label: 'Team' },
]

export function NavLinks() {
  const pathname = usePathname()

  return (
    <ul className="flex flex-col gap-1">
      {links.map(({ href, label }) => {
        const active =
          href === '/dashboard'
            ? pathname === '/dashboard'
            : pathname.startsWith(href)

        return (
          <li key={href}>
            <Link
              href={href}
              className={clsx(
                'flex items-center rounded-lg px-3 py-2 text-sm font-medium transition-colors',
                active
                  ? 'bg-blue-50 text-blue-700'
                  : 'text-zinc-600 hover:bg-zinc-50 hover:text-zinc-900',
              )}
            >
              {label}
            </Link>
          </li>
        )
      })}
    </ul>
  )
}
