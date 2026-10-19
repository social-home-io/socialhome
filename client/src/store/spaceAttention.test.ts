/**
 * "Needs you": the space detail's ``attention`` becomes the store, the
 * More badge counts what Moderation leads to, and the page re-reads on
 * the frames of THIS space only.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'

const { handlers } = vi.hoisted(() => ({
  handlers: {} as Record<string, Array<(e: { type: string; data: unknown }) => void>>,
}))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { type: string; data: unknown }) => void) => {
      ;(handlers[type] ??= []).push(h)
      return () => { handlers[type] = handlers[type].filter((x) => x !== h) }
    },
  },
}))

import { renderHook } from '@testing-library/preact'
import {
  awaitsMyVote, needsYouTotal, setSpaceAttention, spaceAttention, useSpaceAttentionFrames,
} from './spaceAttention'

const ADMIN = { canVote: true, isOwner: false }

const fire = (type: string, spaceId: string) => {
  for (const h of handlers[type] ?? []) h({ type, data: { space_id: spaceId } })
}

beforeEach(() => {
  spaceAttention.value = null
  for (const k of Object.keys(handlers)) delete handlers[k]
})

describe('setSpaceAttention', () => {
  it('maps the wire counts; the total adds proposals awaiting my vote', () => {
    setSpaceAttention('s1', {
      pending_review: 2, reports: 1, join_requests: 3,
      proposals: [{ id: 'p1', action: 'dissolve', status: 'pending', approvals: 1, needed: 2,
                    total_admins: 2, proposed_by_user: 'u', required: 2, voted_by_me: false }],
    })
    expect(spaceAttention.value).toMatchObject({ spaceId: 's1', review: 2, reports: 1, joinRequests: 3 })
    expect(spaceAttention.value?.proposals).toHaveLength(1)
    // A proposal awaiting my vote counts: 2 + 1 + 3 + 1.
    expect(needsYouTotal(spaceAttention.value, 's1', ADMIN)).toBe(7)
    expect(needsYouTotal(spaceAttention.value, 's2', ADMIN)).toBe(0)
    // A moderator can't vote: it doesn't wait for them.
    expect(needsYouTotal(spaceAttention.value, 's1', { canVote: false, isOwner: false })).toBe(6)
  })

  it('null (a member, a follower) means nothing waits', () => {
    setSpaceAttention('s1', null)
    expect(spaceAttention.value).toEqual({ spaceId: 's1', review: 0, reports: 0, joinRequests: 0, proposals: [] })
    expect(needsYouTotal(spaceAttention.value, 's1', ADMIN)).toBe(0)
  })

  it('a proposal I made or already voted on does not count for me', () => {
    setSpaceAttention('s1', {
      pending_review: 2, reports: 1, join_requests: 0,
      proposals: [{ id: 'p1', action: 'dissolve', status: 'pending', approvals: 1, needed: 2,
                    total_admins: 2, proposed_by_user: 'u', required: 2, voted_by_me: true }],
    })
    expect(needsYouTotal(spaceAttention.value, 's1', ADMIN)).toBe(3)
  })
})

describe('awaitsMyVote', () => {
  it('settings authority, the owner for owner-only, and not yet voted', () => {
    expect(awaitsMyVote({}, true, false)).toBe(true)
    expect(awaitsMyVote({}, false, true)).toBe(false)
    expect(awaitsMyVote({ voted_by_me: true }, true, true)).toBe(false)
    expect(awaitsMyVote({ owner_only: true }, true, false)).toBe(false)
    expect(awaitsMyVote({ owner_only: true }, true, true)).toBe(true)
  })
})

describe('useSpaceAttentionFrames', () => {
  it('re-reads on a frame for this space only — proposals included', () => {
    const reload = vi.fn()
    renderHook(() => useSpaceAttentionFrames('s1', true, reload))
    fire('space.moderation.queued', 's2')
    expect(reload).not.toHaveBeenCalled()
    fire('space.join.requested', 's1')
    fire('space.proposal.updated', 's1')
    expect(reload).toHaveBeenCalledTimes(2)
  })

  it('listens to nothing for a viewer with nothing to decide', () => {
    const reload = vi.fn()
    renderHook(() => useSpaceAttentionFrames('s1', false, reload))
    fire('space.moderation.queued', 's1')
    expect(reload).not.toHaveBeenCalled()
  })
})
