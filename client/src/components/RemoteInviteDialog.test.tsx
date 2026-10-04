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

async function renderAndOpen() {
  api.get.mockResolvedValueOnce(FRIENDS_PAYLOAD)
  const result = render(<RemoteInviteDialog />)
  await act(async () => { openRemoteInviteDialog('space-uuid-1') })
  await waitFor(() => {
    expect(result.container.querySelector('[data-testid="remote-invite-search"]'))
      .not.toBeNull()
  })
  return result
}

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

  it('LOCAL pick → /members add + "Add" button', async () => {
    // Regression for Pascal's bug: picking a same-household member
    // (his wife) must seat her immediately via /members, not queue a
    // cross-household federated invite.
    api.post.mockResolvedValueOnce({})
    const { container, getByText } = await renderAndOpen()
    const annaRow = container.querySelector(
      '[data-testid="remote-invite-row-uid-anna"]',
    ) as HTMLButtonElement
    expect(annaRow).not.toBeNull()
    await act(async () => { fireEvent.click(annaRow) })
    // Button label flips to "Add" when the pick is local.
    await act(async () => { fireEvent.click(getByText('Add')) })
    await waitFor(() => {
      expect(api.post).toHaveBeenCalledWith(
        '/api/spaces/space-uuid-1/members',
        { user_id: 'uid-anna' },
      )
      expect(showToast).toHaveBeenCalledWith(
        expect.stringContaining('Added Anna'), 'success',
      )
    })
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
    api.get.mockResolvedValueOnce({
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
      expect(baseElement.textContent).toContain('Jemanden zu diesem Raum hinzufügen')
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
