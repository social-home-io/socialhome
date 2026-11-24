import { describe, it, expect, vi, beforeEach } from 'vitest'

const { apiMock } = vi.hoisted(() => ({ apiMock: { get: vi.fn() } }))
vi.mock('@/api', () => ({ api: apiMock }))

import {
  loadMomentLimits,
  momentReachMax,
  myMomentReach,
  reachKey,
  replyReach,
  shownReach,
} from './momentLimits'

describe('momentLimits — how far the caller\'s moments go', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
    myMomentReach.value = null
    momentReachMax.value = null
  })

  it('reads reach and the household maximum from GET /api/moments/limits', async () => {
    apiMock.get.mockResolvedValue({ next_allowed_at: null, reach: 4, reach_max: 4 })
    const got = await loadMomentLimits()
    expect(apiMock.get).toHaveBeenCalledWith('/api/moments/limits')
    expect(got).toEqual({ next_allowed_at: null, reach: 4, reach_max: 4 })
    expect(myMomentReach.value).toBe(4)
    expect(momentReachMax.value).toBe(4)
  })

  it('an older server without the fields leaves them unknown', async () => {
    apiMock.get.mockResolvedValue({ next_allowed_at: null })
    await loadMomentLimits()
    expect(myMomentReach.value).toBeNull()
    expect(momentReachMax.value).toBeNull()
  })

  it('a failed read returns null and keeps what was known', async () => {
    myMomentReach.value = 2
    apiMock.get.mockRejectedValue(new Error('offline'))
    expect(await loadMomentLimits()).toBeNull()
    expect(myMomentReach.value).toBe(2)
  })

  it('shows 3 hops while the reach is unknown', () => {
    expect(shownReach()).toBe(3)
    myMomentReach.value = 5
    expect(shownReach()).toBe(5)
  })

  it('a reply goes no further than the moment it answers', () => {
    myMomentReach.value = 5
    expect(replyReach(2)).toBe(2)
    expect(replyReach(undefined)).toBe(3)
    myMomentReach.value = 1
    expect(replyReach(4)).toBe(1)
  })

  it('reach 1 reads as its own sentence (the _one key)', () => {
    expect(reachKey('moment.entry.reach', 1)).toBe('moment.entry.reach_one')
    expect(reachKey('moment.entry.reach', 2)).toBe('moment.entry.reach')
    expect(reachKey('moment.entry.reach', 5)).toBe('moment.entry.reach')
  })
})
