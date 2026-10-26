import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, within } from '@testing-library/preact'

const wsHandlers = vi.hoisted(() => new Map<string, (e: { data: unknown }) => void>())
const routeQuery = vi.hoisted(() => ({ current: {} as Record<string, string> }))
const routeSpy = vi.hoisted(() => vi.fn())
const confirmSpy = vi.hoisted(() => vi.fn(async () => true))
const toastSpy = vi.hoisted(() => vi.fn())

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { highlightId: 'h1' }, query: routeQuery.current }),
  useLocation: () => ({ route: routeSpy, url: '/' }),
}))
vi.mock('@/components/Toast', () => ({ showToast: toastSpy }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirmSpy }))
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, fn: (e: { data: unknown }) => void) => {
      wsHandlers.set(type, fn)
      return () => wsHandlers.delete(type)
    },
  },
}))

import { api } from '@/api'
import { currentUser } from '@/store/auth'
import { householdUsers } from '@/store/householdUsers'
import { spaces } from '@/store/spaces'
import { connections } from '@/store/connections'
import type { HighlightInboxItem, User } from '@/types'
import HighlightViewerPage from './HighlightViewerPage'
import { _resetViewerReturnForTests, noteViewerRoute } from './viewerReturn'

const highlight = (author: string, extra: object = {}) => ({
  id: 'h1', author_user_id: author, highlight_date: '2026-10-19',
  audience_kind: 'all_paired', audience_label: 'paired',
  created_at: '2026-10-19T08:00:00Z', expires_at: '2026-10-20T08:00:00Z',
  ...extra,
})
const frames = [{
  id: 'f1', highlight_id: 'h1', sequence: 1, frame_type: 'image',
  media_url: 'api/media/x.webp', caption_text: null, caption_emoji: null,
  duration_ms: null, created_at: '2026-10-19T08:00:00Z', my_reaction: null,
}]
const two = [
  frames[0],
  { ...frames[0], id: 'f2', sequence: 2, media_url: 'api/media/y.webp' },
]

/** Today: ``h1`` (the one open), then whoever else the test adds. */
let today: HighlightInboxItem[] = []
let detailBody: Record<string, unknown> = {}

function route(extra?: (path: string) => unknown) {
  vi.mocked(api.get).mockImplementation(async (path: string) => {
    const hit = extra?.(path)
    if (hit !== undefined) return hit
    if (path === '/api/users') return []
    if (path === '/api/highlights') return today
    if (path === '/api/connections') return []
    return detailBody
  })
}

function todayItem(id: string, author: string, name: string, unseen: number, minutesAgo: number): HighlightInboxItem {
  const at = new Date(Date.now() - minutesAgo * 60_000).toISOString()
  return {
    highlight: {
      id, author_user_id: author, highlight_date: '2026-10-19', audience_kind: 'all_paired',
      audience_label: 'paired', created_at: at, expires_at: new Date(Date.now() + 86_400_000).toISOString(),
      author: { display_name: name, picture_url: null },
    },
    frames: [{ ...frames[0], id: `${id}-f`, highlight_id: id, created_at: at }],
    unseen_count: unseen,
  } as unknown as HighlightInboxItem
}

const activeFrame = () => {
  const segs = [...document.querySelectorAll('.sh-hlv__seg')]
  return segs.findIndex(s => s.classList.contains('sh-hlv__seg--active'))
}
/** Let fetches resolve under fake timers. */
const settle = async () => { await vi.advanceTimersByTimeAsync(0) }

beforeEach(() => {
  wsHandlers.clear()
  routeSpy.mockReset()
  toastSpy.mockReset()
  confirmSpy.mockReset().mockResolvedValue(true)
  vi.mocked(api.get).mockReset()
  vi.mocked(api.post).mockReset().mockResolvedValue({})
  vi.mocked(api.put).mockReset().mockResolvedValue({})
  vi.mocked(api.delete).mockReset().mockResolvedValue({})
  currentUser.value = { user_id: 'uid-me', username: 'me', display_name: 'Me' } as User
  householdUsers.value = new Map()
  spaces.value = []
  connections.value = []
  routeQuery.current = {}
  today = []
  detailBody = {}
  _resetViewerReturnForTests()
  document.documentElement.dir = 'ltr'
})

