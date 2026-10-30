/**
 * Tests for the notifications store: the badge counts a row once, and a
 * page that shows something tells the server it was seen.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const handlers: Record<string, (e: { data: Record<string, unknown> }) => void> = {}

vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: (e: { data: Record<string, unknown> }) => void) => {
      handlers[type] = h
      return () => { delete handlers[type] }
    },
  },
}))

const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: { post: (...args: unknown[]) => apiPost(...args) },
}))

import { flushSeen, markSeen, markTurnsSeen, postLink, recent, unreadCount, wireNotificationsWs } from './notifications'

const frame = (data: Record<string, unknown>) => handlers['notification.new']({ data })

describe('notification.new', () => {
  beforeEach(() => {
    recent.value = []
    unreadCount.value = 0
    wireNotificationsWs()
  })

  it('counts a new row once, a repeated frame not again', () => {
    frame({ notification_id: 'n1', notif_type: 'post_created', title: 'A' })
    frame({ notification_id: 'n1', notif_type: 'post_created', title: 'A' })
    expect(unreadCount.value).toBe(1)
    expect(recent.value).toHaveLength(1)
  })

  it('does not over-count a bumped chat row after a reload (recent is empty)', () => {
    // The server says 3 unread; the page was reloaded, so ``recent`` is empty.
    unreadCount.value = 3
    frame({ notification_id: 'dm1', notif_type: 'dm_message', title: 'Anna messaged you', bumped: true })
    frame({ notification_id: 'dm1', notif_type: 'dm_message', title: 'Anna messaged you', bumped: true })
    expect(unreadCount.value).toBe(3)
    expect(recent.value.map((n) => n.notification_id)).toEqual(['dm1'])
  })

  it('moves a bumped row to the top with its fresh title', () => {
    frame({ notification_id: 'dm1', notif_type: 'dm_message', title: 'old' })
    frame({ notification_id: 'n2', notif_type: 'post_created', title: 'B' })
    frame({ notification_id: 'dm1', notif_type: 'dm_message', title: 'new', bumped: true })
    expect(recent.value.map((n) => n.title)).toEqual(['new', 'B'])
    expect(unreadCount.value).toBe(2)
  })

  it('takes the count from notification.unread_count', () => {
    handlers['notification.unread_count']({ data: { unread_count: 7 } })
    expect(unreadCount.value).toBe(7)
  })
})

describe('markSeen', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    apiPost.mockReset()
    apiPost.mockResolvedValue({ ok: true, marked: 1 })
  })
  afterEach(() => { vi.useRealTimers() })

  it('sends the pages seen in one short window as one call', async () => {
    markSeen('/post/p1')
    markSeen('/spaces/s1')
    markSeen('/post/p1')
    expect(apiPost).not.toHaveBeenCalled()
    await vi.runAllTimersAsync()
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith(
      '/api/notifications/read-by-link',
      { links: ['/post/p1', '/spaces/s1'] },
    )
  })

  it('splits more pages than the server takes into several calls', async () => {
    for (let i = 0; i < 25; i++) markSeen(`/post/p${i}`)
    await vi.runAllTimersAsync()
    expect(apiPost).toHaveBeenCalledTimes(2)
    expect((apiPost.mock.calls[0][1] as { links: string[] }).links).toHaveLength(20)
    expect((apiPost.mock.calls[1][1] as { links: string[] }).links).toHaveLength(5)
  })

  it('stays silent when the call fails', async () => {
    apiPost.mockRejectedValue(new Error('offline'))
    markSeen('/momentum/m1')
    await expect(flushSeen()).resolves.toBeUndefined()
  })

  it('does nothing with nothing queued', async () => {
    await flushSeen()
    expect(apiPost).not.toHaveBeenCalled()
  })
})

describe('markTurnsSeen', () => {
  beforeEach(() => {
    apiPost.mockReset()
    apiPost.mockResolvedValue({ ok: true, marked: 1 })
  })

  it('reads only this app\'s "Your turn" rows — never its invites', () => {
    recent.value = [
      { notification_id: 'n1', notif_type: 'app_turn', title: 'T', link_url: '/apps/chess', object_key: 'app_session:chess/s1' },
      { notification_id: 'n2', notif_type: 'app_turn', title: 'T', link_url: '/apps/chess', object_key: 'app_session:chess/s1' },
      { notification_id: 'n3', notif_type: 'app_turn', title: 'T', link_url: '/apps/go', object_key: 'app_session:go/s9' },
      { notification_id: 'n4', notif_type: 'app_challenge', title: 'I', link_url: '/apps/chess', object_key: 'app_session:chess/s2' },
    ]
    markTurnsSeen('chess')
    expect(apiPost).toHaveBeenCalledOnce()
    expect(apiPost).toHaveBeenCalledWith(
      '/api/notifications/read-objects',
      { object_keys: ['app_session:chess/s1'] },
    )
  })

  it('asks nothing when there is no turn row, and stays silent on failure', async () => {
    recent.value = []
    markTurnsSeen('chess')
    expect(apiPost).not.toHaveBeenCalled()
    recent.value = [
      { notification_id: 'n1', notif_type: 'app_turn', title: 'T', link_url: '/apps/chess', object_key: 'app_session:chess/s1' },
    ]
    apiPost.mockRejectedValue(new Error('offline'))
    expect(() => markTurnsSeen('chess')).not.toThrow()
    await Promise.resolve()
  })
})

describe('postLink', () => {
  it('is the link the server gives a post\'s notifications', () => {
    expect(postLink('p1', null)).toBe('/post/p1')
    expect(postLink('p1', 's1')).toBe('/spaces/s1?post=p1')
  })
})
