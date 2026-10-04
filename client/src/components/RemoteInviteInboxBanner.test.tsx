/* eslint-disable @typescript-eslint/no-explicit-any */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { render, waitFor, fireEvent, cleanup } from '@testing-library/preact'
import { setLocale } from '@/i18n/i18n'

// Mock the api module before importing the component so the
// component's ``api.get`` / ``api.post`` resolve against vi.fn()s
// instead of hitting a real backend.
vi.mock('@/api', () => {
  return {
    api: {
      get: vi.fn(),
      post: vi.fn(),
    },
    ApiError: class extends Error {
      status: number
      constructor(msg: string, status: number) {
        super(msg)
        this.status = status
      }
    },
  }
})

vi.mock('./Toast', () => ({
  showToast: vi.fn(),
}))

import { api } from '@/api'
import { showToast } from './Toast'
import { RemoteInviteInboxBanner } from './RemoteInviteInboxBanner'

const apiGet = api.get as ReturnType<typeof vi.fn>
const apiPost = api.post as ReturnType<typeof vi.fn>

const INVITE = {
  invite_token: 'tok-xyz',
  space_id: 'space-1',
  inviter_user_id: 'uid-alice',
  inviter_instance_id: 'h65qjeaa4b2xcxwdoxxrdiaxvqqe3toa',
  space_display_hint: "Alice's space",
  expires_at: null,
  created_at: null,
}

const FRIENDS_WITH_ALICE = {
  households: [
    {
      instance_id: 'h65qjeaa4b2xcxwdoxxrdiaxvqqe3toa',
      display_name: 'Alpha House',
      local_alias: null,
      federated_display_name: 'Alpha House',
    },
  ],
}

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  ;(showToast as ReturnType<typeof vi.fn>).mockReset()
  cleanup()
})

