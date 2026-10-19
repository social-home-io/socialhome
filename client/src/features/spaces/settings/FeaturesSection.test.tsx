import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { patch, toast } = vi.hoisted(() => ({ patch: vi.fn(), toast: vi.fn() }))
vi.mock('@/api', () => ({ api: { patch } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))

import type { Space } from '@/types'
import { FeaturesSection, nextAllowedPostTypes } from './FeaturesSection'

function space(features: Record<string, unknown> = {}): Space {
  return { id: 's1', name: 'Garden', space_type: 'private', join_mode: 'invite_only', features } as unknown as Space
}

beforeEach(() => { patch.mockReset(); patch.mockResolvedValue({}); toast.mockReset() })

describe('nextAllowedPostTypes', () => {
  it('keeps types the UI does not offer and sorts', () => {
    expect(nextAllowedPostTypes(['text', 'transcript', 'image'], 'image', false)).toEqual(['text', 'transcript'])
    expect(nextAllowedPostTypes(['text'], 'poll', true)).toEqual(['poll', 'text'])
  })

  it('a missing list means all allowed', () => {
    expect(nextAllowedPostTypes(undefined, 'video', false)).not.toContain('video')
  })

  it('refuses to turn off the last kind', () => {
    expect(nextAllowedPostTypes(['text'], 'text', false)).toBeNull()
  })
})

describe('FeaturesSection', () => {
  it('mirrors the stored sections, timetable off and the rest on by default', () => {
    const view = render(<FeaturesSection space={space({ gallery: false })} onUpdate={vi.fn()} />)
    const on = (name: string) => (view.getByRole('switch', { name }) as HTMLInputElement).checked
    expect(on('Chat')).toBe(true)
    expect(on('Timetable')).toBe(false)
    expect(on('Gallery')).toBe(false)
    expect(on('Bazaar')).toBe(true)
  })

  it('a section switch saves alone and puts itself back on failure', async () => {
    patch.mockRejectedValueOnce(new Error('offline'))
    const view = render(<FeaturesSection space={space()} onUpdate={vi.fn()} />)
    const chat = view.getByRole('switch', { name: 'Chat' }) as HTMLInputElement
    fireEvent.click(chat)
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { features: { chat: false } }))
    await waitFor(() => expect(chat.checked).toBe(true))
    fireEvent.click(view.getByRole('switch', { name: 'Timetable' }))
    await waitFor(() => expect(patch).toHaveBeenLastCalledWith('/api/spaces/s1', { features: { timetable: true } }))
  })

  it('a post type unchecks with the rest preserved; the last one stays', async () => {
    const view = render(<FeaturesSection space={space({ allowed_post_types: ['text', 'poll', 'event'] })} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('checkbox', { name: 'Poll' }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', {
      features: { allowed_post_types: ['event', 'text'] },
    }))
    patch.mockReset()
    fireEvent.click(view.getByRole('checkbox', { name: 'Text' }))
    expect(patch).not.toHaveBeenCalled()
    expect(toast).toHaveBeenCalledWith('Turn on at least one kind of post.', 'error')
  })
})
