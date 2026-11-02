import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

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

const people = new Map<string, { user_id: string; display_name: string; username: string }>()
vi.mock('@/store/householdUsers', () => ({
  householdUsers: { get value() { return people } },
  loadHouseholdUsers: vi.fn(async () => undefined),
}))
vi.mock('@/store/spaceMembers', () => ({
  spaceMembers: { value: {} },
  loadSpaceMembers: vi.fn(async () => undefined),
}))

let albumRows: Array<Record<string, unknown>>
let itemRows: Array<Record<string, unknown>>
function album(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    id, name, space_id: 'sp-1', owner_user_id: 'u1',
    cover_url: null, item_count: 0, retention_exempt: false,
    created_at: '2026-10-01T00:00:00+00:00', ...extra,
  }
}
function item(n: number, extra: Record<string, unknown> = {}) {
  return {
    id: `it-${n}`, album_id: 'al-1', uploaded_by: 'u1', item_type: 'photo',
    url: `api/media/p${n}.webp`, thumbnail_url: `api/media/t${n}.webp`,
    width: 10, height: 10, created_at: `2026-10-01T00:00:${String(n % 60).padStart(2, '0')}+00:00`,
    ...extra,
  }
}
/** Path without its query; and the query as an object. */
function split(url: string): [string, Record<string, string>] {
  const [path, q = ''] = url.split('?')
  return [path, Object.fromEntries(new URLSearchParams(q))]
}

beforeEach(() => {
  vi.resetModules()
  handlers.clear()
  showToast.mockReset()
  apiGet.mockReset()
  people.clear()
  albumRows = [album('al-1', 'Holiday')]
  itemRows = []
  apiGet.mockImplementation(async (url: string) => {
    const [path] = split(url)
    if (path === '/api/spaces/sp-1/gallery/albums') return albumRows
    if (path === '/api/gallery/albums') return albumRows
    if (path.startsWith('/api/gallery/albums/')) return itemRows
    return []
  })
})
afterEach(() => { cleanup() })

