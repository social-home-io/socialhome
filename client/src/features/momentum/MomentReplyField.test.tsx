import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, fireEvent, waitFor } from '@testing-library/preact'
import { createRef } from 'preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }))

import { api } from '@/api'
import { showToast } from '@/components/Toast'
import { currentUser } from '@/store/auth'
import { registrations } from '@/store/momentPublic'
import type { Moment } from '@/types'
import { gfsNames, _resetGfsNamesForTests } from './gfsNames'
import { MomentReplyField } from './MomentReplyField'

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
