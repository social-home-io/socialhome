import { describe, it, expect, vi, beforeEach } from 'vitest'
import type { JSX } from 'preact'

// The heavy DmThreadPage cold render (fresh ``import()`` + mocked-API
// microtask chain + layout effects) can take several seconds under the full
// parallel suite on CI. TWO ceilings have to clear it or these tests flake:
//   1. vitest's per-test timeout — defaults to 5 s, which *kills the whole
//      test* ("Test timed out in 5000ms") before any inner ``waitFor`` can
//      help. Raise it for this file.
//   2. the ``waitFor`` budget below — must sit *under* the per-test timeout so
//      a genuinely-stuck wait fails with a useful assertion rather than the
//      opaque test-timeout error.
// Both resolve as soon as the condition holds, so the generous ceilings cost
// nothing on a fast run — they only add headroom under load.
vi.setConfig({ testTimeout: 20_000 })
const RENDER_WAIT = 15_000

// NOTE: ``@/api`` and ``@/store/auth`` are mocked ONCE each, further down
// next to the fixtures they serve. This file used to register a second,
// competing factory for both up here — an easy mistake, because
// ``vi.mock`` is hoisted so the two registrations look far apart in the
// source but land on the same module id. The first ``@/api`` factory
// hardwired ``get`` to ``mockResolvedValue([])``, which no test could
// steer; whenever the registry served that one instead of the
// delegating factory below, every fetch resolved empty, the thread
// rendered no messages, and the four "jump-down chip integration"
// tests burned their full waitFor budget. It presented as a CI-only
// flake for months (see the ceilings above and the fork cap in
// vitest.config.ts, both of which were attempts at this symptom).
// Keep exactly one factory per module id.

describe('DmThreadPage', () => {
  it('module exports a default component', async () => {
    const mod = await import('./DmThreadPage')
    expect(mod.default).toBeTruthy()
    expect(typeof mod.default).toBe('function')
  })
})

describe('isAtLiveEdge', () => {
  // The "live edge" threshold (80 px) is the shared input to two
  // call sites: the user-scroll handler (``handleScroll``) and the
  // notification-driven entry effect (the anchor-scroll
  // ``useLayoutEffect``). Both must agree, or the jump-down chip
  // shows when the user is visually at the bottom — which is the
  // exact bug the helper unifies.

  it('treats a column-reverse container at scrollTop=0 as the live edge', async () => {
    // Chrome / Safari / Edge / modern Firefox land at scrollTop=0
    // when the latest message is in view in a column-reverse list.
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: 0,
      scrollHeight: 800,
      clientHeight: 600,
    })).toBe(true)
  })

  it('returns true when within 80 px of the bottom (slack window)', async () => {
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: -79,
      scrollHeight: 800,
      clientHeight: 600,
    })).toBe(true)
  })

  it('returns false past the 80 px slack window', async () => {
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: -200,
      scrollHeight: 800,
      clientHeight: 600,
    })).toBe(false)
  })

  it('also handles the legacy positive-scrollTop convention', async () => {
    // ``scrollTop = maxScroll`` is the visual bottom on older
    // Firefox's positive-scrollTop column-reverse.
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: 200,
      scrollHeight: 800,
      clientHeight: 600,
    })).toBe(true)
  })

  it('returns true when the content fits in the viewport (no scrollable range)', async () => {
    // Regression for the reported notification → DM flow: a single
    // unread message at the bottom can mean ``scrollHeight ==
    // clientHeight`` (or close enough), so ``distFromBottom = 0``
    // and the user is at the live edge — the chip must NOT render.
    const { isAtLiveEdge } = await import('./DmThreadPage')
    expect(isAtLiveEdge({
      scrollTop: 0,
      scrollHeight: 600,
      clientHeight: 600,
    })).toBe(true)
  })
})

// ── Integration: mount DmThreadPage and assert the chip's render ─
// state matches the scroll-position story. jsdom doesn't lay out,
// so ``scrollHeight`` / ``clientHeight`` / ``scrollTop`` default to
// 0. We override them via ``Object.defineProperty`` to drive the
// two ends of the live-edge condition. Verifies the wiring between
// the layout effect, the post-paint follow-up, and the tail-
// tracking guard — not just the helper math.

const apiGet = vi.fn()
const apiPost = vi.fn()

/** Mutable route holder read by the single ``preact-iso`` factory
 *  below. Defaults to ``'conv-test'`` so every test that doesn't care
 *  sees the historical pinned id; the stale-response test flips it
 *  mid-flight to simulate the user switching threads. A holder (not a
 *  second ``vi.mock`` factory) keeps the "exactly one factory per
 *  module id" invariant the flake fix established. */
const routeState = { convId: 'conv-test' }

vi.mock('preact-iso', () => ({
  // Pin the route so DmThreadPage's ``useRoute().params.id`` resolves
  // to a known conv-id; the real router would set this via
  // ``<Route path="/dms/:id">`` but we're mounting the page directly.
  useRoute: () => ({
    params: { id: routeState.convId },
    path: `/dms/${routeState.convId}`,
  }),
  useLocation: () => ({ url: `/dms/${routeState.convId}`, route: vi.fn() }),
  lazy: (fn: () => Promise<{ default: unknown }>) => fn,
  LocationProvider: ({ children }: { children: unknown }) => children,
  Router: ({ children }: { children: unknown }) => children,
  Route: ({ component: C }: { component: () => unknown }) => C(),
  hydrate: vi.fn(),
  prerender: vi.fn(),
  ErrorBoundary: ({ children }: { children: unknown }) => children,
}))

vi.mock('@/api', async () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: vi.fn().mockResolvedValue({}),
    delete: vi.fn().mockResolvedValue(undefined),
    upload: vi.fn().mockResolvedValue({}),
  },
}))

