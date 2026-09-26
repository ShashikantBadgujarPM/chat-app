import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { ApiError } from '../../api/client'
import { useAuth } from '../auth/AuthContext'
import { chatSocket } from '../../ws/ChatSocket'
import type { SocketStatus } from '../../ws/ChatSocket'
import { UserSearch } from '../users/UserSearch'
import type { UserPublic } from '../users/api'
import { conversationName, conversationsApi } from './api'
import type { ConversationSummary } from './api'

export type ConversationsOutletContext = { refresh: () => void }

/** Sidebar with the caller's conversations; the selected conversation renders in the outlet. */
export function ConversationsLayout() {
  const auth = useAuth()
  const navigate = useNavigate()
  const [items, setItems] = useState<ConversationSummary[]>([])
  const [error, setError] = useState<string | null>(null)
  const [dialog, setDialog] = useState<'dm' | 'group' | null>(null)

  const refresh = useCallback(() => {
    conversationsApi
      .list()
      .then((page) => {
        setItems(page.items)
        setError(null)
      })
      .catch(() => setError('Could not load conversations.'))
  }, [])

  useEffect(refresh, [refresh])

  // Keep the sidebar live: new conversations, renames, membership, new last messages.
  // Coalesce bursts of events into one reload.
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const unsubscribe = chatSocket.subscribe((event) => {
      if (!event.type.startsWith('conversation.') && event.type !== 'message.created') return
      if (timer) clearTimeout(timer)
      timer = setTimeout(refresh, 150)
    })
    return () => {
      unsubscribe()
      if (timer) clearTimeout(timer)
    }
  }, [refresh])

  const [socketStatus, setSocketStatus] = useState<SocketStatus>(chatSocket.status)
  useEffect(() => chatSocket.onStatus(setSocketStatus), [])

  if (auth.status !== 'signed-in') return null
  const me = auth.user

  async function startDirect(user: UserPublic) {
    const conversation = await conversationsApi.startDirect(user.id)
    setDialog(null)
    refresh()
    navigate(`/c/${conversation.id}`)
  }

  return (
    <div className="layout">
      <aside aria-label="Conversations">
        <header>
          <strong>{me.display_name}</strong>
          <small className={`socket-status ${socketStatus}`} role="status">
            {socketStatus === 'online' ? 'Live' : socketStatus === 'connecting' ? 'Connecting…' : 'Offline'}
          </small>
          <nav>
            <Link to="/me">Profile</Link> ·{' '}
            <button type="button" className="link" onClick={() => void auth.logout()}>
              Sign out
            </button>
          </nav>
        </header>
        <div className="actions">
          <button type="button" onClick={() => setDialog('dm')}>
            New message
          </button>
          <button type="button" onClick={() => setDialog('group')}>
            New group
          </button>
        </div>
        {error && <p role="alert">{error}</p>}
        <ul className="conversation-list">
          {items.length === 0 && !error && <li className="empty">No conversations yet</li>}
          {items.map((conversation) => (
            <li key={conversation.id}>
              <NavLink to={`/c/${conversation.id}`}>
                {conversationName(conversation, me.id)}
                {conversation.type === 'group' && <small> · {conversation.member_count} members</small>}
              </NavLink>
            </li>
          ))}
        </ul>
      </aside>
      <section className="content">
        <Outlet context={{ refresh } satisfies ConversationsOutletContext} />
      </section>
      {dialog === 'dm' && (
        <Dialog title="New message" onClose={() => setDialog(null)}>
          <UserSearch label="Send a message to" onSelect={(user) => void startDirect(user)} />
        </Dialog>
      )}
      {dialog === 'group' && (
        <Dialog title="New group" onClose={() => setDialog(null)}>
          <NewGroupForm
            onCreated={(id) => {
              setDialog(null)
              refresh()
              navigate(`/c/${id}`)
            }}
          />
        </Dialog>
      )}
    </div>
  )
}

function Dialog({ title, onClose, children }: { title: string; onClose: () => void; children: React.ReactNode }) {
  const ref = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    const dialog = ref.current
    dialog?.showModal()
    return () => dialog?.close()
  }, [])
  return (
    <dialog ref={ref} onClose={onClose} aria-label={title}>
      <h2>{title}</h2>
      {children}
      <button type="button" onClick={onClose}>
        Cancel
      </button>
    </dialog>
  )
}

function NewGroupForm({ onCreated }: { onCreated: (id: string) => void }) {
  const [title, setTitle] = useState('')
  const [members, setMembers] = useState<UserPublic[]>([])
  const [error, setError] = useState<string | null>(null)
  const chosen = new Set(members.map((member) => member.id))

  async function submit(event: FormEvent) {
    event.preventDefault()
    try {
      const conversation = await conversationsApi.createGroup(
        title,
        members.map((member) => member.id),
      )
      onCreated(conversation.id)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not create the group.')
    }
  }

  return (
    <form onSubmit={submit}>
      <label>
        Group name
        <input value={title} onChange={(e) => setTitle(e.target.value)} maxLength={100} required />
      </label>
      <UserSearch
        label="Add people"
        excludeIds={chosen}
        onSelect={(user) => setMembers((current) => [...current, user])}
      />
      <ul>
        {members.map((member) => (
          <li key={member.id}>
            {member.display_name}{' '}
            <button
              type="button"
              aria-label={`Remove ${member.display_name}`}
              onClick={() => setMembers((current) => current.filter((m) => m.id !== member.id))}
            >
              ×
            </button>
          </li>
        ))}
      </ul>
      {error && <p role="alert">{error}</p>}
      <button type="submit">Create group</button>
    </form>
  )
}
