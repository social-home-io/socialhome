import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render, fireEvent, waitFor, act, cleanup,
} from '@testing-library/preact'

vi.mock('@/api', () => ({
  api: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/store/auth', () => ({
  currentUser: { value: { user_id: 'uid-pascal', display_name: 'Pascal' } },
}))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
const openSpaceInvite = vi.fn()
vi.mock('./SpaceInviteDialog', () => ({ openSpaceInvite: (...a: unknown[]) => openSpaceInvite(...a) }))

const { api } = await import('@/api') as unknown as {
  api: { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> }
}
const { showToast } = await import('./Toast') as unknown as {
  showToast: ReturnType<typeof vi.fn>
}
const { RemoteInviteDialog, openRemoteInviteDialog } = await import('./RemoteInviteDialog')
const { setLocale } = await import('@/i18n/i18n')

const FRIENDS_PAYLOAD = {
  instance: {
    instance_id: 'self-instance',
    display_name: 'Alpha House',
    members: [
      // The viewer (uid-pascal) — must be filtered out of the picker.
      { user_id: 'uid-pascal', display_name: 'Pascal (Alpha)', last_seen_at: null },
      // Wife — present on the same instance; pre-fix she was silently
      // dropped because the dialog only flattened ``households[]``.
      { user_id: 'uid-anna', display_name: 'Anna (Alpha)', last_seen_at: null },
    ],
  },
  households: [
    {
      instance_id: 'beta-instance',
      display_name: 'Beta House',
      status: 'confirmed',
      reachable: true,
      members: [
        {
          user_id: 'uid-bob',
          instance_id: 'beta-instance',
          remote_username: 'bob',
          display_name: 'Bob (Beta)',
          last_seen_at: new Date(Date.now() - 5 * 60_000).toISOString(),
        },
        {
          user_id: 'uid-carol',
          instance_id: 'beta-instance',
          remote_username: 'carol',
          display_name: 'Carol (Beta)',
          last_seen_at: null,
        },
      ],
    },
    {
      instance_id: 'gamma-instance',
      display_name: 'Gamma House',
      status: 'confirmed',
      reachable: false,
      members: [
        {
          user_id: 'uid-dave',
          instance_id: 'gamma-instance',
          remote_username: 'dave',
          display_name: 'Dave (Gamma)',
          last_seen_at: null,
        },
      ],
    },
    {
      // Pending household — should NOT appear in the picker.
      instance_id: 'pending-instance',
      display_name: 'Pending',
      status: 'pending',
      reachable: false,
      members: [
        { user_id: 'uid-x', instance_id: 'pending-instance',
          display_name: 'Pending User', last_seen_at: null },
      ],
    },
  ],
}

beforeEach(() => {
  api.get.mockReset()
  api.post.mockReset()
  showToast.mockReset()
})

afterEach(() => {
  cleanup()
})

/** ``GET /api/friends`` → ``payload``; the space's members → ``members``. */
function mockGets(payload: unknown, members: unknown = []) {
  api.get.mockImplementation((url: string) => (
    url === '/api/friends' ? Promise.resolve(payload) : Promise.resolve(members)
  ))
}

async function renderAndOpen(members: unknown = []) {
  mockGets(FRIENDS_PAYLOAD, members)
  const result = render(<RemoteInviteDialog />)
  await act(async () => { openRemoteInviteDialog('space-uuid-1') })
  await waitFor(() => {
    expect(result.container.querySelector('[data-testid="remote-invite-search"]'))
      .not.toBeNull()
  })
  return result
}

describe('RemoteInviteDialog — the one Invite door', () => {
  it('"Share an invite link" hands over to the link dialog with the opener\'s role', async () => {
    openSpaceInvite.mockReset()
    mockGets(FRIENDS_PAYLOAD)
    const r = render(<RemoteInviteDialog />)
    await act(async () => { openRemoteInviteDialog('space-uuid-1', 'admin') })
    const link = await waitFor(() => r.getByRole('button', { name: 'Share an invite link' }))
    expect(r.getByText(/Not on the list\?/)).toBeTruthy()
    fireEvent.click(link)
    expect(openSpaceInvite).toHaveBeenCalledWith('space-uuid-1', null, 'admin')
    expect(r.queryByRole('button', { name: 'Share an invite link' })).toBeNull()
  })

  it('with nobody to pick, the link is the main action', async () => {
    openSpaceInvite.mockReset()
    mockGets({ instance: { instance_id: 'i', display_name: 'H', members: [] }, households: [] })
    const r = render(<RemoteInviteDialog />)
    await act(async () => { openRemoteInviteDialog('space-uuid-1', 'owner') })
    fireEvent.click(await waitFor(() => r.getByRole('button', { name: 'Share an invite link' })))
    expect(openSpaceInvite).toHaveBeenCalledWith('space-uuid-1', null, 'owner')
  })
})

