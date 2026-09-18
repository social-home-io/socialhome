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
  ApiError: new (status: number, detail?: string | null) => Error
}
const { confirmDialog } = await import('./confirm') as unknown as {
  confirmDialog: ReturnType<typeof vi.fn>
}
const { showToast } = await import('./Toast') as unknown as {
  showToast: ReturnType<typeof vi.fn>
}
const { openSpaceInvite, SpaceInviteDialog } = await import('./SpaceInviteDialog')

type Row = Record<string, unknown>

/** Wire up ``api.get`` for the dialog's three reads: space detail,
 *  links list, connection servers. */
function mockReads(
  { tokens = [], servers = [], linksFail = false }:
  { tokens?: Row[]; servers?: Row[]; linksFail?: boolean } = {},
) {
  api.get.mockImplementation((url: string) => {
    if (url.endsWith('/invite-tokens')) {
      return linksFail
        ? Promise.reject(new Error('boom'))
        : Promise.resolve({ tokens })
    }
    if (url === '/api/gfs/connections') return Promise.resolve(servers)
    return Promise.resolve({ name: 'Fetched space name' })
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
  { hint?: string | null; role?: 'owner' | 'admin' | 'member' } = {},
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

describe('SpaceInviteDialog — publishing to a connection server', () => {
  it('hides the publish toggle when the household has no connection server', async () => {
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
      'Anyone with this link sees the space name and can request the code'
      + ' — they join from their own Social Home.',
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
      new ApiError(422, "This connection server can't relay invites yet."),
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
      .toContain("can't relay invites yet")
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

  it('populates via_gfs with the connection server BASE url when published', async () => {
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
    expect(row.textContent).toContain('lapses in 6 days')
    expect(container.querySelector('[data-testid="invite-link-row-t2"]')!
      .textContent).toContain('Follower')
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
      'This link stops working immediately, here and on the connection '
      + 'server. People who already joined with it stay.',
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

describe('SpaceInviteDialog — Follower is off the table while publishing', () => {
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

  it('disables the Follower role with the cross-household hint once publishing is on', async () => {
    const result = await openWithServerAndPublish()
    const follower = result.container
      .querySelector('[data-testid="invite-role-subscriber"]') as HTMLInputElement
    expect(follower.disabled).toBe(true)
    expect(follower.closest('label')!.textContent).toContain(
      "Followers can't join from another household yet",
    )
    // The other roles stay pickable.
    expect((result.container
      .querySelector('[data-testid="invite-role-member"]') as HTMLInputElement)
      .disabled).toBe(false)
  })

  it('leaves Follower pickable while the link is not published', async () => {
    mockReads({ servers: SERVERS })
    const result = await openDialog()
    const follower = result.container
      .querySelector('[data-testid="invite-role-subscriber"]') as HTMLInputElement
    expect(follower.disabled).toBe(false)
    expect(follower.closest('label')!.textContent).not.toContain(
      "Followers can't join from another household yet",
    )
  })

  it('falls back to Member when publishing is switched on with Follower picked', async () => {
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
    expect((result.container
      .querySelector('[data-testid="invite-role-member"]') as HTMLInputElement)
      .checked).toBe(true)
    // …and the impossible combination can never reach the backend.
    await generate(result, makeRow({ role: 'member' }))
    expect(api.post).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({ role: 'member', publish_to_gfs: 'gfs-1' }),
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
