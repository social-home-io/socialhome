import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const wsHandlers = vi.hoisted(() => new Map<string, (e: { data: unknown }) => void>())

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('preact-iso', () => ({
  useRoute: () => ({ params: { highlightId: 'h1' } }),
  useLocation: () => ({ route: vi.fn(), url: '/' }),
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
