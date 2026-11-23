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
import { clearTextDraft } from '@/store/textDrafts'
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
  for (const post of ['p-1', 'p-a', 'p-b']) {
    clearTextDraft(`comment:${post}`)
    clearTextDraft(`comment:${post}:reply:c-1`)
  }
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

describe('drafts are per post', () => {
  const newField = (c: Element) =>
    c.querySelector<HTMLTextAreaElement>('#sh-comment-new-input')!

  it('text typed under post A does not show under post B, and comes back on A', () => {
    const r = render(<CommentThread comments={[]} postId="p-a" onReply={vi.fn()} />)
    fireEvent.input(newField(r.container), { target: { value: 'for A only' } })
    // The comment sheet is reused for the next post.
    r.rerender(<CommentThread comments={[]} postId="p-b" onReply={vi.fn()} />)
    expect(newField(r.container).value).toBe('')
    r.rerender(<CommentThread comments={[]} postId="p-a" onReply={vi.fn()} />)
    expect(newField(r.container).value).toBe('for A only')
  })

  it('two threads on screen keep their own text', () => {
    const a = render(<CommentThread comments={[]} postId="p-a" onReply={vi.fn()} />)
    const b = render(<CommentThread comments={[]} postId="p-b" onReply={vi.fn()} />)
    fireEvent.input(newField(a.container), { target: { value: 'A' } })
    expect(newField(b.container).value).toBe('')
  })

  it('a reply draft belongs to its post and its comment', () => {
    const r = render(<CommentThread comments={[others]} postId="p-a" onReply={vi.fn()} />)
    fireEvent.click(r.getByText('Reply'))
    const reply = () => r.container.querySelector<HTMLTextAreaElement>('#sh-comment-reply-input')
    fireEvent.input(reply()!, { target: { value: 'reply on A' } })
    r.rerender(<CommentThread comments={[others]} postId="p-b" onReply={vi.fn()} />)
    // Same comment id under another post: the form is open, its own text empty.
    expect(reply()!.value).toBe('')
    r.rerender(<CommentThread comments={[others]} postId="p-a" onReply={vi.fn()} />)
    expect(reply()!.value).toBe('reply on A')
  })

  it('a send in progress on post A does not block or disable post B', async () => {
    let finish: () => void = () => {}
    const slow = vi.fn(() => new Promise<void>((res) => { finish = res }))
    const a = render(<CommentThread comments={[]} postId="p-a" onReply={slow} />)
    const fast = vi.fn().mockResolvedValue(undefined)
    const b = render(<CommentThread comments={[]} postId="p-b" onReply={fast} />)
    fireEvent.input(newField(a.container), { target: { value: 'A' } })
    fireEvent.keyDown(newField(a.container), { key: 'Enter' })
    expect(slow).toHaveBeenCalledTimes(1)
    fireEvent.input(newField(b.container), { target: { value: 'B' } })
    const post = b.getByText('Post').closest('button')!
    expect(post.disabled).toBe(false)
    fireEvent.click(post)
    expect(fast).toHaveBeenCalledWith(null, 'B')
    finish()
    await waitFor(() => expect(newField(a.container).value).toBe(''))
  })

  it('text typed while a send is on its way is kept', async () => {
    let finish: () => void = () => {}
    const slow = vi.fn(() => new Promise<void>((res) => { finish = res }))
    const a = render(<CommentThread comments={[]} postId="p-a" onReply={slow} />)
    fireEvent.input(newField(a.container), { target: { value: 'first' } })
    fireEvent.keyDown(newField(a.container), { key: 'Enter' })
    fireEvent.input(newField(a.container), { target: { value: 'first and more' } })
    finish()
    await waitFor(() => expect(slow).toHaveBeenCalledWith(null, 'first'))
    await new Promise((res) => setTimeout(res, 0))
    expect(newField(a.container).value).toBe('first and more')
  })

  it('a failed send keeps the text; a successful one clears only that post', async () => {
    const fail = vi.fn().mockRejectedValue(new Error('offline'))
    const a = render(<CommentThread comments={[]} postId="p-a" onReply={fail} />)
    const b = render(<CommentThread comments={[]} postId="p-b" onReply={vi.fn().mockResolvedValue(undefined)} />)
    fireEvent.input(newField(a.container), { target: { value: 'try again' } })
    fireEvent.input(newField(b.container), { target: { value: 'goes out' } })
    fireEvent.keyDown(newField(a.container), { key: 'Enter' })
    await waitFor(() => expect(fail).toHaveBeenCalled())
    expect(newField(a.container).value).toBe('try again')
    fireEvent.keyDown(newField(b.container), { key: 'Enter' })
    await waitFor(() => expect(newField(b.container).value).toBe(''))
    expect(newField(a.container).value).toBe('try again')
  })
})

