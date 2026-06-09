'use client'

import { useActionState, useTransition } from 'react'
import { useRouter } from 'next/navigation'
import { sendInvitation } from '@/app/actions/invitations'
import type { InviteFormState } from '@/app/actions/invitations'
import { Input, Button, Alert } from '@/app/_components/ui'

export function InviteForm() {
  const router = useRouter()
  const [, startTransition] = useTransition()
  const [state, action, pending] = useActionState<InviteFormState, FormData>(
    sendInvitation,
    undefined,
  )

  const errors = state && !state.success ? state.errors : undefined
  const message = state && !state.success ? state.message : undefined

  if (state?.success) {
    startTransition(() => router.refresh())
    return (
      <Alert variant="success">
        Invitation sent! The recipient will receive an email with a link.
      </Alert>
    )
  }

  return (
    <form action={action} className="flex flex-col gap-4">
      {message && <Alert variant="error">{message}</Alert>}

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
        <div className="sm:col-span-2">
          <Input
            label="Email"
            name="email"
            type="email"
            required
            placeholder="colleague@example.com"
            error={errors?.email}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="role" className="text-sm font-medium text-zinc-700">
            Role
          </label>
          <select
            id="role"
            name="role"
            defaultValue="analyst"
            className="rounded-lg border border-zinc-300 bg-white px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="viewer">Viewer</option>
            <option value="analyst">Analyst</option>
            <option value="admin">Admin</option>
          </select>
          {errors?.role?.map((e) => (
            <p key={e} className="text-xs text-red-600">{e}</p>
          ))}
        </div>
      </div>

      <Button type="submit" loading={pending} size="md">
        {pending ? 'Sending…' : 'Send invitation'}
      </Button>
    </form>
  )
}
