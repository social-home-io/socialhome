import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
  },
}))

import { toasts } from '@/components/Toast'
import type { Notification } from '@/types'
import {
  DECLINE_UNDO_MS,
  NEEDS_YOU_TYPES,
  acceptContact,
  acting,
  approve,
  declineWithUndo,
  dropHandled,
  flagsOf,
  flushDeclines,
  inlineAction,
  mustReview,
  loadNeedsYou,
  needsYou,
  needsYouEntries,
  reviewHref,
  waitingDeclines,
} from './needsYou'

function row(over: Partial<Notification> & { id: string }): Notification {
  return {
    type: 'space_join_requested',
    title: 't',
    body: null,
    link_url: '/spaces/s1/moderation',
    read_at: null,
    created_at: '2026-10-28T10:00:00+00:00',
    object_key: `join_request:${over.id}`,
    space_id: 's1',
    params: { actors: [{ id: 'kim', name: 'Kim' }], actor_count: 1, new: 1, total: 1 },
    ...over,
  } as Notification
}

beforeEach(() => {
  apiGet.mockReset().mockResolvedValue({ items: [], count: 0 })
  apiPost.mockReset().mockResolvedValue({})
  needsYou.value = []
  acting.value = new Set()
  mustReview.value = new Map()
  toasts.value = []
})

afterEach(() => {
  vi.useRealTimers()
})

describe('needsYouEntries', () => {
  it('groups two or more items of one space, newest first, and hides acted-on items', () => {
    const rows = [
      row({ id: 'r1', space_id: 'a' }),
      row({ id: 'r2', space_id: 'b' }),
      row({ id: 'r3', space_id: 'a', type: 'moderation_pending', object_key: 'space:a' }),
      row({ id: 'p1', space_id: null, type: 'pairing_request', object_key: 'pairing_request:p1' }),
    ]
    const entries = needsYouEntries(rows)
    expect(entries.map(e => e.key)).toEqual(['space:a', 'join_request:r2', 'pairing_request:p1'])
    const group = entries[0]
    expect(group.kind === 'group' && group.rows.map(r => r.id)).toEqual(['r1', 'r3'])
    // Acting on r1 leaves a's other item on its own.
    const rest = needsYouEntries(rows, new Set(['join_request:r1']))
    // (r3's own key is ``space:a`` — the review queue row of space a.)
    expect(rest.map(e => e.key)).toEqual(['join_request:r2', 'space:a', 'pairing_request:p1'])
    expect(rest[1].kind === 'single' && rest[1].row.id).toBe('r3')
  })
})

describe('inlineAction: one tap only when it is safe', () => {
  it('a plain join request is decided inline', () => {
    expect(inlineAction(row({ id: 'r1' }))).toBe('decide')
  })

  it.each([['edge'], ['unpaired'], ['admin'], ['age']])('a request flagged %s goes to Review', (flag) => {
    const r = row({ id: 'r1' })
    r.params = { ...r.params!, flags: [flag] }
    expect(inlineAction(r)).toBe('review')
  })

  it('an older space-wide request row, a review queue and a pairing request go to Review', () => {
    expect(inlineAction(row({ id: 'r1', object_key: 'space:s1' }))).toBe('review')
    expect(inlineAction(row({ id: 'm', type: 'moderation_pending', object_key: 'space:s1' }))).toBe('review')
    expect(inlineAction(row({ id: 'p', type: 'pairing_request', object_key: 'pairing_request:p', space_id: null }))).toBe('review')
  })

  it('an invite is viewed, never joined in one tap', () => {
    expect(inlineAction(row({ id: 'i', type: 'space_invite', object_key: 'space_invite:s1' }))).toBe('invite')
  })

  it('Review opens the row, a group its space\'s review queue', () => {
    const r = row({ id: 'p', type: 'pairing_request', link_url: '/connections', space_id: null })
    expect(reviewHref({ kind: 'single', key: 'k', row: r })).toBe('/connections')
    expect(reviewHref({ kind: 'group', key: 'g', spaceId: 's9', rows: [row({ id: 'x', link_url: null })] }))
      .toBe('/spaces/s9/moderation')
  })
})

