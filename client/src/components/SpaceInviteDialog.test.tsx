import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import {
  render, fireEvent, waitFor, act, cleanup,
} from '@testing-library/preact'
import { decodeInviteCode } from '@/lib/spaceInviteCode'

vi.mock('@/api', () => {
  class ApiError extends Error {
    public readonly detail: string | null
    constructor(
      public readonly status: number,
      detail: string | null = null,
      public readonly code: string | null = null,
    ) {
      super(detail ?? `API ${status}`)
      this.detail = detail
    }
  }
  return {
    api: { get: vi.fn(), post: vi.fn(), delete: vi.fn() },
    ApiError,
  }
})
vi.mock('qrcode', () => ({
  default: { toDataURL: vi.fn(async (data: string) => `data:fake;${data}`) },
}))
vi.mock('./Toast', () => ({ showToast: vi.fn() }))
vi.mock('./confirm', () => ({ confirmDialog: vi.fn() }))

// The dialog reads our own instance id from the SPA's
// :class:`instanceConfig` store and stamps it into the code.
const OUR_INSTANCE_ID = 'aaaabbbbccccddddeeeeffff00001111'
vi.mock('@/store/instance', () => ({
  instanceConfig: {
    value: {
      mode: 'standalone',
      instance_name: 'Test home',
      instance_id: OUR_INSTANCE_ID,
      capabilities: [],
      setup_required: false,
    },
  },
  loadInstanceConfig: vi.fn(),
}))

const { api, ApiError } = await import('@/api') as unknown as {
  api: {
    get: ReturnType<typeof vi.fn>
    post: ReturnType<typeof vi.fn>
    delete: ReturnType<typeof vi.fn>
  }
  ApiError: new (status: number, detail?: string | null, code?: string | null) => Error
}
const { confirmDialog } = await import('./confirm') as unknown as {
  confirmDialog: ReturnType<typeof vi.fn>
}
const { showToast } = await import('./Toast') as unknown as {
  showToast: ReturnType<typeof vi.fn>
}
const { openSpaceInvite, SpaceInviteDialog } = await import('./SpaceInviteDialog')
const { setLocale } = await import('@/i18n/i18n')

type Row = Record<string, unknown>

/** Wire up ``api.get`` for the dialog's four reads: space detail, links
 *  list, connection servers, and the member roster it resolves minter
 *  names against. */
function mockReads(
  { tokens = [], servers = [], members = [], linksFail = false, space = {} }:
  {
    tokens?: Row[]; servers?: Row[]; members?: Row[]; linksFail?: boolean
    space?: Row
  } = {},
) {
  api.get.mockImplementation((url: string) => {
    if (url.endsWith('/invite-tokens')) {
      return linksFail
        ? Promise.reject(new Error('boom'))
        : Promise.resolve({ tokens })
    }
    if (url === '/api/gfs/connections') return Promise.resolve(servers)
    if (url.endsWith('/members')) return Promise.resolve(members)
    return Promise.resolve({ name: 'Fetched space name', ...space })
  })
}

function makeRow(over: Row = {}): Row {
  return {
    token: 'tok-1',
    role: 'member',
    uses_remaining: 3,
    expires_at: new Date(Date.now() + 7 * 86_400_000).toISOString(),
    created_by: 'pascal',
    created_at: new Date().toISOString(),
    code: null,
    gfs: null,
    ...over,
  }
}

beforeEach(() => {
  api.get.mockReset()
  api.post.mockReset()
  api.delete.mockReset()
  confirmDialog.mockReset()
  showToast.mockReset()
  mockReads()
})

afterEach(() => {
  cleanup()
})

async function openDialog(
  { hint = 'Pascal\'s family', role }:
  { hint?: string | null; role?: 'owner' | 'admin' | 'moderator' | 'member' } = {},
) {
  const result = render(<SpaceInviteDialog />)
  await act(async () => {
    openSpaceInvite('space-' + Math.random().toString(36).slice(2, 10), hint, role)
  })
  await waitFor(() => {
    expect(result.container.querySelector('.sh-invite-dialog')).not.toBeNull()
  })
  return result
}

async function generate(
  result: Awaited<ReturnType<typeof openDialog>>,
  row: Row = makeRow(),
) {
  api.post.mockResolvedValueOnce(row)
  await act(async () => {
    fireEvent.click(result.getByText('Create invite link'))
  })
  await waitFor(() => {
    expect(result.container.querySelector('[data-testid="invite-code"]'))
      .not.toBeNull()
  })
  return result
}

