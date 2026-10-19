/**
 * Settings › Who can find it: the four tiers, proposals with the quorum
 * line, the Global GFS choice, and the owner's GFS switches.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { fireEvent, render, waitFor } from '@testing-library/preact'

const api = vi.hoisted(() => ({
  get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn(),
}))
const { toast, confirm } = vi.hoisted(() => ({ toast: vi.fn(), confirm: vi.fn() }))
vi.mock('@/api', () => ({ api }))
vi.mock('@/components/Toast', () => ({ showToast: toast }))
vi.mock('@/components/confirm', () => ({ confirmDialog: confirm }))

import { currentUser } from '@/store/auth'
import type { Space } from '@/types'
import { FindSection, quorumNote } from './FindSection'

const conns = [
  { id: 'g-a', display_name: 'Town GFS', inbox_url: 'https://town.example', status: 'active' },
  { id: 'g-b', display_name: 'Club GFS', inbox_url: 'https://club.example', status: 'active' },
  { id: 'g-h', display_name: 'Held GFS', inbox_url: 'https://held.example', status: 'pending' },
]

function space(over: Partial<Space> = {}): Space {
  return {
    id: 's1', name: 'Garden', space_type: 'private', join_mode: 'invite_only',
    features: {}, ...over,
  } as Space
}

const boxes = (c: Element) =>
  [...c.querySelectorAll<HTMLInputElement>('input[name="space-tier-gfs"]')]

beforeEach(() => {
  for (const f of Object.values(api)) f.mockReset()
  toast.mockReset()
  confirm.mockReset()
  api.get.mockImplementation((url: string) => Promise.resolve(
    url === '/api/gfs/connections' ? conns
      : url.endsWith('/gfs-servers') ? { listings: [], available: [], max: 8 } : []))
  api.post.mockResolvedValue({ proposal: { status: 'pending' } })
  api.patch.mockResolvedValue({})
  currentUser.value = { user_id: 'u1', username: 'anna', is_admin: true } as never
})

describe('quorumNote', () => {
  it('names how many admins must agree', () => {
    expect(quorumNote(1)).toBe("You're the only admin, so a change applies right away.")
    expect(quorumNote(2)).toBe('With 2 admins, a change needs both to agree.')
    expect(quorumNote(5)).toBe('With 5 admins, a change needs 3 of them to agree.')
  })
})

describe('FindSection — tiers', () => {
  it('shows the four tiers, the current one picked, and the quorum line', () => {
    const view = render(<FindSection space={space()} isOwner isRemoteSpace={false} adminCount={2} onUpdate={vi.fn()} />)
    const radios = view.getAllByRole('radio') as HTMLInputElement[]
    expect(radios.map(r => r.value)).toEqual(['private', 'household', 'public', 'global'])
    expect(radios[0].checked).toBe(true)
    expect(view.getByText(/Never on a GFS/)).toBeTruthy()
    expect(view.getByTestId('tier-quorum').textContent).toBe('With 2 admins, a change needs both to agree.')
    expect(view.queryByTestId('tier-pending')).toBeNull()
  })

  it('a pick opens a confirm that proposes; Cancel backs out', async () => {
    const onUpdate = vi.fn()
    const view = render(<FindSection space={space()} isOwner isRemoteSpace={false} adminCount={2} onUpdate={onUpdate} />)
    fireEvent.click(view.getByRole('radio', { name: /Household/ }))
    fireEvent.click(view.getByRole('button', { name: 'Cancel' }))
    expect(view.queryByTestId('tier-pending')).toBeNull()
    expect((view.getByRole('radio', { name: /Private/ }) as HTMLInputElement).checked).toBe(true)

    fireEvent.click(view.getByRole('radio', { name: /Public/ }))
    fireEvent.click(view.getByRole('button', { name: 'Propose Public' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/spaces/s1/proposals', {
      action: 'set_public_tier', space_type: 'public',
    }))
    expect(toast).toHaveBeenCalledWith('Change proposed. Most admins need to agree.', 'info')
    expect(onUpdate).not.toHaveBeenCalled()
  })

  it('a sole admin changes it at once', async () => {
    api.post.mockResolvedValue({ proposal: { status: 'executed' } })
    const onUpdate = vi.fn()
    const view = render(<FindSection space={space()} isOwner isRemoteSpace={false} adminCount={1} onUpdate={onUpdate} />)
    fireEvent.click(view.getByRole('radio', { name: /Household/ }))
    fireEvent.click(view.getByRole('button', { name: 'Change to Household' }))
    await waitFor(() => expect(onUpdate).toHaveBeenCalled())
  })

  it('Global from the owner household requires a GFS and sends the choice', async () => {
    const view = render(<FindSection space={space({ space_type: 'public' })} isOwner isRemoteSpace={false} adminCount={2} onUpdate={vi.fn()} />)
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/gfs/connections'))
    fireEvent.click(view.getByRole('radio', { name: /Global/ }))
    await waitFor(() => expect(boxes(view.container).map(b => b.value)).toEqual(['g-a', 'g-b']))
    fireEvent.click(view.getByRole('button', { name: 'Propose Global' }))
    expect(api.post).not.toHaveBeenCalled()
    expect(view.container.querySelector('#space-tier-error')).not.toBeNull()
    fireEvent.click(boxes(view.container)[1])
    fireEvent.click(view.getByRole('button', { name: 'Propose Global' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/spaces/s1/proposals', {
      action: 'set_public_tier', space_type: 'global', gfs_ids: ['g-b'],
    }))
  })

  it('with no active GFS, says how to connect one and holds the proposal', async () => {
    api.get.mockResolvedValue([conns[2]])
    const view = render(<FindSection space={space()} isOwner isRemoteSpace={false} adminCount={1} onUpdate={vi.fn()} />)
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    fireEvent.click(view.getByRole('radio', { name: /Global/ }))
    expect(view.getByTestId('tier-gfs-none').querySelector('a')!.getAttribute('href')).toBe('/connections')
    expect((view.getByRole('button', { name: 'Change to Global' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('a failed connections load offers Retry', async () => {
    api.get.mockRejectedValueOnce(new Error('offline')).mockResolvedValue(conns)
    const view = render(<FindSection space={space()} isOwner isRemoteSpace={false} adminCount={1} onUpdate={vi.fn()} />)
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    fireEvent.click(view.getByRole('radio', { name: /Global/ }))
    fireEvent.click(await view.findByRole('button', { name: 'Retry' }))
    await waitFor(() => expect(boxes(view.container)).toHaveLength(2))
  })

  it('a remote admin proposing Global reads that the owner chooses, and sends no gfs_ids', async () => {
    const view = render(<FindSection space={space()} isOwner={false} isRemoteSpace adminCount={2} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('radio', { name: /Global/ }))
    expect(view.getByTestId('tier-gfs-owner-chooses')).toBeTruthy()
    expect(api.get).not.toHaveBeenCalledWith('/api/gfs/connections')
    fireEvent.click(view.getByRole('button', { name: 'Propose Global' }))
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/spaces/s1/proposals', {
      action: 'set_public_tier', space_type: 'global',
    }))
  })

  it('leaving Global warns that every GFS drops it', () => {
    const view = render(<FindSection space={space({ space_type: 'global' })} isOwner isRemoteSpace={false} adminCount={2} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('radio', { name: /Public/ }))
    expect(view.getByTestId('tier-leaving-global')).toBeTruthy()
  })
})

describe('FindSection — the owner and the GFS', () => {
  it('a private space: only the owner sees the GFS switch; on saves at once', async () => {
    const admin = render(<FindSection space={space()} isOwner={false} isRemoteSpace={false} adminCount={2} onUpdate={vi.fn()} />)
    expect(admin.queryByTestId('private-gfs')).toBeNull()
    admin.unmount()
    const view = render(<FindSection space={space()} isOwner isRemoteSpace={false} adminCount={2} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('switch', { name: 'Use the GFS for this space' }))
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith('/api/spaces/s1', { features: { private_gfs: true } }))
  })

  it('turning the GFS off asks first; a link-joined household blocks it and is named', async () => {
    confirm.mockResolvedValue(true)
    api.patch.mockRejectedValue({
      code: 'PRIVATE_GFS_LINK_MEMBERS',
      extra: { households: [{ instance_id: 'h1', display_name: 'The Millers', members: [] }] },
    })
    const sp = space({ features: { private_gfs: true } } as Partial<Space>)
    const view = render(<FindSection space={sp} isOwner isRemoteSpace={false} adminCount={1} onUpdate={vi.fn()} />)
    const sw = view.getByRole('switch', { name: 'Use the GFS for this space' }) as HTMLInputElement
    fireEvent.click(sw)
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect((await view.findByTestId('private-gfs-link-members')).textContent).toContain('The Millers')
    expect(sw.checked).toBe(true)
  })

  it('cancelling the off-confirm sends nothing', async () => {
    confirm.mockResolvedValue(false)
    const sp = space({ features: { private_gfs: true } } as Partial<Space>)
    const view = render(<FindSection space={sp} isOwner isRemoteSpace={false} adminCount={1} onUpdate={vi.fn()} />)
    fireEvent.click(view.getByRole('switch', { name: 'Use the GFS for this space' }))
    await waitFor(() => expect(confirm).toHaveBeenCalled())
    expect(api.patch).not.toHaveBeenCalled()
  })

  it('posting through the GFS: owner of a followable public space, saves exactly the mode', async () => {
    const sp = space({ space_type: 'public', features: { allow_subscribers: true } } as Partial<Space>)
    const view = render(<FindSection space={sp} isOwner isRemoteSpace={false} adminCount={1} onUpdate={vi.fn()} />)
    const group = view.getByRole('radiogroup', { name: /Posting through the GFS/ })
    fireEvent.click([...group.querySelectorAll('[role="radio"]')].find(b => b.textContent === 'Strict')!)
    await waitFor(() => expect(api.patch).toHaveBeenCalledWith('/api/spaces/s1', { features: { gfs_publish_mode: 'strict' } }))
  })

  it('posting through the GFS is hidden where members never publish over it', () => {
    const view = render(<FindSection space={space()} isOwner isRemoteSpace={false} adminCount={1} onUpdate={vi.fn()} />)
    expect(view.queryByRole('radiogroup', { name: /Posting through the GFS/ })).toBeNull()
  })
})
