'use client'

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'
import type { Invitation } from '@/lib/types'
import { Badge, Button } from '@/app/_components/ui'

function statusVariant(status: string): 'default' | 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, 'default' | 'success' | 'warning' | 'danger' | 'info'> = {
    pending: 'info',
    accepted: 'success',
    cancelled: 'warning',
    expired: 'default',
  }
  return map[status] ?? 'default'
}

function InvitationRow({ invitation }: { invitation: Invitation }) {
  const router = useRouter()
  const [pending, startTransition] = useTransition()
  const [error, setError] = useState<string | null>(null)

  async function handleAction(action: 'resend' | 'cancel') {
    setError(null)
    startTransition(async () => {
      try {
        if (action === 'resend') {
          // Token is not stored client-side; resend requires calling the server.
          // For now, show a message indicating this should be done server-side.
          // In a full implementation, we'd store the token reference in a separate call.
          setError('Resend is available via admin API. Token not available client-side.')
          return
        }
        // Cancel: same limitation — we don't have the raw token client-side.
        // Show informational error.
        setError('Cancel is available via admin API. Token not available client-side.')
        router.refresh()
      } catch (e: unknown) {
        const err = e as { apiMessage?: string }
        setError(err.apiMessage ?? `Failed to ${action}.`)
      }
    })
  }

  return (
    <tr className="border-b border-zinc-100 last:border-0">
      <td className="px-4 py-3 text-sm text-zinc-900">{invitation.invitee_email}</td>
      <td className="px-4 py-3">
        <Badge variant="info">{invitation.role}</Badge>
      </td>
      <td className="px-4 py-3">
        <Badge variant={statusVariant(invitation.status)}>{invitation.status}</Badge>
      </td>
      <td className="px-4 py-3 text-sm text-zinc-500">
        {new Date(invitation.expires_at).toLocaleDateString()}
      </td>
      <td className="px-4 py-3">
        {invitation.status === 'pending' && (
          <div className="flex items-center gap-2">
            <Button
              size="sm"
              variant="ghost"
              loading={pending}
              onClick={() => handleAction('resend')}
            >
              Resend
            </Button>
            <Button
              size="sm"
              variant="danger"
              loading={pending}
              onClick={() => handleAction('cancel')}
            >
              Cancel
            </Button>
          </div>
        )}
        {error && <p className="text-xs text-red-600 mt-1">{error}</p>}
      </td>
    </tr>
  )
}

export function InvitationsTable({
  invitations,
}: {
  invitations: Invitation[]
}) {
  if (invitations.length === 0) {
    return (
      <p className="text-sm text-zinc-500 py-4 text-center">
        No invitations sent yet.
      </p>
    )
  }

  return (
    <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-zinc-100 bg-zinc-50">
            <th className="px-4 py-3 text-left font-medium text-zinc-600">Email</th>
            <th className="px-4 py-3 text-left font-medium text-zinc-600">Role</th>
            <th className="px-4 py-3 text-left font-medium text-zinc-600">Status</th>
            <th className="px-4 py-3 text-left font-medium text-zinc-600">Expires</th>
            <th className="px-4 py-3" />
          </tr>
        </thead>
        <tbody>
          {invitations.map((inv) => (
            <InvitationRow key={inv.id} invitation={inv} />
          ))}
        </tbody>
      </table>
    </div>
  )
}
