import { useEffect, useState } from 'react'

type ApiStatus =
  | { state: 'checking' }
  | { state: 'ok' }
  | { state: 'error'; detail: string }

// M00 placeholder: confirms the dev proxy reaches the backend. Replaced from M02 on.
function App() {
  const [status, setStatus] = useState<ApiStatus>({ state: 'checking' })

  useEffect(() => {
    const controller = new AbortController()
    fetch('/health/live', { signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`)
        const body = (await response.json()) as { status?: string }
        setStatus(
          body.status === 'ok'
            ? { state: 'ok' }
            : { state: 'error', detail: 'unexpected response' },
        )
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setStatus({ state: 'error', detail: error instanceof Error ? error.message : 'failed' })
      })
    return () => controller.abort()
  }, [])

  return (
    <main>
      <h1>Chat App</h1>
      <p>
        API:{' '}
        {status.state === 'checking' && 'checking…'}
        {status.state === 'ok' && <strong data-testid="api-status">reachable</strong>}
        {status.state === 'error' && (
          <strong data-testid="api-status">unreachable ({status.detail})</strong>
        )}
      </p>
    </main>
  )
}

export default App
