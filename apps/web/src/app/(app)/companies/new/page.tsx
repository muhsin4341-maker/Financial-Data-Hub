import type { Metadata } from 'next'
import Link from 'next/link'
import { CreateCompanyForm } from './_components/create-company-form'
import { Card } from '@/app/_components/ui'

export const metadata: Metadata = { title: 'Add company' }

export default function NewCompanyPage() {
  return (
    <div className="flex flex-col gap-6 max-w-2xl">
      <div>
        <Link href="/companies" className="text-sm text-zinc-500 hover:text-zinc-700">
          ← Companies
        </Link>
        <h1 className="mt-2 text-2xl font-bold text-zinc-900">Add company</h1>
        <p className="mt-1 text-sm text-zinc-500">
          Add a company to your workspace to start extracting financial data.
        </p>
      </div>
      <Card className="p-6">
        <CreateCompanyForm />
      </Card>
    </div>
  )
}
