/**
 * FeedPage — the Feed | Chat tabs (household chat).
 *
 * The thread itself is ConversationView's job (its own tests cover the
 * ``meta`` prop); here it is a stub that records its props.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'

vi.setConfig({ testTimeout: 20_000 })

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn().mockResolvedValue({}),
    patch: vi.fn().mockResolvedValue({}),
    put: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const wsHandlers: Record<string, Handler[]> = {}
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      ;(wsHandlers[type] ??= []).push(h)
      return () => {
        wsHandlers[type] = (wsHandlers[type] ?? []).filter(x => x !== h)
      }
    },
    send: vi.fn(),
  },
}))
function emit(type: string, data: Record<string, unknown>): void {
  for (const h of wsHandlers[type] ?? []) h({ type, data })
}

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-me', username: 'me', display_name: 'Me', is_admin: true, picture_url: null, bio: null, is_new_member: false } },
  token: { value: 't' },
  isAuthed: { value: true },
  setToken: vi.fn(),
  logout: vi.fn(),
}))

const viewProps: Array<Record<string, unknown>> = []
vi.mock('@/features/dms/ConversationView', () => ({
  ConversationView: (props: Record<string, unknown>) => {
    viewProps.push(props)
    return <div data-testid="conversation-view">{String(props.conversationId)}</div>
  },
}))

vi.mock('@/features/welcome/WelcomePage', () => ({
  default: () => <div data-testid="welcome-page" />,
}))

const TOGGLES = {
  feat_feed: true, feat_pages: true, feat_tasks: true, feat_stickies: true,
  feat_calendar: true, feat_presence: true, feat_gallery: true, feat_timetable: true,
  feat_household_chat: true,
  allow_text: true, allow_image: true, allow_video: true, allow_file: true,
  allow_poll: true, allow_schedule: true, allow_highlight_share: true,
  allow_link_preview: true, household_name: 'Home',
}
const SUMMARY = {
  enabled: true, conversation_id: 'hc-1', unread: 0,
  notif_level: 'all', muted_until: null,
}

function wireApi(summary: Record<string, unknown> = SUMMARY): void {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/household/chat') return summary
    if (url === '/api/household/preferences') return TOGGLES
    return []
  })
}

async function renderAt(
  url: string,
  toggleState: Partial<typeof TOGGLES> | null = {},
  page: 'feed' | 'landing' = 'feed',
) {
  const { toggles } = await import('@/components/HouseholdToggles')
  toggles.value = toggleState === null ? null : { ...TOGGLES, ...toggleState }
  window.history.replaceState(null, '', url)
  const tl = await import('@testing-library/preact')
  const { LocationProvider } = await import('preact-iso')
  const Page = page === 'feed'
    ? (await import('./FeedPage')).default
    : (await import('./LandingDispatch')).default
  return { ...tl, ...tl.render(<LocationProvider><Page /></LocationProvider>) }
}

beforeEach(async () => {
  vi.resetModules()
  apiGet.mockReset()
  wireApi()
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

describe('FeedPage — Feed | Chat tabs', () => {
  it('shows both tabs, the feed by default', async () => {
    const { findByRole, getByRole, queryByTestId } = await renderAt('/feed')
    expect((await findByRole('tab', { name: 'Feed' })).getAttribute('aria-selected')).toBe('true')
    expect(getByRole('tab', { name: 'Chat' }).getAttribute('aria-selected')).toBe('false')
    expect(queryByTestId('conversation-view')).toBeNull()
  })

  it('toggle off: no tab strip, ?tab=chat falls back to the feed, no summary fetch', async () => {
    const { queryByRole, queryByTestId, waitFor } = await renderAt(
      '/feed?tab=chat', { feat_household_chat: false },
    )
    await waitFor(() => expect(queryByRole('tablist', { name: 'Home sections' })).toBeNull())
    expect(queryByTestId('conversation-view')).toBeNull()
    expect(apiGet.mock.calls.map(c => c[0])).not.toContain('/api/household/chat')
  })

  it('a server without the toggle (older) shows no Chat tab', async () => {
    const { toggles } = await import('@/components/HouseholdToggles')
    const { feat_household_chat: _drop, ...older } = TOGGLES
    void _drop
    const { queryByRole } = await renderAt('/feed', {})
    toggles.value = older as typeof toggles.value
    const { waitFor } = await import('@testing-library/preact')
    await waitFor(() => expect(queryByRole('tab', { name: 'Chat' })).toBeNull())
  })

  it('enabled:false from the server hides the tab strip', async () => {
    wireApi({ enabled: false, conversation_id: null, unread: 0, notif_level: null, muted_until: null })
    const { queryByRole, waitFor, queryByTestId } = await renderAt('/feed?tab=chat')
    await waitFor(() => expect(queryByRole('tab', { name: 'Chat' })).toBeNull())
    expect(queryByTestId('conversation-view')).toBeNull()
  })

  it('/feed?tab=chat opens the chat with the conversation id and meta', async () => {
    wireApi({ ...SUMMARY, notif_level: 'mentions', unread: 2 })
    const { findByTestId, getByRole } = await renderAt('/feed?tab=chat')
    expect((await findByTestId('conversation-view')).textContent).toBe('hc-1')
    expect(getByRole('tab', { name: /Chat/ }).getAttribute('aria-selected')).toBe('true')
    const props = viewProps[viewProps.length - 1]
    expect(props.embedded).toBe(true)
    expect(props.showHeader).toBe(false)
    expect(props.allowCalls).toBe(false)
    expect(props.showGroupInfo).toBe(false)
    expect(props.meta).toMatchObject({
      type: 'group_dm', notif_level: 'mentions', muted_until: null, unread: 2,
    })
    // The chat's metadata comes from the summary, never the item route
    // (which 404s for system chats).
    expect(apiGet.mock.calls.map(c => c[0])).not.toContain('/api/conversations/hc-1')
    // The chat sits outside the feed's pull-to-refresh.
    expect(document.querySelector('.sh-ptr [data-testid="conversation-view"]')).toBeNull()
  })

  it('/?tab=chat (the notification link) lands on the chat via LandingDispatch', async () => {
    const { findByTestId, queryByTestId } = await renderAt('/?tab=chat', {}, 'landing')
    expect((await findByTestId('conversation-view')).textContent).toBe('hc-1')
    expect(queryByTestId('welcome-page')).toBeNull()
  })

  it('/ without ?tab=chat still honours the landing preference', async () => {
    const { findByTestId } = await renderAt('/', {}, 'landing')
    expect(await findByTestId('welcome-page')).toBeTruthy()
  })

  it('switching tabs at /feed replaces history (no back-button trail)', async () => {
    const push = vi.spyOn(window.history, 'pushState')
    const replace = vi.spyOn(window.history, 'replaceState')
    const { findByRole, fireEvent, waitFor, findByTestId, queryByTestId } =
      await renderAt('/feed?tab=chat')
    await findByTestId('conversation-view')
    replace.mockClear()
    fireEvent.click(await findByRole('tab', { name: 'Feed' }))
    await waitFor(() => expect(queryByTestId('conversation-view')).toBeNull())
    expect(window.location.pathname).toBe('/feed')
    expect(window.location.search).toBe('')
    fireEvent.click(await findByRole('tab', { name: /Chat/ }))
    await findByTestId('conversation-view')
    expect(window.location.pathname).toBe('/feed')
    expect(window.location.search).toBe('?tab=chat')
    expect(replace).toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
    push.mockRestore()
    replace.mockRestore()
  })

  it('at / with the feed as landing page, the tabs stay on /', async () => {
    const { currentUser } = await import('@/store/auth')
    const me = currentUser.value as unknown as Record<string, unknown>
    me.preferences_json = JSON.stringify({ landing_path: '/feed' })
    try {
      const { findByRole, fireEvent, waitFor, queryByTestId, findByTestId } =
        await renderAt('/', {}, 'landing')
      fireEvent.click(await findByRole('tab', { name: /Chat/ }))
      await findByTestId('conversation-view')
      expect(window.location.pathname).toBe('/')
      expect(window.location.search).toBe('?tab=chat')
      fireEvent.click(await findByRole('tab', { name: 'Feed' }))
      await waitFor(() => expect(queryByTestId('conversation-view')).toBeNull())
      expect(window.location.pathname).toBe('/')
      expect(window.location.search).toBe('')
    } finally {
      delete me.preferences_json
    }
  })

  it('at /?tab=chat over the welcome landing, the Feed tab goes to /feed', async () => {
    const push = vi.spyOn(window.history, 'pushState')
    const { findByRole, fireEvent, waitFor, findByTestId } =
      await renderAt('/?tab=chat', {}, 'landing')
    await findByTestId('conversation-view')
    fireEvent.click(await findByRole('tab', { name: 'Feed' }))
    await waitFor(() => expect(window.location.pathname).toBe('/feed'))
    expect(window.location.search).toBe('')
    expect(push).not.toHaveBeenCalled()
    push.mockRestore()
  })

  it('a household chat frame shows an unread pill on Chat; viewing clears it', async () => {
    const { findByRole, fireEvent, waitFor, getByRole } = await renderAt('/feed')
    const chatTab = await findByRole('tab', { name: 'Chat' })
    // Summary loaded (unread 0) before frames count.
    await waitFor(() => {
      expect(apiGet.mock.calls.map(c => c[0])).toContain('/api/household/chat')
    })
    const { householdChat } = await import('@/store/householdChat')
    await waitFor(() => expect(householdChat.value?.conversation_id).toBe('hc-1'))

    // A person-made DM, and my own household message: no pill.
    emit('dm.message', { conversation_id: 'dm-1', system_scope: null, message: { id: 'm0', sender_user_id: 'u-bob' } })
    emit('dm.message', { conversation_id: 'hc-1', system_scope: 'household', message: { id: 'm1', sender_user_id: 'u-me' } })
    expect(chatTab.querySelector('.sh-tab-unread')).toBeNull()

    emit('dm.message', { conversation_id: 'hc-1', system_scope: 'household', message: { id: 'm2', sender_user_id: 'u-bob' } })
    emit('dm.message', { conversation_id: 'hc-1', system_scope: 'household', message: { id: 'm3', sender_user_id: 'u-cat' } })
    await waitFor(() => {
      expect(getByRole('tab', { name: /Chat/ }).querySelector('.sh-tab-unread')?.textContent).toBe('2')
    })
    expect(getByRole('tab', { name: /Chat/ }).textContent).toContain('2 unread')

    fireEvent.click(getByRole('tab', { name: /Chat/ }))
    await waitFor(() => {
      expect(getByRole('tab', { name: 'Chat' }).querySelector('.sh-tab-unread')).toBeNull()
    })
    expect(householdChat.value?.unread).toBe(0)
  })

  it('the summary unread shows on the Chat tab on arrival', async () => {
    wireApi({ ...SUMMARY, unread: 4 })
    const { findByRole, waitFor } = await renderAt('/feed')
    await findByRole('tab', { name: /Feed/ })
    await waitFor(async () => {
      const tab = await findByRole('tab', { name: /Chat/ })
      expect(tab.querySelector('.sh-tab-unread')?.textContent).toBe('4')
    })
  })

  it('a failed summary shows a retry on the Chat tab', async () => {
    let fail = true
    apiGet.mockImplementation(async (url: string) => {
      if (url === '/api/household/chat') {
        if (fail) throw new Error('boom')
        return SUMMARY
      }
      return []
    })
    const { findByText, findByRole, fireEvent, findByTestId } = await renderAt('/feed?tab=chat')
    expect(await findByText("Couldn't load the household chat. Try again.")).toBeTruthy()
    fail = false
    fireEvent.click(await findByRole('button', { name: 'Retry' }))
    expect((await findByTestId('conversation-view')).textContent).toBe('hc-1')
  })
})
