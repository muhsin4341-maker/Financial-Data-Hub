import type { Metadata } from 'next'
import { AcceptInvitationForm } from './_components/accept-invitation-form'

export const metadata: Metadata = { title: 'Accept invitation' }

/**
 * /invite/accept?token=...
 *
 * In Next.js 16, searchParams is a Promise — must be awaited.
 */
export default async function AcceptInvitePage(
  props: PageProps<'/invite/accept'>,
) {
  const searchParams = await props.searchParams
  const token = typeof searchParams?.token === 'string' ? searchParams.token : ''

  if (!token) {
    return (
      <div className="text-center py-12">
        <p className="text-zinc-500 text-sm">
          No invitation token provided. Please use the link from your email.
        </p>
      </div>
    )
  }

  return (
    <div className="max-w-md mx-auto py-12">
      <AcceptInvitationForm token={token} />
    </div>
  )
}