describe('the emoji button in a comment', () => {
  it('puts the emoji at the caret and gives the caret back to the field', async () => {
    const r = render(<CommentThread comments={[]} postId="p-a" onReply={vi.fn()} />)
    const field = r.container.querySelector<HTMLTextAreaElement>('#sh-comment-new-input')!
    fireEvent.input(field, { target: { value: 'hello world' } })
    field.setSelectionRange(5, 5)
    fireEvent.click(r.container.querySelector('.sh-emoji-pick-btn')!)
    fireEvent.click(r.container.querySelector('.sh-reaction-frequent .sh-emoji-btn')!)
    await waitFor(() => expect(field.value).toBe('hello👍 world'))
    await waitFor(() => expect(document.activeElement).toBe(field))
    expect(field.selectionStart).toBe('hello👍'.length)
    expect(field.selectionEnd).toBe('hello👍'.length)
  })

  it('replaces a selection, like typing would', async () => {
    const r = render(<CommentThread comments={[]} postId="p-a" onReply={vi.fn()} />)
    const field = r.container.querySelector<HTMLTextAreaElement>('#sh-comment-new-input')!
    fireEvent.input(field, { target: { value: 'hello world' } })
    field.setSelectionRange(6, 11)
    fireEvent.click(r.container.querySelector('.sh-emoji-pick-btn')!)
    fireEvent.click(r.container.querySelector('.sh-reaction-frequent .sh-emoji-btn')!)
    await waitFor(() => expect(field.value).toBe('hello 👍'))
  })
})

describe('Escape in a comment field', () => {
  function withSheet(run: (sheetEscape: ReturnType<typeof vi.fn>) => void) {
    const sheetEscape = vi.fn()
    document.addEventListener('keydown', sheetEscape)
    try { run(sheetEscape) } finally { document.removeEventListener('keydown', sheetEscape) }
  }

  it('with the :emoji list open closes only the list', () => {
    withSheet((sheetEscape) => {
      const r = render(<CommentThread comments={[]} postId="p-a" onReply={vi.fn()} />)
      const field = r.container.querySelector<HTMLTextAreaElement>('#sh-comment-new-input')!
      fireEvent.input(field, { target: { value: 'nice :smi' } })
      expect(r.container.querySelector('.sh-emoji-autocomplete')).not.toBeNull()
      fireEvent.keyDown(field, { key: 'Escape' })
      expect(r.container.querySelector('.sh-emoji-autocomplete')).toBeNull()
      expect(sheetEscape).not.toHaveBeenCalled()
    })
  })

  it('with no list open still reaches the sheet, which closes', () => {
    withSheet((sheetEscape) => {
      const r = render(<CommentThread comments={[]} postId="p-a" onReply={vi.fn()} />)
      const field = r.container.querySelector<HTMLTextAreaElement>('#sh-comment-new-input')!
      fireEvent.keyDown(field, { key: 'Escape' })
      expect(sheetEscape).toHaveBeenCalledTimes(1)
    })
  })
})
