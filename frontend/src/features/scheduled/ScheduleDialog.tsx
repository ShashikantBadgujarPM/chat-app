import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError } from '../../api/client'
import type { Message } from '../messages/api'
import { isoToLocalInput, localInputToIso, localTimezone, scheduledApi } from './api'

type Props = {
  conversationId: string
  initialBody: string
  replyTo: Message | null
  onClose: () => void
  onScheduled: (scheduledAt: string) => void
}

function defaultLocalValue(): string {
  return isoToLocalInput(new Date(Date.now() + 120_000).toISOString())
}

/** "Schedule send": pick a body and a local date/time, sent as an offset-qualified
 * instant (docs/design/07 §Scheduled messages). No close() on unmount: see the
 * New group dialog fix — StrictMode's double effect would fire onClose right away. */
export function ScheduleDialog({ conversationId, initialBody, replyTo, onClose, onScheduled }: Props) {
  const ref = useRef<HTMLDialogElement>(null)
  const [body, setBody] = useState(initialBody)
  const [when, setWhen] = useState(defaultLocalValue)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    const dialog = ref.current
    if (dialog && !dialog.open) dialog.showModal()
  }, [])

  async function submit(event: FormEvent) {
    event.preventDefault()
    const iso = localInputToIso(when)
    if (!iso) {
      setError('Pick a date and time.')
      return
    }
    if (new Date(iso).getTime() <= Date.now()) {
      // The exact minimum lead (30 s) is enforced server-side and reported there: the
      // datetime-local input has no seconds, so a client-side buffer would reject
      // valid picks made near a minute boundary.
      setError('Pick a time in the future.')
      return
    }
    setSaving(true)
    setError(null)
    try {
      const created = await scheduledApi.create({
        client_message_id: crypto.randomUUID(),
        conversation_id: conversationId,
        body: body.trim(),
        ...(replyTo ? { reply_to_id: replyTo.id } : {}),
        scheduled_at: iso,
        timezone: localTimezone(),
      })
      onScheduled(created.scheduled_at)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not schedule the message.')
      setSaving(false)
    }
  }

  return (
    <dialog ref={ref} aria-label="Schedule send" onClose={onClose}>
      <h2>Schedule send</h2>
      <form onSubmit={submit}>
        {replyTo && (
          <p className="replying-to">
            Replying to <strong>{replyTo.sender?.display_name ?? 'Deleted user'}</strong>
          </p>
        )}
        <label>
          Message
          <textarea
            value={body}
            onChange={(e) => setBody(e.target.value)}
            maxLength={4000}
            rows={3}
            required
          />
        </label>
        <label>
          Send at
          <input
            type="datetime-local"
            value={when}
            onChange={(e) => setWhen(e.target.value)}
            required
          />
        </label>
        <p>
          <small>Your time zone: {localTimezone()}</small>
        </p>
        {error && <p role="alert">{error}</p>}
        <div className="dialog-actions">
          <button type="button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button type="submit" disabled={saving || !body.trim()}>
            {saving ? 'Scheduling…' : 'Schedule'}
          </button>
        </div>
      </form>
    </dialog>
  )
}
