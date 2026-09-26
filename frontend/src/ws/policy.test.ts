import { describe, expect, it } from 'vitest'
import { BACKOFF_CAP_MS, EventDeduper, backoffDelay, closeAction } from './policy'

describe('backoffDelay', () => {
  it('grows exponentially from 0.5 s and is capped at 30 s (upper bound, random = 1)', () => {
    const upper = [0, 1, 2, 3, 4, 5, 6, 7, 10].map((attempt) => backoffDelay(attempt, () => 0.999999))
    expect(upper).toEqual([499, 999, 1999, 3999, 7999, 15999, 29999, 29999, 29999])
  })

  it('uses full jitter: anywhere from 0 up to the ceiling', () => {
    expect(backoffDelay(5, () => 0)).toBe(0)
    expect(backoffDelay(5, () => 0.5)).toBe(8000)
    expect(backoffDelay(50, () => 0.5)).toBe(BACKOFF_CAP_MS / 2)
  })
})

describe('closeAction', () => {
  it.each([
    [1000, '', { kind: 'stop' }],
    [1001, 'idle_timeout', { kind: 'reconnect', delay: 'backoff' }],
    [1006, '', { kind: 'reconnect', delay: 'backoff' }],
    [1011, 'internal_error', { kind: 'reconnect', delay: 'backoff' }],
    [4000, 'bad_frame', { kind: 'reconnect', delay: 'backoff' }],
    [4001, 'invalid_ticket', { kind: 'reauthenticate' }],
    [4001, 'session_revoked', { kind: 'login' }],
    [4003, 'forbidden', { kind: 'reconnect', delay: 'backoff' }],
    [4008, 'slow_consumer', { kind: 'reconnect', delay: 'now' }],
    [4029, 'rate_limited', { kind: 'reconnect', delay: 10_000 }],
  ])('code %i (%s)', (code, reason, expected) => {
    expect(closeAction(code, reason)).toEqual(expected)
  })
})

describe('EventDeduper', () => {
  it('accepts a durable id once', () => {
    const dedup = new EventDeduper()
    expect(dedup.accept(7)).toBe(true)
    expect(dedup.accept(7)).toBe(false)
    expect(dedup.accept(8)).toBe(true)
  })

  it('always accepts ephemeral events', () => {
    const dedup = new EventDeduper()
    expect(dedup.accept(null)).toBe(true)
    expect(dedup.accept(null)).toBe(true)
  })

  it('forgets the oldest ids beyond its capacity', () => {
    const dedup = new EventDeduper(2)
    dedup.accept(1)
    dedup.accept(2)
    dedup.accept(3)
    expect(dedup.accept(1)).toBe(true) // evicted, so accepted again
    expect(dedup.accept(3)).toBe(false)
  })
})
