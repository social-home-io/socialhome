import { describe, it, expect, vi, beforeEach } from 'vitest'

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
function emit(type: string, data: Record<string, unknown>) {
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}

import {
  spaces, activeSpace, wireSpacesWs,
  markLocalDissolve, clearLocalDissolve, isLocalDissolve,
} from './spaces'
import type { Space } from '@/types'

const row = (id: string, name: string) => ({ id, name } as unknown as Space)

describe('spaces store', () => {
  it('starts with empty spaces', () => {
    expect(spaces.value).toEqual([])
  })

  it('activeSpace starts null', () => {
    expect(activeSpace.value).toBe(null)
  })
})

describe('wireSpacesWs — space.config.changed', () => {
  beforeEach(() => {
    handlers.clear()
    apiGet.mockReset()
    spaces.value = [row('sp-1', 'Garden'), row('sp-2', 'Book club')]
    wireSpacesWs()
  })

  it('drops a dissolved space from the list without a refetch', () => {
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'dissolved' })
    expect(spaces.value.map(s => s.id)).toEqual(['sp-2'])
    expect(apiGet).not.toHaveBeenCalled()
  })

  it('refetches the list on another config change of a listed space', async () => {
    apiGet.mockResolvedValue([row('sp-1', 'Allotment'), row('sp-2', 'Book club')])
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'rename', sequence: 4 })
    expect(apiGet).toHaveBeenCalledWith('/api/spaces')
    await vi.waitFor(() => expect(spaces.value[0].name).toBe('Allotment'))
  })

  it('ignores a frame for a space not in the list, or without an id', () => {
    emit('space.config.changed', { space_id: 'sp-9', event_type: 'rename' })
    emit('space.config.changed', { event_type: 'dissolved' })
    expect(apiGet).not.toHaveBeenCalled()
    expect(spaces.value).toHaveLength(2)
  })
})

describe('local dissolve marker', () => {
  it('marks and clears a space this tab is dissolving', () => {
    expect(isLocalDissolve('sp-1')).toBe(false)
    markLocalDissolve('sp-1')
    expect(isLocalDissolve('sp-1')).toBe(true)
    clearLocalDissolve('sp-1')
    expect(isLocalDissolve('sp-1')).toBe(false)
  })
})
