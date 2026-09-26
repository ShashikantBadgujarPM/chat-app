import { useCallback, useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { useNavigate, useOutletContext, useParams } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { useAuth } from '../auth/AuthContext'
import { chatSocket } from '../../ws/ChatSocket'
import { MessagePane } from '../messages/MessagePane'
import { PresenceDot } from '../presence/PresenceDot'
import { UserSearch } from '../users/UserSearch'
import { conversationName, conversationsApi } from './api'
import type { Conversation, Member } from './api'
import type { ConversationsOutletContext } from './ConversationsLayout'

export function NoConversationSelected() {
  return <p className="empty">Select a conversation, or start a new one.</p>
}

/** One conversation: its messages, and details plus member management beside them. */
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

  // Membership or title changed elsewhere: reload; if I was removed, leave the view.
  useEffect(
    () =>
      chatSocket.subscribe((event) => {
        if (event.conversation_id !== conversationId) return
        if (event.type === 'receipt.updated') {
          const receipt = event.payload as { user_id: string; last_read_seq: number }
          setMembers((current) =>
            current.map((m) =>
              m.user.id === receipt.user_id
                ? { ...m, last_read_seq: Math.max(m.last_read_seq ?? 0, receipt.last_read_seq) }
                : m,
            ),
          )
          return
        }
        if (!event.type.startsWith('conversation.')) return
        if (event.type === 'receipt.updated') return // handled below, no reload needed
        const removed = event.payload as { user_id?: string }
        if (event.type === 'conversation.member_removed' && removed.user_id === auth.user?.id) {
          navigate('/')
          return
        }
        load()
      }),
    [conversationId, load, navigate, auth.user?.id],
  )
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
    <div className="conversation">
      <article className="conversation-main">
        <h1>{conversationName(conversation, me.id)}</h1>
        <MessagePane
          key={conversation.id}
          conversationId={conversation.id}
          myId={me.id}
          memberNames={Object.fromEntries(members.map((m) => [m.user.id, m.user.display_name]))}
          myLastReadSeq={members.find((m) => m.user.id === me.id)?.last_read_seq ?? 0}
          otherReadSeq={
            conversation.type === 'direct'
              ? (members.find((m) => m.user.id !== me.id)?.last_read_seq ?? null)
              : null
          }
          onSent={refresh}
        />
      </article>
      <ConversationDetails
        conversation={conversation}
        members={members}
        memberIds={memberIds}
        me={me.id}
        isOwner={isOwner}
        isGroup={isGroup}
        run={run}
        leave={leave}
      />
    </div>
  )
}

function ConversationDetails({
  conversation,
  members,
  memberIds,
  me,
  isOwner,
  isGroup,
  run,
  leave,
}: {
  conversation: Conversation
  members: Member[]
  memberIds: Set<string>
  me: string
  isOwner: boolean
  isGroup: boolean
  run: (action: () => Promise<unknown>) => Promise<void>
  leave: () => Promise<void>
}) {
  return (
    <aside className="conversation-details" aria-label="Conversation details">
      {isGroup && isOwner && <RenameForm current={conversation.title ?? ''} onRename={(t) => run(() => conversationsApi.rename(conversation.id, t))} />}

      <h2>Members ({members.length})</h2>
      <ul className="members">
        {members.map((member) => (
          <li key={member.user.id}>
            <PresenceDot userId={member.user.id} initial={member.user.presence} />
            {member.user.display_name} <small>@{member.user.username}</small>
            {member.role === 'owner' && <small> · owner</small>}
            {isGroup && isOwner && member.user.id !== me && (
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
    </aside>
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
