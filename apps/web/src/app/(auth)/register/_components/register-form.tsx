'use client'

/**
 * Register form — Client Component.
 *
 * Uses React 19 useActionState with the `register` Server Action.
 * Creates a new workspace + owner account in a single request.
 */

import { useActionState } from 'react'
import Link from 'next/link'
import { register, type FormState } from '@/app/actions/auth'
import { Input, Button, Alert } from '@/app/_components/ui'

const INITIAL_STATE: FormState = undefined

export function RegisterForm() {
  const [state, action, pending] = useActionState<FormState, FormData>(
    register,
    INITIAL_STATE,
  )

  return (
    <form action={action} className="flex flex-col gap-5" noValidate>
      <div className="mb-1">
        <h2 className="text-xl font-semibold text-zinc-900">Create your workspace</h2>
        <p className="mt-1 text-sm text-zinc-500">
          Set up your Financial Data Hub account.
        </p>
      </div>

      {state?.message && (
        <Alert variant="error">{state.message}</Alert>
      )}

      <Input
        label="Full name"
        id="full_name"
        name="full_name"
        type="text"
        autoComplete="name"
        required
        placeholder="Jane Smith"
        error={state?.errors?.full_name}
      />

      <Input
        label="Work email"
        id="email"
        name="email"
        type="email"
        autoComplete="email"
        required
        placeholder="jane@company.com"
        error={state?.errors?.email}
      />

      <Input
        label="Password"
        id="password"
        name="password"
        type="password"
        autoComplete="new-password"
        required
        placeholder="••••••••••••"
        error={state?.errors?.password}
      />

      <Input
        label="Workspace name"
        id="workspace_name"
        name="workspace_name"
        type="text"
        autoComplete="organization"
        required
        placeholder="Acme Capital"
        error={state?.errors?.workspace_name}
      />

      <div className="rounded-lg bg-zinc-50 border border-zinc-200 px-4 py-3 text-xs text-zinc-500">
        Password must be at least 12 characters and contain uppercase,
        lowercase, a number, and a special character.
      </div>

      <Button type="submit" loading={pending} size="lg" className="mt-1 w-full">
        {pending ? 'Creating workspace…' : 'Create workspace'}
      </Button>

      <p className="text-center text-sm text-zinc-500">
        Already have an account?{' '}
        <Link
          href="/login"
          className="font-medium text-blue-600 hover:text-blue-500"
        >
          Sign in
        </Link>
      </p>
    </form>
  )
}
