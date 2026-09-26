import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { useAuth } from '../auth/AuthContext'
import { UserSearch } from '../users/UserSearch'
import { conversationName, conversationsApi } from './api'
import type { Conversation, Member } from './api'
import type { ConversationsOutletContext } from './ConversationsLayout'

export function NoConversationSelected() {
  return <p className="empty">Select a conversation, or start a new one.</p>
}

/** Details and member management for one conversation. Messages arrive in M05. */
export function ConversationPanel() {
  const { conversationId = '' } = useParams()
  const auth = useAuth()
  const navigate = useNavigate()
  const { refresh } = useOutletContext<ConversationsOutletContext>()
  const [conversation, setConversation] = useState<Conversation | null>(null)
  const [members, setMembers] = useState<Member[]>([])
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    Promise.all([conversationsApi.get(conversationId), conversationsApi.members(conversationId)])
      .then(([found, list]) => {
        setConversation(found)
        setMembers(list.items)
        setError(null)
      })
      .catch((caught) =>
        setError(caught instanceof ApiError && caught.status === 404 ? 'Conversation not found.' : 'Could not load.'),
      )
  }, [conversationId])

  useEffect(load, [load])
  const memberIds = useMemo(() => new Set(members.map((m) => m.user.id)), [members])

  if (auth.status !== 'signed-in') return null
  if (error) return <p role="alert">{error}</p>
  if (!conversation) return <p aria-busy="true">Loading…</p>

  const me = auth.user
  const isOwner = conversation.my_role === 'owner'
  const isGroup = conversation.type === 'group'

  async function run(action: () => Promise<unknown>) {
    try {
      await action()
      load()
      refresh()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'That did not work.')
    }
  }

  async function leave() {
    try {
      await conversationsApi.removeMember(conversation!.id, me.id)
      refresh()
      navigate('/')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not leave.')
    }
  }

  return (
    <article>
      <h1>{conversationName(conversation, me.id)}</h1>
      {isGroup && isOwner && <RenameForm current={conversation.title ?? ''} onRename={(t) => run(() => conversationsApi.rename(conversation.id, t))} />}

      <h2>Members ({members.length})</h2>
      <ul className="members">
        {members.map((member) => (
          <li key={member.user.id}>
            {member.user.display_name} <small>@{member.user.username}</small>
            {member.role === 'owner' && <small> · owner</small>}
            {isGroup && isOwner && member.user.id !== me.id && (
              <button type="button" onClick={() => run(() => conversationsApi.removeMember(conversation.id, member.user.id))}>
                Remove
              </button>
            )}
          </li>
        ))}
      </ul>

      {isGroup && isOwner && (
        <UserSearch
          label="Add a member"
          excludeIds={memberIds}
          onSelect={(user) => void run(() => conversationsApi.addMembers(conversation.id, [user.id]))}
        />
      )}
      {isGroup && (
        <button type="button" onClick={() => void leave()}>
          Leave group
        </button>
      )}
    </article>
  )
}

function RenameForm({ current, onRename }: { current: string; onRename: (title: string) => void }) {
  const [title, setTitle] = useState(current)
  function submit(event: FormEvent) {
    event.preventDefault()
    onRename(title)
  }
  return (
    <form onSubmit={submit} className="inline">
      <label>
        Group name
        <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={100} required />
      </label>
      <button type="submit">Rename</button>
    </form>
  )
}
