'use client'

import { useTransition } from 'react'
import { useRouter } from 'next/navigation'
import { Button } from '@/app/_components/ui'

export function RefreshButton() {
  const router = useRouter()
  const [pending, startTransition] = useTransition()

  return (
    <Button
      variant="secondary"
      size="sm"
      loading={pending}
      onClick={() => startTransition(() => router.refresh())}
    >
      Refresh
    </Button>
  )
}
