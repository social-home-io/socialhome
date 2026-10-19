import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, waitFor, act } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({ api: { get: (...a: unknown[]) => apiGet(...a) } }))

import { quickLinks, useSpaceLinks } from './useSpaceLinks'

beforeEach(() => { apiGet.mockReset() })

describe('quickLinks', () => {
  it('keeps http(s) links and drops every other scheme', () => {
    expect(quickLinks([
      { id: 'a', label: 'Wiki', url: 'https://wiki.example/', position: 0 },
      { id: 'b', label: 'Evil', url: 'javascript:alert(1)', position: 1 },
      { id: 'c', label: 'Data', url: 'data:text/html,x', position: 2 },
      { id: 'd', label: 'Plain', url: 'http://school.example/plan', position: 3 },
    ])).toEqual([
      { id: 'a', label: 'Wiki', href: 'https://wiki.example/' },
      { id: 'd', label: 'Plain', href: 'http://school.example/plan' },
    ])
  })
})

describe('useSpaceLinks', () => {
  it('reads the space links', async () => {
    apiGet.mockResolvedValue({ links: [{ id: 'a', label: 'Wiki', url: 'https://wiki.example/', position: 0 }] })
    const { result } = renderHook(() => useSpaceLinks('sp-1'))
    await waitFor(() => expect(result.current).toHaveLength(1))
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/sp-1/links')
  })

  it('a failed read leaves no links', async () => {
    apiGet.mockRejectedValue(new Error('403'))
    const { result } = renderHook(() => useSpaceLinks('sp-1'))
    await act(async () => { await Promise.resolve() })
    expect(result.current).toEqual([])
  })
})
