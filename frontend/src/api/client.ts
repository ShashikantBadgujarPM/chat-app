// REST client (docs/design/06 §11.2-11.3, ADR-007).
//
// - The access token lives only in this module's memory: never localStorage or
//   sessionStorage, so XSS can't read a stored credential.
// - The refresh token is an httpOnly cookie the browser sends to /api/v1/auth only.
// - On 401 the client refreshes once and retries. Refresh is single-flight: one per
//   tab (a shared promise) and one across tabs (navigator.locks). The winner shares the
//   new access token with other tabs over BroadcastChannel.
// - 409 refresh_superseded means another tab rotated the cookie a moment ago: retry once.

export type ApiErrorBody = {
  code: string
  message: string
  details: unknown
  request_id: string | null
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details: unknown
  readonly requestId: string | null

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.status = status
    this.code = body.code
    this.details = body.details
    this.requestId = body.request_id
  }
}

type TokenResponse = { access_token: string; token_type: 'bearer'; expires_in: number }

const CSRF_HEADERS = { 'X-Requested-With': 'chat-app' }
const channel = typeof BroadcastChannel !== 'undefined' ? new BroadcastChannel('chat-auth') : null

let accessToken: string | null = null
let refreshInFlight: Promise<string | null> | null = null
const listeners = new Set<(token: string | null) => void>()

export function getAccessToken(): string | null {
  return accessToken
}

export function setAccessToken(token: string | null, { broadcast = true } = {}): void {
  accessToken = token
  listeners.forEach((listener) => listener(token))
  if (broadcast) channel?.postMessage({ type: token ? 'token' : 'signed-out', token })
}

/** Called when the session ends (refresh failed) so the UI can go to the login page. */
export function onAccessTokenChange(listener: (token: string | null) => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

channel?.addEventListener('message', (event: MessageEvent<{ type: string; token?: string }>) => {
  if (event.data.type === 'token' && event.data.token) setAccessToken(event.data.token, { broadcast: false })
  if (event.data.type === 'signed-out') setAccessToken(null, { broadcast: false })
})

async function parseError(response: Response): Promise<ApiError> {
  try {
    const body = (await response.json()) as { error?: ApiErrorBody }
    if (body.error) return new ApiError(response.status, body.error)
  } catch {
    // not JSON; fall through
  }
  return new ApiError(response.status, {
    code: 'http_error',
    message: `Request failed (${response.status})`,
    details: null,
    request_id: response.headers.get('X-Request-ID'),
  })
}

async function postRefresh(): Promise<Response> {
  return fetch('/api/v1/auth/refresh', {
    method: 'POST',
    headers: CSRF_HEADERS,
    credentials: 'same-origin',
  })
}

async function doRefresh(): Promise<string | null> {
  const tokenBefore = accessToken
  let response = await postRefresh()
  if (response.status === 409) {
    // Another tab rotated the cookie; the browser now holds the successor.
    response = await postRefresh()
  }
  if (!response.ok) {
    // Another tab may have refreshed and broadcast a token while we waited.
    if (accessToken && accessToken !== tokenBefore) return accessToken
    setAccessToken(null)
    return null
  }
  const body = (await response.json()) as TokenResponse
  setAccessToken(body.access_token)
  return body.access_token
}

/** Refresh the access token. Concurrent callers share one request. */
export function refreshAccessToken(): Promise<string | null> {
  if (!refreshInFlight) {
    const run = () => doRefresh()
    const locked =
      typeof navigator !== 'undefined' && navigator.locks
        ? navigator.locks.request('chat-auth-refresh', run)
        : run()
    refreshInFlight = locked.finally(() => {
      refreshInFlight = null
    })
  }
  return refreshInFlight
}

type RequestOptions = { method?: string; body?: unknown; auth?: boolean; headers?: Record<string, string> }

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, auth = true, headers = {} } = options

  const send = (token: string | null) =>
    fetch(path, {
      method,
      credentials: 'same-origin',
      headers: {
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
        ...(auth && token ? { Authorization: `Bearer ${token}` } : {}),
        ...headers,
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })

  let response = await send(accessToken)
  if (response.status === 401 && auth) {
    const token = await refreshAccessToken()
    if (token) response = await send(token)
  }
  if (!response.ok) throw await parseError(response)
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export const authApi = {
  csrfHeaders: CSRF_HEADERS,
}
