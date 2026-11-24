import { describe, it, expect, vi, beforeEach } from 'vitest'

const { apiMock } = vi.hoisted(() => ({ apiMock: { get: vi.fn() } }))
vi.mock('@/api', () => ({ api: apiMock }))

import { t } from '@/i18n/i18n'
import { currentUser } from '@/store/auth'
import type { Moment } from '@/types'
import {
  loadMomentLimits,
  momentReach,
  momentReachMax,
  myMomentReach,
  reachText,
  replyReach,
  shownReach,
} from './momentLimits'

describe('momentLimits — how far the caller\'s moments go', () => {
  beforeEach(() => {
    apiMock.get.mockReset()
    myMomentReach.value = null
    momentReachMax.value = null
    currentUser.value = null
  })

  it('reads reach and the household maximum from GET /api/moments/limits', async () => {
    apiMock.get.mockResolvedValue({ next_allowed_at: null, reach: 4, reach_max: 4 })
    const got = await loadMomentLimits()
    expect(apiMock.get).toHaveBeenCalledWith('/api/moments/limits')
    expect(got).toEqual({ next_allowed_at: null, reach: 4, reach_max: 4 })
    expect(myMomentReach.value).toBe(4)
    expect(momentReachMax.value).toBe(4)
  })

  it('a response without the fields leaves them unknown', async () => {
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

  it('shows 3 hops while the reach and the person are unknown', () => {
    expect(shownReach()).toBe(3)
    myMomentReach.value = 5
    expect(shownReach()).toBe(5)
  })

  it('falls back to the person’s own reach setting, never 3, while unread', () => {
    // An upper bound: the household maximum can only lower it.
    currentUser.value = {
      user_id: 'u', username: 'u', preferences_json: JSON.stringify({ moments: { reach: 5 } }),
    } as never
    expect(shownReach()).toBe(5)
    currentUser.value = {
      user_id: 'u', username: 'u', preferences_json: JSON.stringify({ moments: { reach: 1 } }),
    } as never
    expect(shownReach()).toBe(1)
    expect(replyReach({ reach: 4 })).toBe(1)
  })

  it('a protected account falls back to 1 while unread', () => {
    currentUser.value = {
      user_id: 'u', username: 'u', protected: true,
      preferences_json: JSON.stringify({ moments: { reach: 4 } }),
    } as never
    expect(shownReach()).toBe(1)
  })

  it('a reply goes no further than the moment it answers', () => {
    myMomentReach.value = 5
    expect(replyReach({ reach: 2 })).toBe(2)
    expect(replyReach(null)).toBe(3)
    myMomentReach.value = 1
    expect(replyReach({ reach: 4 })).toBe(1)
  })

  it.each([
    [1, 1], [3, 3], [5, 5],
    [0, 3], [6, 3], [2.5, 3], ['4', 3], [undefined, 3], [null, 3],
  ])('a moment with reach %j reads as %i hops', (reach, want) => {
    expect(momentReach({ reach } as unknown as Pick<Moment, 'reach'>)).toBe(want)
  })

  it('loads share one in-flight read', async () => {
    let resolve!: (v: unknown) => void
    apiMock.get.mockReturnValue(new Promise((r) => { resolve = r }))
    const a = loadMomentLimits()
    const b = loadMomentLimits()
    expect(apiMock.get).toHaveBeenCalledTimes(1)
    resolve({ next_allowed_at: null, reach: 2, reach_max: 4 })
    expect(await a).toEqual(await b)
    expect(myMomentReach.value).toBe(2)
    // Once settled, the next load reads again.
    apiMock.get.mockResolvedValue({ next_allowed_at: null, reach: 3, reach_max: 4 })
    await loadMomentLimits()
    expect(apiMock.get).toHaveBeenCalledTimes(2)
    expect(myMomentReach.value).toBe(3)
  })

  it('a fresh read after a save is not answered by an older read', async () => {
    let resolveOld!: (v: unknown) => void
    apiMock.get.mockReturnValueOnce(new Promise((r) => { resolveOld = r }))
    const old = loadMomentLimits()
    apiMock.get.mockResolvedValueOnce({ next_allowed_at: null, reach: 1, reach_max: 1 })
    await loadMomentLimits({ fresh: true })
    expect(apiMock.get).toHaveBeenCalledTimes(2)
    expect(myMomentReach.value).toBe(1)
    // The older read lands late: it must not put back what it saw.
    resolveOld({ next_allowed_at: null, reach: 5, reach_max: 5 })
    await old
    expect(myMomentReach.value).toBe(1)
    expect(momentReachMax.value).toBe(1)
  })

  it('reach 1 reads as its own sentence (the _one key)', () => {
    expect(reachText('moment.entry.reach', 1)).toBe(t('moment.entry.reach_one'))
    expect(reachText('moment.entry.reach', 2)).toBe(t('moment.entry.reach', { n: '2' }))
    expect(reachText('moment.entry.reach', 5)).toContain('5')
  })

  it('reach texts take extra parameters beside n', () => {
    expect(reachText('moment.composer.reach_more', 4, { days: '7 days' }))
      .toBe(t('moment.composer.reach_more', { n: '4', days: '7 days' }))
  })
})