describe('SpaceInviteDialog — role picker', () => {
  it('hides the Admin option from a non-owner', async () => {
    const { container } = await openDialog({ role: 'admin' })
    expect(container.querySelector('[data-testid="invite-role-member"]'))
      .not.toBeNull()
    expect(container.querySelector('[data-testid="invite-role-subscriber"]'))
      .not.toBeNull()
    expect(container.querySelector('[data-testid="invite-role-admin"]'))
      .toBeNull()
  })

  it('hides the Admin option when the opener\'s role is unknown', async () => {
    const { container } = await openDialog({ role: undefined })
    expect(container.querySelector('[data-testid="invite-role-admin"]'))
      .toBeNull()
  })

  it('offers Admin to the owner and mints with the picked role', async () => {
    const result = await openDialog({ role: 'owner' })
    const adminRadio = result.container
      .querySelector('[data-testid="invite-role-admin"]')!
    expect(adminRadio).not.toBeNull()
    await act(async () => { fireEvent.click(adminRadio) })
    await generate(result, makeRow({ role: 'admin' }))
    expect(api.post).toHaveBeenCalledWith(
      expect.stringContaining('/invite-tokens'),
      expect.objectContaining({ role: 'admin' }),
    )
  })

  it('defaults to member, 1 use and a 7-day expiry', async () => {
    const result = await openDialog({ role: 'owner' })
    await generate(result)
    expect(api.post).toHaveBeenCalledWith(
      expect.stringContaining('/invite-tokens'),
      expect.objectContaining({ role: 'member', uses: 1, ttl_seconds: 604_800 }),
    )
  })

  it('caps uses at 100 and floors them at 1', async () => {
    const result = await openDialog()
    const input = result.container
      .querySelector('[data-testid="invite-uses"]') as HTMLInputElement
    await act(async () => {
      fireEvent.input(input, { target: { value: '5000' } })
    })
    await generate(result)
    expect(api.post).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ uses: 100 }),
    )
  })

  it('sends ttl_seconds 0 and shows a hint for "never"', async () => {
    const result = await openDialog()
    const select = result.container
      .querySelector('[data-testid="invite-expiry"]') as HTMLSelectElement
    await act(async () => {
      fireEvent.change(select, { target: { value: 'never' } })
    })
    expect(result.container.querySelector('[data-testid="invite-never-hint"]'))
      .not.toBeNull()
    await generate(result, makeRow({ expires_at: null }))
    expect(api.post).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ ttl_seconds: 0 }),
    )
  })
})

describe('SpaceInviteDialog — the create button stays reachable', () => {
  // With the publish toggle on, the form outgrows a laptop-height modal.
  // The primary CTA lives in the sticky ``sh-invite-dialog__submit``
  // footer row (CSS in app.css) so it stays visible while the fields
  // scroll behind it — the plain mid-dialog action rows stay static.
  it('renders "Create invite link" inside the sticky submit row', async () => {
    mockReads({
      servers: [{ id: 'gfs-1', display_name: 'Relay One', status: 'active' }],
    })
    const result = await openDialog()
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-publish-toggle"]'))
        .not.toBeNull()
    })
    await act(async () => {
      fireEvent.click(result.container
        .querySelector('[data-testid="invite-publish-toggle"]')!)
    })
    const row = result.container
      .querySelector('[data-testid="invite-submit-row"]') as HTMLElement
    expect(row).not.toBeNull()
    expect(row.classList.contains('sh-form-actions')).toBe(true)
    expect(row.classList.contains('sh-invite-dialog__submit')).toBe(true)
    expect(row.contains(result.getByText('Create invite link'))).toBe(true)
  })

  it('keeps the post-create action rows out of the sticky footer', async () => {
    const result = await openDialog()
    await generate(result)
    expect(result.container.querySelector('.sh-invite-dialog__submit'))
      .toBeNull()
  })
})

describe('SpaceInviteDialog — publishing to a GFS', () => {
  it('hides the publish toggle when the household has no GFS', async () => {
    const { container } = await openDialog()
    expect(container.querySelector('[data-testid="invite-publish-toggle"]'))
      .toBeNull()
  })

  it('offers the toggle and sends publish_to_gfs when switched on', async () => {
    mockReads({
      servers: [{ id: 'gfs-1', display_name: 'Relay One', status: 'active' }],
    })
    const result = await openDialog()
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-publish-toggle"]'))
        .not.toBeNull()
    })
    const toggle = result.container
      .querySelector('[data-testid="invite-publish-toggle"]') as HTMLInputElement
    await act(async () => { fireEvent.click(toggle) })
    await generate(result, makeRow({
      gfs: {
        gfs_id: 'gfs-1',
        gfs_token: 'gt1',
        url: 'https://relay.example.org/join/gt1',
      },
    }))
    expect(api.post).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ publish_to_gfs: 'gfs-1' }),
    )
  })

  it('skips servers that are not active', async () => {
    mockReads({
      servers: [{ id: 'gfs-1', display_name: 'Pending One', status: 'pending' }],
    })
    const { container } = await openDialog()
    expect(container.querySelector('[data-testid="invite-publish-toggle"]'))
      .toBeNull()
  })

  it('renders the published URL with the exact share sentence', async () => {
    mockReads({
      servers: [{ id: 'gfs-1', display_name: 'Relay One', status: 'active' }],
    })
    const result = await openDialog()
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-publish-toggle"]'))
        .not.toBeNull()
    })
    await act(async () => {
      fireEvent.click(result.container
        .querySelector('[data-testid="invite-publish-toggle"]')!)
    })
    await generate(result, makeRow({
      gfs: {
        gfs_id: 'gfs-1',
        gfs_token: 'gt1',
        url: 'https://relay.example.org/join/gt1',
      },
    }))
    expect(result.container.querySelector('[data-testid="invite-link-url"]')!
      .textContent).toBe('https://relay.example.org/join/gt1')
    expect(result.container.textContent).toContain(
      'Anyone with this link sees the space name and can ask for the code.'
      + ' They join from their own Social Home.',
    )
  })

  it('surfaces the 422 detail against the picked server', async () => {
    mockReads({
      servers: [
        { id: 'gfs-1', display_name: 'Relay One', status: 'active' },
        { id: 'gfs-2', display_name: 'Relay Two', status: 'active' },
      ],
    })
    const result = await openDialog()
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-publish-toggle"]'))
        .not.toBeNull()
    })
    await act(async () => {
      fireEvent.click(result.container
        .querySelector('[data-testid="invite-publish-toggle"]')!)
    })
    api.post.mockRejectedValueOnce(
      new ApiError(422, "This GFS can't pass on invites yet."),
    )
    await act(async () => {
      fireEvent.click(result.getByText('Create invite link'))
    })
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-publish-blocked"]'))
        .not.toBeNull()
    })
    expect(result.container
      .querySelector('[data-testid="invite-publish-blocked"]')!.textContent)
      .toContain("can't pass on invites yet")
    // …and the option itself is disabled so a second attempt can't
    // repeat the same failure.
    const picker = result.container
      .querySelector('[data-testid="invite-publish-picker"]') as HTMLSelectElement
    const blocked = Array.from(picker.querySelectorAll('option'))
      .find(o => o.value === 'gfs-1')!
    expect(blocked.disabled).toBe(true)
  })
})

