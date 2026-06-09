'use client'

import { useState, useTransition } from 'react'
import { useRouter } from 'next/navigation'
import { createJob } from '@/app/actions/jobs'
import { Button } from '@/app/_components/ui'

export function CreateJobButton({ companyId }: { companyId: string }) {
  const router = useRouter()
  const [pending, startTransition] = useTransition()
  const [error, setError] = useState<string | null>(null)

  async function handleCreate() {
    setError(null)
    startTransition(async () => {
      try {
        const job = await createJob(companyId)
        router.push(`/jobs/${job.id}`)
      } catch (e: unknown) {
        const err = e as { message?: string }
        setError(err.message ?? 'Failed to create job.')
      }
    })
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <Button size="sm" loading={pending} onClick={handleCreate}>
        + New job
      </Button>
      {error && <p className="text-xs text-red-600">{error}</p>}
    </div>
  )
}
