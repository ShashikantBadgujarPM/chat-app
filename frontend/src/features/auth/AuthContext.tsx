import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'
import {
  api,
  authApi,
  onAccessTokenChange,
  refreshAccessToken,
  setAccessToken,
} from '../../api/client'

export type UserMe = {
  id: string
  username: string
  email: string
  display_name: string
  timezone: string
  created_at: string
}

type LoginResponse = { access_token: string; expires_in: number; user: UserMe }

export type RegisterInput = {
  username: string
  email: string
  display_name: string
  password: string
  timezone: string
}

type AuthState =
  | { status: 'loading'; user: null }
  | { status: 'signed-out'; user: null }
  | { status: 'signed-in'; user: UserMe }

type AuthContextValue = AuthState & {
  /** Re-read /users/me after a profile change. */
  reloadUser: () => Promise<void>
  login: (usernameOrEmail: string, password: string) => Promise<void>
  register: (input: RegisterInput) => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: 'loading', user: null })

  // On load: the access token is gone (memory only), but the httpOnly refresh cookie
  // may still be valid, so try a silent refresh to restore the session.
  useEffect(() => {
    let cancelled = false
    refreshAccessToken()
      .then(async (token) => {
        if (!token) return null
        return api<UserMe>('/api/v1/users/me')
      })
      .catch(() => null)
      .then((user) => {
        if (cancelled) return
        setState(user ? { status: 'signed-in', user } : { status: 'signed-out', user: null })
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Session ended elsewhere (refresh failed, or another tab logged out).
  useEffect(
    () =>
      onAccessTokenChange((token) => {
        if (!token) setState({ status: 'signed-out', user: null })
      }),
    [],
  )

  const reloadUser = useCallback(async () => {
    const user = await api<UserMe>('/api/v1/users/me')
    setState({ status: 'signed-in', user })
  }, [])

  const login = useCallback(async (usernameOrEmail: string, password: string) => {
    const result = await api<LoginResponse>('/api/v1/auth/login', {
      method: 'POST',
      auth: false,
      body: { username_or_email: usernameOrEmail, password },
    })
    setAccessToken(result.access_token)
    setState({ status: 'signed-in', user: result.user })
  }, [])

  const register = useCallback(
    async (input: RegisterInput) => {
      await api<UserMe>('/api/v1/auth/register', { method: 'POST', auth: false, body: input })
      await login(input.username, input.password)
    },
    [login],
  )

  const logout = useCallback(async () => {
    try {
      await api<void>('/api/v1/auth/logout', {
        method: 'POST',
        auth: false,
        headers: authApi.csrfHeaders,
      })
    } finally {
      setAccessToken(null)
      setState({ status: 'signed-out', user: null })
    }
  }, [])

  const value = useMemo(
    () => ({ ...state, reloadUser, login, register, logout }),
    [state, reloadUser, login, register, logout],
  )
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext)
  if (!value) throw new Error('useAuth must be used inside <AuthProvider>')
  return value
}