// Leaflet can't run under jsdom; the location surfaces only need a
// stand-in that reports what it was given and lets a test "tap" it.
vi.mock('@/components/LocationMap', () => ({
  LocationMap: ({ markers, onPick }: {
    markers: Array<{ lat: number, lon: number }>
    onPick?: (lat: number, lon: number) => void
  }) => (
    <div data-testid="map" data-marker-count={markers.length}>
      {onPick && (
        <button type="button" onClick={() => onPick(48.858370123, 2.294481987)}>
          tap-map
        </button>
      )}
    </div>
  ),
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

interface MockApiResponses {
  conversations: unknown[]
  messages: unknown[]
  members?: unknown[]
}

function wireApiMock(fixtures: MockApiResponses): void {
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/conversations') return fixtures.conversations
    if (url.startsWith('/api/conversations/conv-test/messages')) {
      return fixtures.messages
    }
    if (url.startsWith('/api/conversations/conv-test/members')) {
      return fixtures.members ?? []
    }
    return []
  })
}

/** Force the messages scroll container's metrics so the live-edge
 *  math evaluates as if we were really laid out. jsdom returns 0
 *  for these by default, so without overriding the test would see
 *  ``distFromBottom = 0`` regardless of what we want to simulate. */
function stubScrollMetrics(opts: {
  scrollTop: number
  scrollHeight: number
  clientHeight: number
}): () => void {
  const proto = HTMLElement.prototype
  const orig = {
    scrollTop: Object.getOwnPropertyDescriptor(proto, 'scrollTop'),
    scrollHeight: Object.getOwnPropertyDescriptor(proto, 'scrollHeight'),
    clientHeight: Object.getOwnPropertyDescriptor(proto, 'clientHeight'),
  }
  Object.defineProperty(proto, 'scrollTop', {
    configurable: true, get: () => opts.scrollTop, set: () => {},
  })
  Object.defineProperty(proto, 'scrollHeight', {
    configurable: true, get: () => opts.scrollHeight,
  })
  Object.defineProperty(proto, 'clientHeight', {
    configurable: true, get: () => opts.clientHeight,
  })
  // ``scrollIntoView`` is a no-op in jsdom; this matches what
  // happens in production when the anchor message is already at the
  // visual bottom (the call does nothing because scrollTop is
  // already 0).
  if (!proto.scrollIntoView) {
    Object.defineProperty(proto, 'scrollIntoView', {
      configurable: true, value: () => {},
    })
  }
  return () => {
    if (orig.scrollTop) Object.defineProperty(proto, 'scrollTop', orig.scrollTop)
    if (orig.scrollHeight) Object.defineProperty(proto, 'scrollHeight', orig.scrollHeight)
    if (orig.clientHeight) Object.defineProperty(proto, 'clientHeight', orig.clientHeight)
  }
}

beforeEach(() => {
  vi.resetModules()
  apiGet.mockReset()
  apiPost.mockReset()
  apiPost.mockResolvedValue({})
  routeState.convId = 'conv-test'
})

describe('DmThreadPage — jump-down chip integration', () => {
  it('does NOT render the chip when the entry-scroll lands at the visual bottom', async () => {
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      wireApiMock({
        conversations: [{
          id: 'conv-test',
          type: 'dm',
          name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2,
          unread: 1,
          last_read_at: '2026-05-17T13:00:29+00:00',
        }],
        messages: [{
          id: 'msg-new',
          sender_user_id: 'u-bob',
          content: 'BUG-REPRO: only one new message',
          type: 'text',
          media_url: null, file_name: null, mime_type: null,
          file_size_bytes: null, reply_to_id: null,
          reactions: [], deleted: false,
          created_at: '2026-05-17T13:00:42+00:00',
          edited_at: null,
        }],
        members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null, is_online: false, is_idle: false, last_seen_at: null }],
      })
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container } = render(<DmThreadPage />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('BUG-REPRO')
      }, { timeout: RENDER_WAIT })
      // Give the layout effect + the follow-up useEffect a tick to settle.
      await new Promise(r => setTimeout(r, 50))
      const chip = container.querySelector('.sh-dm-jump-down')
      expect(chip).toBeNull()
    } finally {
      restore()
    }
  })

  it('DOES render the "New messages" divider when entering scrolled-up with unread', async () => {
    // Bigger scroll range + scrollTop well past the 80 px slack →
    // the entry-scroll's distFromBottom resolves to > 80, so the
    // anchor stays put and the "New messages" divider surfaces.
    // Confirms the fix didn't strip the divider in the legitimate
    // case (the chip itself only appears on subsequent WS arrivals
    // — entry-with-unread surfaces the divider, not the chip).
    const restore = stubScrollMetrics({
      scrollTop: -400, scrollHeight: 2000, clientHeight: 600,
    })
    try {
      wireApiMock({
        conversations: [{
          id: 'conv-test',
          type: 'dm',
          name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2,
          unread: 5,
          last_read_at: '2026-05-17T12:00:00+00:00',
        }],
        // Backend returns ``ORDER BY created_at DESC`` (newest first);
        // the SPA reverses to render oldest→newest. Fixture mirrors
        // the DESC shape: index 0 = newest, index 29 = oldest. Newest
        // 5 are unread (after last_read_at).
        messages: Array.from({ length: 30 }).map((_, i) => ({
          id: `msg-${29 - i}`,
          sender_user_id: 'u-bob',
          content: `msg ${29 - i}`,
          type: 'text',
          media_url: null, file_name: null, mime_type: null,
          file_size_bytes: null, reply_to_id: null,
          reactions: [], deleted: false,
          created_at: i < 5
            ? '2026-05-17T13:00:42+00:00'
            : '2026-05-17T11:00:00+00:00',
          edited_at: null,
        })),
        members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null, is_online: false, is_idle: false, last_seen_at: null }],
      })
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container } = render(<DmThreadPage />)
      await waitFor(() => {
        expect(container.querySelectorAll('[data-msg-id]').length).toBeGreaterThan(0)
      }, { timeout: RENDER_WAIT })
      await new Promise(r => setTimeout(r, 50))
      // distFromBottom = 400 > 80 so the entry-scroll layout effect
      // leaves stickToBottom=false. The follow-up effect must NOT
      // fire the read-mark POST — the user hasn't actually caught
      // up. The chip itself stays hidden because the tail-tracking
      // guard skips the initial population.
      const readPosts = apiPost.mock.calls.filter(
        ([url]) => typeof url === 'string'
          && url.startsWith('/api/conversations/conv-test/read'),
      )
      expect(readPosts).toHaveLength(0)
      const chip = container.querySelector('.sh-dm-jump-down')
      expect(chip).toBeNull()
    } finally {
      restore()
    }
  })

  it('auto-stamps the read watermark when entry-scroll lands at the live edge', async () => {
    // Positive-shape companion to the test above: when
    // ``isAtLiveEdge`` resolves to true, the follow-up useEffect
    // fires the read-mark POST. This is the contract that keeps
    // a subsequent inbound WS message from surfacing a chip the
    // user has already "seen" in the same entry.
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      wireApiMock({
        conversations: [{
          id: 'conv-test',
          type: 'dm',
          name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2,
          unread: 1,
          last_read_at: '2026-05-17T13:00:29+00:00',
        }],
        messages: [{
          id: 'msg-new',
          sender_user_id: 'u-bob',
          content: 'only one new message',
          type: 'text',
          media_url: null, file_name: null, mime_type: null,
          file_size_bytes: null, reply_to_id: null,
          reactions: [], deleted: false,
          created_at: '2026-05-17T13:00:42+00:00',
          edited_at: null,
        }],
        members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null, is_online: false, is_idle: false, last_seen_at: null }],
      })
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container } = render(<DmThreadPage />)
      await waitFor(() => {
        expect(container.querySelectorAll('[data-msg-id]').length).toBeGreaterThan(0)
      }, { timeout: RENDER_WAIT })
      // Poll for the read-mark POST rather than a fixed sleep — the
      // follow-up effect fires it post-render, and a fixed delay races it
      // under CI load.
      await waitFor(() => {
        const readPosts = apiPost.mock.calls.filter(
          ([url]) => typeof url === 'string'
            && url.startsWith('/api/conversations/conv-test/read'),
        )
        expect(readPosts.length).toBeGreaterThanOrEqual(1)
      }, { timeout: RENDER_WAIT })
    } finally {
      restore()
    }
  })

  it('renders an inline react chip on each message bubble (desktop affordance)', async () => {
    // Pre-fix the desktop user had no way to add a reaction: the
    // ``ReactionPicker`` was only reachable from the touch-only
    // ``MessageContextSheet`` (long-press). The hover affordance now
    // sits as a ``.sh-message-react-btn`` next to the existing
    // ``.sh-message-reply-btn`` so mouse users can open the picker
    // by clicking the smiley.
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      wireApiMock({
        conversations: [{
          id: 'conv-test',
          type: 'dm',
          name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2,
          unread: 0,
          last_read_at: '2026-05-17T13:00:42+00:00',
        }],
        messages: [{
          id: 'msg-1',
          sender_user_id: 'u-bob',
          content: 'hello',
          type: 'text',
          media_url: null, file_name: null, mime_type: null,
          file_size_bytes: null, reply_to_id: null,
          reactions: [], deleted: false,
          created_at: '2026-05-17T13:00:42+00:00',
          edited_at: null,
        }],
        members: [{
          user_id: 'u-bob', username: 'bob', display_name: 'Bob',
          picture_url: null, is_online: false, is_idle: false, last_seen_at: null,
        }],
      })
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container } = render(<DmThreadPage />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('hello')
      }, { timeout: RENDER_WAIT })
      // Both buttons are siblings on the bubble — Reply ↩ closer to
      // the bubble, React 😊 the further-out chip. The privacy /
      // hover-CSS contract lives in app.css; this test just pins
      // that the DOM nodes exist on a non-deleted message.
      const reactBtn = container.querySelector('.sh-message-react-btn')
      const replyBtn = container.querySelector('.sh-message-reply-btn')
      expect(reactBtn).not.toBeNull()
      expect(replyBtn).not.toBeNull()
    } finally {
      restore()
    }
  })

  it('emits ws.send(\'dm.active\', {conversation_id}) on mount and clears on unmount', async () => {
    // The active-conversation signal — tells the backend "don't fire
    // the bell row + push for me on this thread, I'm reading it right
    // now". Without it the user gets a notification for a DM they're
    // actively typing a reply to, which is the exact noise we're
    // fixing.
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      wireApiMock({
        conversations: [{
          id: 'conv-test', type: 'dm', name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2, unread: 0, last_read_at: '2026-05-17T13:00:42+00:00',
        }],
        messages: [],
        members: [{
          user_id: 'u-bob', username: 'bob', display_name: 'Bob',
          picture_url: null, is_online: false, is_idle: false, last_seen_at: null,
        }],
      })
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { ws } = await import('@/ws')
      const sendMock = ws.send as unknown as ReturnType<typeof vi.fn>
      sendMock.mockClear()
      const { unmount } = render(<DmThreadPage />)
      await waitFor(() => {
        expect(sendMock).toHaveBeenCalledWith('dm.active', { conversation_id: 'conv-test' })
      }, { timeout: RENDER_WAIT })
      sendMock.mockClear()
      unmount()
      // Cleanup effect must clear the marker so backgrounded threads
      // start emitting notifications again.
      expect(sendMock).toHaveBeenCalledWith('dm.active', { conversation_id: null })
    } finally {
      restore()
    }
  })
})

