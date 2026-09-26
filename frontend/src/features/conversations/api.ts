import { api } from '../../api/client'
import type { Page, UserPublic } from '../users/api'

export type Role = 'owner' | 'member'

export type Conversation = {
  id: string
  type: 'direct' | 'group'
  title: string | null
  created_at: string
  last_message_seq: number
  members_preview: UserPublic[]
  member_count: number
  my_role: Role
}

export type ConversationSummary = Conversation & {
  last_message: unknown | null
  unread_count: number
  unread_mention_count: number
  last_activity_at: string
  my_last_read_seq: number
}

export type Member = { user: UserPublic; role: Role; joined_at: string }

const base = '/api/v1/conversations'

export const conversationsApi = {
  list: (cursor?: string) =>
    api<Page<ConversationSummary>>(`${base}${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`),
  get: (id: string) => api<Conversation>(`${base}/${id}`),
  startDirect: (userId: string) =>
    api<Conversation>(`${base}/direct`, { method: 'POST', body: { user_id: userId } }),
  createGroup: (title: string, memberIds: string[]) =>
    api<Conversation>(`${base}/groups`, { method: 'POST', body: { title, member_ids: memberIds } }),
  rename: (id: string, title: string) =>
    api<Conversation>(`${base}/${id}`, { method: 'PATCH', body: { title } }),
  members: (id: string) => api<{ items: Member[] }>(`${base}/${id}/members`),
  addMembers: (id: string, userIds: string[]) =>
    api<{ added: Member[]; already_members: string[] }>(`${base}/${id}/members`, {
      method: 'POST',
      body: { user_ids: userIds },
    }),
  removeMember: (id: string, userId: string) =>
    api<void>(`${base}/${id}/members/${userId}`, { method: 'DELETE' }),
  setMuted: (id: string, muted: boolean) =>
    api<Member & { notifications_muted: boolean }>(`${base}/${id}/membership`, {
      method: 'PATCH',
      body: { notifications_muted: muted },
    }),
}

/** A direct conversation has no title: show the other member's name. */
export function conversationName(conversation: Conversation, myId: string): string {
  if (conversation.type === 'group') return conversation.title ?? 'Untitled group'
  const other = conversation.members_preview.find((member) => member.id !== myId)
  return other?.display_name ?? 'Direct message'
}
