'use server'

/**
 * Company Server Actions.
 *
 * Called from Client Components via useActionState (React 19).
 * Runs inside Docker — uses API_INTERNAL_URL to reach the FastAPI service.
 *
 * M2.6F update: added optional `cik` field forwarded from the SEC resolver
 * widget when the user selects a typeahead suggestion.
 */

import * as z from 'zod'
import { serverPost } from '@/lib/server-api'
import type { Company } from '@/lib/types'

// ---------------------------------------------------------------------------
// Form state type
// ---------------------------------------------------------------------------

export type CompanyFormState =
  | { errors?: Record<string, string[]>; message?: string; success?: false }
  | { success: true; company: Company }
  | undefined

// ---------------------------------------------------------------------------
// Zod schema (Zod v4 — { error: '...' } syntax)
// ---------------------------------------------------------------------------

const CreateCompanySchema = z.object({
  name: z.string().min(1, { error: 'Company name is required.' }).trim(),
  ticker: z
    .string()
    .min(1, { error: 'Ticker symbol is required.' })
    .max(20, { error: 'Ticker must be 20 characters or fewer.' })
    .trim(),
  // cik — optional; supplied by the SEC resolver widget (M2.6F)
  cik: z.string().trim().optional(),
  exchange: z.string().trim().optional(),
  sector: z.string().trim().optional(),
  website: z
    .string()
    .trim()
    .optional()
    .refine((v) => !v || v === '' || z.url().safeParse(v).success, {
      error: 'Enter a valid URL.',
    }),
})

// ---------------------------------------------------------------------------
// Action
// ---------------------------------------------------------------------------

export async function createCompany(
  _prev: CompanyFormState,
  formData: FormData,
): Promise<CompanyFormState> {
  const raw = {
    name: formData.get('name') as string,
    ticker: formData.get('ticker') as string,
    cik: (formData.get('cik') as string) || undefined,
    exchange: (formData.get('exchange') as string) || undefined,
    sector: (formData.get('sector') as string) || undefined,
    website: (formData.get('website') as string) || undefined,
  }

  const validated = CreateCompanySchema.safeParse(raw)
  if (!validated.success) {
    return { errors: validated.error.flatten().fieldErrors as Record<string, string[]> }
  }

  const body: Record<string, unknown> = {
    name: validated.data.name,
    ticker: validated.data.ticker,
  }
  if (validated.data.cik)     body.cik      = validated.data.cik
  if (validated.data.exchange) body.exchange = validated.data.exchange
  if (validated.data.sector)   body.sector   = validated.data.sector
  if (validated.data.website)  body.website  = validated.data.website

  try {
    const company = await serverPost<Company>('/api/v1/companies', body)
    return { success: true, company }
  } catch (e: unknown) {
    const err = e as { message?: string; apiCode?: string }
    if (err.apiCode === 'CONFLICT' || (err.message ?? '').toLowerCase().includes('already exists')) {
      return { message: `A company with that ticker already exists in your workspace.` }
    }
    return { message: err.message ?? 'Failed to create company. Please try again.' }
  }
}
