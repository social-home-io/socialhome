import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { ws, WsManager, connectionState } from './ws'

/** Minimal controllable stand-in for the browser ``WebSocket``. Tests
 *  drive ``open()`` / ``close()`` by hand so the connection-state
 *  machine is exercised against the same callbacks a real socket
 *  fires. */
class FakeSocket {
  static OPEN = 1
  static instances: FakeSocket[] = []
  readyState = 0
  url: string
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  sent: string[] = []
  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }
  open() { this.readyState = 1; this.onopen?.() }
  close() {
    if (this.readyState === 3) return
    this.readyState = 3
    this.onclose?.()
  }
  send(data: string) { this.sent.push(data) }
}

const latest = () => FakeSocket.instances[FakeSocket.instances.length - 1]

describe('ws', () => {
  it('exports a WsManager instance', () => {
    expect(ws).toBeTruthy()
    expect(typeof ws.on).toBe('function')
    expect(typeof ws.send).toBe('function')
  })
})

describe('WsManager connection state', () => {
  let mgr: WsManager

  beforeEach(() => {
    vi.useFakeTimers()
    FakeSocket.instances = []
    vi.stubGlobal('WebSocket', FakeSocket)
    mgr = new WsManager()
  })

  afterEach(() => {
    mgr.disconnect()
    vi.unstubAllGlobals()
    vi.useRealTimers()
    connectionState.value = 'closed'
  })

  it('dials an absolute URL at the document base, not relative to a nested page (ingress prefix safe)', () => {
    // Firefox resolves a relative WebSocket URL against the page URL and
    // ignores ``<base href>``: a cold load of ``/dms/<id>`` dialled
    // ``/dms/api/ws``. The manager must never hand it a relative URL.
    const before = location.pathname
    history.replaceState(null, '', '/dms/conv-1')
    try {
      mgr.connect()
      expect(latest().url).toBe(`ws://${location.host}/api/ws`)
    } finally {
      history.replaceState(null, '', before)
    }
  })

  it('goes connecting on connect() and open on the open event', () => {
    connectionState.value = 'closed'
    mgr.connect()
    expect(connectionState.value).toBe('connecting')
    latest().open()
    expect(connectionState.value).toBe('open')
  })

  it('goes reconnecting when an open socket drops, and schedules a retry', () => {
    mgr.connect()
    latest().open()
    latest().close()
    expect(connectionState.value).toBe('reconnecting')
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(5000)
    expect(FakeSocket.instances).toHaveLength(2)
    // Still reconnecting while the retry socket is in flight.
    expect(connectionState.value).toBe('reconnecting')
    latest().open()
    expect(connectionState.value).toBe('open')
  })

  it('stays reconnecting across repeated failed retries with exponential backoff', () => {
    mgr.connect()
    latest().close()
    expect(connectionState.value).toBe('reconnecting')
    vi.advanceTimersByTime(5000)
    latest().close()
    // Second retry waits 10 s, not 5 s.
    vi.advanceTimersByTime(5000)
    expect(FakeSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(5000)
    expect(FakeSocket.instances).toHaveLength(3)
    expect(connectionState.value).toBe('reconnecting')
  })

  it('retryNow() connects immediately and cancels the pending backoff', () => {
    mgr.connect()
    latest().close()
    mgr.retryNow()
    expect(FakeSocket.instances).toHaveLength(2)
    latest().open()
    expect(connectionState.value).toBe('open')
    // The cancelled backoff timer must not open a third socket.
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('retryNow() is a no-op while the socket is open', () => {
    mgr.connect()
    latest().open()
    mgr.retryNow()
    expect(FakeSocket.instances).toHaveLength(1)
    expect(connectionState.value).toBe('open')
  })

  it('a superseded socket closing late does not flip state or schedule a retry', () => {
    mgr.connect()
    const first = latest()
    mgr.retryNow()
    latest().open()
    first.close()
    expect(connectionState.value).toBe('open')
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances).toHaveLength(2)
  })

  it('disconnect() goes closed and does not reconnect', () => {
    mgr.connect()
    latest().open()
    mgr.disconnect()
    expect(connectionState.value).toBe('closed')
    vi.advanceTimersByTime(60_000)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('dispatches parsed frames to type and wildcard handlers', () => {
    const typed = vi.fn()
    const any = vi.fn()
    mgr.on('post.created', typed)
    mgr.on('*', any)
    mgr.connect()
    latest().open()
    latest().onmessage?.({ data: JSON.stringify({ type: 'post.created', id: 'p1' }) })
    expect(typed).toHaveBeenCalledWith({
      type: 'post.created',
      data: { type: 'post.created', id: 'p1' },
    })
    expect(any).toHaveBeenCalledTimes(1)
  })
})

describe('WsManager under the HA ingress prefix (/api/hassio_ingress/<token>/)', () => {
  let baseEl: HTMLBaseElement
  const before = location.pathname
  beforeEach(() => {
    baseEl = document.createElement('base')
    baseEl.href = '/api/hassio_ingress/tok/'
    document.head.prepend(baseEl)
    // A cold load of a nested in-app route behind ingress.
    history.replaceState(null, '', '/api/hassio_ingress/tok/dms/conv-1')
    FakeSocket.instances = []
    vi.stubGlobal('WebSocket', FakeSocket)
    vi.resetModules()
  })
  afterEach(() => {
    baseEl.remove()
    history.replaceState(null, '', before)
    vi.unstubAllGlobals()
    vi.resetModules()
  })

  it('dials the socket through the ingress prefix, carrying the bearer token when one is stashed', async () => {
    const auth = await import('@/store/auth')
    const mod = await import('./ws')
    const mgr = new mod.WsManager()
    mgr.connect()
    expect(latest().url).toBe(`ws://${location.host}/api/hassio_ingress/tok/api/ws`)
    mgr.disconnect()
    auth.token.value = 'a/b c'
    try {
      mgr.connect()
      expect(latest().url).toBe(
        `ws://${location.host}/api/hassio_ingress/tok/api/ws?token=a%2Fb%20c`,
      )
    } finally {
      mgr.disconnect()
      auth.token.value = null
    }
  })
})
