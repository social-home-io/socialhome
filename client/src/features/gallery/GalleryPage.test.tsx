import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, waitFor, cleanup } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiPatch = vi.fn()
const apiDelete = vi.fn(async () => undefined)
class FakeApiError extends Error {
  constructor(public status: number) { super(`HTTP ${status}`) }
}
vi.mock('@/api', () => ({
  api: {
    get: (...args: unknown[]) => apiGet(...args),
    post: (...args: unknown[]) => apiPost(...args),
    patch: (...args: unknown[]) => apiPatch(...args),
    delete: (...args: unknown[]) => apiDelete(...(args as [])),
  },
  ApiError: FakeApiError,
}))

const me = {
  value: { user_id: 'u1', is_admin: false, protected: false } as
    { user_id: string; is_admin: boolean; protected?: boolean } | null,
}
vi.mock('@/store/auth', () => ({ currentUser: me }))
const openReport = vi.fn()
vi.mock('@/components/ReportDialog', () => ({ openReport: (...a: unknown[]) => openReport(...a) }))

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
  apiPost.mockReset()
  people.clear()
  albumRows = [album('al-1', 'Holiday')]
  itemRows = []
  apiGet.mockImplementation(async (url: string) => {
    const [path] = split(url)
    if (path === '/api/spaces/sp-1/gallery/albums') return albumRows
    if (path === '/api/gallery/albums') return albumRows
    if (path === '/api/gallery/default-album') return { album: null }
    if (/^\/api\/gallery\/albums\/[^/]+\/items$/.test(path)) return itemRows
    const one = /^\/api\/gallery\/albums\/([^/]+)$/.exec(path)
    if (one) {
      const row = albumRows.find(a => a.id === one[1])
      if (!row) throw new FakeApiError(404)
      return row
    }
    return []
  })
  apiDelete.mockClear()
  openReport.mockClear()
  me.value = { user_id: 'u1', is_admin: false, protected: false }
})
afterEach(() => { cleanup() })

interface PageProps { spaceId?: string; readOnly?: boolean; canModerate?: boolean }

/** The gallery as the app mounts it: behind the router, at its address
 *  (``/gallery…`` or a space's ``/spaces/:id/gallery…``). */
async function mount(props: PageProps = { spaceId: 'sp-1' }, at?: string, state: unknown = null) {
  // jsdom has no <base href>: ``baseUrl`` reads the document base once,
  // at import — from the root, like the app's index.html.
  history.replaceState(null, '', '/')
  const { default: GalleryPage } = await import('./GalleryPage')
  const { IngressLocationProvider } = await import('@/router/IngressLocationProvider')
  const iso = await import('preact-iso')
  history.replaceState(state, '', at ?? (props.spaceId ? `/spaces/${props.spaceId}/gallery` : '/gallery'))
  const Page = (_: { path: string }) => <GalleryPage {...props} />
  return render(
    <IngressLocationProvider>
      <iso.Router>
        <Page path="/gallery/:sub*" />
        <Page path="/spaces/:id/:section?/:sub*" />
      </iso.Router>
    </IngressLocationProvider>,
  )
}

async function renderOpenAlbum(props: PageProps = { spaceId: 'sp-1' }) {
  const view = await mount(props)
  fireEvent.click(await view.findByRole('link', { name: /Open album Holiday/ }))
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
    const view = await mount({ spaceId: 'sp-1' })
    await view.findByRole('link', { name: /Open album Holiday/ })
    apiGet.mockRejectedValueOnce(new Error('offline'))
    emit('gallery.album_created', { album_id: 'al-2', space_id: 'sp-1' })
    await waitFor(() => expect(apiGet).toHaveBeenCalledTimes(2))
    expect(view.getByRole('link', { name: /Open album Holiday/ })).toBeTruthy()
    expect(showToast).not.toHaveBeenCalled()
  })
})

