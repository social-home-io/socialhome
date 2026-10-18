import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))

import { sectionMeta, useSpaceSummary, type SpaceSummary } from './spaceSummary'

const full: SpaceSummary = {
  tasks: { open: 5, mine: 2 },
  pages: { count: 3 },
  gallery: { albums: 2, items: 5 },
  stickies: { count: 1 },
  bazaar: { active: 4 },
  calendar: { upcoming: 3, days: 30 },
}

const empty: SpaceSummary = {
  tasks: { open: 0, mine: 0 },
  pages: { count: 0 },
  gallery: { albums: 0, items: 0 },
  stickies: { count: 0 },
  bazaar: { active: 0 },
  calendar: { upcoming: 0, days: 30 },
}

describe('sectionMeta', () => {
  it('says what each section holds', () => {
    expect(sectionMeta('tasks', full)).toBe('5 open · 2 yours')
    expect(sectionMeta('pages', full)).toBe('3 pages')
    expect(sectionMeta('gallery', full)).toBe('2 albums · 5 photos')
    expect(sectionMeta('stickies', full)).toBe('1 note')
    expect(sectionMeta('bazaar', full)).toBe('4 for sale')
    expect(sectionMeta('calendar', full)).toBe('3 coming up')
  })

  it('no task of mine → just the open count; singulars', () => {
    expect(sectionMeta('tasks', { ...full, tasks: { open: 1, mine: 0 } })).toBe('1 open')
    expect(sectionMeta('gallery', { ...full, gallery: { albums: 1, items: 1 } })).toBe('1 album · 1 photo')
    expect(sectionMeta('pages', { ...full, pages: { count: 1 } })).toBe('1 page')
  })

  it('an empty section says so', () => {
    expect(sectionMeta('tasks', empty)).toBe('Nothing open')
    expect(sectionMeta('pages', empty)).toBe('No pages yet')
    expect(sectionMeta('gallery', empty)).toBe('No albums yet')
    expect(sectionMeta('stickies', empty)).toBe('No notes yet')
    expect(sectionMeta('bazaar', empty)).toBe('Nothing for sale')
    expect(sectionMeta('calendar', empty)).toBe('Nothing coming up')
  })

  it('no line for a section that is off, one without counts, or an unknown summary', () => {
    expect(sectionMeta('bazaar', { ...full, bazaar: null })).toBeNull()
    expect(sectionMeta('map', full)).toBeNull()
    expect(sectionMeta('chat', full)).toBeNull()
    expect(sectionMeta('tasks', null)).toBeNull()
  })
})

describe('useSpaceSummary', () => {
  beforeEach(() => { apiGet.mockReset() })

  it('reads only while enabled', async () => {
    apiGet.mockResolvedValue(full)
    const { result, rerender } = renderHook(({ on }) => useSpaceSummary('s1', on), {
      initialProps: { on: false },
    })
    await act(async () => { await Promise.resolve() })
    expect(apiGet).not.toHaveBeenCalled()
    rerender({ on: true })
    await waitFor(() => expect(result.current).toEqual(full))
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/summary')
  })

  it('a failed read leaves no summary', async () => {
    apiGet.mockRejectedValue(new Error('offline'))
    const { result } = renderHook(() => useSpaceSummary('s1', true))
    await act(async () => { await Promise.resolve() })
    expect(result.current).toBeNull()
  })
})
