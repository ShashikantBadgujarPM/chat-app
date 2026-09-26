import { api } from '../../api/client'
import type { UserMe } from '../auth/AuthContext'

export type Presence = { status: 'online' | 'offline'; last_seen_at: string | null }

export type UserPublic = {
  id: string
  username: string
  display_name: string
  presence: Presence
}

export type Page<T> = { items: T[]; next_cursor: string | null }

export const usersApi = {
  updateMe: (changes: { display_name?: string; timezone?: string }) =>
    api<UserMe>('/api/v1/users/me', { method: 'PATCH', body: changes }),
  get: (id: string) => api<UserPublic>(`/api/v1/users/${encodeURIComponent(id)}`),
  search: (q: string, cursor?: string) => {
    const params = new URLSearchParams({ q, limit: '10' })
    if (cursor) params.set('cursor', cursor)
    return api<Page<UserPublic>>(`/api/v1/users?${params}`)
  },
}
