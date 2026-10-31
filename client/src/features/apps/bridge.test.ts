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

import { _resetAcceptedForTests, _resetPickForTests, mountBridge, pickRequest, PICK_COOLDOWN_MS, type PickedPerson, type PickRequest } from './bridge'
// Import the mocked ApiError so tests can construct it.
import { ApiError } from '@/api'

// ── helpers ────────────────────────────────────────────────────────────────

const APP_ID = 'app-chess'
const USER_ID = 'user-42'
/** Chess: storage + federation, people implied. */
const ALL = ['storage', 'people', 'federation']

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

/** The invite accepts the bridge made (``POST …/sessions/<sid>/accept``). */
function acceptCalls(): unknown[][] {
  return mockApiPost.mock.calls.filter(([url]) => String(url).endsWith('/accept'))
}

/** The mailbox takes the bridge made (``POST …/held``). */
function heldCalls(): unknown[][] {
  return mockApiPost.mock.calls.filter(([url]) => /\/held(\?|$)/.test(String(url)))
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
    _resetAcceptedForTests()
    _resetPickForTests()

    addEventSpy = vi.spyOn(window, 'addEventListener')
    removeEventSpy = vi.spyOn(window, 'removeEventListener')

    cleanup = mountBridge(iframe, { appId: APP_ID, selfUserId: USER_ID, permissions: ALL })
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

  // ── ready ────────────────────────────────────────────────────────────────

  it('onReady fires once, on the app\'s first request — app.ready or any RPC', async () => {
    cleanup()
    const onReady = vi.fn()
    cleanup = mountBridge(iframe, { appId: APP_ID, selfUserId: USER_ID, permissions: ALL, onReady })
    const handler = captureMessageHandler()

    // A stranger's message, or junk from the frame, is not "the app started".
    await handler({ source: { postMessage: vi.fn() } as unknown as Window, data: { id: 1, method: 'app.ready' } })
    await handler({ source: iframe.contentWindow!, data: 'not found' })
    expect(onReady).not.toHaveBeenCalled()

    await handler({ source: iframe.contentWindow!, data: { id: 2, method: 'app.context' } })
    await handler({ source: iframe.contentWindow!, data: { id: 3, method: 'app.ready' } })
    expect(onReady).toHaveBeenCalledOnce()
  })

  it('app.ready is answered ok (an SDK that announces it started)', async () => {
    const handler = captureMessageHandler()
    await handler({ source: iframe.contentWindow!, data: { id: 9, method: 'app.ready' } })
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      { id: 9, ok: true, result: { ok: true } },
      '*',
    )
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

  it('app.context: opened without ?session= the launch is empty and nothing is accepted', async () => {
    const handler = captureMessageHandler()
    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 21, method: 'app.context' },
    })
    const reply = (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls[0][0]
    // A ``launch`` key at all: this host opens games from Play (D12).
    expect(reply.result.launch).toEqual({})
    expect(acceptCalls()).toEqual([])
    expect(mockApiGet).not.toHaveBeenCalled()
  })

  // ── look: language, theme, tokens (D14) ──────────────────────────────────

  describe('the app’s look (D14)', () => {
    const LOOK = {
      locale: 'de',
      dir: 'ltr' as const,
      theme: 'dark' as const,
      reducedMotion: true,
      tokens: {
        bg: '#1a1612', surface: '#2f261e', text: '#f1e9da', muted: '#9a8e7d',
        accent: '#e96a3f', onAccent: '#1a1612', border: '#3d332a', focus: '#f1d9ca',
      },
    }
    let push: ((c: typeof LOOK) => void) | null
    let stopWatching: ReturnType<typeof vi.fn<() => void>>

    beforeEach(() => {
      cleanup()
      push = null
      stopWatching = vi.fn<() => void>()
      cleanup = mountBridge(iframe, {
        appId: APP_ID, selfUserId: USER_ID, permissions: [],
        hostContext: () => LOOK,
        watchHostContext: (cb) => { push = cb; return stopWatching },
      })
    })

    const posted = () => (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls.map(c => c[0])

    it('app.context carries locale, dir, theme, reducedMotion and the eight tokens — needs no permission', async () => {
      await captureMessageHandler()({ source: iframe.contentWindow as unknown as Window, data: { id: 30, method: 'app.context' } })
      const reply = posted()[0]
      expect(reply.ok).toBe(true)
      expect(reply.result).toMatchObject(LOOK)
      expect(reply.result.appId).toBe(APP_ID)
      expect(reply.result).not.toHaveProperty('token')
    })

    it('pushes app.context-changed once the app has spoken, never before', async () => {
      push!({ ...LOOK, theme: 'light' as never })
      expect(posted()).toEqual([])
      await captureMessageHandler()({ source: iframe.contentWindow as unknown as Window, data: { id: 31, method: 'app.ready' } })
      push!({ ...LOOK, locale: 'nl' })
      expect(posted().at(-1)).toEqual({ type: 'app.context-changed', context: { ...LOOK, locale: 'nl' } })
    })

    it('stops watching when the frame goes', () => {
      cleanup()
      expect(stopWatching).toHaveBeenCalledOnce()
      cleanup = () => {}
    })
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

  it('app.send with turn: true says the move hands over the turn; anything else is left out', async () => {
    const handler = captureMessageHandler()
    const send = async (id: number, turn: unknown) => {
      mockApiPost.mockResolvedValueOnce({ ok: true })
      await handler({
        source: iframe.contentWindow as unknown as Window,
        data: {
          id,
          method: 'app.send',
          params: { session_id: 's1', target: 'user-alice', payload: { shot: 'B4' }, turn },
        },
      })
    }
    await send(41, true)
    await send(42, 'yes')
    await send(43, undefined)
    const bodies = mockApiPost.mock.calls
      .filter(c => c[0] === `/api/apps/${APP_ID}/messages`)
      .map(c => c[1] as Record<string, unknown>)
    expect(bodies[0]).toEqual({ session_id: 's1', target: 'user-alice', payload: { shot: 'B4' }, turn: true })
    expect('turn' in bodies[1]).toBe(false)
    expect('turn' in bodies[2]).toBe(false)
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

  // ── launch context: Play opens the game you were invited to ───────────

  describe('opened from Play (?session=)', () => {
    const invite = { session_id: 's1', from_instance: 'peer', from_user: 'alice', payload: { verb: 'open' } }
    const asApp = { sessionId: 's1', fromInstance: 'peer', fromUser: 'alice', payload: { verb: 'open' } }
    let handler: (event: Partial<MessageEvent>) => Promise<void>

    beforeEach(() => {
      cleanup()
      addEventSpy.mockClear()
      cleanup = mountBridge(iframe, { appId: APP_ID, selfUserId: USER_ID, permissions: ALL, launchSession: 's1' })
      handler = captureMessageHandler()
    })

    const replies = () => (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls
      .map(([m]) => m as { id: number; ok: boolean; result: Record<string, unknown> })

    it('accepts only that invite and hands it over as app.context launch', async () => {
      mockApiPost.mockResolvedValueOnce({ session: invite })
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context' } })
      expect(acceptCalls()).toHaveLength(1)
      expect(mockApiPost).toHaveBeenCalledWith(`/api/apps/${APP_ID}/sessions/s1/accept`)
      expect(replies()[0].result.launch).toEqual({ sessionId: 's1', invite: asApp })
      // Never the blanket drain: only the one look whether it is a table.
      expect(mockApiGet.mock.calls.map(c => c[0])).toEqual([`/api/apps/${APP_ID}/groups/s1`])
    })

    it('accepts once, for context and pendingSessions alike, and across frames (Try again)', async () => {
      mockApiPost.mockResolvedValueOnce({ session: invite })
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context' } })
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 2, method: 'app.pendingSessions' } })
      cleanup()
      cleanup = mountBridge(iframe, { appId: APP_ID, selfUserId: USER_ID, permissions: ALL, launchSession: 's1' })
      await captureMessageHandler()({ source: iframe.contentWindow as unknown as Window, data: { id: 3, method: 'app.context' } })
      expect(acceptCalls()).toHaveLength(1)
      expect(replies().map(r => r.result)).toEqual([
        { appId: APP_ID, selfUserId: USER_ID, permissions: ALL, groups: true, launch: { sessionId: 's1', invite: asApp } },
        [invite],
        { appId: APP_ID, selfUserId: USER_ID, permissions: ALL, groups: true, launch: { sessionId: 's1', invite: asApp } },
      ])
    })

    it('an invite that no longer waits (409) still names the game, without an invite', async () => {
      mockApiPost.mockRejectedValueOnce(new ApiError(409, 'x'))
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context' } })
      expect(replies()[0]).toMatchObject({ ok: true, result: { launch: { sessionId: 's1' } } })
      expect(replies()[0].result.launch).not.toHaveProperty('invite')
    })

    it('Play on a table hands over the table and accepts nothing', async () => {
      mockApiGet.mockResolvedValueOnce({
        group: {
          session_id: 's1', app_id: APP_ID, state: 'lobby', me: 2, my_state: 'seated',
          host: 1, is_host: false, min: 2, max: 4, late_join: false, seq: 0,
          players: [{ seat: 2, name: 'Ben', household: null, is_local: true, state: 'seated' }],
        },
      })
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context' } })
      expect(acceptCalls()).toEqual([])
      const launch = replies()[0].result.launch as { sessionId: string; group: { id: string; me: number } }
      expect(launch.sessionId).toBe('s1')
      expect(launch.group).toMatchObject({ id: 's1', me: 2, isHost: false })
      expect(replies()[0].result.groups).toBe(true)
    })

    it('an older app (app.pendingSessions) gets that one invite, not every waiting one', async () => {
      mockApiPost.mockResolvedValueOnce({ session: invite })
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 44, method: 'app.pendingSessions' } })
      expect(replies()[0]).toMatchObject({ id: 44, ok: true, result: [invite] })
      expect(mockApiGet.mock.calls.map(c => c[0])).toEqual([`/api/apps/${APP_ID}/groups/s1`])
    })
  })

  describe('held messages (the mailbox, D11)', () => {
    const rows = [
      { session_id: 'g1', from_instance: 'peer', from_user: 'alice', kind: 'message', payload: { san: 'Nf3' } },
      { session_id: 'g2', from_instance: 'peer', from_user: null, kind: 'session', payload: { verb: 'close', reason: 'declined' } },
    ]
    const events = [
      { kind: 'message', sessionId: 'g1', fromInstance: 'peer', fromUser: 'alice', payload: { san: 'Nf3' } },
      { kind: 'session', sessionId: 'g2', fromInstance: 'peer', fromUser: undefined, payload: { verb: 'close', reason: 'declined' } },
    ]
    const posted = () => (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls
      .map(([m]) => m as Record<string, unknown>)

    it('an app that does not pull gets them as live events right after app.context (older SDKs)', async () => {
      mockApiPost.mockResolvedValueOnce({ messages: rows })
      const handler = captureMessageHandler()
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context' } })
      expect(heldCalls()).toEqual([[`/api/apps/${APP_ID}/held`]])
      const out = posted()
      expect(out[0]).toMatchObject({ id: 1, ok: true })
      expect(out.slice(1)).toEqual(events.map(e => ({ type: 'app:event', ...e })))
      // Once per frame: a second app.context takes nothing more.
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 2, method: 'app.context' } })
      expect(heldCalls()).toHaveLength(1)
      expect(posted()).toHaveLength(4)
    })

    it('an app that pulls (SDK ≥ 1.3) gets nothing pushed and asks with app.held', async () => {
      const handler = captureMessageHandler()
      await handler({
        source: iframe.contentWindow as unknown as Window,
        data: { id: 1, method: 'app.context', params: { held: 'pull' } },
      })
      expect(heldCalls()).toEqual([])
      expect(posted()).toHaveLength(1)
      mockApiPost.mockResolvedValueOnce({ messages: rows })
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 2, method: 'app.held' } })
      expect(posted()[1]).toEqual({ id: 2, ok: true, result: events })
      // Only the 1:1 half: group messages stay held for group.held.
      expect(heldCalls()).toEqual([[`/api/apps/${APP_ID}/held?only=messages`]])
    })

    it('group messages that waited come as group:event, after the 1:1 ones; a pulling app asks with group.held', async () => {
      const group = [{ session_id: 't1', seq: 3, from_seat: 2, direct: false, data: { v: 1 } }]
      const asEvent = { type: 'message', id: 't1', seq: 3, from: 2, data: { v: 1 }, direct: false, held: true }
      mockApiPost.mockResolvedValueOnce({ messages: rows, group_messages: group })
      const handler = captureMessageHandler()
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context' } })
      expect(posted().at(-1)).toEqual({ type: 'group:event', event: asEvent })
      expect(posted()).toHaveLength(4)
      // A new frame that pulls: one take, shared by app.held and group.held.
      cleanup()
      ;(iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mockClear()
      cleanup = mountBridge(iframe, { appId: APP_ID, selfUserId: USER_ID, permissions: ALL })
      const pull = captureMessageHandler()
      await pull({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context', params: { held: 'pull' } } })
      mockApiPost.mockResolvedValueOnce({ messages: [], group_messages: group })
      await pull({ source: iframe.contentWindow as unknown as Window, data: { id: 2, method: 'group.held' } })
      mockApiPost.mockResolvedValueOnce({ messages: rows, group_messages: [] })
      await pull({ source: iframe.contentWindow as unknown as Window, data: { id: 3, method: 'app.held' } })
      expect(posted()[1]).toEqual({ id: 2, ok: true, result: [asEvent] })
      expect(posted()[2]).toEqual({ id: 3, ok: true, result: events })
      // Each half its own take: pulling one never clears the other.
      expect(heldCalls().slice(1)).toEqual([
        [`/api/apps/${APP_ID}/held?only=group`],
        [`/api/apps/${APP_ID}/held?only=messages`],
      ])
    })

    it('a failed take hands over nothing (what waits stays for the next open)', async () => {
      mockApiPost.mockRejectedValueOnce(new ApiError(500, 'x'))
      const handler = captureMessageHandler()
      await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.held' } })
      expect(posted()[0]).toEqual({ id: 1, ok: true, result: [] })
    })
  })

  it('app.pendingSessions: opened without Play it is empty — opening the app accepts nothing', async () => {
    const handler = captureMessageHandler()
    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 44, method: 'app.pendingSessions' },
    })
    expect(mockApiGet).not.toHaveBeenCalled()
    expect(mockApiPost).not.toHaveBeenCalled()
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      expect.objectContaining({ id: 44, ok: true, result: [] }),
      '*',
    )
  })

  it('app.openSession: gone (D3) — refused with use_people_pick, nothing is opened', async () => {
    const handler = captureMessageHandler()

    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: {
        id: 42,
        method: 'app.openSession',
        params: { target: { instance_id: 'peer', user_ref: 'bob', is_local: false } },
      },
    })

    // An app can't open a session without the sheet: no POST /sessions.
    expect(mockApiPost).not.toHaveBeenCalled()
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      { id: 42, ok: false, error: { code: 'use_people_pick', message: expect.any(String) } },
      '*',
    )
  })

  it('app.send to a game the server doesn\'t know comes back as unknown_session', async () => {
    const err = new ApiError(403, 'x') as ApiError & { code: string }
    Object.assign(err, { code: 'APP_SESSION_UNKNOWN' })
    mockApiPost.mockRejectedValueOnce(err)
    const handler = captureMessageHandler()
    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: {
        id: 43,
        method: 'app.send',
        params: { session_id: 'new-id', target: { instance_id: 'peer', user_ref: 'bob', is_local: false }, payload: {} },
      },
    })
    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      { id: 43, ok: false, error: { code: 'unknown_session', message: expect.any(String) } },
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

  it('WS relay: forwards session kind frames (a close) with kind="session" and fromUser', () => {
    wsHandlers['app.message']({
      data: {
        app_id: APP_ID,
        session_id: 'sess-1',
        from_instance: 'peer.example.com',
        from_user: 'user-alice',
        kind: 'session',
        payload: { verb: 'close', reason: 'declined' },
      },
    })

    expect(iframe.contentWindow!.postMessage).toHaveBeenCalledWith(
      {
        type: 'app:event',
        kind: 'session',
        sessionId: 'sess-1',
        fromInstance: 'peer.example.com',
        fromUser: 'user-alice',
        payload: { verb: 'close', reason: 'declined' },
      },
      '*',
    )
  })

  it('WS relay: a live invite never reaches the app (D3: it waits for Play) and accepts nothing', async () => {
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
    expect(mockApiGet).not.toHaveBeenCalled()
    expect(mockApiPost).not.toHaveBeenCalled()
    // The app never learns who invited the person before Play.
    expect(iframe.contentWindow!.postMessage).not.toHaveBeenCalled()
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

// ── Permissions: the bridge only offers what the household agreed to ───────

describe('mountBridge permissions', () => {
  let iframe: HTMLIFrameElement
  let cleanup: () => void
  let addEventSpy: MockInstance
  let removeEventSpy: MockInstance

  function mount(permissions: string[], launchSession?: string) {
    cleanup = mountBridge(iframe, {
      appId: APP_ID, selfUserId: USER_ID, permissions, launchSession, pickPeople: async () => [],
    })
    return captureMessageHandler()
  }

  const replies = () => (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls
    .map(([m]) => m as { id: number; ok: boolean; result?: unknown; error?: Record<string, unknown> })

  const LOCAL = { instance_id: 'own', user_ref: 'u-carl', is_local: true }
  const REMOTE = { instance_id: 'peer', user_ref: 'bob', is_local: false }

  beforeEach(() => {
    mockApiGet.mockReset()
    mockApiPost.mockReset()
    mockApiPut.mockReset()
    mockApiDelete.mockReset()
    Object.keys(wsHandlers).forEach(k => delete wsHandlers[k])
    iframe = makeFakeIframe()
    _resetAcceptedForTests()
    _resetPickForTests()
    addEventSpy = vi.spyOn(window, 'addEventListener')
    removeEventSpy = vi.spyOn(window, 'removeEventListener')
  })

  afterEach(() => {
    cleanup?.()
    addEventSpy.mockRestore()
    removeEventSpy.mockRestore()
  })

  /** Each bridge call and the permission it needs. */
  const CALLS: [string, Record<string, unknown> | undefined, string][] = [
    ['store.get', { key: 'k' }, 'storage'],
    ['store.set', { key: 'k', value: 1 }, 'storage'],
    ['store.delete', { key: 'k' }, 'storage'],
    ['store.list', undefined, 'storage'],
    ['contacts.list', undefined, 'people'],
    ['people.pick', { max: 1 }, 'people'],
    ['peers.list', undefined, 'federation'],
    ['app.held', undefined, 'people'],
    ['app.pendingSessions', undefined, 'people'],
    ['app.send', { session_id: 's1', target: LOCAL, payload: {} }, 'people'],
    ['app.send', { session_id: 's1', target: REMOTE, payload: {} }, 'federation'],
    ['app.cancelSession', { sessionId: 's1', target: LOCAL }, 'people'],
    ['app.cancelSession', { sessionId: 's1', target: REMOTE }, 'federation'],
    ['group.list', undefined, 'people'],
    ['group.get', { id: 't1' }, 'people'],
    ['group.send', { id: 't1', data: 1 }, 'people'],
    ['group.start', { id: 't1' }, 'people'],
    ['group.leave', { id: 't1' }, 'people'],
    ['group.remove', { id: 't1', seat: 2 }, 'people'],
    ['group.close', { id: 't1' }, 'people'],
    ['group.held', undefined, 'people'],
  ]

  it.each(CALLS)('%s refuses without the permission it needs (%j → %s)', async (method, params, permission) => {
    const handler = mount(['storage', 'people', 'federation'].filter(p => p !== permission
      // Without people, federation can't be granted either (it implies people).
      && !(permission === 'people' && p === 'federation')))
    await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 7, method, params } })
    expect(replies()).toEqual([{
      id: 7,
      ok: false,
      error: { code: 'permission_denied', message: expect.any(String), permission },
    }])
    // Never reached the server.
    expect(mockApiGet).not.toHaveBeenCalled()
    expect(mockApiPost).not.toHaveBeenCalled()
    expect(mockApiPut).not.toHaveBeenCalled()
    expect(mockApiDelete).not.toHaveBeenCalled()
  })

  it.each(CALLS)('%s goes through with the permission it needs (%j → %s)', async (method, params, permission) => {
    const table = { session_id: 't1', app_id: 'x', state: 'lobby', me: 1, my_state: 'seated', host: 1, is_host: true, min: 2, max: 2, late_join: false, players: [] }
    mockApiGet.mockResolvedValue({ key: 'k', value: 1, items: [], contacts: [], peers: [], groups: [], group: table })
    mockApiPost.mockResolvedValue({ session_id: 's1', messages: [], group: table, seq: 1 })
    mockApiPut.mockResolvedValue({})
    mockApiDelete.mockResolvedValue({})
    const handler = mount(permission === 'federation' ? ['federation', 'people'] : [permission])
    await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 8, method, params } })
    expect(replies()[0]).toMatchObject({ id: 8, ok: true })
  })

  it('group.open needs people, and answers unavailable until the group sheet lands', async () => {
    let handler = mount(['storage'])
    await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 9, method: 'group.open', params: { suggest: [] } } })
    expect(replies()[0]).toMatchObject({ ok: false, error: { code: 'permission_denied', permission: 'people' } })
    cleanup()
    ;(iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mockClear()
    handler = mount(['people'])
    await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 10, method: 'group.open', params: { suggest: [] } } })
    expect(replies()[0]).toMatchObject({ ok: false, error: { code: 'unavailable' } })
  })

  it('a group refusal from the server comes back with its code (and when to retry)', async () => {
    const handler = mount(['people'])
    const busy = new ApiError(429, 'x') as ApiError & { code: string; extra: Record<string, unknown> }
    Object.assign(busy, { code: 'APP_GROUP_BUSY', extra: { params: { retry_after: 3 } } })
    mockApiPost.mockRejectedValueOnce(busy)
    await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 11, method: 'group.send', params: { id: 't1', data: 1 } } })
    expect(replies()[0]).toMatchObject({ ok: false, error: { code: 'group_busy', retryAfterMs: 3000 } })
  })

  it('app.context tells the app its permissions, so it can leave out what it may not do', async () => {
    const handler = mount(['storage'])
    await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context' } })
    expect(replies()[0]).toMatchObject({ ok: true, result: { permissions: ['storage'] } })
    // Without people there is no mailbox to push.
    expect(mockApiPost).not.toHaveBeenCalled()
  })

  it('without people a Play launch accepts nothing', async () => {
    const handler = mount(['storage'], 's1')
    await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 1, method: 'app.context' } })
    expect(replies()[0]).toMatchObject({ ok: true, result: { launch: {} } })
    expect(mockApiPost).not.toHaveBeenCalled()
  })

  it('a server refusal comes back as the same typed error', async () => {
    const err = new ApiError(403, 'x') as ApiError & { code: string; extra: Record<string, unknown> }
    Object.assign(err, { code: 'APP_PERMISSION_DENIED', extra: { params: { permission: 'federation' } } })
    mockApiPost.mockRejectedValueOnce(err)
    const handler = mount(['federation', 'people'])
    await handler({
      source: iframe.contentWindow as unknown as Window,
      data: { id: 9, method: 'app.send', params: { session_id: 's1', target: REMOTE, payload: {} } },
    })
    expect(replies()[0]).toEqual({
      id: 9,
      ok: false,
      error: { code: 'permission_denied', message: expect.any(String), permission: 'federation' },
    })
  })

  it('the live relay stays quiet for an app without people', () => {
    mount(['storage'])
    wsHandlers['app.message']?.({ data: { app_id: APP_ID, kind: 'message', session_id: 's1', payload: {} } })
    expect(iframe.contentWindow!.postMessage).not.toHaveBeenCalled()
  })

  it('an unknown permission name grants nothing (fails closed)', async () => {
    const handler = mount(['camera'])
    await handler({ source: iframe.contentWindow as unknown as Window, data: { id: 3, method: 'store.list' } })
    expect(replies()[0]).toMatchObject({ ok: false, error: { code: 'permission_denied', permission: 'storage' } })
  })
})

