import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { FormEvent, KeyboardEvent } from 'react'
import { ApiError, api } from '../../api/client'
import { chatSocket } from '../../ws/ChatSocket'
import { messagesApi } from './api'
import type { Message } from './api'
import { typingText } from '../presence/presence'
import { CursorSender } from './readCursor'
import { markDeleted, upsertMessage } from './reducer'
import type { MessageState, Pending } from './reducer'
import { ScheduleDialog } from '../scheduled/ScheduleDialog'

type Props = {
  conversationId: string
  myId: string
  /** Display names of the members, for the typing line. */
  memberNames: Record<string, string>
  myLastReadSeq: number
  /** In a DM, how far the other member has read (for "Seen"); null elsewhere. */
  otherReadSeq: number | null
  onSent?: () => void
}

/** Message history, live updates (M06) and the composer. */
export function MessagePane({
  conversationId,
  myId,
  memberNames,
  myLastReadSeq,
  otherReadSeq,
  onSent,
}: Props) {
  // Who is typing, with when their indicator expires (receivers expire it themselves).
  const [typing, setTyping] = useState<Record<string, number>>({})
  useEffect(
    () =>
      chatSocket.subscribe((event) => {
        if (event.type !== 'typing.updated' || event.conversation_id !== conversationId) return
        const t = event.payload as { user_id: string; is_typing: boolean; expires_in_s: number }
        setTyping((current) => {
          const next = { ...current }
          if (t.is_typing) next[t.user_id] = Date.now() + t.expires_in_s * 1000
          else delete next[t.user_id]
          return next
        })
      }),
    [conversationId],
  )
  useEffect(() => {
    const timer = setInterval(() => {
      setTyping((current) => {
        const now = Date.now()
        const live = Object.fromEntries(Object.entries(current).filter(([, until]) => until > now))
        return Object.keys(live).length === Object.keys(current).length ? current : live
      })
    }, 1000)
    return () => clearInterval(timer)
  }, [])
  const typingLine = typingText(
    Object.keys(typing)
      .filter((id) => id !== myId)
      .map((id) => memberNames[id] ?? 'Someone'),
  )

  const [state, setState] = useState<MessageState>({ messages: [], pending: [] })
  const [hasMore, setHasMore] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const [replyTo, setReplyTo] = useState<Message | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  // Non-null while the "Schedule send" dialog is open, holding the composer's draft.
  const [scheduleDraft, setScheduleDraft] = useState<string | null>(null)
  const listRef = useRef<HTMLOListElement>(null)
  const keepScroll = useRef<{ height: number; top: number } | null>(null)
  const stickToBottom = useRef(true)
  const { messages, pending } = state

  // Move the read cursor while the conversation is actually being read (M07).
  const [sender] = useState(
    () =>
      new CursorSender(
        (seq) =>
          void api(`/api/v1/conversations/${conversationId}/read-cursor`, {
            method: 'PUT',
            body: { last_read_seq: seq },
          }).catch(() => undefined),
        500,
        myLastReadSeq,
      ),
  )
  const latestSeq = messages.length ? messages[messages.length - 1].seq : 0
  const reportReading = useCallback(() => {
    sender.update(latestSeq, {
      visible: document.visibilityState === 'visible',
      focused: document.hasFocus(),
      atBottom: stickToBottom.current,
    })
  }, [sender, latestSeq])

  useEffect(() => {
    reportReading()
    window.addEventListener('focus', reportReading)
    document.addEventListener('visibilitychange', reportReading)
    return () => {
      window.removeEventListener('focus', reportReading)
      document.removeEventListener('visibilitychange', reportReading)
    }
  }, [reportReading])
  useEffect(() => () => sender.dispose(), [sender])

  useEffect(() => {
    // State starts empty per conversation: the parent keys this component by id.
    let cancelled = false
    messagesApi
      .latest(conversationId)
      .then((page) => {
        if (cancelled) return
        // Live events may already have arrived; merge rather than replace.
        setState((current) => [...page.items].reverse().reduce(upsertMessage, current))
        setHasMore(page.has_more)
        stickToBottom.current = true
      })
      .catch(() => !cancelled && setError('Could not load messages.'))
    return () => {
      cancelled = true
    }
  }, [conversationId])

  // Live events for this conversation (08 §15.4).
  useEffect(
    () =>
      chatSocket.subscribe((event) => {
        if (event.conversation_id !== conversationId) return
        if (event.type === 'conversation.read') {
          sender.acknowledge(Number((event.payload as { last_read_seq: number }).last_read_seq))
        } else if (event.type === 'message.created' || event.type === 'message.updated') {
          setState((current) => upsertMessage(current, event.payload as unknown as Message))
        } else if (event.type === 'message.deleted') {
          setState((current) =>
            markDeleted(current, event.payload as unknown as { id: string; deleted_at: string | null }),
          )
        }
      }),
    [conversationId, sender],
  )

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
      setState((current) => page.items.reduce(upsertMessage, current))
      setHasMore(page.has_more)
    } finally {
      setLoadingOlder(false)
    }
  }, [conversationId, hasMore, loadingOlder, messages])

  function onScroll() {
    const list = listRef.current
    if (!list) return
    stickToBottom.current = list.scrollHeight - list.scrollTop - list.clientHeight < 40
    reportReading()
    if (list.scrollTop < 80) void loadOlder()
  }

  function setPendingState(clientMessageId: string, pendingState: Pending['state']) {
    setState((current) => ({
      ...current,
      pending: current.pending.map((p) =>
        p.clientMessageId === clientMessageId ? { ...p, state: pendingState } : p,
      ),
    }))
  }

  async function deliver(item: Pending) {
    setPendingState(item.clientMessageId, 'sending')
    try {
      // Retries reuse the same client_message_id, so the server never duplicates it.
      const message = await messagesApi.send(conversationId, {
        client_message_id: item.clientMessageId,
        body: item.body,
        ...(item.replyToId ? { reply_to_id: item.replyToId } : {}),
      })
      setState((current) => upsertMessage(current, message))
      onSent?.()
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 422) {
        setError(caught.message)
        setState((current) => ({
          ...current,
          pending: current.pending.filter((p) => p.clientMessageId !== item.clientMessageId),
        }))
        return
      }
      setPendingState(item.clientMessageId, 'failed')
    }
  }

  function submit(body: string) {
    const item: Pending = {
      clientMessageId: crypto.randomUUID(),
      body,
      replyToId: replyTo?.id ?? null,
      state: 'sending',
    }
    stickToBottom.current = true
    setState((current) => ({ ...current, pending: [...current.pending, item] }))
    setReplyTo(null)
    setError(null)
    void deliver(item)
  }

  function openSchedule(body: string) {
    setScheduleDraft(body)
    setError(null)
    setNotice(null)
  }

  function scheduled(scheduledAt: string) {
    setScheduleDraft(null)
    setReplyTo(null)
    setNotice(`Message scheduled for ${new Date(scheduledAt).toLocaleString()}.`)
  }

  async function edit(message: Message) {
    const next = window.prompt('Edit message', message.body ?? '')
    if (next === null || next.trim() === message.body) return
    try {
      const updated = await messagesApi.edit(message.id, next)
      setState((current) => upsertMessage(current, updated))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not edit.')
    }
  }

  async function remove(message: Message) {
    if (!window.confirm('Delete this message?')) return
    try {
      await messagesApi.remove(message.id)
      setState((current) => markDeleted(current, { id: message.id, deleted_at: null }))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not delete.')
    }
  }

  const lastMine = [...messages].reverse().find((m) => m.sender?.id === myId && !m.deleted_at)
  const lastMineId = lastMine?.id

  return (
    <div className="message-pane">
      <ol className="messages" ref={listRef} onScroll={onScroll} aria-label="Messages" aria-live="polite">
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
            seen={message.id === lastMineId && otherReadSeq !== null && otherReadSeq >= message.seq}
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
      <p className="typing-line" aria-live="polite">
        {typingLine ?? '\u00a0'}
      </p>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <Composer
        conversationId={conversationId}
        replyTo={replyTo}
        onCancelReply={() => setReplyTo(null)}
        onSubmit={submit}
        onSchedule={openSchedule}
      />
      {scheduleDraft !== null && (
        <ScheduleDialog
          conversationId={conversationId}
          initialBody={scheduleDraft}
          replyTo={replyTo}
          onClose={() => setScheduleDraft(null)}
          onScheduled={scheduled}
        />
      )}
    </div>
  )
}

