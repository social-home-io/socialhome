import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const wsHandlers = vi.hoisted(() => new Map<string, (e: { data: unknown }) => void>())
const routeQuery = vi.hoisted(() => ({ current: {} as Record<string, string> }))
const routeSpy = vi.hoisted(() => vi.fn())

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { highlightId: 'h1' }, query: routeQuery.current }),
  useLocation: () => ({ route: routeSpy, url: '/' }),
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
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
import type { User } from '@/types'
import HighlightViewerPage from './HighlightViewerPage'

const highlight = (author: string, extra: object = {}) => ({
  id: 'h1', author_user_id: author, highlight_date: '2026-10-19',
  audience_kind: 'all_paired', audience: [],
  created_at: '2026-10-19T08:00:00Z', expires_at: '2026-10-20T08:00:00Z',
  ...extra,
})
const frames = [{
  id: 'f1', highlight_id: 'h1', sequence: 1, frame_type: 'image',
  media_url: 'api/media/x.webp', caption_text: null, caption_emoji: null,
  duration_ms: null, created_at: '2026-10-19T08:00:00Z',
}]

describe('HighlightViewerPage people', () => {
  beforeEach(() => {
    wsHandlers.clear()
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockResolvedValue({})
    currentUser.value = { user_id: 'uid-me', username: 'me', display_name: 'Me' } as User
  })

  it('names a remote author from the server, with their picture', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => path === '/api/users' ? [] : {
      highlight: highlight('ru-amaka', {
        author: {
          display_name: 'Amaka Okafor',
          picture_url: 'api/users/ru-amaka/picture?v=1&sig=s',
        },
      }),
      frames,
    })
    const { container } = render(<HighlightViewerPage />)
    await waitFor(() => expect(
      container.querySelector('.sh-highlight-viewer-header strong')?.textContent,
    ).toBe('Amaka Okafor'))
    expect(
      container.querySelector('.sh-highlight-viewer-header img')?.getAttribute('src'),
    ).toBe('api/users/ru-amaka/picture?v=1&sig=s')
    expect(container.textContent).not.toContain('Someone')
  })

  it('"Seen by" names remote viewers / reactors and refetches on a view frame', async () => {
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
      views: { f1: [
        ...first.views.f1,
        {
          viewer_user_id: 'ru-chidi', viewed_at: '2026-10-19T10:00:00Z',
          display_name: 'Chidi Okafor', picture_url: null,
        },
      ] },
    }
    let detailCalls = 0
    vi.mocked(api.get).mockImplementation(async (path: string) => {
      if (path === '/api/users') return []
      detailCalls += 1
      return detailCalls === 1 ? first : second
    })
    const { container, findByRole, findByText } = render(<HighlightViewerPage />)
    fireEvent.click(await findByRole('button', { name: /Seen by/ }))
    await waitFor(() => {
      const names = [...document.querySelectorAll('.sh-seenby-name')]
        .map(e => e.textContent?.trim())
      expect(names).toEqual(['Amaka Okafor', 'Amaka Okafor'])
    })
    // A friend on another household views the frame: the WS frame carries
    // only their id, so the page refetches for the server-named row.
    wsHandlers.get('highlight.frame_viewed')!({
      data: { highlight_id: 'h1', frame_id: 'f1', viewer_user_id: 'ru-chidi' },
    })
    expect(await findByText('Chidi Okafor')).toBeTruthy()
    expect(detailCalls).toBe(2)
    expect(document.body.textContent).not.toContain('ru-chidi')
    expect(container.textContent).not.toContain('Someone')
  })
})

describe('HighlightViewerPage opened from a share card', () => {
  beforeEach(() => {
    wsHandlers.clear()
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset().mockResolvedValue({})
    vi.mocked(api.put).mockReset().mockResolvedValue({})
    currentUser.value = { user_id: 'uid-me', username: 'me', display_name: 'Me' } as User
    routeQuery.current = { via_post: 'p 1' }
  })

  // The share post is what lets a space member outside the audience read
  // the highlight — the detail read, the view mark and a reaction all
  // carry it, or the server answers 404.
  it('passes the share post on the detail read, view and reaction', async () => {
    vi.mocked(api.get).mockImplementation(async (path: string) => path === '/api/users' ? [] : {
      highlight: highlight('ru-amaka', { author: { display_name: 'Amaka', picture_url: null } }),
      frames,
    })
    const { findByRole } = render(<HighlightViewerPage />)
    await waitFor(() => expect(api.post).toHaveBeenCalledWith(
      '/api/highlights/frames/f1/view?via_post=p%201', {},
    ))
    expect(api.get).toHaveBeenCalledWith('/api/highlights/h1?via_post=p%201')
    fireEvent.click(await findByRole('button', { name: /react/i }))
    fireEvent.click(await findByRole('button', { name: '❤️' }))
    await waitFor(() => expect(api.put).toHaveBeenCalledWith(
      '/api/highlights/frames/f1/reaction?via_post=p%201', { emoji: '❤️' },
    ))
    routeQuery.current = {}
  })
})

