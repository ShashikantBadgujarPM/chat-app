import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { ApiError } from '../../api/client'
import { messagesApi } from './api'
import type { Message } from './api'

type Pending = {
  clientMessageId: string
  body: string
  replyTo: Message | null
  state: 'sending' | 'failed'
}

type Props = { conversationId: string; myId: string; onSent?: () => void }

/**
 * Message history and composer (M05). Updates come from REST for now; M06 adds live
 * WebSocket events.
 */
export function MessagePane({ conversationId, myId, onSent }: Props) {
  const [messages, setMessages] = useState<Message[]>([]) // ascending by seq
  const [hasMore, setHasMore] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [pending, setPending] = useState<Pending[]>([])
  const [replyTo, setReplyTo] = useState<Message | null>(null)
  const [error, setError] = useState<string | null>(null)
  const listRef = useRef<HTMLOListElement>(null)
  const keepScroll = useRef<{ height: number; top: number } | null>(null)
  const stickToBottom = useRef(true)

  useEffect(() => {
    let cancelled = false
    messagesApi
      .latest(conversationId)
      .then((page) => {
        if (cancelled) return
        setMessages([...page.items].reverse())
        setHasMore(page.has_more)
        stickToBottom.current = true
      })
      .catch(() => !cancelled && setError('Could not load messages.'))
    return () => {
      cancelled = true
    }
  }, [conversationId])

  // Keep the view steady when older messages are prepended, or pinned to the bottom.
  useLayoutEffect(() => {
    const list = listRef.current
    if (!list) return
    if (keepScroll.current) {
      list.scrollTop = keepScroll.current.top + (list.scrollHeight - keepScroll.current.height)
      keepScroll.current = null
    } else if (stickToBottom.current) {
      list.scrollTop = list.scrollHeight
    }
  }, [messages, pending])

  const loadOlder = useCallback(async () => {
    if (!hasMore || loadingOlder || messages.length === 0) return
    const list = listRef.current
    setLoadingOlder(true)
    try {
      const page = await messagesApi.before(conversationId, messages[0].seq)
      if (list) keepScroll.current = { height: list.scrollHeight, top: list.scrollTop }
      setMessages((current) => [...[...page.items].reverse(), ...current])
      setHasMore(page.has_more)
    } finally {
      setLoadingOlder(false)
    }
  }, [conversationId, hasMore, loadingOlder, messages])

  function onScroll() {
    const list = listRef.current
    if (!list) return
    stickToBottom.current = list.scrollHeight - list.scrollTop - list.clientHeight < 40
    if (list.scrollTop < 80) void loadOlder()
  }

  const upsert = useCallback((message: Message) => {
    setMessages((current) => {
      const index = current.findIndex((m) => m.id === message.id)
      if (index >= 0) return current.map((m) => (m.id === message.id ? message : m))
      return [...current, message].sort((a, b) => a.seq - b.seq)
    })
  }, [])

  async function deliver(item: Pending) {
    setPending((current) =>
      current.map((p) => (p.clientMessageId === item.clientMessageId ? { ...p, state: 'sending' } : p)),
    )
    try {
      // Retries reuse the same client_message_id, so the server never duplicates it.
      const message = await messagesApi.send(conversationId, {
        client_message_id: item.clientMessageId,
        body: item.body,
        ...(item.replyTo ? { reply_to_id: item.replyTo.id } : {}),
      })
      setPending((current) => current.filter((p) => p.clientMessageId !== item.clientMessageId))
      upsert(message)
      onSent?.()
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 422) {
        setError(caught.message)
        setPending((current) => current.filter((p) => p.clientMessageId !== item.clientMessageId))
        return
      }
      setPending((current) =>
        current.map((p) => (p.clientMessageId === item.clientMessageId ? { ...p, state: 'failed' } : p)),
      )
    }
  }

  function submit(body: string) {
    const item: Pending = {
      clientMessageId: crypto.randomUUID(),
      body,
      replyTo,
      state: 'sending',
    }
    stickToBottom.current = true
    setPending((current) => [...current, item])
    setReplyTo(null)
    setError(null)
    void deliver(item)
  }

  async function edit(message: Message) {
    const next = window.prompt('Edit message', message.body ?? '')
    if (next === null || next.trim() === message.body) return
    try {
      upsert(await messagesApi.edit(message.id, next))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not edit.')
    }
  }

  async function remove(message: Message) {
    if (!window.confirm('Delete this message?')) return
    try {
      await messagesApi.remove(message.id)
      upsert({ ...message, body: null, deleted_at: new Date().toISOString(), reply_to: message.reply_to })
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not delete.')
    }
  }

  return (
    <div className="message-pane">
      <ol className="messages" ref={listRef} onScroll={onScroll} aria-label="Messages">
        {hasMore && (
          <li className="load-older">
            <button type="button" onClick={() => void loadOlder()} disabled={loadingOlder}>
              {loadingOlder ? 'Loading…' : 'Load older messages'}
            </button>
          </li>
        )}
        {messages.map((message) => (
          <MessageItem
            key={message.id}
            message={message}
            mine={message.sender?.id === myId}
            onReply={() => setReplyTo(message)}
            onEdit={() => void edit(message)}
            onDelete={() => void remove(message)}
          />
        ))}
        {pending.map((item) => (
          <li key={item.clientMessageId} className={`message mine pending ${item.state}`}>
            <p>{item.body}</p>
            {item.state === 'sending' ? (
              <small>Sending…</small>
            ) : (
              <small>
                Not sent.{' '}
                <button type="button" className="link" onClick={() => void deliver(item)}>
                  Retry
                </button>
              </small>
            )}
          </li>
        ))}
      </ol>
      {error && <p role="alert">{error}</p>}
      <Composer replyTo={replyTo} onCancelReply={() => setReplyTo(null)} onSubmit={submit} />
    </div>
  )
}