describe('HighlightViewerPage people', () => {
  it('names a remote author from the server, with their picture', async () => {
    detailBody = {
      highlight: highlight('ru-amaka', {
        author: { display_name: 'Amaka Okafor', picture_url: 'api/users/ru-amaka/picture?v=1&sig=s' },
      }),
      frames,
    }
    route()
    const { container } = render(<HighlightViewerPage />)
    await waitFor(() => expect(container.querySelector('.sh-hlv__name')?.textContent).toBe('Amaka Okafor'))
    expect(container.querySelector('.sh-hlv__head img')?.getAttribute('src'))
      .toBe('api/users/ru-amaka/picture?v=1&sig=s')
    expect(container.textContent).not.toContain('Someone')
  })

  it('"Seen by" lists each viewer once with their reaction, refetching on a view frame', async () => {
    const first = {
      highlight: highlight('uid-me', { author: { display_name: 'Me', picture_url: null } }),
      frames,
      views: { f1: [{
        viewer_user_id: 'ru-amaka', viewed_at: '2026-10-19T09:00:00Z',
        display_name: 'Amaka Okafor', picture_url: null,
      }] },
      reactions: { f1: [{
        reactor_user_id: 'ru-amaka', emoji: '🔥', reacted_at: '2026-10-19T09:00:00Z',
        display_name: 'Amaka Okafor', picture_url: null,
      }] },
    }
    const second = {
      ...first,
      views: { f1: [...first.views.f1, {
        viewer_user_id: 'ru-chidi', viewed_at: '2026-10-19T10:00:00Z',
        display_name: 'Chidi Okafor', picture_url: null,
      }] },
    }
    let detailCalls = 0
    route(path => {
      if (path !== '/api/highlights/h1') return undefined
      detailCalls += 1
      return detailCalls === 1 ? first : second
    })
    const { findByRole, findByText } = render(<HighlightViewerPage />)
    fireEvent.click(await findByRole('button', { name: 'Seen by 1' }))
    const sheet = await findByRole('dialog', { name: 'Seen by 1 · frame 1 of 1' })
    const rows = [...sheet.querySelectorAll('.sh-hlv-seen__row')]
    expect(rows.map(r => r.querySelector('.sh-hlv-row__t')?.textContent)).toEqual(['Amaka Okafor'])
    expect(rows[0].textContent).toContain('🔥')
    expect(sheet.textContent).toContain('Only you see this list')
    // A friend on another household views the frame: the WS frame carries
    // only their id, so the page refetches for the server-named row.
    wsHandlers.get('highlight.frame_viewed')!({
      data: { highlight_id: 'h1', frame_id: 'f1', viewer_user_id: 'ru-chidi' },
    })
    expect(await findByText('Chidi Okafor')).toBeTruthy()
    expect(detailCalls).toBe(2)
    expect(document.body.textContent).not.toContain('ru-chidi')
  })
})

describe('HighlightViewerPage opened from a share card', () => {
  // The share post is what lets a space member outside the audience read
  // the highlight — the detail read, the view mark and a reaction all
  // carry it, or the server answers 404.
  it('passes the share post on the detail read, view and reaction', async () => {
    routeQuery.current = { via_post: 'p 1' }
    detailBody = { highlight: highlight('ru-amaka', { author: { display_name: 'Amaka', picture_url: null } }), frames }
    route()
    const { findByRole } = render(<HighlightViewerPage />)
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/api/highlights/frames/f1/view?via_post=p%201', {},
    ))
    expect(api.get).toHaveBeenCalledWith('/api/highlights/h1?via_post=p%201')
    fireEvent.click(await findByRole('button', { name: 'React with ❤️' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(
      '/api/highlights/frames/f1/reaction?via_post=p%201', { emoji: '❤️' },
    ))
  })
})

