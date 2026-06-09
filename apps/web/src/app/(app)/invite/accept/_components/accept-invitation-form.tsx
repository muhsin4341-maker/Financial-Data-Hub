'use client'

/**
 * Invitation acceptance component.
 *
 * Flow:
 *   1. On mount, validate the token via Server Action → GET /api/v1/invitations/{token}
 *      (the API endpoint is public — no auth required to validate).
 *   2. If valid, show "Accept invitation" button.
 *   3. On accept, call Server Action → POST /api/v1/invitations/{token}/accept
 *      (requires Bearer token — runs as authenticated server action).
 *   4. On success, redirect to /dashboard.
 */

import { useEffect, useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'
import { validateInvitationToken, acceptInvitation } from '@/app/actions/invitations'
import type { Invitation } from '@/lib/types'
import { Button, Alert, Card, Badge } from '@/app/_components/ui'

interface Props {
  token: string
}

export function AcceptInvitationForm({ token }: Props) {
  const router = useRouter()
  const [invitation, setInvitation] = useState<Invitation | null>(null)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [acceptError, setAcceptError] = useState<string | null>(null)
  const [pending, startTransition] = useTransition()

  useEffect(() => {
    validateInvitationToken(token)
      .then(setInvitation)
      .catch((e: unknown) => {
        const err = e as { statusCode?: number; message?: string }
        if (err.statusCode === 404) {
          setLoadError(
            'This invitation link is invalid, has already been used, or has expired.',
          )
        } else {
          setLoadError(err.message ?? 'Failed to validate invitation.')
        }
      })
  }, [token])

  function handleAccept() {
    setAcceptError(null)
    startTransition(async () => {
      try {
        await acceptInvitation(token)
        router.push('/dashboard')
      } catch (e: unknown) {
        const err = e as { statusCode?: number; message?: string }
        if (err.statusCode === 403) {
          setAcceptError(
            'This invitation was sent to a different email address. ' +
              'Please sign in with the account that received the invitation.',
          )
        } else if (err.statusCode === 409) {
          setAcceptError('You are already a member of this workspace.')
        } else {
          setAcceptError(err.message ?? 'Failed to accept invitation.')
        }
      }
    })
  }

  if (loadError) {
    return <Alert variant="error">{loadError}</Alert>
  }

  if (!invitation) {
    return (
      <p className="text-sm text-zinc-500 text-center animate-pulse">
        Validating invitation…
      </p>
    )
  }

  return (
    <Card className="p-6 flex flex-col gap-5">
      <div>
        <h2 className="text-lg font-semibold text-zinc-900">
          You&apos;ve been invited
        </h2>
        <p className="mt-1 text-sm text-zinc-500">
          Accept this invitation to join the workspace.
        </p>
      </div>

      <dl className="grid grid-cols-1 gap-3 text-sm">
        <div>
          <dt className="text-zinc-500">Invited as</dt>
          <dd className="mt-0.5 font-medium text-zinc-900">
            {invitation.invitee_email}
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">Role</dt>
          <dd className="mt-0.5">
            <Badge variant="info">{invitation.role}</Badge>
          </dd>
        </div>
        <div>
          <dt className="text-zinc-500">Expires</dt>
          <dd className="mt-0.5 text-zinc-700">
            {new Date(invitation.expires_at).toLocaleString()}
          </dd>
        </div>
      </dl>

      {acceptError && <Alert variant="error">{acceptError}</Alert>}

      <Button onClick={handleAccept} loading={pending} size="lg">
        {pending ? 'Accepting…' : 'Accept invitation'}
      </Button>
    </Card>
  )
}
