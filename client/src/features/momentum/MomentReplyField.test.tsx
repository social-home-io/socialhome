import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { createRef } from 'preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))
vi.mock('@/components/UploadProgress', () => ({
  uploadWithProgress: vi.fn(),
  UploadProgressBar: () => null,
}))
vi.mock('@/utils/videoDuration', () => ({
  formatSeconds: (ms: number) => (ms / 1000).toFixed(1),
  readVideoDurationMs: vi.fn(async () => 3_000),
}))

import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { registrations } from '@/store/momentPublic'
import type { Moment } from '@/types'
import { gfsNames, _resetGfsNamesForTests } from './gfsNames'
import { MomentReplyField } from './MomentReplyField'
import { uploadWithProgress } from '@/components/UploadProgress'
import { readVideoDurationMs } from '@/utils/videoDuration'

function parent(extra: Partial<Moment> = {}): Moment {
  return {
    id: 'm-1', author_user_id: 'u-anna', content: 'Wo ist die Fernbedienung?',
    media_url: null, media_type: null, duration_ms: null, parent_moment_id: null,
    origin_instance_id: 'self', created_at: new Date().toISOString(),
    expires_at: new Date(Date.now() + 86_400_000).toISOString(),
    reaction_count: 0, reply_count: 0, ...extra,
  }
}

function renderField(p: Moment, onPosted = vi.fn()) {
  const ref = createRef<HTMLTextAreaElement>()
  return { ref, onPosted, ...render(<MomentReplyField parent={p} inputRef={ref} onPosted={onPosted} />) }
}