describe('DmThreadPage — composer mic⇄send swap', () => {
  // Regression: inserting an emoji as the FIRST composer character via
  // the picker mutates the textarea value programmatically, which does
  // NOT fire ``onInput`` — so the ``composerHasContent`` flag must be
  // refreshed by the splice itself, or the mic button never swaps to
  // Send and the user can't send an emoji-only first message.
  it('swaps mic→send when the first character is an emoji from the picker', async () => {
    wireApiMock({
      conversations: [{
        id: 'conv-test',
        type: 'dm',
        name: null,
        last_message_at: '2026-05-17T13:00:42+00:00',
        members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
        member_count: 2,
        unread: 0,
        last_read_at: '2026-05-17T13:00:42+00:00',
      }],
      messages: [],
      members: [{
        user_id: 'u-bob', username: 'bob', display_name: 'Bob',
        picture_url: null, is_online: false, is_idle: false, last_seen_at: null,
      }],
    })
    const { render, waitFor, fireEvent } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { container } = render(<DmThreadPage />)
    await waitFor(() => {
      expect(container.querySelector('textarea[name="content"]')).not.toBeNull()
    }, { timeout: RENDER_WAIT })

    // Empty composer → no Send button, the voice-record slot owns it.
    expect(container.querySelector('[aria-label="Send message"]')).toBeNull()

    // Open the inline emoji picker and pick the first emoji.
    const emojiBtn = container.querySelector(
      '[aria-label="Insert emoji into message"]',
    ) as HTMLElement
    expect(emojiBtn).not.toBeNull()
    fireEvent.click(emojiBtn)
    const firstEmoji = await waitFor(() => {
      const el = container.querySelector('.sh-emoji-btn')
      expect(el).not.toBeNull()
      return el as HTMLElement
    }, { timeout: RENDER_WAIT })
    fireEvent.click(firstEmoji)

    // The composer now has content (an emoji), so the slot must show Send.
    await waitFor(() => {
      expect(container.querySelector('[aria-label="Send message"]')).not.toBeNull()
    }, { timeout: RENDER_WAIT })
  })
})

