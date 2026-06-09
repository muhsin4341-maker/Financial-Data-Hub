'use server'

/**
 * Job Server Actions — authenticated API calls for the job detail page.
 *
 * Steps 1 and 3 of the document upload flow must be authenticated and
 * therefore run as Server Actions (not client-side axios). Step 2 — the
 * direct PUT to S3 using the pre-signed URL — is client-side only and
 * stays in the component.
 */

import { serverPost } from '@/lib/server-api'
import type { UploadUrlResponse, Job } from '@/lib/types'

/**
 * Step 1: Request a pre-signed S3 PUT URL for a new document upload.
 * Returns the URL (for the client to PUT directly to S3) and the storage key
 * (needed to confirm the upload in step 3).
 */
export async function getUploadUrl(
  jobId: string,
  filename: string,
): Promise<UploadUrlResponse> {
  return serverPost<UploadUrlResponse>(`/api/v1/jobs/${jobId}/upload-url`, {
    filename,
  })
}

/**
 * Step 3: Confirm that the direct S3 upload completed successfully.
 * The backend links the stored document to the job and queues extraction.
 */
export async function completeUpload(jobId: string, key: string): Promise<Job> {
  return serverPost<Job>(`/api/v1/jobs/${jobId}/upload-complete`, { key })
}

/**
 * Cancel a running job.
 */
export async function cancelJob(jobId: string): Promise<Job> {
  return serverPost<Job>(`/api/v1/jobs/${jobId}/cancel`)
}

/**
 * Create a new extraction job for a company.
 */
export async function createJob(
  companyId: string,
  jobType: string = 'sec_10k_annual',
): Promise<Job> {
  return serverPost<Job>('/api/v1/jobs', {
    company_id: companyId,
    job_type: jobType,
  })
}
