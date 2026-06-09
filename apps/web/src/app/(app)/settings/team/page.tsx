import type { Metadata } from 'next'
import { serverGet } from '@/lib/server-api'
import type { Invitation } from '@/lib/types'
import { Card } from '@/app/_components/ui'
import { InviteForm } from './_components/invite-form'
import { InvitationsTable } from './_components/invitations-table'
import { RefreshButton } from './_components/refresh-button'

export const metadata: Metadata = { title: 'Team settings' }

export default async function TeamSettingsPage() {
  let invitations: Invitation[] = []
  let error: string | null = null

  try {
    // Backend returns a list — adapt as needed when endpoint is confirmed.
    const raw = await serverGet<Invitation[]>('/api/v1/invitations')
    invitations = Array.isArray(raw) ? raw : []
  } catch {
    error = 'Could not load invitations.'
  }

  return (
    <div className="flex flex-col gap-8 max-w-3xl">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-zinc-900">Team</h1>
          <p className="mt-1 text-sm text-zinc-500">
            Manage team access to this workspace.
          </p>
        </div>
        <RefreshButton />
      </div>

      {/* Invite form */}
      <Card className="p-6">
        <h2 className="text-base font-semibold text-zinc-900 mb-4">
          Invite team member
        </h2>
        <InviteForm />
      </Card>

      {/* Pending invitations */}
      <div className="flex flex-col gap-4">
        <h2 className="text-base font-semibold text-zinc-900">
          Invitations
        </h2>
        {error && (
          <p className="text-sm text-red-600">{error}</p>
        )}
        <InvitationsTable invitations={invitations} />
      </div>
    </div>
  )
}
