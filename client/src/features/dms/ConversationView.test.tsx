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
  it('a failed message load shows an error with Try again, not an empty thread', async () => {
    // A rate-limited (429) or failed load used to blank the thread, which
    // read as "no messages". It now says so and offers a retry.
    let fail = true
    const wired = apiGet.getMockImplementation()!
    apiGet.mockImplementation(async (url: string) => {
      if (fail && url.startsWith('/api/conversations/conv-a/messages')) {
        throw new Error('API 429: Too many requests')
      }
      return wired(url)
    })
    const { render, waitFor, fireEvent, ConversationView } = await setup()
    const { container, queryByTestId, getByText } = render(
      <ConversationView conversationId="conv-a" />,
    )
    await waitFor(() => {
      expect(queryByTestId('thread-load-error')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    expect(queryByTestId('thread-load-error')!.getAttribute('role')).toBe('alert')
    expect(container.textContent ?? '').toContain("Couldn't load this conversation.")
    fail = false
    fireEvent.click(getByText('Try again'))
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-a')
    }, { timeout: RENDER_WAIT })
    expect(queryByTestId('thread-load-error')).toBeNull()
  })

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

describe('ConversationView — dm.active ownership', () => {
  const activeCalls = (send: ReturnType<typeof vi.fn>) =>
    send.mock.calls.filter(c => c[0] === 'dm.active').map(c => c[1])

  it('the last view to open claims it; an earlier one unmounting leaves the claim alone', async () => {
    const { render, ConversationView } = await setup()
    const { ws } = await import('@/ws')
    const send = ws.send as unknown as ReturnType<typeof vi.fn>
    send.mockClear()
    const first = render(<ConversationView conversationId="conv-a" embedded />)
    const second = render(<ConversationView conversationId="conv-b" embedded />)
    expect(activeCalls(send).at(-1)).toEqual({ conversation_id: 'conv-b' })

    send.mockClear()
    first.unmount()
    // conv-b is still on screen — the backend must keep suppressing it.
    expect(activeCalls(send)).toEqual([])

    second.unmount()
    expect(activeCalls(send)).toEqual([{ conversation_id: null }])
  })

  it('the owner unmounting clears it', async () => {
    const { render, ConversationView } = await setup()
    const { ws } = await import('@/ws')
    const send = ws.send as unknown as ReturnType<typeof vi.fn>
    send.mockClear()
    const only = render(<ConversationView conversationId="conv-a" />)
    expect(activeCalls(send)).toEqual([{ conversation_id: 'conv-a' }])
    send.mockClear()
    only.unmount()
    expect(activeCalls(send)).toEqual([{ conversation_id: null }])
  })
})

describe('ConversationView — composer emoji picker', () => {
  const pickerOpen = (root: Element) =>
    root.querySelector('.sh-composer-emoji-inline')?.getAttribute('aria-expanded') === 'true'

  it('does not pop back open when returning to a thread', async () => {
    const { render, waitFor, fireEvent, ConversationView } = await setup()
    const { container, rerender } = render(
      <ConversationView conversationId="conv-a" embedded />,
    )
    await waitFor(() => {
      expect(container.querySelector('.sh-composer-emoji-inline')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    fireEvent.click(container.querySelector('.sh-composer-emoji-inline')!)
    await waitFor(() => { expect(pickerOpen(container)).toBe(true) })

    rerender(<ConversationView conversationId="conv-b" embedded />)
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-b')
    }, { timeout: RENDER_WAIT })
    rerender(<ConversationView conversationId="conv-a" embedded />)
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('BOB-conv-a')
    }, { timeout: RENDER_WAIT })
    expect(pickerOpen(container)).toBe(false)
  })

  it('does not pop back open after the view unmounts and remounts', async () => {
    const { render, waitFor, fireEvent, ConversationView } = await setup()
    const first = render(<ConversationView conversationId="conv-a" embedded />)
    await waitFor(() => {
      expect(first.container.querySelector('.sh-composer-emoji-inline')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    fireEvent.click(first.container.querySelector('.sh-composer-emoji-inline')!)
    await waitFor(() => { expect(pickerOpen(first.container)).toBe(true) })
    first.unmount()

    const second = render(<ConversationView conversationId="conv-a" embedded />)
    await waitFor(() => {
      expect(second.container.textContent ?? '').toContain('BOB-conv-a')
    }, { timeout: RENDER_WAIT })
    expect(pickerOpen(second.container)).toBe(false)
  })
})

describe('ConversationView — embedded, after leaving', () => {
  it('without onLeave shows a neutral note instead of a dead thread', async () => {
    const { render, waitFor, fireEvent, ConversationView } = await setup()
    const { container, findByRole } = render(
      <ConversationView conversationId="conv-b" embedded />,
    )
    await waitFor(() => {
      expect(container.querySelector('.sh-thread-group-btn')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    fireEvent.click(container.querySelector('.sh-thread-group-btn')!)
    await waitFor(() => {
      expect(document.querySelector('.sh-groupinfo-leave button')).not.toBeNull()
    })
    fireEvent.click(document.querySelector('.sh-groupinfo-leave button')!)
    fireEvent.click(await findByRole('button', { name: 'Leave' }))
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('You’re no longer in this conversation.')
    }, { timeout: RENDER_WAIT })
    expect(container.querySelector('form.sh-composer')).toBeNull()
    expect(container.querySelector('[data-msg-id]')).toBeNull()
    expect(route).not.toHaveBeenCalled()
  })
})

describe('ConversationView — older history across a switch', () => {
  it('an older-history page in flight when conversationId switches never lands in the new thread', async () => {
    const fullPage = (id: string) => Array.from({ length: 25 }).map(
      (_, i) => msgRow(`${id}-${24 - i}`, `${id} body ${24 - i}`),
    )
    let releaseOlderA: (rows: unknown[]) => void = () => {}
    const slowOlderA = new Promise<unknown[]>(res => { releaseOlderA = res })
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/conversations/conv-a') return convRow('conv-a')
      if (url === '/api/conversations/conv-b') return convRow('conv-b')
      if (url.startsWith('/api/conversations/conv-a/messages')) {
        return url.includes('before=') ? slowOlderA : fullPage('THREAD-A')
      }
      if (url.startsWith('/api/conversations/conv-b/messages')) {
        return url.includes('before=') ? [] : [msgRow('msg-b', 'THREAD-B only')]
      }
      if (url.endsWith('/members')) return roster.slice(0, 2)
      return {}
    })
    const { render, waitFor, ConversationView } = await setup()
    const { container, rerender } = render(
      <ConversationView conversationId="conv-a" embedded />,
    )
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('THREAD-A body 0')
    }, { timeout: RENDER_WAIT })
    // jsdom does no layout, so the container reads as "at the top" and
    // a scroll event arms ``loadOlder``.
    container.querySelector('.sh-messages')!.dispatchEvent(new Event('scroll'))
    await waitFor(() => {
      expect(apiGet.mock.calls.some(([u]) => typeof u === 'string'
        && u.startsWith('/api/conversations/conv-a/messages?before='))).toBe(true)
    }, { timeout: RENDER_WAIT })

    rerender(<ConversationView conversationId="conv-b" embedded />)
    await waitFor(() => {
      expect(container.textContent ?? '').toContain('THREAD-B only')
    }, { timeout: RENDER_WAIT })
    releaseOlderA([msgRow('msg-a-old', 'THREAD-A-OLDER from the thread we left')])
    await new Promise(r => setTimeout(r, 50))
    expect(container.textContent ?? '').toContain('THREAD-B only')
    expect(container.textContent ?? '').not.toContain('THREAD-A')
    expect(container.querySelectorAll('[data-msg-id]').length).toBe(1)
    expect(container.querySelector('.sh-dm-load-older')).toBeNull()
  })
})

