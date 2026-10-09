import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: { get: (...args: unknown[]) => apiGet(...args) },
}))

import {
  applySpaceChatUnreadFrame,
  clearSpaceChatUnread,
  isSpaceChatFrame,
  loadSpaceChatUnread,
  spaceChatDeleteNeedsReload,
  loadSpaceChat,
  patchSpaceChat,
  resetSpaceChat,
  spaceChat,
  spaceChatError,
  spaceChatOf,
  spaceChatUnread,
  spaceChatUnreadOf,
  spaceFeedViewFromQuery,
  spaceFeedViews,
} from './spaceChat'

const SUMMARY = {
  enabled: true, conversation_id: 'sc-1', unread: 2,
  notif_level: 'mentions', muted_until: null, last_read_at: '2026-10-08 12:00:00',
}

beforeEach(() => {
  apiGet.mockReset()
  resetSpaceChat(null)
})

describe('loadSpaceChat', () => {
  it('stores the summary of GET /api/spaces/{id}/chat for that space', async () => {
    apiGet.mockResolvedValue(SUMMARY)
    resetSpaceChat('s1')
    await loadSpaceChat('s1')
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/chat')
    expect(spaceChatOf('s1')).toEqual(SUMMARY)
    expect(spaceChatOf('s2')).toBeNull()
    expect(spaceChatError.value).toBeNull()
  })

  it('drops an answer for a space the page already left', async () => {
    let answer!: (v: unknown) => void
    apiGet.mockReturnValue(new Promise((res) => { answer = res }))
    const pending = loadSpaceChat('s1')
    resetSpaceChat('s2')
    answer(SUMMARY)
    await pending
    expect(spaceChat.value).toBeNull()
    expect(spaceChatOf('s1')).toBeNull()
  })

  it('a failure names the space and keeps the previous summary', async () => {
    apiGet.mockResolvedValueOnce(SUMMARY)
    await loadSpaceChat('s1')
    apiGet.mockRejectedValueOnce(new Error('offline'))
    await loadSpaceChat('s1')
    expect(spaceChatError.value).toBe('s1')
    expect(spaceChatOf('s1')?.conversation_id).toBe('sc-1')
    resetSpaceChat('s1')
    expect(spaceChatError.value).toBeNull()
  })

  it('patchSpaceChat merges into a loaded summary only', async () => {
    patchSpaceChat({ unread: 5 })
    expect(spaceChat.value).toBeNull()
    apiGet.mockResolvedValue(SUMMARY)
    await loadSpaceChat('s1')
    patchSpaceChat({ unread: 0 })
    expect(spaceChatOf('s1')?.unread).toBe(0)
  })
})

describe('isSpaceChatFrame', () => {
  it('matches only this space\'s chat frames', () => {
    expect(isSpaceChatFrame({ system_scope: 'space', space_id: 's1' }, 's1')).toBe(true)
    expect(isSpaceChatFrame({ system_scope: 'space', space_id: 's2' }, 's1')).toBe(false)
    expect(isSpaceChatFrame({ system_scope: 'household', space_id: null }, 's1')).toBe(false)
    expect(isSpaceChatFrame({ system_scope: null }, 's1')).toBe(false)
    expect(isSpaceChatFrame(null, 's1')).toBe(false)
  })
})

describe('spaceFeedViews', () => {
  it('chat for a writer while the feature is on and the server agrees', () => {
    for (const role of ['owner', 'admin', 'moderator', 'member'] as const) {
      expect(spaceFeedViews({}, role, null)).toEqual(['posts', 'chat'])
      expect(spaceFeedViews({ chat: true }, role, { enabled: true })).toEqual(['posts', 'chat'])
    }
  })

  it('posts only for a follower, a non-member, chat off or a server "off"', () => {
    expect(spaceFeedViews({}, 'subscriber', null)).toEqual(['posts'])
    expect(spaceFeedViews({}, undefined, null)).toEqual(['posts'])
    expect(spaceFeedViews({ chat: false }, 'owner', { enabled: true })).toEqual(['posts'])
    expect(spaceFeedViews(undefined, 'member', { enabled: false })).toEqual(['posts'])
  })

  it('?view= parsing', () => {
    expect(spaceFeedViewFromQuery('chat')).toBe('chat')
    expect(spaceFeedViewFromQuery('posts')).toBe('posts')
    expect(spaceFeedViewFromQuery(undefined)).toBe('posts')
    expect(spaceFeedViewFromQuery(['chat'])).toBe('posts')
  })
})