describe('actions', () => {
  it('approve posts at once and drops the item', async () => {
    needsYou.value = [row({ id: 'r1' })]
    await approve(needsYou.value[0])
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/join-requests/r1/approve', { via: 'inline' })
    expect(needsYou.value).toEqual([])
    expect(acting.value.size).toBe(0)
    expect(toasts.value.map(t => t.message)).toContain('Approved')
  })

  it('a refused one-tap approval (REVIEW_REQUIRED) turns the item into Review with the server\'s reasons', async () => {
    const r1 = row({ id: 'r1' })
    needsYou.value = [r1]
    // The list reloads with the row's own (stale, empty) flags.
    apiGet.mockResolvedValue({ items: [r1], count: 1 })
    apiPost.mockRejectedValueOnce(Object.assign(new Error('x'), {
      status: 409, code: 'REVIEW_REQUIRED', extra: { params: { flags: 'age,edge' } },
    }))
    await approve(r1)
    expect(needsYou.value.map(n => n.id)).toEqual(['r1'])
    expect(acting.value.size).toBe(0)
    expect(flagsOf(r1)).toEqual(['age', 'edge'])
    expect(inlineAction(needsYou.value[0])).toBe('review')
    const shown = toasts.value.map(t => [t.message, t.type])
    expect(shown).toContainEqual(['Have a closer look at this request first', 'info'])
    // Not read as a lost race, nor as an error.
    expect(shown.map(([, type]) => type)).not.toContain('error')
    expect(shown.map(([m]) => m)).not.toContain('Someone already handled this')
    // Once someone handles it (on the review page), the override goes too.
    dropHandled(['join_request:r1'])
    expect(mustReview.value.size).toBe(0)
  })

  it('REVIEW_REQUIRED without named flags still never offers one tap again', async () => {
    const r1 = row({ id: 'r1' })
    needsYou.value = [r1]
    apiPost.mockRejectedValueOnce(Object.assign(new Error('x'), { status: 409, code: 'REVIEW_REQUIRED' }))
    await approve(r1)
    expect(inlineAction(r1)).toBe('review')
  })

  it('a tap that lost the race hears who won, not an error', async () => {
    needsYou.value = [row({ id: 'r1' })]
    apiPost.mockRejectedValueOnce(Object.assign(new Error('x'), { status: 409, extra: { params: { by: 'Søren' } } }))
    await approve(needsYou.value[0])
    expect(toasts.value.map(t => [t.message, t.type])).toContainEqual(['Søren already handled this', 'info'])
    expect(needsYou.value).toEqual([])
  })

  it('a failure keeps the item and says so', async () => {
    needsYou.value = [row({ id: 'r1' })]
    apiGet.mockResolvedValue({ items: needsYou.value, count: 1 })
    apiPost.mockRejectedValueOnce(Object.assign(new Error('boom'), { status: 500 }))
    await approve(needsYou.value[0])
    expect(toasts.value.map(t => t.type)).toContain('error')
    expect(needsYou.value.map(n => n.id)).toEqual(['r1'])
    expect(acting.value.size).toBe(0)
  })

  it('decline waits out its undo window before it is sent', async () => {
    vi.useFakeTimers()
    const r = row({ id: 'r1' })
    needsYou.value = [r]
    declineWithUndo(r)
    expect(acting.value.has('join_request:r1')).toBe(true) // gone from the list
    expect(apiPost).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(DECLINE_UNDO_MS - 100)
    expect(apiPost).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(200)
    expect(apiPost).toHaveBeenCalledWith('/api/spaces/s1/join-requests/r1/deny', undefined, { keepalive: true })
  })

  it('Undo keeps the request and sends nothing', async () => {
    vi.useFakeTimers()
    const r = row({ id: 'r1' })
    needsYou.value = [r]
    declineWithUndo(r)
    const toast = toasts.value.find(t => t.action)!
    expect(toast.action!.label).toBe('Undo')
    toast.action!.onClick()
    await vi.advanceTimersByTimeAsync(DECLINE_UNDO_MS * 2)
    expect(apiPost).not.toHaveBeenCalled()
    expect(acting.value.size).toBe(0)
    expect(waitingDeclines()).toBe(0)
  })

  it('leaving the page sends a waiting decline at once', async () => {
    const r = row({ id: 'r1' })
    declineWithUndo(r)
    declineWithUndo(r) // a second tap starts nothing new
    expect(waitingDeclines()).toBe(1)
    flushDeclines()
    await vi.waitFor(() => expect(apiPost).toHaveBeenCalledTimes(1))
    expect(waitingDeclines()).toBe(0)
    expect(toasts.value.some(t => t.action)).toBe(false) // nothing left to undo
  })

  it('declining an invite finds it by its space — this household\'s or another\'s', async () => {
    const local = row({ id: 'i1', type: 'space_invite', object_key: 'space_invite:sp-l', space_id: 'sp-l' })
    const remote = row({ id: 'i2', type: 'space_invite', object_key: 'space_invite:sp-r', space_id: 'sp-r' })
    apiGet.mockImplementation(async (path: string) => {
      if (path === '/api/local_invites') return [{ invitation_id: 'inv-1', space_id: 'sp-l' }]
      if (path === '/api/remote_invites') return [{ invite_token: 'tok/2', space_id: 'sp-r' }]
      return { items: [], count: 0 }
    })
    declineWithUndo(local)
    declineWithUndo(remote)
    flushDeclines()
    await vi.waitFor(() => expect(apiPost).toHaveBeenCalledTimes(2))
    expect(apiPost).toHaveBeenCalledWith('/api/local_invites/inv-1/decline')
    expect(apiPost).toHaveBeenCalledWith('/api/remote_invites/tok%2F2/decline')
    expect(toasts.value.map(t => t.message)).toContain('Invite declined')
  })

  it('an invite that is gone already reads as handled', async () => {
    apiGet.mockImplementation(async () => [])
    const inv = row({ id: 'i3', type: 'space_invite', object_key: 'space_invite:sp-x' })
    declineWithUndo(inv)
    flushDeclines()
    await vi.waitFor(() => expect(toasts.value.map(t => t.message)).toContain('Someone already handled this'))
    expect(apiPost).not.toHaveBeenCalled()
  })
})

