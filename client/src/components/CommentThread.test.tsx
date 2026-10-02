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
      expect(menuItems('sp-1')).toEqual(['Edit', 'Delete', 'Report'])
    })

  it('a plain space member may only report somebody else\'s comment', () => {
    seatMe('member')
    expect(menuItems('sp-1')).toEqual(['Report'])
  })

  it('a household admin keeps delete in a space only through their space role', () => {
    me.is_admin = true
    seatMe('member')
    expect(menuItems('sp-1')).toEqual(['Report'])
  })

  it('the household feed keeps the household-admin delete', () => {
    me.is_admin = true
    expect(menuItems(null)).toEqual(['Delete', 'Report'])
  })

  it('Report opens the report dialog for the comment, scoped to the space', async () => {
    const mod = await import('./ReportDialog')
    const spy = vi.spyOn(mod, 'openReport')
    seatMe('member')
    const r = render(
      <CommentThread comments={[others]} spaceId="sp-1" postId="p-1" onReply={vi.fn()} />,
    )
    fireEvent.click(r.container.querySelector('.sh-comment-overflow')!)
    fireEvent.click(r.getByRole('menuitem', { name: 'Report' }))
    expect(spy).toHaveBeenCalledWith('comment', 'c-1', 'sp-1')
  })

  it('no Report on your own comment', () => {
    seatMe('member')
    const mine = { ...(others as object), author: 'u-me' } as never
    const r = render(
      <CommentThread comments={[mine]} spaceId="sp-1" postId="p-1" onReply={vi.fn()} />,
    )
    expect(r.container.querySelector('.sh-comment-overflow')).toBeNull()
  })
})
