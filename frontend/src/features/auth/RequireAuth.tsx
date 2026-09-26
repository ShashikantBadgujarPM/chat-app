import type { ReactNode } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from './AuthContext'

/** Route guard: renders children only for a signed-in user. */
export function RequireAuth({ children }: { children: ReactNode }) {
  const auth = useAuth()
  const location = useLocation()

  if (auth.status === 'loading') return <p aria-busy="true">Loading…</p>
  if (auth.status === 'signed-out') return <Navigate to="/login" replace state={{ from: location }} />
  return <>{children}</>
}
