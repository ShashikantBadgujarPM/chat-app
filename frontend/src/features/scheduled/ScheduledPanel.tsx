import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError } from '../../api/client'
import { chatSocket } from '../../ws/ChatSocket'
import { useAuth } from '../auth/AuthContext'
import { conversationName, conversationsApi } from '../conversations/api'
import type { Conversation } from '../conversations/api'
import { isoToLocalInput, localInputToIso, scheduledApi } from './api'
import type { ScheduledMessage, ScheduledStatus } from './api'

const TABS: { label: string; status: ScheduledStatus | null }[] = [
  { label: 'Pending', status: 'pending' },
  { label: 'Sent', status: 'sent' },
  { label: 'Failed', status: 'failed' },
  { label: 'Cancelled', status: 'cancelled' },
  { label: 'All', status: null },
]

/** All the caller's scheduled messages, across conversations (docs/design/07). */
export function ScheduledPanel() {
  const auth = useAuth()
  const [tab, setTab] = useState<ScheduledStatus | null>('pending')
  const [items, setItems] = useState<ScheduledMessage[]>([])
  const [conversations, setConversations] = useState<Record<string, Conversation>>({})
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  const load = useCallback(() => {
    setLoading(true)
    scheduledApi
      .list({ status: tab ?? undefined })
      .then((page) => {
        setItems(page.items)
        setError(null)
      })
      .catch(() => setError('Could not load scheduled messages.'))
      .finally(() => setLoading(false))
  }, [tab])

  useEffect(load, [load])

  // Conversation names for display; a best-effort lookup over the caller's own list.
  useEffect(() => {
    conversationsApi
      .list()
      .then((page) => setConversations(Object.fromEntries(page.items.map((c) => [c.id, c]))))
      .catch(() => undefined)
  }, [])

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null
    const unsubscribe = chatSocket.subscribe((event) => {
      if (!event.type.startsWith('scheduled_message.')) return
      if (timer) clearTimeout(timer)
      timer = setTimeout(load, 150)
    })
    return () => {
      unsubscribe()
      if (timer) clearTimeout(timer)
    }
  }, [load])

  if (auth.status !== 'signed-in') return null

  return (
    <div className="scheduled-panel">
      <h1>Scheduled messages</h1>
      <nav className="tabs" aria-label="Filter by status">
        {TABS.map((t) => (
          <button
            key={t.label}
            type="button"
            className={t.status === tab ? 'active' : ''}
            onClick={() => setTab(t.status)}
          >
            {t.label}
          </button>
        ))}
      </nav>
      {error && <p role="alert">{error}</p>}
      {loading ? (
        <p aria-busy="true">Loading…</p>
      ) : items.length === 0 ? (
        <p className="empty">Nothing here.</p>
      ) : (
        <ul className="scheduled-list">
          {items.map((item) => (
            <ScheduledRow
              key={item.id}
              item={item}
              conversation={conversations[item.conversation_id]}
              myId={auth.user.id}
              onChanged={load}
            />
          ))}
        </ul>
      )}
    </div>
  )
}

function ScheduledRow({
  item,
  conversation,
  myId,
  onChanged,
}: {
  item: ScheduledMessage
  conversation: Conversation | undefined
  myId: string
  onChanged: () => void
}) {
  const [editing, setEditing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChanged()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'That did not work.')
    } finally {
      setBusy(false)
    }
  }

  const title = conversation ? conversationName(conversation, myId) : item.conversation_id

  return (
    <li className={`scheduled-item status-${item.status}`}>
      <header>
        <strong>{title}</strong>
        <span className="badge">{item.status}</span>
      </header>
      {editing ? (
        <EditForm
          item={item}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false)
            onChanged()
          }}
        />
      ) : (
        <>
          <p>{item.body}</p>
          <p>
            <small>
              For {new Date(item.scheduled_at).toLocaleString()} ({item.timezone})
              {item.status === 'failed' && item.last_error && <> · {item.last_error}</>}
              {item.status === 'sent' && item.sent_at && (
                <> · sent {new Date(item.sent_at).toLocaleString()}</>
              )}
            </small>
          </p>
          {error && <p role="alert">{error}</p>}
          <div className="scheduled-actions">
            {item.status === 'pending' && (
              <>
                <button type="button" className="link" onClick={() => setEditing(true)} disabled={busy}>
                  Edit
                </button>
                <button
                  type="button"
                  className="link"
                  onClick={() => void run(() => scheduledApi.cancel(item.id))}
                  disabled={busy}
                >
                  Cancel
                </button>
              </>
            )}
            {item.status === 'failed' && (
              <button
                type="button"
                className="link"
                onClick={() => void run(() => scheduledApi.retry(item.id))}
                disabled={busy}
              >
                Retry
              </button>
            )}
          </div>
        </>
      )}
    </li>
  )
}

function EditForm({
  item,
  onCancel,
  onSaved,
}: {
  item: ScheduledMessage
  onCancel: () => void
  onSaved: () => void
}) {
  const [body, setBody] = useState(item.body)
  const [when, setWhen] = useState(() => isoToLocalInput(item.scheduled_at))
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const firstField = useRef<HTMLTextAreaElement>(null)

  useEffect(() => firstField.current?.focus(), [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const iso = localInputToIso(when)
    if (!iso) {
      setError('Pick a date and time.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      await scheduledApi.update(item.id, { body: body.trim(), scheduled_at: iso })
      onSaved()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not save.')
      setSaving(false)
    }
  }

  return (
    <form onSubmit={submit} className="scheduled-edit">
      <label>
        Message
        <textarea
          ref={firstField}
          value={body}
          onChange={(e) => setBody(e.target.value)}
          maxLength={4000}
          rows={3}
          required
        />
      </label>
      <label>
        Send at
        <input type="datetime-local" value={when} onChange={(e) => setWhen(e.target.value)} required />
      </label>
      {error && <p role="alert">{error}</p>}
      <div className="dialog-actions">
        <button type="button" onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        <button type="submit" disabled={saving || !body.trim()}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </form>
  )
}