describe('HighlightViewerPage reactions (your own, under the frame)', () => {
  const open = async (mine: string | null) => {
    detailBody = {
      highlight: highlight('ru-anna', { author: { display_name: 'Anna Berger', picture_url: null } }),
      frames: [{ ...frames[0], my_reaction: mine }],
    }
    route()
    const view = render(<HighlightViewerPage />)
    await view.findByRole('group', { name: 'React to this frame' })
    return view
  }

  it('shows your reaction pressed; tapping it again removes it', async () => {
    const { getByRole } = await open('😂')
    const mine = getByRole('button', { name: '😂, your reaction. Select it again to remove it' })
    expect(mine.getAttribute('aria-pressed')).toBe('true')
    expect(getByRole('button', { name: 'React with ❤️' }).getAttribute('aria-pressed')).toBe('false')
    fireEvent.click(mine)
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/highlights/frames/f1/reaction'))
    expect(getByRole('button', { name: 'React with 😂' }).getAttribute('aria-pressed')).toBe('false')
    expect(api.put).not.toHaveBeenCalled()
  })

  it('another emoji changes it; a failed change puts yours back', async () => {
    const { getByRole } = await open('😂')
    fireEvent.click(getByRole('button', { name: 'React with 🔥' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith('/api/highlights/frames/f1/reaction', { emoji: '🔥' }))
    expect(getByRole('button', { name: /🔥, your reaction/ }).getAttribute('aria-pressed')).toBe('true')
    vi.mocked(api.put).mockRejectedValueOnce(new Error('offline'))
    fireEvent.click(getByRole('button', { name: 'React with 👏' }))
    await waitFor(() => expect(toastSpy).toHaveBeenCalledWith(expect.stringContaining('offline'), 'error'))
    expect(getByRole('button', { name: /🔥, your reaction/ }).getAttribute('aria-pressed')).toBe('true')
  })

  it('the tray sits under the frame, never over the caption', async () => {
    detailBody = {
      highlight: highlight('ru-anna', { author: { display_name: 'Anna', picture_url: null } }),
      frames: [{ ...frames[0], caption_text: 'Guten Morgen' }],
    }
    route()
    const { findByRole, container } = render(<HighlightViewerPage />)
    const tray = await findByRole('group', { name: 'React to this frame' })
    expect(container.querySelector('.sh-hlv__stage')?.contains(tray)).toBe(false)
    const cap = container.querySelector('.sh-hlv__stage .sh-hlv__cap')!
    expect(cap.textContent).toBe('Guten Morgen')
    expect(cap.getAttribute('dir')).toBe('auto')
  })
})

describe('HighlightViewerPage private reply (owner decision 11)', () => {
  it('opens the 1:1 chat with the author, quotes the frame and links to the chat', async () => {
    householdUsers.value = new Map([['uid-anna', { user_id: 'uid-anna', username: 'anna', display_name: 'Anna Berger' } as User]])
    detailBody = { highlight: highlight('uid-anna', { author: { display_name: 'Anna Berger', picture_url: null } }), frames }
    route()
    vi.mocked(api.post).mockImplementation(async (path: string) => (
      path === '/api/conversations/dm' ? { id: 'c-9' } : {}
    ))
    const { findByRole } = render(<HighlightViewerPage />)
    const field = await findByRole('textbox', { name: 'Reply to Anna privately…' })
    fireEvent.input(field, { target: { value: 'So schön!' } })
    fireEvent.click(await findByRole('button', { name: 'Send' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/api/highlights/frames/f1/dm-reply', { conversation_id: 'c-9', content: 'So schön!' },
    ))
    expect(api.post).toHaveBeenCalledWith('/api/conversations/dm', { username: 'anna' })
    const [msg, kind, opts] = toastSpy.mock.calls.at(-1)!
    expect([msg, kind]).toEqual(['Sent to Anna', 'success'])
    opts.action.onClick()
    expect(routeSpy).toHaveBeenCalledWith('/dms/c-9')
    // Never the old dead end into an empty chat list.
    expect(routeSpy).not.toHaveBeenCalledWith(expect.stringContaining('highlight_frame_id'))
  })

  it('is not offered on your own highlight', async () => {
    detailBody = { highlight: highlight('uid-me'), frames, views: { f1: [] }, reactions: { f1: [] } }
    route()
    const { findByRole, queryByRole } = render(<HighlightViewerPage />)
    await findByRole('button', { name: 'Share' })
    expect(queryByRole('textbox')).toBeNull()
    expect(queryByRole('group', { name: 'React to this frame' })).toBeNull()
  })
})

describe('HighlightViewerPage pause, keys and sheets', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] }) })
  afterEach(() => { vi.useRealTimers() })

  const open = async (author: string) => {
    detailBody = {
      highlight: highlight(author, { author: { display_name: 'Anna', picture_url: null } }),
      frames: two,
      views: { f1: [], f2: [] },
      reactions: { f1: [], f2: [] },
    }
    route()
    const view = render(<HighlightViewerPage />)
    await settle()
    expect(activeFrame()).toBe(0)
    return view
  }

  it('Pause stops the frame; Space and K toggle it from the keyboard', async () => {
    const { getByRole } = await open('ru-anna')
    fireEvent.click(getByRole('button', { name: 'Pause (Space)' }))
    await vi.advanceTimersByTimeAsync(30000)
    expect(activeFrame()).toBe(0)
    expect(document.querySelector('.sh-hlv--paused')).not.toBeNull()
    fireEvent.keyDown(document.body, { key: ' ' })
    await settle()
    expect(getByRole('button', { name: 'Pause (Space)' })).toBeTruthy()
    fireEvent.keyDown(document.body, { key: 'k' })
    await settle()
    expect(getByRole('button', { name: 'Play (Space)' })).toBeTruthy()
    fireEvent.keyDown(document.body, { key: 'K' })
    await vi.advanceTimersByTimeAsync(6100)
    expect(activeFrame()).toBe(1)
  })

  it('a video pauses with the timer', async () => {
    detailBody = {
      highlight: highlight('ru-anna'),
      frames: [{ ...frames[0], frame_type: 'video', media_url: 'api/media/v.webm', duration_ms: 4000 }],
    }
    route()
    const pause = vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {})
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockImplementation(async () => {})
    const { getByRole } = render(<HighlightViewerPage />)
    await settle()
    fireEvent.click(getByRole('button', { name: 'Pause (Space)' }))
    await settle()
    expect(pause).toHaveBeenCalled()
    vi.restoreAllMocks()
  })

  it('the ⋯ sheet has a heading and a ✕, pauses the frame, and Escape closes only the sheet', async () => {
    const { getByRole } = await open('uid-me')
    await vi.advanceTimersByTimeAsync(4000)
    fireEvent.click(getByRole('button', { name: 'More' }))
    const sheet = getByRole('dialog', { name: 'Frame 1 of 2 · paused' })
    expect(within(sheet).getByRole('button', { name: 'Close dialog' })).toBeTruthy()
    await vi.advanceTimersByTimeAsync(30000)
    expect(activeFrame()).toBe(0)
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await settle()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(routeSpy).not.toHaveBeenCalled()
    // 4 s of the 6 s frame were shown: 2 s are left, not a fresh 6 s.
    await vi.advanceTimersByTimeAsync(1900)
    expect(activeFrame()).toBe(0)
    await vi.advanceTimersByTimeAsync(200)
    expect(activeFrame()).toBe(1)
  })

  it('author menu: delete one frame says the others stay; delete the day removes all, for everyone', async () => {
    const { getByRole } = await open('uid-me')
    fireEvent.click(getByRole('button', { name: 'More' }))
    const sheet = getByRole('dialog', { name: 'Frame 1 of 2 · paused' })
    expect(sheet.textContent).toContain('The other frame stays')
    expect(sheet.textContent).toContain('All 2 frames, for everyone')
    fireEvent.click(within(sheet).getByRole('button', { name: /Delete this highlight/ }))
    await settle()
    expect(confirmSpy).toHaveBeenCalledWith(
      expect.stringContaining('Delete all 2 frames'), expect.objectContaining({ destructive: true }),
    )
    expect(api.delete).toHaveBeenCalledWith('/api/highlights/h1')
    expect(routeSpy).toHaveBeenCalledWith('/highlights', true)
  })

  it('someone else\'s ⋯ offers their profile and Report', async () => {
    const { getByRole } = await open('ru-anna')
    fireEvent.click(getByRole('button', { name: 'More' }))
    const sheet = getByRole('dialog', { name: 'Frame 1 of 2 · paused' })
    expect(within(sheet).getByRole('button', { name: 'About Anna' })).toBeTruthy()
    expect(within(sheet).getByRole('button', { name: 'Report this highlight…' })).toBeTruthy()
    expect(sheet.textContent).not.toContain('Delete')
  })

  it('typing a reply holds the frame; keys typed there never page the viewer', async () => {
    const { getByRole } = await open('ru-anna')
    const field = getByRole('textbox')
    fireEvent.focus(field)
    for (const key of ['ArrowRight', 'ArrowLeft', ' ', 'k']) {
      expect(fireEvent.keyDown(field, { key })).toBe(true)  // not prevented
    }
    await vi.advanceTimersByTimeAsync(30000)
    expect(activeFrame()).toBe(0)
    expect(routeSpy).not.toHaveBeenCalled()
    fireEvent.blur(field)
    await vi.advanceTimersByTimeAsync(6100)
    expect(activeFrame()).toBe(1)
  })

  it('right-to-left: the arrow keys follow the reading direction (C17)', async () => {
    document.documentElement.dir = 'rtl'
    await open('ru-anna')
    fireEvent.keyDown(document.body, { key: 'ArrowLeft' })
    await settle()
    expect(activeFrame()).toBe(1)
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    await settle()
    expect(activeFrame()).toBe(0)
  })
})

