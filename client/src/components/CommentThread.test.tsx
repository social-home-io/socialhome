import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, cleanup } from '@testing-library/preact'

const me = vi.hoisted(() => ({ is_admin: false }))
vi.mock('@/store/auth', () => ({
  currentUser: {
    get value() {
      return { user_id: 'u-me', display_name: 'Me', is_admin: me.is_admin, picture_url: null }
    },
  },
}))

import { spaceMembers } from '@/store/spaceMembers'
import { CommentThread } from './CommentThread'

const others = {
  id: 'c-1', post_id: 'p-1', author: 'u-other', type: 'text',
  content: 'hello', created_at: '2026-01-01T00:00:00Z', parent_id: null,
} as never

function seatMe(role: string) {
  spaceMembers.value = {
    'sp-1': new Map([['u-me', { user_id: 'u-me', role } as never]]),
  }
}

function menuItems(spaceId: string | null) {
  const r = render(
    <CommentThread comments={[others]} spaceId={spaceId} postId="p-1" onReply={vi.fn()}
      onDelete={vi.fn()} onEdit={vi.fn()} />,
  )
  const btn = r.container.querySelector('.sh-comment-overflow')
  if (!btn) return []
  fireEvent.click(btn)
  return Array.from(r.container.querySelectorAll('[role=menuitem]')).map(b => b.textContent)
}

beforeEach(() => {
  cleanup()
  me.is_admin = false
  spaceMembers.value = {}
})

describe('CommentThread', () => {
  it('module exports exist', async () => {
    const mod = await import('./CommentThread')
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it.each(['moderator', 'admin', 'owner'])(
    'a space %s may edit and delete somebody else\'s comment', (role) => {
      seatMe(role)
      expect(menuItems('sp-1')).toEqual(['Edit', 'Delete'])
    })

  it('a plain space member gets no menu on somebody else\'s comment', () => {
    seatMe('member')
    expect(menuItems('sp-1')).toEqual([])
  })

  it('a household admin keeps delete in a space only through their space role', () => {
    me.is_admin = true
    seatMe('member')
    expect(menuItems('sp-1')).toEqual([])
  })

  it('the household feed keeps the household-admin delete', () => {
    me.is_admin = true
    expect(menuItems(null)).toEqual(['Delete'])
  })
})
