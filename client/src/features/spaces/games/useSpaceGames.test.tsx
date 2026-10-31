/**
 * useSpaceGames: reads the games once on, re-reads (debounced) on a
 * ``space.app_table`` frame for this space only, keeps the list on a
 * failed live re-read, and reads nothing while off (a follower).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { act, render } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))
type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))

import { normalizeGames, useSpaceGames, type SpaceGamesState } from './useSpaceGames'

let latest: SpaceGamesState | null = null
function Probe({ enabled, space = 'sp-1' }: { enabled: boolean, space?: string }) {
  latest = useSpaceGames(space, enabled)
  return null
}

const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve() })

beforeEach(() => {
  vi.useFakeTimers()
  handlers.clear()
  apiGet.mockReset()
  latest = null
})
afterEach(() => { vi.useRealTimers() })

describe('useSpaceGames', () => {
  it('reads once and re-reads once for a burst of frames for this space', async () => {
    apiGet.mockResolvedValue({ allowed_apps: ['quiz'], tables: [] })
    render(<Probe enabled />)
    await flush()
    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(latest?.games?.allowed_apps).toEqual(['quiz'])
    const fire = (space: string) => handlers.get('space.app_table')?.forEach(
      (h) => h({ type: 'space.app_table', data: { space_id: space, table: {} } }))
    fire('sp-1'); fire('sp-1'); fire('other')
    await act(async () => { vi.advanceTimersByTime(400) })
    await flush()
    expect(apiGet).toHaveBeenCalledTimes(2)
  })

  it('a failed live re-read keeps the list; a failed first read says so', async () => {
    apiGet.mockResolvedValueOnce({ tables: [{ session_id: 'a' }] }).mockRejectedValueOnce(new Error('x'))
    render(<Probe enabled />)
    await flush()
    await act(async () => { await latest!.reload() })
    expect(latest?.games?.tables).toHaveLength(1)
    expect(latest?.failed).toBe(false)
    apiGet.mockRejectedValueOnce(new Error('x'))
    render(<Probe enabled />)
    await flush()
    expect(latest?.failed).toBe(true)
  })

  it('another space never shows the last one’s games while its own load', async () => {
    let slow!: (v: unknown) => void
    apiGet.mockImplementationOnce(async () => ({ space_id: 'sp-1', tables: [{ session_id: 'a' }] }))
      .mockImplementationOnce(() => new Promise((res) => { slow = res }))
    const v = render(<Probe enabled />)
    await flush()
    expect(latest?.games?.tables).toHaveLength(1)
    v.rerender(<Probe enabled space="sp-2" />)
    await flush()
    expect(latest?.games).toBeNull()
    expect(latest?.loading).toBe(true)
    slow({ space_id: 'sp-2', tables: [] })
    await flush()
    expect(latest?.games?.space_id).toBe('sp-2')
  })

  it('off: reads nothing, listens to nothing', async () => {
    render(<Probe enabled={false} />)
    await flush()
    expect(apiGet).not.toHaveBeenCalled()
    expect(handlers.get('space.app_table')?.size ?? 0).toBe(0)
    expect(latest?.loading).toBe(false)
  })

  it('normalizeGames: a missing list reads as none', () => {
    expect(normalizeGames('sp-1', null)).toEqual({
      space_id: 'sp-1', allowed_apps: [], games_possible: true, can_edit_games: false,
      host_supports: true, tables: [],
    })
  })
})