describe('DmThreadPage — load-effect failure isolation', () => {
  // The messages fetch used to carry a single trailing ``.catch`` that
  // was *documented* as the network-failure branch but structurally
  // also caught anything thrown by its own ~50-line success handler —
  // and its response was to blank the thread. So a bug in the
  // unread-anchor math (or, as here, in the fire-and-forget read POST)
  // presented to the user as "this conversation is empty", with no
  // toast and no log. The two paths are now separate: a rejected fetch
  // clears the skeleton and empties the list, a throw out of the
  // success handler leaves the rendered thread alone and logs.
  it('keeps the fetched messages on screen when the success handler throws', async () => {
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    const errSpy = vi.spyOn(console, 'error').mockImplementation(() => {})
    try {
      wireApiMock({
        // ``unread: 0`` + ``last_read_at: null`` → no unread anchor, so
        // the success handler always reaches the mark-as-read POST.
        conversations: [{
          id: 'conv-test', type: 'dm', name: null,
          last_message_at: '2026-05-17T13:00:42+00:00',
          members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
          member_count: 2, unread: 0, last_read_at: null,
        }],
        messages: [{
          id: 'msg-1',
          sender_user_id: 'u-bob',
          content: 'STILL-HERE: handler threw but the thread stands',
          type: 'text',
          media_url: null, file_name: null, mime_type: null,
          file_size_bytes: null, reply_to_id: null,
          reactions: [], deleted: false,
          created_at: '2026-05-17T13:00:42+00:00',
          edited_at: null,
        }],
        members: [{
          user_id: 'u-bob', username: 'bob', display_name: 'Bob',
          picture_url: null, is_online: false, is_idle: false, last_seen_at: null,
        }],
      })
      // Throw *synchronously* out of the read POST — the cheapest
      // reliable stand-in for a bug anywhere in the success handler.
      apiPost.mockImplementation(() => { throw new Error('boom') })

      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container } = render(<DmThreadPage />)
      // Wait until the handler has actually reached (and thrown from)
      // the read POST, so the assertion below is about the aftermath.
      await waitFor(() => {
        expect(apiPost.mock.calls.some(
          ([url]) => typeof url === 'string' && url.endsWith('/read'),
        )).toBe(true)
      }, { timeout: RENDER_WAIT })
      // Let the rejection propagate through its microtask + a render.
      await new Promise(r => setTimeout(r, 50))
      expect(container.textContent ?? '').toContain('STILL-HERE')
      // Silent failure was half the bug — the thread survives *and*
      // the throw is diagnosable, with the thread id in the log.
      expect(errSpy.mock.calls.some(
        args => args.some(a => typeof a === 'string' && a.includes('conv-test')),
      )).toBe(true)
    } finally {
      errSpy.mockRestore()
      restore()
    }
  })

  it('does not let a slow response from the previous thread overwrite the current one', async () => {
    // Switching threads re-runs the load effect, but the in-flight
    // request from the thread we just left still resolves — and every
    // continuation writes module-level signals. Without a per-run
    // staleness guard the late response repaints thread A's messages
    // over thread B, which the user reads as "wrong conversation".
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      const convRow = (id: string) => ({
        id, type: 'dm', name: null,
        last_message_at: '2026-05-17T13:00:42+00:00',
        members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
        member_count: 2, unread: 0, last_read_at: null,
      })
      const msgRow = (id: string, content: string) => ({
        id, sender_user_id: 'u-bob', content, type: 'text',
        media_url: null, file_name: null, mime_type: null,
        file_size_bytes: null, reply_to_id: null,
        reactions: [], deleted: false,
        created_at: '2026-05-17T13:00:42+00:00',
        edited_at: null,
      })
      // Thread A's messages fetch never settles until we say so.
      let releaseA: (rows: unknown[]) => void = () => {}
      const slowA = new Promise<unknown[]>(res => { releaseA = res })
      apiGet.mockImplementation(async (url: string) => {
        if (url === '/api/conversations') return [convRow('conv-a'), convRow('conv-b')]
        if (url.startsWith('/api/conversations/conv-a/messages')) return slowA
        if (url.startsWith('/api/conversations/conv-b/messages')) {
          return [msgRow('msg-b', 'THREAD-B: the thread the user is looking at')]
        }
        if (url.endsWith('/members')) {
          return [{
            user_id: 'u-bob', username: 'bob', display_name: 'Bob',
            picture_url: null, is_online: false, is_idle: false, last_seen_at: null,
          }]
        }
        return []
      })

      routeState.convId = 'conv-a'
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const { container, rerender } = render(<DmThreadPage />)
      // A's messages fetch is in flight (and pinned open).
      await waitFor(() => {
        expect(apiGet.mock.calls.some(
          ([url]) => typeof url === 'string'
            && url.startsWith('/api/conversations/conv-a/messages'),
        )).toBe(true)
      }, { timeout: RENDER_WAIT })

      // The user navigates to thread B; the effect re-runs against the
      // new id. The changing ``key`` is only a device to force an
      // update — with identical props and no dirty signal,
      // @preact/signals' ``shouldComponentUpdate`` skips the re-render
      // entirely and the effect would never see B. It is NOT what the
      // router does: ``App.tsx`` keys each route by its path *pattern*
      // (``/dms/:id``), so a real DM→DM navigation diffs into the same
      // instance. That distinction doesn't matter here — ``cancelled``
      // is closure-scoped, so it survives either shape — but it does
      // for the ref-vs-module marker in the staleness tests below.
      routeState.convId = 'conv-b'
      rerender(<DmThreadPage key="conv-b" />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('THREAD-B')
      }, { timeout: RENDER_WAIT })

      // Only now does A's request come back.
      releaseA([msgRow('msg-a', 'THREAD-A: stale response from the thread we left')])
      await new Promise(r => setTimeout(r, 50))
      expect(container.textContent ?? '').toContain('THREAD-B')
      expect(container.textContent ?? '').not.toContain('THREAD-A')
    } finally {
      restore()
    }
  })
})

