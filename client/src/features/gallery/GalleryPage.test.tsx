import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}))

type Handler = (e: { type: string; data: Record<string, unknown> }) => void
const handlers = new Map<string, Set<Handler>>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, h: Handler) => {
      if (!handlers.has(type)) handlers.set(type, new Set())
      handlers.get(type)!.add(h)
      return () => { handlers.get(type)?.delete(h) }
    },
  },
}))
function emit(type: string, data: Record<string, unknown>) {
  handlers.get(type)?.forEach(h => h({ type, data: { type, ...data } }))
}

const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a) }))

let albumRows: Array<Record<string, unknown>>
function album(id: string, name: string) {
  return {
    id, name, space_id: 'sp-1', owner_user_id: 'u1',
    cover_url: null, item_count: 0, retention_exempt: false,
  }
}

beforeEach(() => {
  vi.resetModules()
  handlers.clear()
  showToast.mockReset()
  apiGet.mockReset()
  albumRows = [album('al-1', 'Holiday')]
  apiGet.mockImplementation(async (url: string) => {
    if (url === '/api/spaces/sp-1/gallery/albums') return albumRows
    if (url.startsWith('/api/gallery/albums/')) return []
    return []
  })
})

async function renderOpenAlbum() {
  const { default: GalleryPage } = await import('./GalleryPage')
  const view = render(<GalleryPage spaceId="sp-1" />)
  fireEvent.click(await view.findByRole('button', { name: /Open album Holiday/ }))
  await view.findByRole('heading', { name: /Holiday/ })
  return view
}

describe('GalleryPage live album frames', () => {
  it('an album rename from another household updates the open album header', async () => {
    const view = await renderOpenAlbum()
    albumRows = [album('al-1', 'Summer 2026')]
    emit('gallery.album_updated', { album_id: 'al-1', space_id: 'sp-1' })
    expect(await view.findByRole('heading', { name: /Summer 2026/ })).toBeTruthy()
    // Quiet refresh: the open album stays on screen (no spinner swap).
    expect(view.getByRole('button', { name: /Albums/ })).toBeTruthy()
  })

  it('ignores an album frame for another space', async () => {
    await renderOpenAlbum()
    const calls = apiGet.mock.calls.length
    emit('gallery.album_updated', { album_id: 'al-9', space_id: 'sp-2' })
    emit('gallery.album_updated', { album_id: 'al-9', space_id: null })
    expect(apiGet.mock.calls.length).toBe(calls)
  })

  it('deleting the open album elsewhere returns to the list with a toast', async () => {
    const view = await renderOpenAlbum()
    albumRows = []
    emit('gallery.album_deleted', { album_id: 'al-1', space_id: 'sp-1' })
    expect(await view.findByText('No albums yet')).toBeTruthy()
    expect(showToast).toHaveBeenCalledWith('This album was deleted.', 'info')
  })

  it('a failed live refresh keeps the list and shows no error toast', async () => {
    const { default: GalleryPage } = await import('./GalleryPage')
    const view = render(<GalleryPage spaceId="sp-1" />)
    await view.findByRole('button', { name: /Open album Holiday/ })
    apiGet.mockRejectedValueOnce(new Error('offline'))
    emit('gallery.album_created', { album_id: 'al-2', space_id: 'sp-1' })
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(view.getByRole('button', { name: /Open album Holiday/ })).toBeTruthy()
    expect(showToast).not.toHaveBeenCalled()
  })
})
