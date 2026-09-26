import { describe, expect, it, vi } from 'vitest'

vi.mock('../../api/client', () => ({ api: vi.fn(), refreshAccessToken: vi.fn(), ApiError: Error }))

const { lastSeenText, typingText } = await import('./presence')

describe('lastSeenText', () => {
  const now = Date.parse('2026-10-01T12:00:00Z')
  it.each([
    ['2026-10-01T11:59:30Z', 'just now'],
    ['2026-10-01T11:55:00Z', '5 min ago'],
    ['2026-10-01T09:00:00Z', '3 h ago'],
    ['2026-09-29T12:00:00Z', '2 d ago'],
    ['2026-10-01T12:00:05Z', 'just now'], // slightly in the future (skew): never negative
  ])('%s -> %s', (at, expected) => {
    expect(lastSeenText(at, now)).toBe(expected)
  })
})

describe('typingText', () => {
  it('names one or two people and counts more', () => {
    expect(typingText([])).toBeNull()
    expect(typingText(['Rahul'])).toBe('Rahul is typing…')
    expect(typingText(['Rahul', 'Asha'])).toBe('Rahul and Asha are typing…')
    expect(typingText(['A', 'B', 'C'])).toBe('3 people are typing…')
  })
})
