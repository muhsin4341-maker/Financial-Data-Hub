'use client'

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'
import { cancelJob } from '@/app/actions/jobs'
import { Button } from '@/app/_components/ui'

export function CancelJobButton({ jobId }: { jobId: string }) {
  const router = useRouter()
  const [pending, startTransition] = useTransition()
  const [error, setError] = useState<string | null>(null)

  async function handleCancel() {
    setError(null)
    startTransition(async () => {
      try {
        await cancelJob(jobId)
        router.refresh()
      } catch (e: unknown) {
        const err = e as { message?: string }
        setError(err.message ?? 'Failed to cancel job.')
      }
    })
  }

  return (
    <div>
      <Button variant="danger" size="sm" loading={pending} onClick={handleCancel}>
        Cancel job
      </Button>
      {error && <p className="mt-1 text-xs text-red-600">{error}</p>}
    </div>
  )
}