describe('space chat unread for the spaces list', () => {
  const FOREVER = '9999-12-31T23:59:59+00:00'
  const frame = (over: Record<string, unknown> = {}) => ({
    system_scope: 'space', space_id: 'sA', mentions_you: false,
    message: { sender_user_id: 'u-other' }, ...over,
  })

  beforeEach(() => { spaceChatUnread.value = {} })

  it('loads one map for every space and parses it defensively', async () => {
    apiGet.mockResolvedValueOnce({
      spaces: {
        sA: { unread: 3, notif_level: 'all', muted_until: null },
        sB: { unread: -2, notif_level: 'weird', muted_until: 7 },
        sC: 'junk',
      },
    })
    await loadSpaceChatUnread()
    expect(apiGet).toHaveBeenCalledWith('/api/spaces/chat-unread')
    expect(spaceChatUnread.value).toEqual({
      sA: { unread: 3, notif_level: 'all', muted_until: null },
      sB: { unread: 0, notif_level: 'mentions', muted_until: null },
    })
    expect(spaceChatUnreadOf('sA')).toBe(3)
    expect(spaceChatUnreadOf('nowhere')).toBe(0)
  })

  it('a failed load keeps what the list shows', async () => {
    spaceChatUnread.value = { sA: { unread: 1, notif_level: 'all', muted_until: null } }
    apiGet.mockRejectedValueOnce(new Error('offline'))
    await loadSpaceChatUnread()
    expect(spaceChatUnreadOf('sA')).toBe(1)
  })

  it('a muted chat shows no count', () => {
    spaceChatUnread.value = { sA: { unread: 4, notif_level: 'all', muted_until: FOREVER } }
    expect(spaceChatUnreadOf('sA')).toBe(0)
  })

  it('live frames count by level and mute; own and non-space frames do not', () => {
    spaceChatUnread.value = {
      sA: { unread: 0, notif_level: 'mentions', muted_until: null },
      sB: { unread: 0, notif_level: 'all', muted_until: null },
      sM: { unread: 0, notif_level: 'all', muted_until: FOREVER },
    }
    expect(applySpaceChatUnreadFrame(frame(), 'u1')).toBe('ignored')
    expect(applySpaceChatUnreadFrame(frame({ mentions_you: true }), 'u1')).toBe('bumped')
    expect(applySpaceChatUnreadFrame(frame({ space_id: 'sB' }), 'u1')).toBe('bumped')
    expect(applySpaceChatUnreadFrame(frame({ space_id: 'sM', mentions_you: true }), 'u1')).toBe('ignored')
    expect(applySpaceChatUnreadFrame(
      frame({ space_id: 'sB', message: { sender_user_id: 'u1' } }), 'u1',
    )).toBe('ignored')
    expect(applySpaceChatUnreadFrame({ system_scope: 'household' }, 'u1')).toBe('ignored')
    expect(applySpaceChatUnreadFrame(null, 'u1')).toBe('ignored')
    expect(applySpaceChatUnreadFrame(frame({ space_id: 'sNew' }), 'u1')).toBe('unknown')
    expect(spaceChatUnreadOf('sA')).toBe(1)
    expect(spaceChatUnreadOf('sB')).toBe(1)
    clearSpaceChatUnread('sA')
    expect(spaceChatUnreadOf('sA')).toBe(0)
    clearSpaceChatUnread('nowhere')
    expect(Object.keys(spaceChatUnread.value).sort()).toEqual(['sA', 'sB', 'sM'])
  })

  it("the open space's summary keeps its row current", async () => {
    resetSpaceChat('sA')
    apiGet.mockResolvedValueOnce({ ...SUMMARY, unread: 2, notif_level: 'all' })
    await loadSpaceChat('sA')
    expect(spaceChatUnread.value.sA).toEqual({ unread: 2, notif_level: 'all', muted_until: null })
    patchSpaceChat({ muted_until: FOREVER })
    expect(spaceChatUnread.value.sA).toEqual({ unread: 0, notif_level: 'all', muted_until: FOREVER })
    // The chat turned off: the row goes.
    apiGet.mockResolvedValueOnce({ enabled: false })
    await loadSpaceChat('sA')
    expect(spaceChatUnread.value.sA).toBeUndefined()
  })
})

