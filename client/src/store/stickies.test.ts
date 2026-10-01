/**
 * Per-scope sticky stores: one for the household, one per opened space.
 * Loads are deduped, WS frames route by ``space_id``, edits / moves roll
 * back only their own row's fields, deletes go through Undo, and the
 * Organize hub's count reads the household store only.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    patch: (...a: unknown[]) => apiPatch(...a),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
}))

const handlers: Record<string, (e: { data: object }) => void> = {}
const conn = vi.hoisted(() => ({ state: null as unknown as { value: string } }))
vi.mock('@/ws', async () => {
  const { signal: sig } = await import('@preact/signals')
  conn.state = sig('open')
  return {
    ws: { on: (type: string, h: (e: { data: object }) => void) => { handlers[type] = h } },
    connectionState: conn.state,
  }
})

const toasts = vi.hoisted(() => [] as Array<{
  msg: string
  opts?: { action?: { onClick: () => void }; onExpire?: () => void }
}>)
vi.mock('@/components/Toast', () => ({
  showToast: (msg: string, _kind: string, opts?: never) => { toasts.push({ msg, opts }); return toasts.length },
  dismissToast: vi.fn(),
}))

import {
  householdStickyStore, spaceStickyStore, householdStickyCount,
  resetStickies, wireStickiesWs, stickyStoreFor, type StickyRow,
} from './stickies'
import { resetPendingDeletes, pendingDeletes } from '@/utils/undoableDelete'


const row = (id: string, over: Partial<StickyRow> = {}): StickyRow => ({
  id, author: 'u1', content: `note ${id}`, color: '#FFF9B1', position_x: 100, position_y: 50,
  created_at: '', updated_at: '', space_id: null, ...over,
})

const flush = () => new Promise(r => setTimeout(r, 0))

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDelete.mockReset()
  toasts.length = 0
  resetPendingDeletes()
  resetStickies()
  wireStickiesWs()
})

describe('loading', () => {
  it('household loads /api/stickies, a space its own endpoint', async () => {
    apiGet.mockResolvedValueOnce([row('a')])
    await householdStickyStore.load()
    expect(apiGet).toHaveBeenCalledWith('/api/stickies')
    expect(householdStickyStore.rows.value.map(r => r.id)).toEqual(['a'])
    expect(householdStickyStore.loaded.value).toBe(true)

    apiGet.mockResolvedValueOnce([row('s', { space_id: 'sp 1' })])
    await spaceStickyStore('sp 1').load()
    expect(apiGet).toHaveBeenLastCalledWith('/api/spaces/sp%201/stickies')
    expect(householdStickyStore.rows.value.map(r => r.id)).toEqual(['a'])
  })

  it('concurrent loads share one request; ensure skips once loaded', async () => {
    let resolve!: (v: unknown) => void
    apiGet.mockReturnValue(new Promise(r => { resolve = r }))
    const a = householdStickyStore.ensure()
    const b = householdStickyStore.load()
    resolve([row('a')])
    await Promise.all([a, b])
    await householdStickyStore.ensure()
    expect(apiGet).toHaveBeenCalledTimes(1)
  })

  it('force skips the dedupe', async () => {
    apiGet.mockResolvedValue([row('a')])
    await householdStickyStore.load()
    await householdStickyStore.load({ force: true })
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('a failed load leaves the store unloaded and rejects', async () => {
    apiGet.mockRejectedValue(new Error('down'))
    await expect(householdStickyStore.load()).rejects.toThrow('down')
    expect(householdStickyStore.loaded.value).toBe(false)
    expect(householdStickyCount.value).toBeNull()
  })

  it('a response landing after reset is dropped', async () => {
    let resolve!: (v: unknown) => void
    apiGet.mockReturnValue(new Promise(r => { resolve = r }))
    const p = householdStickyStore.load()
    resetStickies()
    resolve([row('a')])
    await p
    expect(householdStickyStore.rows.value).toEqual([])
    expect(householdStickyStore.loaded.value).toBe(false)
  })

  it('stickyStoreFor picks the scope', () => {
    expect(stickyStoreFor(null)).toBe(householdStickyStore)
    expect(stickyStoreFor('x')).toBe(spaceStickyStore('x'))
  })
})

describe('household count', () => {
  it('is null until loaded, then follows the household store only', async () => {
    expect(householdStickyCount.value).toBeNull()
    apiGet.mockResolvedValueOnce([row('a'), row('b')])
    await householdStickyStore.load()
    expect(householdStickyCount.value).toBe(2)
    apiGet.mockResolvedValueOnce([row('s1'), row('s2'), row('s3')])
    await spaceStickyStore('sp').load()
    expect(householdStickyCount.value).toBe(2)
  })

  it('excludes stickies hidden behind an Undo toast', async () => {
    apiGet.mockResolvedValueOnce([row('a'), row('b')])
    await householdStickyStore.load()
    householdStickyStore.remove(householdStickyStore.rows.value[0])
    expect(householdStickyCount.value).toBe(1)
    expect(householdStickyStore.visible.value.map(r => r.id)).toEqual(['b'])
  })
})

describe('WS routing', () => {
  it('routes frames by space_id; a space never opened is ignored', async () => {
    apiGet.mockResolvedValueOnce([row('a')])
    await householdStickyStore.load()
    apiGet.mockResolvedValueOnce([])
    const sp = spaceStickyStore('sp')
    await sp.load()

    handlers['sticky.created']({ data: { ...row('b'), type: 'sticky.created' } })
    handlers['sticky.created']({ data: row('b') })
    handlers['sticky.created']({ data: row('x', { space_id: 'sp' }) })
    handlers['sticky.created']({ data: row('y', { space_id: 'never-opened' }) })
    expect(householdStickyStore.rows.value.map(r => r.id)).toEqual(['a', 'b'])
    expect(sp.rows.value.map(r => r.id)).toEqual(['x'])

    handlers['sticky.updated']({ data: { id: 'x', space_id: 'sp', content: 'hi', position_x: 5 } })
    expect(sp.find('x')?.content).toBe('hi')
    expect(sp.find('x')?.position_x).toBe(5)

    handlers['sticky.deleted']({ data: { id: 'a', space_id: null } })
    handlers['sticky.deleted']({ data: { id: 'x', space_id: 'sp' } })
    expect(householdStickyStore.rows.value.map(r => r.id)).toEqual(['b'])
    expect(sp.rows.value).toEqual([])
  })

  it('an update for an unknown sticky does not create a partial row', async () => {
    apiGet.mockResolvedValueOnce([])
    await householdStickyStore.load()
    handlers['sticky.updated']({ data: { id: 'ghost', space_id: null, content: 'x' } })
    expect(householdStickyStore.rows.value).toEqual([])
  })

  it('a reconnect revalidates every loaded store', async () => {
    apiGet.mockResolvedValue([])
    await householdStickyStore.load()
    await spaceStickyStore('sp').load()
    spaceStickyStore('never') // made, not loaded
    apiGet.mockClear()
    conn.state.value = 'reconnecting'
    conn.state.value = 'open'
    await flush()
    expect(apiGet.mock.calls.map(c => c[0]).sort()).toEqual(
      ['/api/spaces/sp/stickies', '/api/stickies'])
  })
})

describe('create / edit / move', () => {
  it('create POSTs and adds the row once (WS echo deduped)', async () => {
    apiGet.mockResolvedValueOnce([])
    await householdStickyStore.load()
    apiPost.mockResolvedValueOnce(row('n'))
    await householdStickyStore.create({ content: 'x', color: '#FFF9B1', position_x: 1, position_y: 2 })
    expect(apiPost).toHaveBeenCalledWith('/api/stickies',
      { content: 'x', color: '#FFF9B1', position_x: 1, position_y: 2 })
    handlers['sticky.created']({ data: { ...row('n') } })
    expect(householdStickyStore.rows.value.map(r => r.id)).toEqual(['n'])
  })

  it('edit is optimistic and a failure rolls back only that row', async () => {
    apiGet.mockResolvedValueOnce([row('a'), row('b')])
    await householdStickyStore.load()
    apiPatch.mockRejectedValueOnce(new Error('nope'))
    const p = householdStickyStore.patch('a', { content: 'new', color: '#FFB3B3' })
    expect(householdStickyStore.find('a')?.content).toBe('new')
    // Meanwhile another row changes and a WS frame recolours "a".
    handlers['sticky.updated']({ data: { id: 'b', space_id: null, content: 'other' } })
    handlers['sticky.updated']({ data: { id: 'a', space_id: null, color: '#B3D4FF' } })
    await expect(p).rejects.toThrow('nope')
    expect(householdStickyStore.find('a')?.content).toBe('note a')
    // The newer WS colour is kept.
    expect(householdStickyStore.find('a')?.color).toBe('#B3D4FF')
    expect(householdStickyStore.find('b')?.content).toBe('other')
  })

  it('moveLocal + commitMove PATCHes the position; a failure puts it back', async () => {
    apiGet.mockResolvedValueOnce([row('a', { position_x: 100, position_y: 50 })])
    await householdStickyStore.load()
    householdStickyStore.moveLocal('a', 300, 200)
    expect(householdStickyStore.find('a')?.position_x).toBe(300)
    apiPatch.mockRejectedValueOnce(new Error('offline'))
    await expect(householdStickyStore.commitMove('a', { x: 100, y: 50 })).rejects.toThrow('offline')
    expect(apiPatch).toHaveBeenCalledWith('/api/stickies/a', { position_x: 300, position_y: 200 })
    expect(householdStickyStore.find('a')?.position_x).toBe(100)
    expect(householdStickyStore.find('a')?.position_y).toBe(50)
  })

  it('commitMove without a change sends nothing', async () => {
    apiGet.mockResolvedValueOnce([row('a')])
    await householdStickyStore.load()
    await householdStickyStore.commitMove('a', { x: 100, y: 50 })
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('an older PATCH answering late does not undo a newer one', async () => {
    apiGet.mockResolvedValueOnce([row('a')])
    await householdStickyStore.load()
    let first!: (v: unknown) => void
    apiPatch.mockReturnValueOnce(new Promise(r => { first = r }))
    apiPatch.mockResolvedValueOnce(row('a', { content: 'two' }))
    const p1 = householdStickyStore.patch('a', { content: 'one' })
    await householdStickyStore.patch('a', { content: 'two' })
    first(row('a', { content: 'one' }))
    await p1
    expect(householdStickyStore.find('a')?.content).toBe('two')
  })
})

describe('delete with Undo', () => {
  it('hides at once, DELETEs when the toast expires, keeps WS echoes out', async () => {
    apiGet.mockResolvedValueOnce([row('a'), row('b')])
    await householdStickyStore.load()
    apiDelete.mockResolvedValueOnce(undefined)
    householdStickyStore.remove(householdStickyStore.find('a')!)
    expect(pendingDeletes.value.has('a')).toBe(true)
    expect(apiDelete).not.toHaveBeenCalled()
    toasts[0].opts!.onExpire!()
    await flush()
    expect(apiDelete).toHaveBeenCalledWith('/api/stickies/a')
    expect(householdStickyStore.rows.value.map(r => r.id)).toEqual(['b'])
    handlers['sticky.created']({ data: { ...row('a') } })
    expect(householdStickyStore.rows.value.map(r => r.id)).toEqual(['b'])
  })

  it('Undo brings it back without any request', async () => {
    apiGet.mockResolvedValueOnce([row('a')])
    await householdStickyStore.load()
    const onUndone = vi.fn()
    householdStickyStore.remove(householdStickyStore.find('a')!, { onUndone })
    toasts[0].opts!.action!.onClick()
    expect(householdStickyStore.visible.value.map(r => r.id)).toEqual(['a'])
    expect(onUndone).toHaveBeenCalled()
    expect(apiDelete).not.toHaveBeenCalled()
  })

  it('a 404 on commit counts as gone', async () => {
    apiGet.mockResolvedValueOnce([row('a')])
    await householdStickyStore.load()
    apiDelete.mockRejectedValueOnce(Object.assign(new Error('gone'), { status: 404 }))
    householdStickyStore.remove(householdStickyStore.find('a')!)
    toasts[0].opts!.onExpire!()
    await flush()
    expect(householdStickyStore.rows.value).toEqual([])
    expect(toasts).toHaveLength(1)
  })

  it('a space delete hits the space endpoint', async () => {
    const sp = spaceStickyStore('sp')
    apiGet.mockResolvedValueOnce([row('s', { space_id: 'sp' })])
    await sp.load()
    apiDelete.mockResolvedValueOnce(undefined)
    sp.remove(sp.find('s')!)
    toasts[0].opts!.onExpire!()
    await flush()
    expect(apiDelete).toHaveBeenCalledWith('/api/spaces/sp/stickies/s')
  })
})

describe('reset', () => {
  it('logout forgets every scope', async () => {
    apiGet.mockResolvedValue([row('a')])
    await householdStickyStore.load()
    const sp = spaceStickyStore('sp')
    await sp.load()
    resetStickies()
    expect(householdStickyStore.rows.value).toEqual([])
    expect(householdStickyCount.value).toBeNull()
    expect(spaceStickyStore('sp')).not.toBe(sp)
  })
})

describe('local moves win over server positions', () => {
  it('while held, WS frames and loads keep the local position but take other fields', async () => {
    apiGet.mockResolvedValueOnce([row('a', { position_x: 100, position_y: 50 })])
    await householdStickyStore.load()
    householdStickyStore.holdPosition('a')
    householdStickyStore.moveLocal('a', 400, 300)
    handlers['sticky.updated']({ data: { id: 'a', space_id: null, content: 'remote', position_x: 1, position_y: 2 } })
    expect(householdStickyStore.find('a')).toMatchObject({ content: 'remote', position_x: 400, position_y: 300 })
    apiGet.mockResolvedValueOnce([row('a', { content: 'loaded', position_x: 7, position_y: 8 })])
    await householdStickyStore.load({ force: true })
    expect(householdStickyStore.find('a')).toMatchObject({ content: 'loaded', position_x: 400, position_y: 300 })
    householdStickyStore.releasePosition('a')
    handlers['sticky.updated']({ data: { id: 'a', space_id: null, position_x: 9, position_y: 9 } })
    expect(householdStickyStore.find('a')).toMatchObject({ position_x: 9, position_y: 9 })
  })

  it('a failed PATCH rolls back and then revalidates the board', async () => {
    apiGet.mockResolvedValueOnce([row('a')])
    await householdStickyStore.load()
    apiPatch.mockRejectedValueOnce(new Error('nope'))
    apiGet.mockResolvedValueOnce([row('a', { content: 'server truth' })])
    await expect(householdStickyStore.patch('a', { content: 'mine' })).rejects.toThrow('nope')
    await flush()
    expect(apiGet).toHaveBeenCalledTimes(2)
    expect(householdStickyStore.find('a')?.content).toBe('server truth')
  })
})