describe('SpaceInviteDialog — the code artifact', () => {
  it('does NOT render an HTTPS link artifact for an unpublished link', async () => {
    const result = await openDialog()
    await generate(result)
    expect(result.container.querySelector('[data-testid="invite-link-url"]'))
      .toBeNull()
  })

  it('emits a code that decodes back to the token + issuer instance id', async () => {
    const result = await openDialog()
    await generate(result, makeRow({ token: 'tok-xyz' }))
    const code = result.container
      .querySelector('[data-testid="invite-code"]')!.textContent!
    expect(code.startsWith('socialhome://invite#')).toBe(true)
    const decoded = decodeInviteCode(code)!
    expect(decoded.token).toBe('tok-xyz')
    expect(decoded.space_display_hint).toBe('Pascal\'s family')
    expect(decoded.issuer_instance_id).toBe(OUR_INSTANCE_ID)
  })

  it('populates via_gfs with the GFS BASE url when published', async () => {
    const result = await openDialog()
    await generate(result, makeRow({
      gfs: {
        gfs_id: 'gfs-1',
        gfs_token: 'gt1',
        url: 'https://relay.example.org/join/gt1',
      },
    }))
    const decoded = decodeInviteCode(result.container
      .querySelector('[data-testid="invite-code"]')!.textContent!)!
    expect(decoded.via_gfs?.gfs_url).toBe('https://relay.example.org')
  })

  it('prefers the backend-built code when the response carries one', async () => {
    const serverCode = 'socialhome://invite#eyJ0b2tlbiI6ICJzZXJ2ZXItc2lkZSJ9'
    const result = await openDialog()
    await generate(result, makeRow({ code: serverCode }))
    expect(result.container.querySelector('[data-testid="invite-code"]')!
      .textContent).toBe(serverCode)
  })

  it('fetches the space name when the caller does not supply a hint', async () => {
    const result = await openDialog({ hint: null })
    await waitFor(() => { expect(api.get).toHaveBeenCalled() })
    await generate(result)
    const decoded = decodeInviteCode(result.container
      .querySelector('[data-testid="invite-code"]')!.textContent!)!
    expect(decoded.space_display_hint).toBe('Fetched space name')
  })

  it('"Make another" wipes the artifacts and returns to the form', async () => {
    const result = await openDialog()
    await generate(result)
    await act(async () => { fireEvent.click(result.getByText('Make another')) })
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-code"]'))
        .toBeNull()
    })
    expect(result.getByText('Create invite link')).toBeTruthy()
  })

  it('renders a QR placeholder or img for the invite code', async () => {
    const result = await openDialog()
    await generate(result)
    expect(result.container.querySelector('.sh-qr-skeleton, .sh-qr-code'))
      .not.toBeNull()
  })
})

