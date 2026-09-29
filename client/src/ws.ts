import { signal } from '@preact/signals'
import { token } from '@/store/auth'

export interface WsEvent {
  type: string
  data: Record<string, unknown>
}

type WsHandler = (event: WsEvent) => void

/**
 * Live state of the realtime socket, driven only by real socket events.
 *
 * * ``closed`` — never connected, or ``disconnect()`` was called.
 * * ``connecting`` — the very first socket is in flight.
 * * ``open`` — the socket fired ``open``.
 * * ``reconnecting`` — a socket closed on us; a retry is scheduled or
 *   in flight. Stays here across failed retries until one opens.
 *
 * ``OfflineIndicator`` reads it to tell "the server is unreachable"
 * apart from "the browser is offline".
 */
export type ConnectionState = 'connecting' | 'open' | 'reconnecting' | 'closed'

export const connectionState = signal<ConnectionState>('closed')

const INITIAL_RETRY_MS = 5000
const MAX_RETRY_MS = 60_000

export class WsManager {
  private ws: WebSocket | null = null
  private handlers = new Map<string, Set<WsHandler>>()
  private retryDelay = INITIAL_RETRY_MS
  private retryTimer: ReturnType<typeof setTimeout> | null = null

  connect() {
    this.clearRetry()
    // Relative URL — modern browsers (Chrome 116+, Firefox 124+,
    // Safari 17+) resolve it against ``document.baseURI`` and
    // auto-rewrite ``http`` → ``ws`` / ``https`` → ``wss``. Under HA
    // Supervisor ingress the document base is
    // ``/api/hassio_ingress/<token>/``, so the socket reaches the
    // add-on through the same prefix without any string surgery here.
    const path = token.value
      ? `api/ws?token=${encodeURIComponent(token.value)}`
      : 'api/ws'
    if (connectionState.value !== 'reconnecting') {
      connectionState.value = 'connecting'
    }
    const sock = new WebSocket(path)
    this.ws = sock

    // Every handler checks ``this.ws === sock`` so a socket we already
    // replaced (``retryNow``) or dropped (``disconnect``) can't flip
    // the state or schedule a second retry loop when it closes late.
    sock.onopen = () => {
      if (this.ws !== sock) return
      this.retryDelay = INITIAL_RETRY_MS
      connectionState.value = 'open'
    }

    sock.onmessage = (e) => {
      // Server broadcasts arrive as a flat object ``{type, ...body}``
      // (see ``RealtimeService._broadcast_*``). Handlers read
      // ``evt.data.x`` so we repackage the parsed frame into
      // ``{type, data: raw}`` here — keeping the whole payload
      // accessible under ``data`` while still giving handlers a
      // stable ``type`` field on the outer envelope.
      const raw = JSON.parse(e.data) as Record<string, unknown>
      const type = String(raw.type ?? '')
      const evt: WsEvent = { type, data: raw }
      this.handlers.get(type)?.forEach(h => h(evt))
      this.handlers.get('*')?.forEach(h => h(evt))
    }

    sock.onclose = () => {
      if (this.ws !== sock) return
      connectionState.value = 'reconnecting'
      this.retryTimer = setTimeout(() => this.connect(), this.retryDelay)
      this.retryDelay = Math.min(this.retryDelay * 2, MAX_RETRY_MS)
    }
  }

  /** Skip the backoff wait and reconnect right away (the banner's
   *  "Retry now"). No-op while the socket is open or never started. */
  retryNow() {
    if (connectionState.value === 'open' || connectionState.value === 'closed') return
    this.retryDelay = INITIAL_RETRY_MS
    const stale = this.ws
    this.ws = null
    stale?.close()
    this.connect()
  }

  on(type: string, handler: WsHandler) {
    if (!this.handlers.has(type)) this.handlers.set(type, new Set())
    this.handlers.get(type)!.add(handler)
    return () => { this.handlers.get(type)?.delete(handler) }
  }

  send(type: string, data: Record<string, unknown> = {}) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type, data }))
    }
  }

  disconnect() {
    this.clearRetry()
    const sock = this.ws
    this.ws = null
    sock?.close()
    connectionState.value = 'closed'
  }

  private clearRetry() {
    if (this.retryTimer !== null) {
      clearTimeout(this.retryTimer)
      this.retryTimer = null
    }
  }
}

export const ws = new WsManager()
