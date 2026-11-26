import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, waitFor, fireEvent, cleanup } from '@testing-library/preact'

const { get, del, post, toast } = vi.hoisted(() => ({
  get: vi.fn(), del: vi.fn(), post: vi.fn(), toast: vi.fn(),
}))
vi.mock('@/api', async (orig) => ({
  ...(await orig<object>()),
  api: { get, delete: del, post },
}))
vi.mock('./Toast', () => ({ showToast: toast }))

import { ApiError } from '@/api'
import { SpaceBanList } from './SpaceBanList'

beforeEach(() => {
  cleanup()
  get.mockReset(); del.mockReset(); post.mockReset(); toast.mockReset()
  del.mockResolvedValue(undefined)
  post.mockResolvedValue({})
})

const serve = (rows: unknown) => get.mockImplementation(async () => rows)

describe('SpaceBanList (Settings › Moderation, owner decision C)', () => {
  it('names each banned person from the server, never "Someone" for all', async () => {
    serve([
      {
        kind: 'person', user_id: 'ru-rita', banned_at: '2026-01-01 10:00:00',
        display_name: 'Remote Rita', picture_url: 'api/users/ru-rita/picture?v=1&sig=x',
      },
      { kind: 'person', user_id: 'ru-sam', banned_at: '2026-01-01', display_name: null, personal_alias: 'Sammy' },
      { kind: 'person', user_id: 'ru-gone', banned_at: '2026-01-01', display_name: null },
    ])
    const r = render(<SpaceBanList spaceId="sp-1" viewerIsOwner ownerInstanceId={null} />)
    await waitFor(() => expect(r.container.querySelectorAll('.sh-member-row--banned').length).toBe(3))
    const names = [...r.container.querySelectorAll('.sh-member-name')].map(n => n.textContent)
    expect(names).toEqual(['Remote Rita', 'Sammy', 'Someone'])
    expect(r.container.querySelector('img')?.getAttribute('src')).toContain('ru-rita')
    expect(r.container.textContent).not.toContain('ru-gone')
    expect(r.container.textContent).toContain('Banned Jan 1, 2026')
  })

  it('lists a household ban by its name and lifts it on its own route, saying what it means', async () => {
    serve([
      { kind: 'household', instance_id: 'peer-x', banned_at: '2026-01-01', household_name: 'Smith Household', reason: 'Bots' },
      { kind: 'household', instance_id: 'peer-y', banned_at: '2026-01-01', household_name: null },
    ])
    const r = render(<SpaceBanList spaceId="sp-1" viewerIsOwner ownerInstanceId={null} />)
    await waitFor(() => expect(r.container.querySelectorAll('[data-ban-kind="household"]').length).toBe(2))
    expect([...r.container.querySelectorAll('.sh-member-name')].map(n => n.textContent))
      .toEqual(['Smith Household', 'A household'])
    expect(r.container.textContent).toContain('Bots')
    expect(r.container.textContent).not.toContain('peer-y')
    fireEvent.click(r.getByLabelText('Unban Smith Household'))
    await waitFor(() => expect(del).toHaveBeenCalledWith('/api/spaces/sp-1/household-bans/peer-x'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith(
      'Ban lifted. People from Smith Household can be invited again.', 'success'))
  })

  it("an admin sees why the owner's ban stays, not an Unban that fails", async () => {
    serve([
      { kind: 'person', user_id: 'u-a', banned_at: '2026-01-01', display_name: 'Al', banned_by_owner: true },
      { kind: 'person', user_id: 'u-b', banned_at: '2026-01-01', display_name: 'Bea', banned_by_owner: false },
    ])
    const r = render(<SpaceBanList spaceId="sp-1" viewerIsOwner={false} ownerInstanceId={null} />)
    await waitFor(() => expect(r.container.textContent).toContain('Bea'))
    expect(r.queryByLabelText('Unban Al')).toBeNull()
    expect(r.container.textContent).toContain('Only the space owner can lift this ban.')
    expect(r.queryByLabelText('Unban Bea')).not.toBeNull()
  })

  it('a refused unban is said in the viewer\'s words', async () => {
    serve([{ kind: 'person', user_id: 'u-a', banned_at: '2026-01-01', display_name: 'Al' }])
    del.mockRejectedValueOnce(new ApiError(403, '/x', { code: 'OWNER_BAN', detail: 'x' }))
    const r = render(<SpaceBanList spaceId="sp-1" viewerIsOwner={false} ownerInstanceId={null} />)
    await waitFor(() => expect(r.container.textContent).toContain('Al'))
    fireEvent.click(r.getByLabelText('Unban Al'))
    await waitFor(() => expect(toast).toHaveBeenCalledWith('Only the space owner can lift this ban.', 'error'))
  })

  it('a banned person offers banning their household — never the owner household, never twice (owner decision B)', async () => {
    serve([
      { kind: 'person', user_id: 'u-a', banned_at: '2026-01-01', display_name: 'Al', instance_id: 'peer-a', household_name: 'A Home' },
      { kind: 'person', user_id: 'u-o', banned_at: '2026-01-01', display_name: 'Olga', instance_id: 'peer-owner' },
      { kind: 'person', user_id: 'u-c', banned_at: '2026-01-01', display_name: 'Cy', instance_id: 'peer-c' },
      { kind: 'household', instance_id: 'peer-c', banned_at: '2026-01-01', household_name: 'C Home' },
      { kind: 'person', user_id: 'u-n', banned_at: '2026-01-01', display_name: 'Nobody known', instance_id: null },
    ])
    const r = render(<SpaceBanList spaceId="sp-1" viewerIsOwner ownerInstanceId="peer-owner" />)
    await waitFor(() => expect(r.container.textContent).toContain('Nobody known'))
    const offers = r.container.querySelectorAll('[data-action="ban-their-household"]')
    expect(offers.length).toBe(1)
    fireEvent.click(offers[0])
    await waitFor(() => expect(r.baseElement.textContent).toContain('Ban A Home?'))
    fireEvent.click(r.baseElement.querySelector('[data-action="ban-confirm"]') as HTMLElement)
    await waitFor(() => expect(post).toHaveBeenCalledWith('/api/spaces/sp-1/ban', { instance_id: 'peer-a' }))
  })

  it('an empty list says nobody is banned', async () => {
    serve([])
    const r = render(<SpaceBanList spaceId="sp-1" viewerIsOwner ownerInstanceId={null} />)
    await waitFor(() => expect(r.getByTestId('bans-empty').textContent).toBe('Nobody is banned.'))
  })

  it('a failed load offers Retry', async () => {
    get.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    const r = render(<SpaceBanList spaceId="sp-1" viewerIsOwner ownerInstanceId={null} />)
    await waitFor(() => expect(r.container.textContent).toContain("Couldn't load who is banned."))
  })
})
