import { describe, it, expect, vi, afterEach } from 'vitest'
import { setLocale } from '@/i18n/i18n'
import { render, waitFor, fireEvent } from '@testing-library/preact'

type WsHandler = (e: { data: Record<string, unknown> }) => void
const wsHandlers = new Map<string, WsHandler[]>()
vi.mock('@/ws', () => ({
  ws: {
    on: (type: string, fn: WsHandler) => {
      wsHandlers.set(type, [...(wsHandlers.get(type) ?? []), fn])
      return () => {
        wsHandlers.set(type, (wsHandlers.get(type) ?? []).filter(h => h !== fn))
      }
    },
  },
}))
function emit(type: string, data: Record<string, unknown>) {
  for (const h of wsHandlers.get(type) ?? []) h({ data })
}

const openInvite = vi.fn()
vi.mock('./RemoteInviteDialog', () => ({ openRemoteInviteDialog: (...a: unknown[]) => openInvite(...a) }))

const apiGet = vi.fn()
vi.mock('@/api', () => ({
  api: {
    get: (...a: unknown[]) => apiGet(...a),
    post: vi.fn(), patch: vi.fn(), delete: vi.fn(),
  },
}))

apiGet.mockImplementation(async (url: string) => {
  if (url.endsWith('/members')) {
    return [
      { user_id: 'u-o', display_name: 'Olivia', role: 'owner', joined_at: '2026-01-01' },
      { user_id: 'u-a', display_name: 'Adam', role: 'admin', joined_at: '2026-01-01' },
      { user_id: 'u-m', display_name: 'Mo', role: 'moderator', joined_at: '2026-01-01' },
      { user_id: 'u-b', display_name: 'Bob', role: 'member', joined_at: '2026-01-01' },
    ]
  }
  return []
})

vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'u-m', display_name: 'Mo', is_admin: false, picture_url: null } },
}))

const openPersonSpy = vi.hoisted(() => vi.fn())
vi.mock('@/components/PersonSheet', () => ({
  openPerson: (...args: unknown[]) => openPersonSpy(...args),
}))