describe('RemoteInviteInboxBanner', () => {
  it('renders nothing when no pending invites', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/remote_invites') return Promise.resolve([])
      if (url === '/api/friends') return Promise.resolve({ households: [] })
      return Promise.reject(new Error('unknown url'))
    })
    const { container } = render(<RemoteInviteInboxBanner />)
    // Wait for the load() effect to settle.
    await waitFor(() => {
      expect(container.querySelector('.sh-remote-invite-banner')).toBeNull()
    })
  })

  it('looks up the inviter household by id and shows its display name', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/remote_invites') return Promise.resolve([INVITE])
      if (url === '/api/friends') return Promise.resolve(FRIENDS_WITH_ALICE)
      return Promise.reject(new Error('unknown url'))
    })
    const { container } = render(<RemoteInviteInboxBanner />)
    await waitFor(() => {
      const banner = container.querySelector('.sh-remote-invite-banner')
      expect(banner).not.toBeNull()
      expect(banner?.textContent ?? '').toContain('Alpha House')
    })
    // The raw instance_id hex must not appear.
    const banner = container.querySelector('.sh-remote-invite-banner')
    expect(banner?.textContent ?? '').not.toContain('h65qjeaa4b2x')
  })

  it('falls back to a short hash when household lookup misses', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/remote_invites') return Promise.resolve([INVITE])
      if (url === '/api/friends') return Promise.resolve({ households: [] })
      return Promise.reject(new Error('unknown url'))
    })
    const { container } = render(<RemoteInviteInboxBanner />)
    await waitFor(() => {
      const banner = container.querySelector('.sh-remote-invite-banner')
      expect(banner).not.toBeNull()
      // Short hash form: 8 chars + ellipsis.
      expect(banner?.textContent ?? '').toContain('h65qjeaa…')
    })
  })

  it('decline requires a confirm click before firing the API', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/remote_invites') return Promise.resolve([INVITE])
      if (url === '/api/friends') return Promise.resolve(FRIENDS_WITH_ALICE)
      return Promise.reject(new Error('unknown url'))
    })
    const { container } = render(<RemoteInviteInboxBanner />)
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-decline"]')).not.toBeNull()
    })

    const decline = container.querySelector(
      '[data-testid="invite-decline"]',
    ) as HTMLButtonElement
    expect(decline.textContent).toContain('Decline')
    fireEvent.click(decline)
    // First click — must NOT have fired the POST yet, and the label
    // flips to "Confirm decline".
    expect(apiPost).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(decline.textContent).toContain('Confirm decline')
    })

    fireEvent.click(decline)
    // Second click within the confirm window — NOW the POST fires.
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        '/api/remote_invites/tok-xyz/decline',
        {},
      )
    })
  })

  it('disables both buttons while an Accept request is in flight', async () => {
    let resolveAccept: () => void = () => {}
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/remote_invites') return Promise.resolve([INVITE])
      if (url === '/api/friends') return Promise.resolve(FRIENDS_WITH_ALICE)
      return Promise.reject(new Error('unknown url'))
    })
    apiPost.mockImplementation(() => new Promise<void>((res) => {
      resolveAccept = res
    }))
    const { container } = render(<RemoteInviteInboxBanner />)
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-accept"]')).not.toBeNull()
    })

    const accept = container.querySelector(
      '[data-testid="invite-accept"]',
    ) as HTMLButtonElement
    const decline = container.querySelector(
      '[data-testid="invite-decline"]',
    ) as HTMLButtonElement

    fireEvent.click(accept)
    // Both buttons disabled while accept is pending. (No mid-Accept
    // decline race; no double-Accept queue.)
    await waitFor(() => {
      expect(accept.disabled).toBe(true)
      expect(decline.disabled).toBe(true)
    })

    // Resolving the API call clears the in-flight state.
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/remote_invites') return Promise.resolve([])
      if (url === '/api/friends') return Promise.resolve(FRIENDS_WITH_ALICE)
      return Promise.reject(new Error('unknown url'))
    })
    resolveAccept()
    await waitFor(() => {
      // After accept resolves, the invite is gone (load() re-fetches
      // and gets the empty list); banner unmounts.
      expect(container.querySelector('.sh-remote-invite-banner')).toBeNull()
    })
  })

  it('also renders pending local-household invitations', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/remote_invites') return Promise.resolve([])
      if (url === '/api/local_invites') {
        return Promise.resolve([
          {
            invitation_id: 'inv-1',
            space_id: 'space-fam',
            invited_by: 'uid-admin',
            expires_at: null,
            created_at: '2026-05-24T12:00:00Z',
          },
        ])
      }
      if (url === '/api/friends') return Promise.resolve({ households: [] })
      if (url === '/api/users') return Promise.resolve([])
      return Promise.reject(new Error(`unknown url: ${url}`))
    })
    const { container } = render(<RemoteInviteInboxBanner />)
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="local-invite-accept"]'),
      ).not.toBeNull()
    })
    expect(container.textContent).toContain('space-fam')
  })

  it('local-invite accept POSTs to /api/local_invites/{id}/accept', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/remote_invites') return Promise.resolve([])
      if (url === '/api/local_invites') {
        return Promise.resolve([
          {
            invitation_id: 'inv-2',
            space_id: 'sp',
            invited_by: 'uid-admin',
            expires_at: null,
            created_at: '2026-05-24T12:00:00Z',
          },
        ])
      }
      if (url === '/api/friends') return Promise.resolve({ households: [] })
      if (url === '/api/users') return Promise.resolve([])
      return Promise.reject(new Error(`unknown url: ${url}`))
    })
    apiPost.mockResolvedValue({})
    const { container } = render(<RemoteInviteInboxBanner />)
    await waitFor(() => {
      expect(
        container.querySelector('[data-testid="local-invite-accept"]'),
      ).not.toBeNull()
    })
    const btn = container.querySelector(
      '[data-testid="local-invite-accept"]',
    ) as HTMLButtonElement
    fireEvent.click(btn)
    await waitFor(() => {
      expect(apiPost).toHaveBeenCalledWith(
        '/api/local_invites/inv-2/accept',
        {},
      )
    })
  })
})

describe('RemoteInviteInboxBanner in German', () => {
  it('shows the heading, inviter and buttons in German', async () => {
    await setLocale('de')
    try {
      apiGet.mockImplementation((url: string) => {
        if (url === '/api/remote_invites') return Promise.resolve([INVITE])
        if (url === '/api/friends') return Promise.resolve(FRIENDS_WITH_ALICE)
        return Promise.resolve([])
      })
      const { container, getByText } = render(<RemoteInviteInboxBanner />)
      await waitFor(() => {
        expect(container.querySelector('h2')?.textContent)
          .toBe('📬 Offene Einladungen aus anderen Haushalten')
      })
      const row = container.querySelector('.sh-remote-invite-banner__row')!
      expect(row.textContent).toContain('von Alpha House')
      expect([...row.querySelectorAll('strong')].map(s => s.textContent)).toContain('Alpha House')
      expect(getByText('Annehmen')).toBeTruthy()
      fireEvent.click(getByText('Ablehnen'))
      expect(getByText('Wirklich ablehnen')).toBeTruthy()
    } finally {
      await setLocale('en')
    }
  })
})