describe('requests to message you and game invites', () => {
  const contact = () => row({
    id: 'c1',
    type: 'dm_contact_requested',
    object_key: 'contact:ru-ben/x',
    space_id: null,
    link_url: '/dms',
  })
  const game = () => row({
    id: 'g1',
    type: 'app_challenge',
    object_key: 'app_session:chess/sess-1',
    space_id: null,
    link_url: '/apps/chess',
  })

  it('a request to message you is pinned and answered inline', () => {
    expect(NEEDS_YOU_TYPES.has('dm_contact_requested')).toBe(true)
    expect(inlineAction(contact())).toBe('contact')
    // A row without its object (before 0129) goes to Review.
    expect(inlineAction(row({ id: 'c0', type: 'dm_contact_requested', object_key: null }))).toBe('review')
  })

  it('a game invite is played or declined inline', () => {
    expect(NEEDS_YOU_TYPES.has('app_challenge')).toBe(true)
    expect(inlineAction(game())).toBe('game')
    // One row per app from before one row per invite: Review (the app).
    expect(inlineAction(row({ id: 'g0', type: 'app_challenge', object_key: 'app:chess' }))).toBe('review')
  })

  it('Accept opens the chat the server answers with', async () => {
    apiPost.mockResolvedValueOnce({ id: 'conv-9', type: 'dm' })
    const c = contact()
    needsYou.value = [c]
    expect(await acceptContact(c)).toBe('conv-9')
    expect(apiPost).toHaveBeenCalledWith('/api/conversations/contact-requests/ru-ben%2Fx/accept')
    expect(needsYou.value).toEqual([])
    expect(toasts.value.map(t => t.message)).toContain('Request accepted')
  })

  it('an Accept that lost the race opens nothing', async () => {
    apiPost.mockRejectedValueOnce(Object.assign(new Error('409'), { status: 409, extra: { params: { status: 'declined' } } }))
    const c = contact()
    needsYou.value = [c]
    expect(await acceptContact(c)).toBeNull()
    expect(toasts.value.map(t => t.message)).toContain('Someone already handled this')
  })

  it('declining a request to message you waits 5 s and then says they are not told', async () => {
    vi.useFakeTimers()
    declineWithUndo(contact())
    expect(toasts.value.map(t => t.message)).toContain('Declining the request…')
    await vi.advanceTimersByTimeAsync(DECLINE_UNDO_MS + 100)
    expect(apiPost).toHaveBeenCalledWith(
      '/api/conversations/contact-requests/ru-ben%2Fx/decline', undefined, { keepalive: true },
    )
    expect(toasts.value.map(t => t.message)).toContain('Declined. They aren\'t told.')
  })

  it('declining a game invite names the app and the session', async () => {
    declineWithUndo(game())
    expect(toasts.value.map(t => t.message)).toContain('Declining the game…')
    flushDeclines()
    await vi.waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/apps/chess/sessions/sess-1/decline', undefined, { keepalive: true },
    ))
    await vi.waitFor(() => expect(toasts.value.map(t => t.message)).toContain('Game declined'))
  })
})

describe('loading', () => {
  it('loads the pending items and keeps them on a failed load', async () => {
    apiGet.mockResolvedValueOnce({ items: [row({ id: 'r1' })], count: 1 })
    await loadNeedsYou()
    expect(needsYou.value.map(n => n.id)).toEqual(['r1'])
    apiGet.mockRejectedValueOnce(new Error('offline'))
    await loadNeedsYou()
    expect(needsYou.value.map(n => n.id)).toEqual(['r1'])
    apiGet.mockResolvedValueOnce(null)
    await loadNeedsYou()
    expect(needsYou.value).toEqual([])
  })

  it('an item someone else handled leaves the list', () => {
    needsYou.value = [row({ id: 'r1' }), row({ id: 'r2' })]
    dropHandled(['join_request:r1'])
    expect(needsYou.value.map(n => n.id)).toEqual(['r2'])
    dropHandled([])
    expect(needsYou.value.map(n => n.id)).toEqual(['r2'])
  })
})
