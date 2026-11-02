import { describe, it, expect, vi, beforeEach } from 'vitest'

const apiPost = vi.fn()
const markSeen = vi.fn()
vi.mock('@/api', () => ({ api: { post: (...a: unknown[]) => apiPost(...a) } }))
vi.mock('@/store/notifications', () => ({ markSeen: (l: string) => markSeen(l) }))

const { noteAlbumSeen, withoutDot } = await import('./albumSeen')

beforeEach(() => { apiPost.mockReset(); markSeen.mockReset() })

describe('the "neu" dot', () => {
  it('opening a household album tells the server and reads its rows', async () => {
    apiPost.mockResolvedValue(undefined)
    await noteAlbumSeen('a 1', null)
    expect(apiPost).toHaveBeenCalledWith('/api/gallery/albums/a%201/seen', {})
    // The path a "new photos" row opens — the same one the server links.
    expect(markSeen).toHaveBeenCalledWith('/gallery/albums/a%201')
  })

  it('a space album reads the rows under its Gallery section', async () => {
    apiPost.mockResolvedValue(undefined)
    await noteAlbumSeen('a1', 's1')
    expect(markSeen).toHaveBeenCalledWith('/spaces/s1/gallery/albums/a1')
  })

  it('a failed note is silent', async () => {
    apiPost.mockRejectedValue(new Error('offline'))
    await expect(noteAlbumSeen('a1', null)).resolves.toBeUndefined()
  })

  it('clears only the opened album', () => {
    const rows = [{ id: 'a', has_new: true }, { id: 'b', has_new: true }]
    expect(withoutDot(rows, 'a')).toEqual([{ id: 'a', has_new: false }, { id: 'b', has_new: true }])
    const calm = [{ id: 'a', has_new: false }]
    expect(withoutDot(calm, 'a')).toBe(calm)
    expect(withoutDot(rows, 'zzz')).toBe(rows)
  })
})
