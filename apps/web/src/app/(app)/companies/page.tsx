import type { Metadata } from 'next'
import Link from 'next/link'
import { serverGet } from '@/lib/server-api'
import type { CompanyListResponse } from '@/lib/types'
import { Button, Badge } from '@/app/_components/ui'

export const metadata: Metadata = { title: 'Companies' }

export default async function CompaniesPage() {
  let data: CompanyListResponse | null = null
  let error: string | null = null

  try {
    data = await serverGet<CompanyListResponse>('/api/v1/companies?page_size=50')
  } catch (e) {
    error = e instanceof Error ? e.message : 'Failed to load companies.'
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-zinc-900">Companies</h1>
          <p className="mt-1 text-sm text-zinc-500">
            {data ? `${data.total} total` : ''}
          </p>
        </div>
        <Link href="/companies/new">
          <Button size="md">+ Add company</Button>
        </Link>
      </div>

      {error && (
        <div className="rounded-lg bg-red-50 border border-red-200 px-4 py-3 text-sm text-red-700">
          {error}
        </div>
      )}

      {data && data.items.length === 0 && (
        <div className="rounded-xl border-2 border-dashed border-zinc-200 p-12 text-center">
          <p className="text-zinc-500 mb-4">No companies yet.</p>
          <Link href="/companies/new">
            <Button variant="secondary">Add your first company</Button>
          </Link>
        </div>
      )}

      {data && data.items.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-zinc-200 bg-white">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-zinc-100 bg-zinc-50">
                <th className="px-4 py-3 text-left font-medium text-zinc-600">Name</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-600">Ticker</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-600">Exchange</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-600">Status</th>
                <th className="px-4 py-3 text-left font-medium text-zinc-600" />
              </tr>
            </thead>
            <tbody>
              {data.items.map((company) => (
                <tr
                  key={company.id}
                  className="border-b border-zinc-100 last:border-0 hover:bg-zinc-50"
                >
                  <td className="px-4 py-3 font-medium text-zinc-900">
                    {company.name}
                  </td>
                  <td className="px-4 py-3 font-mono text-zinc-600">
                    {company.ticker}
                  </td>
                  <td className="px-4 py-3 text-zinc-500">
                    {company.exchange ?? '—'}
                  </td>
                  <td className="px-4 py-3">
                    <Badge variant={company.is_active ? 'success' : 'default'}>
                      {company.is_active ? 'Active' : 'Inactive'}
                    </Badge>
                  </td>
                  <td className="px-4 py-3">
                    <Link
                      href={`/companies/${company.id}`}
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
  )
}
