import { describe, it, expect, vi, beforeEach } from 'vitest'

const post = vi.fn()
const download = vi.fn()
vi.mock('@/api', () => ({
  api: { post: (...a: unknown[]) => post(...a), download: (...a: unknown[]) => download(...a) },
  ApiError: class ApiError extends Error {
    constructor(public status: number, public extra: Record<string, unknown> = {}) { super('x') }
  },
}))
const showToast = vi.fn()
vi.mock('@/components/Toast', () => ({ showToast: (...a: unknown[]) => showToast(...a), dismissToast: vi.fn() }))

beforeEach(() => { post.mockReset(); download.mockReset(); showToast.mockReset() })

describe('galleryManage', () => {
  it('names a batch by what is in it', async () => {
    const { itemsWord } = await import('./galleryManage')
    expect(itemsWord([{ item_type: 'photo' }])).toBe('1 photo')
    expect(itemsWord([{ item_type: 'video' }, { item_type: 'video' }])).toBe('2 videos')
    expect(itemsWord([{ item_type: 'photo' }, { item_type: 'video' }])).toBe('2 items')
  })

  it('lists who added the photos you may not delete', async () => {
    const { othersLine } = await import('./galleryManage')
    const name = (u: string) => ({ b: 'Ben', l: 'Lena' } as Record<string, string>)[u] ?? ''
    expect(othersLine([{ uploaded_by: 'b' }, { uploaded_by: 'b' }], name)).toBe('Ben')
    expect(othersLine([{ uploaded_by: 'b' }, { uploaded_by: 'l' }], name)).toBe('Ben and Lena')
  })

  it('sends a big selection in slices of 500', async () => {
    post.mockImplementation(async (_p: string, body: { item_ids: string[] }) => ({ done: body.item_ids, refused: [] }))
    const { bulk } = await import('./galleryManage')
    const ids = Array.from({ length: 1001 }, (_, i) => `i${i}`)
    const res = await bulk('delete', ids)
    expect(post).toHaveBeenCalledTimes(3)
    expect(res.done).toHaveLength(1001)
  })

  it('household delete: Undo restores from the trash', async () => {
    post.mockImplementation(async (_p: string, body: { item_ids: string[] }) => ({ done: body.item_ids, refused: [] }))
    const changed = vi.fn()
    const { trashWithUndo } = await import('./galleryManage')
    await trashWithUndo(['a'], (n) => `${n} gone`, changed)
    const [msg, , opts] = showToast.mock.calls[0]
    expect(msg).toBe('1 gone')
    opts.action.onClick()
    await vi.waitFor(() => expect(post).toHaveBeenLastCalledWith(
      '/api/gallery/bulk', { action: 'restore', item_ids: ['a'] }, {}))
    await vi.waitFor(() => expect(changed).toHaveBeenCalled())
  })

  it('a too-big download says how many fit', async () => {
    const { downloadError } = await import('./galleryManage')
    const { ApiError } = await import('@/api')
    const Err = ApiError as unknown as new (s: number, e: Record<string, unknown>) => Error
    expect(downloadError(new Err(413, { params: { max_items: 500 } })))
      .toBe("That's too many for one download — at most 500 at a time.")
    expect(downloadError(new Error('offline'))).toBe('Couldn\'t save. Check your connection and try again.')
  })

  it('saves the server-named file', async () => {
    download.mockResolvedValue({ blob: new Blob(['x']), filename: 'Sommer 2026 – 2026-07-12.webp' })
    URL.createObjectURL = vi.fn(() => 'blob:1')
    URL.revokeObjectURL = vi.fn()
    const clicked: string[] = []
    const orig = HTMLAnchorElement.prototype.click
    HTMLAnchorElement.prototype.click = function () { clicked.push(this.download) }
    try {
      const { downloadItems } = await import('./galleryManage')
      await downloadItems(['a'], 'Sommer 2026')
    } finally {
      HTMLAnchorElement.prototype.click = orig
    }
    expect(download).toHaveBeenCalledWith('/api/gallery/download',
      expect.objectContaining({ item_ids: ['a'], zip_name: 'Sommer 2026' }))
    expect(clicked).toEqual(['Sommer 2026 – 2026-07-12.webp'])
  })
})
