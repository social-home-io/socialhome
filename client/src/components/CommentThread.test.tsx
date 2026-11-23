import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, cleanup, waitFor } from '@testing-library/preact'

const me = vi.hoisted(() => ({ is_admin: false }))
vi.mock('@/store/auth', () => ({
  currentUser: {
    get value() {
      return { user_id: 'u-me', display_name: 'Me', is_admin: me.is_admin, picture_url: null }
    },
  },
}))

const openPersonSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

import { spaceMembers } from '@/store/spaceMembers'
import { CommentThread, commentFieldCap } from './CommentThread'

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

  it('the comment author (name and avatar) opens their person sheet', () => {
    openPersonSpy.mockReset()
    spaceMembers.value = {
      'sp-1': new Map([['u-other', {
        user_id: 'u-other', role: 'member', display_name: 'Otto', picture_url: 'api/p/otto',
      } as never]]),
    }
    const r = render(
      <CommentThread comments={[others]} spaceId="sp-1" postId="p-1" onReply={vi.fn()} />,
    )
    const name = r.getByTestId('comment-author')
    expect(name.tagName).toBe('BUTTON')
    fireEvent.click(name)
    expect(openPersonSpy).toHaveBeenCalledWith('u-other', {
      person: expect.objectContaining({ display_name: 'Otto', picture_url: 'api/p/otto' }),
    })
    const face = r.container.querySelector('.sh-person-btn--avatar')!
    expect(face.getAttribute('tabindex')).toBe('-1')
  })

  it('an @mention in a space comment opens that member; @here does not', () => {
    openPersonSpy.mockReset()
    spaceMembers.value = {
      'sp-1': new Map([
        ['u-other', { user_id: 'u-other', role: 'admin' } as never],
        ['u-anna', { user_id: 'u-anna', role: 'member', mention: 'anna', display_name: 'Anna' } as never],
      ]),
    }
    const c = { ...(others as object), content: 'hey @anna and @here' } as never
    const r = render(
      <CommentThread comments={[c]} spaceId="sp-1" postId="p-1" onReply={vi.fn()} />,
    )
    const btn = r.container.querySelector('button[data-mention="anna"]') as HTMLButtonElement
    expect(btn).toBeTruthy()
    fireEvent.click(btn)
    expect(openPersonSpy).toHaveBeenCalledWith('u-anna', expect.anything())
    expect(r.container.querySelector('button[data-mention="here"]')).toBeNull()
  })
})

/** Lay every textarea out as if its text needed ``px()`` px (16 px a
 *  line, ~60 characters a line), with a 1 px border. */
function fakeTextareaLayout() {
  const proto = HTMLTextAreaElement.prototype
  const need = (el: HTMLTextAreaElement) =>
    36 + 16 * (Math.ceil(el.value.length / 60) + el.value.split('\n').length - 2)
  const spies = [
    vi.spyOn(proto, 'scrollHeight', 'get').mockImplementation(function (this: HTMLTextAreaElement) { return Math.max(36, need(this)) }),
    vi.spyOn(proto, 'clientHeight', 'get').mockImplementation(function (this: HTMLTextAreaElement) { return Math.max(36, need(this)) }),
    vi.spyOn(proto, 'offsetHeight', 'get').mockImplementation(function (this: HTMLTextAreaElement) { return Math.max(36, need(this)) + 2 }),
  ]
  return () => spies.forEach(s => s.mockRestore())
}

