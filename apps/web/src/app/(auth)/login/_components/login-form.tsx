'use client'

/**
 * Login form — Client Component.
 *
 * Uses React 19 useActionState with the `login` Server Action.
 * `useActionState` returns [state, action, pending] where:
 *   - state  : FormState (errors / message) returned by the Server Action
 *   - action : the wrapped action to pass to <form action={...}>
 *   - pending: boolean — true while the Server Action is in flight
 *
 * On success the Server Action calls redirect('/dashboard') server-side;
 * no client navigation is needed.
 */

import { useActionState } from 'react'
import Link from 'next/link'
import { login, type FormState } from '@/app/actions/auth'
import { Input, Button, Alert } from '@/app/_components/ui'

const INITIAL_STATE: FormState = undefined

export function LoginForm() {
  const [state, action, pending] = useActionState<FormState, FormData>(
    login,
    INITIAL_STATE,
  )

  return (
    <form action={action} className="flex flex-col gap-5" noValidate>
      <div className="mb-1">
        <h2 className="text-xl font-semibold text-zinc-900">Sign in</h2>
        <p className="mt-1 text-sm text-zinc-500">
          Welcome back. Enter your credentials to continue.
        </p>
      </div>

      {state?.message && (
        <Alert variant="error">{state.message}</Alert>
      )}

      <Input
        label="Email"
        id="email"
        name="email"
        type="email"
        autoComplete="email"
        required
        placeholder="you@example.com"
        error={state?.errors?.email}
      />

      <Input
        label="Password"
        id="password"
        name="password"
        type="password"
        autoComplete="current-password"
        required
        placeholder="••••••••••••"
        error={state?.errors?.password}
      />

      <Button type="submit" loading={pending} size="lg" className="mt-1 w-full">
        {pending ? 'Signing in…' : 'Sign in'}
      </Button>

      <p className="text-center text-sm text-zinc-500">
        Don&apos;t have an account?{' '}
        <Link
          href="/register"
          className="font-medium text-blue-600 hover:text-blue-500"
        >
          Create one
        </Link>
      </p>
    </form>
  )
}
