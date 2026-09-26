import { api } from '../../api/client'
import type { UserPublic } from '../users/api'

export type MessageBrief = {
  id: string
  seq: number
  sender_id: string | null
  body_preview: string
  deleted: boolean
}

export type Message = {
  id: string
  conversation_id: string
  seq: number
  sender: UserPublic | null
  body: string | null
  reply_to: MessageBrief | null
  mentions: string[]
  created_at: string
  edited_at: string | null
  deleted_at: string | null
  scheduled_message_id: string | null
  client_message_id: string
}

export type HistoryPage = { items: Message[]; has_more: boolean }

export const messagesApi = {
  latest: (conversationId: string, limit = 50) =>
    api<HistoryPage>(`/api/v1/conversations/${conversationId}/messages?limit=${limit}`),
  before: (conversationId: string, beforeSeq: number, limit = 50) =>
    api<HistoryPage>(
      `/api/v1/conversations/${conversationId}/messages?before_seq=${beforeSeq}&limit=${limit}`,
    ),
  send: (conversationId: string, body: { client_message_id: string; body: string; reply_to_id?: string }) =>
    api<Message>(`/api/v1/conversations/${conversationId}/messages`, { method: 'POST', body }),
  edit: (messageId: string, body: string) =>
    api<Message>(`/api/v1/messages/${messageId}`, { method: 'PATCH', body: { body } }),
  remove: (messageId: string) => api<void>(`/api/v1/messages/${messageId}`, { method: 'DELETE' }),
}