describe('HighlightViewerPage between people (C8)', () => {
  beforeEach(() => { vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] }) })
  afterEach(() => { vi.useRealTimers() })

  it('after the last frame it moves on to the next person with unseen frames', async () => {
    today = [
      todayItem('h1', 'ru-anna', 'Anna', 2, 10),
      todayItem('h-tom', 'ru-tom', 'Thomas Berger', 1, 20),
    ]
    detailBody = { highlight: highlight('ru-anna', { author: { display_name: 'Anna', picture_url: null } }), frames }
    route()
    const { container } = render(<HighlightViewerPage />)
    await settle()
    expect(container.querySelector('.sh-hlv__side--end')?.textContent).toContain('Up next · Thomas')
    await vi.advanceTimersByTimeAsync(6100)
    expect(routeSpy).toHaveBeenCalledWith('/highlights/h-tom', true)
  })

  it('with nobody left it closes back to where it was opened from', async () => {
    noteViewerRoute('/spaces/s1')
    noteViewerRoute('/highlights/h1')
    const back = vi.spyOn(window.history, 'back').mockImplementation(() => {})
    today = [todayItem('h1', 'ru-anna', 'Anna', 1, 10)]
    detailBody = { highlight: highlight('ru-anna'), frames }
    route()
    render(<HighlightViewerPage />)
    await settle()
    await vi.advanceTimersByTimeAsync(6100)
    expect(back).toHaveBeenCalled()
    expect(routeSpy).not.toHaveBeenCalled()
    back.mockRestore()
  })

  it('Close on a deep link (nothing before it) goes to Highlights', async () => {
    noteViewerRoute('/highlights/h1')
    detailBody = { highlight: highlight('ru-anna'), frames }
    route()
    const { getByRole } = render(<HighlightViewerPage />)
    await settle()
    fireEvent.click(getByRole('button', { name: 'Close (Esc)' }))
    expect(routeSpy).toHaveBeenCalledWith('/highlights', true)
  })
})