describe('ConversationView — host-given meta (system chats)', () => {
  /** The household chat: ``GET /api/conversations/{id}`` 404s for it,
   *  the rest of the per-conversation routes answer. */
  function wireSystemChat(): void {
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/conversations/sys-h') {
        throw Object.assign(new Error('not found'), { status: 404 })
      }
      if (url.startsWith('/api/conversations/sys-h/messages')) {
        return [msgRow('sys-bob', 'HELLO-HOUSEHOLD')]
      }
      if (url === '/api/conversations/sys-h/members') return roster
      return {}
    })
  }
  const META = {
    type: 'group_dm', name: null, managed_here: false,
    muted_until: null, notif_level: 'mentions' as const, unread: 1,
  }

  it('never fetches the conversation row and renders from meta', async () => {
    wireSystemChat()
    const { render, waitFor, ConversationView } = await setup()
    const { container, getByText } = render(
      <ConversationView conversationId="sys-h" embedded meta={META} />,
    )
    await waitFor(() => expect(getByText('HELLO-HOUSEHOLD')).toBeTruthy(),
      { timeout: RENDER_WAIT })
    // A group thread from meta: the sender's name shows, and the header
    // bell reflects the meta's level.
    await waitFor(() => expect(getByText('Bob')).toBeTruthy())
    await waitFor(() => {
      expect(container.querySelector('.sh-thread-mute-btn--mentions')).not.toBeNull()
    })
    const urls = apiGet.mock.calls.map(c => c[0])
    expect(urls).not.toContain('/api/conversations/sys-h')
    expect(urls).toContain('/api/conversations/sys-h/members')
  })

  it('follows a later meta change without refetching', async () => {
    wireSystemChat()
    const { render, waitFor, ConversationView } = await setup()
    const { container, rerender } = render(
      <ConversationView conversationId="sys-h" embedded meta={META} />,
    )
    await waitFor(() => {
      expect(container.querySelector('.sh-thread-mute-btn--mentions')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
    rerender(
      <ConversationView conversationId="sys-h" embedded
        meta={{ ...META, muted_until: '9999-12-31T23:59:59+00:00' }} />,
    )
    await waitFor(() => {
      expect(container.querySelector('.sh-thread-mute-btn--muted')).not.toBeNull()
    })
    expect(apiGet.mock.calls.map(c => c[0])).not.toContain('/api/conversations/sys-h')
  })
})