describe('RemoteInviteDialog', () => {
  it('flattens both local + remote members, viewer themselves filtered', async () => {
    const { container } = await renderAndOpen()
    expect(api.get).toHaveBeenCalledWith('/api/friends')
    // 1 local (Anna; the viewer Pascal is filtered out) + 3 confirmed
    // remote members across two households; the pending household's
    // user must NOT appear.
    const rows = container.querySelectorAll('.sh-remote-invite-row')
    expect(rows.length).toBe(4)
    const names = [...rows].map(r =>
      r.querySelector('.sh-remote-invite-row__name')?.textContent)
    // Local first (most common pick), then remote in source order.
    expect(names).toEqual([
      'Anna (Alpha)',
      'Bob (Beta)', 'Carol (Beta)', 'Dave (Gamma)',
    ])
  })

  it('filters the list by name and by household', async () => {
    const { container } = await renderAndOpen()
    const search = container.querySelector('[data-testid="remote-invite-search"]') as HTMLInputElement
    // Name filter.
    await act(async () => {
      fireEvent.input(search, { target: { value: 'carol' } })
    })
    expect(container.querySelectorAll('.sh-remote-invite-row').length).toBe(1)
    // Household filter.
    await act(async () => {
      fireEvent.input(search, { target: { value: 'gamma' } })
    })
    expect(container.querySelectorAll('.sh-remote-invite-row').length).toBe(1)
    // Local-household members surface under the household name too.
    await act(async () => {
      fireEvent.input(search, { target: { value: 'anna' } })
    })
    expect(container.querySelectorAll('.sh-remote-invite-row').length).toBe(1)
  })

  it('REMOTE pick → /remote-invites + "Send invite" button', async () => {
    api.post.mockResolvedValueOnce({})
    const { container, getByText } = await renderAndOpen()
    const bobRow = container.querySelector(
      '[data-testid="remote-invite-row-uid-bob"]',
    ) as HTMLButtonElement
    await act(async () => { fireEvent.click(bobRow) })
    await act(async () => { fireEvent.click(getByText('Send invite')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/spaces/space-uuid-1/remote-invites',
        {
          invitee_instance_id: 'beta-instance',
          invitee_user_id: 'uid-bob',
        },
      )
      expect(showToast).toHaveBeenCalledWith(
        expect.stringContaining('Invite sent'), 'success',
      )
    })
  })

  it('LOCAL pick → an invite too: /members + "Send invite", "Invite sent"', async () => {
    // Someone in your own household is invited like everyone else: the
    // server makes a pending invite they accept (``invite_local_user``),
    // so the dialog never says "Add" / "Added".
    api.post.mockResolvedValueOnce({ status: 'pending' })
    const { container, getByText, queryByText } = await renderAndOpen()
    const annaRow = container.querySelector(
      '[data-testid="remote-invite-row-uid-anna"]',
    ) as HTMLButtonElement
    expect(annaRow).not.toBeNull()
    await act(async () => { fireEvent.click(annaRow) })
    expect(queryByText('Add')).toBeNull()
    await act(async () => { fireEvent.click(getByText('Send invite')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/spaces/space-uuid-1/members',
        { user_id: 'uid-anna' },
      )
      expect(showToast).toHaveBeenCalledWith('Invite sent to Anna (Alpha)', 'success')
    })
  })

  it('picks several people and sends every invite at once', async () => {
    api.post.mockResolvedValue({})
    const r = await renderAndOpen()
    const row = (id: string) => r.container.querySelector(`[data-testid="remote-invite-row-${id}"]`) as HTMLButtonElement
    await act(async () => { fireEvent.click(row('uid-anna')) })
    await act(async () => { fireEvent.click(row('uid-bob')) })
    await act(async () => { fireEvent.click(row('uid-dave')) })
    // Tapping a picked row again unpicks it.
    await act(async () => { fireEvent.click(row('uid-dave')) })
    expect(row('uid-anna').getAttribute('aria-selected')).toBe('true')
    expect(row('uid-dave').getAttribute('aria-selected')).toBe('false')
    expect(r.getByRole('listbox').getAttribute('aria-multiselectable')).toBe('true')
    await act(async () => { fireEvent.click(r.getByText('Send 2 invites')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith('/api/spaces/space-uuid-1/members', { user_id: 'uid-anna' })
      expect(api.post).toHaveBeenCalledWith('/api/spaces/space-uuid-1/remote-invites', {
        invitee_instance_id: 'beta-instance', invitee_user_id: 'uid-bob',
      })
      expect(showToast).toHaveBeenCalledWith('2 invites sent', 'success')
    })
    expect(api.post).toHaveBeenCalledTimes(2)
    // All sent: the dialog closes.
    await waitFor(() => expect(r.queryByRole('listbox')).toBeNull())
  })

  it('when some invites fail, those people stay picked and are named', async () => {
    api.post.mockImplementation((url: string) => (
      url.endsWith('/remote-invites') ? Promise.reject(new Error('unreachable')) : Promise.resolve({})
    ))
    const r = await renderAndOpen()
    const row = (id: string) => r.container.querySelector(`[data-testid="remote-invite-row-${id}"]`) as HTMLButtonElement
    for (const id of ['uid-anna', 'uid-bob', 'uid-carol']) {
      await act(async () => { fireEvent.click(row(id)) })
    }
    await act(async () => { fireEvent.click(r.getByText('Send 3 invites')) })
    await waitFor(() => {
      expect(r.getByText("Couldn't send to Bob (Beta) and Carol (Beta). Try again.")).toBeTruthy()
    })
    // Anna's went out: a toast, and she is no longer offered.
    expect(showToast).toHaveBeenCalledWith('Invite sent to Anna (Alpha)', 'success')
    expect(row('uid-anna')).toBeNull()
    expect(row('uid-bob').getAttribute('aria-selected')).toBe('true')
    expect(row('uid-carol').getAttribute('aria-selected')).toBe('true')
    expect(r.getByText('Send 2 invites')).toBeTruthy()
  })

  it('the send button waits for a pick', async () => {
    const r = await renderAndOpen()
    expect((r.getByText('Send invite').closest('button') as HTMLButtonElement).disabled).toBe(true)
  })

  it('people already in the space are not offered, local or remote', async () => {
    const { container } = await renderAndOpen([
      // A local member row has no ``instance_id``; a remote one has.
      { user_id: 'uid-pascal', role: 'owner' },
      { user_id: 'uid-anna', role: 'member' },
      { user_id: 'uid-bob', instance_id: 'beta-instance', role: 'member' },
    ])
    expect(api.get).toHaveBeenCalledWith('/api/spaces/space-uuid-1/members')
    const names = [...container.querySelectorAll('.sh-remote-invite-row__name')]
      .map(n => n.textContent)
    expect(names).toEqual(['Carol (Beta)', 'Dave (Gamma)'])
  })

  it('the same user id in another household is still offered', async () => {
    const { container } = await renderAndOpen([
      { user_id: 'uid-carol', instance_id: 'other-instance', role: 'member' },
    ])
    expect(container.querySelector('[data-testid="remote-invite-row-uid-carol"]')).not.toBeNull()
  })

  it('when the member list fails to load, everyone is still offered', async () => {
    api.get.mockImplementation((url: string) => (
      url === '/api/friends' ? Promise.resolve(FRIENDS_PAYLOAD) : Promise.reject(new Error('offline'))
    ))
    const r = render(<RemoteInviteDialog />)
    await act(async () => { openRemoteInviteDialog('space-uuid-1') })
    await waitFor(() => expect(r.container.querySelectorAll('.sh-remote-invite-row').length).toBe(4))
  })

  it('shows last-seen freshness per row', async () => {
    const { container } = await renderAndOpen()
    const bobMeta = container
      .querySelector('[data-testid="remote-invite-row-uid-bob"]')
      ?.querySelector('.sh-remote-invite-row__meta')?.textContent
    // Bob was last seen 5 minutes ago.
    expect(bobMeta).toContain('Beta House')
    expect(bobMeta).toContain('5 min ago')
    const carolMeta = container
      .querySelector('[data-testid="remote-invite-row-uid-carol"]')
      ?.querySelector('.sh-remote-invite-row__meta')?.textContent
    expect(carolMeta).toContain('never seen')
  })

  it('renders the no-people empty state when there is nobody to add', async () => {
    mockGets({
      instance: {
        instance_id: 'self-instance',
        display_name: 'Alpha House',
        // Only the viewer is here — they get filtered → empty list.
        members: [
          { user_id: 'uid-pascal', display_name: 'Pascal (Alpha)', last_seen_at: null },
        ],
      },
      households: [],
    })
    const result = render(<RemoteInviteDialog />)
    await act(async () => { openRemoteInviteDialog('space-uuid-empty') })
    await waitFor(() => {
      expect(result.container.textContent)
        .toContain('nobody else to add yet')
    })
  })
})

describe('RemoteInviteDialog in German', () => {
  it('renders the title, search and last-seen line in German', async () => {
    await setLocale('de')
    try {
      const { container, baseElement } = await renderAndOpen()
      expect(baseElement.textContent).toContain('Leute in diesen Space einladen')
      expect(container.querySelector('[data-testid="remote-invite-search"]')
        ?.getAttribute('placeholder')).toBe('Name oder Haushalt eingeben …')
      const bobMeta = container
        .querySelector('[data-testid="remote-invite-row-uid-bob"]')
        ?.querySelector('.sh-remote-invite-row__meta')?.textContent
      expect(bobMeta).toBe('Beta House · zuletzt gesehen vor 5 Min.')
      const carolMeta = container
        .querySelector('[data-testid="remote-invite-row-uid-carol"]')
        ?.querySelector('.sh-remote-invite-row__meta')?.textContent
      expect(carolMeta).toContain('noch nie gesehen')
    } finally {
      await setLocale('en')
    }
  })
})