describe('the comment field', () => {
  function mount(onReply = vi.fn().mockResolvedValue(undefined), comments: never[] = [others]) {
    const r = render(<CommentThread comments={comments} postId="p-1" onReply={onReply} />)
    const field = r.container.querySelector<HTMLTextAreaElement>('#sh-comment-new-input')!
    return { ...r, field, onReply }
  }

  it('is a one-line textarea capped at the server limit', () => {
    const { field } = mount()
    expect(field.tagName).toBe('TEXTAREA')
    expect(field.getAttribute('rows')).toBe('1')
    expect(field.maxLength).toBe(2000)
  })

  it('is a textarea on an empty thread too', () => {
    const { field } = mount(undefined, [])
    expect(field.tagName).toBe('TEXTAREA')
  })

  it('Enter sends, Shift+Enter starts a new line', () => {
    const { field, onReply } = mount()
    fireEvent.input(field, { target: { value: 'line one' } })
    fireEvent.keyDown(field, { key: 'Enter', shiftKey: true })
    expect(onReply).not.toHaveBeenCalled()
    fireEvent.keyDown(field, { key: 'Enter' })
    expect(onReply).toHaveBeenCalledWith(null, 'line one')
  })

  it('on a touch screen Return is a new line; the Post button sends', () => {
    const orig = window.matchMedia
    window.matchMedia = ((q: string) => ({ matches: q === '(pointer: coarse)' })) as never
    try {
      const { field, onReply, getByText } = mount()
      fireEvent.input(field, { target: { value: 'line one' } })
      fireEvent.keyDown(field, { key: 'Enter' })
      expect(onReply).not.toHaveBeenCalled()
      fireEvent.click(getByText('Post'))
      expect(onReply).toHaveBeenCalledWith(null, 'line one')
    } finally {
      window.matchMedia = orig
    }
  })

  it('Enter that confirms an input-method composition does not send', () => {
    const { field, onReply } = mount()
    fireEvent.input(field, { target: { value: 'にほん' } })
    fireEvent.keyDown(field, { key: 'Enter', isComposing: true })
    expect(onReply).not.toHaveBeenCalled()
  })

  it('grows with long text and shrinks back after sending', async () => {
    const restore = fakeTextareaLayout()
    try {
      const { field } = mount()
      expect(field.style.height).toBe('38px')
      fireEvent.input(field, { target: { value: 'word '.repeat(60) } })
      // 300 characters → 5 lines → 36 + 4 × 16 = 100 px (+ 2 px border).
      expect(field.style.height).toBe('102px')
      expect(field.style.overflowY).toBe('hidden')
      fireEvent.keyDown(field, { key: 'Enter' })
      await waitFor(() => expect(field.value).toBe(''))
      expect(field.style.height).toBe('38px')
    } finally {
      restore()
    }
  })

  it('stops growing at the cap and scrolls inside', () => {
    const restore = fakeTextareaLayout()
    try {
      Object.defineProperty(window, 'innerHeight', { configurable: true, value: 720 })
      const { field } = mount()
      fireEvent.input(field, { target: { value: 'word '.repeat(400) } })
      expect(field.style.height).toBe(`${commentFieldCap(720)}px`)
      expect(field.style.overflowY).toBe('auto')
    } finally {
      restore()
    }
  })

  it('the reply field is a growing textarea as well', () => {
    const { container } = mount()
    fireEvent.click(container.querySelector('.sh-comment-actions .sh-link')!)
    const reply = container.querySelector('#sh-comment-reply-input')!
    expect(reply.tagName).toBe('TEXTAREA')
    // Ready to type: "Reply" puts the caret in the field.
    expect(document.activeElement).toBe(reply)
  })

  it('Edit puts the caret in the edit field; Escape there closes the editor, not the sheet', () => {
    const mine = { ...(others as object), author: 'u-me' } as never
    const sheetEscape = vi.fn()
    document.addEventListener('keydown', sheetEscape)
    try {
      const r = render(<CommentThread comments={[mine]} postId="p-1" onReply={vi.fn()} onEdit={vi.fn()} />)
      fireEvent.click(r.container.querySelector('.sh-comment-overflow')!)
      fireEvent.click(r.getByText('Edit'))
      const field = r.container.querySelector<HTMLTextAreaElement>('.sh-comment-bubble--editing textarea')!
      expect(document.activeElement).toBe(field)
      expect(field.selectionStart).toBe(field.value.length)
      fireEvent.keyDown(field, { key: 'Escape' })
      expect(r.container.querySelector('.sh-comment-bubble--editing')).toBeNull()
      expect(sheetEscape).not.toHaveBeenCalled()
      // Focus goes back to the ··· button that opened the editor.
      expect(document.activeElement).toBe(r.container.querySelector('.sh-comment-overflow'))
    } finally {
      document.removeEventListener('keydown', sheetEscape)
    }
  })

  it('counts characters only near the limit', () => {
    const { field, container } = mount()
    fireEvent.input(field, { target: { value: 'x'.repeat(100) } })
    expect(container.querySelector('.sh-comment-count')).toBeNull()
    fireEvent.input(field, { target: { value: 'x'.repeat(1900) } })
    expect(container.querySelector('.sh-comment-count')!.textContent).toBe('1900/2000')
  })
})

describe('commentFieldCap', () => {
  it('lets a comment take about 40 % of what is visible', () => {
    expect(commentFieldCap(720)).toBe(288)
    expect(commentFieldCap(844)).toBe(337)
  })
  it('keeps at least three lines on a tiny visible area (keyboard up)', () => {
    expect(commentFieldCap(150)).toBe(72)
  })
})
