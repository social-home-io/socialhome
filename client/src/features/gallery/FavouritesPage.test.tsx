import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPut = vi.fn()
const apiDelete = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: (...a: unknown[]) => apiPost(...a),
    put: (...a: unknown[]) => apiPut(...a),
    delete: (...a: unknown[]) => apiDelete(...a),
  },
  ApiError: class extends Error {},
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a), dismissToast: vi.fn() }))
vi.mock('@/store/auth', () => ({ currentUser: { value: { user_id: 'u1', is_admin: false } } }))
const people = new Map([['u-lena', { user_id: 'u-lena', display_name: 'Lena', username: 'lena' }]])
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { get value() { return people } },
  loadHouseholdUsers: vi.fn(async () => undefined),
}))
vi.mock('@/store/spaceMembers', () => ({
  spaceMembers: { value: {} },
  loadSpaceMembers: vi.fn(async () => undefined),
}))
const route = vi.fn()
vi.mock('preact-iso', () => ({ useLocation: () => ({ route }) }))

function row(id: string, extra: Record<string, unknown> = {}) {
  return {
    id, album_id: 'al-1', uploaded_by: 'u-lena', item_type: 'photo',
    url: `api/media/${id}.webp`, thumbnail_url: `api/media/t-${id}.webp`,
    width: 100, height: 80, created_at: '2026-07-14T10:00:00+00:00',
    is_favourite: true, favourited_at: '2026-11-01T10:00:00+00:00',
    album: { id: 'al-1', name: 'Sommer 2026', space_id: null, is_system: false, default_name: false },
    ...extra,
  }
}

beforeEach(() => {
  apiGet.mockReset(); apiPost.mockReset(); apiPut.mockReset(); apiDelete.mockReset()
  showToast.mockReset(); route.mockReset()
})
afterEach(() => cleanup())

async function mount(rows: unknown[], itemId: string | null = null) {
  apiGet.mockResolvedValue(rows)
  const { default: FavouritesPage } = await import('./FavouritesPage')
  return render(<FavouritesPage itemId={itemId} />)
}

describe('FavouritesPage', () => {
  it('lists your favourites as links into the viewer and says only you see them', async () => {
    const view = await mount([row('a'), row('b')])
    expect(apiGet).toHaveBeenCalledWith('/api/gallery/favourites?limit=60')
    const tiles = await view.findAllByRole('link', { name: 'Photo from “Sommer 2026”' })
    expect(tiles.map(t => t.getAttribute('href'))).toEqual(['/gallery/favourites/a', '/gallery/favourites/b'])
    expect(view.getByText(/Only you see your favourites/)).toBeTruthy()
  })

  it('an empty list explains how to add one', async () => {
    const view = await mount([])
    expect(await view.findByRole('heading', { name: 'No favourites yet' })).toBeTruthy()
    expect(view.getByText(/tap ♡ Favourite/)).toBeTruthy()
  })

  it('a failed load offers Retry, never "No favourites yet"', async () => {
    apiGet.mockRejectedValue(new Error('offline'))
    const { default: FavouritesPage } = await import('./FavouritesPage')
    const view = render(<FavouritesPage itemId={null} />)
    expect(await view.findByRole('button', { name: 'Retry' })).toBeTruthy()
    expect(view.queryByText('No favourites yet')).toBeNull()
  })

  it('the next page asks with the last row\'s cursor', async () => {
    const page = Array.from({ length: 60 }, (_, n) => row(`r${n}`))
    const view = await mount(page)
    apiGet.mockResolvedValueOnce([row('older')])
    fireEvent.click(await view.findByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(apiGet).toHaveBeenLastCalledWith(
      '/api/gallery/favourites?limit=60&before=2026-11-01T10%3A00%3A00%2B00%3A00&before_id=r59',
    ))
  })

  it('the viewer opens on the address\'s item with the album name, and ♡ un-hearts it', async () => {
    apiDelete.mockResolvedValue(undefined)
    const view = await mount([row('a'), row('b')], 'b')
    expect(await view.findByRole('dialog')).toBeTruthy()
    expect(view.getByText('2 of 2 · Sommer 2026')).toBeTruthy()
    const heart = view.getByRole('button', { name: 'Favourite' })
    expect(heart.getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(heart)
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/gallery/items/b/favourite'))
    await waitFor(() => expect(view.getByRole('button', { name: 'Favourite' }).getAttribute('aria-pressed')).toBe('false'))
    // No delete in this list: deleting is the album's business.
    expect(view.queryByRole('button', { name: 'Delete' })).toBeNull()
  })

  it('selected favourites come off the list with Undo', async () => {
    apiPost.mockResolvedValue({ done: ['a'], refused: [] })
    const view = await mount([row('a'), row('b')])
    fireEvent.click(await view.findByRole('button', { name: 'Select' }))
    fireEvent.click(view.getAllByRole('checkbox')[0])
    fireEvent.click(view.getByRole('button', { name: 'Remove from favourites' }))
    await waitFor(() => expect(apiPost).toHaveBeenCalledWith(
      '/api/gallery/bulk', { action: 'unfavourite', item_ids: ['a'] }, {},
    ))
    await waitFor(() => expect(view.getAllByRole('link')).toHaveLength(1))
    const [message, , opts] = showToast.mock.calls.at(-1)!
    expect(message).toMatch(/removed from your favourites/)
    expect((opts as { action: { label: string } }).action.label).toBe('Undo')
  })
})
