import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, fireEvent, cleanup, waitFor } from '@testing-library/preact'

const apiGet = vi.fn()
const apiPost = vi.fn()
vi.mock('@/api', () => ({
  api: { get: (...a: unknown[]) => apiGet(...a), post: (...a: unknown[]) => apiPost(...a) },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

class HoldXhr {
  upload = {}
  open() {}
  setRequestHeader() {}
  send() {}
  abort() { (this as unknown as { onabort?: () => void }).onabort?.() }
}

type Q = typeof import('./uploadQueue')
let q: Q
let Sheet: typeof import('./UploadSheet')
const jpg = (n: string) => new File(['x'], n, { type: 'image/jpeg' })
const album = { spaceId: null, albumId: 'al-1', name: 'Holiday', isDefault: false }
const props = { spaceId: null, albums: [], onOpenAlbum: vi.fn(), onAlbumCreated: vi.fn() }

beforeEach(async () => {
  apiGet.mockReset().mockResolvedValue({ album: null })
  apiPost.mockReset()
  vi.stubGlobal('XMLHttpRequest', HoldXhr)
  vi.resetModules()
  q = await import('./uploadQueue')
  Sheet = await import('./UploadSheet')
})
afterEach(() => { cleanup(); q._resetForTests(); vi.unstubAllGlobals() })

describe('UploadSheet', () => {
  it('2 000 picked files render only a screenful of rows', async () => {
    q.addFiles(Array.from({ length: 2000 }, (_, i) => jpg(`IMG_${i}.jpg`)), album)
    const view = render(<Sheet.UploadSheet {...props} />)
    await view.findByRole('heading', { name: '0 of 2000 uploaded' })
    const rows = view.container.querySelectorAll('.sh-upload-row')
    expect(rows.length).toBeGreaterThan(5)
    expect(rows.length).toBeLessThan(40)
    // The list is as tall as all of them, so the scrollbar is honest.
    const ul = view.container.querySelector('.sh-upload-sheet__list > ul') as HTMLElement
    expect(parseInt(ul.style.height, 10)).toBeGreaterThan(2000 * 60)
  })

  it('rows that need a decision come first', async () => {
    const big = jpg('huge.jpg')
    Object.defineProperty(big, 'size', { value: 200 * 1024 * 1024 })
    q.addFiles([jpg('a.jpg'), big, new File(['x'], 'notes.pdf', { type: 'application/pdf' })], album)
    const view = render(<Sheet.UploadSheet {...props} />)
    const heads = [...view.container.querySelectorAll('h3')].map(h => h.textContent)
    expect(heads[0]).toBe('Needs you · 2')
    expect(heads.at(-1)).toBe('Uploading · 1')
    expect(view.getByText(/Not a photo or video Social Home can keep/)).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Remove all' }))
    await waitFor(() => expect(view.queryByText('Needs you · 2')).toBeNull())
  })

  it('a HEIC row shows a placeholder, not a broken preview', async () => {
    q.addFiles([new File(['x'], 'IMG_9.HEIC', { type: 'image/heic' })], album)
    const view = render(<Sheet.UploadSheet {...props} />)
    expect(view.container.querySelector('.sh-upload-ph--heic')?.textContent).toBe('HEIC')
    expect(view.container.querySelector('.sh-upload-row__thumb img')).toBeNull()
  })

  it('pause and resume from the footer, and cancel asks first', async () => {
    q.addFiles([jpg('a.jpg'), jpg('b.jpg'), jpg('c.jpg'), jpg('d.jpg')], album)
    const view = render(<Sheet.UploadSheet {...props} />)
    fireEvent.click(view.getByRole('button', { name: /Pause/ }))
    expect(q.paused.value).toBe(true)
    expect(await view.findByText(/^Paused/)).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: /Resume/ }))
    expect(q.paused.value).toBe(false)
    fireEvent.click(view.getByRole('button', { name: 'Cancel the rest' }))
    expect(view.getByText("Stop 4 uploads? What's already added stays.")).toBeTruthy()
    fireEvent.click(view.getByRole('button', { name: 'Keep going' }))
    expect(q.entries.value).toHaveLength(4)
    fireEvent.click(view.getByRole('button', { name: 'Cancel the rest' }))
    fireEvent.click(view.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(q.entries.value).toHaveLength(0))
    expect(await view.findByRole('heading', { name: 'Nothing uploaded' })).toBeTruthy()
  })

  it('names the household default album when nothing is chosen', async () => {
    apiGet.mockResolvedValue({ album: { id: 'al-def', name: 'Familienfotos' } })
    q.addFiles([jpg('a.jpg')], { spaceId: null, albumId: null, name: 'Photos', isDefault: true })
    q.pause()
    const view = render(<Sheet.UploadSheet {...props} />)
    expect(await view.findByText('No album chosen, so they go into “Familienfotos”.')).toBeTruthy()
    const select = view.getByRole('combobox', { name: 'Album' }) as HTMLSelectElement
    expect(select.selectedOptions[0].textContent).toBe('Familienfotos')
  })

  it('makes a new album from the sheet and sends the files there', async () => {
    apiPost.mockResolvedValue({ id: 'al-new', name: 'Ski 2027', space_id: null })
    // Nothing chosen yet, so nothing starts before the album exists.
    q.addFiles([jpg('a.jpg')], null)
    const onAlbumCreated = vi.fn()
    const view = render(<Sheet.UploadSheet {...props} onAlbumCreated={onAlbumCreated} />)
    fireEvent.change(view.getByRole('combobox', { name: 'Album' }), { target: { value: '__new__' } })
    fireEvent.input(view.getByRole('textbox', { name: 'New album name' }), { target: { value: 'Ski 2027' } })
    fireEvent.click(view.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(onAlbumCreated).toHaveBeenCalled())
    expect(apiPost).toHaveBeenCalledWith('/api/gallery/albums', { name: 'Ski 2027' })
    expect(q.entries.value[0].dest).toMatchObject({ albumId: 'al-new', isDefault: false })
  })

  it('Esc minimises to the pill', async () => {
    q.addFiles([jpg('a.jpg')], album)
    const view = render(<Sheet.UploadSheet {...props} />)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(await view.findByRole('button', { name: /0 of 1 uploaded — Show uploads/ })).toBeTruthy()
  })

  it('screen readers hear milestones and the summary, not every percent', async () => {
    q.addFiles([jpg('a.jpg')], album)
    const view = render(<Sheet.UploadSheet {...props} />)
    const live = view.container.querySelector('[aria-live=polite]')!
    expect(live.textContent).toBe('')
    q.pause()
    await waitFor(() => expect(live.textContent).toBe('Uploads paused'))
    q.cancelAll()
    await waitFor(() => expect(live.textContent).toBe('Upload finished: 0 added · 1 not uploaded'))
  })

  it('summary counts what was added, failed and skipped', () => {
    expect(Sheet.summary()).toBe('0 added')
  })
})
