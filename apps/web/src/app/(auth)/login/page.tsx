import type { Metadata } from 'next'
import { LoginForm } from './_components/login-form'

export const metadata: Metadata = {
  title: 'Sign in',
}

/**
 * /login — Server Component page.
 *
 * The Server Component provides metadata and wraps the LoginForm Client
 * Component, keeping all interactive state on the client side.
 */
export default function LoginPage() {
  return <LoginForm />
}
