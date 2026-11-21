import { describe, it, expect } from 'vitest'
import type { FeedPost } from '@/types'
import { applySpacePostCreated } from './spaceFeedLive'

const post = (id: string, extra: Record<string, unknown> = {}) => ({
  id, author: 'u2', type: 'text', content: `post ${id}`,
  created_at: '2026-11-21T10:00:00+00:00', reactions: {}, comment_count: 0,
  deleted: false, hidden_from_feed: false, bot_id: null, ...extra,
})
const feed = [post('old-2'), post('old-1')] as unknown as FeedPost[]

describe('applySpacePostCreated', () => {
  it('puts a new post on top from the frame', () => {
    const next = applySpacePostCreated(feed, post('new'))
    expect(Array.isArray(next)).toBe(true)
    expect((next as FeedPost[]).map(p => p.id)).toEqual(['new', 'old-2', 'old-1'])
  })

  it('a post already in the feed changes nothing (own post, replayed frame)', () => {
    expect(applySpacePostCreated(feed, post('old-2'))).toBeNull()
  })

  it('a post the feed leaves out never shows: hidden anchors, deleted posts', () => {
    expect(applySpacePostCreated(feed, post('anchor', { hidden_from_feed: true }))).toBeNull()
    expect(applySpacePostCreated(feed, post('gone', { deleted: true }))).toBeNull()
  })

  it('what only the feed can spell asks for a refetch: bot posts, videos, no post', () => {
    expect(applySpacePostCreated(feed, post('b', { bot_id: 'bot-1', author: 'system-integration' }))).toBe('refetch')
    expect(applySpacePostCreated(feed, post('s', { author: 'system-integration' }))).toBe('refetch')
    expect(applySpacePostCreated(feed, post('v', { type: 'video', media_url: 'api/media/a.webm' }))).toBe('refetch')
    expect(applySpacePostCreated(feed, undefined)).toBe('refetch')
    expect(applySpacePostCreated(feed, { content: 'no id' })).toBe('refetch')
  })
})
