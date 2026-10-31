/**
 * Tests for the gift-draw half of the app bridge (drawBridge.ts): the
 * ``draw.*`` calls, the status shape the app sees (never a result), the
 * reveal that opens Social Home's own sheet and hands the app nothing,
 * the live ``draw:event`` posts and the refusal codes.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

type WsHandler = (evt: { data: Record<string, unknown> }) => void
const wsHandlers: Record<string, WsHandler> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, handler: WsHandler) => {
      wsHandlers[type] = handler
      return () => { delete wsHandlers[type] }
    },
  },
}))

const mockGet = vi.fn()
const mockPost = vi.fn()
const mockPut = vi.fn()
const mockPatch = vi.fn()

vi.mock('@/api', () => {
  class ApiError extends Error {
    public readonly code: string | null
    public readonly extra: Record<string, unknown>
    constructor(public readonly status: number, public readonly path: string, parsed?: Record<string, unknown>) {
      super(`API ${status}: ${path}`)
      this.name = 'ApiError'
      this.code = (parsed?.code as string) ?? null
      this.extra = Object.fromEntries(Object.entries(parsed ?? {}).filter(([k]) => k !== 'code'))
    }
  }
  return {
    ApiError,
    api: {
      get: (...a: unknown[]) => mockGet(...a),
      post: (...a: unknown[]) => mockPost(...a),
      put: (...a: unknown[]) => mockPut(...a),
      patch: (...a: unknown[]) => mockPatch(...a),
    },
  }
})

import { ApiError } from '@/api'
import { GroupRefused } from '@/features/apps/groupBridge'
import { DrawBridge, drawRefusal, getDrawResult, isDrawMethod, toDrawStatus, type DrawView } from './drawBridge'

const APP = 'gift-draw'
const T = 'tbl1'
const BASE = `/api/apps/${APP}/groups/${T}/draw`

function view(over: Partial<DrawView> = {}): DrawView {
  return {
    state: 'gathering',
    reason: null,
    gen: 0,
    again: 0,
    round: 0,
    of: 0,
    done: null,
    settings: null,
    wish: '',
    has_result: false,
    is_host: false,
    waiting_for: null,
    false_complaints: null,
    ...over,
  }
}

beforeEach(() => {
  for (const m of [mockGet, mockPost, mockPut, mockPatch]) m.mockReset()
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
})

describe('isDrawMethod', () => {
  it('names the draw.* calls only', () => {
    expect(isDrawMethod('draw.status')).toBe(true)
    expect(isDrawMethod('group.get')).toBe(false)
  })
})

describe('toDrawStatus', () => {
  it('is the app-safe status: state, counts, own wish — no result', () => {
    const s = toDrawStatus(T, view({ state: 'ready', has_result: true, of: 6, done: 6, wish: 'socks', is_host: true }))
    expect(s).toEqual({
      id: T, state: 'ready', round: 0, again: 0, of: 6, done: 6, settings: null,
      wish: 'socks', hasResult: true, isHost: true,
    })
    expect(JSON.stringify(s)).not.toMatch(/name|household|recipient/)
  })

  it('passes the organiser-only fields on when present', () => {
    const s = toDrawStatus(T, view({ state: 'failed', reason: 'rules', waiting_for: [{ seats: [3, 4] }], false_complaints: [{ seats: [5] }] }))
    expect(s.reason).toBe('rules')
    expect(s.waitingFor).toEqual([{ seats: [3, 4] }])
    expect(s.falseComplaints).toEqual([{ seats: [5] }])
  })
})

describe('DrawBridge.call', () => {
  it('status reads the draw route', async () => {
    mockGet.mockResolvedValue({ draw: view({ state: 'drawing', round: 2 }) })
    const b = new DrawBridge(APP, () => {})
    const s = await b.call('draw.status', { id: T }) as { state: string, round: number }
    expect(mockGet).toHaveBeenCalledWith(BASE)
    expect([s.state, s.round]).toEqual(['drawing', 2])
  })

  it('configure sends only the known settings', async () => {
    mockPatch.mockResolvedValue({ draw: view() })
    await new DrawBridge(APP, () => {}).call('draw.configure', {
      id: T, settings: { name: 'Familie Müller', budget: '20 €', day: '2026-12-24', pairs: [[1, 2]], evil: 1 },
    })
    expect(mockPatch).toHaveBeenCalledWith(BASE, { name: 'Familie Müller', budget: '20 €', day: '2026-12-24', pairs: [[1, 2]] })
  })

  it('setWishes and start go to their routes', async () => {
    mockPut.mockResolvedValue({ draw: view() })
    mockPost.mockResolvedValue({ draw: view({ state: 'drawing' }) })
    const b = new DrawBridge(APP, () => {})
    await b.call('draw.setWishes', { id: T, text: 'a book' })
    expect(mockPut).toHaveBeenCalledWith(`${BASE}/wish`, { text: 'a book' })
    await b.call('draw.start', { id: T, again: true })
    expect(mockPost).toHaveBeenCalledWith(`${BASE}/start`, { again: true })
    await b.call('draw.start', { id: T, again: 'yes' })
    expect(mockPost).toHaveBeenLastCalledWith(`${BASE}/start`, { again: false })
  })

  it('reveal opens Social Home\'s sheet and hands the app nothing', async () => {
    const reveal = vi.fn()
    const out = await new DrawBridge(APP, () => {}, reveal).call('draw.reveal', { id: T })
    expect(out).toBeNull()
    expect(reveal).toHaveBeenCalledWith(T)
    // No route was read: the result never passes through the bridge.
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('reveal without a sheet is unavailable', async () => {
    await expect(new DrawBridge(APP, () => {}).call('draw.reveal', { id: T }))
      .rejects.toMatchObject({ code: 'unavailable' })
  })

  it('there is no call that returns a result', async () => {
    for (const m of ['draw.result', 'draw.results', 'draw.get']) {
      await expect(new DrawBridge(APP, () => {}).call(m, { id: T })).rejects.toMatchObject({ code: 'unknown_method' })
    }
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('refuses a malformed table id', async () => {
    await expect(new DrawBridge(APP, () => {}).call('draw.status', { id: '../x' })).rejects.toBeInstanceOf(GroupRefused)
  })

  it('maps server refusals to app codes', async () => {
    mockPost.mockRejectedValue(new ApiError(409, BASE, { code: 'APP_DRAW_TOO_FEW' }))
    await expect(new DrawBridge(APP, () => {}).call('draw.start', { id: T })).rejects.toMatchObject({ code: 'too_few' })
    expect(drawRefusal(new ApiError(409, 'x', { code: 'APP_GROUP_FULL' }))).toBeNull()
  })
})

describe('DrawBridge.listen', () => {
  it('posts app.draw frames of this app as draw:event status', () => {
    const post = vi.fn()
    const off = new DrawBridge(APP, post).listen(() => true)
    wsHandlers['app.draw']({ data: { app_id: 'other', session_id: T, draw: view() } })
    wsHandlers['app.draw']({ data: { app_id: APP, session_id: T, draw: view({ state: 'ready', has_result: true }) } })
    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][0]).toMatchObject({ type: 'draw:event', event: { type: 'status', id: T, status: { state: 'ready', hasResult: true } } })
    off()
    expect(wsHandlers['app.draw']).toBeUndefined()
  })

  it('stays quiet without the people permission', () => {
    const post = vi.fn()
    new DrawBridge(APP, post).listen(() => false)
    wsHandlers['app.draw']({ data: { app_id: APP, session_id: T, draw: view() } })
    expect(post).not.toHaveBeenCalled()
  })
})

describe('getDrawResult', () => {
  it('reads the result route (the sheet only)', async () => {
    mockGet.mockResolvedValue({ result: { name: 'Ben' } })
    expect(await getDrawResult(APP, T)).toEqual({ name: 'Ben' })
    expect(mockGet).toHaveBeenCalledWith(`${BASE}/result`)
  })
})
