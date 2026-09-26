import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, useParams } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { useAuth } from '../auth/AuthContext'
import { PresenceDot } from '../presence/PresenceDot'
import { lastSeenText } from '../presence/presence'
import { usersApi } from './api'
import type { UserPublic } from './api'

function timezones(): string[] {
  const intl = Intl as unknown as { supportedValuesOf?: (key: string) => string[] }
  return intl.supportedValuesOf?.('timeZone') ?? ['UTC']
}

export function MyProfilePage() {
  const auth = useAuth()
  const zones = useMemo(() => timezones(), [])
  const user = auth.status === 'signed-in' ? auth.user : null
  const [displayName, setDisplayName] = useState(user?.display_name ?? '')
  const [timezone, setTimezone] = useState(user?.timezone ?? 'UTC')
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  if (!user) return null

  async function save(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setMessage(null)
    try {
      await usersApi.updateMe({ display_name: displayName, timezone })
      await auth.reloadUser()
      setMessage('Saved.')
    } catch (caught) {
      setMessage(caught instanceof ApiError ? caught.message : 'Could not save.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <form onSubmit={save} aria-labelledby="profile-title">
      <h1 id="profile-title">Your profile</h1>
      <p>
        @{user.username} · {user.email}
      </p>
      <label>
        Display name
        <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} maxLength={64} required />
      </label>
      <label>
        Time zone
        <select value={timezone} onChange={(e) => setTimezone(e.target.value)}>
          {(zones.includes(timezone) ? zones : [timezone, ...zones]).map((zone) => (
            <option key={zone} value={zone}>
              {zone}
            </option>
          ))}
        </select>
      </label>
      {message && <p role="status">{message}</p>}
      <button type="submit" disabled={busy}>
        {busy ? 'Saving…' : 'Save'}
      </button>
      <p>
        <Link to="/">Back</Link>
      </p>
    </form>
  )
}

export function PublicProfilePage() {
  const { userId = '' } = useParams()
  const [user, setUser] = useState<UserPublic | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    usersApi
      .get(userId)
      .then((found) => !cancelled && setUser(found))
      .catch((caught) => {
        if (!cancelled) setError(caught instanceof ApiError && caught.status === 404 ? 'User not found.' : 'Could not load the profile.')
      })
    return () => {
      cancelled = true
    }
  }, [userId])

  if (error) return <p role="alert">{error}</p>
  if (!user) return <p aria-busy="true">Loading…</p>
  return (
    <section>
      <h1>{user.display_name}</h1>
      <p>@{user.username}</p>
      <p>
        <PresenceDot userId={user.id} initial={user.presence} />{' '}
        {user.presence.status === 'online'
          ? 'Online'
          : user.presence.last_seen_at
            ? `Last seen ${lastSeenText(user.presence.last_seen_at)}`
            : 'Offline'}
      </p>
      <p>
        <Link to="/">Back</Link>
      </p>
    </section>
  )
}
