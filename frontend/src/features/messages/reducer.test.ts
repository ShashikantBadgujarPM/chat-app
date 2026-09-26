import { describe, expect, it } from 'vitest'
import type { Message } from './api'
import { markDeleted, upsertMessage } from './reducer'
import type { MessageState } from './reducer'

function message(overrides: Partial<Message> = {}): Message {
  return {
    id: 'm1',
    conversation_id: 'c1',
    seq: 1,
    sender: null,
    body: 'hello',
    reply_to: null,
    mentions: [],
    created_at: '2026-10-01T12:00:00.000Z',
    edited_at: null,
    deleted_at: null,
    scheduled_message_id: null,
    client_message_id: 'client-1',
    ...overrides,
  }
}

const empty: MessageState = { messages: [], pending: [] }

describe('upsertMessage', () => {
  it('reconciles the optimistic entry by client_message_id', () => {
    const state: MessageState = {
      messages: [],
      pending: [{ clientMessageId: 'client-1', body: 'hello', replyToId: null, state: 'sending' }],
    }

    const next = upsertMessage(state, message())

    expect(next.pending).toEqual([])
    expect(next.messages).toHaveLength(1)
  })

  it('is idempotent: the same event twice leaves one message', () => {
    const once = upsertMessage(empty, message())
    const twice = upsertMessage(once, message())

    expect(twice.messages).toHaveLength(1)
  })

  it('keeps seq order when events arrive out of order', () => {
    let state = upsertMessage(empty, message({ id: 'b', seq: 2, client_message_id: 'x' }))
    state = upsertMessage(state, message({ id: 'a', seq: 1, client_message_id: 'y' }))

    expect(state.messages.map((m) => m.seq)).toEqual([1, 2])
  })

  it('never resurrects a deleted message from a stale event', () => {
    let state = upsertMessage(empty, message())
    state = markDeleted(state, { id: 'm1', deleted_at: '2026-10-01T12:05:00.000Z' })
    state = upsertMessage(state, message({ body: 'stale created event' }))

    expect(state.messages[0].body).toBeNull()
  })

  it('does not downgrade an edit with an older version', () => {
    let state = upsertMessage(empty, message({ body: 'v2', edited_at: '2026-10-01T12:02:00.000Z' }))
    state = upsertMessage(state, message({ body: 'v1', edited_at: null }))

    expect(state.messages[0].body).toBe('v2')
  })
})
