import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/UploadProgress', () => ({ uploadWithProgress: vi.fn() }))
vi.mock('@/utils/videoDuration', () => ({
  formatSeconds: (ms: number) => (ms / 1000).toFixed(1),
  readVideoDurationMs: vi.fn(),
}))

import { showToast } from '@/components/Toast'
import { uploadWithProgress } from '@/components/UploadProgress'
import { readVideoDurationMs } from '@/utils/videoDuration'
import { MOMENT_MAX_VIDEO_MS, prepareMomentMedia } from './momentMedia'

const file = (name: string, type: string) => new File(['x'], name, { type })

describe('prepareMomentMedia', () => {
  beforeEach(() => {
    vi.mocked(showToast).mockReset()
    vi.mocked(uploadWithProgress).mockReset()
    vi.mocked(readVideoDurationMs).mockReset()
  })

  it('uploads a picture and hands back the stored and the preview URL', async () => {
    vi.mocked(uploadWithProgress).mockResolvedValue({ url: '/m/p.webp', signed_url: 'api/m/p.webp?s=1', filename: 'p' })
    expect(await prepareMomentMedia(file('p.jpg', 'image/jpeg'))).toEqual({
      url: '/m/p.webp', previewUrl: 'api/m/p.webp?s=1', type: 'image', durationMs: null,
    })
  })

  it('a video carries its length; one longer than 15 s is refused before any upload', async () => {
    expect(MOMENT_MAX_VIDEO_MS).toBe(15_000)
    vi.mocked(readVideoDurationMs).mockResolvedValueOnce(4_321.4)
    vi.mocked(uploadWithProgress).mockResolvedValue({ url: '/m/v.webm', signed_url: 'api/m/v.webm', filename: 'v' })
    expect((await prepareMomentMedia(file('v.mp4', 'video/mp4')))?.durationMs).toBe(4321)
    vi.mocked(uploadWithProgress).mockClear()
    vi.mocked(readVideoDurationMs).mockResolvedValueOnce(16_000)
    expect(await prepareMomentMedia(file('v.mp4', 'video/mp4'))).toBeNull()
    expect(uploadWithProgress).not.toHaveBeenCalled()
    expect(showToast).toHaveBeenCalledWith(expect.stringContaining('16.0'), 'error')
  })

  it('an unreadable video, another file type or a failed upload says why and gives nothing', async () => {
    vi.mocked(readVideoDurationMs).mockResolvedValueOnce(null)
    expect(await prepareMomentMedia(file('v.mp4', 'video/mp4'))).toBeNull()
    expect(await prepareMomentMedia(file('a.pdf', 'application/pdf'))).toBeNull()
    vi.mocked(uploadWithProgress).mockRejectedValueOnce(new Error('boom'))
    expect(await prepareMomentMedia(file('p.png', 'image/png'))).toBeNull()
    expect(showToast).toHaveBeenCalledTimes(3)
    for (const call of vi.mocked(showToast).mock.calls) expect(call[1]).toBe('error')
  })
})
