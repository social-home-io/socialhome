import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPut = vi.fn(async () => undefined)
const apiDelete = vi.fn(async () => undefined)
class FakeApiError extends Error {
  constructor(public status: number) { super(`HTTP ${status}`) }
}
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn(),
    patch: vi.fn(),
    put: (...args: unknown[]) => apiPut(...(args as [])),
    delete: (...args: unknown[]) => apiDelete(...(args as [])),
  },
  ApiError: FakeApiError,
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u1', is_admin: false, protected: false } },
}))
vi.mock('@/ws', () => ({ ws: { on: () => () => undefined } }))
vi.mock('@/store/notifications', () => ({ markSeen: vi.fn() }))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({
  showToast: (...a: unknown[]) => showToast(...a),
  dismissToast: vi.fn(),
}))
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { value: new Map() },
  loadHouseholdUsers: vi.fn(async () => undefined),
}))
vi.mock('@/store/spaceMembers', () => ({
  spaceMembers: { value: {} },
  loadSpaceMembers: vi.fn(async () => undefined),
}))

function row(n: number, extra: Record<string, unknown> = {}) {
  return {
    id: `it-${n}`, album_id: 'al-1', uploaded_by: 'u1', item_type: 'photo',
    url: `api/media/p${n}.webp`, thumbnail_url: `api/media/t${n}.webp`,
    width: 10, height: 10, created_at: '2026-10-01T00:00:00+00:00',
    album: { id: 'al-1', name: 'Kroatien', space_id: null, is_system: false, default_name: false },
    snippet: '', is_favourite: false,
    ...extra,
  }
}

let found: { items: unknown[]; albums: unknown[]; more: boolean }
function split(url: string): [string, Record<string, string>] {
  const [path, q = ''] = url.split('?')
  return [path, Object.fromEntries(new URLSearchParams(q))]
}

beforeEach(() => {
  vi.resetModules()
  showToast.mockReset()
  apiGet.mockReset()
  found = { items: [], albums: [], more: false }
  apiGet.mockImplementation(async (url: string) => {
    const [path] = split(url)
    if (path === '/api/gallery/albums' || path === '/api/spaces/sp-1/gallery/albums') {
      return [{
        id: 'al-1', name: 'Kroatien', space_id: null, owner_user_id: 'u1', cover_url: null,
        item_count: 2, retention_exempt: false, created_at: '2026-10-01T00:00:00+00:00',
      }]
    }
    if (path === '/api/gallery/on-this-day') return { card: null, settings: null }
    if (path === '/api/gallery/default-album') return { album: null }
    if (path === '/api/gallery/search') return found
    return []
  })
})
afterEach(() => { cleanup() })

/** The gallery as the app mounts it, behind the router at ``at``. */
async function mount(spaceId: string | undefined, at: string) {
  history.replaceState(null, '', '/')
  const { default: GalleryPage } = await import('./GalleryPage')
  const { IngressLocationProvider } = await import('@/router/IngressLocationProvider')
  const iso = await import('preact-iso')
  history.replaceState(null, '', at)
  const Page = (_: { path: string }) => <GalleryPage spaceId={spaceId} />
  return render(
    <IngressLocationProvider>
      <iso.Router>
        <Page path="/gallery/:sub*" />
        <Page path="/spaces/:id/:section?/:sub*" />
      </iso.Router>
    </IngressLocationProvider>,
  )
}

function searchCalls(): Record<string, string>[] {
  return apiGet.mock.calls
    .map(c => split(String(c[0])))
    .filter(([p]) => p === '/api/gallery/search')
    .map(([, q]) => q)
}

