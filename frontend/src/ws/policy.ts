// Pure reconnect policy (docs/design/08 §14.7), kept separate so it can be unit-tested.

export const BACKOFF_BASE_MS = 500
export const BACKOFF_FACTOR = 2
export const BACKOFF_CAP_MS = 30_000
/** A connection stable for this long resets the backoff. */
export const STABLE_AFTER_MS = 60_000

/** Exponential backoff with full jitter: uniform in [0, min(cap, base * factor^attempt)]. */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const ceiling = Math.min(BACKOFF_CAP_MS, BACKOFF_BASE_MS * BACKOFF_FACTOR ** attempt)
  return Math.floor(random() * ceiling)
}

export type CloseAction =
  | { kind: 'stop' } // we closed it (e.g. logout): don't reconnect
  | { kind: 'reconnect'; delay: 'backoff' | 'now' | number }
  | { kind: 'reauthenticate' } // refresh the access token, then reconnect
  | { kind: 'login' } // the session is gone

export function closeAction(code: number, reason: string): CloseAction {
  switch (code) {
    case 1000:
      return { kind: 'stop' }
    case 4001:
      return reason === 'session_revoked' ? { kind: 'login' } : { kind: 'reauthenticate' }
    case 4008: // slow_consumer: reconnect at once, then sync
      return { kind: 'reconnect', delay: 'now' }
    case 4029:
      return { kind: 'reconnect', delay: 10_000 }
    default: // 1001, 1006, 1011, 4000, 4003, ...
      return { kind: 'reconnect', delay: 'backoff' }
  }
}

/** Remembers recent durable event ids, so an event delivered twice is applied once. */
export class EventDeduper {
  private readonly seen = new Set<number>()
  private readonly order: number[] = []
  private readonly capacity: number

  constructor(capacity = 2000) {
    this.capacity = capacity
  }

  /** True the first time an id is seen. Ephemeral events (id null) always pass. */
  accept(id: number | null): boolean {
    if (id === null) return true
    if (this.seen.has(id)) return false
    this.seen.add(id)
    this.order.push(id)
    if (this.order.length > this.capacity) {
      const evicted = this.order.shift()
      if (evicted !== undefined) this.seen.delete(evicted)
    }
    return true
  }
}