// ── Integration: the older-history (``loadOlder``) staleness guard ──
// ``loadOlder`` lives outside the big load effect, so it can't see that
// effect's per-run ``cancelled`` flag. It captures ``convId`` from the
// render closure, awaits a network round-trip, and then *prepends* into
// the module-level ``messages`` signal — so a page belonging to the
// thread the user just left would splice foreign history into the
// thread they're actually reading. ``activeConvRef`` is the guard.
describe('DmThreadPage — older-history staleness', () => {
  const convRow = (id: string) => ({
    id, type: 'dm', name: null,
    last_message_at: '2026-05-17T13:00:42+00:00',
    members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
    member_count: 2, unread: 0, last_read_at: null,
  })
  const msgRow = (id: string, content: string) => ({
    id, sender_user_id: 'u-bob', content, type: 'text',
    media_url: null, file_name: null, mime_type: null,
    file_size_bytes: null, reply_to_id: null,
    reactions: [], deleted: false,
    created_at: '2026-05-17T13:00:42+00:00',
    edited_at: null,
  })
  const memberRows = [{
    user_id: 'u-bob', username: 'bob', display_name: 'Bob',
    picture_url: null, is_online: false, is_idle: false, last_seen_at: null,
  }]
  /** ``unread: 0`` + ``last_read_at: null`` → the load effect's window
   *  ``limit`` floors at 25, and it derives ``hasMoreHistory`` from
   *  ``data.length === limit``. A page of exactly 25 is therefore the
   *  fixture shape that leaves "there is older history" ON, which is
   *  what arms the ``loadOlder`` trigger. */
  const fullPage = (prefix: string) => Array.from({ length: 25 }).map(
    (_, i) => msgRow(`${prefix}-${24 - i}`, `${prefix} body ${24 - i}`),
  )

  /** Put the container at the visual top and fire the scroll handler.
   *  ``scrollHeight === clientHeight`` makes ``maxScroll`` 0, so
   *  ``distFromTop`` is 0 — inside ``handleScroll``'s 120 px
   *  lazy-load trigger. jsdom does no layout, so a dispatched
   *  ``scroll`` event on the container is the only way in. */
  /** Re-render the SAME component instance against a new route id.
   *
   *  ``DmThreadPage`` declares no props, so we cast at the JSX site to
   *  pass a throwaway ``tick``. That's the cheapest way to make
   *  @preact/signals' ``shouldComponentUpdate`` see changed props and
   *  actually re-render — with identical props and no dirty signal it
   *  skips the update entirely and the load effect never observes the
   *  new ``convId``.
   *
   *  A changing ``key`` forces an update too, but by REMOUNTING, which
   *  throws away the hook state — including ``activeConvRef`` — so the
   *  stale ``loadOlder`` closure would keep reading its own dead ref,
   *  pinned at the old id, and the guard could never fire. Production
   *  does not remount on a DM→DM navigation: ``App.tsx`` keys each
   *  ``<Route>`` by its path *pattern* (``/dms/:id``), so conv-a →
   *  conv-b diffs into the same instance and only the ``[convId]``
   *  effect re-runs. Same-instance re-render is the faithful shape. */
  const asPage = (C: unknown) => C as (p: { tick: number }) => JSX.Element

  const scrollToTop = (container: Element) => {
    const el = container.querySelector('.sh-messages')
    expect(el).not.toBeNull()
    el!.dispatchEvent(new Event('scroll'))
  }

  it('does not prepend an older-history page belonging to the thread we left', async () => {
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      // Thread A's *older-history* page is pinned open until we release it.
      let releaseOlderA: (rows: unknown[]) => void = () => {}
      const slowOlderA = new Promise<unknown[]>(res => { releaseOlderA = res })
      apiGet.mockImplementation(async (url: string) => {
        if (url === '/api/conversations') return [convRow('conv-a'), convRow('conv-b')]
        if (url.startsWith('/api/conversations/conv-a/messages')) {
          return url.includes('before=') ? slowOlderA : fullPage('THREAD-A')
        }
        if (url.startsWith('/api/conversations/conv-b/messages')) {
          // Short page (< limit) → B has no older history of its own,
          // so nothing but the stale write can touch B's list.
          return url.includes('before=')
            ? []
            : [msgRow('msg-b', 'THREAD-B: the thread the user is looking at')]
        }
        if (url.endsWith('/members')) return memberRows
        return []
      })

      routeState.convId = 'conv-a'
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const Page = asPage(DmThreadPage)
      const { container, rerender } = render(<Page tick={1} />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('THREAD-A body 0')
      }, { timeout: RENDER_WAIT })

      // Scroll up in A → the older-history fetch goes out and hangs.
      scrollToTop(container)
      await waitFor(() => {
        expect(apiGet.mock.calls.some(
          ([url]) => typeof url === 'string'
            && url.startsWith('/api/conversations/conv-a/messages?before='),
        )).toBe(true)
      }, { timeout: RENDER_WAIT })

      // The user switches to thread B — same mount, new route id (see
      // ``asPage`` for why this is a prop bump and not a ``key`` bump).
      routeState.convId = 'conv-b'
      rerender(<Page tick={2} />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('THREAD-B')
      }, { timeout: RENDER_WAIT })

      // Only now does A's older page land.
      releaseOlderA([msgRow('msg-a-old', 'THREAD-A-OLDER: history from the thread we left')])
      await new Promise(r => setTimeout(r, 50))
      expect(container.textContent ?? '').toContain('THREAD-B')
      expect(container.textContent ?? '').not.toContain('THREAD-A-OLDER')
    } finally {
      restore()
    }
  })

  it('a send that resolves after a thread switch does not touch the new thread', async () => {
    // The send paths were flagged as sharing loadOlder's race. They do
    // not: every mutation is keyed on ``tempId`` or the server-assigned
    // id, neither of which can exist in the other thread's list, so a
    // late resolution is a content no-op. This test pins that
    // invariant — if someone reworks the reconcile to touch the list
    // positionally (the way loadOlder's prepend did), it fails.
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      let releaseSend: (v: unknown) => void = () => {}
      const slowSend = new Promise<unknown>(res => { releaseSend = res })
      apiGet.mockImplementation(async (url: string) => {
        if (url === '/api/conversations') return [convRow('conv-a'), convRow('conv-b')]
        if (url.startsWith('/api/conversations/conv-a/messages')) {
          return url.includes('before=') ? [] : [msgRow('a-1', 'THREAD-A body 0')]
        }
        if (url.startsWith('/api/conversations/conv-b/messages')) {
          return url.includes('before=')
            ? []
            : [msgRow('msg-b', 'THREAD-B: the thread the user is looking at')]
        }
        if (url.endsWith('/members')) return memberRows
        return []
      })
      // The send POST hangs until we release it.
      apiPost.mockImplementation(async (url: string) => {
        if (url === '/api/conversations/conv-a/messages') return slowSend
        return {}
      })

      routeState.convId = 'conv-a'
      const { render, waitFor, fireEvent } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const Page = asPage(DmThreadPage)
      const { container, rerender } = render(<Page tick={1} />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('THREAD-A body 0')
      }, { timeout: RENDER_WAIT })

      // Send from thread A — the optimistic bubble appears at once and
      // the POST hangs.
      const ta = container.querySelector('textarea')
      const form = container.querySelector('form.sh-composer')
      expect(ta).not.toBeNull(); expect(form).not.toBeNull()
      ;(ta as HTMLTextAreaElement).value = 'SENT-FROM-A'
      fireEvent.input(ta as HTMLTextAreaElement)
      fireEvent.submit(form as HTMLFormElement)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('SENT-FROM-A')
      }, { timeout: RENDER_WAIT })

      // Switch to B, then let A's send resolve with a real id.
      routeState.convId = 'conv-b'
      rerender(<Page tick={2} />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('THREAD-B')
      }, { timeout: RENDER_WAIT })
      releaseSend({ id: 'real-a-id' })
      await new Promise(r => setTimeout(r, 80))

      // B is untouched: its own message is still there and nothing
      // from A leaked in.
      expect(container.textContent ?? '').toContain('THREAD-B')
      expect(container.textContent ?? '').not.toContain('SENT-FROM-A')
      expect(container.querySelectorAll('[data-msg-id]').length).toBe(1)
    } finally {
      restore()
    }
  })

  it('does not prepend a stale page after leaving the thread via the inbox', async () => {
    // The realistic switch is thread A → /dms → thread B, which
    // UNMOUNTS DmThreadPage between the two (the inbox is its own
    // route). A per-instance ref is recreated on the remount, so a
    // stale ``loadOlder`` closure would compare against its own dead
    // copy — still pinned at conv-a — and let the write through. Only a
    // marker that outlives the unmount can catch this path, which is
    // the common one; a direct conv-a → conv-b deep link is the rare
    // one.
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      let releaseOlderA: (rows: unknown[]) => void = () => {}
      const slowOlderA = new Promise<unknown[]>(res => { releaseOlderA = res })
      apiGet.mockImplementation(async (url: string) => {
        if (url === '/api/conversations') return [convRow('conv-a'), convRow('conv-b')]
        if (url.startsWith('/api/conversations/conv-a/messages')) {
          return url.includes('before=') ? slowOlderA : fullPage('THREAD-A')
        }
        if (url.startsWith('/api/conversations/conv-b/messages')) {
          return url.includes('before=')
            ? []
            : [msgRow('msg-b', 'THREAD-B: the thread the user is looking at')]
        }
        if (url.endsWith('/members')) return memberRows
        return []
      })

      routeState.convId = 'conv-a'
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const Page = asPage(DmThreadPage)
      const first = render(<Page tick={1} />)
      await waitFor(() => {
        expect(first.container.textContent ?? '').toContain('THREAD-A body 0')
      }, { timeout: RENDER_WAIT })
      scrollToTop(first.container)
      await waitFor(() => {
        expect(apiGet.mock.calls.some(
          ([url]) => typeof url === 'string'
            && url.startsWith('/api/conversations/conv-a/messages?before='),
        )).toBe(true)
      }, { timeout: RENDER_WAIT })

      // Leave the thread entirely (the /dms hop), then open B fresh.
      first.unmount()
      routeState.convId = 'conv-b'
      const second = render(<Page tick={1} />)
      await waitFor(() => {
        expect(second.container.textContent ?? '').toContain('THREAD-B')
      }, { timeout: RENDER_WAIT })

      releaseOlderA([msgRow('msg-a-old', 'THREAD-A-OLDER: history from the thread we left')])
      await new Promise(r => setTimeout(r, 50))
      expect(second.container.textContent ?? '').toContain('THREAD-B')
      expect(second.container.textContent ?? '').not.toContain('THREAD-A-OLDER')
    } finally {
      restore()
    }
  })

  it('does not let a stale older-history page clear the new thread\'s spinner', async () => {
    // ``isLoadingOlder`` is a module-level signal shared across threads.
    // A stale page resolving its ``finally`` would flip it to ``false``
    // while the *new* thread's own page is still in flight — killing the
    // spinner and re-arming ``handleScroll`` to double-fetch.
    const restore = stubScrollMetrics({
      scrollTop: 0, scrollHeight: 600, clientHeight: 600,
    })
    try {
      let releaseOlderA: (rows: unknown[]) => void = () => {}
      const slowOlderA = new Promise<unknown[]>(res => { releaseOlderA = res })
      // B's older page never settles — the spinner is its proxy.
      const neverOlderB = new Promise<unknown[]>(() => {})
      apiGet.mockImplementation(async (url: string) => {
        if (url === '/api/conversations') return [convRow('conv-a'), convRow('conv-b')]
        if (url.startsWith('/api/conversations/conv-a/messages')) {
          return url.includes('before=') ? slowOlderA : fullPage('THREAD-A')
        }
        if (url.startsWith('/api/conversations/conv-b/messages')) {
          return url.includes('before=') ? neverOlderB : fullPage('THREAD-B')
        }
        if (url.endsWith('/members')) return memberRows
        return []
      })

      routeState.convId = 'conv-a'
      const { render, waitFor } = await import('@testing-library/preact')
      const { default: DmThreadPage } = await import('./DmThreadPage')
      const Page = asPage(DmThreadPage)
      const { container, rerender } = render(<Page tick={1} />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('THREAD-A body 0')
      }, { timeout: RENDER_WAIT })
      scrollToTop(container)
      await waitFor(() => {
        expect(apiGet.mock.calls.some(
          ([url]) => typeof url === 'string'
            && url.startsWith('/api/conversations/conv-a/messages?before='),
        )).toBe(true)
      }, { timeout: RENDER_WAIT })

      routeState.convId = 'conv-b'
      rerender(<Page tick={2} />)
      await waitFor(() => {
        expect(container.textContent ?? '').toContain('THREAD-B body 0')
      }, { timeout: RENDER_WAIT })

      // B scrolls up too — its own older fetch is now the in-flight one.
      scrollToTop(container)
      await waitFor(() => {
        expect(container.querySelector('.sh-dm-load-older')).not.toBeNull()
      }, { timeout: RENDER_WAIT })

      releaseOlderA([msgRow('msg-a-old', 'THREAD-A-OLDER: history from the thread we left')])
      await new Promise(r => setTimeout(r, 50))
      expect(container.querySelector('.sh-dm-load-older')).not.toBeNull()
    } finally {
      restore()
    }
  })
})


