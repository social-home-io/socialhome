/**
 * Tests for the app-engine half of the app bridge (engineBridge.ts): the
 * ``engine.draw.*`` calls, the declared / supported gate, the status shape
 * the app sees (never a result), the reveal that opens Social Home's own
 * sheet and hands the app nothing, the ``engine:event`` posts and the
 * refusal codes.
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
    },
  }
})

import { ApiError } from '@/api'
import { GroupRefused } from '@/features/apps/groupBridge'
import { EngineBridge, engineRefusal, getDrawResult, isEngineMethod, toDrawStatus, type DrawView } from './engineBridge'

const APP = 'gift-draw'
const T = 'tbl1'
const BASE = `/api/apps/${APP}/groups/${T}/engines/draw`

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
    blamed: null,
    ...over,
  }
}

const bridge = (reveal?: (id: string) => void, post = vi.fn(), engines = ['draw'], declared = ['draw']) =>
  new EngineBridge(APP, post, engines, declared, reveal)

beforeEach(() => {
  mockGet.mockReset()
  mockPost.mockReset()
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
})

describe('isEngineMethod', () => {
  it('names the engine.* calls only', () => {
    expect(isEngineMethod('engine.draw.status')).toBe(true)
    expect(isEngineMethod('draw.status')).toBe(false)
    expect(isEngineMethod('group.get')).toBe(false)
  })
})

describe('the gate', () => {
  it('offers only engines declared AND here', () => {
    expect(bridge(undefined, vi.fn(), ['draw', 'deal'], ['draw', 'deal']).available()).toEqual(['draw'])
  })

  it('refuses an engine the app did not declare', async () => {
    await expect(bridge(undefined, vi.fn(), [], []).call('engine.draw.status', { id: T }))
      .rejects.toMatchObject({ code: 'engine_not_declared' })
  })

  it('refuses a declared engine this Social Home does not have', async () => {
    await expect(bridge(undefined, vi.fn(), [], ['draw']).call('engine.draw.status', { id: T }))
      .rejects.toMatchObject({ code: 'engine_not_supported' })
    await expect(bridge(undefined, vi.fn(), ['deal'], ['deal']).call('engine.deal.status', { id: T }))
      .rejects.toMatchObject({ code: 'engine_not_supported' })
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('maps the server gate and the draw refusals', () => {
    expect(engineRefusal(new ApiError(403, 'x', { code: 'APP_ENGINE_NOT_DECLARED' }))?.code).toBe('engine_not_declared')
    expect(engineRefusal(new ApiError(409, 'x', { code: 'APP_ENGINE_UNSUPPORTED' }))?.code).toBe('engine_not_supported')
    expect(engineRefusal(new ApiError(409, 'x', { code: 'APP_DRAW_TOO_FEW' }))?.code).toBe('too_few')
    expect(engineRefusal(new ApiError(409, 'x', { code: 'APP_GROUP_FULL' }))).toBeNull()
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
    const s = toDrawStatus(T, view({ state: 'failed', reason: 'rules', waiting_for: [{ seats: [3, 4] }], blamed: [{ seats: [5] }] }))
    expect(s.reason).toBe('rules')
    expect(s.waitingFor).toEqual([{ seats: [3, 4] }])
    expect(s.blamed).toEqual([{ seats: [5] }])
  })
})

describe('engine.draw.*', () => {
  it('status reads the engine route', async () => {
    mockGet.mockResolvedValue({ state: view({ state: 'drawing', round: 2 }) })
    const s = await bridge().call('engine.draw.status', { id: T }) as { state: string, round: number }
    expect(mockGet).toHaveBeenCalledWith(BASE)
    expect([s.state, s.round]).toEqual(['drawing', 2])
  })

  it('configure sends only the fields given (a patch)', async () => {
    mockPost.mockResolvedValue({ state: view() })
    await bridge().call('engine.draw.configure', { id: T, settings: { pairs: [[1, 2]], evil: 1 } })
    expect(mockPost).toHaveBeenCalledWith(`${BASE}/configure`, { pairs: [[1, 2]] })
  })

  it('setWishes and start go to their routes', async () => {
    mockPost.mockResolvedValue({ state: view({ state: 'drawing' }) })
    const b = bridge()
    await b.call('engine.draw.setWishes', { id: T, text: 'a book' })
    expect(mockPost).toHaveBeenCalledWith(`${BASE}/wish`, { text: 'a book' })
    await b.call('engine.draw.start', { id: T, again: true })
    expect(mockPost).toHaveBeenCalledWith(`${BASE}/start`, { again: true })
    await b.call('engine.draw.start', { id: T, again: 'yes' })
    expect(mockPost).toHaveBeenLastCalledWith(`${BASE}/start`, { again: false })
  })

  it('reveal opens Social Home\'s sheet and hands the app nothing', async () => {
    const reveal = vi.fn()
    expect(await bridge(reveal).call('engine.draw.reveal', { id: T })).toBeNull()
    expect(reveal).toHaveBeenCalledWith(T)
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('reveal without a sheet is unavailable', async () => {
    await expect(bridge().call('engine.draw.reveal', { id: T })).rejects.toMatchObject({ code: 'unavailable' })
  })

  it('there is no call that returns a result', async () => {
    for (const m of ['engine.draw.result', 'engine.draw.results', 'engine.draw.get', 'engine.draw']) {
      await expect(bridge().call(m, { id: T })).rejects.toMatchObject({ code: 'unknown_method' })
    }
    expect(mockGet).not.toHaveBeenCalled()
  })

  it('refuses a malformed table id', async () => {
    await expect(bridge().call('engine.draw.status', { id: '../x' })).rejects.toBeInstanceOf(GroupRefused)
  })

  it('maps server refusals to app codes', async () => {
    mockPost.mockRejectedValue(new ApiError(409, BASE, { code: 'APP_DRAW_TOO_FEW' }))
    await expect(bridge().call('engine.draw.start', { id: T })).rejects.toMatchObject({ code: 'too_few' })
  })
})

describe('listen', () => {
  it('posts app.engine frames of this app as engine:event status', () => {
    const post = vi.fn()
    const off = bridge(undefined, post).listen(() => true)
    wsHandlers['app.engine']({ data: { app_id: 'other', engine: 'draw', session_id: T, state: view() } })
    wsHandlers['app.engine']({ data: { app_id: APP, engine: 'deal', session_id: T, state: view() } })
    wsHandlers['app.engine']({ data: { app_id: APP, engine: 'draw', session_id: T, state: view({ state: 'ready', has_result: true }) } })
    expect(post).toHaveBeenCalledTimes(1)
    expect(post.mock.calls[0][0]).toMatchObject({
      type: 'engine:event', engine: 'draw',
      event: { type: 'status', id: T, status: { state: 'ready', hasResult: true } },
    })
    off()
    expect(wsHandlers['app.engine']).toBeUndefined()
  })

  it('stays quiet without the people permission or an undeclared engine', () => {
    const post = vi.fn()
    bridge(undefined, post).listen(() => false)
    wsHandlers['app.engine']({ data: { app_id: APP, engine: 'draw', session_id: T, state: view() } })
    bridge(undefined, post, [], []).listen(() => true)
    wsHandlers['app.engine']({ data: { app_id: APP, engine: 'draw', session_id: T, state: view() } })
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
