import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiGet = vi.fn()
const apiPatch = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    patch: (...a: unknown[]) => apiPatch(...a),
  },
}))

beforeEach(() => { vi.resetModules(); apiGet.mockReset(); apiPatch.mockReset() })

describe('gallerySettings', () => {
  it('everyone may add until the server says otherwise', async () => {
    const s = await import('./gallerySettings')
    expect(s.mayAddToGallery()).toBe(true)
    apiGet.mockResolvedValue({ children_may_upload: false, may_add: false, can_edit: false })
    await s.loadGallerySettings()
    expect(s.mayAddToGallery()).toBe(false)
  })

  it('an offline read keeps what was known', async () => {
    const s = await import('./gallerySettings')
    apiGet.mockResolvedValueOnce({ children_may_upload: true, may_add: true, can_edit: true })
    await s.loadGallerySettings()
    apiGet.mockRejectedValueOnce(new Error('offline'))
    expect((await s.loadGallerySettings())?.may_add).toBe(true)
  })

  it('saves the switch and keeps the answer', async () => {
    const s = await import('./gallerySettings')
    apiPatch.mockResolvedValue({ children_may_upload: false, may_add: true, can_edit: true })
    await s.saveChildrenMayUpload(false)
    expect(apiPatch).toHaveBeenCalledWith('/api/gallery/settings', { children_may_upload: false })
    expect(s.gallerySettings.value?.children_may_upload).toBe(false)
  })
})