function MessageItem({
  message,
  mine,
  onReply,
  onEdit,
  onDelete,
}: {
  message: Message
  mine: boolean
  onReply: () => void
  onEdit: () => void
  onDelete: () => void
}) {
  const deleted = message.deleted_at !== null
  return (
    <li className={`message${mine ? ' mine' : ''}${deleted ? ' deleted' : ''}`}>
      <header>
        <strong>{message.sender?.display_name ?? 'Deleted user'}</strong>{' '}
        <time dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString()}</time>
        {message.edited_at && !deleted && <small> (edited)</small>}
      </header>
      {message.reply_to && (
        <blockquote className="reply-preview">
          {message.reply_to.deleted ? <em>This message was deleted.</em> : message.reply_to.body_preview}
        </blockquote>
      )}
      {deleted ? <p><em>This message was deleted.</em></p> : <p>{message.body}</p>}
      {!deleted && (
        <div className="message-actions">
          <button type="button" className="link" onClick={onReply}>
            Reply
          </button>
          {mine && (
            <>
              <button type="button" className="link" onClick={onEdit}>
                Edit
              </button>
              <button type="button" className="link" onClick={onDelete}>
                Delete
              </button>
            </>
          )}
        </div>
      )}
    </li>
  )
}

function Composer({
  replyTo,
  onCancelReply,
  onSubmit,
}: {
  replyTo: Message | null
  onCancelReply: () => void
  onSubmit: (body: string) => void
}) {
  const [body, setBody] = useState('')

  function send(event?: FormEvent) {
    event?.preventDefault()
    const text = body.trim()
    if (!text) return
    onSubmit(text)
    setBody('')
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      send()
    }
  }

  return (
    <form className="composer" onSubmit={send}>
      {replyTo && (
        <div className="replying-to">
          Replying to <strong>{replyTo.sender?.display_name ?? 'Deleted user'}</strong>:{' '}
          {replyTo.body?.slice(0, 80)}{' '}
          <button type="button" className="link" onClick={onCancelReply}>
            Cancel
          </button>
        </div>
      )}
      <label className="visually-hidden" htmlFor="composer-input">
        Message
      </label>
      <textarea
        id="composer-input"
        value={body}
        onChange={(e) => setBody(e.target.value)}
        onKeyDown={onKeyDown}
        maxLength={4000}
        rows={2}
        placeholder="Write a message (Enter to send, Shift+Enter for a new line)"
      />
      <button type="submit" disabled={!body.trim()}>
        Send
      </button>
    </form>
  )
}