describe('SpaceInviteDialog — the links list', () => {
  it('shows an empty state when nothing is live', async () => {
    const { container } = await openDialog()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-links-empty"]'))
        .not.toBeNull()
    })
    expect(container.querySelector('[data-testid="invite-links-empty"]')!
      .textContent).toBe('No active links.')
  })

  it('renders one row per live link with uses, expiry and author', async () => {
    mockReads({
      tokens: [
        makeRow({ token: 't1', uses_remaining: 2, created_by: 'pascal' }),
        makeRow({ token: 't2', role: 'subscriber', uses_remaining: 1 }),
      ],
    })
    const { container } = await openDialog()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-link-row-t1"]'))
        .not.toBeNull()
    })
    const row = container.querySelector('[data-testid="invite-link-row-t1"]')!
    expect(row.textContent).toContain('2 uses left')
    expect(row.textContent).toContain('by pascal')
    // A link minted with a 7-day life reads as seven days, not six —
    // the sentence must not contradict the picker that made it.
    expect(row.textContent).toContain('expires in 7 days')
    expect(container.querySelector('[data-testid="invite-link-row-t2"]')!
      .textContent).toContain('Follower')
  })

  it('counts uses against the size the link was minted with', async () => {
    mockReads({
      tokens: [makeRow({ token: 't1', uses: 5, uses_remaining: 2 })],
    })
    const { container } = await openDialog()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-link-row-t1"]'))
        .not.toBeNull()
    })
    expect(container.querySelector('[data-testid="invite-link-row-t1"]')!
      .textContent).toContain('2 of 5 uses left')
  })

  it('names the minter instead of printing their opaque user id', async () => {
    mockReads({
      tokens: [makeRow({ token: 't1', created_by: 'rvnmljabltqi4mp3a7vot3w7uzz' })],
      members: [{
        user_id: 'rvnmljabltqi4mp3a7vot3w7uzz',
        display_name: 'Maximiliana Featherstonehaugh-Wintergreen',
      }],
    })
    const { container } = await openDialog()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-link-row-t1"]')!
        .textContent).toContain('by Maximiliana Featherstonehaugh-Wintergreen')
    })
  })

  it('keeps the raw id when the minter is no longer a member', async () => {
    mockReads({
      tokens: [makeRow({ token: 't1', created_by: 'ghost-id' })],
      members: [{ user_id: 'someone-else', display_name: 'Someone Else' }],
    })
    const { container } = await openDialog()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-link-row-t1"]'))
        .not.toBeNull()
    })
    expect(container.querySelector('[data-testid="invite-link-row-t1"]')!
      .textContent).toContain('by ghost-id')
  })

  it('marks a published link with a 🌐 and offers a link copy', async () => {
    mockReads({
      tokens: [makeRow({
        token: 't1',
        gfs: {
          gfs_id: 'g', gfs_token: 'gt', url: 'https://relay.example.org/join/gt',
        },
      })],
    })
    const { container } = await openDialog()
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-link-row-t1"]'))
        .not.toBeNull()
    })
    const row = container.querySelector('[data-testid="invite-link-row-t1"]')!
    expect(row.textContent).toContain('🌐')
    expect(row.textContent).toContain('Copy link')
  })

  it('shows a retryable error state when the list fails to load', async () => {
    mockReads({ linksFail: true })
    const result = await openDialog()
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-links-error"]'))
        .not.toBeNull()
    })
    // Retry re-reads and recovers.
    mockReads({ tokens: [makeRow({ token: 't9' })] })
    await act(async () => { fireEvent.click(result.getByText('Try again')) })
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-link-row-t9"]'))
        .not.toBeNull()
    })
  })

  it('prepends a freshly minted link to the list', async () => {
    mockReads({ tokens: [makeRow({ token: 'old' })] })
    const result = await openDialog()
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-link-row-old"]'))
        .not.toBeNull()
    })
    await generate(result, makeRow({ token: 'fresh' }))
    const rows = result.container.querySelectorAll('.sh-invite-link-row-item')
    expect(rows[0].getAttribute('data-testid')).toBe('invite-link-row-fresh')
  })
})

describe('SpaceInviteDialog — revoke', () => {
  async function openWithLink() {
    mockReads({ tokens: [makeRow({ token: 't1' })] })
    const result = await openDialog()
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-link-row-t1"]'))
        .not.toBeNull()
    })
    return result
  }

  it('asks first, naming exactly what does and does not happen', async () => {
    const result = await openWithLink()
    confirmDialog.mockResolvedValueOnce(false)
    await act(async () => {
      fireEvent.click(result.container
        .querySelector('[data-testid="invite-revoke-t1"]')!)
    })
    expect(confirmDialog).toHaveBeenCalledWith(
      'This link stops working right away, here and on the GFS. People who '
      + 'already joined with it stay.',
      expect.objectContaining({ destructive: true }),
    )
    // Declined → the row stays and nothing was deleted.
    expect(api.delete).not.toHaveBeenCalled()
    expect(result.container.querySelector('[data-testid="invite-link-row-t1"]'))
      .not.toBeNull()
  })

  it('removes the row optimistically on confirm', async () => {
    const result = await openWithLink()
    confirmDialog.mockResolvedValueOnce(true)
    api.delete.mockResolvedValueOnce(undefined)
    await act(async () => {
      fireEvent.click(result.container
        .querySelector('[data-testid="invite-revoke-t1"]')!)
    })
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-link-row-t1"]'))
        .toBeNull()
    })
    expect(api.delete).toHaveBeenCalledWith(
      expect.stringContaining('/invite-tokens/t1'),
    )
  })

  it('rolls the row back when the revoke call fails', async () => {
    const result = await openWithLink()
    confirmDialog.mockResolvedValueOnce(true)
    api.delete.mockRejectedValueOnce(new Error('offline'))
    await act(async () => {
      fireEvent.click(result.container
        .querySelector('[data-testid="invite-revoke-t1"]')!)
    })
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith('offline', 'error')
    })
    // A failed revoke must not look like a successful one.
    expect(result.container.querySelector('[data-testid="invite-link-row-t1"]'))
      .not.toBeNull()
  })
})