describe('DmThreadPage — mute in the header', () => {
  const row = (muted_until: string | null) => ({
    id: 'conv-test', type: 'dm', name: null, last_message_at: null,
    members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
    member_count: 2, unread: 0, last_read_at: null, muted_until,
  })

  it('an unmuted thread shows the bell that opens the mute menu', async () => {
    wireApiMock({ conversations: [row(null)], messages: [] })
    const { render } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { findByLabelText } = render(<DmThreadPage />)
    const bell = await findByLabelText('Mute notifications', {}, { timeout: RENDER_WAIT })
    expect(bell.textContent).toBe('🔔')
  })

  it('a muted thread shows the bell-slash with when it ends', async () => {
    const soon = new Date(Date.now() + 3600_000).toISOString()
    wireApiMock({ conversations: [row(soon)], messages: [] })
    const { render } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { findByLabelText } = render(<DmThreadPage />)
    const btn = await findByLabelText(/^Muted until .* — select to unmute$/, {}, { timeout: RENDER_WAIT })
    expect(btn.textContent).toBe('🔕')
  })
})


describe('DmThreadPage — location messages', () => {
  const conv = {
    id: 'conv-test', type: 'group_dm', name: 'Trip', last_message_at: null,
    members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
    member_count: 3, unread: 0, last_read_at: null,
  }
  const locRow = (content: string) => ({
    id: 'msg-loc', sender_user_id: 'u-bob', content, type: 'location',
    media_url: null, file_name: null, mime_type: null, file_size_bytes: null,
    reply_to_id: null, reactions: [], deleted: false,
    created_at: '2026-05-17T13:00:42+00:00', edited_at: null,
  })

  it('renders a shared location as a card, never as raw JSON', async () => {
    wireApiMock({
      conversations: [conv],
      messages: [locRow('{"lat":52.3702,"lon":4.8952,"label":"Dam square","accuracy_m":50}')],
    })
    const { render } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { container, findByText } = render(<DmThreadPage />)
    await findByText('📍 Dam square', {}, { timeout: RENDER_WAIT })
    expect(container.textContent).not.toContain('"lat"')
    const link = container.querySelector('a.sh-location-post-open') as HTMLAnchorElement
    expect(link.href).toContain('mlat=52.3702')
  })

  it('a DM file whose media_url is javascript: or dropped renders as plain text, not a link', async () => {
    const fileRow = (id: string, media_url: string | null, file_name: string) => ({
      ...locRow(''), id, type: 'file', media_url, file_name,
      mime_type: 'application/pdf', file_size_bytes: 10,
    })
    wireApiMock({
      conversations: [conv],
      messages: [
        fileRow('msg-js', 'javascript:alert(document.domain)', 'evil.pdf'),
        fileRow('msg-null', null, 'dropped.pdf'),
        fileRow('msg-ok', 'api/media/f1.pdf', 'fine.pdf'),
      ],
    })
    const { render } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { findByText } = render(<DmThreadPage />)
    for (const name of ['evil.pdf', 'dropped.pdf']) {
      const chip = (await findByText(name, {}, { timeout: RENDER_WAIT }))
        .closest('.sh-message-file') as HTMLElement
      expect(chip.tagName).toBe('SPAN')
      expect(chip.closest('a')).toBeNull()
      expect(chip.textContent).toContain('File not available')
    }
    const fine = (await findByText('fine.pdf')).closest('a.sh-message-file')
    expect(fine?.getAttribute('href')).toBe('api/media/f1.pdf')
  })

  it('"Open in new tab" is hidden for a javascript: media_url and never calls window.open with it', async () => {
    const fileRow = (id: string, media_url: string, file_name: string) => ({
      ...locRow(''), id, type: 'file', media_url, file_name,
      mime_type: 'application/pdf', file_size_bytes: 10,
    })
    wireApiMock({
      conversations: [conv],
      messages: [
        fileRow('msg-js', 'javascript:alert(document.domain)', 'evil.pdf'),
        fileRow('msg-ok', 'api/media/f1.pdf', 'fine.pdf'),
      ],
    })
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(null)
    const { render, fireEvent } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { findByText, queryByRole, findByRole } = render(<DmThreadPage />)
    const evilBubble = (await findByText('evil.pdf', {}, { timeout: RENDER_WAIT }))
      .closest('.sh-message') as HTMLElement
    fireEvent.click(evilBubble.querySelector('.sh-message-react-btn') as HTMLElement)
    await findByRole('dialog')
    expect(queryByRole('button', { name: /Open in new tab/ })).toBeNull()
    fireEvent.keyDown(document, { key: 'Escape' })
    // The local file still offers the action, and opens its own URL.
    const fineBubble = (await findByText('fine.pdf')).closest('.sh-message') as HTMLElement
    fireEvent.click(fineBubble.querySelector('.sh-message-react-btn') as HTMLElement)
    fireEvent.click(await findByRole('button', { name: /Open in new tab/ }))
    expect(openSpy).toHaveBeenCalledWith('api/media/f1.pdf', '_blank', 'noopener,noreferrer')
    expect(openSpy).not.toHaveBeenCalledWith(
      expect.stringContaining('javascript:'), expect.anything(), expect.anything(),
    )
    openSpy.mockRestore()
  })

  it('shares a location picked on the map from the attach menu', async () => {
    wireApiMock({ conversations: [conv], messages: [] })
    apiPost.mockResolvedValue({ id: 'srv-loc' })
    const { render, fireEvent, waitFor } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { container, getByRole, getByText, findByRole } = render(<DmThreadPage />)
    fireEvent.click(await findByRole('button', { name: 'Attach' }, { timeout: RENDER_WAIT }))
    fireEvent.click(getByRole('menuitem', { name: /Location/ }))
    fireEvent.click(getByText(/Pick a spot on the map/))
    fireEvent.click(getByText('tap-map'))
    fireEvent.click(getByRole('button', { name: 'Send location' }))
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        '/api/conversations/conv-test/messages',
        {
          type: 'location',
          content: '{"lat":48.8584,"lon":2.2945,"label":null,"accuracy_m":null}',
        },
      )
    })
    // The optimistic bubble is the card, not the JSON.
    expect(container.textContent).toContain('48.8584, 2.2945')
    expect(container.textContent).not.toContain('"lat"')
  })
})


