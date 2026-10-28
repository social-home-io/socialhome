/**
 * Tests for the host↔iframe postMessage bridge (bridge.ts).
 *
 * Security invariants exercised:
 * - Events from the wrong source are silently dropped (no origin check).
 * - The bearer token NEVER appears in any reply to the iframe.
 * - WS relay forwards only frames whose app_id matches the mounted context.
 */

import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'

// ── mock @/ws ──────────────────────────────────────────────────────────────
// Capture every ws.on registration so tests can drive synthetic frames.
type WsHandler = (evt: { data: Record<string, unknown> }) => void
const wsHandlers: Record<string, WsHandler> = {}
// eslint-disable-next-line @typescript-eslint/no-explicit-any
const wsOffFns: Record<string, MockInstance> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, handler: WsHandler) => {
      wsHandlers[type] = handler
      const off = vi.fn(() => { delete wsHandlers[type] })
      wsOffFns[type] = off
      return off
    },
  },
}))

// ── mock @/api ─────────────────────────────────────────────────────────────
const mockApiGet = vi.fn()
const mockApiPost = vi.fn()
const mockApiPut = vi.fn()
const mockApiDelete = vi.fn()

vi.mock('@/api', () => {
  class ApiError extends Error {
    constructor(
      public readonly status: number,
      public readonly path: string,
    ) {
      super(`API ${status}: ${path}`)
      this.name = 'ApiError'
    }
  }

  return {
    ApiError,
    api: {
      get: (...args: unknown[]) => mockApiGet(...args),
      post: (...args: unknown[]) => mockApiPost(...args),
      put: (...args: unknown[]) => mockApiPut(...args),
      delete: (...args: unknown[]) => mockApiDelete(...args),
    },
  }
})

import { mountBridge } from './bridge'
// Import the mocked ApiError so tests can construct it.
import { ApiError } from '@/api'

// ── helpers ────────────────────────────────────────────────────────────────

const APP_ID = 'app-chess'
const USER_ID = 'user-42'

function makeFakeIframe() {
  return {
    contentWindow: { postMessage: vi.fn() },
  } as unknown as HTMLIFrameElement
}

/** Capture the ``message`` handler registered by mountBridge. */
function captureMessageHandler(): (event: Partial<MessageEvent>) => Promise<void> {
  // window.addEventListener is spied on in beforeEach; read mock.calls
  // to find the 'message' entry that mountBridge registered.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const spy = window.addEventListener as unknown as { mock: { calls: any[][] } }
  const calls = spy.mock?.calls ?? []
  const last = [...calls].reverse().find(([type]) => type === 'message')
  const raw: (event: Partial<MessageEvent>) => void =
    last ? (last[1] as (event: Partial<MessageEvent>) => void) : () => {}
  // Wrap the synchronous listener so the test can await the async
  // work that ``onMessage`` fires-and-forgets via ``handleRpc``.
  return async (event: Partial<MessageEvent>) => {
    raw(event)
    // Flush all pending microtasks so handleRpc's awaits resolve.
    await new Promise<void>(resolve => setTimeout(resolve, 0))
  }
}

// ── test suite ─────────────────────────────────────────────────────────────

