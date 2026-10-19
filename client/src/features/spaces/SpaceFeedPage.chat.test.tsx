/**
 * SpaceFeedPage — the space chat, its own section (``/spaces/s1/chat``).
 *
 * The thread itself is ConversationView's job (its own tests cover
 * delete and ``meta``); here it is a stub that records its props.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.setConfig({ testTimeout: 20_000 })

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const wsHandlers: Record<string, Handler[]> = {}
const conn = vi.hoisted(() => ({ state: null as null | { value: string } }))
vi.mock('@/ws', async () => {
  const { signal } = await import('@preact/signals')
  const state = signal('open')
  conn.state = state
  return {
    connectionState: state,
    ws: {
      on: (type: string, h: Handler) => {
        ;(wsHandlers[type] ??= []).push(h)
        return () => { wsHandlers[type] = (wsHandlers[type] ?? []).filter(x => x !== h) }
      },
      send: vi.fn(),
    },
  }
})
function emit(type: string, data: Record<string, unknown>): void {
  for (const h of wsHandlers[type] ?? []) h({ type, data })
}

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'me', display_name: 'Me', is_admin: false, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 't' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const route = vi.hoisted(() => ({
  section: undefined as string | undefined,
  query: {} as Record<string, string>,
  sig: null as null | { value: string | undefined },
  go: [] as string[],
}))
// The section lives in a signal so a navigation re-renders the page the
// way the router's URL change does.
vi.mock('preact-iso', async () => {
  const { signal } = await import('@preact/signals')
  const sig = signal<string | undefined>(route.section)
  route.sig = sig
  return {
    useRoute: () => ({ params: { id: 's1', section: sig.value }, query: route.query, path: '/spaces/s1' }),
    useLocation: () => ({
      route: (url: string) => {
        route.go.push(url)
        const m = /^\/spaces\/s1(?:\/([a-z]+))?$/.exec(url.split('?')[0])
        route.query = {}
        sig.value = m?.[1]
      },
      url: sig.value ? `/spaces/s1/${sig.value}` : '/spaces/s1',
      path: '/spaces/s1',
      query: route.query,
    }),
  }
})

function goTo(section: string | undefined): void {
  route.section = section
  if (route.sig) route.sig.value = section
}

const viewProps: Array<Record<string, unknown>> = []
vi.mock('@/features/dms/ConversationView', () => ({
  ConversationView: (props: Record<string, unknown>) => {
    viewProps.push(props)
    return <div data-testid="conversation-view">{String(props.conversationId)}</div>
  },
}))
vi.mock('@/features/dms/ConversationMute', () => ({
  MuteSection: (p: { heading?: string }) => <h3>{p.heading}</h3>,
}))

const SUMMARY = {
  enabled: true, conversation_id: 'sc-1', unread: 0,
  notif_level: 'all', muted_until: null, last_read_at: '2026-10-08 12:00:00',
}
const FOREVER = '9999-12-31T23:59:59+00:00'
const summaryCalls = () => apiGet.mock.calls.filter(c => c[0] === '/api/spaces/s1/chat').length

function wire(opts: {
  role?: string | null
  features?: Record<string, unknown>
  summary?: Record<string, unknown> | 'fail'
  archived?: boolean
  /** Only the viewer is a member. */
  solo?: boolean
} = {}) {
  const { role = 'member', features = {}, summary = SUMMARY, archived = false, solo = false } = opts
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Choir', features, archived, my_role: role ?? undefined }
    if (url === '/api/spaces/s1/members') return role ? [{ user_id: 'u1', role }, ...(solo ? [] : [{ user_id: 'u2', role: 'member' }])] : []
    if (url === '/api/spaces/s1/chat') {
      if (summary === 'fail') throw new Error('offline')
      return summary
    }
    if (url === '/api/spaces/s1/links') return { links: [] }
    if (url === '/api/spaces/s1/notif-prefs') return { level: 'all' }
    return []
  })
}

async function renderPage() {
  const tl = await import('@testing-library/preact')
  const { default: Page } = await import('./SpaceFeedPage')
  const r = tl.render(<Page />)
  return { ...tl, ...r }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  route.query = {}
  route.go = []
  goTo(undefined)
  viewProps.length = 0
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
  ;(window as unknown as Record<string, unknown>).ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  }
  HTMLElement.prototype.scrollIntoView = vi.fn()
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
})

const chatLink = (r: { queryByRole: (role: string, o: { name: RegExp }) => HTMLElement | null }) =>
  r.queryByRole('link', { name: /^Chat/ })

