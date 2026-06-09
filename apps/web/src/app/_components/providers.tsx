'use client'

/**
 * Client-side providers wrapped in a single component so the root layout
 * (a Server Component) can include them without the 'use client' boundary.
 *
 * Includes:
 *   - TanStack Query (server state)
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { useState } from 'react'

export function Providers({ children }: { children: React.ReactNode }) {
  // Create a new QueryClient per component mount so tests and SSR don't
  // share state between requests.
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            staleTime: 30 * 1000,        // 30 s
            retry: 1,
            refetchOnWindowFocus: false,
          },
        },
      }),
  )

  return (
    <QueryClientProvider client={queryClient}>
      {children}
    </QueryClientProvider>
  )
}