describe('SpaceInviteDialog — a Follower link can be published', () => {
  const SERVERS = [{ id: 'gfs-1', display_name: 'Relay One', status: 'active' }]

  async function openWithServerAndPublish() {
    mockReads({ servers: SERVERS })
    const result = await openDialog()
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-publish-toggle"]'))
        .not.toBeNull()
    })
    await act(async () => {
      fireEvent.click(result.container
        .querySelector('[data-testid="invite-publish-toggle"]')!)
    })
    return result
  }

  it('keeps the Follower role pickable once publishing is on', async () => {
    const result = await openWithServerAndPublish()
    const follower = result.container
      .querySelector('[data-testid="invite-role-subscriber"]') as HTMLInputElement
    expect(follower.disabled).toBe(false)
    // The "not yet" hint is gone -- a household CAN join as a Follower now.
    expect(follower.closest('label')!.textContent).not.toContain(
      "Followers can't join from another household yet",
    )
    expect(follower.closest('label')!.textContent).toContain('reads only')
  })

  it('keeps Follower picked when publishing is switched on, and submits it',
    async () => {
      mockReads({ servers: SERVERS })
      const result = await openDialog()
      await waitFor(() => {
        expect(result.container.querySelector('[data-testid="invite-publish-toggle"]'))
          .not.toBeNull()
      })
      await act(async () => {
        fireEvent.click(result.container
          .querySelector('[data-testid="invite-role-subscriber"]')!)
      })
      await act(async () => {
        fireEvent.click(result.container
          .querySelector('[data-testid="invite-publish-toggle"]')!)
      })
      // No silent fallback to Member: the combination is real now.
      expect((result.container
        .querySelector('[data-testid="invite-role-subscriber"]') as HTMLInputElement)
        .checked).toBe(true)
      await generate(result, makeRow({ role: 'subscriber' }))
      expect(api.post).toHaveBeenCalledWith(
        expect.anything(),
        expect.objectContaining({ role: 'subscriber', publish_to_gfs: 'gfs-1' }),
      )
    })
})

describe('SpaceInviteDialog — a published never-expiring link', () => {
  const GFS = {
    gfs_id: 'gfs-1',
    gfs_token: 'gt1',
    url: 'https://relay.example.org/join/gt1',
  }

  it('qualifies "never lapses" with the 30-day cap on the web link', async () => {
    const result = await openDialog()
    await generate(result, makeRow({ expires_at: null, gfs: GFS }))
    const hint = result.container
      .querySelector('[data-testid="invite-published-never-hint"]')
    expect(hint).not.toBeNull()
    expect(hint!.textContent).toContain('30 days')
    expect(hint!.textContent).toContain('The code below keeps')
  })

  it('omits the qualifier for a never-expiring link that was NOT published', async () => {
    const result = await openDialog()
    await generate(result, makeRow({ expires_at: null, gfs: null }))
    expect(result.container
      .querySelector('[data-testid="invite-published-never-hint"]')).toBeNull()
  })

  it('omits the qualifier for a published link that does expire', async () => {
    const result = await openDialog()
    await generate(result, makeRow({ gfs: GFS }))
    expect(result.container
      .querySelector('[data-testid="invite-published-never-hint"]')).toBeNull()
  })
})

