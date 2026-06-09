'use client'

import { useTransition } from 'react'
import { logout } from '@/app/actions/auth'

export function LogoutButton() {
  const [pending, startTransition] = useTransition()

  return (
    <button
      onClick={() => startTransition(() => logout())}
      disabled={pending}
      className="w-full rounded-lg px-3 py-2 text-left text-sm font-medium text-zinc-500 hover:bg-zinc-50 hover:text-zinc-900 transition-colors disabled:opacity-50"
    >
      {pending ? 'Signing out…' : 'Sign out'}
    </button>
  )
}
