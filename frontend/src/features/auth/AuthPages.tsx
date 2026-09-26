import { useState } from 'react'
import type { FormEvent } from 'react'
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { useAuth } from './AuthContext'

function errorText(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 429 || error.status === 423) {
      return `${error.message} Please wait before trying again.`
    }
    return error.message
  }
  return 'Something went wrong. Please try again.'
}

export function LoginPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (auth.status === 'signed-in') return <Navigate to="/" replace />

  const from = (location.state as { from?: { pathname: string } } | null)?.from?.pathname ?? '/'

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await auth.login(identifier, password)
      navigate(from, { replace: true })
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} aria-labelledby="login-title">
      <h1 id="login-title">Sign in</h1>
      <label>
        Username or email
        <input
          value={identifier}
          onChange={(e) => setIdentifier(e.target.value)}
          autoComplete="username"
          required
        />
      </label>
      <label>
        Password
        <input
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
      </label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={busy}>
        {busy ? 'Signing in…' : 'Sign in'}
      </button>
      <p>
        No account yet? <Link to="/register">Create one</Link>
      </p>
    </form>
  )
}

export function RegisterPage() {
  const auth = useAuth()
  const navigate = useNavigate()
  const [form, setForm] = useState({
    username: '',
    email: '',
    display_name: '',
    password: '',
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
  })
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (auth.status === 'signed-in') return <Navigate to="/" replace />

  const update = (field: keyof typeof form) => (e: { target: { value: string } }) =>
    setForm((current) => ({ ...current, [field]: e.target.value }))

  async function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await auth.register(form)
      navigate('/', { replace: true })
    } catch (caught) {
      setError(errorText(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={submit} aria-labelledby="register-title">
      <h1 id="register-title">Create an account</h1>
      <label>
        Username
        <input
          value={form.username}
          onChange={update('username')}
          pattern="[A-Za-z0-9_]{3,32}"
          title="3–32 letters, digits or underscores"
          autoComplete="username"
          required
        />
      </label>
      <label>
        Email
        <input type="email" value={form.email} onChange={update('email')} autoComplete="email" required />
      </label>
      <label>
        Display name
        <input value={form.display_name} onChange={update('display_name')} maxLength={64} required />
      </label>
      <label>
        Password <small>(at least 10 characters)</small>
        <input
          type="password"
          value={form.password}
          onChange={update('password')}
          minLength={10}
          maxLength={128}
          autoComplete="new-password"
          required
        />
      </label>
      {error && <p role="alert">{error}</p>}
      <button type="submit" disabled={busy}>
        {busy ? 'Creating…' : 'Create account'}
      </button>
      <p>
        Already registered? <Link to="/login">Sign in</Link>
      </p>
    </form>
  )
}
