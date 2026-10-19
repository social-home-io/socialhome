import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { patch, toast, confirm } = vi.hoisted(() => ({ patch: vi.fn(), toast: vi.fn(), confirm: vi.fn() }))
vi.mock('@/api', () => ({ api: { patch } }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

import type { Space } from '@/types'
import { MembersSection } from './MembersSection'

function space(over: Partial<Space> = {}): Space {
  return {
    id: 's1', name: 'Garden', space_type: 'public', join_mode: 'invite_only',
    features: { posts_access: 'open' }, ...over,
  } as Space
}

beforeEach(() => { patch.mockReset(); patch.mockResolvedValue({}); toast.mockReset(); confirm.mockReset() })

describe('MembersSection', () => {
  it('a household space has no join choice, only who is in', () => {
    const view = render(<MembersSection space={space({ space_type: 'household' })} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
    expect(view.queryByRole('radio', { name: /Invite only/ })).toBeNull()
    expect(view.getByText('Everyone in your home is a member automatically.')).toBeTruthy()
  })

  it('the join mode saves the moment it is picked', async () => {
    const onUpdate = vi.fn()
    const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={onUpdate} />)
    fireEvent.click(view.getByRole('radio', { name: /Open/ }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { join_mode: 'open' }))
    expect(onUpdate).toHaveBeenCalled()
  })

  it('a refused save puts the pick back', async () => {
    patch.mockRejectedValue(new Error('nope'))
    const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('radio', { name: /Open/ }))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('nope', 'error'))
    expect((view.getByRole('radio', { name: /Invite only/ }) as HTMLInputElement).checked).toBe(true)
  })

  it('links to Members & roles with the head count', () => {
    const view = render(<MembersSection space={space()} isRemoteSpace={false}
                                        members={{ count: 8, admins: 2 }} onUpdate={vi.fn()} />)
    const link = view.getByTestId('settings-members-link')
    expect(link.getAttribute('href')).toBe('/spaces/s1/members')
    expect(link.textContent).toContain('8 people · 2 admins')
  })

  it('an access level saves alone; an older household asks, then forces', async () => {
    patch
      .mockRejectedValueOnce({ code: 'PEERS_TOO_OLD', extra: { households: [{ instance_id: 'i', display_name: 'Granny' }] } })
      .mockResolvedValueOnce({})
    confirm.mockResolvedValue(true)
    const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
    const select = view.container.querySelector('select[data-feature="posts"]') as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'moderated' } })
    await waitFor(() => expect(patch).toHaveBeenLastCalledWith('/api/spaces/s1', {
      features: { posts_access: 'moderated' }, force: true,
    }))
    expect(select.value).toBe('moderated')
  })

  it('a forwarded access change shows the level in force', async () => {
    patch.mockResolvedValue({ forwarded: true })
    const view = render(<MembersSection space={space()} isRemoteSpace members={null} onUpdate={vi.fn()} />)
    const select = view.container.querySelector('select[data-feature="posts"]') as HTMLSelectElement
    fireEvent.change(select, { target: { value: 'admin_only' } })
    await waitFor(() => expect(patch).toHaveBeenCalled())
    await waitFor(() => expect(select.value).toBe('open'))
  })

  it('followers: the first switch gates the other two; each saves alone', async () => {
    const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
    expect((view.getByRole('switch', { name: 'Let followers leave reactions' }) as HTMLInputElement).disabled).toBe(true)
    fireEvent.click(view.getByRole('switch', { name: /Let anyone follow this space/ }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { features: { allow_subscribers: true } }))
    expect((view.getByRole('switch', { name: 'Let followers leave reactions' }) as HTMLInputElement).disabled).toBe(false)
  })

  it('@here saves alone; the admins-act-for-me switch is hidden on a space hosted elsewhere', async () => {
    const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('switch', { name: /notify everyone with @here/ }))
    await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', { allow_here_mention: true }))
    expect(view.getByRole('switch', { name: 'Let admins act for me' })).toBeTruthy()
    view.unmount()
    const remote = render(<MembersSection space={space()} isRemoteSpace members={null} onUpdate={vi.fn()} />)
    expect(remote.queryByRole('switch', { name: 'Let admins act for me' })).toBeNull()
  })
})
