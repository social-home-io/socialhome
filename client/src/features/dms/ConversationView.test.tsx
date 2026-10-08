import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

// Same ceilings as DmThreadPage.test.tsx — the cold render of the thread
// (fresh ``import()`` + mocked-API microtask chain) can be slow under the
// full parallel suite.
vi.setConfig({ testTimeout: 20_000 })
const RENDER_WAIT = 15_000

const apiGet = vi.fn()
const apiPost = vi.fn()
const route = vi.fn()

vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: {}, path: '/' }),
  useLocation: () => ({ url: '/', route }),
}))

vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn().mockResolvedValue({}),
  },
}))

vi.mock('@/components/LocationMap', () => ({
  LocationMap: () => <div data-testid="map" />,
}))

vi.mock('@/ws', () => ({
  ws: { on: vi.fn(() => () => {}), send: vi.fn() },
}))

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 't' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const convRow = (id: string, type: 'dm' | 'group_dm' = 'dm') => ({
  id, type, name: type === 'group_dm' ? `Group ${id}` : null,
  last_message_at: '2026-05-17T13:00:42+00:00',
  members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
  member_count: type === 'group_dm' ? 3 : 2,
  managed_here: true, unread: 0, last_read_at: null,
  muted_until: null, notif_level: 'all',
})

const msgRow = (
  id: string, content: string, sender = 'u-bob', type = 'text',
) => ({
  id, sender_user_id: sender, content, type,
  media_url: null, file_name: null, mime_type: null,
  file_size_bytes: null, reply_to_id: null,
  reactions: [], deleted: false,
  created_at: '2026-05-17T13:00:42+00:00',
  edited_at: null,
})

const roster = [
  { user_id: 'u-me', username: 'me', display_name: 'Me', picture_url: null, is_self: true, is_online: true, is_idle: false, last_seen_at: null },
  { user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null, is_self: false, is_online: false, is_idle: false, last_seen_at: null },
  { user_id: 'u-cat', username: 'cat', display_name: 'Cat', picture_url: null, is_self: false, is_online: false, is_idle: false, last_seen_at: null },
]

/** Two threads: ``conv-a`` (a 1:1 with Bob) and ``conv-b`` (a group),
 *  each with one message from Bob and one of the viewer's own. */
function wireApi(): void {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/conversations') {
      throw new Error('the thread must not fetch the whole inbox')
    }
    if (url === '/api/conversations/conv-a') return convRow('conv-a')
    if (url === '/api/conversations/conv-b') return convRow('conv-b', 'group_dm')
    for (const id of ['conv-a', 'conv-b']) {
      if (url.startsWith(`/api/conversations/${id}/messages`)) {
        return [
          msgRow(`${id}-mine`, `MINE-${id}`, 'u-me'),
          msgRow(`${id}-bob`, `BOB-${id}`),
          msgRow(`${id}-call`, JSON.stringify({ event: 'missed', call_type: 'audio' }), 'u-bob', 'call_event'),
        ]
      }
      if (url === `/api/conversations/${id}/members`) {
        return id === 'conv-a' ? roster.slice(0, 2) : roster
      }
    }
    return {}
  })
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPost.mockResolvedValue({})
  route.mockReset()
  wireApi()
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
  document.body.className = ''
})

async function setup() {
  const tl = await import('@testing-library/preact')
  const { ConversationView } = await import('./ConversationView')
  const { pageTitle } = await import('@/store/pageTitle')
  return { ...tl, ConversationView, pageTitle }
}

const threadOf = (root: Element, id: string): HTMLElement =>
  root.querySelector<HTMLElement>(`[data-testid="${id}"] .sh-thread`)!

