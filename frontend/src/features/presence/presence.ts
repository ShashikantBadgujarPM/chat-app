// Client presence and typing state (M08). Presence events are ephemeral and never
// replayed, so REST (/users/presence) seeds the state after every (re)connect and live
// presence.updated events keep it current.

import { useEffect, useState } from 'react'
import { api } from '../../api/client'
import { chatSocket } from '../../ws/ChatSocket'
import type { Presence } from '../users/api'

const presence = new Map<string, Presence>()
const listeners = new Set<() => void>()

function notify() {
  listeners.forEach((listener) => listener())
}

export function seedPresence(userId: string, value: Presence | undefined) {
  if (value && !presence.has(userId)) {
    presence.set(userId, value)
    notify()
  }
}

chatSocket.subscribe((event) => {
  if (event.type === 'presence.updated') {
    const p = event.payload as { user_id: string; status: Presence['status']; last_seen_at: string | null }
    presence.set(p.user_id, { status: p.status, last_seen_at: p.last_seen_at })
    notify()
  } else if (event.type === 'hello') {
    // After a (re)connect, refresh everyone we know about: we may have missed changes.
    const ids = [...presence.keys()]
    for (let i = 0; i < ids.length; i += 100) {
      const batch = ids.slice(i, i + 100)
      void api<{ items: ({ user_id: string } & Presence)[] }>(
        `/api/v1/users/presence?ids=${batch.join(',')}`,
      )
        .then((result) => {
          result.items.forEach((item) =>
            presence.set(item.user_id, { status: item.status, last_seen_at: item.last_seen_at }),
          )
          notify()
        })
        .catch(() => undefined)
    }
  }
})

export function usePresence(userId: string, initial?: Presence): Presence | undefined {
  const [, force] = useState(0)
  useEffect(() => {
    seedPresence(userId, initial)
    const listener = () => force((n) => n + 1)
    listeners.add(listener)
    return () => {
      listeners.delete(listener)
    }
  }, [userId, initial])
  return presence.get(userId) ?? initial
}

/** "just now", "5 min ago", "3 h ago", "2 d ago", using the server's clock (skew-corrected). */
export function lastSeenText(lastSeenAt: string, nowMs: number = chatSocket.serverNow()): string {
  const seconds = Math.max(0, Math.round((nowMs - Date.parse(lastSeenAt)) / 1000))
  if (seconds < 60) return 'just now'
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  return `${Math.round(hours / 24)} d ago`
}

/** "Rahul is typing…", "Rahul and Asha are typing…", "3 people are typing…". */
export function typingText(names: string[]): string | null {
  if (names.length === 0) return null
  if (names.length === 1) return `${names[0]} is typing…`
  if (names.length === 2) return `${names[0]} and ${names[1]} are typing…`
  return `${names.length} people are typing…`
}
