import type { Presence } from '../users/api'
import { lastSeenText, usePresence } from './presence'

/** A presence dot, with "last seen" as its accessible label and tooltip. */
export function PresenceDot({ userId, initial }: { userId: string; initial?: Presence }) {
  const presence = usePresence(userId, initial)
  if (!presence) return null
  const online = presence.status === 'online'
  const label = online
    ? 'Online'
    : presence.last_seen_at
      ? `Last seen ${lastSeenText(presence.last_seen_at)}`
      : 'Offline'
  return (
    <span className={`presence-dot ${online ? 'online' : 'offline'}`} title={label} role="img" aria-label={label} />
  )
}