function MessageItem({
  message,
  mine,
  seen,
  onReply,
  onEdit,
  onDelete,
}: {
  message: Message
  mine: boolean
  seen: boolean
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
        {message.scheduled_message_id && <small> · scheduled</small>}
      </header>
      {message.reply_to && (
        <blockquote className="reply-preview">
          {message.reply_to.deleted ? <em>This message was deleted.</em> : message.reply_to.body_preview}
        </blockquote>
      )}
      {deleted ? (
        <p>
          <em>This message was deleted.</em>
        </p>
      ) : (
        <p>{message.body}</p>
      )}
      {seen && <small className="seen">Seen</small>}
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

const TYPING_REPEAT_MS = 3000

function Composer({
  conversationId,
  replyTo,
  onCancelReply,
  onSubmit,
  onSchedule,
}: {
  conversationId: string
  replyTo: Message | null
  onCancelReply: () => void
  onSubmit: (body: string) => void
  onSchedule: (body: string) => void
}) {
  const [body, setBody] = useState('')
  const lastTypingSent = useRef(0)

  // typing.start at most every 3 s while typing; typing.stop on send, blur or empty.
  function typingStart() {
    const now = Date.now()
    if (now - lastTypingSent.current < TYPING_REPEAT_MS) return
    if (chatSocket.send({ type: 'typing.start', payload: { conversation_id: conversationId } })) {
      lastTypingSent.current = now
    }
  }

  function typingStop() {
    if (lastTypingSent.current === 0) return
    lastTypingSent.current = 0
    chatSocket.send({ type: 'typing.stop', payload: { conversation_id: conversationId } })
  }

  function change(value: string) {
    setBody(value)
    if (value.trim()) typingStart()
    else typingStop()
  }

  function send(event?: FormEvent) {
    event?.preventDefault()
    const text = body.trim()
    if (!text) return
    onSubmit(text)
    setBody('')
    typingStop()
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      send()
    }
  }

  function schedule() {
    const text = body.trim()
    if (!text) return
    onSchedule(text)
    setBody('')
    typingStop()
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
        onChange={(e) => change(e.target.value)}
        onBlur={typingStop}
        onKeyDown={onKeyDown}
        maxLength={4000}
        rows={2}
        placeholder="Write a message (Enter to send, Shift+Enter for a new line)"
      />
      <button type="submit" disabled={!body.trim()}>
        Send
      </button>
      <button type="button" onClick={schedule} disabled={!body.trim()}>
        Schedule send
      </button>
    </form>
  )
}