describe('SpaceMemberList', () => {
  it('an admin sees "N join requests · Review", leading to Moderation', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" joinRequests={2} />)
    await waitFor(() => expect(r.container.querySelector('.sh-member-join-requests')).toBeTruthy())
    const row = r.container.querySelector('.sh-member-join-requests') as HTMLAnchorElement
    expect(row.getAttribute('href')).toBe('/spaces/sp-1/moderation')
    expect(row.textContent).toBe('2 join requestsReview')
  })

  it('no join-requests row with none waiting, nor for a moderator', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const a = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" joinRequests={0} />)
    await waitFor(() => expect(a.container.textContent).toContain('Olivia'))
    expect(a.container.querySelector('.sh-member-join-requests')).toBeNull()
    a.unmount()
    const m = render(<SpaceMemberList spaceId="sp-1" viewerRole="moderator" joinRequests={3} />)
    await waitFor(() => expect(m.container.textContent).toContain('Olivia'))
    expect(m.container.querySelector('.sh-member-join-requests')).toBeNull()
  })

  it('module exports exist', async () => {
    const mod = await import('./SpaceMemberList')
    expect(mod).toBeTruthy()
    expect(Object.keys(mod).length).toBeGreaterThan(0)
  })

  it('an admin gets ONE Invite button: people or an invite link, in one dialog', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" />)
    const btn = await waitFor(() => r.getByRole('button', { name: '+ Invite' }))
    expect(r.container.querySelectorAll('.sh-member-list-invite-actions button')).toHaveLength(1)
    fireEvent.click(btn)
    expect(openInvite).toHaveBeenCalledWith('sp-1', 'admin')
  })

  it('badges owner, admin and moderator; a moderator manages nobody', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="moderator" />)
    await waitFor(() => expect(r.container.querySelector('.sh-badge--moderator')).toBeTruthy())
    expect(r.container.querySelector('.sh-badge--moderator')!.textContent).toBe('Moderator')
    expect(r.container.querySelector('.sh-badge--admin')!.textContent).toBe('Admin')
    expect(r.container.querySelector('.sh-badge--owner')!.textContent).toBe('Owner')
    // Content authority only — no member-management kebab, no invites.
    expect(r.container.querySelector('.sh-post-overflow')).toBeNull()
    expect(r.container.querySelector('.sh-member-list-invite-actions')).toBeNull()
    expect(r.container.textContent).not.toContain('+ Invite')
  })

  it('a follower sees who, never when: no presence dot, no join date', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    apiGet.mockImplementationOnce(async () => [
      // A follower's roster carries no presence / joined_at at all.
      { user_id: 'u-o', display_name: 'Olivia', role: 'owner' },
    ])
    const r = render(<SpaceMemberList spaceId="sp-follow" viewerRole="subscriber" />)
    await waitFor(() => expect(r.container.textContent).toContain('Olivia'))
    // The household-wide ``user.online`` frame must not paint a dot.
    emit('user.online', { user_id: 'u-o' })
    await new Promise(res => setTimeout(res, 10))
    expect(r.container.querySelector('.sh-avatar-status-dot')).toBeNull()
    expect(r.container.querySelector('.sh-member-joined')).toBeNull()
  })

  it("refetches when the host's role update arrives, not for other config", async () => {
    // v_47: a stub's forwarded role change lands as the host's role
    // broadcast, which the backend re-emits as space.config.changed.
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" />)
    await waitFor(() => expect(r.container.querySelector('.sh-badge--moderator')).toBeTruthy())
    const memberFetches = () =>
      apiGet.mock.calls.filter(c => String(c[0]).endsWith('/members')).length
    const before = memberFetches()
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'renamed' })
    emit('space.config.changed', { space_id: 'sp-other', event_type: 'role_changed' })
    expect(memberFetches()).toBe(before)
    emit('space.config.changed', { space_id: 'sp-1', event_type: 'role_changed' })
    expect(memberFetches()).toBe(before + 1)
  })

  it('a failed load offers Retry instead of spinning forever', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    let fail = true
    apiGet.mockImplementation(async (url: string) => {
      if (url.endsWith('/members')) {
        if (fail) throw new Error(`API 429: ${url}`)
        return [{ user_id: 'u-b', display_name: 'Bob', role: 'member', joined_at: '2026-01-01' }]
      }
      return []
    })
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" />)
    const alert = await r.findByRole('alert')
    expect(alert.textContent).toContain("Couldn't load the members.")
    fail = false
    fireEvent.click(r.getByText('Retry'))
    await waitFor(() => expect(r.getByText('Bob')).toBeTruthy())
  })

  it("offers no role change on the owner's seat a stub mirrors", async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    apiGet.mockImplementation(async (url: string) => url.endsWith('/members')
      ? [{
          user_id: 'u-h', display_name: 'Hannah', role: 'owner', is_owner: true,
          instance_id: 'host-x', joined_at: '2026-01-01',
        }]
      : [])
    const r = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" />)
    await waitFor(() => expect(r.container.querySelector('.sh-badge--owner')).toBeTruthy())
    fireEvent.click(r.getByLabelText('Manage Hannah'))
    await waitFor(() => expect(r.baseElement.querySelector('.sh-member-actions')).toBeTruthy())
    expect(r.baseElement.querySelector('[data-role-option]')).toBeNull()
  })
})