describe('HighlightViewerPage with a dialog or sheet open', () => {
  const two = [
    frames[0],
    { ...frames[0], id: 'f2', sequence: 2, media_url: 'api/media/y.webp' },
  ]
  const activeFrame = () => {
    const segs = [...document.querySelectorAll('.sh-highlight-progress-seg')]
    return segs.findIndex(s => s.classList.contains('sh-highlight-progress-seg--active'))
  }
  /** Let the detail fetch resolve under fake timers. */
  const settle = async () => { await vi.advanceTimersByTimeAsync(0) }

  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'Date'] })
    wsHandlers.clear()
    routeSpy.mockReset()
    vi.mocked(api.get).mockReset()
    vi.mocked(api.post).mockReset().mockResolvedValue({})
    vi.mocked(api.put).mockReset().mockResolvedValue({})
    currentUser.value = { user_id: 'uid-me', username: 'me', display_name: 'Me' } as User
  })
  afterEach(() => { vi.useRealTimers() })

  const open = async (author: string) => {
    vi.mocked(api.get).mockImplementation(async (path: string) => path === '/api/users' ? [] : {
      highlight: highlight(author, { author: { display_name: 'Anna', picture_url: null } }),
      frames: two,
      views: { f1: [], f2: [] },
      reactions: { f1: [], f2: [] },
    })
    const view = render(<HighlightViewerPage />)
    await settle()
    expect(activeFrame()).toBe(0)
    return view
  }

  it('Escape closes the "Seen by" sheet and leaves the viewer open', async () => {
    const { getByRole } = await open('uid-me')
    fireEvent.click(getByRole('button', { name: /views|Seen by/i }))
    expect(document.querySelector('[role="dialog"]')).not.toBeNull()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await settle()
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(routeSpy).not.toHaveBeenCalled()
    // With no dialog left, Escape closes the viewer as before.
    fireEvent.keyDown(document.body, { key: 'Escape' })
    expect(routeSpy).toHaveBeenCalledWith('/highlights')
  })

  it('the frame timer stops while a dialog is open and picks up where it left off', async () => {
    const { getByRole } = await open('uid-me')
    await vi.advanceTimersByTimeAsync(4000)
    fireEvent.click(getByRole('button', { name: /views|Seen by/i }))
    await vi.advanceTimersByTimeAsync(30000)
    expect(activeFrame()).toBe(0)
    expect(document.querySelector('.sh-highlight-viewer--paused')).not.toBeNull()
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' })
    await settle()
    // 4 s of the 6 s frame were shown: 2 s are left, not a fresh 6 s.
    await vi.advanceTimersByTimeAsync(1900)
    expect(activeFrame()).toBe(0)
    await vi.advanceTimersByTimeAsync(200)
    expect(activeFrame()).toBe(1)
  })

  it('the frame timer stops while the reaction tray is open', async () => {
    const { getByRole } = await open('ru-anna')
    fireEvent.click(getByRole('button', { name: /react/i }))
    await vi.advanceTimersByTimeAsync(30000)
    expect(activeFrame()).toBe(0)
  })

  it('a reaction goes to the frame the tray was opened on', async () => {
    const { getByRole, queryByRole } = await open('ru-anna')
    fireEvent.click(getByRole('button', { name: /react/i }))
    // The person steps on while the tray is up: it closes rather than
    // send a reaction meant for frame 1 to frame 2.
    fireEvent.keyDown(document.body, { key: 'ArrowRight' })
    await settle()
    expect(activeFrame()).toBe(1)
    expect(queryByRole('button', { name: '❤️' })).toBeNull()
    fireEvent.click(getByRole('button', { name: /react/i }))
    fireEvent.click(getByRole('button', { name: '❤️' }))
    await settle()
    expect(api.put).toHaveBeenCalledTimes(1)
    expect(api.put).toHaveBeenCalledWith('/api/highlights/frames/f2/reaction', { emoji: '❤️' })
  })

  it('keys typed in a field never page the viewer or block the caret', async () => {
    await open('ru-anna')
    const field = document.createElement('input')
    document.body.append(field)
    for (const key of ['ArrowRight', 'ArrowLeft', 'Escape']) {
      expect(fireEvent.keyDown(field, { key })).toBe(true)  // not prevented
    }
    await settle()
    expect(activeFrame()).toBe(0)
    expect(routeSpy).not.toHaveBeenCalled()
    field.remove()
  })
})