// ── people.pick: invites go through Social Home's sheet (D3) ──────────────

describe('people.pick (the invite sheet)', () => {
  let iframe: HTMLIFrameElement
  let cleanup: () => void
  let addEventSpy: MockInstance
  let removeEventSpy: MockInstance
  /** The sheets the host was asked to show; resolve one to "close" it. */
  let sheets: { req: PickRequest; resolve: (p: PickedPerson[]) => void }[]

  const BEN: PickedPerson = {
    instance_id: 'brookside', user_ref: 'ben', is_local: false,
    display_name: 'Ben · Brookside', session_id: 'sess-1',
  }

  function mount(appId = APP_ID) {
    cleanup = mountBridge(iframe, {
      appId, selfUserId: USER_ID, permissions: ALL,
      pickPeople: req => new Promise(resolve => { sheets.push({ req, resolve }) }),
    })
    return captureMessageHandler()
  }

  const replies = () => (iframe.contentWindow!.postMessage as ReturnType<typeof vi.fn>).mock.calls
    .map(([m]) => m as { id: number; ok: boolean; result?: unknown; error?: Record<string, unknown> })

  const call = (handler: (e: Partial<MessageEvent>) => Promise<void>, id: number, params?: unknown) =>
    handler({ source: iframe.contentWindow as unknown as Window, data: { id, method: 'people.pick', params } })

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-10-29T12:00:00Z'))
    mockApiGet.mockReset()
    mockApiPost.mockReset()
    iframe = makeFakeIframe()
    sheets = []
    _resetPickForTests()
    addEventSpy = vi.spyOn(window, 'addEventListener')
    removeEventSpy = vi.spyOn(window, 'removeEventListener')
  })

  afterEach(() => {
    cleanup?.()
    addEventSpy.mockRestore()
    removeEventSpy.mockRestore()
    vi.useRealTimers()
  })

  it('opens Social Home\'s sheet; the app gets only who was picked and the session ids', async () => {
    const handler = mount()
    await call(handler, 1, { max: 1 })
    expect(sheets).toHaveLength(1)
    // The bridge itself opens no session and reads no roster: the sheet does.
    expect(mockApiPost).not.toHaveBeenCalled()
    expect(mockApiGet).not.toHaveBeenCalled()
    sheets[0].resolve([{ ...BEN, extra: 'never forwarded' } as PickedPerson])
    await new Promise<void>(r => setTimeout(r, 0))
    expect(replies()).toEqual([{
      id: 1,
      ok: true,
      result: [{
        instance_id: 'brookside', user_ref: 'ben', is_local: false,
        display_name: 'Ben · Brookside', session_id: 'sess-1',
      }],
    }])
  })

  it('cancel: an empty list', async () => {
    const handler = mount()
    await call(handler, 1)
    sheets[0].resolve([])
    await new Promise<void>(r => setTimeout(r, 0))
    expect(replies()).toEqual([{ id: 1, ok: true, result: [] }])
  })

  it('one sheet at a time', async () => {
    const handler = mount()
    await call(handler, 1)
    await call(handler, 2)
    expect(sheets).toHaveLength(1)
    expect(replies()).toEqual([{ id: 2, ok: false, error: { code: 'busy', message: expect.any(String) } }])
  })

  it('5 s before the same app can open it again (after Cancel or Send)', async () => {
    const handler = mount()
    await call(handler, 1)
    sheets[0].resolve([])
    await new Promise<void>(r => setTimeout(r, 0))
    vi.setSystemTime(Date.now() + PICK_COOLDOWN_MS - 1000)
    await call(handler, 2)
    expect(sheets).toHaveLength(1)
    expect(replies()[1]).toEqual({
      id: 2,
      ok: false,
      error: { code: 'too_soon', message: expect.any(String), retryAfterMs: 1000 },
    })
    vi.setSystemTime(Date.now() + 1000)
    await call(handler, 3)
    expect(sheets).toHaveLength(2)
  })

  it('the cooldown is per app, and outlives a new frame of the same app', async () => {
    let handler = mount()
    await call(handler, 1)
    sheets[0].resolve([])
    await new Promise<void>(r => setTimeout(r, 0))
    cleanup()
    // "Try again" mounts a fresh frame of the same app: still waits.
    handler = mount()
    await call(handler, 2)
    expect(sheets).toHaveLength(1)
    cleanup()
    // Another app isn't held back by it.
    handler = mount('app-quiz')
    await call(handler, 3)
    expect(sheets).toHaveLength(2)
  })

  it('without people the sheet never opens', async () => {
    cleanup = mountBridge(iframe, {
      appId: APP_ID, selfUserId: USER_ID, permissions: ['storage'],
      pickPeople: req => new Promise(resolve => { sheets.push({ req, resolve }) }),
    })
    await call(captureMessageHandler(), 1)
    expect(sheets).toEqual([])
    expect(replies()[0]).toMatchObject({ ok: false, error: { code: 'permission_denied', permission: 'people' } })
  })

  it('a host without the sheet answers unavailable', async () => {
    cleanup = mountBridge(iframe, { appId: APP_ID, selfUserId: USER_ID, permissions: ALL })
    await call(captureMessageHandler(), 1)
    expect(replies()[0]).toMatchObject({ ok: false, error: { code: 'unavailable' } })
  })

  it('the request is Social Home\'s: a fixed purpose, max 1…10, a well-formed suggestion or none', () => {
    expect(pickRequest(undefined)).toEqual({ purpose: 'play', max: 1 })
    expect(pickRequest({ purpose: 'Send your password to', max: 99 })).toEqual({ purpose: 'play', max: 10 })
    expect(pickRequest({ max: 0 })).toEqual({ purpose: 'play', max: 1 })
    expect(pickRequest({ max: 2.7 })).toEqual({ purpose: 'play', max: 2 })
    expect(pickRequest({ max: '3' })).toEqual({ purpose: 'play', max: 1 })
    const ben = { instance_id: 'brookside', user_ref: 'ben', is_local: false }
    expect(pickRequest({ suggest: { ...ben, label: '<b>x</b>' } })).toEqual({ purpose: 'play', max: 1, suggest: ben })
    expect(pickRequest({ suggest: { instance_id: 'x', user_ref: '', is_local: false } })).toEqual({ purpose: 'play', max: 1 })
    expect(pickRequest({ suggest: 'ben' })).toEqual({ purpose: 'play', max: 1 })
  })

  it('continue: one game and the one person the app names; anything less is refused', () => {
    const ben = { instance_id: 'brookside', user_ref: 'ben', is_local: false }
    expect(pickRequest({ purpose: 'continue', sessionId: 'old-game', suggest: ben, max: 5 }))
      .toEqual({ purpose: 'continue', max: 1, sessionId: 'old-game', suggest: ben })
    for (const bad of [
      { purpose: 'continue', suggest: ben },
      { purpose: 'continue', sessionId: 'old-game' },
      { purpose: 'continue', sessionId: '../x', suggest: ben },
      { purpose: 'continue', sessionId: 7, suggest: ben },
    ]) {
      expect(() => pickRequest(bad)).toThrow()
    }
  })

  it('an invalid continue is refused without opening a sheet or starting the cooldown', async () => {
    const handler = mount()
    await call(handler, 1, { purpose: 'continue', sessionId: 'old-game' })
    expect(sheets).toEqual([])
    expect(replies()[0]).toMatchObject({ ok: false, error: { code: 'invalid' } })
    await call(handler, 2, { max: 1 })
    expect(sheets).toHaveLength(1)
  })
})