describe('space chat unread — coalesced loads and deletes', () => {
  const ROW = (unread: number) => ({ unread, notif_level: 'all', muted_until: null })
  function deferred<T>() {
    let resolve!: (v: T) => void
    const promise = new Promise<T>((r) => { resolve = r })
    return { promise, resolve }
  }
  const calls = () => apiGet.mock.calls.filter(c => c[0] === '/api/spaces/chat-unread').length

  beforeEach(() => { spaceChatUnread.value = {} })

  it('concurrent loads share one request', async () => {
    const d = deferred<unknown>()
    apiGet.mockReturnValueOnce(d.promise)
    const a = loadSpaceChatUnread()
    const b = loadSpaceChatUnread()
    // A second caller joins the request in flight: one GET so far.
    expect(calls()).toBe(1)
    apiGet.mockResolvedValueOnce({ spaces: { sA: ROW(1) } })
    d.resolve({ spaces: { sA: ROW(1) } })
    await Promise.all([a, b])
    // …and asked for one fresh read after it, never a third.
    expect(calls()).toBe(2)
    expect(spaceChatUnreadOf('sA')).toBe(1)
  })

  it('a bump during a load is not lost: one more read reconciles', async () => {
    spaceChatUnread.value = { sA: ROW(0) as never }
    const first = deferred<unknown>()
    apiGet.mockReturnValueOnce(first.promise)
    const p = loadSpaceChatUnread()
    // A message lands while the (stale) answer is in flight.
    expect(applySpaceChatUnreadFrame(
      { system_scope: 'space', space_id: 'sA', message: { sender_user_id: 'u2' } }, 'u1',
    )).toBe('bumped')
    expect(spaceChatUnreadOf('sA')).toBe(1)
    apiGet.mockResolvedValueOnce({ spaces: { sA: ROW(1) } })
    first.resolve({ spaces: { sA: ROW(0) } })
    await p
    expect(calls()).toBe(2)
    expect(spaceChatUnreadOf('sA')).toBe(1)
  })

  it('a failed load ends the loop', async () => {
    apiGet.mockRejectedValueOnce(new Error('offline'))
    await loadSpaceChatUnread()
    expect(calls()).toBe(1)
    // The next load starts afresh.
    apiGet.mockResolvedValueOnce({ spaces: { sA: ROW(2) } })
    await loadSpaceChatUnread()
    expect(spaceChatUnreadOf('sA')).toBe(2)
  })

  it('a deleted message refetches only a space chat that shows a count', () => {
    spaceChatUnread.value = { sA: ROW(2) as never, sB: ROW(0) as never }
    expect(spaceChatDeleteNeedsReload({ system_scope: 'space', space_id: 'sA' })).toBe(true)
    expect(spaceChatDeleteNeedsReload({ system_scope: 'space', space_id: 'sB' })).toBe(false)
    expect(spaceChatDeleteNeedsReload({ system_scope: 'space', space_id: 'sX' })).toBe(false)
    expect(spaceChatDeleteNeedsReload({ system_scope: 'household' })).toBe(false)
    expect(spaceChatDeleteNeedsReload(null)).toBe(false)
  })
})