describe('MomentReplyField', () => {
  beforeEach(() => {
    currentUser.value = { user_id: 'u-me', username: 'me', display_name: 'Me' } as never
    registrations.value = []
    _resetGfsNamesForTests()
    vi.mocked(api.post).mockReset()
    vi.mocked(showToast).mockClear()
  })

  it('says where the reply goes; Reply waits for text', () => {
    const { getByRole, container, ref } = renderField(parent())
    const field = getByRole('textbox', { name: 'Write a reply' })
    expect(ref.current).toBe(field)
    expect(field.getAttribute('dir')).toBe('auto')
    expect(container.textContent).toContain('Your reply goes to your paired households and theirs, like a moment.')
    expect((getByRole('button', { name: 'Reply' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('a protected account’s reply stays with its household and paired households', () => {
    currentUser.value = { user_id: 'u-me', username: 'me', protected: true } as never
    const { container } = renderField(parent())
    expect(container.textContent).toContain('Your reply goes to your household and paired households, like a moment.')
  })

  it('posts the reply, clears the field and hands the new reply up', async () => {
    const posted = parent({ id: 'r-1', parent_moment_id: 'm-1', author_user_id: 'u-me' })
    vi.mocked(api.post).mockResolvedValue(posted)
    const { getByRole, onPosted } = renderField(parent())
    const field = getByRole('textbox', { name: 'Write a reply' }) as HTMLTextAreaElement
    fireEvent.input(field, { target: { value: '  Im Schrank  ' } })
    fireEvent.click(getByRole('button', { name: 'Reply' }))
    await waitFor(() => expect(onPosted).toHaveBeenCalledWith(posted))
    expect(api.post).toHaveBeenCalledWith('/api/moments', { content: 'Im Schrank', parent_moment_id: 'm-1' })
    expect(field.value).toBe('')
  })

  it('Ctrl+Enter sends', async () => {
    vi.mocked(api.post).mockResolvedValue(parent({ id: 'r-1' }))
    const { getByRole } = renderField(parent())
    const field = getByRole('textbox', { name: 'Write a reply' })
    fireEvent.input(field, { target: { value: 'Hi' } })
    fireEvent.keyDown(field, { key: 'Enter', ctrlKey: true })
    await waitFor(() => expect(api.post).toHaveBeenCalled())
  })

  it('a failed post keeps the text and says so', async () => {
    vi.mocked(api.post).mockRejectedValue(new Error('offline'))
    const { getByRole } = renderField(parent())
    const field = getByRole('textbox', { name: 'Write a reply' }) as HTMLTextAreaElement
    fireEvent.input(field, { target: { value: 'Hi' } })
    fireEvent.click(getByRole('button', { name: 'Reply' }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith("Couldn't post: offline", 'error'))
    expect(field.value).toBe('Hi')
  })

  it('under a public moment, a registered person may reply publicly — off until they switch it on', async () => {
    registrations.value = [{ gfs_id: 'g1', default_share: true } as never]
    gfsNames.value = { g1: 'Demo GFS' }
    vi.mocked(api.post).mockResolvedValue(parent({ id: 'r-1' }))
    const { getByRole, container } = renderField(parent({ is_public: true }))
    const sw = getByRole('switch') as HTMLInputElement
    expect(sw.checked).toBe(false)
    expect(container.textContent).toContain('Also public on Demo GFS')
    fireEvent.click(sw)
    expect(getByRole('button', { name: 'Reply publicly' })).toBeTruthy()
    fireEvent.input(getByRole('textbox', { name: 'Write a reply' }), { target: { value: 'Hi' } })
    fireEvent.click(getByRole('button', { name: 'Reply publicly' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/moments', {
      content: 'Hi', parent_moment_id: 'm-1', is_public: true,
    }))
  })

  it('never offers public under a household moment, and says so on the wire', async () => {
    registrations.value = [{ gfs_id: 'g1', default_share: true } as never]
    vi.mocked(api.post).mockResolvedValue(parent({ id: 'r-1' }))
    const { getByRole, queryByRole } = renderField(parent({ is_public: false }))
    expect(queryByRole('switch')).toBeNull()
    fireEvent.input(getByRole('textbox', { name: 'Write a reply' }), { target: { value: 'Hi' } })
    fireEvent.click(getByRole('button', { name: 'Reply' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/moments', {
      content: 'Hi', parent_moment_id: 'm-1', is_public: false,
    }))
  })

  it('a protected account never gets the public switch', () => {
    currentUser.value = { user_id: 'u-me', username: 'me', restrictions: ['public_moments'] } as never
    registrations.value = [{ gfs_id: 'g1', default_share: true } as never]
    const { queryByRole } = renderField(parent({ is_public: true }))
    expect(queryByRole('switch')).toBeNull()
  })
})

describe('MomentReplyField — a photo or video with the reply', () => {
  beforeEach(() => {
    currentUser.value = { user_id: 'u-me', username: 'me', display_name: 'Me' } as never
    registrations.value = []
    vi.mocked(api.post).mockReset()
    vi.mocked(showToast).mockClear()
    vi.mocked(uploadWithProgress).mockReset()
    vi.mocked(readVideoDurationMs).mockReset().mockResolvedValue(3_000)
  })

  const pick = (container: Element, file: File) => {
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    Object.defineProperty(input, 'files', { value: [file], configurable: true })
    fireEvent.change(input)
  }

  it('a photo button picks images and videos; Reply waits for the upload, then posts it with the reply', async () => {
    let done: (v: unknown) => void = () => {}
    vi.mocked(uploadWithProgress).mockReturnValue(new Promise(r => { done = r }) as never)
    vi.mocked(api.post).mockResolvedValue(parent({ id: 'r-1', parent_moment_id: 'm-1' }))
    const { getByRole, container, onPosted } = renderField(parent())
    const add = getByRole('button', { name: 'Add a photo or video' })
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    expect(input.getAttribute('accept')).toBe('image/*,video/*')
    fireEvent.click(add)
    pick(container, new File(['x'], 'p.jpg', { type: 'image/jpeg' }))
    const reply = getByRole('button', { name: 'Reply' }) as HTMLButtonElement
    await waitFor(() => expect(add.getAttribute('aria-busy')).toBe('true'))
    expect(reply.disabled).toBe(true)
    done({ url: '/media/p.webp', signed_url: 'api/media/p.webp?sig=1' })
    await waitFor(() => expect(container.querySelector('img.sh-moment-replyfield__preview')).not.toBeNull())
    // A picture alone is a reply.
    expect(reply.disabled).toBe(false)
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/moments', {
      content: '', parent_moment_id: 'm-1', media_url: '/media/p.webp', media_type: 'image',
    }))
    await waitFor(() => expect(onPosted).toHaveBeenCalled())
    expect(container.querySelector('img.sh-moment-replyfield__preview')).toBeNull()
  })

  it('the picture can be removed before replying', async () => {
    vi.mocked(uploadWithProgress).mockResolvedValue({ url: '/media/p.webp', signed_url: 'api/media/p.webp' } as never)
    const { getByRole, container } = renderField(parent())
    pick(container, new File(['x'], 'p.jpg', { type: 'image/jpeg' }))
    await waitFor(() => expect(container.querySelector('img.sh-moment-replyfield__preview')).not.toBeNull())
    fireEvent.click(getByRole('button', { name: 'Remove attachment' }))
    expect(container.querySelector('img.sh-moment-replyfield__preview')).toBeNull()
    expect((getByRole('button', { name: 'Reply' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('a video is checked like in the composer: too long is refused and never uploaded', async () => {
    vi.mocked(readVideoDurationMs).mockResolvedValue(40_000)
    const { container } = renderField(parent())
    pick(container, new File(['x'], 'v.mp4', { type: 'video/mp4' }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(expect.stringContaining('15'), 'error'))
    expect(uploadWithProgress).not.toHaveBeenCalled()
  })

  it('a short video goes with its length', async () => {
    vi.mocked(uploadWithProgress).mockResolvedValue({ url: '/media/v.webm', signed_url: 'api/media/v.webm' } as never)
    vi.mocked(api.post).mockResolvedValue(parent({ id: 'r-2', parent_moment_id: 'm-1' }))
    const { container, getByRole } = renderField(parent())
    pick(container, new File(['x'], 'v.mp4', { type: 'video/mp4' }))
    await waitFor(() => expect(container.querySelector('video.sh-moment-replyfield__preview')).not.toBeNull())
    fireEvent.input(getByRole('textbox', { name: 'Write a reply' }), { target: { value: 'Goal!' } })
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/moments', {
      content: 'Goal!', parent_moment_id: 'm-1', media_url: '/media/v.webm', media_type: 'video', duration_ms: 3000,
    }))
  })

  it('something that is neither a picture nor a video is refused', async () => {
    const { container } = renderField(parent())
    pick(container, new File(['x'], 'a.pdf', { type: 'application/pdf' }))
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(expect.any(String), 'error'))
    expect(uploadWithProgress).not.toHaveBeenCalled()
  })
})