describe('GalleryPage for a read-only follower', () => {
  it('offers no New album / Upload — a follower only looks', async () => {
    const view = await mount({ spaceId: 'sp-1', readOnly: true })
    await view.findByRole('link', { name: /Open album Holiday/ })
    expect(view.queryByRole('button', { name: /New album/ })).toBeNull()
    fireEvent.click(view.getByRole('link', { name: /Open album Holiday/ }))
    await view.findByRole('heading', { name: /Holiday/ })
    expect(view.queryByRole('button', { name: /Upload/ })).toBeNull()
  })

  it('an empty space gallery invites no album for a follower', async () => {
    albumRows = []
    const view = await mount({ spaceId: 'sp-1', readOnly: true })
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
    await waitFor(() => expect(view.getAllByRole('link', { name: /^Photo/ })).toHaveLength(50))
    const older = [item(50), item(49)]
    apiGet.mockImplementationOnce(async () => older)
    fireEvent.click(view.getByRole('button', { name: 'Load more' }))
    await waitFor(() => expect(view.getAllByRole('link', { name: /^Photo/ })).toHaveLength(52))
    const [path, q] = split(apiGet.mock.calls.at(-1)![0] as string)
    expect(path).toBe('/api/gallery/albums/al-1/items')
    expect(q).toMatchObject({ sort: 'taken', after_id: 'it-51', after_day: '2026-10-01', after: '2026-10-01T00:00:51+00:00', limit: '50' })
    // A short page is the last one: no more "Load more".
    expect(view.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('a short first page offers no Load more', async () => {
    itemRows = [item(1), item(2)]
    const view = await renderOpenAlbum()
    await waitFor(() => expect(view.getAllByRole('link', { name: /^Photo/ })).toHaveLength(2))
    expect(view.queryByRole('button', { name: 'Load more' })).toBeNull()
  })

  it('more than 30 albums: the next page keys on the last regular album', async () => {
    albumRows = [
      album('sys', 'Posts', { is_system: true, owner_user_id: null, created_at: '2020-01-01T00:00:00+00:00' }),
      ...Array.from({ length: 29 }, (_, i) => album(`a-${i}`, `Trip ${i}`)),
    ]
    const view = await mount({ spaceId: 'sp-1' })
    await view.findByRole('link', { name: /Open album Trip 28/ })
    // At least 30 — the counts say so while more wait to load.
    expect(view.getByText('30+')).toBeTruthy()
    apiGet.mockImplementationOnce(async () => [album('a-old', 'Sommer 2024')])
    fireEvent.click(view.getByRole('button', { name: 'Load more' }))
    expect(await view.findByRole('link', { name: /Open album Sommer 2024/ })).toBeTruthy()
    const [, q] = split(apiGet.mock.calls.at(-1)![0] as string)
    expect(q.before_id).toBe('a-28')
    expect(view.queryByRole('button', { name: 'Load more' })).toBeNull()
  })
})

describe('GalleryPage across galleries (UX review #8)', () => {
  it('an album opened in a space gallery does not follow to /gallery', async () => {
    const first = await renderOpenAlbum({ spaceId: 'sp-1' })
    first.unmount()
    albumRows = [album('hh-1', 'Garden', { space_id: null })]
    const view = await mount({})
    expect(await view.findByRole('link', { name: /Open album Garden/ })).toBeTruthy()
    expect(view.queryByRole('heading', { name: /Holiday/ })).toBeNull()
  })
})

describe('GalleryPage when the network is gone', () => {
  it('a failed first load is an error with Retry — never "No albums yet"', async () => {
    apiGet.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const view = await mount({ spaceId: 'sp-1' })
    // No answer from home at all: say so, and that nothing is lost.
    expect(await view.findByRole('heading', { name: 'No connection to your home' })).toBeTruthy()
    expect(view.getByText(/Your photos are safe at home/)).toBeTruthy()
    expect(view.queryByText('No albums yet')).toBeNull()
    expect(view.queryByText(/Failed to fetch/)).toBeNull()
    fireEvent.click(view.getByRole('button', { name: 'Retry' }))
    expect(await view.findByRole('link', { name: /Open album Holiday/ })).toBeTruthy()
  })

  it('a server error on the first load is not called a lost connection', async () => {
    apiGet.mockRejectedValueOnce(new FakeApiError(500))
    const view = await mount({ spaceId: 'sp-1' })
    expect(await view.findByRole('heading', { name: "Couldn't load the albums" })).toBeTruthy()
    expect(view.queryByText('No connection to your home')).toBeNull()
  })

  it('back to the list while offline keeps the albums already loaded', async () => {
    const view = await renderOpenAlbum()
    apiGet.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    fireEvent.click(view.getByRole('button', { name: /Albums/ }))
    expect(await view.findByRole('link', { name: /Open album Holiday/ })).toBeTruthy()
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "Couldn't refresh the gallery — showing what was loaded before.", 'error',
    ))
    expect(view.queryByText('No albums yet')).toBeNull()
    // The banner says the list may be out of date, and goes once a
    // refresh works.
    expect(await view.findByText(/Uploads wait until you're back online/)).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(view.queryByText(/Uploads wait until you're back online/)).toBeNull())
  })

  it('going offline shows the banner and keeps the list; back online re-reads it', async () => {
    const view = await mount({ spaceId: 'sp-1' })
    await view.findByRole('link', { name: /Open album Holiday/ })
    window.dispatchEvent(new Event('offline'))
    expect(await view.findByText(/Uploads wait until you're back online/)).toBeTruthy()
    expect(view.getByRole('link', { name: /Open album Holiday/ })).toBeTruthy()
    const calls = apiGet.mock.calls.length
    window.dispatchEvent(new Event('online'))
    await waitFor(() => expect(view.queryByText(/Uploads wait until you're back online/)).toBeNull())
    expect(apiGet.mock.calls.length).toBeGreaterThan(calls)
  })
})

describe('GalleryPage system album (UX review #14)', () => {
  it('shows the translated name and no duplicate English description', async () => {
    albumRows = [album('al-1', 'Holiday'), album('sys', 'Server English Posts', {
      is_system: true, owner_user_id: null,
      description: 'Photos and videos shared to the feed appear here automatically.',
    })]
    const view = await mount({ spaceId: 'sp-1' })
    fireEvent.click(await view.findByRole('link', { name: /Open album Posts/ }))
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
    expect(await view.findByRole('link', { name: 'Photo from Jun 21, 2026, added by Anna' }))
      .toBeTruthy()
  })

  it('a processing video is a placeholder that turns into the poster on media.ready', async () => {
    itemRows = [item(1, { item_type: 'video', url: 'api/media/v1.webm', media_status: 'processing' })]
    const view = await renderOpenAlbum()
    const tile = await view.findByRole('img', { name: /processing/ })
    expect(tile.querySelector('img')).toBeNull()
    itemRows = [item(1, { item_type: 'video', url: 'api/media/v1.webm', media_status: 'ready' })]
    emit('media.ready', { output_filename: 'v1.webm' })
    await waitFor(() => expect(view.getByRole('link', { name: /^Video/ }).querySelector('img')).toBeTruthy())
  })

  it('a processing video says roughly when it will be ready', async () => {
    itemRows = [item(1, {
      item_type: 'video', url: 'api/media/v1.webm', media_status: 'processing', media_eta_s: 230,
    })]
    const view = await renderOpenAlbum()
    const tile = await view.findByRole('img', { name: 'Video — processing · Ready in about 4 min' })
    expect(tile.textContent).toContain('Ready in about 4 min')
    const { readyInLabel } = await import('./GalleryPage')
    expect(readyInLabel(30)).toBe('Ready in under a minute')
    expect(readyInLabel(undefined)).toBeNull()
    expect(readyInLabel(0)).toBeNull()
  })

  it('the album list has a page heading', async () => {
    const view = await mount({ spaceId: 'sp-1' })
    expect(await view.findByRole('heading', { level: 1, name: 'Gallery' })).toBeTruthy()
  })
})

// ─── Upload sheet (gallery redesign PR 2) ────────────────────────────────

type Reply = { status: number; body?: string; retryAfter?: string }
let replies: Map<string, Reply[]>
const sent: Array<{ name: string; url: string; type: string }> = []

class FakeXhr {
  status = 0
  responseText = ''
  upload: { onprogress?: (e: unknown) => void; onload?: () => void } = {}
  onload?: () => void
  onerror?: () => void
  onabort?: () => void
  withCredentials = false
  private url = ''
  private retryAfter: string | null = null
  open(_m: string, url: string) { this.url = url }
  setRequestHeader() {}
  abort() { this.onabort?.() }
  getResponseHeader(name: string) { return name === 'Retry-After' ? this.retryAfter : null }
  send(fd: FormData) {
    const file = fd.get('file') as File
    sent.push({ name: file.name, url: this.url, type: file.type })
    const queue = replies.get(file.name) ?? []
    const r = queue.shift() ?? {
      status: 201,
      body: JSON.stringify({ id: `it-${file.name}`, item_type: 'photo', thumbnail_url: `api/media/t-${file.name}` }),
    }
    setTimeout(() => {
      this.status = r.status
      this.responseText = r.body ?? ''
      this.retryAfter = r.retryAfter ?? null
      this.upload.onload?.()
      this.onload?.()
    }, 0)
  }
}

describe('GalleryPage upload sheet', () => {
  beforeEach(() => {
    replies = new Map()
    sent.length = 0
    vi.stubGlobal('XMLHttpRequest', FakeXhr)
  })
  afterEach(() => { vi.unstubAllGlobals() })

  function pick(view: { container: Element }, files: File[]) {
    const input = view.container.querySelector('[data-testid=gallery-upload-input]') as HTMLInputElement
    Object.defineProperty(input, 'files', { value: files, configurable: true })
    fireEvent.change(input)
  }
  const jpg = (n: string) => new File(['x'], n, { type: 'image/jpeg' })

  it('every file goes up into the open album and the sheet says so', async () => {
    const view = await renderOpenAlbum()
    pick(view, [jpg('a.jpg'), jpg('b.jpg'), jpg('c.jpg')])
    expect(await view.findByRole('dialog', { name: /uploaded|added/ })).toBeTruthy()
    await view.findByRole('heading', { name: '3 added' })
    expect(sent.map(s => s.url)).toEqual([
      'api/gallery/albums/al-1/items', 'api/gallery/albums/al-1/items', 'api/gallery/albums/al-1/items',
    ])
    expect(view.getByText('Done · 3')).toBeTruthy()
    expect(showToast).not.toHaveBeenCalled()
  })

  it('a refused file needs you, with a translated reason — never raw JSON', async () => {
    replies.set('list.jpg', [{
      status: 422,
      body: JSON.stringify({ error: { code: 'IMAGE_UNREADABLE', detail: "This image couldn't be opened." } }),
    }])
    const view = await renderOpenAlbum()
    pick(view, [jpg('a.jpg'), jpg('list.jpg')])
    await view.findByText("This file couldn't be opened as a photo or video.")
    expect(view.getByText('Needs you · 1')).toBeTruthy()
    expect(view.getByRole('heading', { name: '1 of 2 uploaded' })).toBeTruthy()
    expect(view.container.textContent).not.toContain('IMAGE_UNREADABLE')
    // Try again sends it once more.
    fireEvent.click(view.getByRole('button', { name: 'Try list.jpg again' }))
    await view.findByRole('heading', { name: '2 added' })
    expect(sent.filter(s => s.name === 'list.jpg')).toHaveLength(2)
  })

  it('a file the household already has is counted, not called new', async () => {
    replies.set('b.jpg', [{ status: 200, body: JSON.stringify({ id: 'it-old', item_type: 'photo', duplicate: 'already_here' }) }])
    replies.set('c.jpg', [{ status: 200, body: JSON.stringify({ id: 'it-tr', item_type: 'photo', duplicate: 'restored' }) }])
    const view = await renderOpenAlbum()
    pick(view, [jpg('a.jpg'), jpg('b.jpg'), jpg('c.jpg')])
    await view.findByRole('heading', { name: '1 added' })
    expect(view.getByText('Done · 3 · 1 already here · 1 back from the trash')).toBeTruthy()
  })

  it('a 429 waits and sends the same file again instead of failing it', async () => {
    replies.set('a.jpg', [{ status: 429, body: '{}', retryAfter: '0.01' }])
    const view = await renderOpenAlbum()
    pick(view, [jpg('a.jpg')])
    await view.findByRole('heading', { name: '1 added' })
    expect(sent.map(s => s.name)).toEqual(['a.jpg', 'a.jpg'])
  })

  it('a file over 100 MB is never sent and waits for a decision', async () => {
    const view = await renderOpenAlbum()
    const big = jpg('huge.jpg')
    Object.defineProperty(big, 'size', { value: 105 * 1024 * 1024 })
    pick(view, [big, jpg('ok.jpg')])
    await view.findByText('Too large — a file can be up to 100 MB.')
    await waitFor(() => expect(sent.map(s => s.name)).toEqual(['ok.jpg']))
    fireEvent.click(view.getByRole('button', { name: 'Remove huge.jpg' }))
    await waitFor(() => expect(view.queryByText('huge.jpg')).toBeNull())
  })

  it('a HEIC from a folder without a type is sent as image/heic', async () => {
    const view = await renderOpenAlbum()
    pick(view, [new File(['x'], 'IMG_1.HEIC', { type: '' })])
    await view.findByRole('heading', { name: '1 added' })
    expect(sent[0].type).toBe('image/heic')
  })

  it('upload from the household album list needs no album: the default one is used', async () => {
    albumRows = [album('al-1', 'Holiday', { space_id: null })]
    apiPost.mockImplementation(async (url: string) => {
      if (url === '/api/gallery/default-album') return { id: 'al-def', name: 'Photos', space_id: null }
      return {}
    })
    const view = await mount({})
    await view.findByRole('link', { name: /Open album Holiday/ })
    pick(view, [jpg('a.jpg'), jpg('b.jpg')])
    await view.findByRole('heading', { name: '2 added' })
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/gallery/default-album', { name: 'Photos' })
    expect(sent.map(s => s.url)).toEqual([
      'api/gallery/albums/al-def/items', 'api/gallery/albums/al-def/items',
    ])
    expect(view.getByText(/No album chosen, so they go into “Photos”/)).toBeTruthy()
  })

  it('a space gallery with no album offers no upload', async () => {
    albumRows = []
    const view = await mount({ spaceId: 'sp-1' })
    await view.findByRole('heading', { name: /No albums yet/ })
    expect(view.queryByRole('button', { name: /Upload/ })).toBeNull()
    expect(view.container.querySelector('[data-testid=gallery-upload-input]')).toBeNull()
  })

  it('a space gallery with two albums waits until one is chosen', async () => {
    albumRows = [album('al-1', 'Holiday'), album('al-2', 'Garden')]
    const view = await mount({ spaceId: 'sp-1' })
    await view.findByRole('link', { name: /Open album Holiday/ })
    pick(view, [jpg('a.jpg')])
    await view.findByText('Choose an album to start uploading.')
    await new Promise(r => setTimeout(r, 20))
    expect(sent).toEqual([])
    fireEvent.change(view.getByRole('combobox', { name: 'Album' }), { target: { value: 'al-2' } })
    await view.findByRole('heading', { name: '1 added' })
    expect(sent[0].url).toBe('api/gallery/albums/al-2/items')
  })

  it('the sheet minimises to a pill and comes back', async () => {
    const view = await renderOpenAlbum()
    pick(view, [jpg('a.jpg')])
    await view.findByRole('heading', { name: '1 added' })
    fireEvent.click(view.getByRole('button', { name: 'Minimise' }))
    const pill = await view.findByRole('button', { name: /1 added — Show uploads/ })
    fireEvent.click(pill)
    expect(await view.findByRole('dialog')).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Close' }))
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
  })
})

// ─── Addresses + viewer (gallery redesign PR 1) ──────────────────────────

describe('GalleryPage addresses', () => {
  it('opening an album gives it its own address; Back returns to the list', async () => {
    const view = await renderOpenAlbum()
    expect(location.pathname).toBe('/spaces/sp-1/gallery/albums/al-1')
    fireEvent.click(view.getByRole('button', { name: /Albums/ }))
    // Opened from the list: Back is a step back in history.
    await waitFor(() => expect(location.pathname).toBe('/spaces/sp-1/gallery'))
    expect(await view.findByRole('link', { name: /Open album Holiday/ })).toBeTruthy()
  })

  it('an album link opens the album on a cold load, even one past the first page of albums', async () => {
    albumRows = [album('al-1', 'Holiday', { space_id: null })]
    apiGet.mockImplementation(async (url: string) => {
      const [path] = split(url)
      if (path === '/api/gallery/albums') return []          // not on the first page
      if (path === '/api/gallery/albums/al-1') return albumRows[0]
      if (path === '/api/gallery/albums/al-1/items') return [item(1)]
      return []
    })
    const view = await mount({}, '/gallery/albums/al-1')
    expect(await view.findByRole('heading', { name: /Holiday/ })).toBeTruthy()
    expect(await view.findByRole('link', { name: /^Photo/ })).toBeTruthy()
    // No list in history: Back goes to the list's address.
    fireEvent.click(view.getByRole('button', { name: /Albums/ }))
    await waitFor(() => expect(location.pathname).toBe('/gallery'))
  })

  it('a link to an album that is gone says so and shows the albums', async () => {
    const view = await mount({ spaceId: 'sp-1' }, '/spaces/sp-1/gallery/albums/nope')
    await waitFor(() => expect(location.pathname).toBe('/spaces/sp-1/gallery'))
    expect(showToast).toHaveBeenCalledWith("This album isn't here any more.", 'info')
    expect(await view.findByRole('link', { name: /Open album Holiday/ })).toBeTruthy()
  })

  it('a household link never opens a space album', async () => {
    albumRows = [album('al-1', 'Holiday', { space_id: 'sp-1' })]
    await mount({}, '/gallery/albums/al-1')
    await waitFor(() => expect(location.pathname).toBe('/gallery'))
  })

  it('an address the gallery does not know goes to the albums', async () => {
    await mount({ spaceId: 'sp-1' }, '/spaces/sp-1/gallery/photos/x')
    await waitFor(() => expect(location.pathname).toBe('/spaces/sp-1/gallery'))
  })

  it('a tile opens the viewer at the photo\'s address; Close steps back to the album', async () => {
    itemRows = [item(3), item(2), item(1)]
    const view = await renderOpenAlbum()
    const tiles = await view.findAllByRole('link', { name: /^Photo/ })
    expect(tiles[1].getAttribute('href')).toBe('/spaces/sp-1/gallery/albums/al-1/it-2')
    fireEvent.click(tiles[1])
    await waitFor(() => expect(location.pathname).toBe('/spaces/sp-1/gallery/albums/al-1/it-2'))
    const dialog = await view.findByRole('dialog', { name: 'Photo 2 of 3' })
    // Next replaces the address: Back never walks through every photo.
    const before = history.length
    fireEvent.keyDown(dialog, { key: 'ArrowRight' })
    await waitFor(() => expect(location.pathname).toBe('/spaces/sp-1/gallery/albums/al-1/it-1'))
    expect(history.length).toBe(before)
    fireEvent.click(view.getByRole('button', { name: /Close viewer/ }))
    await waitFor(() => expect(location.pathname).toBe('/spaces/sp-1/gallery/albums/al-1'))
    await waitFor(() => expect(view.queryByRole('dialog')).toBeNull())
    // Focus goes back to the tile of the photo seen last.
    await waitFor(() => expect(document.activeElement?.getAttribute('data-item-id')).toBe('it-1'))
  })

  it('a photo link opens the viewer on a cold load; Esc goes to its album', async () => {
    albumRows = [album('al-1', 'Holiday', { space_id: null, item_count: 2 })]
    itemRows = [item(2), item(1)]
    const view = await mount({}, '/gallery/albums/al-1/it-1')
    const dialog = await view.findByRole('dialog', { name: 'Photo 2 of 2' })
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(location.pathname).toBe('/gallery/albums/al-1'))
  })

  it('a photo opened from the grid, then reloaded, still closes with a step back (no second album entry)', async () => {
    albumRows = [album('al-1', 'Holiday', { space_id: null, item_count: 2 })]
    itemRows = [item(2), item(1)]
    const back = vi.spyOn(history, 'back').mockImplementation(() => undefined)
    try {
      // What the grid noted on the entry before the reload.
      const view = await mount({}, '/gallery/albums/al-1/it-2', { shGalleryBack: '/gallery/albums/al-1' })
      const dialog = await view.findByRole('dialog', { name: 'Photo 1 of 2' })
      // The next photo replaces the entry — the note is written again.
      fireEvent.keyDown(dialog, { key: 'ArrowRight' })
      await waitFor(() => expect(location.pathname).toBe('/gallery/albums/al-1/it-1'))
      await waitFor(() => expect((history.state as Record<string, string>)?.shGalleryBack).toBe('/gallery/albums/al-1'))
      fireEvent.keyDown(view.getByRole('dialog'), { key: 'Escape' })
      expect(back).toHaveBeenCalled()
    } finally {
      back.mockRestore()
    }
  })

  it('a link to a photo past the loaded pages reads on until it finds it', async () => {
    albumRows = [album('al-1', 'Holiday', { space_id: null, item_count: 51 })]
    const first = Array.from({ length: 50 }, (_, i) => item(100 - i))
    apiGet.mockImplementation(async (url: string) => {
      const [path, q] = split(url)
      if (path === '/api/gallery/albums') return albumRows
      if (path === '/api/gallery/albums/al-1') return albumRows[0]
      if (path === '/api/gallery/albums/al-1/items') return q.after_id ? [item(7)] : first
      return []
    })
    const view = await mount({}, '/gallery/albums/al-1/it-7')
    expect(await view.findByRole('dialog', { name: 'Photo 51 of 51' })).toBeTruthy()
  })

  it('a link to a photo that is gone says so and shows its album', async () => {
    albumRows = [album('al-1', 'Holiday', { space_id: null })]
    itemRows = [item(1)]
    await mount({}, '/gallery/albums/al-1/it-404')
    await waitFor(() => expect(location.pathname).toBe('/gallery/albums/al-1'))
    expect(showToast).toHaveBeenCalledWith("This photo or video isn't here any more.", 'info')
  })
})

describe('GalleryPage viewer actions follow the delete rules (#1271)', () => {
  async function openOn(n: number, props: PageProps, row: Record<string, unknown> = {}) {
    albumRows = [album('al-1', 'Holiday', { space_id: props.spaceId ?? null, ...row })]
    itemRows = [item(1, { uploaded_by: 'u-anna' }), item(2, { uploaded_by: 'u1' })]
    const base = props.spaceId ? `/spaces/${props.spaceId}/gallery` : '/gallery'
    const view = await mount(props, `${base}/albums/al-1/it-${n}`)
    await view.findByRole('dialog', { name: /^Photo/ })
    return view
  }

  it('household: your own photo yes, someone else\'s no — unless you own the album or are an admin', async () => {
    let view = await openOn(2, {}, { owner_user_id: 'u-other' })
    expect(view.getByRole('button', { name: 'Delete' })).toBeTruthy()
    cleanup()
    view = await openOn(1, {}, { owner_user_id: 'u-other' })
    expect(view.queryByRole('button', { name: 'Delete' })).toBeNull()
    cleanup()
    view = await openOn(1, {}, { owner_user_id: 'u1' })
    expect(view.getByRole('button', { name: 'Delete' })).toBeTruthy()
    cleanup()
    me.value = { user_id: 'u1', is_admin: true }
    view = await openOn(1, {}, { owner_user_id: 'u-other' })
    expect(view.getByRole('button', { name: 'Delete' })).toBeTruthy()
  })

  it('a protected account deletes only its own, even in its own album', async () => {
    me.value = { user_id: 'u1', is_admin: false, protected: true }
    const view = await openOn(1, {}, { owner_user_id: 'u1' })
    expect(view.queryByRole('button', { name: 'Delete' })).toBeNull()
  })

  it('the Posts album offers no Delete (its photos follow their posts)', async () => {
    const view = await openOn(2, {}, { is_system: true, owner_user_id: null })
    expect(view.queryByRole('button', { name: 'Delete' })).toBeNull()
  })

  it('space: a moderator may delete others\' items; a member reports them', async () => {
    let view = await openOn(1, { spaceId: 'sp-1' })
    expect(view.queryByRole('button', { name: 'Delete' })).toBeNull()
    fireEvent.click(view.getByRole('button', { name: 'Report' }))
    expect(openReport).toHaveBeenCalledWith('gallery_item', 'it-1', 'sp-1')
    cleanup()
    view = await openOn(1, { spaceId: 'sp-1', canModerate: true })
    expect(view.getByRole('button', { name: 'Delete' })).toBeTruthy()
  })

  it('deleting moves on to the next photo and drops the tile', async () => {
    const view = await openOn(2, {}, { owner_user_id: 'u1', item_count: 2 })
    fireEvent.click(view.getByRole('button', { name: 'Delete' }))
    expect(await view.findByRole('alertdialog', { name: 'Delete this photo?' })).toBeTruthy()
    fireEvent.click(view.getAllByRole('button', { name: 'Delete' }).at(-1)!)
    await waitFor(() => expect(apiDelete).toHaveBeenCalledWith('/api/gallery/items/it-2'))
    await waitFor(() => expect(location.pathname).toBe('/gallery/albums/al-1/it-1'))
    expect(await view.findByRole('dialog', { name: 'Photo 1 of 1' })).toBeTruthy()
  })
})

describe('GalleryPage albums refresh (PR 4)', () => {
  it('an album shows its photos under month and day headings, by date taken', async () => {
    people.set('u1', { user_id: 'u1', display_name: 'Anna', username: 'anna' })
    people.set('u2', { user_id: 'u2', display_name: 'Lena', username: 'lena' })
    albumRows = [album('al-1', 'Holiday', { space_id: null, item_count: 3 })]
    itemRows = [
      item(3, { day: '2026-08-02', taken_at: '2026-08-02', uploaded_by: 'u2' }),
      item(2, { day: '2026-07-13', taken_at: '2026-07-13' }),
      item(1, { day: '2026-07-13', taken_at: '2026-07-13', uploaded_by: 'u2' }),
    ]
    const view = await renderOpenAlbum({})
    await waitFor(() => expect(view.getAllByRole('link', { name: /^Photo/ })).toHaveLength(3))
    const months = view.getAllByRole('heading', { level: 2 }).map(h => h.textContent)
    expect(months).toEqual(['August 2026', 'July 2026'])
    const days = view.getAllByRole('heading', { level: 3 }).map(h => h.textContent)
    expect(days[0]).toMatch(/Aug.*2 · by Lena$/)
    expect(days[1]).toMatch(/Jul.*13 · by Anna, Lena$/)
    const [, q] = split(apiGet.mock.calls.find(c => String(c[0]).includes('/items'))![0] as string)
    expect(q.sort).toBe('taken')
  })

  it('the order toggle flips to oldest first and is remembered for that album', async () => {
    albumRows = [album('al-1', 'Holiday', { item_count: 2 })]
    itemRows = [item(2, { day: '2026-08-02' }), item(1, { day: '2026-07-13' })]
    let view = await renderOpenAlbum()
    const toggle = await view.findByRole('button', { name: 'Newest first — show oldest first' })
    fireEvent.click(toggle)
    await waitFor(() => {
      const [, q] = split(apiGet.mock.calls.at(-1)![0] as string)
      expect(q.sort).toBe('taken_asc')
    })
    expect(await view.findByRole('button', { name: 'Oldest first — show newest first' })).toBeTruthy()
    expect(localStorage.getItem('sh.gallery.order.u1.al-1')).toBe('oldest')
    // Opened again: still oldest first.
    cleanup()
    apiGet.mockClear()
    view = await renderOpenAlbum()
    await view.findByRole('button', { name: 'Oldest first — show newest first' })
    const [, q] = split(apiGet.mock.calls.find(c => String(c[0]).includes('/items'))![0] as string)
    expect(q.sort).toBe('taken_asc')
    // Back to newest first forgets the choice.
    fireEvent.click(view.getByRole('button', { name: 'Oldest first — show newest first' }))
    await waitFor(() => expect(localStorage.getItem('sh.gallery.order.u1.al-1')).toBeNull())
  })

  it('the default album is shown in the reader\'s language until renamed', async () => {
    albumRows = [
      album('def', 'Fotos', { space_id: null, is_default: true, default_name: true, item_count: 1 }),
      album('ren', 'Familie', { space_id: null, is_default: false, default_name: false, item_count: 1 }),
    ]
    const view = await mount({})
    expect(await view.findByRole('link', { name: /Open album Photos/ })).toBeTruthy()
    expect(view.getByRole('link', { name: /Open album Familie/ })).toBeTruthy()
    expect(view.queryByText('Fotos')).toBeNull()
  })

  it('albums page by newest activity: the next page keys on activity_at', async () => {
    albumRows = Array.from({ length: 30 }, (_, i) => album(`a-${i}`, `Trip ${i}`, {
      space_id: null, item_count: 1, activity_at: `2026-10-${String(30 - i).padStart(2, '0')}T08:00:00+00:00`,
    }))
    const view = await mount({})
    await view.findByRole('link', { name: /Open album Trip 29/ })
    apiGet.mockImplementationOnce(async () => [])
    fireEvent.click(view.getByRole('button', { name: 'Load more' }))
    await waitFor(() => {
      const [path, q] = split(apiGet.mock.calls.at(-1)![0] as string)
      expect(path).toBe('/api/gallery/albums')
      expect(q).toMatchObject({ before: '2026-10-01T08:00:00+00:00', before_id: 'a-29' })
    })
  })

  it('first run: a household without photos explains and offers one big Add photos', async () => {
    albumRows = [album('sys', 'Posts', { space_id: null, is_system: true, owner_user_id: null })]
    const view = await mount({})
    expect(await view.findByRole('heading', { name: 'Your photos, all in one place' })).toBeTruthy()
    expect(view.getByText(/Everyone at home sees what lands here/)).toBeTruthy()
    expect(view.getByText(/They go into “Photos”/)).toBeTruthy()
    const input = view.getByTestId('gallery-upload-input') as HTMLInputElement
    const click = vi.spyOn(input, 'click')
    fireEvent.click(view.getByRole('button', { name: 'Add photos' }))
    expect(click).toHaveBeenCalled()
    // Not the regular header or "No albums yet".
    expect(view.queryByText('No albums yet')).toBeNull()
    fireEvent.click(view.getByRole('button', { name: 'Make an album instead' }))
    expect(view.getByPlaceholderText('e.g. Summer 2026')).toBeTruthy()
  })

  it('a space with no albums keeps its own empty state, never the household first run', async () => {
    albumRows = []
    const view = await mount({ spaceId: 'sp-1' })
    expect(await view.findByText('No albums yet')).toBeTruthy()
    expect(view.queryByText('Your photos, all in one place')).toBeNull()
  })
})

describe('GalleryPage On this day (D14)', () => {
  function withCard(settings: Record<string, unknown> = {}) {
    albumRows = [album('al-1', 'Autumn break', { space_id: null, item_count: 6 })]
    const base = apiGet.getMockImplementation()!
    apiGet.mockImplementation(async (url: string) => {
      const [path] = split(url)
      if (path === '/api/gallery/on-this-day') {
        return {
          card: {
            album: { id: 'al-1', name: 'Autumn break' },
            date: '2024-11-01', years_ago: 2, count: 6,
            items: [item(1), item(2), item(3)],
          },
          settings: { enabled: true, hidden_today: false, excluded_album_ids: [], ...settings },
        }
      }
      return base(url)
    })
  }

  it('one card at the top of the household albums opens the photo in the viewer', async () => {
    withCard()
    const view = await mount({})
    const card = await view.findByRole('region', { name: 'On this day' })
    expect(card.textContent).toContain('2 years ago today')
    expect(card.textContent).toContain('6 photos · Autumn break')
    const [path, q] = split(apiGet.mock.calls.find(c => String(c[0]).startsWith('/api/gallery/on-this-day'))![0] as string)
    expect(path).toBe('/api/gallery/on-this-day')
    expect(q.date).toMatch(/^\d{4}-\d{2}-\d{2}$/)
    const link = view.getAllByRole('link').find(a => a.getAttribute('href') === '/gallery/albums/al-1/it-1')
    expect(link).toBeTruthy()
  })

  it('never in a space gallery', async () => {
    withCard()
    const view = await mount({ spaceId: 'sp-1' })
    await view.findByRole('link', { name: /Open album/ })
    expect(view.queryByRole('region', { name: 'On this day' })).toBeNull()
    expect(apiGet.mock.calls.some(c => String(c[0]).startsWith('/api/gallery/on-this-day'))).toBe(false)
  })

  it('✕ hides it for today, with Undo', async () => {
    withCard()
    apiPatch.mockResolvedValue({ settings: { enabled: true, hidden_today: true, excluded_album_ids: [] } })
    const view = await mount({})
    await view.findByRole('region', { name: 'On this day' })
    fireEvent.click(view.getByRole('button', { name: 'Hide for today' }))
    await waitFor(() => expect(view.queryByRole('region', { name: 'On this day' })).toBeNull())
    expect(apiPatch).toHaveBeenCalledWith('/api/gallery/on-this-day', expect.objectContaining({ hide_today: true }))
    expect(showToast).toHaveBeenCalledWith('Hidden until tomorrow.', 'info', expect.objectContaining({
      action: expect.objectContaining({ label: 'Undo' }),
    }))
  })

  it('don\'t show this album again, and turn it off, from the ⋯ menu', async () => {
    withCard()
    apiPatch.mockResolvedValue({ settings: { enabled: true, hidden_today: false, excluded_album_ids: ['al-1'] } })
    const view = await mount({})
    await view.findByRole('region', { name: 'On this day' })
    fireEvent.click(view.getByRole('button', { name: 'More options for On this day' }))
    fireEvent.click(await view.findByRole('menuitem', { name: 'Don\'t show “Autumn break” again' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(
      '/api/gallery/on-this-day', expect.objectContaining({ exclude_album_id: 'al-1' }),
    ))
    await waitFor(() => expect(view.queryByRole('region', { name: 'On this day' })).toBeNull())
    // The foot of the list says how to bring the album back.
    expect(await view.findByText('On this day leaves out 1 album.')).toBeTruthy()
    apiPatch.mockResolvedValue({ settings: { enabled: true, hidden_today: false, excluded_album_ids: [] } })
    fireEvent.click(view.getByRole('button', { name: 'Show them again' }))
    await waitFor(() => expect(apiPatch).toHaveBeenLastCalledWith(
      '/api/gallery/on-this-day', expect.objectContaining({ include_all: true }),
    ))
  })

  it('turned off: no card, and the foot of the list turns it back on', async () => {
    albumRows = [album('al-1', 'Holiday', { space_id: null, item_count: 1 })]
    const base = apiGet.getMockImplementation()!
    apiGet.mockImplementation(async (url: string) => (split(url)[0] === '/api/gallery/on-this-day'
      ? { card: null, settings: { enabled: false, hidden_today: false, excluded_album_ids: [] } }
      : base(url)))
    apiPatch.mockResolvedValue({ settings: { enabled: true, hidden_today: false, excluded_album_ids: [] } })
    const view = await mount({})
    expect(await view.findByText('On this day is off.')).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Turn it on' }))
    await waitFor(() => expect(apiPatch).toHaveBeenCalledWith(
      '/api/gallery/on-this-day', expect.objectContaining({ enabled: true }),
    ))
  })

  it('a failed save puts the card back and says so', async () => {
    withCard()
    apiPatch.mockRejectedValue(new TypeError('Failed to fetch'))
    const view = await mount({})
    await view.findByRole('region', { name: 'On this day' })
    fireEvent.click(view.getByRole('button', { name: 'Hide for today' }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(
      "Couldn't save that. Check your connection and try again.", 'error',
    ))
    expect(await view.findByRole('region', { name: 'On this day' })).toBeTruthy()
  })
})