describe('SpaceInviteDialog — link type on a private space', () => {
  const privateSpace = (privateGfs: boolean) => ({
    space_type: 'private', features: { private_gfs: privateGfs },
  })
  const radio = (c: Element, id: 'gfs' | 'internal') =>
    c.querySelector(`[data-testid="invite-via-${id}"]`) as HTMLInputElement

  it('keeps today\'s form on a public space: no choice, no via sent', async () => {
    mockReads({ space: { space_type: 'public', features: {} } })
    const result = await openDialog({ role: 'owner' })
    await waitFor(() => expect(api.get).toHaveBeenCalled())
    expect(result.container.querySelector('[data-testid="invite-via"]')).toBeNull()
    await generate(result)
    expect(api.post.mock.calls[0][1]).not.toHaveProperty('via')
  })

  it('defaults to internal and disables the GFS link when off', async () => {
    mockReads({ space: privateSpace(false) })
    const result = await openDialog({ role: 'owner' })
    await waitFor(() => expect(radio(result.container, 'internal')).not.toBeNull())
    expect(radio(result.container, 'internal').checked).toBe(true)
    const gfs = radio(result.container, 'gfs')
    expect(gfs.checked).toBe(false)
    expect(gfs.disabled).toBe(true)
    expect(gfs.getAttribute('aria-describedby')).toBe('sh-invite-via-gfs-off')
    // The owner gets the way to turn it on, anchored on the app base.
    const hint = result.getByTestId('invite-via-gfs-off')
    expect(hint.textContent).toContain(
      "This space doesn't use the GFS.",
    )
    expect(hint.querySelector('a')!.getAttribute('href'))
      .toMatch(/^\/spaces\/space-[a-z0-9]+\/settings$/)
  })

  it('tells a non-owner admin that only the owner can turn it on', async () => {
    mockReads({ space: privateSpace(false) })
    const result = await openDialog({ role: 'admin' })
    const hint = await waitFor(() => result.getByTestId('invite-via-gfs-off'))
    expect(hint.textContent).toBe(
      "This space doesn't use the GFS. Only its owner can turn it on.",
    )
    expect(hint.querySelector('a')).toBeNull()
  })

  it('defaults to a GFS link when on, with both enabled', async () => {
    mockReads({ space: privateSpace(true) })
    const result = await openDialog({ role: 'owner' })
    await waitFor(() => expect(radio(result.container, 'gfs')?.checked).toBe(true))
    expect(radio(result.container, 'gfs').disabled).toBe(false)
    expect(radio(result.container, 'internal').disabled).toBe(false)
    expect(result.queryByTestId('invite-via-gfs-off')).toBeNull()
  })

  it('sends via: "internal" by default when off', async () => {
    mockReads({ space: privateSpace(false) })
    const result = await openDialog({ role: 'owner' })
    await waitFor(() => expect(radio(result.container, 'internal')).not.toBeNull())
    await generate(result, makeRow({ via: 'internal' }))
    expect(api.post.mock.calls[0]).toEqual([
      expect.stringContaining('/invite-tokens'),
      { role: 'member', uses: 1, ttl_seconds: 604_800, via: 'internal' },
    ])
    expect(result.getByTestId('invite-created-internal').textContent)
      .toContain('Local link')
  })

  it('sends the picked via when on', async () => {
    mockReads({ space: privateSpace(true) })
    const result = await openDialog({ role: 'owner' })
    await waitFor(() => expect(radio(result.container, 'gfs')?.checked).toBe(true))
    await act(async () => { fireEvent.click(radio(result.container, 'internal')) })
    await generate(result, makeRow({ via: 'internal' }))
    expect(api.post.mock.calls[0][1]).toMatchObject({ via: 'internal' })
  })

  it('hides publishing for an internal link and never sends publish_to_gfs', async () => {
    mockReads({
      space: privateSpace(true),
      servers: [{ id: 'g1', display_name: 'Relay One', status: 'active', inbox_url: 'https://g1' }],
    })
    const result = await openDialog({ role: 'owner' })
    const toggle = await waitFor(() => result.getByTestId('invite-publish-toggle'))
    await act(async () => { fireEvent.click(toggle) })
    await act(async () => { fireEvent.click(radio(result.container, 'internal')) })
    expect(result.queryByTestId('invite-publish-toggle')).toBeNull()
    // Back to a connection server link: the toggle returns, switched off.
    await act(async () => { fireEvent.click(radio(result.container, 'gfs')) })
    expect((result.getByTestId('invite-publish-toggle') as HTMLInputElement).checked)
      .toBe(false)
    await act(async () => { fireEvent.click(radio(result.container, 'internal')) })
    await generate(result, makeRow({ via: 'internal' }))
    expect(api.post.mock.calls[0][1]).not.toHaveProperty('publish_to_gfs')
    expect(api.post.mock.calls[0][1]).toMatchObject({ via: 'internal' })
  })

  it('toasts a 409 PRIVATE_GFS_OFF and falls back to a local link', async () => {
    mockReads({ space: privateSpace(true) })
    const result = await openDialog({ role: 'owner' })
    await waitFor(() => expect(radio(result.container, 'gfs')?.checked).toBe(true))
    const detail = "This private space doesn't use the GFS. Turn it on in the space settings, or create a local link."
    api.post.mockRejectedValueOnce(new ApiError(409, detail, 'PRIVATE_GFS_OFF'))
    await act(async () => { fireEvent.click(result.getByText('Create invite link')) })
    await waitFor(() => expect(showToast).toHaveBeenCalledWith(detail, 'error'))
    expect(radio(result.container, 'internal').checked).toBe(true)
    expect(radio(result.container, 'gfs').disabled).toBe(true)
    expect(result.container.querySelector('[data-testid="invite-code"]')).toBeNull()
  })

  it('toasts a 422 with the server detail', async () => {
    mockReads({ space: privateSpace(false) })
    const result = await openDialog({ role: 'owner' })
    await waitFor(() => expect(radio(result.container, 'internal')).not.toBeNull())
    api.post.mockRejectedValueOnce(new ApiError(422, 'via must be gfs or internal'))
    await act(async () => { fireEvent.click(result.getByText('Create invite link')) })
    await waitFor(() =>
      expect(showToast).toHaveBeenCalledWith('via must be gfs or internal', 'error'),
    )
  })
})