async function renderOpenAlbum(props: { spaceId?: string } = { spaceId: 'sp-1' }) {
  const { default: GalleryPage } = await import('./GalleryPage')
  const view = render(<GalleryPage {...props} />)
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

describe('GalleryPage for a read-only follower', () => {
  it('offers no New album / Upload — a follower only looks', async () => {
    const { default: GalleryPage } = await import('./GalleryPage')
    const view = render(<GalleryPage spaceId="sp-1" readOnly />)
    await view.findByRole('button', { name: /Open album Holiday/ })
    expect(view.queryByRole('button', { name: /New album/ })).toBeNull()
    fireEvent.click(view.getByRole('button', { name: /Open album Holiday/ }))
    await view.findByRole('heading', { name: /Holiday/ })
    expect(view.queryByRole('button', { name: /Upload/ })).toBeNull()
  })

  it('an empty space gallery invites no album for a follower', async () => {
    albumRows = []
    const { default: GalleryPage } = await import('./GalleryPage')
    const view = render(<GalleryPage spaceId="sp-1" readOnly />)
    await view.findByRole('heading', { name: /No albums yet/ })
    expect(view.queryByRole('button', { name: /album/i })).toBeNull()
  })

  it('a member still gets both', async () => {
    const view = await renderOpenAlbum()
    expect(view.getByRole('button', { name: /Upload/ })).toBeTruthy()
  })
})

describe('GalleryPage paging (UX review #1, #2)', () => {
  it('an album of more than one page loads the rest with the last row cursor', async () => {
    itemRows = Array.from({ length: 50 }, (_, i) => item(100 - i))
    const view = await renderOpenAlbum()
    await waitFor(() => expect(view.getAllByRole('button', { name: /^Photo/ })).toHaveLength(50))
    const older = [item(50), item(49)]
    apiGet.mockImplementationOnce(async () => older)
    fireEvent.click(view.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(view.getAllByRole('button', { name: /^Photo/ })).toHaveLength(52))
    const [path, q] = split(apiGet.mock.calls.at(-1)![0] as string)
    expect(path).toBe('/api/gallery/albums/al-1/items')
    expect(q).toMatchObject({ before_id: 'it-51', before: '2026-10-01T00:00:51+00:00', limit: '50' })
    // A short page is the last one: no more "Load more".
    expect(view.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('a short first page offers no Load more', async () => {
    itemRows = [item(1), item(2)]
    const view = await renderOpenAlbum()
    await waitFor(() => expect(view.getAllByRole('button', { name: /^Photo/ })).toHaveLength(2))
    expect(view.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('more than 30 albums: the next page keys on the last regular album', async () => {
    albumRows = [
      album('sys', 'Posts', { is_system: true, owner_user_id: null, created_at: '2020-01-01T00:00:00+00:00' }),
      ...Array.from({ length: 29 }, (_, i) => album(`a-${i}`, `Trip ${i}`)),
    ]
    const { default: GalleryPage } = await import('./GalleryPage')
    const view = render(<GalleryPage spaceId="sp-1" />)
    await view.findByRole('button', { name: /Open album Trip 28/ })
    // At least 30 — the counts say so while more wait to load.
    expect(view.getByText('30+')).toBeTruthy()
    apiGet.mockImplementationOnce(async () => [album('a-old', 'Sommer 2024')])
    fireEvent.click(view.getByRole('button', { name: 'Load more' }))
    expect(await view.findByRole('button', { name: /Open album Sommer 2024/ })).toBeTruthy()
    const [, q] = split(apiGet.mock.calls.at(-1)![0] as string)
    expect(q.before_id).toBe('a-28')
    expect(view.queryByRole('button', { name: 'Load more' })).toBeNull()
  })
})

describe('GalleryPage across galleries (UX review #8)', () => {
  it('an album opened in a space gallery does not follow to /gallery', async () => {
    const first = await renderOpenAlbum({ spaceId: 'sp-1' })
    first.unmount()
    const { default: GalleryPage } = await import('./GalleryPage')
    albumRows = [album('hh-1', 'Garden', { space_id: null })]
    const view = render(<GalleryPage />)
    expect(await view.findByRole('button', { name: /Open album Garden/ })).toBeTruthy()
    expect(view.queryByRole('heading', { name: /Holiday/ })).toBeNull()
  })
})

describe('GalleryPage when the network is gone', () => {
  it('a failed first load is an error with Retry — never "No albums yet"', async () => {
    apiGet.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const { default: GalleryPage } = await import('./GalleryPage')
    const view = render(<GalleryPage spaceId="sp-1" />)
    expect(await view.findByRole('heading', { name: "Couldn't load the albums" })).toBeTruthy()
    expect(view.queryByText('No albums yet')).toBeNull()
    expect(view.queryByText(/Failed to fetch/)).toBeNull()
    fireEvent.click(view.getByRole('button', { name: 'Retry' }))
    expect(await view.findByRole('button', { name: /Open album Holiday/ })).toBeTruthy()
  })

  it('back to the list while offline keeps the albums already loaded', async () => {
    const view = await renderOpenAlbum()
    apiGet.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    fireEvent.click(view.getByRole('button', { name: /Albums/ }))
    expect(await view.findByRole('button', { name: /Open album Holiday/ })).toBeTruthy()
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "Couldn't refresh the gallery — showing what was loaded before.", 'error',
    ))
    expect(view.queryByText('No albums yet')).toBeNull()
  })
})

describe('GalleryPage system album (UX review #14)', () => {
  it('shows the translated name and no duplicate English description', async () => {
    albumRows = [album('al-1', 'Holiday'), album('sys', 'Server English Posts', {
      is_system: true, owner_user_id: null,
      description: 'Photos and videos shared to the feed appear here automatically.',
    })]
    const { default: GalleryPage } = await import('./GalleryPage')
    const view = render(<GalleryPage spaceId="sp-1" />)
    fireEvent.click(await view.findByRole('button', { name: /Open album Posts/ }))
    await view.findByRole('heading', { name: /Posts/ })
    expect(view.queryByText(/Server English Posts/)).toBeNull()
    expect(view.queryByText('Photos and videos shared to the feed appear here automatically.')).toBeNull()
  })
})

describe('GalleryPage tiles', () => {
  it('names a tile by its day and who added it, not just "Photo"', async () => {
    people.set('u-anna', { user_id: 'u-anna', display_name: 'Anna', username: 'anna' })
    albumRows = [album('al-1', 'Holiday', { space_id: null })]
    itemRows = [item(1, { taken_at: '2026-06-21', uploaded_by: 'u-anna' })]
    const view = await renderOpenAlbum({})
    expect(await view.findByRole('button', { name: 'Photo from Jun 21, 2026, added by Anna' }))
      .toBeTruthy()
  })

  it('a processing video is a placeholder that turns into the poster on media.ready', async () => {
    itemRows = [item(1, { item_type: 'video', url: 'api/media/v1.webm', media_status: 'processing' })]
    const view = await renderOpenAlbum()
    const tile = await view.findByRole('button', { name: /processing/ })
    expect(tile.querySelector('img')).toBeNull()
    itemRows = [item(1, { item_type: 'video', url: 'api/media/v1.webm', media_status: 'ready' })]
    emit('media.ready', { output_filename: 'v1.webm' })
    await waitFor(() => expect(view.getByRole('button', { name: /^Video/ }).querySelector('img')).toBeTruthy())
  })

  it('the album list has a page heading', async () => {
    const { default: GalleryPage } = await import('./GalleryPage')
    const view = render(<GalleryPage spaceId="sp-1" />)
    expect(await view.findByRole('heading', { level: 1, name: 'Gallery' })).toBeTruthy()
  })
})

// ─── Upload (UX review #9 + live walk) ───────────────────────────────────

type Reply = { status: number; body?: string; retryAfter?: string }
let replies: Map<string, Reply[]>
const sent: string[] = []

class FakeXhr {
  status = 0
  responseText = ''
  upload: { onprogress?: (e: unknown) => void; onload?: () => void } = {}
  onload?: () => void
  onerror?: () => void
  withCredentials = false
  private retryAfter: string | null = null
  open() {}
  setRequestHeader() {}
  getResponseHeader(name: string) { return name === 'Retry-After' ? this.retryAfter : null }
  send(fd: FormData) {
    const file = fd.get('file') as File
    sent.push(file.name)
    const queue = replies.get(file.name) ?? []
    const r = queue.shift() ?? { status: 201, body: '{}' }
    setTimeout(() => {
      this.status = r.status
      this.responseText = r.body ?? ''
      this.retryAfter = r.retryAfter ?? null
      this.upload.onload?.()
      this.onload?.()
    }, 0)
  }
}

describe('GalleryPage upload results', () => {
  beforeEach(() => {
    replies = new Map()
    sent.length = 0
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  async function pick(view: Awaited<ReturnType<typeof renderOpenAlbum>>, names: string[]) {
    const input = view.container.querySelector('input[type=file]') as HTMLInputElement
    const files = names.map(n => new File(['x'], n, { type: 'image/jpeg' }))
    Object.defineProperty(input, 'files', { value: files, configurable: true })
    fireEvent.change(input)
  }

  it('says how many made it, and why the rest did not — never raw JSON', async () => {
    replies.set('list.pdf', [{
      status: 422,
      body: JSON.stringify({ error: { code: 'IMAGE_UNREADABLE', detail: "This image couldn't be opened." } }),
    }])
    const view = await renderOpenAlbum()
    await pick(view, ['a.jpg', 'list.pdf', 'b.jpg'])
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    const [msg, kind] = showToast.mock.calls.at(-1)!
    expect(kind).toBe('error')
    expect(msg).toBe(
      "Uploaded 2 of 3 — 1 failed: Couldn't upload list.pdf — that file type isn't supported. Try JPEG, PNG, WebP, or MP4.",
    )
    expect(showToast).toHaveBeenCalledTimes(1)
  })

  it('all fine is the plain success toast', async () => {
    const view = await renderOpenAlbum()
    await pick(view, ['a.jpg', 'b.jpg'])
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('Uploaded 2 items', 'success'))
  })

  it('a 429 waits and sends the same file again instead of failing it', async () => {
    replies.set('a.jpg', [{ status: 429, body: '{}', retryAfter: '0.01' }])
    const view = await renderOpenAlbum()
    await pick(view, ['a.jpg', 'b.jpg'])
    await waitFor(() => expect(showToast).toHaveBeenCalledWith('Uploaded 2 items', 'success'))
    expect(sent).toEqual(['a.jpg', 'a.jpg', 'b.jpg'])
  })

  it('a file over 100 MB is refused in the reader\'s language', async () => {
    const view = await renderOpenAlbum()
    const input = view.container.querySelector('input[type=file]') as HTMLInputElement
    const big = new File(['x'], 'huge.jpg', { type: 'image/jpeg' })
    Object.defineProperty(big, 'size', { value: 105 * 1024 * 1024 })
    Object.defineProperty(input, 'files', { value: [big], configurable: true })
    fireEvent.change(input)
    await waitFor(() => expect(showToast).toHaveBeenCalled())
    const [msg] = showToast.mock.calls.at(-1)!
    expect(msg).toMatch(/too large/)
    expect(msg).not.toContain('>100 MB')
    expect(sent).toEqual([])
  })
})
