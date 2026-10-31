/**
 * Tests for the group-table half of the app bridge (groupBridge.ts):
 * the ``group.*`` calls, the table shape the app sees (seats, never
 * usernames), the events derived from ``app.group`` frames, and the
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

const mockApiGet = vi.fn()
const mockApiPost = vi.fn()

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
      get: (...args: unknown[]) => mockApiGet(...args),
      post: (...args: unknown[]) => mockApiPost(...args),
    },
  }
})

import { ApiError } from '@/api'
import {
  diffEvents,
  GroupBridge,
  GroupRefused,
  groupRefusal,
  heldToEvent,
  isGroupMethod,
  toGroupSession,
  type GroupSession,
} from './groupBridge'

const APP = 'quiz'

function view(over: Record<string, unknown> = {}) {
  return {
    session_id: 't1',
    app_id: APP,
    state: 'lobby',
    end_reason: null,
    space_id: null,
    rev: 2,
    seq: 0,
    host: 1,
    is_host: true,
    me: 1,
    my_state: 'seated',
    min: 3,
    max: 6,
    late_join: false,
    note: null,
    created_at: 'x',
    players: [
      { seat: 1, name: 'Anna', household: null, is_local: true, state: 'seated' },
      { seat: 2, name: 'Ben', household: 'Okafor', is_local: false, state: 'seated' },
    ],
    ...over,
  }
}

describe('toGroupSession', () => {
  it('maps the server view to seats and names — no usernames, no extras', () => {
    const s = toGroupSession(view({ note: 'Snacks', end_reason: null }) as never)
    expect(s).toEqual({
      id: 't1', appId: APP, state: 'lobby', me: 1, myState: 'seated', host: 1,
      isHost: true, min: 3, max: 6, lateJoin: false, seq: 0, note: 'Snacks',
      players: [
        { seat: 1, name: 'Anna', household: null, isLocal: true, state: 'seated' },
        { seat: 2, name: 'Ben', household: 'Okafor', isLocal: false, state: 'seated' },
      ],
    })
    expect(JSON.stringify(s)).not.toContain('user')
  })

  it('carries an end reason and a space when there is one', () => {
    const s = toGroupSession(view({ state: 'ended', end_reason: 'cancelled', space_id: 'sp' }) as never)
    expect(s).toMatchObject({ endReason: 'cancelled', spaceId: 'sp' })
  })
})

describe('diffEvents', () => {
  const base = toGroupSession(view() as never)
  const players = (states: string[]) => states.map((state, i) => ({
    seat: i + 1, name: `P${i + 1}`, household: null, isLocal: false, state,
  })) as GroupSession['players']

  it('a new seat is joined; a seat turning left / removed says so', () => {
    const next = { ...base, players: players(['seated', 'left', 'seated']) }
    expect(diffEvents(base, next).map(e => [e.type, 'seat' in e ? e.seat : null])).toEqual([
      ['left', 2], ['joined', 3],
    ])
    const removed = { ...base, players: players(['seated', 'removed']) }
    expect(diffEvents(base, removed).map(e => e.type)).toEqual(['removed'])
  })

  it('started, host-changed and ended, each with the table after the change', () => {
    const next = { ...base, state: 'playing' as const, host: 2 }
    const events = diffEvents(base, next)
    expect(events.map(e => e.type)).toEqual(['started', 'host-changed'])
    expect(events.every(e => 'session' in e && e.session === next)).toBe(true)
    const ended = { ...next, state: 'ended' as const }
    expect(diffEvents(next, ended).map(e => e.type)).toEqual(['ended'])
    expect(diffEvents(ended, ended)).toEqual([])
  })
})

describe('refusals', () => {
  it('maps the server codes to the bridge codes', () => {
    const busy = groupRefusal(new ApiError(429, 'x', { code: 'APP_GROUP_BUSY', params: { retry_after: 7 } }))
    expect(busy).toMatchObject({ code: 'group_busy', retryAfterMs: 7000 })
    expect(groupRefusal(new ApiError(409, 'x', { code: 'APP_GROUP_SEAT' }))?.code).toBe('group_seat')
    expect(groupRefusal(new ApiError(403, 'x', { code: 'APP_GROUP_CANT_JOIN' }))?.code).toBe('cant_join')
    expect(groupRefusal(new ApiError(413, 'x', { code: 'PAYLOAD_TOO_LARGE' }))?.code).toBe('too_large')
    expect(groupRefusal(new ApiError(409, 'x', { code: 'APP_GROUPS_NOT_SUPPORTED' }))?.code).toBe('groups_not_supported')
    expect(groupRefusal(new ApiError(500, 'x', { code: 'BOOM' }))).toBeNull()
    expect(groupRefusal(new Error('x'))).toBeNull()
  })

  it('isGroupMethod', () => {
    expect(isGroupMethod('group.send')).toBe(true)
    expect(isGroupMethod('app.send')).toBe(false)
  })
})

describe('GroupBridge', () => {
  let posted: unknown[]
  let bridge: GroupBridge
  const held = vi.fn()

  beforeEach(() => {
    mockApiGet.mockReset()
    mockApiPost.mockReset()
    held.mockReset()
    Object.keys(wsHandlers).forEach(k => delete wsHandlers[k])
    posted = []
    bridge = new GroupBridge(APP, m => posted.push(m), held)
  })

  it('group.send posts data, to and turn; answers the seq', async () => {
    mockApiPost.mockResolvedValueOnce({ seq: 4 })
    await expect(bridge.call('group.send', { id: 't1', data: { a: 1 }, to: [2], turn: 2 }))
      .resolves.toEqual({ seq: 4 })
    expect(mockApiPost).toHaveBeenCalledWith(`/api/apps/${APP}/groups/t1/messages`, { data: { a: 1 }, to: [2], turn: 2 })
    mockApiPost.mockResolvedValueOnce({ seq: null })
    await expect(bridge.call('group.send', { id: 't1', data: 'x' })).resolves.toEqual({ seq: null })
    expect(mockApiPost).toHaveBeenLastCalledWith(`/api/apps/${APP}/groups/t1/messages`, { data: 'x' })
  })

  it('group.send passes final: true for the game\'s last message, drops final: false', async () => {
    mockApiPost.mockResolvedValueOnce({ seq: 9 })
    await bridge.call('group.send', { id: 't1', data: { over: true }, final: true })
    expect(mockApiPost).toHaveBeenLastCalledWith(`/api/apps/${APP}/groups/t1/messages`, { data: { over: true }, final: true })
    mockApiPost.mockResolvedValueOnce({ seq: 10 })
    await bridge.call('group.send', { id: 't1', data: 1, final: false })
    expect(mockApiPost).toHaveBeenLastCalledWith(`/api/apps/${APP}/groups/t1/messages`, { data: 1 })
  })

  it('refuses a bad id, to or turn without calling the server', async () => {
    for (const params of [
      { data: 1 },
      { id: '../x', data: 1 },
      { id: 't1', data: 1, to: [] },
      { id: 't1', data: 1, to: [0] },
      { id: 't1', data: 1, to: 'all' },
      { id: 't1', data: 1, turn: 1.5 },
      { id: 't1', data: 1, final: 'yes' },
    ]) {
      await expect(bridge.call('group.send', params)).rejects.toMatchObject({ code: 'invalid' })
    }
    await expect(bridge.call('group.remove', { id: 't1' })).rejects.toBeInstanceOf(GroupRefused)
    expect(mockApiPost).not.toHaveBeenCalled()
  })

  it('list / get / start / remove answer tables; leave / close answer ok', async () => {
    mockApiGet.mockResolvedValueOnce({ groups: [view()] })
    expect(await bridge.call('group.list', {})).toEqual([toGroupSession(view() as never)])
    mockApiGet.mockResolvedValueOnce({ group: view() })
    expect(await bridge.call('group.get', { id: 't1' })).toMatchObject({ id: 't1' })
    mockApiPost.mockResolvedValueOnce({ group: view({ state: 'playing' }) })
    expect(await bridge.call('group.start', { id: 't1' })).toMatchObject({ state: 'playing' })
    mockApiPost.mockResolvedValueOnce({ group: view() })
    await bridge.call('group.remove', { id: 't1', seat: 2 })
    expect(mockApiPost).toHaveBeenLastCalledWith(`/api/apps/${APP}/groups/t1/remove`, { seat: 2 })
    mockApiPost.mockResolvedValue({ ok: true })
    expect(await bridge.call('group.leave', { id: 't1' })).toEqual({ ok: true })
    expect(await bridge.call('group.close', { id: 't1' })).toEqual({ ok: true })
    expect(mockApiPost).toHaveBeenLastCalledWith(`/api/apps/${APP}/groups/t1/close`)
  })

  it('group.open on its own (no sheet) is unavailable; group.held hands over the take', async () => {
    await expect(bridge.call('group.open', { suggest: [] })).rejects.toMatchObject({ code: 'unavailable' })
    held.mockResolvedValueOnce([{ type: 'message' }])
    expect(await bridge.call('group.held', {})).toEqual([{ type: 'message' }])
    await expect(bridge.call('group.nope', {})).rejects.toMatchObject({ code: 'unknown_method' })
  })

  it('launch: a table the person sits at, else null', async () => {
    mockApiGet.mockResolvedValueOnce({ group: view() })
    expect(await bridge.launch('t1')).toMatchObject({ id: 't1', me: 1 })
    mockApiGet.mockRejectedValueOnce(new ApiError(404, 'x', { code: 'APP_GROUP_NOT_FOUND' }))
    expect(await bridge.launch('s9')).toBeNull()
  })

  it('relays messages and roster changes of this app as group:event', async () => {
    const stop = bridge.listen(() => true)
    wsHandlers['app.group.message']({ data: { app_id: APP, session_id: 't1', seq: 3, from_seat: 2, data: { x: 1 }, direct: true } })
    wsHandlers['app.group.message']({ data: { app_id: 'other', session_id: 't1', seq: 4, from_seat: 2, data: 1, direct: false } })
    expect(posted).toEqual([{ type: 'group:event', event: { type: 'message', id: 't1', seq: 3, from: 2, data: { x: 1 }, direct: true } }])
    posted.length = 0
    // First sighting: remembered, nothing to compare with.
    wsHandlers['app.group']({ data: { app_id: APP, session_id: 't1', group: view() } })
    expect(posted).toEqual([])
    const third = { seat: 3, name: 'Cleo', household: 'C', is_local: false, state: 'seated' }
    wsHandlers['app.group']({ data: { app_id: APP, session_id: 't1', group: view({ players: [...view().players, third] }) } })
    expect(posted).toMatchObject([{ type: 'group:event', event: { type: 'joined', id: 't1', seat: 3 } }])
    // My own place ends: a removed event for my seat.
    posted.length = 0
    wsHandlers['app.group']({ data: { app_id: APP, session_id: 't1', change: 'removed', reason: null } })
    expect(posted).toMatchObject([{ event: { type: 'removed', seat: 1, session: { myState: 'removed' } } }])
    stop()
    expect(wsHandlers['app.group']).toBeUndefined()
  })

  it('keep: a game the group sheet opened is the view the next frame diffs against', () => {
    bridge.listen(() => true)
    const kept = bridge.keep(toGroupSession(view() as never))
    expect(kept).toMatchObject({ id: 't1' })
    const third = { seat: 3, name: 'Cleo', household: 'C', is_local: false, state: 'seated' }
    wsHandlers['app.group']({ data: { app_id: APP, session_id: 't1', group: view({ players: [...view().players, third] }) } })
    expect(posted).toMatchObject([{ type: 'group:event', event: { type: 'joined', id: 't1', seat: 3 } }])
  })

  it('stays quiet when the app may not see people', () => {
    bridge.listen(() => false)
    wsHandlers['app.group.message']({ data: { app_id: APP, session_id: 't1', seq: 1, from_seat: 1, data: 1, direct: false } })
    expect(posted).toEqual([])
  })

  it('held rows become held message events', () => {
    expect(heldToEvent({ session_id: 't1', seq: 2, from_seat: 3, direct: false, data: 'x' })).toEqual({
      type: 'message', id: 't1', seq: 2, from: 3, data: 'x', direct: false, held: true,
    })
  })
})