describe('HighlightViewerPage sharing to Feed or a space (S6 / P6)', () => {
  const open = async (audience: object) => {
    householdUsers.value = new Map([
      ['uid-me', { user_id: 'uid-me', username: 'me' } as User],
      ['uid-lena', { user_id: 'uid-lena', username: 'lena' } as User],
    ])
    detailBody = { highlight: highlight('uid-me', audience), frames, views: { f1: [] }, reactions: { f1: [] } }
    route(path => (path === '/api/spaces'
      ? [{ id: 's1', name: 'Garden club', emoji: '🥕', space_type: 'private', my_role: 'member' },
         { id: 's2', name: 'Follows', emoji: null, space_type: 'private', my_role: 'subscriber' }]
      : path === '/api/spaces/s1/members'
        ? [{ user_id: 'uid-me' }, { user_id: 'ru-x', instance_id: 'inst-far' }]
        : undefined))
    vi.mocked(api.post).mockResolvedValue({ post_id: 'p1' })
    const view = render(<HighlightViewerPage />)
    fireEvent.click(await view.findByRole('button', { name: 'Share' }))
    return view
  }

  it('asks first when the space reaches more people than the highlight', async () => {
    confirmSpy.mockResolvedValue(false)
    const { findByRole, queryByRole } = await open({ audience_kind: 'users', audience: ['uid-lena'] })
    const sheet = await findByRole('dialog', { name: 'Share to Feed or a space' })
    expect(queryByRole('button', { name: 'Follows' })).toBeNull()  // a follower can't post there
    fireEvent.click(within(sheet).getByRole('button', { name: 'Garden club' }))
    await waitFor(() => expect(confirmSpy).toHaveBeenCalledWith(
      'Everyone in Garden club will see today\'s frames, also people who can\'t see it now.',
      expect.objectContaining({ title: 'Show it to more people?' }),
    ))
    expect(api.post).not.toHaveBeenCalledWith('/api/highlights/h1/share', expect.anything())
  })

  it('shares straight away when the household feed is no wider', async () => {
    const { findByRole } = await open({ audience_kind: 'all_paired', audience: [] })
    const sheet = await findByRole('dialog', { name: 'Share to Feed or a space' })
    fireEvent.click(within(sheet).getByRole('button', { name: /Household feed/ }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/highlights/h1/share', { scope: 'household' }))
    expect(confirmSpy).not.toHaveBeenCalled()
    expect(toastSpy).toHaveBeenCalledWith('Highlight shared', 'success')
  })
})