describe('SpaceFeedPage — the Chat section', () => {
  it('a member gets Chat right after Feed; /spaces/s1/chat opens the thread', async () => {
    wire()
    const r = await renderPage()
    const chat = await r.findByRole('link', { name: 'Chat' })
    expect(chat.getAttribute('href')).toBe('/spaces/s1/chat')
    const links = Array.from(r.getByRole('navigation', { name: 'Space sections' })
      .querySelectorAll('a')).map((a) => a.textContent)
    expect(links.slice(0, 2)).toEqual(['Feed', 'Chat'])
    expect(r.getByRole('link', { name: 'Feed' }).getAttribute('aria-current')).toBe('page')
    expect(r.queryByTestId('conversation-view')).toBeNull()
    goTo('chat')
    await r.waitFor(() => expect(r.getByTestId('conversation-view').textContent).toBe('sc-1'))
    expect(r.getByRole('link', { name: 'Chat' }).getAttribute('aria-current')).toBe('page')
    expect(r.getByRole('link', { name: 'Feed' }).getAttribute('aria-current')).toBeNull()
    const props = viewProps.at(-1)!
    expect(props).toMatchObject({
      embedded: true, showHeader: false, showGroupInfo: false, allowCalls: false,
      allowAttachments: false, allowDelete: true, canModerate: false,
    })
    expect(props.meta).toMatchObject({ type: 'group_dm', notif_level: 'all', last_read_at: '2026-10-08 12:00:00' })
    // Who can read it is said in the empty thread, not in a bar above it;
    // the thread is a named log.
    expect(props.emptyNote).toBe('No messages yet. Members of Choir can read this chat.')
    expect(props.logLabel).toBe('Chat in Choir')
    expect(r.queryByText('Members of Choir can read this chat.')).toBeNull()
    // No second bell in the chat: its notifications live in More.
    expect(r.container.querySelector('.sh-feed-chat-bar')).toBeNull()
    goTo(undefined)
    await r.waitFor(() => expect(r.queryByTestId('conversation-view')).toBeNull())
  })

  it('a space of one has no Chat; it appears with the second member', async () => {
    wire({ solo: true })
    const r = await renderPage()
    await r.findByRole('link', { name: 'Feed' })
    await r.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/members'))
    await new Promise((res) => setTimeout(res, 20))
    expect(chatLink(r)).toBeNull()
  })

  it('the chat\'s notifications sit next to the space\'s in More', async () => {
    wire({ summary: { ...SUMMARY, muted_until: FOREVER } })
    const r = await renderPage()
    await r.findByRole('link', { name: 'Chat' })
    r.fireEvent.click(r.getByRole('button', { name: /^More/ }))
    const row = await r.findByRole('button', { name: /^Your notifications/ })
    await r.waitFor(() => expect(row.textContent).toContain('Chat muted'))
    r.fireEvent.click(row)
    // One style for both: two radio groups, Posts and Chat; muted, the
    // chat says so with an Unmute.
    expect(r.getByRole('group', { name: 'Posts' })).toBeTruthy()
    expect(r.getByRole('group', { name: 'Chat' })).toBeTruthy()
    expect(r.getByRole('button', { name: 'Unmute' })).toBeTruthy()
  })

  it('the old ?view=chat link moves to /spaces/s1/chat', async () => {
    route.query = { view: 'chat' }
    wire()
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view').textContent).toBe('sc-1'))
    expect(route.go).toContain('/spaces/s1/chat')
  })

  it('an owner / admin / moderator may moderate the chat', async () => {
    goTo('chat')
    wire({ role: 'moderator' })
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view')).toBeTruthy())
    expect(viewProps.at(-1)!.canModerate).toBe(true)
  })

  it('no Chat while the space turned chat off; /chat falls back to the feed', async () => {
    goTo('chat')
    wire({ features: { chat: false } })
    const r = await renderPage()
    await r.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/feed'))
    await r.waitFor(() => expect(route.go).toContain('/spaces/s1'))
    expect(chatLink(r)).toBeNull()
    expect(r.queryByTestId('conversation-view')).toBeNull()
    expect(summaryCalls()).toBe(0)
  })

  it('a follower gets no Chat and never asks for the chat', async () => {
    wire({ role: 'subscriber' })
    const r = await renderPage()
    await r.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/members'))
    await new Promise(res => setTimeout(res, 50))
    expect(chatLink(r)).toBeNull()
    expect(summaryCalls()).toBe(0)
  })

  it('a non-member (public space visitor) gets no Chat', async () => {
    wire({ role: null })
    const r = await renderPage()
    await r.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/members'))
    await new Promise(res => setTimeout(res, 50))
    expect(chatLink(r)).toBeNull()
    expect(summaryCalls()).toBe(0)
  })

  it('the server saying "off" hides Chat', async () => {
    wire({ summary: { ...SUMMARY, enabled: false, conversation_id: null } })
    const r = await renderPage()
    await r.waitFor(() => expect(summaryCalls()).toBe(1))
    await r.waitFor(() => expect(chatLink(r)).toBeNull())
  })

  it('a failed summary offers Retry in the chat section', async () => {
    goTo('chat')
    wire({ summary: 'fail' })
    const r = await renderPage()
    const retry = await r.findByRole('button', { name: 'Retry' })
    wire()
    r.fireEvent.click(retry)
    await r.waitFor(() => expect(r.getByTestId('conversation-view').textContent).toBe('sc-1'))
  })

  it('unread: a quiet count on Chat, read as "Chat, 2 unread"; live frames for this space count', async () => {
    wire({ summary: { ...SUMMARY, unread: 2 } })
    const r = await renderPage()
    const chat = await r.findByRole('link', { name: 'Chat, 2 unread' })
    expect(chat.querySelector('.sh-space-badge--unread')?.textContent).toBe('2')
    // Another space's chat, the household chat and the viewer's own
    // message don't count.
    emit('dm.message', { system_scope: 'space', space_id: 's2', message: { sender_user_id: 'u2' } })
    emit('dm.message', { system_scope: 'household', space_id: null, message: { sender_user_id: 'u2' } })
    emit('dm.message', { system_scope: 'space', space_id: 's1', message: { sender_user_id: 'u1' } })
    emit('dm.message', { system_scope: 'space', space_id: 's1', message: { sender_user_id: 'u2' } })
    await r.findByRole('link', { name: 'Chat, 3 unread' })
    // Opening the chat clears the badge.
    goTo('chat')
    await r.waitFor(() => expect(r.getByRole('link', { name: 'Chat' })).toBeTruthy())
  })

  it('unread: none while muted; at "Only @mentions" only frames that mention me', async () => {
    wire({ summary: { ...SUMMARY, unread: 4, muted_until: FOREVER } })
    const r = await renderPage()
    await r.waitFor(() => expect(summaryCalls()).toBe(1))
    const chat = await r.findByRole('link', { name: 'Chat' })
    expect(chat.textContent).toBe('Chat')
    r.cleanup()
    wsHandlersReset()
    wire({ summary: { ...SUMMARY, unread: 1, notif_level: 'mentions' } })
    const r2 = await renderPage()
    await r2.findByRole('link', { name: 'Chat, 1 unread' })
    emit('dm.message', { system_scope: 'space', space_id: 's1', message: { sender_user_id: 'u2' } })
    emit('dm.message', {
      system_scope: 'space', space_id: 's1', mentions_you: false, message: { sender_user_id: 'u2' },
    })
    await new Promise(res => setTimeout(res, 20))
    expect(r2.getByRole('link', { name: 'Chat, 1 unread' })).toBeTruthy()
    emit('dm.message', {
      system_scope: 'space', space_id: 's1', mentions_you: true, message: { sender_user_id: 'u2' },
    })
    await r2.findByRole('link', { name: 'Chat, 2 unread' })
  })

  it('re-reads the summary on a WS reconnect and on leaving the chat', async () => {
    goTo('chat')
    wire()
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view')).toBeTruthy())
    expect(summaryCalls()).toBe(1)
    conn.state!.value = 'reconnecting'
    conn.state!.value = 'open'
    await r.waitFor(() => expect(summaryCalls()).toBe(2))
    goTo(undefined)
    await r.waitFor(() => expect(summaryCalls()).toBe(3))
  })

  it('an archived space shows its chat read-only', async () => {
    goTo('chat')
    wire({ archived: true })
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view')).toBeTruthy())
    await r.waitFor(() => expect(viewProps.at(-1)!.readOnly).toBe(true))
    expect(viewProps.at(-1)).toMatchObject({
      readOnly: true,
      readOnlyNote: 'This space is archived. The chat is read-only.',
      // Deleting stays: the server still takes it in an archived space.
      allowDelete: true,
    })
  })

  it('a live space keeps the composer', async () => {
    goTo('chat')
    wire()
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view')).toBeTruthy())
    expect(viewProps.at(-1)!.readOnly).toBe(false)
  })
})

function wsHandlersReset(): void {
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
}