describe('DmThreadPage — @-mentions in a group chat', () => {
  const group = {
    id: 'conv-test', type: 'group_dm', name: 'Trip', last_message_at: null,
    members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
    member_count: 3, unread: 0, last_read_at: null, muted_until: null,
    notif_level: 'all',
  }
  const roster = [
    { user_id: 'u-me', username: 'me', display_name: 'Me', picture_url: null, is_self: true, is_online: true, is_idle: false, last_seen_at: null, mention: 'me' },
    { user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null, is_self: false, is_online: false, is_idle: false, last_seen_at: null, mention: 'bob' },
    { user_id: 'u-bea', username: 'bea', display_name: 'Beatrix', picture_url: null, is_self: false, is_online: false, is_idle: false, last_seen_at: null, mention: 'bea', instance_id: 'peer', household_name: 'The Smiths' },
  ]
  const textRow = (id: string, content: string) => ({
    id, sender_user_id: 'u-bob', content, type: 'text',
    media_url: null, file_name: null, mime_type: null, file_size_bytes: null,
    reply_to_id: null, reactions: [], deleted: false,
    created_at: '2026-05-17T13:00:42+00:00', edited_at: null,
  })

  it('highlights member tokens in bubbles, the viewer’s own one distinctly', async () => {
    wireApiMock({
      conversations: [group],
      messages: [textRow('m1', 'hey @me and @bea, not @nobody')],
      members: roster,
    })
    const { render, waitFor } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { container } = render(<DmThreadPage />)
    await waitFor(() => {
      expect(container.querySelectorAll('.sh-mention').length).toBe(2)
    }, { timeout: RENDER_WAIT })
    const spans = [...container.querySelectorAll('.sh-mention')]
    expect(spans.map(s => [s.textContent, s.classList.contains('sh-mention--self')]))
      .toEqual([['@me', true], ['@bea', false]])
  })

  it('typing @ in the composer offers the other members (roster fetched once)', async () => {
    wireApiMock({ conversations: [group], messages: [], members: roster })
    const { render, fireEvent, waitFor } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { container, findByPlaceholderText } = render(<DmThreadPage />)
    const ta = await findByPlaceholderText('Type a message...', {}, { timeout: RENDER_WAIT }) as HTMLTextAreaElement
    await waitFor(() => expect(container.querySelector('.sh-thread-mute-btn')).toBeTruthy(), { timeout: RENDER_WAIT })
    ta.value = 'hi @b'
    ta.setSelectionRange(5, 5)
    fireEvent.input(ta)
    await waitFor(() => expect(document.getElementById('sh-mention-listbox')).toBeTruthy())
    const opts = [...document.querySelectorAll('#sh-mention-listbox [role="option"]')]
    expect(opts.map(o => o.textContent)).toEqual(['BOBob@bob', 'BEBeatrix@bea · The Smiths'])
    fireEvent.keyDown(ta, { key: 'Enter' })
    expect(ta.value).toBe('hi @bob ')
    const rosterCalls = apiGet.mock.calls.filter(c => c[0] === '/api/conversations/conv-test/members')
    expect(rosterCalls).toHaveLength(1)
  })
})


