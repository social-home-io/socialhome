import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const { patch, get, toast, confirm } = vi.hoisted(() => ({
  patch: vi.fn(), get: vi.fn(), toast: vi.fn(), confirm: vi.fn(),
}))
vi.mock('@/api', () => ({ api: { patch, get } }))
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

beforeEach(() => {
  patch.mockReset(); patch.mockResolvedValue({}); toast.mockReset(); confirm.mockReset()
  get.mockReset(); get.mockResolvedValue([])
})

const HOME_B = 'bbbbccccddddeeeeffffgggghhhhiiii'
const HOME_C = 'ccccddddeeeeffffgggghhhhiiiijjjj'
/** The owner (local), a local admin, two admins of household B, one of
 *  household C, and a plain member of a fourth household. */
const ROSTER = [
  { user_id: 'u-me', role: 'owner', display_name: 'Me', is_owner: true },
  { user_id: 'u-kid', role: 'admin', display_name: 'Kid' },
  { user_id: 'u-b1', role: 'admin', display_name: 'Bea', instance_id: HOME_B, household_name: 'The Bakers' },
  { user_id: 'u-b2', role: 'admin', display_name: 'Ben', instance_id: HOME_B, household_name: 'The Bakers' },
  { user_id: 'u-c1', role: 'admin', display_name: 'Cleo', instance_id: HOME_C, household_name: null },
  { user_id: 'u-d1', role: 'member', display_name: 'Dan', instance_id: 'ddddeeeeffffgggghhhhiiiijjjjkkkk' },
]
const actSwitch = (view: ReturnType<typeof render>) =>
  view.getByRole('switch', { name: 'Let admins act for me' }) as HTMLInputElement

describe('MembersSection', () => {
  it('a household space has no join choice, only who is in', () => {
    const view = render(<MembersSection space={space({ space_type: 'household' })} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
    expect(view.queryByRole('radio', { name: /Invite only/ })).toBeNull()
    expect(view.getByText('Everyone in your home is a member automatically.')).toBeTruthy()
  })

  it('a local community\'s join modes never say "anyone" — households in the area, the edge to an admin', () => {
    const view = render(<MembersSection space={space({ local_area: { lat: 47.3769, lon: 8.5417, radius_km: 5 } })}
                                        isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
    expect(view.container.textContent).not.toMatch(/Anyone can join/)
    expect(view.getByText('Households in the area join right away. Near the edge, an admin decides.')).toBeTruthy()
    const plain = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
    expect(plain.getByText('Anyone can join right away.')).toBeTruthy()
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

  describe('turning "Let admins act for me" on', () => {
    it('names every other household with an admin, by household ID, before anything is saved', async () => {
      get.mockResolvedValue(ROSTER)
      const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
      fireEvent.click(actSwitch(view))
      const panel = await view.findByTestId('admin-authority-confirm')
      expect(get).toHaveBeenCalledWith('/api/spaces/s1/members')
      expect(patch).not.toHaveBeenCalled()
      expect(actSwitch(view).checked).toBe(false)
      // One row per household — its own household and plain members are not listed.
      const ids = Array.from(panel.querySelectorAll('[data-testid="household-id"]')).map(e => e.textContent)
      expect(ids).toEqual([HOME_B, HOME_C])
      expect(panel.textContent).toContain('The Bakers')
      expect(panel.textContent).toContain('Bea, Ben')
      expect(panel.textContent).toContain('Cleo')
      expect(panel.textContent).not.toContain('Kid')
      expect(panel.textContent).not.toContain('Dan')
    })

    it('saves only once the owner confirms', async () => {
      get.mockResolvedValue(ROSTER)
      const onUpdate = vi.fn()
      const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={onUpdate} />)
      fireEvent.click(actSwitch(view))
      const turnOn = await view.findByRole('button', { name: 'Turn on' })
      fireEvent.click(turnOn)
      fireEvent.click(turnOn)
      await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', {
        features: { delegated_admin_authority: true },
      }))
      await waitFor(() => expect(view.queryByTestId('admin-authority-confirm')).toBeNull())
      expect(actSwitch(view).checked).toBe(true)
      // The note took the focus with it: it is back on the switch.
      expect(document.activeElement).toBe(actSwitch(view))
      expect(patch).toHaveBeenCalledTimes(1)
    })

    it('Cancel leaves it off and sends nothing', async () => {
      get.mockResolvedValue(ROSTER)
      const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
      fireEvent.click(actSwitch(view))
      fireEvent.click(await view.findByRole('button', { name: 'Cancel' }))
      expect(view.queryByTestId('admin-authority-confirm')).toBeNull()
      expect(patch).not.toHaveBeenCalled()
      expect(actSwitch(view).checked).toBe(false)
    })

    it('with no admin in another household it saves at once', async () => {
      get.mockResolvedValue(ROSTER.slice(0, 2))
      const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
      fireEvent.click(actSwitch(view))
      await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', {
        features: { delegated_admin_authority: true },
      }))
      expect(view.queryByTestId('admin-authority-confirm')).toBeNull()
    })

    it('stays off when the admins cannot be loaded', async () => {
      get.mockRejectedValue(new Error('offline'))
      const view = render(<MembersSection space={space()} isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
      fireEvent.click(actSwitch(view))
      await waitFor(() => expect(toast).toHaveBeenCalledWith(
        "Couldn't check who your admins are, so nothing was changed. Try again.", 'error',
      ))
      expect(patch).not.toHaveBeenCalled()
      expect(actSwitch(view).checked).toBe(false)
    })

    it('turning it off asks nothing', async () => {
      const view = render(<MembersSection space={space({ features: { delegated_admin_authority: true } } as Partial<Space>)}
                                          isRemoteSpace={false} members={null} onUpdate={vi.fn()} />)
      fireEvent.click(actSwitch(view))
      await waitFor(() => expect(patch).toHaveBeenCalledWith('/api/spaces/s1', {
        features: { delegated_admin_authority: false },
      }))
      expect(get).not.toHaveBeenCalled()
    })
  })
})
