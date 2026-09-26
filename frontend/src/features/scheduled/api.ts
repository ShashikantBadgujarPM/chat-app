import { api } from '../../api/client'
import type { Page } from '../users/api'

export type ScheduledStatus = 'pending' | 'sent' | 'cancelled' | 'failed'

export type ScheduledMessage = {
  id: string
  conversation_id: string
  body: string
  reply_to_id: string | null
  scheduled_at: string
  timezone: string
  status: ScheduledStatus
  attempts: number
  last_error: string | null
  sent_message_id: string | null
  sent_at: string | null
  created_at: string
  updated_at: string
  cancelled_at: string | null
}

const base = '/api/v1/scheduled-messages'

export const scheduledApi = {
  create: (input: {
    client_message_id: string
    conversation_id: string
    body: string
    reply_to_id?: string
    scheduled_at: string
    timezone?: string
  }) => api<ScheduledMessage>(base, { method: 'POST', body: input }),
  list: (params: { status?: ScheduledStatus; conversationId?: string; cursor?: string } = {}) => {
    const q = new URLSearchParams()
    if (params.status) q.set('status', params.status)
    if (params.conversationId) q.set('conversation_id', params.conversationId)
    if (params.cursor) q.set('cursor', params.cursor)
    const query = q.toString()
    return api<Page<ScheduledMessage>>(`${base}${query ? `?${query}` : ''}`)
  },
  get: (id: string) => api<ScheduledMessage>(`${base}/${id}`),
  update: (
    id: string,
    changes: { body?: string; scheduled_at?: string; timezone?: string; reply_to_id?: string | null },
  ) => api<ScheduledMessage>(`${base}/${id}`, { method: 'PATCH', body: changes }),
  cancel: (id: string) => api<ScheduledMessage>(`${base}/${id}/cancel`, { method: 'POST' }),
  retry: (id: string, scheduledAt?: string) =>
    api<ScheduledMessage>(`${base}/${id}/retry`, {
      method: 'POST',
      body: scheduledAt ? { scheduled_at: scheduledAt } : {},
    }),
}

/** The browser's IANA zone, for display and for the request's `timezone` field. */
export function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone
  } catch {
    return 'UTC'
  }
}

/** `<input type="datetime-local">` gives local wall-clock components with no offset;
 * the Date constructor applies the browser's own zone, so toISOString() is correctly
 * offset-qualified (a trailing "Z" satisfies the server's naive-datetime check). */
export function localInputToIso(value: string): string | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date.toISOString()
}

/** For pre-filling a datetime-local input from an ISO instant. */
export function isoToLocalInput(iso: string): string {
  const date = new Date(iso)
  const pad = (n: number) => String(n).padStart(2, '0')
  return (
    `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}` +
    `T${pad(date.getHours())}:${pad(date.getMinutes())}`
  )
}
