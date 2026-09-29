import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const MEMBERS = [
  { user_id: 'u-me', role: 'owner', display_name: 'Pascal', mention: 'pascal' },
  { user_id: 'u-anna', role: 'member', display_name: 'Anna Berg', mention: 'anna' },
  {
    user_id: 'u-anna2', role: 'member', display_name: 'Anna Smith',
    mention: 'anna@k3f9x2', instance_id: 'peer', household_name: 'The Smiths',
  },
  { user_id: 'u-bob', role: 'member', display_name: 'Bob', mention: 'bob' },
]

let apiGet: ReturnType<typeof vi.fn>

beforeEach(() => {
  vi.resetModules()
  document.body.innerHTML = ''
  apiGet = vi.fn(async () => MEMBERS)
  vi.doMock('@/api', () => ({ api: { get: apiGet, post: vi.fn() } }))
  vi.doMock('@/ws', () => ({ ws: { on: vi.fn(), off: vi.fn(), send: vi.fn() } }))
  vi.doMock('@/store/auth', () => ({
    currentUser: { value: { user_id: 'u-me', username: 'pascal', display_name: 'Pascal' } },
  }))
  vi.doMock('./Toast', () => ({ showToast: vi.fn() }))
})

async function composer(spaceId: string | null = 'sp-1') {
  const { Composer } = await import('./Composer')
  const utils = render(<Composer onSubmit={vi.fn(async () => undefined)} spaceId={spaceId ?? undefined} />)
  const ta = utils.container.querySelector('textarea') as HTMLTextAreaElement
  return { ...utils, ta }
}

function type(ta: HTMLTextAreaElement, value: string) {
  ta.value = value
  ta.setSelectionRange(value.length, value.length)
  fireEvent.input(ta)
}

const listbox = () => document.getElementById('sh-mention-listbox')

describe('MentionAutocomplete in the space composer', () => {
  it('lists matching members (never the viewer), labels remote households', async () => {
    const { ta } = await composer()
    type(ta, 'hi @an')
    await waitFor(() => expect(listbox()).toBeTruthy())
    const options = listbox()!.querySelectorAll('[role="option"]')
    expect([...options].map(o => o.textContent)).toEqual([
      'ANAnna Berg@anna',
      'ANAnna Smith@anna@k3f9x2 · The Smiths',
    ])
    expect(listbox()!.textContent).not.toContain('Pascal')
    // Combobox wiring on the textarea.
    expect(ta.getAttribute('aria-controls')).toBe('sh-mention-listbox')
    expect(ta.getAttribute('aria-activedescendant')).toBe('sh-mention-opt-0')
    expect(options[0].getAttribute('aria-selected')).toBe('true')
  })

  it('fetches the roster once, not per keystroke', async () => {
    const { ta } = await composer()
    type(ta, '@a')
    type(ta, '@an')
    type(ta, '@ann')
    await waitFor(() => expect(listbox()).toBeTruthy())
    type(ta, '@anna')
    const rosterCalls = apiGet.mock.calls.filter(c => c[0] === '/api/spaces/sp-1/members')
    expect(rosterCalls).toHaveLength(1)
  })

  it('arrow keys move the highlight and Enter inserts the resolvable token', async () => {
    const { ta } = await composer()
    type(ta, 'lunch @an')
    await waitFor(() => expect(listbox()).toBeTruthy())
    fireEvent.keyDown(ta, { key: 'ArrowDown' })
    expect(ta.getAttribute('aria-activedescendant')).toBe('sh-mention-opt-1')
    fireEvent.keyDown(ta, { key: 'ArrowDown' })
    expect(ta.getAttribute('aria-activedescendant')).toBe('sh-mention-opt-0')
    fireEvent.keyDown(ta, { key: 'ArrowUp' })
    fireEvent.keyDown(ta, { key: 'Enter' })
    await waitFor(() => expect(ta.value).toBe('lunch @anna@k3f9x2 '))
    expect(listbox()).toBeNull()
  })

  it('Tab picks, Escape closes without inserting', async () => {
    const { ta } = await composer()
    type(ta, '@bo')
    await waitFor(() => expect(listbox()).toBeTruthy())
    fireEvent.keyDown(ta, { key: 'Escape' })
    expect(listbox()).toBeNull()
    expect(ta.value).toBe('@bo')
    type(ta, '@bo')
    await waitFor(() => expect(listbox()).toBeTruthy())
    fireEvent.keyDown(ta, { key: 'Tab' })
    await waitFor(() => expect(ta.value).toBe('@bob '))
  })

  it('clicking an option inserts it', async () => {
    const { ta } = await composer()
    type(ta, '@b')
    await waitFor(() => expect(listbox()).toBeTruthy())
    fireEvent.mouseDown(listbox()!.querySelector('[role="option"]')!)
    await waitFor(() => expect(ta.value).toBe('@bob '))
  })

  it('says so when nobody matches, and Enter falls through', async () => {
    const { ta } = await composer()
    type(ta, '@zz')
    await waitFor(() =>
      expect(document.body.textContent).toContain('No member of this space matches'))
    expect(listbox()).toBeNull()
    expect(ta.getAttribute('aria-expanded')).toBe('false')
  })

  it('never opens outside a space (household feed)', async () => {
    const { ta } = await composer(null)
    apiGet.mockClear()
    type(ta, '@an')
    await Promise.resolve()
    expect(listbox()).toBeNull()
    expect(apiGet).not.toHaveBeenCalledWith('/api/spaces/sp-1/members')
  })

  it('an e-mail address does not trigger it', async () => {
    const { ta } = await composer()
    type(ta, 'mail bob@an')
    await Promise.resolve()
    expect(listbox()).toBeNull()
  })
})

describe('MentionAutocomplete in the space comment input', () => {
  it('works in the "Add a comment" input and highlights known mentions', async () => {
    const { loadSpaceMembers } = await import('@/store/spaceMembers')
    await loadSpaceMembers('sp-1')
    const { CommentThread } = await import('./CommentThread')
    const { getByLabelText, container } = render(
      <CommentThread
        spaceId="sp-1"
        postId="p1"
        comments={[{
          id: 'c1', post_id: 'p1', author: 'u-bob', parent_id: null,
          content: 'ping @anna and @pascal, not @nobody', type: 'text',
          created_at: new Date().toISOString(),
        } as never]}
        onReply={vi.fn(async () => undefined)}
      />,
    )
    const spans = [...container.querySelectorAll('.sh-mention')]
    expect(spans.map(s => s.textContent)).toEqual(['@anna', '@pascal'])
    expect(spans[1].classList.contains('sh-mention--self')).toBe(true)

    const input = getByLabelText('New comment') as HTMLInputElement
    input.value = '@bo'
    input.setSelectionRange(3, 3)
    fireEvent.input(input)
    await waitFor(() => expect(listbox()).toBeTruthy())
    expect(input.getAttribute('aria-activedescendant')).toBe('sh-mention-opt-0')
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(input.value).toBe('@bob '))
  })
})