describe('SpaceInviteDialog — link type badge in the list', () => {
  it('labels each link with its type, and nothing for an older backend', async () => {
    mockReads({
      tokens: [
        makeRow({ token: 'tok-g', via: 'gfs' }),
        makeRow({ token: 'tok-i', via: 'internal' }),
        makeRow({ token: 'tok-old' }),
      ],
    })
    const result = await openDialog()
    await waitFor(() => result.getByTestId('invite-link-row-tok-g'))
    expect(result.getByTestId('invite-via-badge-tok-g').textContent)
      .toBe('GFS')
    expect(result.getByTestId('invite-via-badge-tok-i').textContent)
      .toBe('Local')
    expect(result.queryByTestId('invite-via-badge-tok-old')).toBeNull()
  })
})

describe('SpaceInviteDialog — grandfathered links', () => {
  it('marks an earlier link and says it turns the GFS on when used', async () => {
    mockReads({ tokens: [makeRow({ token: 'tok-legacy', via: 'gfs_legacy' })] })
    const result = await openDialog()
    await waitFor(() => result.getByTestId('invite-link-row-tok-legacy'))
    const badge = result.getByTestId('invite-via-badge-tok-legacy')
    expect(badge.textContent).toBe('Earlier link')
    expect(badge.getAttribute('title')).toMatch(/turns the GFS on/i)
  })
})

describe('SpaceInviteDialog — moderator links', () => {
  afterEach(async () => { await setLocale('en') })

  /** The role radios' visible labels, in display order. */
  function roleLabels(container: Element): string[] {
    return Array.from(container.querySelectorAll('.sh-invite-role__label'))
      .slice(0, 4)
      .map(el => el.textContent ?? '')
  }

  it('offers the owner Member, Follower, Moderator and Admin, in that order', async () => {
    const { container } = await openDialog({ role: 'owner' })
    const ids = Array.from(
      container.querySelectorAll('input[name="sh-invite-role"]'),
    ).map(el => (el as HTMLInputElement).value)
    expect(ids).toEqual(['member', 'subscriber', 'moderator', 'admin'])
    expect(roleLabels(container))
      .toEqual(['Member', 'Follower', 'Moderator', 'Admin'])
  })

  it('offers an admin Moderator with its hint, but not Admin', async () => {
    const { container } = await openDialog({ role: 'admin' })
    const radio = container.querySelector('[data-testid="invite-role-moderator"]')
    expect(radio).not.toBeNull()
    expect(radio!.closest('label')!.textContent).toContain(
      "reviews and removes posts, can't change settings",
    )
    expect(container.querySelector('[data-testid="invite-role-admin"]'))
      .toBeNull()
  })

  it('offers neither Moderator nor Admin when the opener\'s role is unknown', async () => {
    const { container } = await openDialog({ role: undefined })
    expect(container.querySelector('[data-testid="invite-role-moderator"]'))
      .toBeNull()
    expect(container.querySelector('[data-testid="invite-role-admin"]'))
      .toBeNull()
  })

  it('offers a moderator no Moderator or Admin choice', async () => {
    const { container } = await openDialog({ role: 'moderator' })
    expect(container.querySelector('[data-testid="invite-role-moderator"]'))
      .toBeNull()
    expect(container.querySelector('[data-testid="invite-role-admin"]'))
      .toBeNull()
  })

  it('sends role "moderator" when Moderator is picked', async () => {
    const result = await openDialog({ role: 'admin' })
    await act(async () => {
      fireEvent.click(result.container
        .querySelector('[data-testid="invite-role-moderator"]')!)
    })
    await generate(result, makeRow({ role: 'moderator' }))
    expect(api.post).toHaveBeenCalledWith(
      expect.stringContaining('/invite-tokens'),
      expect.objectContaining({ role: 'moderator' }),
    )
    expect(result.container.textContent).toContain('They join as a moderator.')
  })

  it('uses the translated role labels and hints', async () => {
    await setLocale('de')
    const { container } = await openDialog({ role: 'owner' })
    expect(roleLabels(container))
      .toEqual(['Mitglied', 'Follower', 'Moderator', 'Admin'])
    expect(container.querySelector('[data-testid="invite-role-member"]')!
      .closest('label')!.textContent).toContain('kann posten und mitmachen')
  })

  it('says the summary and row metadata in German, with singular forms', async () => {
    await setLocale('de')
    mockReads({ tokens: [makeRow({ token: 'tg', uses_remaining: 1, uses: null })] })
    const result = await openDialog({ role: 'owner' })
    expect(result.container.textContent).toContain('In den Raum einladen')
    expect(result.container.textContent).toContain('Beitreten als')
    await waitFor(() => {
      expect(result.container.querySelector('[data-testid="invite-link-row-tg"]'))
        .not.toBeNull()
    })
    const row = result.container.querySelector('[data-testid="invite-link-row-tg"]')!
    expect(row.textContent).toContain('noch 1 Platz')
    expect(row.textContent).toContain('läuft in 7 Tagen ab')
    expect(row.textContent).toContain('von pascal')
    expect(row.textContent).toContain('Widerrufen')
    api.post.mockResolvedValueOnce(makeRow({ uses_remaining: 1, role: 'subscriber' }))
    await act(async () => {
      fireEvent.click(result.getByText('Einladungslink erstellen'))
    })
    await waitFor(() => {
      expect(result.queryByTestId('invite-created-summary')).not.toBeNull()
    })
    expect(result.getByTestId('invite-created-summary').textContent).toBe(
      '1 Person kann damit beitreten. Der Link funktioniert in 7 Tagen nicht mehr.'
      + ' Sie treten als Follower bei.',
    )
  })

  it('uses the plural summary and says when a link never expires', async () => {
    const result = await openDialog({ role: 'owner' })
    await generate(result, makeRow({ uses_remaining: 4, expires_at: null }))
    expect(result.getByTestId('invite-created-summary').textContent).toBe(
      '4 people can join with it. It never expires. They join as a member.',
    )
  })

  it('shows a Moderator badge on a moderator link in the list', async () => {
    mockReads({ tokens: [makeRow({ token: 'tm', role: 'moderator' })] })
    const { container } = await openDialog({ role: 'owner' })
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-role-badge-tm"]'))
        .not.toBeNull()
    })
    expect(container.querySelector('[data-testid="invite-role-badge-tm"]')!
      .textContent).toBe('Moderator')
  })
})