describe('gallery search', () => {
  it('typing on the albums page moves to /gallery/search?q= and keeps the same field focused', async () => {
    found = { items: [row(1, { snippet: 'Am <mark>Strand</mark>' })], albums: [], more: false }
    const view = await mount(undefined, '/gallery')
    const field = await view.findByRole('searchbox', { name: 'Search photos and videos' })
    field.focus()
    fireEvent.input(field, { target: { value: 'Strand' } })
    await waitFor(() => expect(location.pathname).toBe('/gallery/search'))
    expect(new URLSearchParams(location.search).get('q')).toBe('Strand')
    // Same element: the person keeps typing.
    expect(view.getByRole('searchbox')).toBe(field)
    expect(document.activeElement).toBe(field)
    expect(await view.findByText('1 photo or video')).toBeTruthy()
    expect(searchCalls()[0]).toMatchObject({ q: 'Strand', limit: '60', offset: '0' })
    // The household's search spans every album: no space filter.
    expect(searchCalls()[0].space_id).toBeUndefined()
    // The matched word is marked, as text.
    const tile = view.getByRole('link', { name: /Photo from “Kroatien”: Am Strand/ })
    expect(tile.querySelector('mark')?.textContent).toBe('Strand')
    expect(tile.getAttribute('href')).toBe('/gallery/search/it-1?q=Strand')
  })

  it('a space searches only its own albums (/spaces/:id/gallery/search — an in-app path under the ingress prefix too)', async () => {
    const view = await mount('sp-1', '/spaces/sp-1/gallery/search?q=kaffee')
    await waitFor(() => expect(searchCalls().length).toBe(1))
    expect(searchCalls()[0]).toMatchObject({ q: 'kaffee', space_id: 'sp-1' })
    expect((view.getByRole('searchbox') as HTMLInputElement).value).toBe('kaffee')
  })

  it('nothing found says what search looks at — and that it never looks inside the pictures', async () => {
    const view = await mount(undefined, '/gallery/search?q=beach')
    expect(await view.findByText('Nothing found for “beach”')).toBeTruthy()
    expect(view.getByText(/doesn't look at what's in the pictures/)).toBeTruthy()
  })

  it('matching albums show above the photos and open the album', async () => {
    found = {
      items: [row(1), row(2)],
      albums: [{ id: 'al-1', space_id: null, name: 'Kroatien', item_count: 2, cover_url: null }],
      more: true,
    }
    const view = await mount(undefined, '/gallery/search?q=kro')
    const link = await view.findByRole('link', { name: /Open album Kroatien/ })
    expect(link.getAttribute('href')).toBe('/gallery/albums/al-1')
    expect(view.getByText('2+ photos and videos')).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(searchCalls().some(q => q.offset === '2')).toBe(true))
  })

  it('a result opens the viewer at its own address', async () => {
    found = { items: [row(1), row(2)], albums: [], more: false }
    const view = await mount(undefined, '/gallery/search/it-2?q=kro')
    expect(await view.findByRole('dialog')).toBeTruthy()
  })

  it('a link to something no longer found says so and shows the search', async () => {
    found = { items: [row(1)], albums: [], more: false }
    await mount(undefined, '/gallery/search/gone?q=kro')
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('This photo or video isn\'t here any more.', 'info'))
    await waitFor(() => expect(location.pathname).toBe('/gallery/search'))
  })

  it('one letter waits for more, and clearing the field goes back to the albums', async () => {
    const view = await mount(undefined, '/gallery/search?q=k')
    expect(await view.findByText('Keep typing…')).toBeTruthy()
    expect(searchCalls()).toEqual([])
    fireEvent.click(view.getByRole('button', { name: 'Clear search' }))
    await waitFor(() => expect(location.pathname).toBe('/gallery'))
  })

  it('a failed search offers to try again', async () => {
    apiGet.mockImplementation(async (url: string) => {
      const [path] = split(url)
      if (path === '/api/gallery/search') throw new FakeApiError(500)
      if (path === '/api/gallery/on-this-day') return { card: null, settings: null }
      return []
    })
    const view = await mount(undefined, '/gallery/search?q=kro')
    expect(await view.findByRole('alert')).toBeTruthy()
    expect(view.getByRole('button', { name: 'Retry' })).toBeTruthy()
  })
})
