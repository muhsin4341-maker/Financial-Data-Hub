import type { Metadata } from 'next'
import Link from 'next/link'
import { notFound } from 'next/navigation'
import { serverGet } from '@/lib/server-api'
import type { Company, JobListResponse } from '@/lib/types'
import { Card, Badge, JobStatusBadge } from '@/app/_components/ui'
import { CreateJobButton } from './_components/create-job-button'

// In Next.js 16, params is a Promise — must be awaited.
export async function generateMetadata(
  props: PageProps<'/companies/[id]'>,
): Promise<Metadata> {
  const { id } = await props.params
  try {
    const company = await serverGet<Company>(`/api/v1/companies/${id}`)
    return { title: company.name }
  } catch {
    return { title: 'Company' }
  }
}

export default async function CompanyDetailPage(
  props: PageProps<'/companies/[id]'>,
) {
  const { id } = await props.params

  let company: Company | null = null
  let jobs: JobListResponse | null = null

  try {
    company = await serverGet<Company>(`/api/v1/companies/${id}`)
  } catch (e) {
    const err = e as { statusCode?: number }
    if (err.statusCode === 404) notFound()
    company = null
  }

  if (company) {
    try {
      jobs = await serverGet<JobListResponse>(
        `/api/v1/jobs?company_id=${id}&page_size=20`,
      )
    } catch {
      jobs = null
    }
  }

  if (!company) {
    return (
      <div className="text-center py-12 text-zinc-500">
        Failed to load company.
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-6">
      {/* Breadcrumb */}
      <div>
        <Link href="/companies" className="text-sm text-zinc-500 hover:text-zinc-700">
          ← Companies
        </Link>
        <div className="mt-2 flex items-center gap-3">
          <h1 className="text-2xl font-bold text-zinc-900">{company.name}</h1>
          <span className="font-mono text-sm text-zinc-500">{company.ticker}</span>
          <Badge variant={company.is_active ? 'success' : 'default'}>
            {company.is_active ? 'Active' : 'Inactive'}
          </Badge>
        </div>
      </div>

      {/* Sub-nav: Data views for this company */}
      <div className="flex items-center gap-1 border-b border-zinc-200 -mb-2">
        {[
          { href: `/companies/${id}`,            label: 'Overview' },
          { href: `/companies/${id}/filings`,    label: 'Filings' },
          { href: `/companies/${id}/financials`, label: 'Financial Ledger' },
          { href: `/companies/${id}/analytics`,  label: '📈 Analytics' },
        ].map(({ href, label }) => (
          <Link
            key={href}
            href={href}
            className="px-4 py-2 text-sm font-medium text-zinc-500 hover:text-zinc-800 border-b-2 border-transparent hover:border-zinc-300 transition-colors"
          >
            {label}
          </Link>
        ))}
      </div>

      {/* Company info */}
      <Card className="p-6">
        <h2 className="text-base font-semibold text-zinc-900 mb-4">Details</h2>
        <dl className="grid grid-cols-2 gap-4 text-sm">
          {[
            ['Exchange', company.exchange],
            ['Sector', company.sector],
            ['Industry', company.industry],
            ['CIK', company.cik],
            ['Website', company.website],
          ].map(([label, value]) =>
            value ? (
              <div key={label as string}>
                <dt className="text-zinc-500">{label}</dt>
                <dd className="font-medium text-zinc-900 mt-0.5">
                  {label === 'Website' ? (
                    <a
                      href={value as string}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-blue-600 hover:underline"
                    >
                      {value}
                    </a>
                  ) : (
                    value
                  )}
                </dd>
              </div>
            ) : null,
          )}
        </dl>
        {company.description && (
          <p className="mt-4 text-sm text-zinc-600 leading-relaxed">
            {company.description}
          </p>
        )}
      </Card>

      {/* Jobs */}
      <div className="flex flex-col gap-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold text-zinc-900">
            Jobs {jobs ? `(${jobs.total})` : ''}
          </h2>
          <CreateJobButton companyId={id} />
        </div>

        {jobs && jobs.items.length === 0 && (
          <div className="rounded-xl border-2 border-dashed border-zinc-200 p-8 text-center text-sm text-zinc-500">
            No jobs yet. Create one to start extracting financial data.
          </div>
        )}

        {jobs && jobs.items.length > 0 && (
          <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-zinc-100 bg-zinc-50">
                  <th className="px-4 py-3 text-left font-medium text-zinc-600">Type</th>
                  <th className="px-4 py-3 text-left font-medium text-zinc-600">Year</th>
                  <th className="px-4 py-3 text-left font-medium text-zinc-600">Status</th>
                  <th className="px-4 py-3 text-left font-medium text-zinc-600">Created</th>
                  <th className="px-4 py-3" />
                </tr>
              </thead>
              <tbody>
                {jobs.items.map((job) => (
                  <tr
                    key={job.id}
                    className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50"
                  >
                    <td className="px-4 py-3 font-mono text-xs text-zinc-700">
                      {job.job_type}
                    </td>
                    <td className="px-4 py-3 text-zinc-600">
                      {job.fiscal_year ?? '—'}
                    </td>
                    <td className="px-4 py-3">
                      <JobStatusBadge status={job.status} />
                    </td>
                    <td className="px-4 py-3 text-zinc-500">
                      {new Date(job.created_at).toLocaleDateString()}
                    </td>
                    <td className="px-4 py-3">
                      <Link
                        href={`/jobs/${job.id}`}
                        className="text-blue-600 hover:text-blue-500 font-medium"
                      >
                        View →
                      </Link>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