describe('SpaceInviteDialog — the space\'s home household', () => {
  const OFFLINE = "The space's home household is offline right now. Try again later."
  const TOO_OLD = "The space's home household needs to update Social Home first."

  it('says the home household is offline when a create times out', async () => {
    const result = await openDialog({ role: 'admin' })
    api.post.mockRejectedValueOnce(
      new ApiError(503, 'host did not answer', 'HOST_UNREACHABLE'),
    )
    await act(async () => {
      fireEvent.click(result.getByText('Create invite link'))
    })
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(OFFLINE, 'error')
    })
  })

  it('says the home household must update when it is too old to create', async () => {
    const result = await openDialog({ role: 'admin' })
    api.post.mockRejectedValueOnce(
      new ApiError(409, 'host too old', 'HOST_TOO_OLD'),
    )
    await act(async () => {
      fireEvent.click(result.getByText('Create invite link'))
    })
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(TOO_OLD, 'error')
    })
  })

  it('names the reason when the links list fails on the home household', async () => {
    api.get.mockImplementation((url: string) => {
      if (url.endsWith('/invite-tokens')) {
        return Promise.reject(
          new ApiError(503, 'host did not answer', 'HOST_UNREACHABLE'),
        )
      }
      if (url === '/api/gfs/connections') return Promise.resolve([])
      if (url.endsWith('/members')) return Promise.resolve([])
      return Promise.resolve({ name: 'Space' })
    })
    const { container } = await openDialog({ role: 'admin' })
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-links-error-text"]')
        ?.textContent).toBe(OFFLINE)
    })
  })

  it('says the home household is unknown when there is no route to it yet', async () => {
    const result = await openDialog({ role: 'admin' })
    const err = new ApiError(503, 'unknown host', 'HOST_UNREACHABLE')
    Object.assign(err, { extra: { reason: 'unknown_host' } })
    api.post.mockRejectedValueOnce(err)
    await act(async () => {
      fireEvent.click(result.getByText('Create invite link'))
    })
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(
        "This space's host household isn't known here yet.", 'error',
      )
    })
  })

  it('keeps the generic list error for any other failure', async () => {
    mockReads({ linksFail: true })
    const { container } = await openDialog({ role: 'admin' })
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-links-error-text"]')
        ?.textContent).toBe("Couldn't load the links for this space.")
    })
  })

  it('says the home household must update when a revoke is refused', async () => {
    mockReads({ tokens: [makeRow({ token: 't1' })] })
    confirmDialog.mockResolvedValueOnce(true)
    api.delete.mockRejectedValueOnce(
      new ApiError(409, 'host too old', 'HOST_TOO_OLD'),
    )
    const { container } = await openDialog({ role: 'admin' })
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-revoke-t1"]'))
        .not.toBeNull()
    })
    await act(async () => {
      fireEvent.click(container
        .querySelector('[data-testid="invite-revoke-t1"]')!)
    })
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(TOO_OLD, 'error')
    })
    // The row comes back: the revoke didn't land.
    expect(container.querySelector('[data-testid="invite-link-row-t1"]'))
      .not.toBeNull()
  })

  it('says the home household is offline when a revoke times out', async () => {
    mockReads({ tokens: [makeRow({ token: 't1' })] })
    confirmDialog.mockResolvedValueOnce(true)
    api.delete.mockRejectedValueOnce(
      new ApiError(503, 'host did not answer', 'HOST_UNREACHABLE'),
    )
    const { container } = await openDialog({ role: 'admin' })
    await waitFor(() => {
      expect(container.querySelector('[data-testid="invite-revoke-t1"]'))
        .not.toBeNull()
    })
    await act(async () => {
      fireEvent.click(container
        .querySelector('[data-testid="invite-revoke-t1"]')!)
    })
    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(OFFLINE, 'error')
    })
  })
})
