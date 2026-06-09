'use client'

/**
 * Document upload component — integrates with the S3 pre-signed URL flow.
 *
 * Flow:
 *   1. User selects a file.
 *   2. Server Action: getUploadUrl() → POST /api/v1/jobs/{id}/upload-url (authenticated).
 *   3. Client-side: PUT file directly to S3 using the pre-signed URL (no auth needed).
 *   4. Server Action: completeUpload() → POST /api/v1/jobs/{id}/upload-complete (authenticated).
 *   5. Router refresh re-loads the server component data.
 */

import { useState, useRef, useTransition } from 'react'
import { useRouter } from 'next/navigation'
import axios from 'axios'
import { getUploadUrl, completeUpload } from '@/app/actions/jobs'
import { Button } from '@/app/_components/ui'

interface Props {
  jobId: string
  currentDocumentUrl: string | null
}

type UploadPhase = 'idle' | 'requesting-url' | 'uploading' | 'completing' | 'done'

export function UploadDocument({ jobId, currentDocumentUrl }: Props) {
  const router = useRouter()
  const fileRef = useRef<HTMLInputElement>(null)
  const [phase, setPhase] = useState<UploadPhase>('idle')
  const [progress, setProgress] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const [, startTransition] = useTransition()

  async function handleUpload(file: File) {
    setError(null)
    setProgress(0)

    try {
      // Step 1: Get pre-signed PUT URL via Server Action (authenticated).
      setPhase('requesting-url')
      const { url, key } = await getUploadUrl(jobId, file.name)

      // Step 2: Upload directly to S3 — pre-signed URL is self-authenticated,
      // do NOT add our Bearer token to this request.
      setPhase('uploading')
      await axios.put(url, file, {
        headers: { 'Content-Type': 'application/octet-stream' },
        onUploadProgress: (e) => {
          if (e.total) setProgress(Math.round((e.loaded / e.total) * 100))
        },
        transformRequest: [(data) => data],
      })

      // Step 3: Confirm upload via Server Action (authenticated).
      setPhase('completing')
      await completeUpload(jobId, key)

      setPhase('done')
      setProgress(100)

      // Refresh server component data to show updated document_url.
      startTransition(() => router.refresh())
    } catch (e: unknown) {
      setPhase('idle')
      const err = e as { message?: string }
      setError(err.message ?? 'Upload failed. Please try again.')
    }
  }

  function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (file) void handleUpload(file)
  }

  const phaseLabel: Record<UploadPhase, string> = {
    idle: currentDocumentUrl ? 'Replace document' : 'Upload document',
    'requesting-url': 'Preparing upload…',
    uploading: `Uploading… ${progress}%`,
    completing: 'Finalising…',
    done: 'Upload complete',
  }

  const busy = phase !== 'idle' && phase !== 'done'

  return (
    <div className="flex flex-col gap-3">
      {currentDocumentUrl && (
        <p className="text-sm text-zinc-600">
          <span className="font-medium">Current document:</span>{' '}
          <span className="font-mono text-xs break-all">
            {currentDocumentUrl.split('/').pop() ?? currentDocumentUrl}
          </span>
        </p>
      )}

      {phase === 'uploading' && (
        <div className="w-full bg-zinc-100 rounded-full h-2 overflow-hidden">
          <div
            className="bg-blue-600 h-2 rounded-full transition-all duration-200"
            style={{ width: `${progress}%` }}
          />
        </div>
      )}

      {error && <p className="text-sm text-red-600">{error}</p>}

      <input
        ref={fileRef}
        type="file"
        className="hidden"
        accept=".pdf,.html,.xbrl,.xml"
        onChange={handleFileChange}
        disabled={busy}
      />

      <Button
        variant={phase === 'done' ? 'secondary' : 'primary'}
        size="sm"
        loading={busy}
        onClick={() => fileRef.current?.click()}
        disabled={busy}
      >
        {phaseLabel[phase]}
      </Button>
    </div>
  )
}
