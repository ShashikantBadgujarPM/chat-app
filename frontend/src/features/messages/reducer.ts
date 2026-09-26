// Pure message-list updates, shared by REST responses and live events.
//
// Events are applied idempotently (docs/design/08 §14.1 principle 4): the entity's
// version is edited_at / deleted_at, order comes from seq, and an optimistic entry is
// reconciled with the real message by client_message_id.

import type { Message } from './api'

export type Pending = {
  clientMessageId: string
  body: string
  replyToId: string | null
  state: 'sending' | 'failed'
}

export type MessageState = {
  messages: Message[] // ascending by seq
  pending: Pending[]
}

function version(message: Message): string {
  return `${message.deleted_at ?? ''}|${message.edited_at ?? ''}`
}

/** Insert or replace a message; never downgrade an entity to an older version. */
export function upsertMessage(state: MessageState, message: Message): MessageState {
  const index = state.messages.findIndex((m) => m.id === message.id)
  let messages: Message[]
  if (index >= 0) {
    const current = state.messages[index]
    // A tombstone is final; otherwise the newer edit wins.
    if (current.deleted_at && !message.deleted_at) return reconcile(state, message)
    if (version(message) < version(current) && !message.deleted_at) return reconcile(state, message)
    messages = state.messages.map((m) => (m.id === message.id ? message : m))
  } else {
    messages = [...state.messages, message].sort((a, b) => a.seq - b.seq)
  }
  return reconcile({ ...state, messages }, message)
}

/** Drop the optimistic entry this message confirms (same tab or another tab's echo). */
function reconcile(state: MessageState, message: Message): MessageState {
  const pending = state.pending.filter((p) => p.clientMessageId !== message.client_message_id)
  return pending.length === state.pending.length ? state : { ...state, pending }
}

export function markDeleted(
  state: MessageState,
  deleted: { id: string; deleted_at: string | null },
): MessageState {
  return {
    ...state,
    messages: state.messages.map((m) =>
      m.id === deleted.id ? { ...m, body: null, deleted_at: deleted.deleted_at ?? new Date().toISOString() } : m,
    ),
  }
}