describe('DmThreadPage — editing your own message', () => {
  const dm = {
    id: 'conv-test', type: 'dm', name: null, last_message_at: null,
    members: [{ user_id: 'u-bob', username: 'bob', display_name: 'Bob', picture_url: null }],
    member_count: 2, unread: 0, last_read_at: null, muted_until: null,
  }
  const row = (id: string, sender: string, content: string, extra = {}) => ({
    id, sender_user_id: sender, content, type: 'text',
    media_url: null, file_name: null, mime_type: null, file_size_bytes: null,
    reply_to_id: null, reactions: [], deleted: false,
    created_at: '2026-05-17T13:00:42+00:00', edited_at: null, ...extra,
  })

  it('offers ✎ on own text messages only, and Enter saves the edit', async () => {
    wireApiMock({
      conversations: [dm],
      messages: [row('m-mine', 'u-me', 'helo'), row('m-bob', 'u-bob', 'hi there')],
    })
    const { api } = await import('@/api')
    const patch = api.patch as unknown as ReturnType<typeof vi.fn>
    patch.mockResolvedValueOnce({
      id: 'm-mine', content: 'hello', edited_at: '2026-05-17T13:05:00+00:00',
    })
    const { render, fireEvent, waitFor } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { findAllByLabelText, findByLabelText, container } = render(<DmThreadPage />)
    const edits = await findAllByLabelText('Edit your message', {}, { timeout: RENDER_WAIT })
    expect(edits).toHaveLength(1) // Bob's message carries no edit chip
    fireEvent.click(edits[0])
    const box = await findByLabelText('Edit message') as HTMLTextAreaElement
    expect(box.value).toBe('helo')
    fireEvent.input(box, { target: { value: 'hello' } })
    fireEvent.keyDown(box, { key: 'Enter' })
    await waitFor(() => expect(patch).toHaveBeenCalledWith(
      '/api/conversations/conv-test/messages/m-mine', { content: 'hello' },
    ))
    await waitFor(() => expect(container.querySelector('.sh-message-edit')).toBeNull())
    expect(container.textContent).toContain('hello')
    expect(container.querySelector('.sh-message-edited')?.textContent).toBe('edited')
  })

  it('Escape cancels without saving; a failed save keeps the draft open', async () => {
    wireApiMock({ conversations: [dm], messages: [row('m-mine', 'u-me', 'draft')] })
    const { api } = await import('@/api')
    const patch = api.patch as unknown as ReturnType<typeof vi.fn>
    patch.mockClear()
    const { render, fireEvent, waitFor } = await import('@testing-library/preact')
    const { default: DmThreadPage } = await import('./DmThreadPage')
    const { findByLabelText, container } = render(<DmThreadPage />)
    fireEvent.click(await findByLabelText('Edit your message', {}, { timeout: RENDER_WAIT }))
    const box = await findByLabelText('Edit message') as HTMLTextAreaElement
    fireEvent.keyDown(box, { key: 'Escape' })
    await waitFor(() => expect(container.querySelector('.sh-message-edit')).toBeNull())
    expect(patch).not.toHaveBeenCalled()

    patch.mockRejectedValueOnce(new Error('offline'))
    fireEvent.click(await findByLabelText('Edit your message'))
    const again = await findByLabelText('Edit message') as HTMLTextAreaElement
    fireEvent.input(again, { target: { value: 'draft 2' } })
    fireEvent.keyDown(again, { key: 'Enter' })
    await waitFor(() => expect(patch).toHaveBeenCalledTimes(1))
    expect(container.querySelector('.sh-message-edit')).not.toBeNull()
  })

  it('canEditMessage: never someone else’s, a deleted, pending or voice message', async () => {
    const { canEditMessage } = await import('./DmThreadPage')
    const base = row('m-1', 'u-me', 'x') as unknown as Parameters<typeof canEditMessage>[0]
    expect(canEditMessage(base, 'u-me')).toBe(true)
    expect(canEditMessage(base, 'u-bob')).toBe(false)
    expect(canEditMessage({ ...base, deleted: true }, 'u-me')).toBe(false)
    expect(canEditMessage({ ...base, id: 'tmp-1' }, 'u-me')).toBe(false)
    expect(canEditMessage({ ...base, type: 'audio' }, 'u-me')).toBe(false)
    expect(canEditMessage({ ...base, type: 'image', content: '' }, 'u-me')).toBe(false)
    expect(canEditMessage({ ...base, type: 'image', content: 'cap' }, 'u-me')).toBe(true)
  })
})
