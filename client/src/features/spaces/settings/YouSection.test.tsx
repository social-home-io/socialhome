import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({ get: vi.fn(), delete: vi.fn(), patch: vi.fn(), upload: vi.fn() }))
const { toast, confirm, nav } = vi.hoisted(() => ({ toast: vi.fn(), confirm: vi.fn(), nav: { route: vi.fn() } }))
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))
vi.mock('preact-iso', async (orig) => ({ ...(await orig<object>()), useLocation: () => ({ route: nav.route }) }))

import { currentUser } from '@/store/auth'
import type { Space } from '@/types'
import { LeaveSection, UnfollowSection, YouSection } from './YouSection'

const sp = { id: 's1', name: 'Football', space_type: 'private', join_mode: 'invite_only', features: {} } as Space

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  api.get.mockResolvedValue([])
  api.delete.mockResolvedValue({})
  toast.mockReset(); confirm.mockReset(); nav.route.mockReset()
  currentUser.value = { user_id: 'u1', username: 'pascal', display_name: 'Pascal' } as never
})

describe('YouSection', () => {
  it('names you as the space shows you, and opens the space profile', () => {
    const view = render(<YouSection space={sp} seat={{ display_name: 'Pascal' }} locationAllowed={false} sharing={false} />)
    expect(view.getByTestId('settings-you-profile').textContent).toContain('Pascal · uses your household profile')
    expect(view.queryByTestId('settings-you-location')).toBeNull()
  })

  it('a space nickname reads as yours here; location links to the Map', () => {
    const view = render(<YouSection space={sp} seat={{ display_name: 'Pascal', space_display_name: 'Coach P' }}
                                    locationAllowed sharing />)
    expect(view.getByTestId('settings-you-profile').textContent).toContain('Coach P · just in this space')
    const loc = view.getByTestId('settings-you-location')
    expect(loc.getAttribute('href')).toBe('/spaces/s1/map')
    expect(loc.textContent).toContain('On')
  })
})

describe('Leave / Unfollow', () => {
  it('Leave asks, drops the seat and goes to the list', async () => {
    confirm.mockResolvedValue(true)
    const view = render(<LeaveSection space={sp} />)
    expect((view.getByTestId('settings-leave') as HTMLDetailsElement).open).toBe(false)
    fireEvent.click(view.getByRole('button', { name: 'Leave space' }))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/spaces/s1/members/u1'))
    expect(nav.route).toHaveBeenCalledWith('/spaces', true)
  })

  it('Leave cancelled does nothing', async () => {
    confirm.mockResolvedValue(false)
    const view = render(<LeaveSection space={sp} />)
    fireEvent.click(view.getByRole('button', { name: 'Leave space' }))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(api.delete).not.toHaveBeenCalled()
  })

  it('Unfollow stops following and goes to the list', async () => {
    const view = render(<UnfollowSection space={sp} />)
    fireEvent.click(view.getByRole('button', { name: /Unfollow/ }))
    await waitFor(() => expect(api.delete).toHaveBeenCalledWith('/api/spaces/s1/subscribe'))
    expect(nav.route).toHaveBeenCalledWith('/spaces', true)
  })
})
