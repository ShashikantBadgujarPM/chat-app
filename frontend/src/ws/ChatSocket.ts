// The WebSocket client (docs/design/08 §14.2, §14.6-14.7).
//
// Get a ticket over REST -> connect -> hello -> live events. Heartbeat pings every
// `heartbeat_interval_s`; a missing pong within 10 s means the connection is dead.
// Reconnects with full-jitter backoff, following the close-code policy.

import { ApiError, api, refreshAccessToken } from '../api/client'
import { EventDeduper, STABLE_AFTER_MS, backoffDelay, closeAction } from './policy'

export type Envelope<T = Record<string, unknown>> = {
  v: number
  id: number | null
  type: string
  conversation_id: string | null
  occurred_at: string
  correlation_id: string | null
  payload: T
  ref?: string
}

export type SocketStatus = 'connecting' | 'online' | 'offline'
type Listener = (event: Envelope) => void
type StatusListener = (status: SocketStatus) => void

const PONG_TIMEOUT_MS = 10_000

function socketUrl(ticket: string): string {
  const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws'
  return `${scheme}://${window.location.host}/ws?ticket=${encodeURIComponent(ticket)}`
}

export class ChatSocket {
  private ws: WebSocket | null = null
  private attempt = 0
  private stopped = true
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null
  private pongTimer: ReturnType<typeof setTimeout> | null = null
  private stableTimer: ReturnType<typeof setTimeout> | null = null
  private readonly listeners = new Set<Listener>()
  private readonly statusListeners = new Set<StatusListener>()
  private readonly deduper = new EventDeduper()
  /** The highest durable event id applied; M09 resumes from it. */
  lastEventId: number | null = null
  status: SocketStatus = 'offline'

  onSessionEnded: () => void = () => {}

  subscribe(listener: Listener): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  onStatus(listener: StatusListener): () => void {
    this.statusListeners.add(listener)
    return () => this.statusListeners.delete(listener)
  }

  start(): void {
    if (!this.stopped) return
    this.stopped = false
    void this.connect()
  }

  stop(): void {
    this.stopped = true
    this.clearTimers()
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer)
    this.ws?.close(1000, 'client_stop')
    this.ws = null
    this.setStatus('offline')
  }

  private setStatus(status: SocketStatus) {
    this.status = status
    this.statusListeners.forEach((listener) => listener(status))
  }

  private async connect(): Promise<void> {
    if (this.stopped) return
    this.setStatus('connecting')
    let ticket: string
    try {
      ticket = (await api<{ ticket: string }>('/api/v1/auth/ws-ticket', { method: 'POST' })).ticket
    } catch (error) {
      // 401 after the client's own refresh attempt: the session is over.
      if (error instanceof ApiError && error.status === 401) {
        this.stop()
        this.onSessionEnded()
        return
      }
      this.scheduleReconnect('backoff')
      return
    }
    if (this.stopped) return

    const ws = new WebSocket(socketUrl(ticket))
    this.ws = ws
    ws.onmessage = (message) => this.handleFrame(message.data)
    ws.onclose = (event) => {
      if (this.ws !== ws) return
      this.ws = null
      this.clearTimers()
      this.setStatus('offline')
      if (!this.stopped) void this.handleClose(event.code, event.reason)
    }
  }

  private handleFrame(raw: unknown) {
    if (typeof raw !== 'string') return
    let frame: Envelope
    try {
      frame = JSON.parse(raw) as Envelope
    } catch {
      return
    }
    if (frame.type === 'hello') {
      this.setStatus('online')
      const interval = Number((frame.payload as { heartbeat_interval_s?: number }).heartbeat_interval_s ?? 25)
      this.startHeartbeat(interval * 1000)
      this.stableTimer = setTimeout(() => {
        this.attempt = 0
      }, STABLE_AFTER_MS)
    } else if (frame.type === 'pong') {
      if (this.pongTimer) clearTimeout(this.pongTimer)
      this.pongTimer = null
      return
    }
    if (!this.deduper.accept(frame.id)) return
    if (frame.id !== null) this.lastEventId = Math.max(this.lastEventId ?? 0, frame.id)
    this.listeners.forEach((listener) => listener(frame))
  }

  private startHeartbeat(intervalMs: number) {
    this.heartbeatTimer = setInterval(() => {
      if (this.ws?.readyState !== WebSocket.OPEN) return
      this.ws.send(JSON.stringify({ type: 'ping' }))
      this.pongTimer ??= setTimeout(() => {
        // No pong: treat the connection as dead and reconnect.
        this.ws?.close(4000, 'pong_timeout')
      }, PONG_TIMEOUT_MS)
    }, intervalMs)
  }

  private clearTimers() {
    if (this.heartbeatTimer) clearInterval(this.heartbeatTimer)
    if (this.pongTimer) clearTimeout(this.pongTimer)
    if (this.stableTimer) clearTimeout(this.stableTimer)
    this.heartbeatTimer = this.pongTimer = this.stableTimer = null
  }

  private async handleClose(code: number, reason: string) {
    const action = closeAction(code, reason)
    switch (action.kind) {
      case 'stop':
        return
      case 'login':
        this.stop()
        this.onSessionEnded()
        return
      case 'reauthenticate': {
        const token = await refreshAccessToken()
        if (!token) {
          this.stop()
          this.onSessionEnded()
          return
        }
        this.scheduleReconnect('now')
        return
      }
      case 'reconnect':
        this.scheduleReconnect(action.delay)
    }
  }

  private scheduleReconnect(delay: 'backoff' | 'now' | number) {
    if (this.stopped) return
    const ms = delay === 'now' ? 0 : delay === 'backoff' ? backoffDelay(this.attempt++) : delay
    this.reconnectTimer = setTimeout(() => void this.connect(), ms)
  }
}

/** One socket per tab, started while signed in. */
export const chatSocket = new ChatSocket()
