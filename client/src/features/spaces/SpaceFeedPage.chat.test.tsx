/**
 * SpaceFeedPage — the Feed tab's Feed | Chat switch (space chat).
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
  query: {} as Record<string, string>,
  sig: null as null | { value: Record<string, string> },
}))
// The query lives in a signal so a switch click re-renders the page the
// way the router's URL change does.
vi.mock('preact-iso', async () => {
  const { signal } = await import('@preact/signals')
  const sig = signal(route.query)
  route.sig = sig
  return {
    useRoute: () => ({ params: { id: 's1' }, query: sig.value, path: '/spaces/s1' }),
    useLocation: () => ({
      route: (url: string) => {
        const q = url.split('?')[1] ?? ''
        route.query = Object.fromEntries(new URLSearchParams(q))
        sig.value = route.query
      },
      url: sig.value.view ? `/spaces/s1?view=${sig.value.view}` : '/spaces/s1',
      path: '/spaces/s1',
      query: sig.value,
    }),
  }
})

function setQuery(q: Record<string, string>): void {
  route.query = q
  if (route.sig) route.sig.value = q
}

const viewProps: Array<Record<string, unknown>> = []
vi.mock('@/features/dms/ConversationView', () => ({
  ConversationView: (props: Record<string, unknown>) => {
    viewProps.push(props)
    return <div data-testid="conversation-view">{String(props.conversationId)}</div>
  },
}))
vi.mock('@/features/dms/ConversationMute', () => ({
  MuteButton: () => <button type="button">mute</button>,
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
} = {}) {
  const { role = 'member', features = {}, summary = SUMMARY, archived = false } = opts
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/s1') return { id: 's1', name: 'Choir', features, archived }
    if (url === '/api/spaces/s1/members') return role ? [{ user_id: 'u1', role }] : []
    if (url === '/api/spaces/s1/chat') {
      if (summary === 'fail') throw new Error('offline')
      return summary
    }
    if (url === '/api/spaces/s1/links') return { links: [] }
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
  setQuery({})
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

describe('SpaceFeedPage — Feed | Chat switch', () => {
  it('a member gets the switch, posts first; Chat opens the thread', async () => {
    wire()
    const r = await renderPage()
    const chat = await r.findByRole('button', { name: 'Chat' })
    const feed = r.getByRole('button', { name: 'Feed' })
    expect(r.getByRole('group', { name: 'Feed or chat' })).toBeTruthy()
    expect(feed.getAttribute('aria-pressed')).toBe('true')
    expect(r.queryByTestId('conversation-view')).toBeNull()
    r.fireEvent.click(chat)
    await r.waitFor(() => expect(r.getByTestId('conversation-view').textContent).toBe('sc-1'))
    expect(route.query).toEqual({ view: 'chat' })
    const props = viewProps.at(-1)!
    expect(props).toMatchObject({
      embedded: true, showHeader: false, showGroupInfo: false, allowCalls: false,
      allowAttachments: false, allowDelete: true, canModerate: false,
    })
    expect(props.meta).toMatchObject({ type: 'group_dm', notif_level: 'all', last_read_at: '2026-10-08 12:00:00' })
    expect(r.getByText('Members of Choir can read this chat.')).toBeTruthy()
    // Back to the posts drops the query.
    r.fireEvent.click(r.getByRole('button', { name: 'Feed' }))
    await r.waitFor(() => expect(r.queryByTestId('conversation-view')).toBeNull())
    expect(route.query).toEqual({})
  })

  it('?view=chat deep-links straight into the chat', async () => {
    setQuery({ view: 'chat' })
    wire()
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view').textContent).toBe('sc-1'))
    expect(r.getByRole('button', { name: 'Chat' }).getAttribute('aria-pressed')).toBe('true')
  })

  it('an owner / admin / moderator may moderate the chat', async () => {
    setQuery({ view: 'chat' })
    wire({ role: 'moderator' })
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view')).toBeTruthy())
    expect(viewProps.at(-1)!.canModerate).toBe(true)
  })

  it('no switch while the space turned chat off; ?view=chat shows the posts', async () => {
    setQuery({ view: 'chat' })
    wire({ features: { chat: false } })
    const r = await renderPage()
    await r.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/feed'))
    await r.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/members'))
    await new Promise(res => setTimeout(res, 50))
    expect(r.queryByRole('group', { name: 'Feed or chat' })).toBeNull()
    expect(r.queryByTestId('conversation-view')).toBeNull()
    expect(summaryCalls()).toBe(0)
  })

  it('a follower gets no switch and never asks for the chat', async () => {
    wire({ role: 'subscriber' })
    const r = await renderPage()
    await r.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/members'))
    await new Promise(res => setTimeout(res, 50))
    expect(r.queryByRole('group', { name: 'Feed or chat' })).toBeNull()
    expect(summaryCalls()).toBe(0)
  })

  it('a non-member (public space visitor) gets no switch', async () => {
    wire({ role: null })
    const r = await renderPage()
    await r.waitFor(() => expect(apiGet).toHaveBeenCalledWith('/api/spaces/s1/members'))
    await new Promise(res => setTimeout(res, 50))
    expect(r.queryByRole('group', { name: 'Feed or chat' })).toBeNull()
    expect(summaryCalls()).toBe(0)
  })

  it('the server saying "off" hides the switch', async () => {
    wire({ summary: { ...SUMMARY, enabled: false, conversation_id: null } })
    const r = await renderPage()
    await r.waitFor(() => expect(summaryCalls()).toBe(1))
    await new Promise(res => setTimeout(res, 50))
    expect(r.queryByRole('group', { name: 'Feed or chat' })).toBeNull()
  })

  it('a failed summary offers Retry in the chat view', async () => {
    setQuery({ view: 'chat' })
    wire({ summary: 'fail' })
    const r = await renderPage()
    const retry = await r.findByRole('button', { name: 'Retry' })
    wire()
    r.fireEvent.click(retry)
    await r.waitFor(() => expect(r.getByTestId('conversation-view').textContent).toBe('sc-1'))
  })

  it('unread: the server count on Chat; live frames for this space count', async () => {
    wire({ summary: { ...SUMMARY, unread: 2 } })
    const r = await renderPage()
    await r.findByRole('button', { name: /Chat.*2 unread/ })
    // Another space's chat, the household chat and the viewer's own
    // message don't count.
    emit('dm.message', { system_scope: 'space', space_id: 's2', message: { sender_user_id: 'u2' } })
    emit('dm.message', { system_scope: 'household', space_id: null, message: { sender_user_id: 'u2' } })
    emit('dm.message', { system_scope: 'space', space_id: 's1', message: { sender_user_id: 'u1' } })
    emit('dm.message', { system_scope: 'space', space_id: 's1', message: { sender_user_id: 'u2' } })
    await r.findByRole('button', { name: /Chat.*3 unread/ })
    // Opening the chat clears the badge.
    r.fireEvent.click(r.getByRole('button', { name: /Chat/ }))
    await r.waitFor(() => expect(r.getByRole('button', { name: 'Chat' })).toBeTruthy())
  })

  it('unread: none while muted; at "Only @mentions" only frames that mention me', async () => {
    wire({ summary: { ...SUMMARY, unread: 4, muted_until: FOREVER } })
    const r = await renderPage()
    await r.waitFor(() => expect(summaryCalls()).toBe(1))
    const chat = await r.findByRole('button', { name: 'Chat' })
    expect(chat.textContent).toBe('Chat')
    r.cleanup()
    wsHandlersReset()
    wire({ summary: { ...SUMMARY, unread: 1, notif_level: 'mentions' } })
    const r2 = await renderPage()
    await r2.findByRole('button', { name: /Chat.*1 unread/ })
    emit('dm.message', { system_scope: 'space', space_id: 's1', message: { sender_user_id: 'u2' } })
    emit('dm.message', {
      system_scope: 'space', space_id: 's1', mentions_you: false, message: { sender_user_id: 'u2' },
    })
    await new Promise(res => setTimeout(res, 20))
    expect(r2.getByRole('button', { name: /Chat.*1 unread/ })).toBeTruthy()
    emit('dm.message', {
      system_scope: 'space', space_id: 's1', mentions_you: true, message: { sender_user_id: 'u2' },
    })
    await r2.findByRole('button', { name: /Chat.*2 unread/ })
  })

  it('re-reads the summary on a WS reconnect and on leaving the chat', async () => {
    setQuery({ view: 'chat' })
    wire()
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view')).toBeTruthy())
    expect(summaryCalls()).toBe(1)
    conn.state!.value = 'reconnecting'
    conn.state!.value = 'open'
    await r.waitFor(() => expect(summaryCalls()).toBe(2))
    r.fireEvent.click(r.getByRole('button', { name: 'Feed' }))
    await r.waitFor(() => expect(summaryCalls()).toBe(3))
  })

  it('an archived space shows its chat read-only', async () => {
    setQuery({ view: 'chat' })
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
    setQuery({ view: 'chat' })
    wire()
    const r = await renderPage()
    await r.waitFor(() => expect(r.getByTestId('conversation-view')).toBeTruthy())
    expect(viewProps.at(-1)!.readOnly).toBe(false)
  })
})

function wsHandlersReset(): void {
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
}