describe('SpaceMemberList — header count and language', () => {
  afterEach(async () => { await setLocale('en') })

  const members = (n: number) => Array.from({ length: n }, (_, i) => ({
    user_id: `u-${i}`, display_name: `Person ${i}`, role: 'member', joined_at: '2026-01-01',
  }))
  const banned = [{ user_id: 'u-x', banned_by: 'u-0', banned_at: '2026-01-01' }]
  const serve = (n: number) => apiGet.mockImplementation(async (url: string) =>
    url.endsWith('/members') ? members(n) : url.endsWith('/bans') ? banned : [])

  it('says "1 member" for one and "3 members" for more', async () => {
    const { SpaceMemberList } = await import('./SpaceMemberList')
    serve(1)
    const one = render(<SpaceMemberList spaceId="sp-1" viewerRole="member" />)
    await waitFor(() => expect(one.container.querySelector('h3')?.textContent).toBe('1 member'))
    one.unmount()
    serve(3)
    const many = render(<SpaceMemberList spaceId="sp-1" viewerRole="member" />)
    await waitFor(() => expect(many.container.querySelector('h3')?.textContent).toBe('3 members'))
  })

  it('renders the header, ban list and chips in German', async () => {
    await setLocale('de')
    const { SpaceMemberList } = await import('./SpaceMemberList')
    serve(1)
    const one = render(<SpaceMemberList spaceId="sp-1" viewerRole="admin" />)
    await waitFor(() => expect(one.container.querySelector('h3')?.textContent).toBe('1 Mitglied'))
    const headings = [...one.container.querySelectorAll('h3')].map(h => h.textContent)
    expect(headings).toEqual(['1 Mitglied', 'Gesperrt'])
    expect(one.getByText('Entsperren')).toBeTruthy()
    expect(one.container.textContent).toContain('Nutzt dein Haushaltsprofil')
    expect(one.getByLabelText('Person 0 verwalten')).toBeTruthy()
    one.unmount()
    serve(4)
    const many = render(<SpaceMemberList spaceId="sp-1" viewerRole="member" />)
    await waitFor(() => expect(many.container.querySelector('h3')?.textContent).toBe('4 Mitglieder'))
  })
})

describe('SpaceMemberList — tapping a member opens their person sheet', () => {
  const rows = [
    { user_id: 'u-m', display_name: 'Mo', role: 'moderator', joined_at: '2026-01-01' },
    {
      user_id: 'ru-chidi', display_name: 'Chidi Okafor', personal_alias: 'Coach',
      role: 'member', joined_at: '2026-01-01', instance_id: 'p-ok',
      household_name: 'Okafor family', picture_url: 'api/s/chidi',
      is_online: true, is_idle: false,
    },
  ]

  it('the row is one button; the hint carries name, alias, picture and household', async () => {
    openPersonSpy.mockReset()
    apiGet.mockImplementation(async (url: string) => url.endsWith('/members') ? rows : [])
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-9" viewerRole="admin" />)
    await waitFor(() => expect(r.getAllByTestId('member-person')).toHaveLength(2))
    const chidi = r.getAllByTestId('member-person')
      .find(b => b.getAttribute('data-person-id') === 'ru-chidi')!
    expect(chidi.tagName).toBe('BUTTON')
    fireEvent.click(chidi)
    expect(openPersonSpy).toHaveBeenCalledWith('ru-chidi', expect.objectContaining({
      person: expect.objectContaining({
        display_name: 'Chidi Okafor',
        personal_alias: 'Coach',
        household: { instance_id: 'p-ok', display_name: 'Okafor family', is_own: false },
        is_online: true,
      }),
      onAliasChanged: expect.any(Function),
    }))
    // Never claims a remote member is reachable by DM — the sheet decides.
    expect(openPersonSpy.mock.calls[0][1].person.is_local).toBeUndefined()
  })

  it('a nickname saved in the sheet patches the row in place', async () => {
    openPersonSpy.mockReset()
    apiGet.mockImplementation(async (url: string) => url.endsWith('/members') ? rows : [])
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-9" viewerRole="member" />)
    await waitFor(() => expect(r.getByText('Coach')).toBeTruthy())
    fireEvent.click(r.getAllByTestId('member-person')[1])
    const { onAliasChanged } = openPersonSpy.mock.calls[0][1]
    onAliasChanged('ru-chidi', 'Chidi (football)')
    await waitFor(() => expect(r.getByText('Chidi (football)')).toBeTruthy())
  })

  it('nickname moved into the sheet; space report and admin ⋯ stay on the row', async () => {
    apiGet.mockImplementation(async (url: string) => url.endsWith('/members') ? rows : [])
    const { SpaceMemberList } = await import('./SpaceMemberList')
    const r = render(<SpaceMemberList spaceId="sp-9" viewerRole="admin" />)
    await waitFor(() => expect(r.getAllByTestId('member-person')).toHaveLength(2))
    expect(r.container.querySelector('.sh-member-rename-btn:not(.sh-member-report-btn)')).toBeNull()
    expect(r.container.querySelector('.sh-member-report-btn')).not.toBeNull()
    expect(r.getByLabelText('Manage Coach')).toBeTruthy()
  })
})
