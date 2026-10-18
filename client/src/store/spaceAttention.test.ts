/**
 * "Needs you" counts: read from the endpoints the Moderation section
 * already uses, only for the roles that act on them, and kept live on
 * the moderation / join-request frames of THIS space.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { apiGet, handlers } = vi.hoisted(() => ({
  apiGet: vi.fn(),
  handlers: {} as Record<string, Array<(e: { type: string; data: unknown }) => void>>,
}))
vi.mock('@/api', () => ({ api: { get: apiGet } }))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { type: string; data: unknown }) => void) => {
      ;(handlers[type] ??= []).push(h)
      return () => { handlers[type] = handlers[type].filter((x) => x !== h) }
    },
  },
}))

import { renderHook, waitFor } from '@testing-library/preact'
import { attentionTotal, loadSpaceAttention, spaceAttention, useSpaceAttention } from './spaceAttention'

beforeEach(() => {
  apiGet.mockReset()
  spaceAttention.value = null
  for (const k of Object.keys(handlers)) delete handlers[k]
  apiGet.mockImplementation(async (url: string) => {
    if (url.endsWith('/moderation')) return [{}, {}]
    if (url.endsWith('/reports')) return [{}]
    if (url.endsWith('/join-requests')) return [{}, {}, {}]
    return []
  })
})

describe('loadSpaceAttention', () => {
  it('an admin counts held items, reports and join requests', async () => {
    await loadSpaceAttention('s1', true, true)
    expect(spaceAttention.value).toEqual({ spaceId: 's1', review: 2, reports: 1, joinRequests: 3 })
    expect(attentionTotal(spaceAttention.value, 's1')).toBe(6)
    expect(attentionTotal(spaceAttention.value, 's2')).toBe(0)
  })

  it('a moderator never asks for join requests', async () => {
    await loadSpaceAttention('s1', true, false)
    expect(spaceAttention.value?.joinRequests).toBe(0)
    expect(apiGet).not.toHaveBeenCalledWith('/api/spaces/s1/join-requests')
  })

  it('a member asks for nothing', async () => {
    await loadSpaceAttention('s1', false, false)
    expect(apiGet).not.toHaveBeenCalled()
    expect(attentionTotal(spaceAttention.value, 's1')).toBe(0)
  })

  it('a failed read counts as zero', async () => {
    apiGet.mockRejectedValue(new Error('offline'))
    await loadSpaceAttention('s1', true, true)
    expect(attentionTotal(spaceAttention.value, 's1')).toBe(0)
  })
})

describe('useSpaceAttention', () => {
  it('re-reads on a frame for this space only', async () => {
    renderHook(() => useSpaceAttention('s1', true, true))
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(3))
    for (const h of handlers['space.moderation.queued'] ?? []) h({ type: 'space.moderation.queued', data: { space_id: 's2' } })
    expect(apiGet).toHaveBeenCalledTimes(3)
    for (const h of handlers['space.join.requested'] ?? []) h({ type: 'space.join.requested', data: { space_id: 's1' } })
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(6))
  })
})
