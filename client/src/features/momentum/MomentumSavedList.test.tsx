import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent } from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
const route = vi.hoisted(() => vi.fn())
vi.mock('preact-iso', () => ({
  useLocation: () => ({ route, url: '/momentum?tab=yours&show=saved', path: '/momentum' }),
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
const wsHandlers = vi.hoisted(() => new Map<string, () => void>())
vi.mock('@/ws', () => ({
  ws: { on: (type: string, fn: () => void) => { wsHandlers.set(type, fn); return () => undefined } },
}))

import { api } from '@/api'
import type { Moment } from '@/types'
import MomentumSavedList, { SAVED_PAGE } from './MomentumSavedList'

const DAY = 24 * 3_600_000

function saved(id: string, savedAgoMs: number, extra: Partial<Moment> = {}): Moment {
  const created = Date.now() - 10 * DAY
  return {
    id, author_user_id: 'ru-amaka', content: `text ${id}`,
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'peer-o',
    created_at: new Date(created).toISOString(),
    expires_at: new Date(created + 7 * DAY).toISOString(),
    reaction_count: 0, reply_count: 0, received_via: 'household',
    author: { display_name: 'Amaka Okafor', picture_url: null },
    saved_by_me: true, can_save: true,
    saved_at: new Date(Date.now() - savedAgoMs).toISOString(),
    ...extra,
  }
}

type Params = Record<string, string> | undefined
function serve(pages: (params: Params) => Moment[] | Promise<Moment[]>) {
  vi.mocked(api.get).mockImplementation(async (path: string, params?: Params) => {
    if (path === '/api/moments/archive') return pages(params)
    throw new Error(`unexpected ${path}`)
  })
}

describe('MomentumSavedList', () => {
  beforeEach(() => {
    wsHandlers.clear()
    vi.mocked(api.get).mockReset()
    vi.mocked(api.delete).mockReset()
    route.mockReset()
  })

  it('lists your saved moments from scope=saved with their author and says how long they stay', async () => {
    serve(() => [saved('m-a', 1000), saved('m-b', DAY)])
    const { findByText, getByText, getAllByRole } = render(<MomentumSavedList />)
    await findByText('text m-a')
    expect(api.get).toHaveBeenCalledWith('/api/moments/archive', { scope: 'saved', limit: String(SAVED_PAGE) })
    getByText('text m-b')
    expect(getByText(/stay with you until the author deletes them/)).toBeTruthy()
    expect(getAllByRole('button', { name: /Saved/ }).every(b => b.getAttribute('aria-pressed') === 'true')).toBe(true)
    expect(getAllByRole('link')[0].textContent).toContain('Amaka Okafor')
  })

  it('opens a saved moment', async () => {
    serve(() => [saved('m-a', 1000)])
    const { findByText } = render(<MomentumSavedList />)
    fireEvent.click(await findByText('text m-a'))
    expect(route).toHaveBeenCalledWith('/momentum/m-a')
  })

  it('removes a moment from the list when you unsave it', async () => {
    serve(() => [saved('m-a', 1000), saved('m-b', 2000)])
    vi.mocked(api.delete).mockResolvedValue({ moment_id: 'm-a', saved: false })
    const { findByText, getAllByRole, queryByText } = render(<MomentumSavedList />)
    await findByText('text m-a')
    fireEvent.click(getAllByRole('button', { name: /Saved/ })[0])
    await waitFor(() => expect(queryByText('text m-a')).toBeNull())
    expect(api.delete).toHaveBeenCalledWith('/api/moments/m-a/save')
    expect(queryByText('text m-b')).not.toBeNull()
  })

  it('pages older saves with the saved_at cursor', async () => {
    const first = Array.from({ length: SAVED_PAGE }, (_, i) => saved(`m-${i}`, (i + 1) * 1000))
    const older = [saved('m-older', 99 * DAY)]
    serve((p) => (p?.before ? older : first))
    const { findByText, getByRole } = render(<MomentumSavedList />)
    await findByText('text m-0')
    fireEvent.click(getByRole('button', { name: 'Load older' }))
    await findByText('text m-older')
    expect(api.get).toHaveBeenLastCalledWith('/api/moments/archive', {
      scope: 'saved', limit: String(SAVED_PAGE), before: first[SAVED_PAGE - 1].saved_at,
    })
  })

  it('shows the empty state, and an error (not the empty state) when loading fails', async () => {
    serve(() => [])
    const empty = render(<MomentumSavedList />)
    await empty.findByText('No saved moments')
    empty.unmount()

    serve(() => { throw new Error('offline') })
    const failed = render(<MomentumSavedList />)
    await failed.findByText("Couldn't load your saved moments")
    expect(failed.queryByText('No saved moments')).toBeNull()
  })

  it('refreshes when a moment is deleted', async () => {
    let rows = [saved('m-a', 1000)]
    serve(() => rows)
    const { findByText, queryByText } = render(<MomentumSavedList />)
    await findByText('text m-a')
    rows = []
    wsHandlers.get('moment.deleted')!()
    await waitFor(() => expect(queryByText('text m-a')).toBeNull())
  })
})
