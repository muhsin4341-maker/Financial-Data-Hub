'use server'

/**
 * Invitation Server Actions.
 */

import * as z from 'zod'
import { serverGet, serverPost } from '@/lib/server-api'
import type { Invitation } from '@/lib/types'

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type InviteFormState =
  | { errors?: Record<string, string[]>; message?: string; success?: false }
  | { success: true }
  | undefined

// ---------------------------------------------------------------------------
// Schema
// ---------------------------------------------------------------------------

const SendInvitationSchema = z.object({
  email: z.email({ error: 'Please enter a valid email address.' }),
  role: z.enum(['viewer', 'analyst', 'admin'], {
    error: 'Please select a valid role.',
  }),
})

// ---------------------------------------------------------------------------
// Actions
// ---------------------------------------------------------------------------

export async function sendInvitation(
  _prev: InviteFormState,
  formData: FormData,
): Promise<InviteFormState> {
  const raw = {
    email: formData.get('email') as string,
    role: formData.get('role') as string,
  }

  const validated = SendInvitationSchema.safeParse(raw)
  if (!validated.success) {
    return { errors: validated.error.flatten().fieldErrors as Record<string, string[]> }
  }

  try {
    await serverPost<Invitation>('/api/v1/invitations', {
      email: validated.data.email,
      role: validated.data.role,
    })
    return { success: true }
  } catch (e: unknown) {
    const err = e as { message?: string }
    return { message: err.message ?? 'Failed to send invitation. Please try again.' }
  }
}

/**
 * Validate an invitation token (public — no auth required).
 * Called on the accept-invitation page to verify the token is usable.
 */
export async function validateInvitationToken(
  token: string,
): Promise<Invitation & { is_usable: boolean }> {
  return serverGet<Invitation & { is_usable: boolean }>(
    `/api/v1/invitations/${encodeURIComponent(token)}`,
  )
}

/**
 * Accept an invitation (requires the accepting user to be logged in).
 */
export async function acceptInvitation(token: string): Promise<void> {
  await serverPost(`/api/v1/invitations/${encodeURIComponent(token)}/accept`)
}