describe('mountBridge', () => {
  let iframe: HTMLIFrameElement
  let cleanup: () => void
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let addEventSpy: MockInstance
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  let removeEventSpy: MockInstance

  beforeEach(() => {
    mockApiGet.mockReset()
    mockApiPost.mockReset()
    mockApiPut.mockReset()
    mockApiDelete.mockReset()
    Object.keys(wsHandlers).forEach(k => delete wsHandlers[k])
    Object.keys(wsOffFns).forEach(k => delete wsOffFns[k])

    iframe = makeFakeIframe()

    addEventSpy = vi.spyOn(window, 'addEventListener')
    removeEventSpy = vi.spyOn(window, 'removeEventListener')

    cleanup = mountBridge(iframe, { appId: APP_ID, selfUserId: USER_ID })
  })

  afterEach(() => {
    // Vitest 4 no longer auto-restores spies between tests; without this the
    // window add/removeEventListener spies accumulate calls across tests and
    // ``mock.calls.find('message')`` returns a *previous* test's handler.
    addEventSpy.mockRestore()
    removeEventSpy.mockRestore()
  })

  // ── wrong source ─────────────────────────────────────────────────────────

  it('ignores events whose source is not iframe.contentWindow', async () => {
    const otherWindow = { postMessage: vi.fn() } as unknown as Window
    const handler = captureMessageHandler()

    await handler({ source: otherWindow, data: { id: 1, method: 'store.list' } })

    expect(mockApiGet).not.toHaveBeenCalled()
    expect(iframe.contentWindow!.postMessage).not.toHaveBeenCalled()
  })

  // ── store.set ────────────────────────────────────────────────────────────

  it('store.set: calls api.put with correct path and body; replies ok:true', async () => {
    mockApiPut.mockResolvedValueOnce(undefined)
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 7, method: 'store.set', params: { key: 'score', value: 99 } },
    })

    expect(mockApiPut).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/store/score`,
      { value: 99 },
    )
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 7, ok: true }),
      '*',
    )
  })

  it('store.set: encodes special characters in the key', async () => {
    mockApiPut.mockResolvedValueOnce(undefined)
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 8, method: 'store.set', params: { key: 'a/b c', value: 1 } },
    })

    expect(mockApiPut).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/store/${encodeURIComponent('a/b c')}`,
      { value: 1 },
    )
  })

  // ── store.get ────────────────────────────────────────────────────────────

  it('store.get: returns the value field from the API response', async () => {
    mockApiGet.mockResolvedValueOnce({ key: 'score', value: 42 })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 10, method: 'store.get', params: { key: 'score' } },
    })

    expect(mockApiGet).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/store/${encodeURIComponent('score')}`,
    )
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 10, ok: true, result: 42 }),
      '*',
    )
  })

  it('store.get: returns null when the key is missing (404)', async () => {
    mockApiGet.mockRejectedValueOnce(new ApiError(404, `/api/apps/${APP_ID}/store/missing`))
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 11, method: 'store.get', params: { key: 'missing' } },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 11, ok: true, result: null }),
      '*',
    )
  })

  it('store.get: replies ok:false for non-404 API errors', async () => {
    mockApiGet.mockRejectedValueOnce(new ApiError(500, `/api/apps/${APP_ID}/store/x`))
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 12, method: 'store.get', params: { key: 'x' } },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 12, ok: false }),
      '*',
    )
  })

  // ── app.context ──────────────────────────────────────────────────────────

  it('app.context: returns appId and selfUserId — no token field', async () => {
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 20, method: 'app.context' },
    })

    const call = (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls[0]
    const reply = call[0] as { id: number; ok: boolean; result: Record<string, unknown> }

    expect(reply.ok).toBe(true)
    expect(reply.result.appId).toBe(APP_ID)
    expect(reply.result.selfUserId).toBe(USER_ID)
    // Security: the bearer token must never be forwarded to the app iframe.
    expect(reply.result).not.toHaveProperty('token')
  })

  // ── unknown method ────────────────────────────────────────────────────────

  it('unknown method: replies ok:false', async () => {
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 30, method: 'does.not.exist' },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 30, ok: false }),
      '*',
    )
    const call = (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(call[0].error.code).toBe('unknown_method')
  })

  // ── app.send ──────────────────────────────────────────────────────────────

  it('app.send: POSTs to /messages with session_id, target, payload; replies ok:true', async () => {
    mockApiPost.mockResolvedValueOnce({ ok: true })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: {
        id: 40,
        method: 'app.send',
        params: {
          session_id: 'sess-abc',
          target: 'user-alice',
          payload: { move: 'e4' },
        },
      },
    })

    expect(mockApiPost).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/messages`,
      {
        session_id: 'sess-abc',
        target: 'user-alice',
        payload: { move: 'e4' },
      },
    )
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 40, ok: true }),
      '*',
    )
  })

  // ── peers.list ──────────────────────────────────────────────────────────

  it('peers.list: GETs /peers and returns peers array', async () => {
    const peers = [
      { instance_id: 'peer.example.com', display_name: 'Example Peer' },
    ]
    mockApiGet.mockResolvedValueOnce({ peers })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 41, method: 'peers.list' },
    })

    expect(mockApiGet).toHaveBeenCalledWith(`/api/apps/${APP_ID}/peers`)
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 41, ok: true, result: peers }),
      '*',
    )
  })

  // ── contacts.list ───────────────────────────────────────────────────────

  it('contacts.list: GETs /contacts and returns contacts array', async () => {
    const contacts = [
      { user_id: 'user-alice', display_name: 'Alice' },
      { user_id: 'user-bob', display_name: 'Bob' },
    ]
    mockApiGet.mockResolvedValueOnce({ contacts })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 43, method: 'contacts.list' },
    })

    expect(mockApiGet).toHaveBeenCalledWith(`/api/apps/${APP_ID}/contacts`)
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 43, ok: true, result: contacts }),
      '*',
    )
  })

  // ── app.pendingSessions ─────────────────────────────────────────────────

  it('app.pendingSessions: GETs /pending-sessions and returns sessions array', async () => {
    const sessions = [
      { session_id: 's1', from_instance: 'i1', from_user: 'u', payload: {} },
    ]
    mockApiGet.mockResolvedValueOnce({ sessions })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 44, method: 'app.pendingSessions' },
    })

    expect(mockApiGet).toHaveBeenCalledWith(`/api/apps/${APP_ID}/pending-sessions`)
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 44, ok: true, result: sessions }),
      '*',
    )
  })

  // ── app.openSession ────────────────────────────────────────────────────

  it('app.openSession: POSTs to /sessions with target; returns session_id', async () => {
    mockApiPost.mockResolvedValueOnce({ session_id: 'sess-xyz' })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: {
        id: 42,
        method: 'app.openSession',
        params: { target: 'user-bob' },
      },
    })

    expect(mockApiPost).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/sessions`,
      { target: 'user-bob' },
    )
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 42, ok: true, result: 'sess-xyz' }),
      '*',
    )
  })

  // ── WS relay ──────────────────────────────────────────────────────────────

  it('WS relay: forwards app.message frames matching the appId into the iframe with full identity', () => {
    expect(wsHandlers['app.message']).toBeDefined()

    wsHandlers['app.message']({
      data: {
        app_id: APP_ID,
        session_id: 'sess-abc',
        from_instance: 'peer.example.com',
        kind: 'message',
        payload: { move: 'e4' },
      },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      {
        type: 'app:event',
        kind: 'message',
        sessionId: 'sess-abc',
        fromInstance: 'peer.example.com',
        fromUser: undefined,
        payload: { move: 'e4' },
      },
      '*',
    )
  })

  it('WS relay: forwards session kind frames with kind="session" and fromUser when present', () => {
    wsHandlers['app.message']({
      data: {
        app_id: APP_ID,
        session_id: 'sess-invite',
        from_instance: 'peer.example.com',
        from_user: 'user-alice',
        kind: 'session',
        payload: { verb: 'open' },
      },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      {
        type: 'app:event',
        kind: 'session',
        sessionId: 'sess-invite',
        fromInstance: 'peer.example.com',
        fromUser: 'user-alice',
        payload: { verb: 'open' },
      },
      '*',
    )
  })

  it('WS relay: an invite reaching the open app is taken in (its Needs-you item goes) and the rest handed over once', async () => {
    mockApiGet.mockResolvedValueOnce({
      sessions: [
        { session_id: 'sess-live', from_instance: 'peer', from_user: 'alice', payload: { verb: 'open' } },
        { session_id: 'sess-older', from_instance: 'peer', from_user: 'bob', payload: { verb: 'open' } },
      ],
    })
    wsHandlers['app.message']({
      data: {
        app_id: APP_ID,
        session_id: 'sess-live',
        from_instance: 'peer',
        from_user: 'alice',
        kind: 'session',
        payload: { verb: 'open' },
      },
    })
    await new Promise<void>(resolve => setTimeout(resolve, 0))
    expect(mockApiGet).toHaveBeenCalledWith(`/api/apps/${APP_ID}/pending-sessions`)
    const sent = (iframe.contentWindow!.postMessage as unknown as MockInstance).mock.calls
      .map(([m]) => (m as { sessionId?: string }).sessionId)
    // The live one once (from the frame), the older one from the drain.
    expect(sent).toEqual(['sess-live', 'sess-older'])
  })

  it('WS relay: a move or a close takes nothing in', () => {
    wsHandlers['app.message']({
      data: { app_id: APP_ID, session_id: 's', from_instance: 'p', kind: 'session', payload: { verb: 'close' } },
    })
    wsHandlers['app.message']({
      data: { app_id: APP_ID, session_id: 's', from_instance: 'p', kind: 'message', payload: { verb: 'open' } },
    })
    expect(mockApiGet).not.toHaveBeenCalled()
  })

  it('app.cancelSession: POSTs /sessions/{id}/cancel with the target', async () => {
    mockApiPost.mockResolvedValueOnce({ ok: true })
    const handler = captureMessageHandler()
    const target = { instance_id: 'i', user_ref: 'bob', is_local: false }
    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 7, method: 'app.cancelSession', params: { sessionId: 's/1', target } },
    })
    expect(mockApiPost).toHaveBeenCalledWith(`/api/apps/${APP_ID}/sessions/s%2F1/cancel`, { target })
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 7, ok: true }),
      '*',
    )
  })

  it('WS relay: does NOT forward frames for a different app_id', () => {
    wsHandlers['app.message']({
      data: {
        app_id: 'other-app',
        session_id: 'sess-x',
        from_instance: 'peer.example.com',
        kind: 'message',
        payload: { move: 'd5' },
      },
    })

    expect(iframe.contentWindow!.postMessage).not.toHaveBeenCalled()
  })

  // ── cleanup ──────────────────────────────────────────────────────────────

  it('cleanup: removes the message listener from window', () => {
    // Find the handler that was registered in beforeEach
    const addCalls = addEventSpy.mock.calls
    const messageHandler = addCalls.find((c: unknown[]) => c[0] === 'message')?.[1]
    expect(messageHandler).toBeDefined()

    cleanup()

    expect(removeEventSpy).toHaveBeenCalledWith('message', messageHandler)
  })

  it('cleanup: calls the ws unsubscribe function', () => {
    const offFn = wsOffFns['app.message']
    expect(offFn).toBeDefined()

    cleanup()

    expect(offFn).toHaveBeenCalled()
  })

  // ── store.list ────────────────────────────────────────────────────────────

  it('store.list: calls api.get on /store and returns items', async () => {
    mockApiGet.mockResolvedValueOnce({ items: ['a', 'b'] })
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 50, method: 'store.list' },
    })

    expect(mockApiGet).toHaveBeenCalledWith(`/api/apps/${APP_ID}/store`)
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 50, ok: true, result: ['a', 'b'] }),
      '*',
    )
  })

  // ── store.delete ──────────────────────────────────────────────────────────

  it('store.delete: calls api.delete with correct path and replies ok:true', async () => {
    mockApiDelete.mockResolvedValueOnce(undefined)
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 60, method: 'store.delete', params: { key: 'score' } },
    })

    expect(mockApiDelete).toHaveBeenCalledWith(
      `/api/apps/${APP_ID}/store/${encodeURIComponent('score')}`,
    )
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 60, ok: true }),
      '*',
    )
  })
})
