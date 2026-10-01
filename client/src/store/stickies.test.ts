/**
 * The household sticky count the Organize hub shows. The ``stickies``
 * signal is shared by the household board and every space board, so
 * the count is kept apart: loaded from ``/api/stickies`` (deduped with
 * the household board's own load) and kept current by household-scoped
 * WS frames and local adds / deletes. (The per-scope sticky cache is
 * the next PR; this is only the count.)
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))

const handlers: Record<string, (e: { data: Record<string, unknown> }) => void> = {}
vi.mock('@/ws', () => ({
  ws: { on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => { handlers[type] = h } },
}))

import {
  householdStickyCount, loadHouseholdStickies, ensureHouseholdStickies,
  trackHouseholdSticky, resetHouseholdStickies, wireStickiesWs, stickies, activeStickyScope,
} from './stickies'

const row = (id: string, space_id: string | null = null) => ({
  id, author: 'u1', content: '', color: '#fff', position_x: 0, position_y: 0,
  created_at: '', updated_at: '', space_id,
})

beforeEach(() => {
  apiGet.mockReset()
  resetHouseholdStickies()
  stickies.value = []
  activeStickyScope.value = null
})

describe('household sticky count', () => {
  it('is null until loaded, then the household row count', async () => {
    apiGet.mockResolvedValue([row('a'), row('b')])
    expect(householdStickyCount.value).toBeNull()
    await ensureHouseholdStickies()
    expect(apiGet).toHaveBeenCalledWith('/api/stickies')
    expect(householdStickyCount.value).toBe(2)
  })

  it('concurrent loads share one request; ensure skips once loaded', async () => {
    let resolve!: (v: unknown) => void
    apiGet.mockReturnValue(new Promise(r => { resolve = r }))
    const a = ensureHouseholdStickies()
    const b = loadHouseholdStickies()
    resolve([row('a')])
    expect(await b).toEqual([row('a')])
    await a
    await ensureHouseholdStickies()
    expect(apiGet).toHaveBeenCalledTimes(1)
  })

  it('ignores a space board sitting in the shared stickies signal', async () => {
    apiGet.mockResolvedValue([row('a')])
    await ensureHouseholdStickies()
    stickies.value = [row('s1', 'space'), row('s2', 'space'), row('s3', 'space')]
    expect(householdStickyCount.value).toBe(1)
  })

  it('follows household WS frames whatever board is mounted, never space ones', async () => {
    wireStickiesWs()
    apiGet.mockResolvedValue([row('a')])
    await ensureHouseholdStickies()
    activeStickyScope.value = 'space'
    handlers['sticky.created']({ data: row('b') })
    handlers['sticky.created']({ data: row('b') })
    handlers['sticky.created']({ data: row('x', 'space') })
    expect(householdStickyCount.value).toBe(2)
    handlers['sticky.deleted']({ data: { id: 'a', space_id: null } })
    handlers['sticky.deleted']({ data: { id: 'x', space_id: 'space' } })
    expect(householdStickyCount.value).toBe(1)
  })

  it('tracks local adds / deletes', async () => {
    apiGet.mockResolvedValue([])
    await ensureHouseholdStickies()
    trackHouseholdSticky('n', true)
    expect(householdStickyCount.value).toBe(1)
    trackHouseholdSticky('n', false)
    expect(householdStickyCount.value).toBe(0)
  })

  it('a failed load leaves the count unknown', async () => {
    apiGet.mockRejectedValue(new Error('down'))
    await expect(ensureHouseholdStickies()).rejects.toThrow('down')
    expect(householdStickyCount.value).toBeNull()
  })
})
