import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), delete: vi.fn() }))
const { toast, confirm, nav, dissolve } = vi.hoisted(() => ({
  toast: vi.fn(), confirm: vi.fn(), nav: { route: vi.fn() },
  dissolve: { mark: vi.fn(), clear: vi.fn() },
}))
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))
vi.mock('preact-iso', async (orig) => ({ ...(await orig<object>()), useLocation: () => ({ route: nav.route }) }))
vi.mock('@/store/spaces', () => ({ markLocalDissolve: dissolve.mark, clearLocalDissolve: dissolve.clear }))

import type { Space } from '@/types'
import { DangerSection } from './DangerSection'

function space(over: Partial<Space> = {}): Space {
  return { id: 's1', name: 'Garden', space_type: 'private', join_mode: 'invite_only', features: {}, ...over } as Space
}

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  api.get.mockResolvedValue([])
  toast.mockReset(); confirm.mockReset(); nav.route.mockReset()
  dissolve.mark.mockReset(); dissolve.clear.mockReset()
})

describe('DangerSection', () => {
  it('is one collapsed row unless its own URL opened it', () => {
    const view = render(<DangerSection space={space()} isOwner isRemoteSpace={false} open={false} onUpdate={vi.fn()} />)
    const details = view.getByTestId('settings-danger') as HTMLDetailsElement
    expect(details.open).toBe(false)
    expect(details.querySelector('summary')!.textContent).toContain('Archive, hand over or delete')
  })

  it('the owner of a space hosted here can hand it over; nobody else', () => {
    const owner = render(<DangerSection space={space()} isOwner isRemoteSpace={false} open onUpdate={vi.fn()} />)
    expect(owner.getByTestId('owner-transfer')).toBeTruthy()
    owner.unmount()
    for (const [isOwner, remote] of [[false, false], [true, true]] as const) {
      const v = render(<DangerSection space={space()} isOwner={isOwner} isRemoteSpace={remote} open onUpdate={vi.fn()} />)
      expect(v.queryByTestId('owner-transfer')).toBeNull()
      v.unmount()
    }
  })

  it('archives, and a dissolved space offers no Unarchive', async () => {
    api.post.mockResolvedValue({})
    const onUpdate = vi.fn()
    const view = render(<DangerSection space={space()} isOwner isRemoteSpace={false} open onUpdate={onUpdate} />)
    fireEvent.click(view.getByRole('button', { name: 'Archive space' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/spaces/s1/archive'))
    expect(onUpdate).toHaveBeenCalled()
    view.unmount()
    const gone = render(<DangerSection space={space({ archived: true, archived_reason: 'dissolved' } as Partial<Space>)}
                                       isOwner isRemoteSpace={false} open onUpdate={vi.fn()} />)
    expect(gone.queryByRole('button', { name: 'Unarchive' })).toBeNull()
  })

  it('an executed delete claims the dissolve and leaves for the list in-app', async () => {
    confirm.mockResolvedValue(true)
    api.post.mockResolvedValue({ proposal: { status: 'executed' } })
    const view = render(<DangerSection space={space()} isOwner isRemoteSpace={false} open onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('button', { name: 'Dissolve space' }))
    await waitFor(() => expect(nav.route).toHaveBeenCalledWith('/spaces', true))
    expect(dissolve.mark).toHaveBeenCalledWith('s1')
    expect(api.post).toHaveBeenCalledWith('/api/spaces/s1/proposals', { action: 'dissolve' })
  })

  it('an only-proposed or failed delete stays and releases the claim; cancel sends nothing', async () => {
    confirm.mockResolvedValue(false)
    const view = render(<DangerSection space={space()} isOwner isRemoteSpace={false} open onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('button', { name: 'Dissolve space' }))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(api.post).not.toHaveBeenCalled()
    confirm.mockResolvedValue(true)
    api.post.mockResolvedValueOnce({ proposal: { status: 'pending' } }).mockRejectedValueOnce(new Error('x'))
    fireEvent.click(view.getByRole('button', { name: 'Dissolve space' }))
    await waitFor(() => expect(dissolve.clear).toHaveBeenCalledTimes(1))
    fireEvent.click(view.getByRole('button', { name: 'Dissolve space' }))
    await waitFor(() => expect(dissolve.clear).toHaveBeenCalledTimes(2))
    expect(nav.route).not.toHaveBeenCalled()
  })
})
