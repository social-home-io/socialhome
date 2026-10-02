/** An edit (PATCH answer or WS ``post.edited``) changes only the text,
 *  its ``edited_at`` and the link card — the viewer's own fields on the
 *  loaded post (comment count, latest comment, reactions) stay. */
import { describe, it, expect, vi } from 'vitest'
import type { FeedPost } from '@/types'

const handlers = vi.hoisted(() => new Map<string, (e: { data: unknown }) => void>())
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: (e: { data: unknown }) => void) => { handlers.set(type, fn); return () => {} } },
}))
vi.mock('@/api', () => ({ api: { get: vi.fn() } }))

import { posts, wireFeedWs, mergePostEdit } from './feed'

const loaded = {
  id: 'p1', author: 'u1', type: 'text', content: 'old', created_at: '2026-01-01',
  edited_at: null, reactions: { '👍': ['u2'] }, comment_count: 4,
  latest_comment: { id: 'c1', author: 'u2', content: 'hi' },
  link_preview: { url: 'https://a.example', title: 'A' },
} as unknown as FeedPost

const answer = {
  ...loaded, content: 'new', edited_at: '2026-01-02T00:00:00+00:00',
  link_preview: null, reactions: {}, comment_count: 0, latest_comment: null,
} as unknown as FeedPost

describe('post edit merge', () => {
  it('takes content, edited_at and link_preview only', () => {
    const merged = mergePostEdit(loaded, answer)
    expect(merged.content).toBe('new')
    expect(merged.edited_at).toBe('2026-01-02T00:00:00+00:00')
    expect(merged.link_preview).toBeNull()
    expect(merged.comment_count).toBe(4)
    expect(merged.reactions).toEqual({ '👍': ['u2'] })
    expect(merged.latest_comment).toEqual(loaded.latest_comment)
  })

  it('the WS post.edited frame merges the same way', () => {
    wireFeedWs()
    posts.value = [loaded]
    handlers.get('post.edited')!({ data: { post: answer } })
    expect(posts.value[0].content).toBe('new')
    expect(posts.value[0].comment_count).toBe(4)
  })
})
