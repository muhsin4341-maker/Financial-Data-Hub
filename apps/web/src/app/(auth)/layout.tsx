/**
 * Auth layout — centered, full-height card with brand header.
 * Shared by /login and /register.
 * Server Component (no 'use client' needed).
 */
export default function AuthLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-4 py-12 bg-zinc-50">
      <div className="mb-8 text-center">
        <h1 className="text-2xl font-bold tracking-tight text-zinc-900">
          Financial Data Hub
        </h1>
        <p className="mt-1 text-sm text-zinc-500">
          Production-grade financial data acquisition
        </p>
      </div>
      <div className="w-full max-w-md rounded-2xl border border-zinc-200 bg-white p-8 shadow-sm">
        {children}
      </div>
    </div>
  )
}
