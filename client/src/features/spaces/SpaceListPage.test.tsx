import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

const apiGet = vi.fn()
// Mock the API module before importing the page
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
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

// Mock auth store
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', username: 'admin', display_name: 'Admin', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 'test-tok' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const SPACES = [
  { id: 'sA', name: 'Choir', space_type: 'private' },
  { id: 'sB', name: 'Band', space_type: 'private' },
]

function wire(unread: Record<string, unknown>) {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces') return SPACES
    if (url === '/api/me/subscriptions') return { subscriptions: [] }
    if (url === '/api/spaces/chat-unread') return { spaces: unread }
    return []
  })
}

const unreadCalls = () => apiGet.mock.calls.filter(c => c[0] === '/api/spaces/chat-unread').length

async function renderPage() {
  const tl = await import('@testing-library/preact')
  const { default: Page } = await import('./SpaceListPage')
  return { ...tl, ...tl.render(<Page />) }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiGet.mockResolvedValue([])
  for (const k of Object.keys(wsHandlers)) delete wsHandlers[k]
})

afterEach(async () => {
  const { cleanup } = await import('@testing-library/preact')
  cleanup()
})

describe('SpaceListPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./SpaceListPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })
})

describe('SpaceListPage — chat unread pill', () => {
  it('shows the honest count per space, with screen-reader text', async () => {
    wire({
      sA: { unread: 2, notif_level: 'all', muted_until: null },
      sB: { unread: 0, notif_level: 'mentions', muted_until: null },
    })
    const r = await renderPage()
    await r.findByText('Choir')
    expect(r.getByText('2 unread in chat')).toBeTruthy()
    expect(r.queryByText(/1 unread in chat/)).toBeNull()
    const pills = r.container.querySelectorAll('.sh-space-card__chat-unread')
    expect(pills).toHaveLength(1)
    expect(pills[0].getAttribute('aria-hidden')).toBe('true')
    expect(pills[0].closest('a')?.getAttribute('href')).toContain('/spaces/sA')
  })

  it('live messages bump it by level; own messages do not', async () => {
    wire({
      sA: { unread: 0, notif_level: 'mentions', muted_until: null },
      sB: { unread: 0, notif_level: 'all', muted_until: null },
    })
    const r = await renderPage()
    await r.findByText('Choir')
    const msg = (space: string, extra: Record<string, unknown> = {}) => ({
      system_scope: 'space', space_id: space, message: { sender_user_id: 'u2' }, ...extra,
    })
    emit('dm.message', msg('sA'))
    emit('dm.message', msg('sB', { message: { sender_user_id: 'u1' } }))
    expect(r.container.querySelectorAll('.sh-space-card__chat-unread')).toHaveLength(0)
    emit('dm.message', msg('sA', { mentions_you: true }))
    emit('dm.message', msg('sB'))
    await r.waitFor(() => expect(r.getAllByText('1 unread in chat')).toHaveLength(2))
  })

  it('a message for a chat the list does not know yet refetches', async () => {
    wire({})
    const r = await renderPage()
    await r.findByText('Choir')
    expect(unreadCalls()).toBe(1)
    wire({ sA: { unread: 1, notif_level: 'all', muted_until: null } })
    emit('dm.message', { system_scope: 'space', space_id: 'sA', message: { sender_user_id: 'u2' } })
    await r.waitFor(() => expect(r.getByText('1 unread in chat')).toBeTruthy())
    expect(unreadCalls()).toBe(2)
  })
})

describe('SpaceListPage — chat unread stays current', () => {
  it('a deleted message in a counted space chat re-reads the counts', async () => {
    wire({ sA: { unread: 2, notif_level: 'all', muted_until: null } })
    const r = await renderPage()
    await r.findByText('2 unread in chat')
    wire({ sA: { unread: 1, notif_level: 'all', muted_until: null } })
    emit('dm.message_deleted', { conversation_id: 'c', system_scope: 'household', message_id: 'm' })
    expect(unreadCalls()).toBe(1)
    emit('dm.message_deleted', { conversation_id: 'c', system_scope: 'space', space_id: 'sA', message_id: 'm' })
    await r.waitFor(() => expect(r.getByText('1 unread in chat')).toBeTruthy())
    expect(unreadCalls()).toBe(2)
  })

  it('a WS reconnect re-reads the counts (frames missed while down)', async () => {
    wire({})
    const r = await renderPage()
    await r.findByText('Choir')
    expect(unreadCalls()).toBe(1)
    wire({ sB: { unread: 4, notif_level: 'all', muted_until: null } })
    conn.state!.value = 'reconnecting'
    conn.state!.value = 'open'
    await r.waitFor(() => expect(r.getByText('4 unread in chat')).toBeTruthy())
    expect(unreadCalls()).toBe(2)
  })
})
