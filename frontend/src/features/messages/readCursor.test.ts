import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CursorSender } from './readCursor'

const reading = { visible: true, focused: true, atBottom: true }

describe('CursorSender', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('sends only when the tab is visible, focused and at the bottom', () => {
    const sent: number[] = []
    const sender = new CursorSender((seq) => sent.push(seq))

    sender.update(5, { ...reading, visible: false })
    sender.update(5, { ...reading, focused: false })
    sender.update(5, { ...reading, atBottom: false })
    vi.advanceTimersByTime(1000)
    expect(sent).toEqual([])

    sender.update(5, reading)
    vi.advanceTimersByTime(500)
    expect(sent).toEqual([5])
  })

  it('merges rapid updates into one PUT with the highest seq', () => {
    const sent: number[] = []
    const sender = new CursorSender((seq) => sent.push(seq))

    for (const seq of [3, 4, 7, 6]) {
      sender.update(seq, reading)
      vi.advanceTimersByTime(100)
    }
    vi.advanceTimersByTime(500)

    expect(sent).toEqual([7])
  })

  it('never re-sends a seq it (or another tab) already covered', () => {
    const sent: number[] = []
    const sender = new CursorSender((seq) => sent.push(seq), 500, 4)

    sender.update(4, reading)
    vi.advanceTimersByTime(600)
    sender.acknowledge(9) // another tab read further
    sender.update(8, reading)
    vi.advanceTimersByTime(600)

    expect(sent).toEqual([])
  })
})