describe('ConversationView', () => {
  it('loads its metadata from GET /api/conversations/{id}, never the whole list', async () => {
    const { render, waitFor, ConversationView } = await setup()
    const { container } = render(<ConversationView conversationId="conv-b" />)
    await waitFor(() => {
      expect(container.querySelector('.sh-thread-group-btn')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    const urls = apiGet.mock.calls.map(c => c[0])
    expect(urls).toContain('/api/conversations/conv-b')
    expect(urls).not.toContain('/api/conversations')
  })

  it('two instances never share reply or edit state', async () => {
    const { render, waitFor, fireEvent, ConversationView } = await setup()
    const { container } = render(
      <div>
        <div data-testid="a"><ConversationView conversationId="conv-a" embedded /></div>
        <div data-testid="b"><ConversationView conversationId="conv-b" embedded /></div>
      </div>,
    )
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-a')
      expect(container.textContent ?? '').toContain('BOB-conv-b')
    }, { timeout: RENDER_WAIT })
    const a = threadOf(container, 'a')
    const b = threadOf(container, 'b')

    // Reply to Bob in A → only A's composer shows the reply chip.
    fireEvent.click(
      a.querySelector('[data-msg-id="conv-a-bob"] .sh-message-reply-btn')!,
    )
    await waitFor(() => {
      expect(a.querySelector('.sh-composer-reply')).not.toBeNull()
    })
    expect(b.querySelector('.sh-composer-reply')).toBeNull()

    // Edit the viewer's own message in B → only B's bubble opens the box.
    fireEvent.click(
      b.querySelector('[data-msg-id="conv-b-mine"] .sh-message-edit-btn')!,
    )
    await waitFor(() => {
      expect(b.querySelector('.sh-message-edit')).not.toBeNull()
    })
    expect(a.querySelector('.sh-message-edit')).toBeNull()
    // …and A's reply target is untouched by B.
    expect(a.querySelector('.sh-composer-reply')).not.toBeNull()
    expect(b.querySelector('.sh-composer-reply')).toBeNull()
  })

  it('the routed (default) view carries the page chrome', async () => {
    const { render, waitFor, ConversationView, pageTitle } = await setup()
    const { container } = render(<ConversationView conversationId="conv-a" />)
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-a')
    }, { timeout: RENDER_WAIT })
    expect(container.querySelector('.sh-thread-back')).not.toBeNull()
    expect(document.body.classList.contains('sh-dm-thread-open')).toBe(true)
    expect(container.querySelector('textarea')?.getAttribute('data-shortcut')).toBe('composer')
    await waitFor(() => { expect(pageTitle.value).toBe('Bob') })
  })

  it('embedded hides the page chrome', async () => {
    const { render, waitFor, ConversationView, pageTitle } = await setup()
    pageTitle.value = 'Feed'
    const { container } = render(<ConversationView conversationId="conv-a" embedded />)
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-a')
    }, { timeout: RENDER_WAIT })
    expect(container.querySelector('.sh-thread--embedded')).not.toBeNull()
    expect(container.querySelector('.sh-thread-back')).toBeNull()
    expect(document.body.classList.contains('sh-dm-thread-open')).toBe(false)
    expect(container.querySelector('textarea')?.hasAttribute('data-shortcut')).toBe(false)
    // The host page keeps its own TopBar title.
    expect(pageTitle.value).toBe('Feed')
    // The thread itself is all there.
    expect(container.querySelector('.sh-thread-header')).not.toBeNull()
    expect(container.querySelector('form.sh-composer')).not.toBeNull()
  })

  it('showHeader={false} drops the header but keeps the thread', async () => {
    const { render, waitFor, ConversationView } = await setup()
    const { container } = render(
      <ConversationView conversationId="conv-a" embedded showHeader={false} />,
    )
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-a')
    }, { timeout: RENDER_WAIT })
    expect(container.querySelector('.sh-thread-header')).toBeNull()
    expect(container.querySelector('form.sh-composer')).not.toBeNull()
  })

  it('shows attach, group info and call controls by default', async () => {
    const { render, waitFor, ConversationView } = await setup()
    const { container } = render(<ConversationView conversationId="conv-b" embedded />)
    await waitFor(() => {
      expect(container.querySelector('.sh-thread-group-btn')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    expect(container.querySelector('.sh-dm-attach-btn')).not.toBeNull()
    expect(container.querySelector('.sh-thread-call-buttons')).not.toBeNull()
    expect(container.querySelector('.sh-thread-history')).not.toBeNull()
    expect(container.querySelector('.sh-call-event button')).not.toBeNull()
  })

  it('capability flags hide attach, group info and call controls', async () => {
    const { render, waitFor, ConversationView } = await setup()
    const { container } = render(
      <ConversationView
        conversationId="conv-b"
        embedded
        allowAttachments={false}
        showGroupInfo={false}
        allowCalls={false}
      />,
    )
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-b')
    }, { timeout: RENDER_WAIT })
    // Let the metadata + roster land so the controls would have shown.
    await waitFor(() => {
      expect(container.querySelector('[aria-label="Mute notifications"]')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    expect(container.querySelector('.sh-dm-attach-btn')).toBeNull()
    expect(container.querySelector('input[type="file"]')).toBeNull()
    expect(container.querySelector('.sh-thread-group-btn')).toBeNull()
    expect(container.querySelector('.sh-thread-call-buttons')).toBeNull()
    expect(container.querySelector('.sh-thread-history')).toBeNull()
    // The missed-call row still reads, without a "Call back" button.
    expect(container.querySelector('.sh-call-event')).not.toBeNull()
    expect(container.querySelector('.sh-call-event button')).toBeNull()
  })

  it('switching conversationId resets the thread state and loads the new one', async () => {
    const { render, waitFor, fireEvent, ConversationView } = await setup()
    const { container, rerender } = render(
      <ConversationView conversationId="conv-a" embedded />,
    )
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-a')
    }, { timeout: RENDER_WAIT })
    fireEvent.click(
      container.querySelector('[data-msg-id="conv-a-bob"] .sh-message-reply-btn')!,
    )
    fireEvent.click(
      container.querySelector('[data-msg-id="conv-a-mine"] .sh-message-edit-btn')!,
    )
    await waitFor(() => {
      expect(container.querySelector('.sh-composer-reply')).not.toBeNull()
      expect(container.querySelector('.sh-message-edit')).not.toBeNull()
    })

    rerender(<ConversationView conversationId="conv-b" embedded />)
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-b')
    }, { timeout: RENDER_WAIT })
    expect(container.textContent ?? '').not.toContain('BOB-conv-a')
    expect(container.querySelector('.sh-composer-reply')).toBeNull()
    expect(container.querySelector('.sh-message-edit')).toBeNull()
    const urls = apiGet.mock.calls.map(c => c[0])
    expect(urls).toContain('/api/conversations/conv-b')
    expect(urls.some(u => typeof u === 'string'
      && u.startsWith('/api/conversations/conv-b/messages'))).toBe(true)
    // The new thread's metadata drives the header (a group here).
    await waitFor(() => {
      expect(container.querySelector('.sh-thread-group-btn')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
  })

  it('embedded: leaving calls onLeave instead of routing to the chat list', async () => {
    const { render, waitFor, fireEvent, ConversationView } = await setup()
    const onLeave = vi.fn()
    const { container, findByRole } = render(
      <ConversationView conversationId="conv-b" embedded onLeave={onLeave} />,
    )
    await waitFor(() => {
      expect(container.querySelector('.sh-thread-group-btn')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    fireEvent.click(container.querySelector('.sh-thread-group-btn')!)
    await waitFor(() => {
      expect(document.querySelector('.sh-groupinfo-leave button')).not.toBeNull()
    })
    fireEvent.click(document.querySelector('.sh-groupinfo-leave button')!)
    // GroupInfoDialog asks to confirm before leaving.
    fireEvent.click(await findByRole('button', { name: 'Leave' }))
    await waitFor(() => { expect(onLeave).toHaveBeenCalled() }, { timeout: RENDER_WAIT })
    expect(route).not.toHaveBeenCalled()
  })
})
