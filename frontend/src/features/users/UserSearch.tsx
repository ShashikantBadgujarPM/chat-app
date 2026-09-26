import { useEffect, useId, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { usersApi } from './api'
import type { UserPublic } from './api'

const DEBOUNCE_MS = 250
const MIN_QUERY = 2

type Props = {
  label: string
  onSelect: (user: UserPublic) => void
  /** Users that can't be picked (e.g. already members). */
  excludeIds?: ReadonlySet<string>
}

/**
 * A debounced user-search combobox (ARIA 1.2 pattern). Reused by "start a DM" and
 * "add a member" (M04).
 */
export function UserSearch({ label, onSelect, excludeIds }: Props) {
  const inputId = useId()
  const listId = useId()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<UserPublic[]>([])
  const [active, setActive] = useState(-1)
  const [open, setOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const latestRequest = useRef(0)

  const trimmed = query.trim()
  // Derived during render: a too-short query simply hides the list.
  const showList = open && trimmed.length >= MIN_QUERY

  useEffect(() => {
    const q = query.trim()
    if (q.length < MIN_QUERY) return
    const requestNumber = ++latestRequest.current
    const timer = setTimeout(() => {
      usersApi
        .search(q)
        .then((page) => {
          // Ignore responses that arrive after a newer query was sent.
          if (requestNumber !== latestRequest.current) return
          setResults(page.items.filter((user) => !excludeIds?.has(user.id)))
          setActive(-1)
          setOpen(true)
          setError(null)
        })
        .catch(() => {
          if (requestNumber === latestRequest.current) setError('Search failed. Try again.')
        })
    }, DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query, excludeIds])

  function choose(user: UserPublic) {
    onSelect(user)
    setQuery('')
    setResults([])
    setOpen(false)
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    // Enter in the search box picks a user; it must never submit a surrounding form.
    if (event.key === 'Enter') event.preventDefault()
    if (!showList || results.length === 0) return
    if (event.key === 'ArrowDown') {
      event.preventDefault()
      setActive((i) => (i + 1) % results.length)
    } else if (event.key === 'ArrowUp') {
      event.preventDefault()
      setActive((i) => (i <= 0 ? results.length - 1 : i - 1))
    } else if (event.key === 'Enter' && active >= 0) {
      choose(results[active])
    } else if (event.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div className="user-search">
      <label htmlFor={inputId}>{label}</label>
      <input
        id={inputId}
        role="combobox"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={active >= 0 ? `${listId}-${active}` : undefined}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Type at least 2 characters"
        autoComplete="off"
      />
      {error && <p role="alert">{error}</p>}
      {showList && (
        <ul id={listId} role="listbox">
          {results.length === 0 && <li className="empty">No matching users</li>}
          {results.map((user, index) => (
            <li
              key={user.id}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === active}
              onMouseDown={(e) => {
                e.preventDefault() // keep focus in the input
                choose(user)
              }}
            >
              <strong>{user.display_name}</strong> <span>@{user.username}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
